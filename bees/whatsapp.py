"""Shopping on WhatsApp.

* "Order on WhatsApp" buttons (product page and cart) open a chat with the
  store's WhatsApp number with the order already written.
* With a Twilio WhatsApp sender, the store's number answers by itself
  (webhook at /whatsapp/incoming/, signature-checked):
    - a message with products in it ("... (#108) x2") -> a one-tap link
      that puts them in the cart and opens checkout;
    - YES / CONFIRM / HAAN -> confirms the customer's waiting cash order;
    - NO / CANCEL -> cancels a cash order that hasn't shipped;
    - STATUS -> where their latest order is;
    - anything else -> a short menu (and the team still sees the chat in Twilio).
  Customers are matched by the phone number WhatsApp gives us, so nobody
  can confirm or cancel someone else's order.
"""
import base64
import hashlib
import hmac
import re
from urllib.parse import quote

from django.core import signing
from django.urls import reverse

SALT = "wa-cart"
YES = {"yes", "y", "confirm", "ok", "haan", "han", "ji", "jee", "1", "نعم", "ہاں", "si", "oui"}
NO = {"no", "n", "cancel", "nahi", "nahin", "2", "لا", "نہیں"}
STATUS = {"status", "order", "track", "where", "kahan", "3"}
ITEM = re.compile(r"\(#(\d{1,9})(?:-(\d{1,9}))?\)\s*(?:x\s*(\d{1,3}))?", re.I)
URL_ITEM = re.compile(r"/product/(\d{1,9})/")


def store_number():
    from .models import SiteSettings
    return "".join(ch for ch in (SiteSettings.load().whatsapp_number or "") if ch.isdigit())


def enabled():
    return bool(store_number())


def chat_link(text):
    return f"https://wa.me/{store_number()}?text={quote(text[:1500])}"


def product_link(request, product, qty=1, variant=None):
    from .models import SiteSettings
    name = product.name + (f" ({variant.label})" if variant else "")
    ref = f"#{product.pk}" + (f"-{variant.pk}" if variant else "")
    url = request.build_absolute_uri(reverse("product_detail", args=[product.pk]))
    text = (f"Hi {SiteSettings.load().site_name}! I'd like to order:\n"
            f"• {name} ({ref}) x{qty}\n{url}")
    return chat_link(text)


def cart_link(request, rows):
    from .models import SiteSettings
    lines = [f"• {r['product'].name}{' (' + r['label'] + ')' if r.get('label') and not r.get('quote') else ''} "
             f"(#{r['product'].pk}{'-' + str(r['variant'].pk) if r.get('variant') else ''}) x{r['qty']}"
             for r in rows if not r.get("quote") and not r.get("subscription")][:20]
    if not lines:
        return ""
    return chat_link(f"Hi {SiteSettings.load().site_name}! I'd like to order:\n" + "\n".join(lines))


# ---------------------------------------------------------------- one-tap cart links

def checkout_token(items):
    """items: [(product_id, variant_id or None, qty)] -> signed token."""
    return signing.dumps([[int(p), int(v) if v else 0, max(1, min(int(q), 99))] for p, v, q in items[:20]], salt=SALT, compress=True)


def read_token(token, max_age=7 * 24 * 3600):
    return signing.loads(token, salt=SALT, max_age=max_age)


def parse_items(text):
    """Products named in a message: '(#108) x2', '(#4-12) x1' or product links."""
    found = {}
    for m in ITEM.finditer(text or ""):
        key = (int(m.group(1)), int(m.group(2) or 0))
        found[key] = found.get(key, 0) + int(m.group(3) or 1)
    for m in URL_ITEM.finditer(text or ""):
        found.setdefault((int(m.group(1)), 0), 1)
    return [(p, v, q) for (p, v), q in found.items()][:20]


# ---------------------------------------------------------------- Twilio webhook

def valid_signature(request):
    """Twilio signs the full URL plus the sorted POST fields with the auth
    token (HMAC-SHA1, base64). Both http and https forms are tried because
    proxies may change the scheme Django sees."""
    from .sms import _env
    token = _env("TWILIO_AUTH_TOKEN")
    given = request.META.get("HTTP_X_TWILIO_SIGNATURE", "")
    if not token or not given:
        return False
    params = "".join(f"{k}{v}" for k, v in sorted(request.POST.items()))
    url = request.build_absolute_uri()
    for candidate in {url, url.replace("http://", "https://", 1)}:
        digest = base64.b64encode(hmac.new(token.encode(), (candidate + params).encode(), hashlib.sha1).digest()).decode()
        if hmac.compare_digest(digest, given):
            return True
    return False


def _orders_for(phone):
    """Orders placed with this WhatsApp number (matched on the last 10 digits)."""
    from .models import Order
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 8:
        return Order.objects.none()
    tail = digits[-10:]
    ids = [o.pk for o in Order.objects.filter(phone__contains=tail[-7:]).order_by("-created_at")[:50]
           if "".join(ch for ch in o.phone if ch.isdigit()).endswith(tail)]
    return Order.objects.filter(pk__in=ids).order_by("-created_at")


def reply_for(phone, body, absolute):
    """The text the store answers with. ``absolute`` turns a path into a full URL."""
    from .models import OrderEvent, SiteSettings
    from . import payments
    site = SiteSettings.load()
    text = (body or "").strip()
    word = text.lower().strip(" .!")
    orders = _orders_for(phone)

    items = parse_items(text)
    if items:
        from .models import Product
        live = set(Product.objects.live().filter(pk__in=[p for p, _v, _q in items]).values_list("pk", flat=True))
        items = [i for i in items if i[0] in live]
        if items:
            link = absolute(reverse("whatsapp_cart", args=[checkout_token(items)]))
            return (f"Thanks! Tap to check your order and choose how to pay (cash on delivery, card or wallet):\n{link}\n"
                    "The link works for 7 days.")
        return "Sorry, those products aren't available right now. Reply MENU to see what I can do."

    waiting = orders.filter(payment_method="cod", cod_confirmed=False, status="pending").first()
    if word in YES:
        if waiting is None:
            return "You don't have a cash order waiting for confirmation. Reply STATUS to see your latest order."
        from .models import Order
        if Order.objects.filter(pk=waiting.pk, cod_confirmed=False).update(cod_confirmed=True, cod_otp_hash=""):
            OrderEvent.objects.create(order=waiting, kind="note", text="Cash on delivery order confirmed on WhatsApp")
            from .views import send_order_confirmation_for
            waiting.cod_confirmed = True
            send_order_confirmation_for(waiting)
        return f"✅ Order #{waiting.id} is confirmed. Please keep {waiting.total} {waiting.currency.upper()} ready in cash for the rider."
    if word in NO:
        target = waiting or orders.filter(payment_method="cod", status__in=["pending", "confirmed"]).first()
        if target is None or not target.is_cancellable:
            return "There's no cash order I can cancel here. If it has shipped, reply STATUS or contact us."
        if any(i.fulfillment_status != "pending" for i in target.items.all()):
            return f"Order #{target.id} is already being packed, so it can't be cancelled here. Please contact us."
        from django.db import transaction
        with transaction.atomic():
            target.status, target.cancel_reason = "cancelled", "changed my mind"
            target.save(update_fields=["status", "cancel_reason"])
            payments.restock(target)
        OrderEvent.objects.create(order=target, kind="note", text="Cancelled by the customer on WhatsApp")
        return f"Order #{target.id} has been cancelled. Nothing to pay. We hope to see you again!"
    if word in STATUS:
        latest = orders.first()
        if latest is None:
            return f"I couldn't find an order for this number. Shop at {absolute('/')}"
        state = {"pending": "received", "confirmed": "confirmed and being packed", "shipped": "on its way",
                 "delivered": "delivered", "cancelled": "cancelled"}.get(latest.status, latest.status)
        extra = f" Tracking: {latest.tracking_number}." if latest.tracking_number and latest.status == "shipped" else ""
        code = (f" Your delivery code is {latest.delivery_code} - give it to the rider only when you have the parcel."
                if latest.delivery_code and latest.status in ("confirmed", "shipped") and latest.payment_method == "cod" else "")
        return f"Order #{latest.id} is {state}.{extra}{code} Details: {absolute(latest.tracking_path())}"
    hint = (f"You have cash order #{waiting.id} waiting: reply YES to confirm it or NO to cancel.\n" if waiting else "")
    return (f"Hi from {site.site_name}! 👋\n{hint}"
            "• Send a product link to order it\n• STATUS - where's my order?\n• YES / NO - confirm or cancel a cash order\n"
            f"Shop: {absolute('/')}")


def twiml(text):
    from xml.sax.saxutils import escape
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escape(text)}</Message></Response>'
