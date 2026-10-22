"""Smart store tools for owners and sellers:

* restock forecast - days of stock left and how many to reorder
* order risk check - a 0-100 score with plain-language reasons
* price-drop protection - shoppers get the difference back as store
  credit when a price falls soon after they bought
* customer segments - VIP, loyal, promising, new, at risk, lost (RFM)
* store copilot - quick insights, and answers in plain words when an
  AI key is set
"""
import json
import math
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

CENT = Decimal("0.01")

# ---------------------------------------------------------------- restock

LEAD_DAYS = 14     # time to get new stock in
COVER_DAYS = 30    # how long a reorder should last


def _units_sold(product_ids, days):
    from .models import OrderItem
    since = timezone.now() - timedelta(days=days)
    rows = (OrderItem.objects.counted().filter(order__created_at__gte=since, product_id__in=product_ids)
            .values("product_id").annotate(n=Sum("quantity")))
    return {r["product_id"]: r["n"] or 0 for r in rows}


def restock_plan(products, days=30, lead_days=LEAD_DAYS, cover_days=COVER_DAYS):
    """One row per product: sold in the window, daily rate, days left,
    suggested reorder and a status (out / urgent / soon / ok / slow).
    Digital and auction items are skipped. Most urgent first."""
    products = [p for p in products if not p.is_digital and not p.is_auction]
    sold = _units_sold([p.pk for p in products], days)
    rows = []
    for p in products:
        units = sold.get(p.pk, 0)
        rate = units / days
        stock = p.stock or 0
        days_left = (stock / rate) if rate else None
        reorder = max(math.ceil(rate * (lead_days + cover_days) - stock), 0) if rate else 0
        if rate and stock <= 0:
            status = "out"
        elif days_left is not None and days_left <= lead_days:
            status = "urgent"
        elif days_left is not None and days_left <= lead_days + 14:
            status = "soon"
        elif not units and stock > 0:
            status = "slow"
        else:
            status = "ok"
        rows.append({"product": p, "sold": units, "per_week": round(rate * 7, 1), "stock": stock,
                     "days_left": None if days_left is None else int(days_left), "reorder": reorder, "status": status})
    order = {"out": 0, "urgent": 1, "soon": 2, "ok": 3, "slow": 4}
    rows.sort(key=lambda r: (order[r["status"]], r["days_left"] if r["days_left"] is not None else 10 ** 6, -r["sold"]))
    return rows


# ---------------------------------------------------------------- order risk

DISPOSABLE = {"mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "temp-mail.org", "yopmail.com",
              "trashmail.com", "sharklasers.com", "getnada.com", "dispostable.com", "maildrop.cc", "throwawaymail.com",
              "fakeinbox.com", "mintemail.com", "mohmal.com", "emailondeck.com"}


def _avg_order_value():
    from django.core.cache import cache
    value = cache.get("smart:aov")
    if value is None:
        from .models import OrderItem
        rows = (OrderItem.objects.counted().filter(order__created_at__gte=timezone.now() - timedelta(days=90))
                .values("order_id").annotate(v=Sum(F("price") * F("quantity"))))
        totals = [r["v"] for r in rows if r["v"]]
        value = (sum(totals) / len(totals)) if totals else Decimal("0")
        cache.set("smart:aov", value, 3600)
    return Decimal(value)


def order_risk(order):
    """{'score': 0-100, 'level': 'low'|'medium'|'high', 'reasons': [...]}.
    Rules a fraud analyst would check first - nothing is sent anywhere."""
    from .models import Order
    score, reasons = 0, []

    def flag(points, why):
        nonlocal score
        score += points
        reasons.append(why)

    email = (order.email or order.guest_email or "").lower()
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    value = order.subtotal
    aov = _avg_order_value()
    others = Order.objects.exclude(pk=order.pk)
    same_buyer = others.filter(email__iexact=email) if email else others.none()
    if order.phone and len(order.phone) >= 6:
        same_buyer = same_buyer | others.filter(phone=order.phone)
    earlier = same_buyer.filter(created_at__lt=order.created_at)
    first_order = not earlier.exclude(status="cancelled").exists()

    if domain in DISPOSABLE:
        flag(30, "Throwaway email address")
    if order.user_id is None:
        flag(8, "Guest checkout")
    elif order.user.date_joined and order.created_at - order.user.date_joined < timedelta(hours=1):
        flag(10, "Account created minutes before ordering")
    if aov and value > aov * 5:
        flag(25, f"Order is {int(value / aov)}× the store's average")
    elif aov and value > aov * 3:
        flag(12, f"Order is {int(value / aov)}× the store's average")
    if first_order and aov and value > aov * 2:
        flag(10, "Large first order")
    if earlier.filter(cod_refused=True).exists() or earlier.filter(status="cancelled", payment_method="cod").count() >= 2:
        flag(30, "Earlier cash orders were refused or cancelled")
    if earlier.filter(payment_status="failed", created_at__gte=order.created_at - timedelta(days=1)).count() >= 2:
        flag(20, "Several failed card payments in the last day")
    burst = same_buyer.filter(created_at__gte=order.created_at - timedelta(hours=24), created_at__lte=order.created_at + timedelta(hours=24)).count()
    if burst >= 3:
        flag(15, f"{burst + 1} orders within 24 hours")
    if order.payment_method == "cod":
        if not order.phone or len("".join(c for c in order.phone if c.isdigit())) < 7:
            flag(15, "Cash order without a usable phone number")
        if aov and value > aov * 2:
            flag(10, "High-value cash order")
    countries = set(earlier.exclude(country="").values_list("country", flat=True)[:20])
    if countries and order.country and order.country not in countries:
        flag(10, "Ships to a different country than earlier orders")
    if any(item.quantity >= 5 for item in order.items.all()) and first_order:
        flag(8, "Many of the same item on a first order")
    try:
        from .trustcheck import network_signals
        for points, why in network_signals(order):
            flag(points, why)
    except Exception:
        pass
    if not reasons:
        reasons.append("Nothing unusual")
    score = min(score, 100)
    return {"score": score, "level": "high" if score >= 50 else "medium" if score >= 25 else "low", "reasons": reasons}


# ---------------------------------------------------------------- price-drop protection

def current_price(item):
    """What the same item costs today (variant price when it has one)."""
    product = item.product
    if item.variant_id and item.variant and item.variant.price is not None:
        return item.variant.price
    return product.price


def price_protection(now=None):
    """Credits the difference when a price drops within the protection
    window (Store settings). Once per drop; returns how many credits."""
    from .models import Notification, OrderItem, Profile, SiteSettings
    days = SiteSettings.load().price_protection_days
    if not days:
        return 0
    now = now or timezone.now()
    items = (OrderItem.objects.counted().filter(order__created_at__gte=now - timedelta(days=days), order__user__isnull=False,
                                                product__isnull=False, quote__isnull=True)
             .exclude(fulfillment_status="cancelled").exclude(product__is_auction=True)
             # Automatic price moves (repricer, price tests) aren't covered - only real price drops.
             .exclude(product__reprice_rule__active=True).exclude(product__price_tests__status="running")
             .select_related("product", "variant", "order__user"))
    paid = 0
    for item in items[:5000]:
        if item.return_requests.exclude(status="rejected").exists():
            continue
        today = current_price(item)
        if today is None or today >= item.price:
            continue
        owed = ((item.price - today) * item.quantity - item.protected_credit).quantize(CENT, rounding=ROUND_HALF_UP)
        if owed < CENT:
            continue
        with transaction.atomic():
            if not OrderItem.objects.filter(pk=item.pk, protected_credit=item.protected_credit).update(protected_credit=F("protected_credit") + owed):
                continue
            profile, _ = Profile.objects.get_or_create(user=item.order.user)
            Profile.objects.filter(pk=profile.pk).update(store_credit=F("store_credit") + owed)
        Notification.objects.create(user=item.order.user, link="/profile/",
                                    message=f"Price drop protection: '{item.product_name}' got cheaper, so we added {owed} to your store credit."[:255])
        paid += 1
    return paid


# ---------------------------------------------------------------- customer segments

SEGMENTS = [
    ("vip", "VIP", "Bought often, recently and spent the most. Reward them first."),
    ("loyal", "Loyal", "Order again and again. Early access and points keep them."),
    ("promising", "Promising", "Bought recently, a second order makes them loyal."),
    ("new", "New", "First order in the last 30 days. A welcome series helps."),
    ("at_risk", "At risk", "Used to buy often but not for 90+ days. Win them back now."),
    ("lost", "Lost", "One order long ago. A strong offer or let them go."),
]
SEGMENT_LABELS = {k: label for k, label, _ in SEGMENTS}


def customers(now=None):
    """{email: {'orders', 'spent', 'last', 'first', 'name', 'segment'}} for
    every customer with a counted order."""
    from .models import Order, OrderItem
    now = now or timezone.now()
    counted = Order.objects.exclude(status="cancelled").exclude(payment_status__in=["pending", "failed", "refunded"])
    totals = dict(OrderItem.objects.filter(order__in=counted).values("order_id")
                  .annotate(v=Sum(F("price") * F("quantity"))).values_list("order_id", "v"))
    people = {}
    for oid, email, guest, name, created, discount in counted.values_list("id", "email", "guest_email", "full_name", "created_at", "discount_amount"):
        key = (email or guest or "").lower()
        if not key:
            continue
        p = people.setdefault(key, {"email": key, "orders": 0, "spent": Decimal("0"), "last": created, "first": created, "name": name})
        p["orders"] += 1
        p["spent"] += max((totals.get(oid) or Decimal("0")) - (discount or 0), Decimal("0"))
        if created > p["last"]:
            p["last"], p["name"] = created, name or p["name"]
        p["first"] = min(p["first"], created)
    if not people:
        return {}
    spends = sorted(p["spent"] for p in people.values())
    top_spend = spends[int(len(spends) * 0.8)] if len(spends) >= 5 else spends[-1]
    for p in people.values():
        days = (now - p["last"]).days
        p["days"] = days
        if p["orders"] >= 3 and days <= 60 and p["spent"] >= top_spend:
            seg = "vip"
        elif p["orders"] >= 2 and days <= 90:
            seg = "loyal"
        elif p["orders"] >= 2:
            seg = "at_risk" if days <= 365 else "lost"
        elif days <= 30:
            seg = "new" if (now - p["first"]).days <= 30 else "promising"
        elif days <= 90:
            seg = "promising"
        else:
            seg = "lost"
        p["segment"] = seg
    return people


def segment_summary(people=None):
    people = customers() if people is None else people
    total_spent = sum((p["spent"] for p in people.values()), Decimal("0")) or Decimal("1")
    out = []
    for key, label, hint in SEGMENTS:
        members = [p for p in people.values() if p["segment"] == key]
        spent = sum((p["spent"] for p in members), Decimal("0"))
        out.append({"key": key, "label": label, "hint": hint, "count": len(members), "spent": spent,
                    "share": round(float(spent / total_spent * 100), 1) if members else 0,
                    "avg": (spent / len(members)).quantize(CENT) if members else Decimal("0"),
                    "top": sorted(members, key=lambda p: -p["spent"])[:8]})
    return out


def segment_emails(segment):
    from django.core.cache import cache
    key = f"smart:segment:{segment}"
    emails = cache.get(key)
    if emails is None:
        emails = sorted(e for e, p in customers().items() if p["segment"] == segment)
        cache.set(key, emails, 300)
    return emails


# ---------------------------------------------------------------- store copilot

def snapshot():
    """The numbers the copilot (and quick insights) work from."""
    from . import analytics
    from .models import Order, Product
    from .multistore import store_settings
    today = timezone.localdate()
    week = analytics.report(today - timedelta(days=6), today)
    before = analytics.report(today - timedelta(days=13), today - timedelta(days=7))
    month = analytics.report(today - timedelta(days=29), today)
    listed = list(Product.objects.listed()[:2000])
    plan = restock_plan(listed)
    seg = segment_summary()
    risky = []
    for o in Order.objects.filter(status__in=["pending", "confirmed"]).order_by("-created_at")[:40]:
        r = order_risk(o)
        if r["level"] == "high":
            risky.append({"order": o.pk, "score": r["score"], "why": r["reasons"][:3]})
    return {
        "currency": store_settings.STORE_CURRENCY.upper(),
        "last_7_days": {k: week[k] for k in ("revenue", "orders", "aov", "customers", "conversion", "refund_rate", "return_rate")},
        "previous_7_days": {k: before[k] for k in ("revenue", "orders", "aov")},
        "last_30_days": {k: month[k] for k in ("revenue", "orders", "aov", "repeat_rate", "cancel_rate")},
        "top_products_30d": [{"name": r["product_name"], "units": r["units"], "revenue": r["revenue"]} for r in month["top_products"][:8]],
        "top_categories_30d": [{"name": r["label"], "revenue": r["revenue"]} for r in month["by_category"][:6]],
        "top_countries_30d": [{"name": r["name"], "orders": r["orders"]} for r in month["by_country"][:5]],
        "searches_with_no_results_30d": [{"query": r["key"], "times": r["n"]} for r in month["zero_searches"][:8]],
        "funnel_30d": [{"step": f["label"], "count": f["n"]} for f in month["funnel"]],
        "running_out": [{"name": r["product"].name, "stock": r["stock"], "days_left": r["days_left"], "reorder": r["reorder"]}
                        for r in plan if r["status"] in ("out", "urgent")][:10],
        "not_selling": [{"name": r["product"].name, "stock": r["stock"]} for r in plan if r["status"] == "slow"][:10],
        "customer_segments": [{"segment": s["label"], "customers": s["count"], "revenue_share_pct": s["share"]} for s in seg],
        "risky_open_orders": risky[:10],
    }


def _pct_change(now, before):
    if not before:
        return None
    return round(float((Decimal(now) - Decimal(before)) / Decimal(before) * 100))


def quick_insights(data=None):
    """Plain-language findings, worked out without any AI."""
    data = data or snapshot()
    cur = data["currency"]
    out = []
    w, b = data["last_7_days"], data["previous_7_days"]
    change = _pct_change(w["revenue"], b["revenue"])
    if change is not None:
        out.append(("up" if change >= 0 else "down",
                    f"Sales this week: {cur} {w['revenue']:,.2f} ({'+' if change >= 0 else ''}{change}% vs the week before, {w['orders']} orders)."))
    elif w["orders"]:
        out.append(("up", f"Sales this week: {cur} {w['revenue']:,.2f} from {w['orders']} orders."))
    if data["running_out"]:
        names = ", ".join(f"{r['name']} ({'sold out' if not r['stock'] else str(r['days_left']) + ' days left'})" for r in data["running_out"][:3])
        out.append(("warn", f"{len(data['running_out'])} product(s) sold out or running out within two weeks: {names}. See Restock forecast for how many to reorder."))
    if data["searches_with_no_results_30d"]:
        q = ", ".join(f"“{r['query']}”" for r in data["searches_with_no_results_30d"][:4])
        out.append(("idea", f"Shoppers searched for {q} and found nothing - products you could add or name better."))
    seg = {s["segment"]: s for s in data["customer_segments"]}
    if seg.get("At risk", {}).get("customers"):
        out.append(("warn", f"{seg['At risk']['customers']} good customer(s) haven't ordered for 90+ days. Send them a win-back email (Campaigns > audience 'At risk')."))
    if seg.get("VIP", {}).get("customers"):
        out.append(("idea", f"Your {seg['VIP']['customers']} VIP customer(s) bring {seg['VIP']['revenue_share_pct']}% of sales. Early access or a thank-you code keeps them."))
    if data["risky_open_orders"]:
        ids = ", ".join(f"#{r['order']}" for r in data["risky_open_orders"][:4])
        out.append(("warn", f"Check before shipping: order {ids} look risky."))
    if data["not_selling"]:
        out.append(("idea", f"{len(data['not_selling'])} product(s) in stock haven't sold in 30 days - try a deal, better photos or a lower price."))
    m = data["last_30_days"]
    if m["orders"] and m["repeat_rate"] < 20:
        out.append(("idea", f"Only {m['repeat_rate']}% of customers came back. Points, Subscribe & Save or a post-delivery coupon raise this."))
    if not out:
        out.append(("idea", "Not enough orders yet for insights. Share your store link and check back in a few days."))
    return out


def ask(question, data=None):
    """Answers a question about the store from the snapshot. Returns
    (answer, used_ai). Without an AI key, returns the quick insights."""
    from .multistore import store_settings
    data = data or snapshot()
    if not getattr(store_settings, "AI_API_KEY", ""):
        return "\n".join(f"• {text}" for _k, text in quick_insights(data)), False
    from .ai import _call_claude
    facts = json.dumps(data, default=str, ensure_ascii=False)[:12000]
    prompt = ("You are the analyst for an online store. Answer the owner's question in plain, friendly English, in at most "
              "8 short sentences or bullet points. Use only the numbers in the data below; if the data can't answer it, say "
              "what would be needed. End with one concrete action.\n\n"
              f"STORE DATA (JSON):\n{facts}\n\nQUESTION: {question.strip()[:500]}")
    try:
        answer = _call_claude(prompt, max_tokens=700)
    except Exception:
        answer = ""
    if not answer:
        return "\n".join(f"• {text}" for _k, text in quick_insights(data)), False
    return answer.strip(), True
