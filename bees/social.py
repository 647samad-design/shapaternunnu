"""Sign in with Google and Sign in with Apple (OpenID Connect).

Turned on by adding the keys in Store settings > Keys & passwords:
  Google: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
  Apple:  APPLE_SERVICES_ID, APPLE_TEAM_ID, APPLE_KEY_ID, APPLE_PRIVATE_KEY
Redirect URLs to register with them are shown on that page.

* The authorization code is exchanged directly with Google/Apple over TLS
  using our secret, and the ID token is checked (issuer, audience, expiry,
  nonce); per OpenID Connect that makes the token trustworthy.
* An account with the same *verified* email is linked; otherwise a new
  account is created. Two-step sign-in is still asked when it's on.
* State and nonce live server-side (cache) for 10 minutes and are single-use.
"""
import base64
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from django.core.cache import cache
from django.urls import reverse

TIMEOUT = 20
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
APPLE_AUTH = "https://appleid.apple.com/auth/authorize"
APPLE_TOKEN = "https://appleid.apple.com/auth/token"


class SocialError(Exception):
    pass


def _env(name):
    from .multistore import env as _store_env
    value = _store_env(name)
    return value.replace("\\n", "\n") if name == "APPLE_PRIVATE_KEY" else value


def google_on():
    return bool(_env("GOOGLE_CLIENT_ID") and _env("GOOGLE_CLIENT_SECRET"))


def apple_on():
    return all(_env(k) for k in ("APPLE_SERVICES_ID", "APPLE_TEAM_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY"))


def providers():
    return [p for p, on in (("google", google_on()), ("apple", apple_on())) if on]


def redirect_uri(request, provider):
    return request.build_absolute_uri(reverse("social_callback", args=[provider]))


def start(request, provider, next_url):
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    cache.set(f"oidc:{state}", {"provider": provider, "nonce": nonce, "next": next_url}, 600)
    if provider == "google":
        params = {"client_id": _env("GOOGLE_CLIENT_ID"), "redirect_uri": redirect_uri(request, "google"), "response_type": "code",
                  "scope": "openid email profile", "state": state, "nonce": nonce, "prompt": "select_account"}
        return f"{GOOGLE_AUTH}?{urllib.parse.urlencode(params)}"
    params = {"client_id": _env("APPLE_SERVICES_ID"), "redirect_uri": redirect_uri(request, "apple"), "response_type": "code",
              "response_mode": "form_post", "scope": "name email", "state": state, "nonce": nonce}
    return f"{APPLE_AUTH}?{urllib.parse.urlencode(params)}"


def pop_state(state):
    key = f"oidc:{state}"
    data = cache.get(key)
    cache.delete(key)
    return data


def _post_form(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read() or b"{}")
        except ValueError:
            detail = {}
        raise SocialError(detail.get("error_description") or detail.get("error") or f"HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SocialError(f"couldn't reach the sign-in service ({exc})") from None


def _claims(id_token):
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        raise SocialError("unreadable ID token") from None


def _apple_secret():
    import jwt
    now = int(time.time())
    return jwt.encode({"iss": _env("APPLE_TEAM_ID"), "iat": now, "exp": now + 3600, "aud": "https://appleid.apple.com",
                       "sub": _env("APPLE_SERVICES_ID")}, _env("APPLE_PRIVATE_KEY"), algorithm="ES256",
                      headers={"kid": _env("APPLE_KEY_ID")})


def finish(request, provider, code, nonce):
    """Exchanges the code; returns dict(uid, email, email_verified, name)."""
    if provider == "google":
        data = _post_form(GOOGLE_TOKEN, {"code": code, "client_id": _env("GOOGLE_CLIENT_ID"), "client_secret": _env("GOOGLE_CLIENT_SECRET"),
                                         "redirect_uri": redirect_uri(request, "google"), "grant_type": "authorization_code"})
        issuers, audience = ("https://accounts.google.com", "accounts.google.com"), _env("GOOGLE_CLIENT_ID")
    else:
        data = _post_form(APPLE_TOKEN, {"code": code, "client_id": _env("APPLE_SERVICES_ID"), "client_secret": _apple_secret(),
                                        "redirect_uri": redirect_uri(request, "apple"), "grant_type": "authorization_code"})
        issuers, audience = ("https://appleid.apple.com",), _env("APPLE_SERVICES_ID")
    if not data.get("id_token"):
        raise SocialError("no ID token returned")
    c = _claims(data["id_token"])
    aud = c.get("aud")
    if c.get("iss") not in issuers or (aud != audience and audience not in (aud if isinstance(aud, list) else [])):
        raise SocialError("ID token is for another app")
    if int(c.get("exp", 0)) < time.time():
        raise SocialError("ID token expired")
    if c.get("nonce") != nonce:
        raise SocialError("sign-in request didn't match")
    verified = c.get("email_verified") in (True, "true")
    return {"uid": str(c.get("sub", "")), "email": (c.get("email") or "").lower(), "email_verified": verified,
            "name": c.get("name", ""), "given": c.get("given_name", ""), "family": c.get("family_name", "")}


def account_for(info, provider, extra_name=None):
    """Finds or creates the user for this sign-in. Returns (user, created)."""
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from django.utils import timezone
    from .models import Profile, SocialAccount
    User = get_user_model()
    if not info["uid"]:
        raise SocialError("missing account id")
    link = SocialAccount.objects.select_related("user").filter(provider=provider, uid=info["uid"]).first()
    if link:
        SocialAccount.objects.filter(pk=link.pk).update(last_login=timezone.now())
        if not link.user.is_active:
            raise SocialError("this account is disabled")
        return link.user, False
    if not info["email"] or not info["email_verified"]:
        raise SocialError("your email address isn't verified with " + provider.title())
    with transaction.atomic():
        user = User.objects.filter(email__iexact=info["email"]).order_by("id").first()
        created = False
        if user is None:
            base = "".join(ch for ch in info["email"].split("@")[0] if ch.isalnum() or ch in "._-")[:20] or "user"
            username = base
            while User.objects.filter(username__iexact=username).exists():
                username = f"{base}{secrets.randbelow(9000) + 1000}"
            first = info.get("given") or (extra_name or {}).get("firstName", "") or (info.get("name") or "").split(" ")[0]
            last = info.get("family") or (extra_name or {}).get("lastName", "")
            user = User(username=username, email=info["email"], first_name=first[:150], last_name=last[:150])
            user.set_unusable_password()
            user.save()
            created = True
        elif not user.is_active:
            raise SocialError("this account is disabled")
        profile, _ = Profile.objects.get_or_create(user=user)
        if not profile.email_verified:
            Profile.objects.filter(pk=profile.pk).update(email_verified=True)
        SocialAccount.objects.create(user=user, provider=provider, uid=info["uid"], email=info["email"], last_login=timezone.now())
    return user, created
