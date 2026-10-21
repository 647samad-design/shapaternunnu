"""Text messages (SMS) through Twilio.

Turned on by TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM in
Store settings > Keys & passwords. Messages go out after the page has been
sent (never slows anyone down) and a failure never breaks an order.
Numbers must be in international format (+923001234567); local numbers
are converted using the order's country when possible.
"""
import base64
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)
TIMEOUT = 15
CALLING_CODES = {
    "PK": "92", "AE": "971", "SA": "966", "GB": "44", "US": "1", "CA": "1", "IN": "91", "BD": "880", "QA": "974", "KW": "965",
    "OM": "968", "BH": "973", "DE": "49", "FR": "33", "ES": "34", "IT": "39", "NL": "31", "AU": "61", "NZ": "64", "TR": "90",
    "MY": "60", "SG": "65", "EG": "20", "ZA": "27", "NG": "234", "KE": "254", "IE": "353", "SE": "46", "NO": "47", "DK": "45",
}


def _env(name):
    from .multistore import env as _store_env
    return _store_env(name)


def is_configured():
    from django.conf import settings
    if getattr(settings, "STAGING", False) and not _env("STAGING_ALLOW_SMS"):
        return False  # never text real customers from the test site
    return bool(_env("TWILIO_ACCOUNT_SID") and _env("TWILIO_AUTH_TOKEN") and _env("TWILIO_FROM"))


def normalize(phone, country=""):
    """'+92 300-1234567' / '03001234567' (PK) -> '+923001234567', or ''."""
    raw = (phone or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    if raw.startswith("+"):
        number = "+" + digits
    elif digits.startswith("00"):
        number = "+" + digits[2:]
    else:
        code = CALLING_CODES.get((country or "").upper())
        if not code:
            return ""
        number = "+" + code + digits.lstrip("0")
    return number if 8 <= len(number) <= 16 else ""


def send_now(to, body):
    """Sends right away. Returns True if Twilio accepted it."""
    if not is_configured() or not to:
        return False
    sid, sender = _env("TWILIO_ACCOUNT_SID"), _env("TWILIO_FROM")
    if _store().message_channel == "whatsapp":
        # Same Twilio account; WhatsApp numbers carry a "whatsapp:" prefix.
        wa_from = _env("TWILIO_WHATSAPP_FROM") or sender
        data = {"To": f"whatsapp:{to}", "Body": body[:600], "From": wa_from if wa_from.startswith("whatsapp:") else f"whatsapp:{wa_from}"}
    else:
        data = {"To": to, "Body": body[:600]}
        data["MessagingServiceSid" if sender.startswith("MG") else "From"] = sender
    auth = base64.b64encode(f"{sid}:{_env('TWILIO_AUTH_TOKEN')}".encode()).decode()
    req = urllib.request.Request(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                                 data=urllib.parse.urlencode(data).encode(), method="POST",
                                 headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        logger.warning("Twilio refused SMS to %s: %s", to[:6] + "…", exc.read()[:300])
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("Twilio unreachable: %s", exc)
    from .alerts import log
    log(f"SMS failed: message to {to[:6]}… wasn't sent")
    return False


def send(to, body):
    """Sends after the response (or straight away outside a request)."""
    if not is_configured() or not to:
        return
    from .background import after_response
    after_response(send_now, to, body)


def _store():
    from .models import SiteSettings
    return SiteSettings.load()


def _customer_ok(order):
    site = _store()
    if not site.sms_customer_updates or not is_configured():
        return ""
    if order.user_id:
        from .models import Profile
        if Profile.objects.filter(user_id=order.user_id, sms_updates=False).exists():
            return ""
    return normalize(order.phone, order.country)


def order_status(order, status):
    to = _customer_ok(order)
    if not to:
        return
    name = _store().site_name
    text = {
        "confirmed": f"{name}: order #{order.id} is confirmed. We'll text you when it ships.",
        "shipped": f"{name}: order #{order.id} is on its way" + (f" with {order.courier_name}" if order.courier_name else "")
                   + (f", tracking {order.tracking_number}" if order.tracking_number else "") + ".",
        "delivered": f"{name}: order #{order.id} was delivered. Enjoy! Rate your items in My orders.",
        "cancelled": f"{name}: order #{order.id} has been cancelled.",
    }.get(status)
    if text:
        send(to, text)


def package_update(order, names, status):
    to = _customer_ok(order)
    if to:
        verb = "was delivered" if status == "delivered" else "is on its way"
        send(to, f"{_store().site_name}: {names[:60]} from order #{order.id} {verb}.")


def seller_new_order(seller, order, units):
    site = _store()
    if not site.sms_seller_alerts or not is_configured():
        return
    to = normalize(seller.phone, _country_code(seller.country))
    if to:
        send(to, f"{site.site_name}: new order #{order.id} - {units} item{'s' if units != 1 else ''} to ship. Pack it within 2 days.")


def _country_code(value):
    from .couriers import _country_code as cc
    return cc(value)
