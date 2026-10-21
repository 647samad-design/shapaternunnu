"""Digital gift cards.

Anyone can buy one (card via Stripe or PayPal). The code is emailed to the
recipient, who adds it to their account as store credit (used at checkout
like any credit). Staff can issue cards for free (compensation, prizes) and
cancel unused ones from Manage > Gift cards.
"""
import logging
import secrets
import time
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import F, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .ratelimit import ratelimit
from .multistore import store_settings  # per-store values in multi-store mode

logger = logging.getLogger(__name__)
AMOUNTS = [25, 50, 100, 200]
MIN_AMOUNT, MAX_AMOUNT = Decimal("5"), Decimal("1000")
VALID_YEARS = 3
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I


class GiftCardError(Exception):
    pass


def _site():
    from .models import SiteSettings
    return SiteSettings.load()


def new_code():
    from .models import GiftCard
    while True:
        raw = "".join(secrets.choice(ALPHABET) for _ in range(12))
        code = f"GIFT-{raw[:4]}-{raw[4:8]}-{raw[8:]}"
        if not GiftCard.objects.filter(code=code).exists():
            return code


def clean_code(value):
    raw = "".join(ch for ch in (value or "").upper() if ch.isalnum())
    if raw.startswith("GIFT"):
        raw = raw[4:]
    if len(raw) != 12:
        return ""
    return f"GIFT-{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def pay_methods():
    from . import payments, paypal
    out = []
    if payments.is_configured():
        out.append("card")
    if paypal.is_configured():
        out.append("paypal")
    return out


def enabled():
    return _site().gift_cards_enabled and bool(pay_methods())


# ---------------------------------------------------------------------------
# Activation, emails and redeeming
# ---------------------------------------------------------------------------

def activate(card, method=""):
    """Marks a paid (or staff-issued) card active and emails it. Idempotent."""
    from .models import GiftCard
    now = timezone.now()
    updated = GiftCard.objects.filter(pk=card.pk, status="pending").update(
        status="active", paid_at=now, expires_at=(now + timedelta(days=365 * VALID_YEARS)).date(),
        payment_method=method or card.payment_method)
    if not updated:
        return False
    card.refresh_from_db()
    transaction.on_commit(lambda: send_email(card.pk))
    if card.purchaser_id:
        from .models import Notification
        Notification.objects.create(user_id=card.purchaser_id, link=reverse("gift_cards_mine"),
                                    message=f"Your gift card ({_money(card.amount)}) has been sent to {card.recipient_email or 'you'}.")
    return True


def _money(value):
    from .templatetags.bees_extras import money
    return money(value)


def send_email(card_id):
    from .models import GiftCard
    from .order_emails import absolute
    from .views import _send_html_email
    card = GiftCard.objects.filter(pk=card_id, status="active").first()
    if not card:
        return
    to = card.recipient_email or card.purchaser_email
    _send_html_email(
        f"{card.from_name or 'Someone'} sent you a {_money(card.amount)} gift card for {_site().site_name}"
        if card.recipient_email and card.recipient_email != card.purchaser_email else f"Your {_site().site_name} gift card ({_money(card.amount)})",
        "bees/emails/gift_card.html",
        {"card": card, "redeem_url": absolute(reverse("gift_cards_mine")) + f"?code={card.code}", "shop_url": absolute("/")},
        to,
    )
    GiftCard.objects.filter(pk=card.pk).update(sent_at=timezone.now())


def redeem(user, code):
    """Adds the card's value to the user's store credit. Returns the card."""
    from .models import AuditLog, GiftCard, Notification, Profile
    code = clean_code(code)
    if not code:
        raise GiftCardError("That doesn't look like a gift card code (GIFT-XXXX-XXXX-XXXX).")
    with transaction.atomic():
        card = GiftCard.objects.select_for_update().filter(code=code).first()
        if card is None or card.status == "pending":
            raise GiftCardError("We couldn't find that gift card. Check the code and try again.")
        if card.status == "redeemed":
            raise GiftCardError("This gift card has already been used." if card.redeemed_by_id != user.id else "You've already added this gift card.")
        if card.status == "void":
            raise GiftCardError("This gift card was cancelled. Please contact support.")
        if card.expires_at and card.expires_at < timezone.localdate():
            raise GiftCardError("This gift card has expired.")
        card.status, card.redeemed_by, card.redeemed_at = "redeemed", user, timezone.now()
        card.save(update_fields=["status", "redeemed_by", "redeemed_at"])
        Profile.objects.get_or_create(user=user)
        Profile.objects.filter(user=user).update(store_credit=F("store_credit") + card.amount)
        AuditLog.objects.create(user=user, action=f"Redeemed gift card {card.masked} ({card.amount})")
        Notification.objects.create(user=user, link="/profile/", message=f"{_money(card.amount)} gift card added to your store credit.")
    return card


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

def _stripe_checkout(request, card):
    from . import payments
    stripe = payments._stripe()
    currency = card.currency.lower()
    params = {
        "mode": "payment",
        "line_items": [{"quantity": 1, "price_data": {"currency": currency, "unit_amount": payments.to_cents(card.amount, currency),
                                                      "product_data": {"name": f"{_site().site_name} gift card"}}}],
        "client_reference_id": f"gift-{card.id}",
        "metadata": {"gift_card_id": str(card.id)},
        "payment_intent_data": {"metadata": {"gift_card_id": str(card.id)}},
        "success_url": request.build_absolute_uri(reverse("gift_card_done", args=[card.id])) + "?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": request.build_absolute_uri(reverse("gift_cards")),
        "expires_at": int(time.time()) + payments.CHECKOUT_SESSION_LIFETIME_SECONDS,
    }
    if card.purchaser_email:
        params["customer_email"] = card.purchaser_email
    try:
        session = stripe.checkout.Session.create(idempotency_key=f"gift-{card.id}-{uuid.uuid4().hex}", **params)
    except Exception as exc:
        logger.exception("Stripe gift card checkout failed")
        raise GiftCardError("Card payment is temporarily unavailable. Please try again in a few minutes.") from exc
    type(card).objects.filter(pk=card.pk).update(stripe_session_id=session.id)
    return session.url


def confirm_stripe(card_id, session):
    from . import payments
    from .models import GiftCard
    card = GiftCard.objects.filter(pk=card_id).first()
    if not card or (card.stripe_session_id and session.get("id") != card.stripe_session_id):
        return None
    if session.get("payment_status") != "paid":
        return card
    if int(session.get("amount_total") or 0) < payments.to_cents(card.amount, card.currency):
        logger.error("Gift card %s: paid %s < expected", card.id, session.get("amount_total"))
        return card
    activate(card, "card")
    card.refresh_from_db()
    return card


def _paypal_amount(card):
    return {"currency_code": card.currency.upper(), "value": f"{card.amount:.2f}"}


def _paypal_checkout(request, card):
    from . import paypal
    body = {
        "intent": "CAPTURE",
        "purchase_units": [{"reference_id": f"gift-{card.id}", "custom_id": f"gift-{card.id}",
                            "description": f"{_site().site_name} gift card"[:127], "amount": _paypal_amount(card)}],
        "payment_source": {"paypal": {"experience_context": {
            "brand_name": _site().site_name[:127], "user_action": "PAY_NOW", "shipping_preference": "NO_SHIPPING",
            "return_url": request.build_absolute_uri(reverse("gift_card_paypal_return", args=[card.id])),
            "cancel_url": request.build_absolute_uri(reverse("gift_cards")),
        }}},
    }
    try:
        data = paypal.api("POST", "/v2/checkout/orders", body, request_id=f"lumen-gift-{card.id}")
    except paypal.PayPalError as exc:
        raise GiftCardError(f"PayPal isn't available right now ({exc}).") from exc
    link = next((l["href"] for l in data.get("links", []) if l.get("rel") in ("payer-action", "approve")), "")
    if not data.get("id") or not link:
        raise GiftCardError("PayPal didn't return a payment page.")
    type(card).objects.filter(pk=card.pk).update(paypal_order_id=data["id"])
    return link


def capture_paypal(card):
    """Captures (or re-checks) the PayPal payment. Returns True when active."""
    from . import paypal
    if card.status != "pending":
        return card.status in ("active", "redeemed")
    if not card.paypal_order_id:
        return False
    data = paypal.api("GET", f"/v2/checkout/orders/{card.paypal_order_id}")
    if data.get("status") == "APPROVED":
        data = paypal.api("POST", f"/v2/checkout/orders/{card.paypal_order_id}/capture", {}, request_id=f"lumen-gift-cap-{card.id}")
    unit = (data.get("purchase_units") or [{}])[0]
    capture = ((unit.get("payments") or {}).get("captures") or [{}])[0]
    amount = capture.get("amount") or {}
    if capture.get("status") != "COMPLETED" or unit.get("custom_id", f"gift-{card.id}") != f"gift-{card.id}":
        return False
    try:
        paid = Decimal(amount.get("value", "0"))
    except InvalidOperation:
        paid = Decimal("0")
    if amount.get("currency_code", "").upper() != card.currency.upper() or paid < card.amount:
        logger.error("Gift card %s: PayPal amount mismatch %s", card.id, amount)
        return False
    activate(card, "paypal")
    return True


# ---------------------------------------------------------------------------
# Shop pages
# ---------------------------------------------------------------------------

@ratelimit("giftcard_buy", rate_limit=10, window_seconds=600, redirect_to="gift_cards",
           message="Too many attempts. Please wait a few minutes.")
def buy(request):
    from .models import GiftCard
    site = _site()
    methods = pay_methods()
    if not site.gift_cards_enabled:
        raise Http404
    form = {"amount": "50", "from_name": request.user.get_full_name() if request.user.is_authenticated else "",
            "email": request.user.email if request.user.is_authenticated else ""}
    if request.method == "POST":
        form = {k: request.POST.get(k, "").strip() for k in ("amount", "custom", "from_name", "email", "recipient_name", "recipient_email", "message", "method")}
        try:
            raw = form["custom"] if form["amount"] == "custom" else form["amount"]
            try:
                amount = Decimal(raw.replace(",", "")).quantize(Decimal("0.01"))
            except InvalidOperation:
                raise ValidationError("Choose an amount.")
            if not MIN_AMOUNT <= amount <= MAX_AMOUNT:
                raise ValidationError(f"Gift cards can be from {_money(MIN_AMOUNT)} to {_money(MAX_AMOUNT)}.")
            validate_email(form["email"])
            if form["recipient_email"]:
                validate_email(form["recipient_email"])
            if form["method"] not in methods:
                raise ValidationError("Choose how to pay.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return render(request, "bees/gift_cards.html", {"form": form, "amounts": AMOUNTS, "methods": methods})
        card = GiftCard.objects.create(
            code=new_code(), amount=amount, currency=store_settings.STORE_CURRENCY.lower(),
            purchaser=request.user if request.user.is_authenticated else None, purchaser_email=form["email"].lower(),
            from_name=form["from_name"][:80], recipient_name=form["recipient_name"][:80],
            recipient_email=form["recipient_email"].lower(), message=form["message"][:300], payment_method=form["method"])
        request.session["gift_card"] = card.id
        try:
            return redirect(_stripe_checkout(request, card) if form["method"] == "card" else _paypal_checkout(request, card))
        except GiftCardError as exc:
            card.delete()
            messages.error(request, str(exc))
            return render(request, "bees/gift_cards.html", {"form": form, "amounts": AMOUNTS, "methods": methods})
    return render(request, "bees/gift_cards.html", {"form": form, "amounts": AMOUNTS, "methods": methods})


def _own_purchase(request, card_id):
    from .models import GiftCard
    card = get_object_or_404(GiftCard, pk=card_id)
    if request.session.get("gift_card") == card.id or (request.user.is_authenticated and card.purchaser_id == request.user.id):
        return card
    raise Http404


def done(request, card_id):
    """Back from Stripe. The webhook also activates the card."""
    from . import payments
    card = _own_purchase(request, card_id)
    session_id = request.GET.get("session_id", "")
    if card.status == "pending" and session_id and session_id == card.stripe_session_id:
        session = payments.retrieve_session(session_id)
        if session:
            card = confirm_stripe(card.id, session) or card
    return render(request, "bees/gift_card_done.html", {"card": card})


def paypal_return(request, card_id):
    from . import paypal
    card = _own_purchase(request, card_id)
    if request.GET.get("token") and request.GET["token"] != card.paypal_order_id:
        raise Http404
    try:
        capture_paypal(card)
    except paypal.PayPalError as exc:
        messages.error(request, f"We couldn't confirm the PayPal payment yet ({exc}). If you were charged, the card is sent automatically within minutes.")
    card.refresh_from_db()
    return render(request, "bees/gift_card_done.html", {"card": card})


@login_required
@ratelimit("giftcard_redeem", rate_limit=10, window_seconds=900, redirect_to="gift_cards_mine",
           message="Too many tries. Please wait 15 minutes and try again.")
def mine(request):
    from .models import GiftCard, Profile
    if request.method == "POST":
        try:
            card = redeem(request.user, request.POST.get("code", ""))
        except GiftCardError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"{_money(card.amount)} added to your store credit. It's taken off your next order automatically.")
        nxt = request.POST.get("next", "")
        return redirect("checkout" if nxt == "checkout" else "gift_cards_mine")
    profile, _ = Profile.objects.get_or_create(user=request.user)
    return render(request, "bees/account/gift_cards.html", {
        "tab": "gift_cards", "balance": profile.store_credit, "code": request.GET.get("code", "")[:24],
        "bought": GiftCard.objects.filter(purchaser=request.user).exclude(status="pending")[:50],
        "redeemed": GiftCard.objects.filter(redeemed_by=request.user)[:50],
        "can_buy": enabled(),
    })


@login_required
@require_POST
def resend(request, card_id):
    from .models import GiftCard
    card = get_object_or_404(GiftCard, pk=card_id, purchaser=request.user, status="active")
    if card.sent_at and timezone.now() - card.sent_at < timedelta(minutes=10):
        messages.info(request, "We sent it a few minutes ago. Please check the spam folder, or try again later.")
    else:
        send_email(card.id)
        messages.success(request, f"Sent again to {card.recipient_email or card.purchaser_email}.")
    return redirect("gift_cards_mine")


# ---------------------------------------------------------------------------
# Store admin
# ---------------------------------------------------------------------------

def admin_page(request):
    from .manage_views import _log, _render
    from .models import GiftCard
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "issue":
            try:
                amount = Decimal(request.POST.get("amount", "")).quantize(Decimal("0.01"))
                if not Decimal("1") <= amount <= Decimal("5000"):
                    raise InvalidOperation
                email = request.POST.get("email", "").strip().lower()
                validate_email(email)
            except (InvalidOperation, ValidationError):
                messages.error(request, "Enter an amount (1 - 5,000) and a valid email.")
                return redirect("manage_gift_cards")
            card = GiftCard.objects.create(
                code=new_code(), amount=amount, currency=store_settings.STORE_CURRENCY.lower(), purchaser_email=email,
                recipient_email=email, recipient_name=request.POST.get("name", "").strip()[:80], from_name=_site().site_name,
                message=request.POST.get("message", "").strip()[:300], payment_method="staff", issued_by=request.user,
                note=request.POST.get("note", "").strip()[:200])
            with transaction.atomic():
                activate(card, "staff")
            _log(request, f"Issued gift card {card.masked} for {amount} to {email}")
            messages.success(request, f"Gift card for {_money(amount)} emailed to {email}.")
        elif action == "void":
            card = get_object_or_404(GiftCard, pk=request.POST.get("id"))
            if card.status != "active":
                messages.error(request, "Only unused, active gift cards can be cancelled.")
            else:
                GiftCard.objects.filter(pk=card.pk, status="active").update(status="void", note=(request.POST.get("reason", "") or card.note)[:200])
                _log(request, f"Cancelled gift card {card.masked}")
                messages.success(request, f"Gift card {card.masked} cancelled. Refund the buyer in Stripe/PayPal if they paid for it.")
        elif action == "resend":
            card = get_object_or_404(GiftCard, pk=request.POST.get("id"), status="active")
            send_email(card.id)
            messages.success(request, "Sent again.")
        return redirect("manage_gift_cards")
    q = request.GET.get("q", "").strip()
    cards = GiftCard.objects.exclude(status="pending").select_related("purchaser", "redeemed_by", "issued_by")
    if q:
        from django.db.models import Q
        cards = cards.filter(Q(code__icontains=q) | Q(purchaser_email__icontains=q) | Q(recipient_email__icontains=q))
    totals = {
        "sold": GiftCard.objects.exclude(status="pending").exclude(payment_method="staff").aggregate(s=Sum("amount"))["s"] or 0,
        "outstanding": GiftCard.objects.filter(status="active").aggregate(s=Sum("amount"))["s"] or 0,
        "redeemed": GiftCard.objects.filter(status="redeemed").aggregate(s=Sum("amount"))["s"] or 0,
    }
    return _render(request, "gift_cards.html", {"section": "gift_cards", "cards": cards[:300], "q": q, "totals": totals,
                                                "enabled": _site().gift_cards_enabled, "methods": pay_methods()})
