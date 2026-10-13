"""Product recommendations from real orders ("customers also bought").

Counts which products were bought in the same orders (cancelled and unpaid
orders don't count) and tops up with best sellers from the same category
when there isn't enough history yet. Results are cached for an hour.
"""
from collections import Counter

from django.core.cache import cache
from django.db.models import Count, Sum

TTL = 3600


def _counted_items():
    from .models import OrderItem
    return OrderItem.objects.exclude(order__status="cancelled").exclude(order__payment_status__in=["pending", "failed", "refunded"])


def also_bought_ids(product_ids, limit=8):
    product_ids = [int(p) for p in product_ids]
    key = "recs:" + ",".join(str(p) for p in sorted(product_ids)) + f":{limit}"
    ids = cache.get(key)
    if ids is not None:
        return ids
    items = _counted_items()
    order_ids = list(items.filter(product_id__in=product_ids).values_list("order_id", flat=True).distinct()[:2000])
    counts = Counter()
    if order_ids:
        for row in items.filter(order_id__in=order_ids).exclude(product_id__in=product_ids).exclude(product_id=None) \
                .values("product_id").annotate(n=Count("order_id", distinct=True)):
            counts[row["product_id"]] = row["n"]
    ids = [pid for pid, _n in counts.most_common(limit * 2)]
    if len(ids) < limit:
        from .models import Product
        cats = list(Product.objects.filter(id__in=product_ids).values_list("category", flat=True))
        best = items.filter(product__category__in=cats).exclude(product_id__in=product_ids + ids).exclude(product_id=None) \
            .values("product_id").annotate(n=Sum("quantity")).order_by("-n")[:limit * 2]
        ids += [r["product_id"] for r in best]
    cache.set(key, ids, TTL)
    return ids


def also_bought(product_ids, limit=6):
    """Live, in-stock products (with ratings) in recommendation order."""
    from .models import Product
    from .views import with_ratings
    ids = also_bought_ids(product_ids, limit * 2)
    if not ids:
        return []
    found = {p.id: p for p in with_ratings(Product.objects.live().filter(id__in=ids, stock__gt=0))}
    return [found[i] for i in ids if i in found][:limit]
