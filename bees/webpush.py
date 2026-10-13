"""Web push notifications (phone / desktop notifications, even when the site
is closed) - RFC 8291 message encryption and RFC 8292 VAPID, written with the
``cryptography`` package only, so nothing that needs compiling is required.

Keys: VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY in Keys & passwords (the page has a
"Create push keys" button). Every site Notification (order updates, new
messages, payouts...) is also pushed to the person's devices that opted in.
"""
import base64
import json
import logging
import os
import struct
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)
TIMEOUT = 10


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_decode(text):
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _env(name):
    return os.environ.get(name, "").strip()


def is_configured():
    return bool(_env("VAPID_PUBLIC_KEY") and _env("VAPID_PRIVATE_KEY"))


def public_key():
    return _env("VAPID_PUBLIC_KEY")


def generate_keys():
    """Returns (public, private) as base64url strings for the .env file."""
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64u(public), b64u(private)


def _private_key():
    value = int.from_bytes(b64u_decode(_env("VAPID_PRIVATE_KEY")), "big")
    return ec.derive_private_key(value, ec.SECP256R1())


def _hkdf(salt, ikm, info, length):
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(payload, p256dh, auth, salt=None, server_key=None):
    """aes128gcm body for one subscription (RFC 8291)."""
    ua_public = b64u_decode(p256dh)
    auth_secret = b64u_decode(auth)
    server_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_public = server_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = server_key.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    header = salt + struct.pack("!L", 4096) + bytes([len(as_public)]) + as_public
    return header + ciphertext


def _vapid_header(endpoint):
    import jwt
    parts = urlparse(endpoint)
    from .models import SiteSettings
    contact = _env("VAPID_EMAIL") or SiteSettings.load().support_email or "admin@example.com"
    token = jwt.encode({"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": f"mailto:{contact}"},
                       _private_key(), algorithm="ES256")
    return f"vapid t={token}, k={public_key()}"


def send(subscription, title, body="", url="/", tag=""):
    """Pushes one message. Returns 'ok', 'gone' (delete it) or 'error'."""
    payload = json.dumps({"title": title[:120], "body": body[:300], "url": url or "/", "tag": tag[:60]}).encode()
    try:
        data = encrypt(payload, subscription.p256dh, subscription.auth)
    except (ValueError, TypeError) as exc:
        logger.warning("Bad push subscription %s: %s", subscription.pk, exc)
        return "gone"
    req = urllib.request.Request(subscription.endpoint, data=data, method="POST", headers={
        "TTL": "86400", "Urgency": "normal", "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
        "Authorization": _vapid_header(subscription.endpoint)})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return "ok" if resp.status < 300 else "error"
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            return "gone"
        logger.warning("Push refused (%s): %s", exc.code, exc.read()[:200])
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("Push service unreachable: %s", exc)
    return "error"


def notify_user(user_id, title, body="", url="/"):
    """Pushes to every device of a user (runs after the response)."""
    from .models import PushSubscription
    if not is_configured():
        return
    from django.utils import timezone
    for sub in PushSubscription.objects.filter(user_id=user_id)[:10]:
        result = send(sub, title, body, url)
        if result == "gone":
            sub.delete()
        elif result == "ok":
            PushSubscription.objects.filter(pk=sub.pk).update(last_used=timezone.now())


def on_notification(sender, instance, created, **kwargs):
    """post_save for Notification: push it too."""
    if not created or not is_configured():
        return
    from .models import PushSubscription
    if not PushSubscription.objects.filter(user_id=instance.user_id).exists():
        return
    from django.db import transaction
    from .background import after_response
    from .models import SiteSettings
    name = SiteSettings.load().site_name
    transaction.on_commit(lambda: after_response(notify_user, instance.user_id, name, instance.message, instance.link or "/"))


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def subscribe_view(request):
    from django.http import JsonResponse
    from .models import PushSubscription
    if request.method != "POST" or not request.user.is_authenticated:
        return JsonResponse({"ok": False}, status=400)
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"ok": False}, status=400)
    if data.get("unsubscribe"):
        PushSubscription.objects.filter(user=request.user, endpoint=data.get("endpoint", "")).delete()
        return JsonResponse({"ok": True})
    endpoint = (data.get("endpoint") or "")[:500]
    keys = data.get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        return JsonResponse({"ok": False}, status=400)
    try:
        if len(b64u_decode(keys["p256dh"])) != 65 or len(b64u_decode(keys["auth"])) != 16:
            raise ValueError
    except ValueError:
        return JsonResponse({"ok": False}, status=400)
    PushSubscription.objects.update_or_create(endpoint=endpoint, defaults={
        "user": request.user, "p256dh": keys["p256dh"][:120], "auth": keys["auth"][:60],
        "user_agent": request.META.get("HTTP_USER_AGENT", "")[:200]})
    return JsonResponse({"ok": True})


def test_view(request):
    from django.http import JsonResponse
    if request.method != "POST" or not request.user.is_authenticated:
        return JsonResponse({"ok": False}, status=400)
    from .models import SiteSettings
    notify_user(request.user.id, SiteSettings.load().site_name, "Notifications are on. You'll hear about orders and messages here.", "/")
    return JsonResponse({"ok": True})
