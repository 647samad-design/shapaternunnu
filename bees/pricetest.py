"""Price tests: which of two prices earns more?

The product alternates between price A and price B day by day
("switchback" testing). On any given day every shopper sees the same
price - nobody is charged more than the person next to them - and the
results are compared: visitors, conversion, revenue and (with a unit
cost) profit per visitor. At the end the better price is kept
automatically. Confidence says how likely the difference is real rather
than luck (two-proportion z-test on conversion).
"""
import math
from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import F, Sum
from django.utils import timezone


def running_for(product):
    return product.price_tests.filter(status="running").first()


def start(product, price_b, days=14, unit_cost=None, user=None):
    from .models import PriceTest, PriceTestBlock
    if running_for(product):
        raise ValueError("This product already has a price test running.")
    price_b = Decimal(price_b).quantize(Decimal("0.01"))
    if price_b <= 0 or price_b == product.price:
        raise ValueError("Price B must be above 0 and different from the current price.")
    if product.is_auction or product.quote_only:
        raise ValueError("Auctions and quote-only products can't be price-tested.")
    days = max(4, min(int(days or 14), 60))
    test = PriceTest.objects.create(product=product, price_a=product.price, price_b=price_b, days=days,
                                    unit_cost=unit_cost, created_by=user, current_arm="a")
    PriceTestBlock.objects.create(test=test, arm="a", start=timezone.now())
    return test


def _set_price(product, price):
    from .models import PriceHistory, Product
    Product.objects.filter(pk=product.pk).update(price=price)
    PriceHistory.objects.create(product=product, price=price)


def switch(test, now=None):
    """Ends today's block and starts the other price."""
    from .models import PriceTestBlock
    now = now or timezone.now()
    test.blocks.filter(end__isnull=True).update(end=now)
    arm = "b" if test.current_arm == "a" else "a"
    PriceTestBlock.objects.create(test=test, arm=arm, start=now)
    test.current_arm = arm
    test.save(update_fields=["current_arm"])
    _set_price(test.product, test.price_a if arm == "a" else test.price_b)


def finish(test, keep=None, stopped=False, now=None):
    now = now or timezone.now()
    test.blocks.filter(end__isnull=True).update(end=now)
    res = results(test)
    keep = keep or (res["winner"] if not stopped and res["winner"] else "a")
    _set_price(test.product, test.price_a if keep == "a" else test.price_b)
    test.status = "stopped" if stopped else "finished"
    test.applied = keep
    test.ended_at = now
    test.save(update_fields=["status", "applied", "ended_at"])
    return res


def tick(now=None):
    """Daily: switch running tests to the other price, finish due ones."""
    from .models import Notification, PriceTest
    from .seller_center import _seller_users
    now = now or timezone.now()
    done = 0
    for test in PriceTest.objects.filter(status="running").select_related("product__seller_account"):
        if now - test.started_at >= timedelta(days=test.days):
            res = finish(test, now=now)
            kept = test.price_a if test.applied == "a" else test.price_b
            msg = (f"Price test on '{test.product.name}' finished: kept {kept} "
                   f"({res['summary']})")[:255]
            users = _seller_users(test.product.seller_account) if test.product.seller_account_id else []
            Notification.objects.bulk_create([Notification(user=u, link="/seller/price-tests/", message=msg) for u in users])
            if test.created_by_id and test.created_by not in users:
                Notification.objects.create(user_id=test.created_by_id, link="/manage/price-tests/", message=msg)
        else:
            switch(test, now=now)
        done += 1
    return done


def _arm_days(test):
    """{date: arm} - each calendar day belongs to the price that was shown
    for most of that day (days are counted in the store's time zone)."""
    share = {}
    now = timezone.now()
    for b in test.blocks.all():
        start, end = timezone.localtime(b.start), timezone.localtime(b.end or now)
        d = start.date()
        while d <= end.date():
            day_start = timezone.make_aware(datetime(d.year, d.month, d.day))
            day_end = day_start + timedelta(days=1)
            seconds = (min(end, day_end) - max(start, day_start)).total_seconds()
            if seconds > 0:
                share.setdefault(d, {}).setdefault(b.arm, 0)
                share[d][b.arm] += seconds
            d += timedelta(days=1)
    return {d: max(arms, key=arms.get) for d, arms in share.items()}


def _phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def results(test):
    from .models import DailyStat, OrderItem
    arms = {"a": {"price": test.price_a, "views": 0, "orders": 0, "units": 0, "revenue": Decimal("0"), "days": 0},
            "b": {"price": test.price_b, "views": 0, "orders": 0, "units": 0, "revenue": Decimal("0"), "days": 0}}
    days = _arm_days(test)
    for d, arm in days.items():
        arms[arm]["days"] += 1
    for row in DailyStat.objects.filter(kind="view", key=str(test.product_id), date__in=list(days)).values("date", "count"):
        arms[days[row["date"]]]["views"] += row["count"]
    for b in test.blocks.all():
        items = OrderItem.objects.counted().filter(product_id=test.product_id, order__created_at__gte=b.start,
                                                   order__created_at__lt=b.end or timezone.now())
        agg = items.aggregate(units=Sum("quantity"), revenue=Sum(F("price") * F("quantity")))
        arms[b.arm]["units"] += agg["units"] or 0
        arms[b.arm]["revenue"] += agg["revenue"] or Decimal("0")
        arms[b.arm]["orders"] += items.values("order_id").distinct().count()
    for a in arms.values():
        v = a["views"]
        a["conversion"] = round(a["orders"] * 100 / v, 2) if v else 0
        a["per_view"] = (a["revenue"] / v).quantize(Decimal("0.01")) if v else Decimal("0")
        a["profit"] = (a["revenue"] - a["units"] * test.unit_cost) if test.unit_cost is not None else None
        a["profit_per_view"] = (a["profit"] / v).quantize(Decimal("0.01")) if v and a["profit"] is not None else None
    A, B = arms["a"], arms["b"]
    confidence = 0
    if A["views"] and B["views"]:
        p1, p2 = A["orders"] / A["views"], B["orders"] / B["views"]
        pooled = (A["orders"] + B["orders"]) / (A["views"] + B["views"])
        se = math.sqrt(pooled * (1 - pooled) * (1 / A["views"] + 1 / B["views"])) if 0 < pooled < 1 else 0
        if se:
            confidence = round((2 * _phi(abs(p1 - p2) / se) - 1) * 100)
    metric = "profit_per_view" if test.unit_cost is not None else "per_view"
    winner = ""
    if A["views"] >= 30 and B["views"] >= 30 and (A["orders"] + B["orders"]) >= 3:
        if (B[metric] or 0) > (A[metric] or 0):
            winner = "b"
        elif (A[metric] or 0) > (B[metric] or 0):
            winner = "a"
    label = "profit" if metric == "profit_per_view" else "revenue"
    if winner:
        w, l = (B, A) if winner == "b" else (A, B)
        lift = round(((w[metric] - l[metric]) / l[metric] * 100)) if l[metric] else None
        summary = (f"{w['price']} earned {'more' if lift is None else f'{lift}% more'} {label} per visitor"
                   + (f", {confidence}% confidence" if confidence else ""))
    else:
        summary = "not enough visitors or orders yet to pick a winner"
    return {"a": A, "b": B, "confidence": confidence, "winner": winner, "summary": summary, "metric": label}
