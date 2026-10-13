"""First-party, privacy-friendly store statistics.

Only anonymous daily counters are stored (how many visits, product views,
add-to-carts, checkout starts and searches) - no cookies of their own, no
IP addresses, nothing per person - so no consent banner is needed for them.
Bots and browser prefetches are ignored.
"""
import re
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

BOT = re.compile(r"bot|crawl|spider|slurp|preview|monitor|headless|python-requests|curl|wget|lighthouse", re.I)
KINDS = ("visit", "view", "cart", "checkout", "order", "search", "search_zero")


def is_human(request):
    ua = request.META.get("HTTP_USER_AGENT", "")
    if not ua or BOT.search(ua):
        return False
    if request.headers.get("Sec-Purpose", "").startswith("prefetch") or request.headers.get("Purpose") == "prefetch":
        return False
    return True


def bump(kind, key="", amount=1, day=None):
    from .models import DailyStat
    day = day or timezone.localdate()
    key = (key or "")[:150]
    updated = DailyStat.objects.filter(date=day, kind=kind, key=key).update(count=F("count") + amount)
    if not updated:
        try:
            with transaction.atomic():
                DailyStat.objects.create(date=day, kind=kind, key=key, count=amount)
        except IntegrityError:
            DailyStat.objects.filter(date=day, kind=kind, key=key).update(count=F("count") + amount)


def track(request, kind, key=""):
    """Counts an event after the response is sent. Safe to call anywhere."""
    if request is not None and not is_human(request):
        return
    if request is not None and getattr(request, "user", None) is not None and request.user.is_authenticated and request.user.is_staff:
        return  # staff browsing their own shop isn't traffic
    _buffer(kind, key)


def _buffer(kind, key=""):
    """Counts in the shared cache; flush() writes the totals to the database
    every few minutes, so busy pages never queue up database writes."""
    from django.core.cache import cache
    try:
        day = timezone.localdate().isoformat()
        ckey = f"st|{day}|{kind}|{(key or '')[:150]}"
        if cache.add(ckey, 1, 172800):
            index = cache.get("st|index") or []
            index.append(ckey)
            cache.set("st|index", index[-20000:], 172800)
        else:
            try:
                cache.incr(ckey)
            except ValueError:
                cache.set(ckey, 1, 172800)
    except Exception:  # statistics must never break a page
        pass


def flush():
    """Writes buffered counters to DailyStat. Returns how many events."""
    from datetime import date
    from django.core.cache import cache
    index = cache.get("st|index") or []
    if not index:
        return 0
    cache.delete("st|index")
    total = 0
    for ckey in index:
        n = cache.get(ckey)
        cache.delete(ckey)
        if not n:
            continue
        _p, day, kind, key = ckey.split("|", 3)
        try:
            bump(kind, key, amount=n, day=date.fromisoformat(day))
            total += n
        except Exception:
            pass
    return total


def _safe_bump(kind, key):
    try:
        bump(kind, key)
    except Exception:  # statistics must never break a page
        pass


def track_visit(request):
    """Once per browser session per day."""
    if request.method != "GET" or not hasattr(request, "session"):
        return
    today = timezone.localdate().isoformat()
    if request.session.get("_seen") == today:
        return
    if not is_human(request):
        return
    request.session["_seen"] = today
    track(request, "visit")


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _counts(kind, start, end):
    from django.db.models import Sum
    from .models import DailyStat
    return DailyStat.objects.filter(kind=kind, date__gte=start, date__lte=end).aggregate(n=Sum("count"))["n"] or 0


def _pct(part, whole):
    return round(part * 100 / whole, 1) if whole else 0


def report(start, end):
    """All numbers for the Reports page between two dates (inclusive)."""
    flush()
    from collections import defaultdict
    from django.db.models import Sum
    from .countries import COUNTRIES
    from .models import DailyStat, Order, OrderItem, Product, ReturnRequest
    names = dict(COUNTRIES)
    cats = dict(Product.CATEGORY_CHOICES)
    in_period = Order.objects.filter(created_at__date__gte=start, created_at__date__lte=end)
    counted = in_period.exclude(status="cancelled").exclude(payment_status__in=["pending", "failed", "refunded"])
    # Light rows instead of full objects: fast even with a year of orders.
    item_totals = dict(OrderItem.objects.filter(order__in=counted).values("order_id")
                       .annotate(v=Sum(F("price") * F("quantity"))).values_list("order_id", "v"))
    rows = list(counted.values_list("id", "country", "payment_method", "created_at", "discount_amount",
                                    "shipping_amount", "tax_amount", "tax_included", "email", "guest_email"))
    revenue = Decimal("0")
    by_country, by_method = defaultdict(lambda: [0, Decimal("0")]), defaultdict(lambda: [0, Decimal("0")])
    daily = defaultdict(lambda: [0, Decimal("0")])
    emails = []
    for oid, country, method, created, discount, shipping, tax, included, email, guest_email in rows:
        total = max((item_totals.get(oid) or Decimal("0")) - (discount or 0), Decimal("0")) + (shipping or 0) + (Decimal("0") if included else (tax or 0))
        revenue += total
        by_country[country][0] += 1
        by_country[country][1] += total
        by_method[method or "other"][0] += 1
        by_method[method or "other"][1] += total
        day = timezone.localtime(created).date()
        daily[day][0] += 1
        daily[day][1] += total
        if email or guest_email:
            emails.append((email or guest_email).lower())
    n_orders = len(rows)
    items = OrderItem.objects.filter(order__in=counted)
    by_cat = list(items.values("product__category").annotate(units=Sum("quantity"), revenue=Sum(F("price") * F("quantity"))).order_by("-revenue")[:12])
    for row in by_cat:
        row["label"] = cats.get(row["product__category"], row["product__category"] or "Removed products")
    by_seller = list(items.values("seller_account__business_name", "seller_account_id")
                     .annotate(units=Sum("quantity"), revenue=Sum(F("price") * F("quantity")), commission=Sum("commission_amount"))
                     .order_by("-revenue")[:12])
    top_products = list(items.values("product_id", "product_name").annotate(units=Sum("quantity"), revenue=Sum(F("price") * F("quantity")))
                        .order_by("-revenue")[:10])

    # customers
    buyers = set(emails)
    earlier = set()
    buyer_list = list(buyers)
    for k in range(0, len(buyer_list), 500):
        earlier.update(e.lower() for e in Order.objects.exclude(status="cancelled").filter(
            created_at__date__lt=start, email__in=buyer_list[k:k + 500]).values_list("email", flat=True).distinct())
    from collections import Counter
    seen = Counter(emails)
    returning = {e for e in buyers if e in earlier or seen[e] > 1}
    refunded = in_period.filter(payment_status="refunded").count()
    paid_total = in_period.exclude(payment_status__in=["pending", "failed"]).exclude(status="cancelled", payment_status="not_applicable").count()
    returns = ReturnRequest.objects.filter(created_at__date__gte=start, created_at__date__lte=end).count()
    items_sold = items.aggregate(n=Sum("quantity"))["n"] or 0
    cancelled = in_period.filter(status="cancelled").count()

    # funnel
    funnel_raw = [("visit", "Visits"), ("view", "Product views"), ("cart", "Added to cart"), ("checkout", "Started checkout"), ("order", "Placed an order")]
    funnel = []
    first = None
    prev = None
    for kind, label in funnel_raw:
        n = _counts(kind, start, end) if kind != "order" else in_period.count()
        first = first if first is not None else n
        funnel.append({"label": label, "n": n, "of_first": _pct(n, first), "of_prev": _pct(n, prev) if prev is not None else None})
        prev = n
    top_viewed = list(DailyStat.objects.filter(kind="view", date__gte=start, date__lte=end).values("key")
                      .annotate(n=Sum("count")).order_by("-n")[:10])
    titles = dict(Product.objects.filter(id__in=[int(r["key"]) for r in top_viewed if r["key"].isdigit()]).values_list("id", "name"))
    sold_units = {str(r["product_id"]): r["units"] for r in items.values("product_id").annotate(units=Sum("quantity"))}
    for r in top_viewed:
        r["name"] = titles.get(int(r["key"]) if r["key"].isdigit() else 0, f"#{r['key']}")
        r["sold"] = sold_units.get(r["key"], 0)
        r["rate"] = _pct(r["sold"], r["n"])
    searches = list(DailyStat.objects.filter(kind="search", date__gte=start, date__lte=end).values("key").annotate(n=Sum("count")).order_by("-n")[:15])
    zero = list(DailyStat.objects.filter(kind="search_zero", date__gte=start, date__lte=end).values("key").annotate(n=Sum("count")).order_by("-n")[:15])

    series = []
    d = start
    peak = max([v[1] for v in daily.values()] or [Decimal("1")]) or Decimal("1")
    while d <= end:
        n, amount = daily.get(d, (0, Decimal("0")))
        series.append({"date": d, "orders": n, "revenue": amount, "pct": float(amount / peak * 100) if peak else 0})
        d += timedelta(days=1)
    return {
        "start": start, "end": end,
        "revenue": revenue, "orders": n_orders, "aov": (revenue / n_orders).quantize(Decimal("0.01")) if n_orders else Decimal("0"),
        "items_sold": items_sold, "customers": len(buyers), "returning": len(returning), "repeat_rate": _pct(len(returning), len(buyers)),
        "refunded": refunded, "refund_rate": _pct(refunded, paid_total), "returns": returns, "return_rate": _pct(returns, items_sold),
        "cancelled": cancelled, "cancel_rate": _pct(cancelled, in_period.count()),
        "commission": items.aggregate(c=Sum("commission_amount"))["c"] or Decimal("0"),
        "by_country": sorted(({"code": c, "name": names.get(c, c or "—"), "orders": v[0], "revenue": v[1]} for c, v in by_country.items()),
                             key=lambda r: -r["revenue"])[:15],
        "by_method": sorted(({"method": m, "orders": v[0], "revenue": v[1]} for m, v in by_method.items()), key=lambda r: -r["revenue"]),
        "by_category": by_cat, "by_seller": by_seller, "top_products": top_products,
        "funnel": funnel, "top_viewed": top_viewed, "searches": searches, "zero_searches": zero, "series": series,
        "conversion": _pct(n_orders, funnel[0]["n"]),
    }


SKIP = ("/static/", "/media/", "/manage/", "/seller/", "/admin/", "/live/", "/api/", "/shipping/webhook", "/stripe/",
        "/paypal/", "/health/", "/sw.js", "/manifest", "/favicon", "/robots.txt", "/sitemap", "/app-icon", "/offline")


class VisitMiddleware:
    """Counts one visit per browser session per day (shop pages only)."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            if request.method == "GET" and not request.path.startswith(("/static/", "/media/")):
                from .promotions import tick_soon
                tick_soon()
            if (response.status_code == 200 and request.method == "GET" and not request.path.startswith(SKIP)
                    and "text/html" in response.get("Content-Type", "")):
                track_visit(request)
        except Exception:
            pass
        return response
