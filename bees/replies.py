"""Suggested replies for sellers' chats.

The seller presses "Suggest reply" and gets a ready answer to the
customer's latest message, built from facts the store knows: stock and
price of the product they're asking about, delivery dates, the return
window and the status of this customer's order with the seller. With an
AI key the answer is written by AI in the customer's language (only from
those facts); without one, common questions (availability, delivery time,
order status, price, size, returns, cash on delivery) get a template
answer. The seller always reviews and presses Send - nothing goes out on
its own.
"""
import json
import re

INTENTS = [
    ("status", r"\b(where|status|track|tracking|kahan|kab tak|order\s*#?\d*|parcel|shipped)\b"),
    ("delivery", r"\b(deliver|delivery|shipping|ship|arrive|days|kab|kitne din|dispatch)\b"),
    ("stock", r"\b(available|availability|in stock|stock|milega|mil jaye|have it|left)\b"),
    ("price", r"\b(price|discount|cheaper|last price|rate|kam|best price|offer)\b"),
    ("size", r"\b(size|fit|small|large|medium|measurements?|cm|inch)\b"),
    ("return", r"\b(return|exchange|refund|wapas|change it|replace)\b"),
    ("cod", r"\b(cash|cod|cash on delivery|pay at door)\b"),
]


def _intent(text):
    low = (text or "").lower()
    return [name for name, rx in INTENTS if re.search(rx, low)]


def facts(conv):
    from django.utils import timezone
    from .models import Message, OrderItem, SiteSettings
    from . import insights
    site = SiteSettings.load()
    last = conv.messages.filter(from_seller=False).order_by("-created_at").first()
    product = (last.product if last and last.product_id else None) or conv.product
    data = {"store": site.site_name, "seller": conv.seller.display_name, "customer_first_name": (conv.buyer.first_name or "").strip(),
            "return_days": site.return_days, "cash_on_delivery": site.allow_cash_on_delivery}
    if product is not None:
        window = insights.delivery_window(product)
        data["product"] = {"name": product.name, "price": str(product.price), "in_stock": product.stock > 0, "stock": product.stock,
                           "sizes": [v.label for v in product.variants.filter(stock__gt=0)][:12] if product.has_variants else [],
                           "delivery": f"arrives between {window[0]:%a %d %b} and {window[1]:%a %d %b}" if window else "digital download, instant"}
    item = (OrderItem.objects.filter(order__user=conv.buyer, seller_account=conv.seller).select_related("order")
            .order_by("-order__created_at").first())
    if item is not None:
        o = item.order
        data["latest_order"] = {"number": o.id, "status": o.status, "item": item.product_name, "item_status": item.fulfillment_status,
                                "tracking": o.tracking_number, "courier": o.courier_name,
                                "estimated_delivery": o.estimated_delivery.strftime("%d %b") if o.estimated_delivery else "",
                                "placed_days_ago": (timezone.now() - o.created_at).days}
    data["recent_messages"] = [{"from": "seller" if m.from_seller else "customer", "text": m.body[:400]}
                               for m in Message.objects.filter(conversation=conv).order_by("-created_at")[:6]][::-1]
    return data, (last.body if last else "")


def template_reply(data, question):
    name = data.get("customer_first_name")
    hello = f"Hi {name}! " if name else "Hi! "
    p = data.get("product")
    o = data.get("latest_order")
    parts = []
    for intent in _intent(question) or ["thanks"]:
        if intent == "status" and o:
            state = {"pending": "has been received", "confirmed": "is confirmed and being packed", "shipped": "is on its way",
                     "delivered": "was delivered", "cancelled": "was cancelled"}.get(o["status"], o["status"])
            line = f"your order #{o['number']} ({o['item']}) {state}"
            if o["status"] == "shipped" and o["tracking"]:
                line += f" - tracking {o['tracking']}" + (f" with {o['courier']}" if o["courier"] else "")
            if o["estimated_delivery"] and o["status"] in ("confirmed", "shipped"):
                line += f", expected by {o['estimated_delivery']}"
            parts.append(line[0].upper() + line[1:] + ".")
        elif intent == "stock" and p:
            if p["in_stock"]:
                parts.append(f"Yes, {p['name']} is in stock" + (f" in {', '.join(p['sizes'])}" if p["sizes"] else "") + ".")
            else:
                parts.append(f"{p['name']} is sold out right now - tap 'Email me when it's back' on the product page and you'll hear first.")
        elif intent == "delivery":
            if p and p.get("delivery"):
                parts.append(f"Delivery: {p['delivery']}.")
            else:
                parts.append("We ship within 1-2 working days and you'll get a tracking link by email.")
        elif intent == "price" and p:
            parts.append(f"The price is {p['price']}, already our best price. You can also use 'Make an offer' on the product page if it's switched on.")
        elif intent == "size" and p:
            parts.append("Sizes in stock: " + ", ".join(p["sizes"]) + ". If you're between sizes, tell me your measurements and I'll help." if p["sizes"]
                         else "The measurements are in the size guide on the product page - send me yours and I'll recommend one.")
        elif intent == "return":
            parts.append(f"You can return it within {data['return_days']} days of delivery from My orders > Return - it's quick and easy.")
        elif intent == "cod":
            parts.append("Yes, cash on delivery is available at checkout." if data["cash_on_delivery"] else "We take card and online payments at checkout.")
    if not parts:
        parts.append("Thanks for your message! Let me check and get back to you shortly.")
    return hello + " ".join(parts) + f"\n- {data['seller']}"


def draft(conv):
    """(text, used_ai)."""
    from .multistore import store_settings
    data, question = facts(conv)
    if getattr(store_settings, "AI_API_KEY", ""):
        from .ai import _call_claude
        prompt = ("You are helping a seller on an online marketplace answer a customer chat. Write the seller's reply to the "
                  "customer's LAST message: friendly, short (1-4 sentences), in the same language and script the customer used "
                  "(e.g. Roman Urdu, Urdu, Arabic, English). Use ONLY the facts below; never invent prices, dates, discounts or "
                  "promises. If the facts don't answer it, say you'll check and reply soon. No greetings like 'Dear valued "
                  "customer'. Sign with the seller name.\n\nFACTS (JSON):\n" + json.dumps(data, ensure_ascii=False)[:6000])
        try:
            text = _call_claude(prompt, max_tokens=300)
            if text:
                return text.strip()[:1500], True
        except Exception:
            pass
    return template_reply(data, question), False
