"""Pakistan payments.

JazzCash online checkout (Hosted Checkout / page redirection v1.1)
------------------------------------------------------------------
The customer is sent to JazzCash's own payment page (wallet, card or
voucher), pays there and is sent back. Every request and response is
signed with HMAC-SHA256 using the merchant's Integrity Salt, and the
amount and reference are checked before an order is marked paid, so a
tampered return can never confirm an order. Keys go in Keys & passwords:
JAZZCASH_MERCHANT_ID, JAZZCASH_PASSWORD, JAZZCASH_INTEGRITY_SALT and
JAZZCASH_MODE (sandbox or live).

Easypaisa / JazzCash wallet / bank (Raast) transfer
---------------------------------------------------
For stores without a gateway account: the customer sends the money to one
of the store's accounts, then enters the transaction ID (and optionally a
screenshot). The store team confirms it under Orders > Payments to check.
Orders without a transaction ID are cancelled after the set hours.
"""
import hashlib
import hmac
import json
import logging
import urllib.parse
import urllib.request
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

SANDBOX = "https://sandbox.jazzcash.com.pk"
LIVE = "https://payments.jazzcash.com.pk"
FORM_PATH = "/CustomerPortal/transactionmanagement/merchantform/"
INQUIRY_PATH = "/ApplicationAPI/API/PaymentInquiry/Inquire"
SUCCESS_CODES = {"000", "121"}   # 121 = transaction successful (wallet)
PENDING_CODES = {"124", "157"}   # voucher issued / waiting for the customer to pay


def _env(name):
    from .multistore import env
    return env(name)


# ---------------------------------------------------------------- money in PKR

def pkr_rate():
    """How many PKR one unit of the store currency is (None if unknown)."""
    from .currency import get, store_code
    if store_code() == "PKR":
        return Decimal("1")
    cur = get("PKR")
    return cur.rate if cur else None


def pkr_amount(order):
    rate = pkr_rate()
    if rate is None:
        return None
    return (Decimal(str(order.total)) * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _fix_pkr_charge(order):
    """Records the PKR amount on the order so refunds and receipts match
    what the customer actually paid."""
    from .currency import store_code
    from .models import Order
    if store_code() == "PKR":
        fields = {"charge_currency": "", "charge_rate": None, "charge_amount": None}
    else:
        rate = pkr_rate()
        fields = {"charge_currency": "pkr", "charge_rate": rate, "charge_amount": pkr_amount(order)}
    Order.objects.filter(pk=order.pk).update(**fields)
    for k, v in fields.items():
        setattr(order, k, v)


# ---------------------------------------------------------------- JazzCash

def jazzcash_configured():
    return bool(_env("JAZZCASH_MERCHANT_ID") and _env("JAZZCASH_PASSWORD") and _env("JAZZCASH_INTEGRITY_SALT")) \
        and pkr_rate() is not None


def jazzcash_base():
    return LIVE if _env("JAZZCASH_MODE").lower() == "live" else SANDBOX


def jazzcash_status_line():
    if not (_env("JAZZCASH_MERCHANT_ID") and _env("JAZZCASH_PASSWORD") and _env("JAZZCASH_INTEGRITY_SALT")):
        return "off", "JazzCash is off - add the Merchant ID, Password and Integrity Salt under Keys & passwords."
    if pkr_rate() is None:
        return "missing", "JazzCash keys are set, but prices can't be turned into PKR: add PKR under Currencies (or use PKR as the store currency)."
    mode = "live - real money" if _env("JAZZCASH_MODE").lower() == "live" else "sandbox - test payments"
    return "ok", f"JazzCash is on ({mode})."


def secure_hash(fields, salt):
    """JazzCash v1.1: the salt and every non-empty pp_/ppmpf_ value, sorted
    by field name and joined with '&', signed with HMAC-SHA256 (salt as key),
    in upper-case hex."""
    values = [str(fields[k]) for k in sorted(fields) if k.lower().startswith("pp") and k != "pp_SecureHash"
              and fields[k] not in (None, "")]
    message = "&".join([salt] + values)
    return hmac.new(salt.encode(), message.encode(), hashlib.sha256).hexdigest().upper()


def _now_pk():
    from zoneinfo import ZoneInfo
    return timezone.now().astimezone(ZoneInfo("Asia/Karachi"))


def jazzcash_form(request, order):
    """(url, fields) for the auto-submitting form that takes the customer
    to JazzCash. Stores the transaction reference on the order."""
    from django.urls import reverse
    from .models import Order
    _fix_pkr_charge(order)
    amount = pkr_amount(order)
    now = _now_pk()
    ref = f"T{now:%Y%m%d%H%M%S}{order.pk % 1000:03d}"[:20]
    Order.objects.filter(pk=order.pk).update(gateway_ref=ref)
    order.gateway_ref = ref
    return_url = request.build_absolute_uri(reverse("jazzcash_return"))
    if not return_url.startswith("https://") and not request.get_host().startswith(("127.0.0.1", "localhost", "testserver")):
        return_url = "https://" + return_url.split("://", 1)[-1]
    fields = {
        "pp_Version": "1.1", "pp_TxnType": "", "pp_Language": "EN",
        "pp_MerchantID": _env("JAZZCASH_MERCHANT_ID"), "pp_SubMerchantID": "", "pp_Password": _env("JAZZCASH_PASSWORD"),
        "pp_BankID": "", "pp_ProductID": "", "pp_TxnRefNo": ref,
        "pp_Amount": str(int((amount * 100).to_integral_value())), "pp_TxnCurrency": "PKR",
        "pp_TxnDateTime": f"{now:%Y%m%d%H%M%S}", "pp_BillReference": f"order{order.pk}",
        "pp_Description": f"Order {order.pk}", "pp_TxnExpiryDateTime": f"{now + timedelta(hours=2):%Y%m%d%H%M%S}",
        "pp_ReturnURL": return_url, "ppmpf_1": str(order.pk), "ppmpf_2": "", "ppmpf_3": "", "ppmpf_4": "", "ppmpf_5": "",
    }
    fields["pp_SecureHash"] = secure_hash(fields, _env("JAZZCASH_INTEGRITY_SALT"))
    return jazzcash_base() + FORM_PATH, fields


def verify_response(data):
    """True when JazzCash's reply carries a valid signature."""
    salt = _env("JAZZCASH_INTEGRITY_SALT")
    given = (data.get("pp_SecureHash") or "").upper()
    if not salt or not given:
        return False
    return hmac.compare_digest(secure_hash(dict(data), salt), given)


def handle_response(data):
    """Processes the signed reply. Returns (order, state) where state is
    'paid', 'pending', 'failed' or 'invalid'."""
    from .models import Order
    if not verify_response(data):
        logger.warning("JazzCash reply with a bad signature for ref %s", data.get("pp_TxnRefNo"))
        return None, "invalid"
    ref = data.get("pp_TxnRefNo", "")
    order = Order.objects.filter(gateway_ref=ref, payment_method="jazzcash").first() if ref else None
    if order is None:
        return None, "invalid"
    code = data.get("pp_ResponseCode", "")
    expected = str(int((pkr_amount(order) if not order.charge_currency else order.charge_amount) * 100))
    if code in SUCCESS_CODES:
        if data.get("pp_Amount") != expected:
            from .alerts import notify_staff
            notify_staff(f"JazzCash payment for order #{order.id} didn't match the order total - check it in the JazzCash portal.",
                         link=f"/manage/orders/{order.id}/")
            return order, "invalid"
        newly = mark_paid(order, "JazzCash", f"JazzCash {ref}" + (f" ({data.get('pp_RetreivalReferenceNo')})" if data.get("pp_RetreivalReferenceNo") else ""))
        return order, "paid" if newly or order.payment_status == "paid" else "invalid"
    if code in PENDING_CODES:
        return order, "pending"
    logger.info("JazzCash payment for order %s not completed: %s %s", order.id, code, data.get("pp_ResponseMessage", ""))
    return order, "failed"


def inquire(order):
    """Asks JazzCash for the status of a payment (used for orders whose
    customer closed the page before coming back). Returns 'paid', 'pending',
    'failed' or '' when unknown."""
    if not order.gateway_ref or not jazzcash_configured():
        return ""
    fields = {"pp_TxnRefNo": order.gateway_ref, "pp_MerchantID": _env("JAZZCASH_MERCHANT_ID"), "pp_Password": _env("JAZZCASH_PASSWORD")}
    fields["pp_SecureHash"] = secure_hash(fields, _env("JAZZCASH_INTEGRITY_SALT"))
    req = urllib.request.Request(jazzcash_base() + INQUIRY_PATH, data=json.dumps(fields).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "LumenMarket/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode() or "{}")
    except Exception:
        logger.info("JazzCash inquiry failed for order %s", order.id, exc_info=True)
        return ""
    if data.get("pp_SecureHash") and not verify_response(data):
        return ""
    if data.get("pp_ResponseCode") == "000" and data.get("pp_PaymentResponseCode") in SUCCESS_CODES:
        mark_paid(order, "JazzCash", f"JazzCash {order.gateway_ref}")
        return "paid"
    if data.get("pp_PaymentResponseCode") in PENDING_CODES:
        return "pending"
    return "failed" if data.get("pp_ResponseCode") == "000" else ""


# ---------------------------------------------------------------- shared

def mark_paid(order, via, note):
    """Marks an order paid exactly once. Returns True if it changed now."""
    from .models import Order, OrderEvent
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        if order.payment_status == "paid":
            return False
        if order.status == "cancelled":
            # Paid after it was released: keep the money visible to staff for a manual refund.
            Order.objects.filter(pk=order.pk).update(payment_status="paid")
            OrderEvent.objects.create(order=order, kind="paid", text=f"Payment received with {via} after the order was cancelled - refund it"[:200])
            from .alerts import notify_staff
            notify_staff(f"Order #{order.id} was paid with {via} after it was cancelled. Send the money back to the customer.",
                         link=f"/manage/orders/{order.id}/")
            return False
        order.payment_status = "paid"
        if order.status == "pending":
            order.status = "confirmed"
        order.cod_confirmed = True
        if not order.estimated_delivery:
            from .order_emails import default_delivery_date
            order.estimated_delivery = default_delivery_date(order)
        order._skip_status_email = True
        order.save(update_fields=["payment_status", "status", "cod_confirmed", "estimated_delivery"])
        order._skip_status_email = False
        OrderEvent.objects.create(order=order, kind="paid", text=f"Payment received: {note}"[:200])
    transaction.on_commit(lambda: _after_paid(order.pk))
    return True


def _after_paid(order_id):
    from .models import Notification, Order
    from .views import send_order_confirmation_for
    order = Order.objects.get(pk=order_id)
    send_order_confirmation_for(order)
    if order.user_id:
        Notification.objects.create(user_id=order.user_id, message=f"Payment received for order #{order.id}.", link="/my-orders/")


def manual_refund(order, amount=None):
    """JazzCash and transfer payments are sent back by the store team
    (wallet or bank). Records it on the order and tells the team."""
    from .alerts import REFUND_FAILED, notify_staff
    from .models import OrderEvent
    value = amount if amount is not None else order.pay_amount
    cur = order.pay_currency.upper()
    where = order.transfer_account or ("JazzCash" if order.payment_method == "jazzcash" else "the account they paid from")
    OrderEvent.objects.create(order=order, kind="refunded", text=f"Refund of {value} {cur} to be sent back to {where} by the store team"[:200])
    notify_staff(f"Send {value} {cur} back for order #{order.id} ({order.full_name}, {order.phone}) to {where}.",
                 link=f"/manage/orders/{order.id}/", category=REFUND_FAILED)
    return True


# ---------------------------------------------------------------- transfers

def transfer_accounts():
    """[(name, details)] from Store settings."""
    from .models import SiteSettings
    out = []
    for line in (SiteSettings.load().transfer_accounts or "").splitlines():
        if ":" in line:
            name, details = line.split(":", 1)
            if name.strip() and details.strip():
                out.append((name.strip()[:60], details.strip()[:120]))
    return out[:8]


def transfer_on():
    from .models import SiteSettings
    return SiteSettings.load().transfer_enabled and bool(transfer_accounts())


def submit_transfer(order, account, reference, proof=None):
    """The customer says they've sent the money."""
    from .alerts import notify_staff
    from .models import Order, OrderEvent
    reference = " ".join((reference or "").split())[:60]
    if not reference or len(reference) < 4:
        raise ValueError("Enter the transaction ID from your Easypaisa, JazzCash or bank receipt.")
    if Order.objects.filter(payment_method="transfer", transfer_reference__iexact=reference).exclude(pk=order.pk).exists():
        raise ValueError("That transaction ID was already used for another order. Check the receipt and try again.")
    names = dict(transfer_accounts())
    if account not in names:
        raise ValueError("Choose the account you sent the money to.")
    order.transfer_account = f"{account}: {names[account]}"[:120]
    order.transfer_reference = reference
    order.transfer_submitted_at = timezone.now()
    order.transfer_note = ""
    fields = ["transfer_account", "transfer_reference", "transfer_submitted_at", "transfer_note"]
    if proof is not None:
        from .images import optimize
        order.transfer_proof = optimize(proof, 1600)
        fields.append("transfer_proof")
    order.save(update_fields=fields)
    OrderEvent.objects.create(order=order, kind="note", text=f"Customer sent the payment: {reference} to {account}"[:200])
    notify_staff(f"Order #{order.id}: payment sent by {account} (ref {reference}). Check it and confirm.",
                 link="/manage/orders/transfers/")


def approve_transfer(order, user):
    from .models import AuditLog
    if order.payment_method != "transfer" or order.payment_status == "paid":
        return False
    changed = mark_paid(order, order.transfer_account.split(":")[0] or "transfer", f"transfer {order.transfer_reference} confirmed by {user.username}")
    AuditLog.objects.create(user=user, action=f"Confirmed transfer for order #{order.id} ({order.transfer_reference})"[:255])
    return changed


def reject_transfer(order, user, reason):
    """The money didn't arrive (or the ID is wrong): the customer can send
    a corrected ID until the deadline."""
    from .models import AuditLog, Notification, Order, OrderEvent
    reason = " ".join((reason or "").split())[:200] or "We couldn't find this payment."
    Order.objects.filter(pk=order.pk).update(transfer_note=reason, transfer_submitted_at=None)
    OrderEvent.objects.create(order=order, kind="note", text=f"Payment not found: {reason}"[:200])
    AuditLog.objects.create(user=user, action=f"Rejected transfer for order #{order.id}: {reason}"[:255])
    if order.user_id:
        Notification.objects.create(user_id=order.user_id, link="/my-orders/",
                                    message=f"Order #{order.id}: we couldn't confirm your payment - {reason}"[:255])


def release_unpaid_transfers(now=None):
    """Cancels transfer orders with no transaction ID after the deadline
    and JazzCash orders left unpaid for 3 hours (after asking JazzCash)."""
    from .models import Order, SiteSettings
    from . import payments
    now = now or timezone.now()
    hours = SiteSettings.load().transfer_hours or 48
    released = 0
    for order in Order.objects.filter(payment_method="transfer", payment_status="pending", status="pending",
                                      transfer_submitted_at__isnull=True, created_at__lt=now - timedelta(hours=hours)):
        payments.release_unpaid_order(order.id, reason="failed")
        released += 1
    for order in Order.objects.filter(payment_method="jazzcash", payment_status="pending", created_at__lt=now - timedelta(hours=3)):
        state = inquire(order)
        if state in ("paid", "pending"):
            continue
        payments.release_unpaid_order(order.id, reason="failed")
        released += 1
    return released
