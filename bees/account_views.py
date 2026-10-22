"""Customer account settings: password & sign-in, saved cards, addresses,
privacy (email preferences, data download, account deletion) and the
seller's own store settings."""
import json
import secrets
import logging

from django.contrib import messages
from django.contrib.auth import logout as auth_logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import payments
from .models import (
    Address, AuditLog, ChatThread, NewsletterSubscriber, Notification, Order, OrganizationMember,
    Product, Profile, Question, Review, SellerAccount, Wishlist,
)
from .ratelimit import ratelimit
from .security import validate_image_upload

logger = logging.getLogger(__name__)

ADDRESS_FIELDS = ("full_name", "phone", "address", "city", "state", "postal_code", "country")
OPEN_ORDER_STATUSES = ("pending", "confirmed", "shipped")


def _profile(user):
    profile, _ = Profile.objects.get_or_create(user=user, defaults={"referral_code": secrets.token_hex(4).upper()})
    return profile


def _send(subject, template, context, user):
    from .views import _send_html_email
    _send_html_email(subject, template, context, user.email)


def _other_sessions(request):
    """Session keys of this user's other signed-in browsers."""
    keys = []
    current = request.session.session_key
    for session in Session.objects.filter(expire_date__gt=timezone.now()).iterator():
        if session.session_key == current:
            continue
        try:
            data = session.get_decoded()
        except Exception:
            continue
        if data.get("_auth_user_id") == str(request.user.pk):
            keys.append(session.session_key)
    return keys


# ---------------------------------------------------------------------------
# Security: password and devices
# ---------------------------------------------------------------------------

@login_required
@ratelimit("change_password", rate_limit=10, window_seconds=600, redirect_to="account_security",
           message="Too many attempts. Please wait a few minutes and try again.")
def security(request):
    form = PasswordChangeForm(request.user, request.POST or None)
    form.fields["old_password"].label = "Current password"
    form.fields["new_password2"].label = "Confirm new password"
    for field in form.fields.values():
        field.widget.attrs.update({"class": "input"})
    if request.method == "POST":
        action = request.POST.get("action", "password")
        if action in ("revoke_token", "revoke_all_tokens"):
            from .models import ApiToken
            tokens = ApiToken.objects.filter(user=request.user)
            if action == "revoke_token":
                tokens = tokens.filter(pk=request.POST.get("token") if str(request.POST.get("token", "")).isdigit() else 0)
            n = tokens.count()
            tokens.delete()
            AuditLog.objects.create(user=request.user, action=f"Revoked {n} API token{'s' if n != 1 else ''}")
            messages.success(request, f"Revoked {n} app/API sign-in{'s' if n != 1 else ''}." if n else "Nothing to revoke.")
            return redirect("account_security")
        if action == "signout_others":
            keys = _other_sessions(request)
            Session.objects.filter(session_key__in=keys).delete()
            AuditLog.objects.create(user=request.user, action="Signed out of other devices")
            messages.success(request, f"Signed out of {len(keys)} other device{'s' if len(keys) != 1 else ''}." if keys else "You weren't signed in anywhere else.")
            return redirect("account_security")
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)  # stay signed in here; other devices are signed out
            from .models import ApiToken
            ApiToken.objects.filter(user=user).delete()  # apps must sign in again with the new password
            AuditLog.objects.create(user=user, action="Changed password")
            if user.email:
                _send("Your password was changed", "bees/emails/security_notice.html", {
                    "user": user, "event": "Your password was changed",
                    "detail": "If this wasn't you, reset your password straight away and contact us.",
                    "reset_url": request.build_absolute_uri(reverse("password_reset")),
                }, user)
            messages.success(request, "Password changed. You've been signed out on your other devices and apps.")
            return redirect("account_security")
    profile = _profile(request.user)
    return render(request, "bees/account/security.html", {
        "tab": "security", "form": form, "other_sessions": len(_other_sessions(request)),
        "totp_enabled": profile.totp_enabled, "backup_left": len(profile.backup_codes or []),
        "twofa_required": _staff_must_use_2fa(request.user),
        "api_tokens": request.user.api_tokens.all()[:20],
        "passkeys": list(request.user.passkeys.all()),
    })


# ---------------------------------------------------------------------------
# Saved cards (stored by Stripe, never on our servers)
# ---------------------------------------------------------------------------

@login_required
def payment_methods(request):
    profile = _profile(request.user)
    enabled = payments.is_configured()
    if enabled and request.GET.get("added") and profile.stripe_customer_id:
        if payments.finish_card_setup(request.GET["added"], profile.stripe_customer_id):
            messages.success(request, "Card saved. You can pick it at checkout.")
        return redirect("payment_methods")
    cards, error = [], None
    if enabled and profile.stripe_customer_id:
        try:
            cards = payments.list_cards(profile.stripe_customer_id, user=request.user)
        except payments.PaymentError as exc:
            error = str(exc)
    return render(request, "bees/account/payment_methods.html", {
        "tab": "cards", "enabled": enabled, "cards": cards, "error": error,
    })


@login_required
@require_POST
def add_card(request):
    if not payments.is_configured():
        messages.error(request, "Card payments aren't available yet.")
        return redirect("payment_methods")
    customer_id = payments.ensure_customer(request.user)
    if not customer_id:
        messages.error(request, "We couldn't reach our payment provider. Please try again shortly.")
        return redirect("payment_methods")
    try:
        return redirect(payments.create_card_setup_session(request, customer_id, user=request.user))
    except payments.PaymentError as exc:
        reason = getattr(exc, "reason", "")
        # Staff see the technical reason so they can fix the setup.
        messages.error(request, str(exc) + (f" (Admin info: {reason})" if reason and request.user.is_staff else ""))
        return redirect("payment_methods")


@login_required
@require_POST
def remove_card(request):
    profile = _profile(request.user)
    if payments.remove_card(profile.stripe_customer_id, request.POST.get("card", "")):
        AuditLog.objects.create(user=request.user, action="Removed a saved card")
        messages.success(request, "Card removed.")
    else:
        messages.error(request, "That card couldn't be removed. Please try again.")
    return redirect("payment_methods")


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------

def _address_data(request):
    data = {f: request.POST.get(f, "").strip() for f in ADDRESS_FIELDS}
    data["country"] = data["country"].upper()[:2]
    missing = [f.replace("_", " ") for f in ("full_name", "phone", "address", "city", "country") if not data[f]]
    if missing:
        raise ValidationError(f"Please fill in: {', '.join(missing)}.")
    limits = {"full_name": 150, "phone": 30, "address": 255, "city": 100, "state": 100, "postal_code": 20}
    return {k: (v[:limits[k]] if k in limits else v) for k, v in data.items()}


def _make_default(user, address):
    Address.objects.filter(user=user).exclude(pk=address.pk).update(is_default=False)
    if not address.is_default:
        address.is_default = True
        address.save(update_fields=["is_default"])


@login_required
def edit_address(request, pk):
    address = get_object_or_404(Address, pk=pk, user=request.user)
    if request.method == "POST":
        try:
            data = _address_data(request)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return render(request, "bees/account/address_form.html", {"tab": "profile", "address": address, "form": request.POST})
        for key, value in data.items():
            setattr(address, key, value)
        address.label = request.POST.get("label", "").strip()[:30] or address.label
        address.save()
        if request.POST.get("is_default"):
            _make_default(request.user, address)
        messages.success(request, "Address updated.")
        return redirect(reverse("profile") + "#addresses")
    return render(request, "bees/account/address_form.html", {"tab": "profile", "address": address, "form": address})


@login_required
@require_POST
def default_address(request, pk):
    address = get_object_or_404(Address, pk=pk, user=request.user)
    _make_default(request.user, address)
    messages.success(request, f"'{address.label}' is now your default address. It's filled in for you at checkout.")
    return redirect(reverse("profile") + "#addresses")


# ---------------------------------------------------------------------------
# Privacy: emails, data download, delete account
# ---------------------------------------------------------------------------

def _seller_block(user):
    if SellerAccount.objects.filter(user=user).exists():
        return "You have a seller account. To close it, contact us so we can settle payouts and remove your products first."
    if OrganizationMember.objects.filter(user=user).exists():
        return "You're part of a store's team. Ask the store owner to remove you from the team first."
    return ""


def _sms_on():
    from . import sms
    from .models import SiteSettings
    return sms.is_configured() and SiteSettings.load().sms_customer_updates


@login_required
def privacy(request):
    email = (request.user.email or "").lower()
    if request.method == "POST" and request.POST.get("action") == "emails":
        from .translations import TRANSLATIONS
        lang = request.POST.get("language", "")
        Profile.objects.filter(pk=_profile(request.user).pk).update(
            cart_reminders=bool(request.POST.get("cart_reminders")),
            review_emails=bool(request.POST.get("review_emails")),
            price_drop_alerts=bool(request.POST.get("price_drop_alerts")),
            sms_updates=bool(request.POST.get("sms_updates")),
            language=lang if lang in TRANSLATIONS else "")
        wants = bool(request.POST.get("marketing"))
        if wants and email:
            NewsletterSubscriber.objects.get_or_create(email=email)
        else:
            NewsletterSubscriber.objects.filter(email__iexact=email).delete()
        messages.success(request, "Preferences saved." + ("" if email or not wants else " Add an email address to your profile first."))
        return redirect("account_privacy")
    open_orders = Order.objects.filter(user=request.user, status__in=OPEN_ORDER_STATUSES).count()
    return render(request, "bees/account/privacy.html", {
        "tab": "privacy",
        "subscribed": bool(email) and NewsletterSubscriber.objects.filter(email__iexact=email).exists(),
        "cart_reminders": _profile(request.user).cart_reminders,
        "review_emails": _profile(request.user).review_emails,
        "price_drop_alerts": _profile(request.user).price_drop_alerts,
        "sms_updates": _profile(request.user).sms_updates,
        "sms_on": _sms_on(),
        "email_language": _profile(request.user).language,
        "open_orders": open_orders,
        "seller_block": _seller_block(request.user),
    })


@login_required
def download_data(request):
    """Everything we hold about this customer, as JSON (GDPR-style export)."""
    user = request.user
    profile = _profile(user)
    data = {
        "exported_at": timezone.now().isoformat(),
        "account": {
            "username": user.username, "name": user.get_full_name(), "email": user.email,
            "joined": user.date_joined.isoformat(), "last_login": user.last_login.isoformat() if user.last_login else None,
            "phone": profile.phone, "email_verified": profile.email_verified,
            "reward_points": profile.loyalty_points, "store_credit": str(profile.store_credit),
            "referral_code": profile.referral_code,
        },
        "addresses": [
            {"label": a.label, **{f: getattr(a, f) for f in ADDRESS_FIELDS}, "default": a.is_default}
            for a in Address.objects.filter(user=user)
        ],
        "orders": [
            {
                "id": o.id, "date": o.created_at.isoformat(), "status": o.status, "payment": o.payment_status,
                "payment_method": o.payment_method, "total": str(o.total), "currency": o.currency,
                "ship_to": {"name": o.full_name, "address": o.address, "city": o.city, "state": o.state,
                            "postal_code": o.postal_code, "country": o.country, "phone": o.phone},
                "items": [{"product": i.product_name, "quantity": i.quantity, "price": str(i.price)} for i in o.items.all()],
            }
            for o in Order.objects.filter(user=user).prefetch_related("items")
        ],
        "reviews": [{"product": r.product.name, "rating": r.rating, "comment": r.comment, "date": r.created_at.isoformat()}
                    for r in Review.objects.filter(user=user).select_related("product")],
        "questions": [{"product": q.product.name, "question": q.question, "answer": q.answer}
                      for q in Question.objects.filter(user=user).select_related("product")],
        "wishlist": list(Wishlist.objects.filter(user=user).values_list("product__name", flat=True)),
        "newsletter": NewsletterSubscriber.objects.filter(email__iexact=user.email or "-").exists(),
    }
    response = HttpResponse(json.dumps(data, indent=2), content_type="application/json")
    response["Content-Disposition"] = f'attachment; filename="my-data-{user.username}.json"'
    AuditLog.objects.create(user=user, action="Downloaded personal data")
    return response


@login_required
@require_POST
@ratelimit("delete_account", rate_limit=5, window_seconds=600, redirect_to="account_privacy")
def delete_account(request):
    user = request.user
    if not user.check_password(request.POST.get("password", "")):
        messages.error(request, "That password isn't right, so your account was not deleted.")
        return redirect("account_privacy")
    blocked = _seller_block(user)
    if user.is_staff:
        blocked = "Staff accounts can't be deleted here."
    if not blocked and Order.objects.filter(user=user, status__in=OPEN_ORDER_STATUSES).exists():
        blocked = "You have orders that haven't been delivered yet. Cancel them or wait until they arrive, then try again."
    if blocked:
        messages.error(request, blocked)
        return redirect("account_privacy")

    profile = _profile(user)
    payments.delete_customer(profile.stripe_customer_id)  # removes saved cards at Stripe
    email = user.email
    with transaction.atomic():
        # Orders stay (stores must keep sales records) but are no longer
        # linked to a person who can sign in.
        Address.objects.filter(user=user).delete()
        Wishlist.objects.filter(user=user).delete()
        Notification.objects.filter(user=user).delete()
        ChatThread.objects.filter(user=user).delete()
        Review.objects.filter(user=user).update(user=None, username="Former customer")
        Question.objects.filter(user=user).update(user=None, username="Former customer")
        if email:
            NewsletterSubscriber.objects.filter(email__iexact=email).delete()
        Profile.objects.filter(pk=profile.pk).update(
            phone="", stripe_customer_id="", store_credit=0, loyalty_points=0, email_verified=False,
            saved_cart={}, cart_updated_at=None, cart_reminders=False, review_emails=False,
        )
        user.username = f"deleted-{user.pk}-{secrets.token_hex(3)}"
        user.email = ""
        user.first_name = ""
        user.last_name = ""
        user.is_active = False
        user.set_unusable_password()
        user.save()
        AuditLog.objects.create(user=user, action="Deleted account")
    auth_logout(request)
    messages.success(request, "Your account has been deleted. We're sorry to see you go.")
    return redirect("home")


# ---------------------------------------------------------------------------
# Seller: store settings
# ---------------------------------------------------------------------------

STORE_TEXT_FIELDS = {
    "store_description": 5000, "phone": 30, "business_address": 255, "city": 100, "country": 100,
    "bank_details": 255, "tax_info": 100, "brand_info": 5000, "pickup_state": 100, "pickup_postal_code": 20,
    "company_registration": 60, "whatsapp": 20,
}


@login_required
def store_settings(request):
    from .views import get_seller_account_for_user
    seller, role = get_seller_account_for_user(request.user)
    if not seller:
        return redirect("become_seller")
    if role not in ("owner", "admin"):
        messages.error(request, "Only the store owner or a team admin can change store settings.")
        return redirect("seller_dashboard")
    if request.method == "POST":
        name = request.POST.get("store_name", "").strip()[:150]
        try:
            if not name:
                raise ValidationError("Store name is required.")
            taken = SellerAccount.objects.exclude(pk=seller.pk).filter(business_name__iexact=name).exists() or \
                SellerAccount.objects.exclude(pk=seller.pk).filter(organization_name__iexact=name).exists()
            if taken:
                raise ValidationError("Another store already uses that name.")
            for field in ("store_logo", "store_banner"):
                validate_image_upload(request.FILES.get(field))
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect("seller_store_settings")
        old_name = seller.display_name
        with transaction.atomic():
            if seller.account_type == "organization" and not seller.business_name:
                seller.organization_name = name
            else:
                seller.business_name = name
            for field, limit in STORE_TEXT_FIELDS.items():
                setattr(seller, field, request.POST.get(field, "").strip()[:limit])
            if "pickup_latitude" in request.POST:
                from . import maps
                seller.pickup_latitude, seller.pickup_longitude = maps.from_post(request, "pickup_")
            trader_email = request.POST.get("trader_email", "").strip()[:254]
            try:
                if trader_email:
                    from django.core.validators import validate_email
                    validate_email(trader_email)
                seller.trader_email = trader_email
            except ValidationError:
                messages.warning(request, "The public contact email wasn't valid, so it wasn't changed.")
            seller.digest_emails = bool(request.POST.get("digest_emails"))
            if request.POST.get("trader_declared") and not seller.trader_declared_at:
                seller.trader_declared_at = timezone.now()
            elif not request.POST.get("trader_declared"):
                seller.trader_declared_at = None
            from .images import BANNER_MAX, LOGO_MAX, optimize
            for field in ("store_logo", "store_banner"):
                if request.FILES.get(field):
                    setattr(seller, field, optimize(request.FILES[field], LOGO_MAX if field == "store_logo" else BANNER_MAX))
                elif request.POST.get(f"remove_{field}"):
                    setattr(seller, field, None)
            seller.save()
            if seller.display_name != old_name:
                Product.objects.filter(seller_account=seller).update(seller_name=seller.display_name)
        AuditLog.objects.create(user=request.user, action=f"Updated store settings for {seller.display_name}")
        messages.success(request, "Store settings saved.")
        return redirect("seller_store_settings")
    from .seller_views import _render as seller_render
    request.seller, request.seller_role = seller, role
    from .ops import identity_available
    return seller_render(request, "settings.html", {"section": "settings", "identity_on": identity_available()})


# ---------------------------------------------------------------------------
# Two-step sign-in
# ---------------------------------------------------------------------------

def _staff_must_use_2fa(user):
    from .models import SiteSettings
    return user.is_staff and SiteSettings.load().require_staff_2fa


@login_required
def two_factor_setup(request):
    from . import twofactor
    from .models import SiteSettings
    profile = _profile(request.user)
    if profile.totp_enabled:
        return redirect("account_security")
    secret = request.session.get("totp_setup_secret")
    if not secret:
        secret = twofactor.new_secret()
        request.session["totp_setup_secret"] = secret
    if request.method == "POST":
        step = twofactor.verify(secret, request.POST.get("code", ""))
        if step is None:
            messages.error(request, "That code didn't match. Check the time on your phone is set automatically, then try the newest code.")
        else:
            codes, hashes = twofactor.new_backup_codes()
            Profile.objects.filter(pk=profile.pk).update(
                totp_secret=secret, totp_enabled=True, totp_last_step=step, backup_codes=hashes)
            request.session.pop("totp_setup_secret", None)
            request.session["2fa_verified"] = True
            AuditLog.objects.create(user=request.user, action="Turned on two-step sign-in")
            if request.user.email:
                _send("Two-step sign-in is on", "bees/emails/security_notice.html", {
                    "user": request.user, "event": "Two-step sign-in was turned on for your account",
                    "detail": "From now on you'll enter a code from your authenticator app when you sign in. If this wasn't you, contact us straight away.",
                    "reset_url": request.build_absolute_uri(reverse("password_reset")),
                }, request.user)
            return render(request, "bees/account/backup_codes.html", {"tab": "security", "codes": codes, "first_time": True})
    account = request.user.email or request.user.username
    uri = twofactor.provisioning_uri(secret, account, SiteSettings.load().site_name)
    return render(request, "bees/account/two_factor_setup.html", {
        "tab": "security", "secret": secret, "qr": twofactor.qr_svg(uri),
        "required": _staff_must_use_2fa(request.user),
        "secret_spaced": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
    })


def _check_password_and_code(request, profile):
    from . import twofactor
    if not request.user.check_password(request.POST.get("password", "")):
        return "That password isn't right."
    code = request.POST.get("code", "")
    step = twofactor.verify(profile.totp_secret, code, profile.totp_last_step)
    if step is not None:
        Profile.objects.filter(pk=profile.pk).update(totp_last_step=step)
        return ""
    remaining = twofactor.use_backup_code(profile.backup_codes, code)
    if remaining is not None:
        Profile.objects.filter(pk=profile.pk).update(backup_codes=remaining)
        return ""
    return "That code didn't match."


@login_required
@require_POST
@ratelimit("2fa_manage", rate_limit=10, window_seconds=600, redirect_to="account_security")
def two_factor_disable(request):
    profile = _profile(request.user)
    if not profile.totp_enabled:
        return redirect("account_security")
    error = _check_password_and_code(request, profile)
    if error:
        messages.error(request, error)
        return redirect("account_security")
    Profile.objects.filter(pk=profile.pk).update(totp_enabled=False, totp_secret="", backup_codes=[], totp_last_step=0)
    AuditLog.objects.create(user=request.user, action="Turned off two-step sign-in")
    if _staff_must_use_2fa(request.user):
        messages.info(request, "Two-step sign-in is off. Set it up again (for example on your new phone) before opening the store admin.")
        return redirect("two_factor_setup")
    messages.success(request, "Two-step sign-in is off.")
    return redirect("account_security")


@login_required
@require_POST
@ratelimit("2fa_manage", rate_limit=10, window_seconds=600, redirect_to="account_security")
def two_factor_new_codes(request):
    from . import twofactor
    profile = _profile(request.user)
    if not profile.totp_enabled:
        return redirect("account_security")
    error = _check_password_and_code(request, profile)
    if error:
        messages.error(request, error)
        return redirect("account_security")
    codes, hashes = twofactor.new_backup_codes()
    Profile.objects.filter(pk=profile.pk).update(backup_codes=hashes)
    AuditLog.objects.create(user=request.user, action="Created new backup codes")
    return render(request, "bees/account/backup_codes.html", {"tab": "security", "codes": codes})


@ratelimit("2fa_login", rate_limit=10, window_seconds=300, redirect_to="login",
           message="Too many code attempts. Please wait a few minutes and sign in again.")
def login_code(request):
    """Second sign-in step: the code from the authenticator app."""
    from django.contrib.auth import get_user_model, login as auth_login
    from . import twofactor
    from .security import safe_next_url
    pending = request.session.get("2fa_pending")
    if not pending or pending.get("expires", 0) < timezone.now().timestamp():
        request.session.pop("2fa_pending", None)
        messages.error(request, "Please sign in again.")
        return redirect("login")
    user = get_user_model().objects.filter(pk=pending["uid"], is_active=True).first()
    profile = Profile.objects.filter(user=user).first() if user else None
    if not user or not profile or not profile.totp_enabled:
        request.session.pop("2fa_pending", None)
        return redirect("login")
    if request.method == "POST":
        code = request.POST.get("code", "")
        ok = False
        step = twofactor.verify(profile.totp_secret, code, profile.totp_last_step)
        if step is not None:
            ok = Profile.objects.filter(pk=profile.pk, totp_last_step__lt=step).update(totp_last_step=step) == 1
        else:
            remaining = twofactor.use_backup_code(profile.backup_codes, code)
            if remaining is not None:
                Profile.objects.filter(pk=profile.pk).update(backup_codes=remaining)
                ok = True
                if not remaining:
                    messages.warning(request, "That was your last backup code. Create new ones under Password & security.")
        if ok:
            request.session.pop("2fa_pending", None)
            auth_login(request, user, backend=pending["backend"])
            request.session["2fa_verified"] = True
            if not pending.get("remember"):
                request.session.set_expiry(0)
            return redirect(safe_next_url(request, pending.get("next"), reverse("home")))
        pending["tries"] = pending.get("tries", 0) + 1
        if pending["tries"] >= 5:
            request.session.pop("2fa_pending", None)
            messages.error(request, "Too many wrong codes. Please sign in again.")
            return redirect("login")
        request.session["2fa_pending"] = pending
        messages.error(request, "That code didn't match. Use the newest code from your app, or a backup code.")
    return render(request, "bees/auth/login_code.html", {})


@login_required
def referrals(request):
    """The customer's referral dashboard: their link, share buttons, who
    joined, and the rewards they've earned."""
    from .models import Coupon, Order, SiteSettings
    site = SiteSettings.load()
    if not site.referral_enabled:
        messages.info(request, "The referral program isn't running right now.")
        return redirect("profile")
    profile = _profile(user=request.user)
    friends = list(Profile.objects.filter(referred_by=profile.referral_code).exclude(user=request.user)
                   .select_related("user").order_by("-user__date_joined")[:100])
    for f in friends:
        name = f.user.first_name or f.user.username
        f.masked = name[:2] + "•" * max(len(name) - 2, 2)
    rewards = list(Coupon.objects.filter(owner=request.user, purpose__in=["referral_reward", "referral_welcome"]).order_by("-id"))
    used_codes = set(Order.objects.filter(coupon_code__in=[c.code for c in rewards]).exclude(status="cancelled")
                     .values_list("coupon_code", flat=True))
    today = timezone.localdate()
    for c in rewards:
        c.state = "used" if c.code in used_codes else ("expired" if c.expiry_date and c.expiry_date < today else "ready")
    from . import loyalty
    link = request.build_absolute_uri(reverse("signup")) + f"?ref={profile.referral_code}"
    shop_link = request.build_absolute_uri(reverse("home")) + f"?ref={profile.referral_code}"
    return render(request, "bees/account/referrals.html", {
        "tab": "referrals", "profile": profile, "link": link, "shop_link": shop_link,
        "friends": friends, "rewards": rewards,
        "stats": {"joined": len(friends), "bought": sum(1 for f in friends if f.referral_rewarded),
                  "earned": sum(1 for c in rewards if c.purpose == "referral_reward")},
        "site": site,
        "affiliate": loyalty.affiliate_stats(profile) if site.affiliate_percent else None,
        "share_text": f"Here's {site.referral_friend_percent}% off your first order at {site.site_name}: {link}",
        "share_short": f"Get {site.referral_friend_percent}% off your first order at {site.site_name}",
    })


# ---------------------------------------------------------------------------
# Sign in with Google / Apple
# ---------------------------------------------------------------------------

def social_start(request, provider):
    from . import social
    from .security import safe_next_url
    if provider not in social.providers():
        messages.error(request, "That sign-in option isn't available.")
        return redirect("login")
    next_url = safe_next_url(request, request.GET.get("next"), reverse("home"))
    return redirect(social.start(request, provider, next_url))


@csrf_exempt  # Apple posts back from its own site; protected by the single-use state + nonce instead
def social_callback(request, provider):
    import json as _json
    from django.contrib.auth import login as auth_login
    from . import social
    from .security import safe_next_url
    data = request.POST if request.method == "POST" else request.GET
    pending = social.pop_state(data.get("state", ""))
    if not pending or pending.get("provider") != provider or provider not in social.providers():
        messages.error(request, "That sign-in link expired. Please try again.")
        return redirect("login")
    if data.get("error"):
        messages.info(request, "Sign-in was cancelled.")
        return redirect("login")
    try:
        info = social.finish(request, provider, data.get("code", ""), pending["nonce"])
        extra = {}
        if provider == "apple" and data.get("user"):
            try:
                extra = (_json.loads(data["user"]) or {}).get("name") or {}
            except ValueError:
                extra = {}
        user, created = social.account_for(info, provider, extra)
    except social.SocialError as exc:
        messages.error(request, f"Couldn't sign in with {provider.title()}: {exc}.")
        return redirect("login")
    next_url = safe_next_url(request, pending.get("next"), reverse("home"))
    backend = "django.contrib.auth.backends.ModelBackend"
    if Profile.objects.filter(user=user, totp_enabled=True).exists():
        request.session["2fa_pending"] = {"uid": user.pk, "backend": backend, "next": next_url, "remember": True,
                                          "tries": 0, "expires": timezone.now().timestamp() + 300}
        return redirect("login_code")
    auth_login(request, user, backend=backend)
    AuditLog.objects.create(user=user, action=f"Signed in with {provider.title()}" + (" (new account)" if created else ""))
    if created:
        messages.success(request, f"Welcome, {user.first_name or user.username}! Your account is ready.")
    return redirect(next_url)
