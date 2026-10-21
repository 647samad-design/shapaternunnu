"""Shopper-facing signals the big stores show: "50+ bought in the past
month", "#1 Best Seller in Hoodies", delivery dates and personal picks."""
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Sum
from django.utils import timezone

WINDOW = timedelta(days=30)


def _sales_30d():
    """{product_id: units} for the last 30 days, cached for an hour."""
    data = cache.get("insights:sales30")
    if data is None:
        from .models import OrderItem
        since = timezone.now() - WINDOW
        rows = (OrderItem.objects.counted().filter(order__created_at__gte=since, product__isnull=False)
                .values("product_id").annotate(n=Sum("quantity")))
        data = {r["product_id"]: r["n"] for r in rows}
        cache.set("insights:sales30", data, 3600)
    return data


def bought_label(product_id):
    n = _sales_30d().get(product_id, 0)
    for floor, label in ((1000, "1K+"), (500, "500+"), (100, "100+"), (50, "50+"), (20, "20+"), (10, "10+"), (5, "5+")):
        if n >= floor:
            return f"{label} bought in the past month"
    return ""


def best_sellers():
    """{product_id: (rank, category slug)} - the top 3 sellers of each
    category over the last 30 days (at least 3 sold). Cached for an hour."""
    data = cache.get("insights:best")
    if data is None:
        from .models import Product
        sales = _sales_30d()
        ids = [pid for pid, n in sales.items() if n >= 3]
        by_cat = {}
        for pid, cat in Product.objects.listed().filter(pk__in=ids).values_list("id", "category"):
            by_cat.setdefault(cat, []).append(pid)
        data = {}
        for cat, pids in by_cat.items():
            for rank, pid in enumerate(sorted(pids, key=lambda p: -sales[p])[:3], 1):
                data[pid] = (rank, cat)
        cache.set("insights:best", data, 3600)
    return data


def best_seller_badge(product):
    hit = best_sellers().get(product.pk)
    if not hit:
        return None
    from .categories import label
    return {"rank": hit[0], "category": hit[1], "label": label(hit[1])}


def delivery_window(product, country=""):
    """(earliest, latest) dates for the product page, or None for downloads."""
    if product.is_digital:
        return None
    from .models import SiteSettings
    days = SiteSettings.load().delivery_days or 5
    start = timezone.localdate()
    if product.preorder_ships_on and product.preorder_ships_on > start:
        start = product.preorder_ships_on  # pre-order: counts from the ship date
    def add_working(d, n):
        while n > 0:
            d += timedelta(days=1)
            if d.weekday() < 5:
                n -= 1
        return d
    return add_working(start, max(1, days - 2)), add_working(start, days)


def recommended_for(request, limit=10):
    """Products from the categories the shopper viewed or bought, that they
    haven't seen yet. Empty for new visitors."""
    from .models import OrderItem, Product
    seen = [int(i) for i in request.session.get("recently_viewed", []) if str(i).isdigit()][:20]
    cats = set(Product.objects.filter(pk__in=seen).values_list("category", flat=True))
    bought = set()
    if request.user.is_authenticated:
        recent = OrderItem.objects.filter(order__user=request.user, product__isnull=False).order_by("-id")[:30]
        bought = set(recent.values_list("product_id", flat=True))
        cats |= set(Product.objects.filter(pk__in=bought).values_list("category", flat=True))
    if not cats:
        return []
    from .categories import with_children
    all_cats = set()
    for c in cats:
        all_cats |= set(with_children(c))
    sales = _sales_30d()
    qs = (Product.objects.listed().filter(category__in=all_cats, stock__gt=0).exclude(pk__in=set(seen) | bought)
          .select_related("seller_account")[:200])
    picks = sorted(qs, key=lambda p: (-sales.get(p.pk, 0), -p.pk))
    return picks[:limit]
