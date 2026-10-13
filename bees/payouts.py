"""Automatic seller payouts.

A seller picks how they get paid (Seller Center > Earnings):

* PayPal - money goes to their PayPal email through PayPal Payouts (uses the
  store's PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET; Payouts must be enabled on
  the PayPal business account).
* Stripe - the seller connects a Stripe Express account once (Stripe checks
  their identity and bank); we send Stripe transfers to it and Stripe pays it
  out to their bank. Not every country is supported by Stripe.
* Bank / manual - the store sends the money itself and records it (as before).

Only the "available" balance is ever sent: earnings of delivered items whose
return window has passed. The paid-out total is reserved before calling the
provider, so a payout can't be sent twice; a failed payout gives it back.
"""
import logging
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)
PAYPAL_FAILED = {"FAILED", "RETURNED", "BLOCKED", "REFUNDED", "REVERSED", "DENIED", "CANCELED"}


class PayoutError(Exception):
    pass


def _site():
    from .models import SiteSettings
    return SiteSettings.load()


def _money(value):
    from .templatetags.bees_extras import money
    return money(value)


def paypal_ready():
    from . import paypal
    return paypal.is_configured()


def stripe_ready():
    from . import payments
    return payments.is_configured()


def method_ready(seller):
    """True when this seller can be paid automatically right now."""
    if seller.payout_method == "paypal":
        return bool(seller.paypal_email) and paypal_ready()
    if seller.payout_method == "stripe":
        return bool(seller.stripe_account_id and seller.stripe_payouts_ready) and stripe_ready()
    return False


def options():
    """Automatic methods the store can offer sellers."""
    return {"paypal": paypal_ready(), "stripe": stripe_ready()}


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def send(seller, amount=None, user=None, request_payout=None):
    """Sends ``amount`` (default: the whole available balance) to the seller
    with their automatic method. Returns the Payout. Raises PayoutError."""
    from . import seller_center as sc
    from .models import Payout, SellerAccount
    if getattr(settings, "STAGING", False):
        raise PayoutError("Payouts are switched off on the test site.")
    if not method_ready(seller):
        raise PayoutError("This seller hasn't set up PayPal or Stripe payouts yet.")
    with transaction.atomic():
        seller = SellerAccount.objects.select_for_update().get(pk=seller.pk)
        if seller.payouts.filter(status="processing").exists():
            raise PayoutError("A payout to this seller is already on its way.")
        available = sc.balance(seller)["available"]
        amount = (amount if amount is not None else available).quantize(Decimal("0.01"))
        if amount <= 0:
            raise PayoutError("Nothing available to pay out yet.")
        if amount > available:
            raise PayoutError(f"Only {_money(available)} is available (the rest is still in the return window).")
        payout = request_payout or Payout(seller=seller, requested_by=user)
        payout.amount, payout.status, payout.method = amount, "processing", seller.payout_method
        payout.automatic, payout.recorded_by, payout.error = True, user, ""
        payout.save()
        seller.total_paid_out = (seller.total_paid_out or 0) + amount  # reserve it
        seller.save(update_fields=["total_paid_out"])
    try:
        if seller.payout_method == "paypal":
            _send_paypal(seller, payout)
        else:
            _send_stripe(seller, payout)
    except PayoutError as exc:
        fail(payout, str(exc))
        raise
    payout.refresh_from_db()
    if payout.status == "paid":
        _tell_seller(payout)
    return payout


def fail(payout, reason):
    """Marks a payout failed and gives the amount back to the seller's balance."""
    from django.db.models import F
    from .models import Payout, SellerAccount
    with transaction.atomic():
        changed = Payout.objects.filter(pk=payout.pk, status__in=["processing", "requested"]).update(status="failed", error=reason[:255])
        if changed:
            SellerAccount.objects.filter(pk=payout.seller_id).update(total_paid_out=F("total_paid_out") - payout.amount)
    if changed:
        from .alerts import notify_staff
        notify_staff(f"Payout of {_money(payout.amount)} to {payout.seller.display_name} failed: {reason[:150]}",
                     link=f"/manage/sellers/{payout.seller_id}/")
        from .models import Notification
        from .seller_center import _seller_users
        for u in _seller_users(payout.seller):
            Notification.objects.create(user=u, link="/seller/earnings/",
                                        message=f"Your payout of {_money(payout.amount)} couldn't be sent. Check your payout details.")


def _mark_paid(payout, reference=""):
    from .models import Payout
    return Payout.objects.filter(pk=payout.pk, status="processing").update(
        status="paid", paid_at=timezone.now(), reference=(reference or payout.reference)[:120])


def _tell_seller(payout):
    from .models import Notification
    from .seller_center import _seller_users
    from .views import _send_html_email
    for u in _seller_users(payout.seller):
        Notification.objects.create(user=u, link="/seller/earnings/",
                                    message=f"A payout of {_money(payout.amount)} has been sent to you ({payout.get_method_display()}).")
    seller = payout.seller
    from .order_emails import absolute
    transaction.on_commit(lambda: _send_html_email(
        f"{_site().site_name}: payout of {_money(payout.amount)} sent", "bees/emails/seller_payout.html",
        {"seller": seller, "payout": payout, "url": absolute(reverse("seller_earnings"))}, seller.user.email))


# --- PayPal Payouts --------------------------------------------------------

def _send_paypal(seller, payout):
    from . import paypal
    currency = settings.STORE_CURRENCY.upper()
    body = {
        "sender_batch_header": {
            "sender_batch_id": f"lumen-payout-{payout.id}",
            "email_subject": f"You have a payout from {_site().site_name}",
            "email_message": f"Your earnings from {_site().site_name}. Thank you for selling with us!",
        },
        "items": [{
            "recipient_type": "EMAIL", "receiver": seller.paypal_email, "sender_item_id": str(payout.id),
            "amount": {"value": f"{payout.amount:.2f}", "currency": currency},
            "note": f"Payout #{payout.id} - {_site().site_name}"[:4000],
        }],
    }
    try:
        data = paypal.api("POST", "/v1/payments/payouts", body, request_id=f"lumen-payout-{payout.id}")
    except paypal.PayPalError as exc:
        raise PayoutError(f"PayPal: {exc}") from None
    batch = (data.get("batch_header") or {})
    batch_id = batch.get("payout_batch_id", "")
    if not batch_id:
        raise PayoutError("PayPal didn't accept the payout.")
    type(payout).objects.filter(pk=payout.pk).update(reference=f"PayPal {batch_id}"[:120])
    payout.reference = f"PayPal {batch_id}"
    refresh_paypal(payout)


def _batch_id(payout):
    return payout.reference.split(" ", 1)[1] if payout.reference.startswith("PayPal ") else ""


def refresh_paypal(payout):
    """Checks a PayPal payout batch; marks it paid / failed. Returns status."""
    from . import paypal
    batch_id = _batch_id(payout)
    if not batch_id or payout.status != "processing":
        return payout.status
    try:
        data = paypal.api("GET", f"/v1/payments/payouts/{batch_id}")
    except paypal.PayPalError as exc:
        logger.warning("PayPal payout check failed: %s", exc)
        return payout.status
    batch_status = (data.get("batch_header") or {}).get("batch_status", "")
    item = (data.get("items") or [{}])[0]
    status = item.get("transaction_status", "")
    if status == "SUCCESS":
        if _mark_paid(payout, f"PayPal {item.get('transaction_id') or batch_id}"):
            payout.refresh_from_db()
            _tell_seller(payout)
    elif status in PAYPAL_FAILED or batch_status in ("DENIED", "CANCELED"):
        errors = item.get("errors") or {}
        fail(payout, f"PayPal: {errors.get('message') or status or batch_status}")
    elif status == "UNCLAIMED":
        from .models import Notification
        from .seller_center import _seller_users
        key_note = "unclaimed"
        if key_note not in payout.note:
            type(payout).objects.filter(pk=payout.pk).update(note=(payout.note + " unclaimed").strip()[:255])
            for u in _seller_users(payout.seller):
                Notification.objects.create(user=u, link="/seller/earnings/",
                                            message=f"PayPal is holding your payout of {_money(payout.amount)}: open a PayPal account with {payout.seller.paypal_email} within 30 days to receive it.")
    payout.refresh_from_db()
    return payout.status


# --- Stripe Connect --------------------------------------------------------

def _send_stripe(seller, payout):
    from . import payments
    stripe = payments._stripe()
    currency = settings.STORE_CURRENCY.lower()
    try:
        transfer = stripe.Transfer.create(
            amount=payments.to_cents(payout.amount, currency), currency=currency, destination=seller.stripe_account_id,
            description=f"Payout #{payout.id}", metadata={"payout_id": str(payout.id), "seller_id": str(seller.id)},
            idempotency_key=f"payout-{payout.id}",
        )
    except Exception as exc:
        raise PayoutError(f"Stripe: {payments.explain_error(exc)}") from None
    transfer_id = transfer.get("id") if hasattr(transfer, "get") else getattr(transfer, "id", "")
    _mark_paid(payout, f"Stripe {transfer_id}")


def stripe_onboarding_link(request, seller):
    """Creates the seller's Stripe Express account (once) and returns the
    Stripe page where they add their details and bank."""
    from . import payments
    stripe = payments._stripe()
    try:
        if not seller.stripe_account_id:
            from .couriers import _country_code
            country = _country_code(seller.country) or None
            params = {"type": "express", "email": seller.user.email or None,
                      "capabilities": {"transfers": {"requested": True}},
                      "business_profile": {"name": seller.display_name[:100]},
                      "metadata": {"seller_id": str(seller.id)}}
            if country:
                params["country"] = country
                home = (_site().ship_from_country or "").upper()
                if home and home != country:
                    params["tos_acceptance"] = {"service_agreement": "recipient"}  # cross-border payouts
            account = stripe.Account.create(idempotency_key=f"connect-{seller.id}-{uuid.uuid4().hex}", **params)
            type(seller).objects.filter(pk=seller.pk).update(stripe_account_id=account["id"], payout_method="stripe")
            seller.stripe_account_id = account["id"]
        link = stripe.AccountLink.create(
            account=seller.stripe_account_id, type="account_onboarding",
            refresh_url=request.build_absolute_uri(reverse("seller_stripe_connect")),
            return_url=request.build_absolute_uri(reverse("seller_stripe_return")),
        )
    except Exception as exc:
        raise PayoutError(payments.explain_error(exc)) from None
    return link["url"]


def refresh_stripe_account(seller):
    """Asks Stripe whether the connected account can receive transfers."""
    from . import payments
    if not seller.stripe_account_id:
        return False
    try:
        acct = payments._stripe().Account.retrieve(seller.stripe_account_id)
    except Exception as exc:
        logger.warning("Stripe account check failed: %s", exc)
        return seller.stripe_payouts_ready
    caps = acct.get("capabilities") or {}
    ready = bool(acct.get("payouts_enabled")) and caps.get("transfers") == "active"
    type(seller).objects.filter(pk=seller.pk).update(stripe_payouts_ready=ready)
    seller.stripe_payouts_ready = ready
    return ready


# ---------------------------------------------------------------------------
# Batch (admin button + daily task)
# ---------------------------------------------------------------------------

def pay_all(user=None, minimum=None):
    """Pays every seller who has automatic payouts set up. Returns
    (sent_count, total, errors)."""
    from . import seller_center as sc
    from .models import SellerAccount
    minimum = _site().min_auto_payout if minimum is None else minimum
    sent, total, errors = 0, Decimal("0"), []
    for seller in SellerAccount.objects.filter(status="approved", payout_method__in=["paypal", "stripe"]).select_related("user"):
        if not method_ready(seller):
            continue
        available = sc.balance(seller)["available"]
        if available < max(minimum, Decimal("0.01")):
            continue
        open_request = seller.payouts.filter(status="requested").first()
        try:
            payout = send(seller, available, user=user, request_payout=open_request)
        except PayoutError as exc:
            errors.append(f"{seller.display_name}: {exc}")
            continue
        sent += 1
        total += payout.amount
    return sent, total, errors


def refresh_processing():
    from .models import Payout
    done = 0
    for payout in Payout.objects.filter(status="processing", method="paypal").select_related("seller"):
        refresh_paypal(payout)
        done += 1
    return done


def due_today():
    mode = _site().auto_payouts
    if mode == "daily":
        return True
    return mode == "weekly" and timezone.localdate().weekday() == 0
