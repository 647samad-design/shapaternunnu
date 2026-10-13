"""Timed sales (Promotion) for sellers and the store.

``tick()`` starts promotions whose time has come and ends finished ones.
It runs with the daily tasks and, throttled to every few minutes, after
ordinary shop requests, so sales start and stop on time without a cron job.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone


def _cut(price, pct):
    return max((Decimal(price) * Decimal(100 - pct) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), Decimal("0.01"))


def products_for(promo):
    from .models import Product
    qs = Product.objects.filter(seller_account_id=promo.seller_account_id).exclude(approval_status="archived")
    if promo.category:
        qs = qs.filter(category=promo.category)
    if promo.product_ids:
        qs = qs.filter(id__in=promo.product_ids)
    return qs


def _busy_ids(exclude_pk):
    from .models import Promotion
    ids = set()
    for saved in Promotion.objects.filter(status="active").exclude(pk=exclude_pk).values_list("saved_prices", flat=True):
        ids.update(int(k) for k in saved)
    return ids


def start(promo):
    from .models import Product, ProductVariant, Promotion
    with transaction.atomic():
        promo = Promotion.objects.select_for_update().get(pk=promo.pk)
        if promo.status != "scheduled":
            return 0
        busy = _busy_ids(promo.pk)
        saved = {}
        for p in products_for(promo).select_for_update():
            if p.id in busy:
                continue  # already in another running sale
            new = _cut(p.price, promo.percent_off)
            entry = {"price": str(p.price), "old_price": str(p.old_price) if p.old_price is not None else None, "sale_price": str(new), "variants": {}}
            for v in ProductVariant.objects.filter(product=p).exclude(price=None):
                v_new = _cut(v.price, promo.percent_off)
                entry["variants"][str(v.id)] = [str(v.price), str(v_new)]
                ProductVariant.objects.filter(pk=v.pk).update(price=v_new)
            p.old_price = p.price if p.old_price is None or p.old_price <= p.price else p.old_price
            p.price = new
            p.save(update_fields=["price", "old_price", "updated_at"])  # price-drop alerts go out from signals
            saved[str(p.id)] = entry
        promo.saved_prices = saved
        promo.status = "active"
        promo.save(update_fields=["saved_prices", "status"])
    return len(saved)


def stop(promo, status="ended"):
    """Puts prices back (unless the seller changed them during the sale)."""
    from .models import Product, ProductVariant, Promotion
    with transaction.atomic():
        promo = Promotion.objects.select_for_update().get(pk=promo.pk)
        if promo.status == "scheduled":
            promo.status = status
            promo.save(update_fields=["status"])
            return 0
        if promo.status != "active":
            return 0
        restored = 0
        for pid, entry in promo.saved_prices.items():
            p = Product.objects.select_for_update().filter(pk=int(pid)).first()
            if p is None:
                continue
            if p.price == Decimal(entry["sale_price"]):
                p.price = Decimal(entry["price"])
                p.old_price = Decimal(entry["old_price"]) if entry["old_price"] else None
                p.save(update_fields=["price", "old_price", "updated_at"])
                restored += 1
            for vid, (orig, sale) in entry.get("variants", {}).items():
                ProductVariant.objects.filter(pk=int(vid), price=Decimal(sale)).update(price=Decimal(orig))
        promo.status = status
        promo.save(update_fields=["status"])
    return restored


def tick():
    from .ads import flush_impressions
    from .analytics import flush
    flush_impressions()
    flush()
    from .models import Promotion
    now = timezone.now()
    for promo in Promotion.objects.filter(status="active", ends_at__lte=now):
        stop(promo)
    for promo in Promotion.objects.filter(status="scheduled", starts_at__lte=now):
        if promo.ends_at <= now:
            Promotion.objects.filter(pk=promo.pk).update(status="ended")
        else:
            start(promo)


def tick_soon():
    """Called after shop requests; does the work at most every 5 minutes."""
    if cache.add("promotions:tick", 1, 300):
        from .background import after_response
        from .campaigns import continue_sending
        after_response(tick)
        after_response(continue_sending)
    if cache.add("unpaid:release", 1, 900):
        # Give back stock held by card/PayPal orders that were never paid,
        # every 15 minutes, instead of waiting for the daily task.
        from .background import after_response
        after_response(_release_unpaid)


def _release_unpaid():
    import io
    from django.core.management import call_command
    call_command("release_unpaid_orders", stdout=io.StringIO())
