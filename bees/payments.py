"""
Stripe Checkout integration.

Flow
----
1. Checkout creates the Order (status "pending", payment_status "pending")
   and reserves stock inside a database transaction.
2. ``create_checkout_session`` sends the customer to Stripe's hosted
   payment page (cards, Apple Pay, Google Pay, Link ... whatever is enabled
   in your Stripe dashboard).
3. Stripe redirects back to /payment/success/ and, independently, calls
   our signed webhook at /payment/stripe/webhook/. The webhook is the
   source of truth: it checks the amount and marks the order paid.
4. If the customer abandons the page, Stripe sends
   ``checkout.session.expired`` (after 60 minutes) and we cancel the order
   and put the stock back.

Configuration (environment variables)
-------------------------------------
STRIPE_SECRET_KEY        sk_test_... / sk_live_...
STRIPE_PUBLISHABLE_KEY   pk_test_... / pk_live_...  (not required by
                         hosted Checkout, kept for future Elements use)
STRIPE_WEBHOOK_SECRET    whsec_...  (from Dashboard > Developers > Webhooks,
                         or from `stripe listen` when testing locally)
STORE_CURRENCY           usd (default), eur, gbp ...
STRIPE_API_BASE          optional relay URL (see deploy/supabase/stripe-relay)

Until STRIPE_SECRET_KEY is set, card payments are hidden at checkout.
"""
import json
import uuid
import logging
import time
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import transaction
from django.urls import reverse

logger = logging.getLogger(__name__)

CHECKOUT_SESSION_LIFETIME_SECONDS = 60 * 60  # Stripe needs at least 30 min; 60 leaves room for clock and network delay


class PaymentError(Exception):
    """Raised when Stripe can't be reached or rejects a request. The
    message is safe to show to customers."""


def is_configured():
    return bool(getattr(settings, "STRIPE_SECRET_KEY", ""))


def webhook_configured():
    return bool(getattr(settings, "STRIPE_WEBHOOK_SECRET", ""))


def _stripe():
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    stripe.max_network_retries = 2
    stripe.api_base = getattr(settings, "STRIPE_API_BASE", "") or "https://api.stripe.com"
    return stripe


def _plain(obj):
    """Stripe objects as plain dicts. Newer versions of the stripe library
    (v15+) no longer behave like dicts, so ``obj.get(...)`` would crash."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    for method in ("to_dict_recursive", "to_dict"):
        convert = getattr(obj, method, None)
        if callable(convert):
            return convert()
    return dict(obj)


def to_cents(amount, currency=None):
    """Amount in Stripe's smallest currency unit (cents for USD, whole yen
    for JPY ...)."""
    from .templatetags.bees_extras import ZERO_DECIMAL
    currency = (currency or settings.STORE_CURRENCY).lower()
    factor = 1 if currency in ZERO_DECIMAL else 100
    return int((Decimal(str(amount)) * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _absolute(request, path):
    return request.build_absolute_uri(path)


def create_checkout_session(request, order):
    """Creates a Stripe Checkout Session for ``order`` and returns its URL."""
    stripe = _stripe()
    currency = settings.STORE_CURRENCY.lower()

    line_items = []
    for item in order.items.all():
        line_items.append({
            "quantity": item.quantity,
            "price_data": {
                "currency": currency,
                "unit_amount": to_cents(item.price, currency),
                "product_data": {"name": item.product_name[:250]},
            },
        })
    if order.shipping_amount:
        line_items.append({
            "quantity": 1,
            "price_data": {
                "currency": currency,
                "unit_amount": to_cents(order.shipping_amount, currency),
                "product_data": {"name": "Shipping"},
            },
        })
    if order.tax_amount:
        line_items.append({
            "quantity": 1,
            "price_data": {
                "currency": currency,
                "unit_amount": to_cents(order.tax_amount, currency),
                "product_data": {"name": "Tax"},
            },
        })

    params = {
        "mode": "payment",
        "line_items": line_items,
        "client_reference_id": str(order.id),
        "metadata": {"order_id": str(order.id)},
        "payment_intent_data": {"metadata": {"order_id": str(order.id)}},
        "success_url": _absolute(request, reverse("payment_success")) + "?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": _absolute(request, reverse("payment_cancel", args=[order.id])),
        "expires_at": int(time.time()) + CHECKOUT_SESSION_LIFETIME_SECONDS,
    }
    email = order.contact_email
    customer_id = ensure_customer(order.user) if order.user_id else None
    if customer_id:
        # Returning customers see their saved cards; new cards can be saved
        # with one tick on Stripe's page.
        params["customer"] = customer_id
        params["saved_payment_method_options"] = {"payment_method_save": "enabled"}
    elif email:
        params["customer_email"] = email

    try:
        reduction = order.discount_amount + order.credit_used
        if reduction:
            if order.discount_amount and order.credit_used:
                label = "Discount & store credit"
            elif order.credit_used:
                label = "Store credit"
            else:
                label = f"Code {order.coupon_code}" if order.coupon_code else "Discount"
            coupon = stripe.Coupon.create(
                amount_off=to_cents(reduction, currency),
                currency=currency,
                duration="once",
                name=label[:40],
                max_redemptions=1,
            )
            params["discounts"] = [{"coupon": coupon.id}]
        try:
            session = stripe.checkout.Session.create(
                idempotency_key=f"order-{order.id}-{uuid.uuid4().hex}",
                **params,
            )
        except Exception as first:
            # Saved-card features must never block a payment. If Stripe
            # rejects the request because of the saved customer (deleted,
            # or made with other keys) or the card-saving option, pay as a
            # guest checkout instead and note why.
            if not (customer_id and _is_request_error(first)):
                raise
            from .alerts import PAYMENT_FAILED, log
            log(f"{PAYMENT_FAILED}: order #{order.id} retried without saved cards - {explain_error(first)}")
            if _is_missing_customer(first):
                forget_customer(order.user, customer_id)
            params.pop("customer", None)
            params.pop("saved_payment_method_options", None)
            if email:
                params["customer_email"] = email
            session = stripe.checkout.Session.create(
                idempotency_key=f"order-{order.id}-{uuid.uuid4().hex}",
                **params,
            )
    except Exception as exc:  # stripe.StripeError and network failures
        logger.exception("Stripe Checkout Session creation failed for order %s", order.id)
        from .alerts import PAYMENT_FAILED, log
        log(f"{PAYMENT_FAILED}: order #{order.id} - {explain_error(exc)}")
        msg = "Card payment is temporarily unavailable. Please try again in a few minutes"
        from .models import SiteSettings
        if SiteSettings.load().allow_cash_on_delivery:
            msg += ", or choose Cash on delivery"
        raise PaymentError(msg + ".") from exc

    order.stripe_session_id = session.id
    order.save(update_fields=["stripe_session_id"])
    from .alerts import payments_working
    payments_working(f"payment page opened for order #{order.id}")
    return session.url


def create_plan_checkout(request, payment):
    """Stripe payment page for a seller plan (one-off payment for
    ``payment.months`` months; nothing renews automatically)."""
    stripe = _stripe()
    currency = settings.STORE_CURRENCY.lower()
    months = payment.months
    params = {
        "mode": "payment",
        "line_items": [{
            "quantity": months,
            "price_data": {
                "currency": currency,
                "unit_amount": to_cents(payment.amount / months, currency),
                "product_data": {"name": f"{payment.plan_name} seller plan - 1 month"},
            },
        }],
        "client_reference_id": f"plan-{payment.id}",
        "metadata": {"plan_payment_id": str(payment.id)},
        "payment_intent_data": {"metadata": {"plan_payment_id": str(payment.id)}},
        "success_url": _absolute(request, reverse("seller_plan_done")) + "?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": _absolute(request, reverse("seller_plan")),
        "expires_at": int(time.time()) + CHECKOUT_SESSION_LIFETIME_SECONDS,
    }
    if payment.seller.user.email:
        params["customer_email"] = payment.seller.user.email
    try:
        session = stripe.checkout.Session.create(idempotency_key=f"plan-{payment.id}-{uuid.uuid4().hex}", **params)
    except Exception as exc:
        logger.exception("Stripe plan checkout failed for plan payment %s", payment.id)
        from .alerts import PAYMENT_FAILED, log
        log(f"{PAYMENT_FAILED}: seller plan payment #{payment.id} - {explain_error(exc)}")
        raise PaymentError("Card payment is temporarily unavailable. Please try again in a few minutes.") from exc
    payment.stripe_session_id = session.id
    payment.save(update_fields=["stripe_session_id"])
    return session.url


def confirm_plan_payment(payment_id, session):
    """Activates a plan payment once Stripe says it's paid and the amount
    matches. Returns the PlanPayment (or None)."""
    from .models import PlanPayment
    payment = PlanPayment.objects.select_related("seller").filter(pk=payment_id).first()
    if not payment or (payment.stripe_session_id and session.get("id") != payment.stripe_session_id):
        return None
    if session.get("payment_status") != "paid":
        return payment
    currency = settings.STORE_CURRENCY.lower()
    expected = to_cents(payment.amount / payment.months, currency) * payment.months
    if int(session.get("amount_total") or 0) < expected:
        logger.error("Plan payment %s: Stripe total %s < expected %s", payment.id, session.get("amount_total"), expected)
        return payment
    payment.activate()
    return payment


def explain_error(exc):
    """Plain-language reason for a failed Stripe call (for the owner)."""
    text = str(exc)
    status = getattr(exc, "http_status", None)
    relay = bool(getattr(settings, "STRIPE_API_BASE", ""))
    if relay and status == 401 and "Invalid API Key" not in text:
        return ("The Supabase relay is still checking for a login token (JWT), so it blocks every payment. "
                "In Supabase > Edge Functions > stripe-relay > Details, turn OFF 'Verify JWT' and save.")
    if status == 401:
        return "Stripe rejected the secret key (STRIPE_SECRET_KEY). Copy it again from Stripe > Developers > API keys."
    if relay and status == 403:
        return "The Supabase relay refused the request: RELAY_SECRET in Supabase doesn't match the end of STRIPE_API_BASE in .env."
    if relay and status == 404:
        return "The relay address wasn't found. Check STRIPE_API_BASE and that the stripe-relay function is deployed."
    if relay and status == 500 and "RELAY_SECRET" in text:
        return "RELAY_SECRET isn't set in Supabase > Edge Functions > Secrets."
    if status is None:
        return f"Couldn't reach {'the Supabase relay' if relay else 'Stripe'} from the server: {text[:200]}"
    return f"Stripe error {status}: {text[:250]}"


def retrieve_session(session_id):
    stripe = _stripe()
    try:
        return stripe.checkout.Session.retrieve(session_id).to_dict()
    except Exception:
        logger.exception("Could not retrieve Stripe session %s", session_id)
        return None


# ---------------------------------------------------------------------------
# Saved cards. Cards are stored by Stripe on a Stripe "customer"; we only
# keep the customer id. Adding a card happens on Stripe's own page.
# ---------------------------------------------------------------------------

def ensure_customer(user):
    """Returns the user's Stripe customer id, creating the customer the
    first time. Returns None if Stripe isn't configured or can't be
    reached (checkout then simply works without saved cards)."""
    if not is_configured() or not user or not user.is_authenticated:
        return None
    from .models import Profile
    profile, _ = Profile.objects.get_or_create(user=user, defaults={"referral_code": uuid.uuid4().hex[:8].upper()})
    if profile.stripe_customer_id:
        return profile.stripe_customer_id
    try:
        customer = _stripe().Customer.create(
            email=user.email or None,
            name=user.get_full_name() or user.username,
            metadata={"user_id": str(user.pk)},
            # A fresh key each time: a fixed key would make Stripe replay an
            # old (possibly deleted) customer for 24 hours.
            idempotency_key=f"customer-{user.pk}-{uuid.uuid4().hex[:16]}",
        )
    except Exception as exc:
        logger.exception("Could not create Stripe customer for user %s", user.pk)
        from .alerts import PAYMENT_FAILED, log
        log(f"{PAYMENT_FAILED}: creating Stripe customer - {explain_error(exc)}")
        return None
    Profile.objects.filter(pk=profile.pk, stripe_customer_id="").update(stripe_customer_id=customer.id)
    return Profile.objects.filter(pk=profile.pk).values_list("stripe_customer_id", flat=True).first()


def forget_customer(user, customer_id):
    """The saved Stripe customer doesn't exist for the current keys (keys
    or account switched): drop it so a new one is made next time."""
    from .models import Profile
    if user and customer_id:
        Profile.objects.filter(user=user, stripe_customer_id=customer_id).update(stripe_customer_id="")


def list_cards(customer_id, user=None):
    """Saved cards as plain dicts. Raises PaymentError if Stripe fails."""
    if not customer_id:
        return []
    try:
        result = _plain(_stripe().Customer.list_payment_methods(customer_id, type="card", limit=20))
    except Exception as exc:
        if _is_missing_customer(exc):
            forget_customer(user, customer_id)
            return []
        logger.exception("Could not list cards for %s", customer_id)
        raise PaymentError("We couldn't load your saved cards right now. Please try again shortly.") from exc
    cards = []
    try:
        for pm in result.get("data") or []:
            card = pm.get("card") or {}
            cards.append({
                "id": pm.get("id"),
                "brand": (card.get("display_brand") or card.get("brand") or "card").replace("_", " ").title(),
                "last4": card.get("last4", ""),
                "exp_month": card.get("exp_month"),
                "exp_year": card.get("exp_year"),
            })
    except Exception as exc:  # unexpected response shape: show a message, never a crash
        logger.exception("Unexpected card list for %s", customer_id)
        raise PaymentError("We couldn't load your saved cards right now. Please try again shortly.") from exc
    return cards


def remove_card(customer_id, payment_method_id):
    """Detaches a saved card, only if it belongs to ``customer_id``."""
    if not customer_id or not payment_method_id:
        return False
    stripe = _stripe()
    try:
        pm = _plain(stripe.PaymentMethod.retrieve(payment_method_id))
        if pm.get("customer") != customer_id:
            return False
        stripe.PaymentMethod.detach(payment_method_id)
    except Exception:
        logger.exception("Could not remove card %s", payment_method_id)
        return False
    return True


def _is_request_error(exc):
    """Stripe said the request itself was wrong (HTTP 400/404), as opposed
    to a network problem or a bad API key."""
    return getattr(exc, "http_status", None) in (400, 404)


def _is_missing_customer(exc):
    return getattr(exc, "code", "") == "resource_missing" and "customer" in str(exc).lower()


def create_card_setup_session(request, customer_id, user=None):
    """Stripe-hosted page where the customer adds a card for later.
    If the saved Stripe customer no longer exists (for example after
    switching Stripe accounts or test/live keys), a new one is created and
    the request is tried once more."""
    def _create(cid):
        params = dict(
            mode="setup",
            customer=cid,
            # Stripe's current API takes the payment methods from the
            # Dashboard settings; passing payment_method_types is rejected.
            currency=settings.STORE_CURRENCY.lower(),
            success_url=_absolute(request, reverse("payment_methods")) + "?added={CHECKOUT_SESSION_ID}",
            cancel_url=_absolute(request, reverse("payment_methods")),
        )
        try:
            return _stripe().checkout.Session.create(idempotency_key=f"setup-{cid}-{uuid.uuid4().hex}", **params)
        except Exception as exc:
            # Older Stripe API versions need payment_method_types instead of
            # a currency in setup mode.
            if getattr(exc, "param", "") not in ("currency", "payment_method_types") and "payment_method_types" not in str(exc):
                raise
            params.pop("currency", None)
            params["payment_method_types"] = ["card"]
            return _stripe().checkout.Session.create(idempotency_key=f"setup-{cid}-{uuid.uuid4().hex}", **params)
    try:
        try:
            session = _create(customer_id)
        except Exception as exc:
            if not (user and _is_missing_customer(exc)):
                raise
            from .models import Profile
            Profile.objects.filter(user=user, stripe_customer_id=customer_id).update(stripe_customer_id="")
            new_id = ensure_customer(user)
            if not new_id:
                raise
            session = _create(new_id)
    except Exception as exc:
        reason = explain_error(exc)
        logger.exception("Could not start card setup for %s", customer_id)
        from .alerts import PAYMENT_FAILED, log
        log(f"{PAYMENT_FAILED}: saved-card page - {reason}")
        error = PaymentError("We couldn't open the secure card page. Please try again in a moment.")
        error.reason = reason
        raise error from exc
    from .alerts import payments_working
    payments_working("saved-card page opened")
    return session.url


def finish_card_setup(session_id, customer_id):
    """After a card was added on Stripe's page, mark it so Stripe offers
    it again at checkout. Returns True if a card was saved."""
    stripe = _stripe()
    try:
        session = _plain(stripe.checkout.Session.retrieve(session_id, expand=["setup_intent"]))
        if session.get("customer") != customer_id or session.get("status") != "complete":
            return False
        intent = session.get("setup_intent") or {}
        pm_id = intent.get("payment_method") if isinstance(intent, dict) else None
        if pm_id:
            stripe.PaymentMethod.modify(pm_id, allow_redisplay="always")
    except Exception:
        logger.exception("Could not finish card setup %s", session_id)
        return False
    return True


def delete_customer(customer_id):
    """Deletes the Stripe customer and with it every saved card."""
    if not customer_id or not is_configured():
        return
    try:
        _stripe().Customer.delete(customer_id)
    except Exception:
        logger.exception("Could not delete Stripe customer %s", customer_id)


def void_pending_payment(order):
    """Called when an order that is still waiting for online payment is
    cancelled: closes its Stripe checkout page so it can't be paid any more
    and updates the payment fields (the caller saves the order)."""
    if order.payment_status != "pending":
        return
    if order.stripe_session_id and is_configured():
        try:
            _stripe().checkout.Session.expire(order.stripe_session_id)
        except Exception:
            # Already expired/completed, or Stripe unreachable. If it does
            # get paid later, mark_order_paid refunds it automatically.
            logger.info("Could not expire Stripe session for order %s", order.id)
    if order.cod_fallback:
        order.cod_fallback = False
        order.payment_method = "cod"
        order.payment_status = "not_applicable"
    else:
        order.payment_status = "failed"


def parse_webhook(payload: bytes, signature: str):
    """Verifies the Stripe signature and returns the event as a plain dict.
    Raises ValueError if the payload or signature is invalid."""
    stripe = _stripe()
    try:
        stripe.Webhook.construct_event(payload, signature, settings.STRIPE_WEBHOOK_SECRET)
    except Exception as exc:
        raise ValueError("Invalid Stripe webhook") from exc
    return json.loads(payload)


def refund_order(order):
    """Refunds a paid card order in full, to the card it was paid with.
    Records the refund on the order and its timeline. Returns True on success."""
    from django.utils import timezone
    from .models import Order, OrderEvent
    if order.total_cents == 0:
        return True  # paid entirely with store credit / coupon - nothing to send back to a card
    if not order.stripe_payment_intent:
        return False
    stripe = _stripe()
    try:
        refund = stripe.Refund.create(
            payment_intent=order.stripe_payment_intent,
            idempotency_key=f"refund-order-{order.id}",
        )
    except Exception:
        logger.exception("Stripe refund failed for order %s", order.id)
        return False
    order.refund_id = str(getattr(refund, "id", "") or "")[:80]
    order.refunded_at = timezone.now()
    Order.objects.filter(pk=order.pk).update(refund_id=order.refund_id, refunded_at=order.refunded_at)
    to = f" to {order.card_label}" if order.card_label else " to the original card"
    OrderEvent.objects.create(order=order, kind="refunded", text=f"Refund of {order.total} {order.currency.upper()} sent{to}")
    return True


def remember_card(order):
    """Saves the card brand and last 4 digits used for an order (for the
    receipt and "refunded to Visa •••• 4242"). Best effort, never raises."""
    from .models import Order
    if not order.stripe_payment_intent or order.card_last4 or not getattr(settings, "STRIPE_CARD_DETAILS", True):
        return
    try:
        intent = _plain(_stripe().PaymentIntent.retrieve(order.stripe_payment_intent, expand=["latest_charge"]))
        charge = intent.get("latest_charge") or {}
        card = ((charge.get("payment_method_details") or {}).get("card") or {}) if isinstance(charge, dict) else {}
        brand, last4 = (card.get("brand") or "")[:20], (card.get("last4") or "")[:4]
        if last4:
            Order.objects.filter(pk=order.pk).update(card_brand=brand, card_last4=last4)
            order.card_brand, order.card_last4 = brand, last4
    except Exception:
        logger.info("Could not read card details for order %s", order.id, exc_info=True)


def refund_amount(order, amount, key):
    """Refunds part of a paid card order (e.g. one returned item).
    ``key`` makes the refund idempotent. Returns True on success."""
    if not order.stripe_payment_intent or not amount:
        return False
    stripe = _stripe()
    try:
        stripe.Refund.create(
            payment_intent=order.stripe_payment_intent,
            amount=to_cents(amount, order.currency),
            idempotency_key=f"refund-{key}",
        )
    except Exception:
        logger.exception("Stripe partial refund failed for order %s", order.id)
        return False
    return True


# ---------------------------------------------------------------------------
# Order state changes driven by Stripe. All idempotent: Stripe may deliver the
# same event more than once, and the success page may race the webhook.
# ---------------------------------------------------------------------------

def mark_order_paid(order_id, session):
    """Marks the order paid if ``session`` (a Checkout Session dict) is a
    completed, fully-paid session for exactly this order's amount.
    Returns (order, newly_paid)."""
    from .models import Order

    with transaction.atomic():
        order = Order.objects.select_for_update().filter(pk=order_id).first()
        if not order:
            return None, False
        if order.payment_status == "paid":
            return order, False
        if session.get("id") != order.stripe_session_id:
            logger.warning("Stripe session %s does not match order %s", session.get("id"), order.id)
            return order, False
        if session.get("payment_status") != "paid":
            return order, False
        if session.get("amount_total") != order.total_cents or (session.get("currency") or "").lower() != order.currency.lower():
            logger.error(
                "Amount mismatch for order %s: Stripe %s %s, expected %s %s",
                order.id, session.get("amount_total"), session.get("currency"), order.total_cents, order.currency,
            )
            return order, False
        if order.status == "cancelled":
            # Paid on a checkout page that was still open after the order
            # was cancelled: give the money straight back.
            order.stripe_payment_intent = session.get("payment_intent") or ""
            order.payment_method = "card"
            order.cod_fallback = False
            refunded = refund_order(order)
            order.payment_status = "refunded" if refunded else "paid"
            order.save(update_fields=["payment_status", "payment_method", "cod_fallback", "stripe_payment_intent"])
            if not refunded:
                from .alerts import REFUND_FAILED, notify_staff
                notify_staff(f"Order #{order.id} was paid after being cancelled and the automatic refund failed. Refund it in Stripe.",
                             link=f"/manage/orders/{order.id}/", category=REFUND_FAILED)
            if order.user_id:
                from .models import Notification
                Notification.objects.create(
                    user_id=order.user_id, link="/my-orders/",
                    message=f"Order #{order.id} was already cancelled, so your payment has been refunded." if refunded
                    else f"Order #{order.id} was already cancelled. We'll refund your payment shortly.",
                )
            return order, False
        order.payment_status = "paid"
        order.payment_method = "card"
        order.cod_fallback = False
        order.stripe_payment_intent = session.get("payment_intent") or ""
        if order.status == "pending":
            order.status = "confirmed"
        if not order.estimated_delivery:
            from .order_emails import default_delivery_date
            order.estimated_delivery = default_delivery_date(order)
        # The caller sends the "payment received" email, which already says
        # the order is confirmed - don't send a second one.
        order._skip_status_email = True
        order.save(update_fields=["payment_status", "payment_method", "cod_fallback", "stripe_payment_intent", "status", "estimated_delivery"])
        order._skip_status_email = False
        from .models import OrderEvent
        OrderEvent.objects.create(order=order, kind="paid", text="Payment received by card")
    remember_card(order)
    return order, True


def release_unpaid_order(order_id, reason="failed"):
    """Cancels an unpaid card order and returns its stock. A cash-on-delivery
    order whose advance online payment was abandoned goes back to cash on
    delivery instead."""
    from .models import Order

    with transaction.atomic():
        order = Order.objects.select_for_update().filter(pk=order_id).first()
        if not order or order.payment_status == "paid" or order.status == "cancelled":
            return order
        if order.cod_fallback:
            order.cod_fallback = False
            order.payment_method = "cod"
            order.payment_status = "not_applicable"
            order.stripe_session_id = ""
            order.save(update_fields=["cod_fallback", "payment_method", "payment_status", "stripe_session_id"])
            return order
        order.payment_status = reason
        order.status = "cancelled"
        order.save(update_fields=["payment_status", "status"])
        restock(order)
    return order


def restock_item(item, quantity):
    """Puts ``quantity`` units of an order line back on sale (its size/
    colour too), then lets back-in-stock alerts go out."""
    from django.db.models import F
    from .models import Product, ProductVariant
    if not item.product_id:
        return
    if item.variant_id:
        ProductVariant.objects.filter(pk=item.variant_id).update(stock=F("stock") + quantity)
    Product.objects.filter(pk=item.product_id).update(stock=F("stock") + quantity)
    from .stock_alerts import notify_restocked
    notify_restocked(item.product_id, item.variant_id)


def restock(order):
    """Undoes a cancelled order: puts the stock back and returns any store
    credit the customer spent on it (once)."""
    from django.db.models import F
    from .models import Order, Product, Profile

    for item in order.items.all():
        restock_item(item, item.quantity)
    if order.credit_used and order.user_id:
        returned = Order.objects.filter(pk=order.pk, credit_returned=False).update(credit_returned=True)
        if returned:
            order.credit_returned = True
            Profile.objects.get_or_create(user_id=order.user_id)
            Profile.objects.filter(user_id=order.user_id).update(store_credit=F("store_credit") + order.credit_used)


def self_test(request):
    """Runs the same Stripe calls a real customer triggers, with a $1 test
    order, and reports each step in plain words. Everything it creates is
    cleaned up again. Used by Admin > System check."""
    stripe = _stripe()
    currency = settings.STORE_CURRENCY.lower()
    steps, made = [], {}

    def step(name, fn):
        try:
            result = fn()
            steps.append((name, True, "Works."))
            return result
        except Exception as exc:
            raw = getattr(exc, "user_message", None) or str(exc)
            steps.append((name, False, f"{explain_error(exc)} [{raw[:200]}]"))
            return None

    step("Connection and secret key", lambda: stripe.Balance.retrieve())
    if not steps[-1][1]:
        return steps
    customer = step("Create a customer (for saved cards)", lambda: stripe.Customer.create(
        email="system-check@example.com", name="System check", metadata={"system_check": "1"},
        idempotency_key=f"check-{uuid.uuid4().hex}"))
    if customer:
        made["customer"] = customer.id
    base = {
        "success_url": _absolute(request, reverse("payment_success")) + "?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": _absolute(request, reverse("home")),
    }
    pay = step("Payment page for a guest", lambda: stripe.checkout.Session.create(
        mode="payment", customer_email="system-check@example.com", **base,
        line_items=[{"quantity": 1, "price_data": {"currency": currency, "unit_amount": to_cents(1, currency),
                                                   "product_data": {"name": "System check"}}}],
        idempotency_key=f"check-{uuid.uuid4().hex}"))
    if customer:
        pay2 = step("Payment page for a signed-in customer (saved cards)", lambda: stripe.checkout.Session.create(
            mode="payment", customer=customer.id, saved_payment_method_options={"payment_method_save": "enabled"}, **base,
            line_items=[{"quantity": 1, "price_data": {"currency": currency, "unit_amount": to_cents(1, currency),
                                                       "product_data": {"name": "System check"}}}],
            idempotency_key=f"check-{uuid.uuid4().hex}"))
        setup = step("Add-a-card page", lambda: stripe.checkout.Session.create(
            mode="setup", customer=customer.id, currency=currency, **base,
            idempotency_key=f"check-{uuid.uuid4().hex}"))
    else:
        pay2 = setup = None
    for session in (pay, pay2, setup):
        if session is not None:
            try:
                stripe.checkout.Session.expire(session.id)
            except Exception:
                pass
    if made.get("customer"):
        try:
            stripe.Customer.delete(made["customer"])
        except Exception:
            pass
    if all(ok for _, ok, _ in steps):
        from .alerts import payments_working
        payments_working("full payment test passed")
    return steps
