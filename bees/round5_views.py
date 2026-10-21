"""Passkey sign-in, chat translation and the Sales channels admin page."""
import base64
import json
import logging
import urllib.request

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .manage_views import _render, staff_required
from .ratelimit import ratelimit
from .multistore import store_settings  # per-store values in multi-store mode

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- helpers

def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text):
    text = str(text or "")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _rp(request):
    """Relying party id and origin: the site's own host."""
    host = request.get_host().split(":")[0]
    origin = f"{request.scheme}://{request.get_host()}"
    return getattr(settings, "PASSKEY_RP_ID", "") or host, origin


def _json_body(request):
    try:
        return json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return {}


# ---------------------------------------------------------------- passkeys

@login_required
@require_POST
@ratelimit("passkey_add", rate_limit=10, window_seconds=300)
def passkey_register_options(request):
    from webauthn import generate_registration_options, options_to_json
    from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                          ResidentKeyRequirement, UserVerificationRequirement)
    from .models import SiteSettings
    from .views import _is_demo_user
    if _is_demo_user(request.user):
        return JsonResponse({"ok": False, "error": "Demo accounts can't add passkeys."}, status=403)
    rp_id, _origin = _rp(request)
    user = request.user
    options = generate_registration_options(
        rp_id=rp_id, rp_name=SiteSettings.load().site_name[:60],
        user_id=str(user.pk).encode(), user_name=(user.email or user.username)[:64],
        user_display_name=(user.get_full_name() or user.username)[:64],
        exclude_credentials=[PublicKeyCredentialDescriptor(id=_unb64(k.credential_id)) for k in user.passkeys.all()],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.PREFERRED),
    )
    request.session["passkey_reg"] = _b64(options.challenge)
    return JsonResponse(json.loads(options_to_json(options)))


@login_required
@require_POST
@ratelimit("passkey_add", rate_limit=10, window_seconds=300)
def passkey_register(request):
    from webauthn import verify_registration_response
    from .models import AuditLog, Passkey
    challenge = request.session.pop("passkey_reg", None)
    body = _json_body(request)
    if not challenge or not body.get("credential"):
        return JsonResponse({"ok": False, "error": "Please try again."}, status=400)
    rp_id, origin = _rp(request)
    try:
        result = verify_registration_response(credential=body["credential"], expected_challenge=_unb64(challenge),
                                              expected_rp_id=rp_id, expected_origin=origin, require_user_verification=False)
    except Exception as exc:  # the library raises several error types
        logger.info("Passkey registration failed: %s", exc)
        return JsonResponse({"ok": False, "error": "That passkey couldn't be saved. Please try again."}, status=400)
    cred_id = _b64(result.credential_id)
    if Passkey.objects.filter(credential_id=cred_id).exists():
        return JsonResponse({"ok": False, "error": "This passkey is already saved."}, status=400)
    transports = (body["credential"].get("response") or {}).get("transports") or []
    name = " ".join(str(body.get("name") or "").split())[:60] or _device_name(request)
    Passkey.objects.create(user=request.user, credential_id=cred_id, public_key=_b64(result.credential_public_key),
                           sign_count=result.sign_count, transports=[t for t in transports if isinstance(t, str)][:6], name=name)
    AuditLog.objects.create(user=request.user, action=f"Added a passkey ({name})")
    _notify(request.user, "A passkey was added to your account",
            f"You can now sign in with \"{name}\". If this wasn't you, remove it under Password & security and change your password.")
    return JsonResponse({"ok": True})


def _device_name(request):
    agent = request.META.get("HTTP_USER_AGENT", "")
    for key, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android phone"), ("Macintosh", "Mac"),
                      ("Windows", "Windows PC"), ("CrOS", "Chromebook"), ("Linux", "Linux computer")):
        if key in agent:
            return name
    return "Passkey"


def _notify(user, event, detail):
    if not user.email:
        return
    try:
        from .account_views import _send
        _send(event, "bees/emails/security_notice.html", {"user": user, "event": event, "detail": detail}, user)
    except Exception:
        logger.exception("Security email failed")


@login_required
@require_POST
def passkey_delete(request, pk):
    from .models import AuditLog
    key = get_object_or_404(request.user.passkeys, pk=pk)
    name = key.name
    key.delete()
    AuditLog.objects.create(user=request.user, action=f"Removed a passkey ({name})")
    messages.success(request, f"Passkey \"{name}\" removed.")
    return redirect("account_security")


@require_POST
@ratelimit("passkey_login", rate_limit=20, window_seconds=300)
def passkey_login_options(request):
    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers.structs import UserVerificationRequirement
    rp_id, _origin = _rp(request)
    options = generate_authentication_options(rp_id=rp_id, user_verification=UserVerificationRequirement.PREFERRED)
    request.session["passkey_auth"] = {"challenge": _b64(options.challenge), "expires": timezone.now().timestamp() + 300}
    return JsonResponse(json.loads(options_to_json(options)))


@require_POST
@ratelimit("passkey_login", rate_limit=20, window_seconds=300)
def passkey_login(request):
    from webauthn import verify_authentication_response
    from .models import AuditLog, Passkey
    from .security import safe_next_url
    pending = request.session.pop("passkey_auth", None)
    body = _json_body(request)
    fail = JsonResponse({"ok": False, "error": "That passkey didn't work. Try again, or sign in with your password."}, status=400)
    if not pending or pending.get("expires", 0) < timezone.now().timestamp() or not body.get("credential"):
        return fail
    cred = body["credential"]
    key = Passkey.objects.select_related("user").filter(credential_id=str(cred.get("id") or cred.get("rawId") or "")).first()
    if not key or not key.user.is_active:
        return fail
    rp_id, origin = _rp(request)
    try:
        result = verify_authentication_response(
            credential=cred, expected_challenge=_unb64(pending["challenge"]), expected_rp_id=rp_id, expected_origin=origin,
            credential_public_key=_unb64(key.public_key), credential_current_sign_count=key.sign_count,
            require_user_verification=False)
    except Exception as exc:
        logger.info("Passkey sign-in failed: %s", exc)
        return fail
    key.sign_count = result.new_sign_count
    key.last_used_at = timezone.now()
    key.save(update_fields=["sign_count", "last_used_at"])
    auth_login(request, key.user, backend="django.contrib.auth.backends.ModelBackend")
    AuditLog.objects.create(user=key.user, action=f"Signed in with a passkey ({key.name})")
    next_url = safe_next_url(request, body.get("next"), reverse("home"))
    return JsonResponse({"ok": True, "next": next_url})


# ---------------------------------------------------------------- chat translation

LANG_NAMES = {"en": "English", "ur": "Urdu (Urdu script)", "roman": "Roman Urdu (Urdu written in Latin letters)",
              "ar": "Arabic", "es": "Spanish", "fr": "French"}


def translate_text(text, lang):
    """Translates a chat message with Claude. '' on any problem."""
    if not getattr(store_settings, "AI_API_KEY", ""):
        return ""
    target = LANG_NAMES.get(lang, "English")
    prompt = (f"Translate this message from an online shop chat into {target}. Keep the meaning, tone, numbers, "
              "sizes and names exactly. Reply with only the translation.\n\n" + text)
    body = json.dumps({"model": settings.AI_MODEL, "max_tokens": 800,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST", headers={
        "x-api-key": store_settings.AI_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            out = json.loads(resp.read().decode())
    except Exception:
        logger.exception("Chat translation failed")
        return ""
    return "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text").strip()[:3000]


@login_required
@require_POST
@ratelimit("translate", rate_limit=40, window_seconds=60)
def translate_message(request, pk):
    from .messaging import side_for
    from .models import Message
    msg = get_object_or_404(Message.objects.select_related("conversation__seller"), pk=pk)
    if not request.user.is_staff and not side_for(request.user, msg.conversation):
        raise Http404
    if not msg.body:
        return JsonResponse({"ok": False, "error": "Nothing to translate."}, status=400)
    lang = getattr(request, "shop_lang", "") or request.session.get("site_lang") or "en"
    lang = request.POST.get("lang") if request.POST.get("lang") in LANG_NAMES else lang
    key = f"chat_tr:{msg.pk}:{lang}"
    text = cache.get(key)
    if text is None:
        text = translate_text(msg.body, lang)
        if not text:
            return JsonResponse({"ok": False, "error": "Translation isn't available right now."}, status=503)
        cache.set(key, text, 60 * 60 * 24 * 30)
    return JsonResponse({"ok": True, "text": text, "lang": LANG_NAMES.get(lang, "English")})


# ---------------------------------------------------------------- sales channels (admin)

@staff_required
def sales_channels(request):
    from . import feeds
    from .models import AgentCheckout, SiteSettings
    site = SiteSettings.load()
    from datetime import timedelta
    since = timezone.now() - timedelta(days=30)
    agents = AgentCheckout.objects.filter(created_at__gte=since)
    return _render(request, "channels.html", {
        "section": "channels", "site": site, "ready": feeds.readiness(),
        "google_url": request.build_absolute_uri(reverse("feed_google")),
        "csv_url": request.build_absolute_uri(reverse("feed_csv")),
        "ucp_url": request.build_absolute_uri(reverse("ucp_profile")),
        "llms_url": request.build_absolute_uri(reverse("llms_txt")),
        "agent_count": agents.count(), "agent_opened": agents.exclude(opened_at=None).count(),
        "ai_on": bool(getattr(store_settings, "AI_API_KEY", "")),
    })
