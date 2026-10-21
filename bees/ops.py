"""Running the store: outgoing webhooks, stock locations, business
invoices, AI help-chat answers and seller ID checks."""
import hashlib
import hmac
import json
import logging
import urllib.request
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone
from .multistore import store_settings  # per-store values in multi-store mode

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- webhooks

RETRY_MINUTES = [1, 5, 30, 120, 360, 720]  # then give up on that delivery
DISABLE_AFTER = 50                          # failed deliveries in a row


def order_payload(order):
    return {
        "id": order.id, "status": order.status, "payment_status": order.payment_status, "payment_method": order.payment_method,
        "currency": order.currency, "total": str(order.total), "subtotal": str(order.subtotal), "shipping": str(order.shipping_amount),
        "tax": str(order.tax_amount), "discount": str(order.discount_amount), "created_at": order.created_at.isoformat(),
        "customer": {"name": order.full_name, "email": order.email, "phone": order.phone},
        "shipping_address": {"address": order.address, "city": order.city, "state": order.state, "postal_code": order.postal_code,
                             "country": order.country},
        "company": order.company.name if order.company_id else "", "po_number": order.po_number,
        "tracking": {"courier": order.courier_name, "number": order.tracking_number},
        "items": [{"id": i.id, "product_id": i.product_id, "name": i.product_name, "quantity": i.quantity, "price": str(i.price),
                   "seller": i.seller_account.display_name if i.seller_account_id else "", "status": i.fulfillment_status}
                  for i in order.items.select_related("seller_account")],
    }


def emit(event, build):
    """Queues ``event`` for every endpoint that wants it. ``build`` returns
    the data dict; it runs after the database transaction commits."""
    from .models import WebhookEndpoint
    if not WebhookEndpoint.objects.filter(active=True).exists():
        return

    def queue():
        from .models import WebhookDelivery
        endpoints = [e for e in WebhookEndpoint.objects.filter(active=True) if event in (e.events or [])]
        if not endpoints:
            return
        try:
            data = build()
        except Exception:
            logger.exception("Webhook payload for %s failed", event)
            return
        ids = [WebhookDelivery.objects.create(endpoint=e, event=event, payload=data, next_try_at=timezone.now()).pk for e in endpoints]
        from .background import after_response
        after_response(deliver_many, ids)
    transaction.on_commit(queue)


def sign(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def deliver(delivery):
    """One attempt. Returns True when the other app answered 2xx."""
    from .models import WebhookDelivery, WebhookEndpoint
    ep = delivery.endpoint
    body = json.dumps({"id": delivery.pk, "event": delivery.event, "created_at": delivery.created_at.isoformat(),
                       "data": delivery.payload}, separators=(",", ":"), default=str).encode()
    req = urllib.request.Request(ep.url, data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": "LumenMarket-Webhooks/1.0", "X-Lumen-Event": delivery.event,
        "X-Lumen-Delivery": str(delivery.pk), "X-Lumen-Signature": sign(ep.secret, body)})
    code, error = None, ""
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            code = resp.status
    except urllib.error.HTTPError as exc:
        code, error = exc.code, f"HTTP {exc.code}"
    except Exception as exc:
        error = str(exc)[:200] or exc.__class__.__name__
    ok = code is not None and 200 <= code < 300
    now = timezone.now()
    delivery.attempts += 1
    delivery.status_code, delivery.error = code, error[:200]
    if ok:
        delivery.delivered_at, delivery.next_try_at = now, None
        WebhookEndpoint.objects.filter(pk=ep.pk).update(failures=0, last_status=f"OK ({code})", last_sent_at=now)
    else:
        wait = RETRY_MINUTES[delivery.attempts - 1] if delivery.attempts <= len(RETRY_MINUTES) else None
        delivery.next_try_at = now + timedelta(minutes=wait) if wait else None
        WebhookEndpoint.objects.filter(pk=ep.pk).update(failures=F("failures") + 1, last_status=(error or f"HTTP {code}")[:120], last_sent_at=now)
        ep.refresh_from_db(fields=["failures"])
        if ep.failures >= DISABLE_AFTER:
            WebhookEndpoint.objects.filter(pk=ep.pk).update(active=False)
            from .alerts import notify_staff
            notify_staff(f"Webhook to {ep.url[:60]} was turned off after {DISABLE_AFTER} failed deliveries.", link="/manage/integrations/")
    WebhookDelivery.objects.filter(pk=delivery.pk).update(attempts=delivery.attempts, status_code=code, error=delivery.error,
                                                          delivered_at=delivery.delivered_at, next_try_at=delivery.next_try_at)
    return ok


def deliver_many(ids):
    from .models import WebhookDelivery
    for d in WebhookDelivery.objects.filter(pk__in=ids, delivered_at=None).select_related("endpoint"):
        deliver(d)


def retry_due():
    """Called by the scheduled job: retries failed deliveries."""
    from .models import WebhookDelivery
    due = WebhookDelivery.objects.filter(delivered_at=None, next_try_at__lte=timezone.now(), endpoint__active=True).select_related("endpoint")
    n = 0
    for d in due[:200]:
        deliver(d)
        n += 1
    return n


def connect_signals():
    from django.contrib.auth.models import User
    from django.db.models.signals import post_save
    from .models import OrderEvent, Product, Review
    kinds = {"placed": "order.created", "paid": "order.paid", "shipped": "order.shipped", "delivered": "order.delivered",
             "cancelled": "order.cancelled"}

    def on_event(sender, instance, created, **kwargs):
        if created and instance.kind in kinds:
            order_id = instance.order_id
            from .models import Order
            emit(kinds[instance.kind], lambda: order_payload(Order.objects.get(pk=order_id)))

    def on_product(sender, instance, created, update_fields=None, **kwargs):
        if update_fields and set(update_fields) <= {"stock", "updated_at"}:
            return  # stock moves on every order; not worth a message
        pid = instance.pk
        emit("product.updated", lambda: _product_payload(pid))

    def on_review(sender, instance, created, **kwargs):
        if created:
            rid = instance.pk
            emit("review.created", lambda: _review_payload(rid))

    def on_user(sender, instance, created, **kwargs):
        if created and not instance.is_staff:
            emit("customer.created", lambda: {"id": instance.pk, "email": instance.email, "name": instance.get_full_name(),
                                              "joined": instance.date_joined.isoformat()})
    post_save.connect(on_event, sender=OrderEvent, dispatch_uid="wh_order_event")
    post_save.connect(on_product, sender=Product, dispatch_uid="wh_product")
    post_save.connect(on_review, sender=Review, dispatch_uid="wh_review")
    post_save.connect(on_user, sender=User, dispatch_uid="wh_user")


def _product_payload(pid):
    from .models import Product
    p = Product.objects.get(pk=pid)
    return {"id": p.id, "name": p.name, "price": str(p.price), "old_price": str(p.old_price) if p.old_price is not None else None,
            "stock": p.stock, "category": p.category, "brand": p.brand, "gtin": p.gtin, "status": p.approval_status,
            "seller": p.seller_name, "image_url": p.image_url}


def _review_payload(rid):
    from .models import Review
    r = Review.objects.select_related("product").get(pk=rid)
    return {"id": r.id, "product_id": r.product_id, "product": r.product.name, "rating": r.rating, "comment": r.comment,
            "author": r.username, "verified": r.is_verified_purchase}


def send_test(endpoint):
    from .models import WebhookDelivery
    d = WebhookDelivery.objects.create(endpoint=endpoint, event="ping", payload={"message": "Test from your store"})
    return deliver(d)


# ---------------------------------------------------------------- stock locations

def has_levels(product_id, variant_id=None):
    from .models import StockLevel
    return StockLevel.objects.filter(product_id=product_id, variant_id=variant_id, warehouse__active=True).exists()


def sync_total(product_id, variant_id=None):
    """Product (or size/colour) stock = the sum of its locations."""
    from .models import Product, ProductVariant, StockLevel
    total = StockLevel.objects.filter(product_id=product_id, variant_id=variant_id, warehouse__active=True).aggregate(n=Sum("quantity"))["n"] or 0
    total = max(total, 0)
    if variant_id:
        ProductVariant.objects.filter(pk=variant_id).update(stock=total)
        if Product.objects.filter(pk=product_id, has_variants=True).exists():
            vt = ProductVariant.objects.filter(product_id=product_id).aggregate(n=Sum("stock"))["n"] or 0
            Product.objects.filter(pk=product_id).update(stock=vt)
    else:
        Product.objects.filter(pk=product_id).update(stock=total)
    return total


def pick_location(product_id, variant_id, qty, country=""):
    """The location an order line ships from: one in the buyer's country
    that has enough, else the first (by priority) with enough, else the one
    with the most."""
    from .models import StockLevel
    levels = list(StockLevel.objects.select_related("warehouse").filter(product_id=product_id, variant_id=variant_id, warehouse__active=True)
                  .order_by("warehouse__priority", "warehouse_id"))
    if not levels:
        return None
    enough = [lv for lv in levels if lv.quantity >= qty]
    local = [lv for lv in enough if country and lv.warehouse.country == country.upper()]
    return (local or enough or sorted(levels, key=lambda lv: -lv.quantity))[0]


def take_stock(item, country=""):
    """After an order line is saved: reduce the chosen location's stock and
    remember where it ships from."""
    from .models import StockLevel
    level = pick_location(item.product_id, item.variant_id, item.quantity, country)
    if level is None:
        return None
    StockLevel.objects.filter(pk=level.pk).update(quantity=F("quantity") - item.quantity)
    item.warehouse_id = level.warehouse_id
    type(item).objects.filter(pk=item.pk).update(warehouse=level.warehouse_id)
    return level.warehouse


def put_back(item, quantity):
    """Restock: the units go back to the location they shipped from."""
    from .models import StockLevel
    if not item.warehouse_id:
        return
    StockLevel.objects.filter(warehouse_id=item.warehouse_id, product_id=item.product_id, variant_id=item.variant_id).update(
        quantity=F("quantity") + quantity)


# ---------------------------------------------------------------- business accounts & invoices

def membership(user):
    from .models import CompanyMember
    if not getattr(user, "is_authenticated", False):
        return None
    return CompanyMember.objects.select_related("company").filter(user=user).first()


def company_for(user):
    """The approved business account this shopper buys for, or None."""
    member = membership(user)
    return member.company if member and member.company.status == "approved" else None


def can_pay_by_invoice(company, amount):
    return bool(company and company.payment_terms and amount <= company.invoice_room())


def overdue_invoices():
    from .models import Order
    return Order.objects.filter(payment_status="invoiced", payment_due__lt=timezone.localdate()).exclude(status="cancelled")


def remind_invoices():
    """Daily: a reminder 3 days before the due date and weekly once overdue."""
    from .models import Order
    from .order_emails import absolute
    from .views import _send_html_email
    today = timezone.localdate()
    sent = 0
    for order in Order.objects.filter(payment_status="invoiced").exclude(status="cancelled").select_related("company")[:500]:
        if not order.payment_due:
            continue
        days = (order.payment_due - today).days
        last = order.invoice_reminded_at
        due_soon = days == 3
        overdue = days < 0 and (not last or last < timezone.now() - timedelta(days=7))
        if not (due_soon or overdue):
            continue
        to = order.company.billing_email if order.company_id else order.email
        _send_html_email(f"Invoice for order #{order.id} is {'overdue' if days < 0 else 'due soon'}", "bees/emails/invoice_reminder.html",
                         {"order": order, "days": days, "invoice_url": absolute(f"/order/{order.id}/invoice/")}, to)
        Order.objects.filter(pk=order.pk).update(invoice_reminded_at=timezone.now())
        sent += 1
    return sent


# ---------------------------------------------------------------- AI help chat

def ai_support_answer(text, request, thread):
    """Answers a help-chat question from the store's real rules and the
    customer's own orders. Returns (answer, needs_person) or None when AI
    isn't available."""
    from . import assistant
    from .models import Order, SiteSettings
    site = SiteSettings.load()
    if not site.support_ai or not getattr(store_settings, "AI_API_KEY", ""):
        return None
    facts = [f"Store: {site.site_name}. Currency: {store_settings.STORE_CURRENCY.upper()}.",
             f"Returns: within {site.return_days} days of delivery, from My orders.",
             f"Payment methods: {', '.join(assistant._payment_methods())}.",
             f"Shipping: {assistant._shipping_text()}",
             f"Support email: {site.support_email or 'use this chat'}."]
    if request.user.is_authenticated:
        for o in Order.objects.filter(user=request.user).order_by("-id")[:3]:
            items = ", ".join(f"{i.product_name} x{i.quantity} ({i.get_fulfillment_status_display()})" for i in o.items.all()[:6])
            facts.append(f"Customer order #{o.id} ({o.created_at:%d %b %Y}): status {o.get_status_display()}, payment {o.get_payment_status_display()}, "
                         f"total {o.total} {o.currency}, courier {o.courier_name or '-'} tracking {o.tracking_number or '-'}, "
                         f"estimated delivery {o.estimated_delivery or '-'}. Items: {items}.")
    else:
        facts.append("The customer isn't signed in, so you can't see their orders; ask them to sign in for order questions.")
    history = [f"{'Customer' if m.sender == 'user' else 'Store'}: {m.message[:400]}" for m in thread.messages.order_by("-id")[1:7]][::-1]
    prompt = ("You answer customer questions in an online shop's help chat. Use ONLY the facts below; never invent policies, "
              "prices, dates or order details. Be short and friendly (max 4 sentences), in the customer's language. If the facts "
              "don't answer it, or it needs a refund, cancellation decision, complaint or anything sensitive, say a team member "
              "will reply here and set handoff to true.\nReply with JSON only: {\"answer\": \"...\", \"handoff\": true|false}\n\n"
              "FACTS:\n" + "\n".join(facts) + ("\n\nEARLIER IN THIS CHAT:\n" + "\n".join(history) if history else "")
              + "\n\nCUSTOMER: " + text[:1500])
    body = json.dumps({"model": settings.AI_MODEL, "max_tokens": 400, "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST", headers={
        "x-api-key": store_settings.AI_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            out = json.loads(resp.read().decode())
        raw = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
        data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except Exception:
        logger.exception("AI help chat failed")
        return None
    answer = str(data.get("answer") or "").strip()[:1200]
    if not answer:
        return None
    return answer, bool(data.get("handoff"))


# ---------------------------------------------------------------- seller ID checks (Stripe Identity)

def identity_available():
    from . import payments
    from .models import SiteSettings
    return payments.is_configured() and SiteSettings.load().identity_checks


def start_identity(request, seller):
    """Creates a Stripe Identity session and returns its URL."""
    from . import payments
    stripe = payments._stripe()
    session = stripe.identity.VerificationSession.create(
        type="document", options={"document": {"require_matching_selfie": True}},
        metadata={"seller_id": str(seller.pk)},
        return_url=request.build_absolute_uri("/seller/settings/?identity=done"))
    type(seller).objects.filter(pk=seller.pk).update(identity_session=session["id"], identity_status="pending")
    return session["url"]


def identity_event(event_type, obj):
    """Stripe webhook: identity.verification_session.* events."""
    from .models import Notification, SellerAccount
    seller = SellerAccount.objects.filter(identity_session=obj.get("id", "")).first()
    if not seller:
        return
    if event_type == "identity.verification_session.verified":
        SellerAccount.objects.filter(pk=seller.pk).update(identity_status="verified", verified_at=seller.verified_at or timezone.now())
        Notification.objects.create(user=seller.user, link="/seller/settings/", message="Your ID check passed - your store now shows the Verified badge.")
    elif event_type in ("identity.verification_session.requires_input", "identity.verification_session.canceled"):
        SellerAccount.objects.filter(pk=seller.pk).update(identity_status="failed")
        Notification.objects.create(user=seller.user, link="/seller/settings/", message="Your ID check didn't go through. You can try again from Store settings.")
