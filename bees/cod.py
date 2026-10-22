"""Cash on delivery protection.

* Phone check: with Store settings > "COD phone check" on and Twilio keys
  added, a cash order is only sent to sellers after the customer types the
  6-digit code we text to the phone number on the order.
* Order limit: cash is only offered up to "Largest cash order".
* Refusals: when a customer refuses a cash parcel the admin (or the courier
  webhook) marks the order "refused". After "refusal limit" refusals the
  phone number and email can only pay online. The block list can also be
  edited by hand in Manage > Cash on delivery.
"""
import hashlib
import hmac
import re
import secrets
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

CODE_MINUTES = 30
MAX_TRIES = 5
RESEND_SECONDS = 60


def _site():
    from .models import SiteSettings
    return SiteSettings.load()


def phone_key(phone):
    """Last 9 digits: the same number written as +92300.. or 0300.. matches."""
    digits = re.sub(r"\D", "", phone or "").lstrip("0")
    return digits[-9:] if len(digits) >= 7 else ""


def email_key(email):
    return (email or "").strip().lower()


def blocked(email="", phone=""):
    """Returns the CodBlock that stops this customer paying cash, or None."""
    from .models import CodBlock
    keys = []
    if email_key(email):
        keys.append(("email", email_key(email)))
    if phone_key(phone):
        keys.append(("phone", phone_key(phone)))
    for kind, value in keys:
        block = CodBlock.objects.filter(kind=kind, value=value).first()
        if block:
            return block
    return None


def refusals(email="", phone=""):
    """How many cash parcels this email or phone number has refused."""
    from .models import Order
    mail, tel = email_key(email), phone_key(phone)
    if not mail and not tel:
        return 0
    count = 0
    for o_email, o_guest, o_phone in Order.objects.filter(cod_refused=True).values_list("email", "guest_email", "phone"):
        if (mail and mail in (email_key(o_email), email_key(o_guest))) or (tel and phone_key(o_phone) == tel):
            count += 1
    return count


def problem(total, email="", phone=""):
    """Why cash on delivery isn't allowed for this order ('' = allowed)."""
    site = _site()
    if site.cod_max_order and Decimal(total) > site.cod_max_order:
        from .templatetags.bees_extras import money
        return f"Cash on delivery is available for orders up to {money(site.cod_max_order)}. Please pay online for this order."
    if blocked(email, phone):
        return "Cash on delivery isn't available for this account. Please pay online - it's safe and your order ships faster."
    return ""


def phone_check_on():
    from . import sms
    return _site().cod_phone_check and sms.is_configured()


def _hash(order, code):
    return hmac.new(settings.SECRET_KEY.encode(), f"{order.pk}:{code}".encode(), hashlib.sha256).hexdigest()


def send_code(order):
    """Creates a new 6-digit code and texts it. Returns True if sent."""
    from . import sms
    from .models import Order
    to = sms.normalize(order.phone, order.country)
    if not to:
        return False
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = timezone.now()
    Order.objects.filter(pk=order.pk).update(cod_otp_hash=_hash(order, code), cod_otp_sent_at=now, cod_otp_tries=0)
    order.cod_otp_hash, order.cod_otp_sent_at, order.cod_otp_tries = _hash(order, code), now, 0
    reply = " Or simply reply YES to confirm." if _site().message_channel == "whatsapp" else ""
    sms.send(to, f"{_site().site_name}: your code to confirm cash order #{order.id} is {code}. It expires in {CODE_MINUTES} minutes.{reply}")
    return True


def can_resend(order):
    return not order.cod_otp_sent_at or timezone.now() - order.cod_otp_sent_at >= timedelta(seconds=RESEND_SECONDS)


def check_code(order, code):
    """Returns (ok, message). Confirms the order when the code is right."""
    from django.db.models import F
    from .models import Order, OrderEvent
    if order.cod_confirmed:
        return True, ""
    if not order.cod_otp_hash or not order.cod_otp_sent_at:
        return False, "Please ask for a new code."
    if timezone.now() - order.cod_otp_sent_at > timedelta(minutes=CODE_MINUTES):
        return False, "That code has expired. Send a new one."
    if order.cod_otp_tries >= MAX_TRIES:
        return False, "Too many wrong codes. Send a new one."
    code = re.sub(r"\D", "", code or "")
    if len(code) != 6 or not hmac.compare_digest(_hash(order, code), order.cod_otp_hash):
        Order.objects.filter(pk=order.pk).update(cod_otp_tries=F("cod_otp_tries") + 1)
        order.cod_otp_tries += 1
        left = MAX_TRIES - order.cod_otp_tries
        return False, f"That code isn't right. {left} tr{'y' if left == 1 else 'ies'} left." if left > 0 else "Too many wrong codes. Send a new one."
    Order.objects.filter(pk=order.pk).update(cod_confirmed=True, cod_otp_hash="")
    order.cod_confirmed, order.cod_otp_hash = True, ""
    OrderEvent.objects.create(order=order, kind="note", text="Cash on delivery order confirmed with the code sent to the customer's phone")
    return True, ""


def mark_refused(order, user=None, reason="Customer refused the cash on delivery parcel"):
    """Cancels a refused cash order (stock goes back), tells the sellers and
    blocks cash for repeat refusers. Returns True if something changed."""
    from .models import AuditLog, CodBlock, Order, OrderEvent
    if order.payment_method != "cod" or order.cod_refused:
        return False
    Order.objects.filter(pk=order.pk).update(cod_refused=True)
    order.cod_refused = True
    OrderEvent.objects.create(order=order, kind="note", text=reason[:200])
    if order.status != "cancelled":
        from django.db import transaction
        from . import payments
        with transaction.atomic():
            order.status = "cancelled"
            order.cancel_reason = "Refused on delivery"
            order.save(update_fields=["status", "cancel_reason"])
            payments.restock(order)
    from .seller_center import _seller_users
    from .models import Notification
    sellers = {i.seller_account for i in order.items.select_related("seller_account") if i.seller_account_id}
    for seller in sellers:
        Notification.objects.bulk_create([
            Notification(user=u, link=f"/seller/orders/{order.id}/", message=f"Order #{order.id}: the customer refused the cash parcel. It has been cancelled; the courier returns it to you.")
            for u in _seller_users(seller)])
    if user is not None:
        AuditLog.objects.create(user=user, action=f"Order #{order.id}: marked as refused (cash on delivery)")
    limit = _site().cod_refusal_limit
    if limit and refusals(order.contact_email, order.phone) >= limit:
        for kind, value in (("email", email_key(order.contact_email)), ("phone", phone_key(order.phone))):
            if value:
                CodBlock.objects.get_or_create(kind=kind, value=value, defaults={
                    "reason": f"Refused {limit}+ cash parcels (last: order #{order.id})", "automatic": True})
    return True
