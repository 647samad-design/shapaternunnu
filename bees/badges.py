"""Seller badges.

* Verified seller - given by store staff after checking the seller's ID /
  business documents (Admin > Sellers > seller > Verify).
* Top seller - earned automatically: enough orders in the last 90 days
  and a high product rating. Recalculated every day by daily_tasks.
"""
from datetime import timedelta

from django.conf import settings
from django.db.models import Avg, Count
from django.utils import timezone

WINDOW_DAYS = 90


def rules():
    return {
        "orders": getattr(settings, "TOP_SELLER_MIN_ORDERS", 10),
        "rating": getattr(settings, "TOP_SELLER_MIN_RATING", 4.5),
        "reviews": getattr(settings, "TOP_SELLER_MIN_REVIEWS", 3),
    }


def progress(seller):
    """Where a seller stands against the Top seller rules."""
    from .models import OrderItem, Review
    since = timezone.now() - timedelta(days=WINDOW_DAYS)
    orders = (OrderItem.objects.filter(seller_account=seller, order__created_at__gte=since).counted()
              .values("order_id").distinct().count())
    agg = Review.objects.filter(product__seller_account=seller).aggregate(avg=Avg("rating"), n=Count("id"))
    r = rules()
    avg = round(agg["avg"] or 0, 2)
    return {
        "orders": orders, "orders_needed": r["orders"],
        "rating": avg, "rating_needed": r["rating"],
        "reviews": agg["n"], "reviews_needed": r["reviews"],
        "orders_ok": orders >= r["orders"],
        "rating_ok": agg["n"] >= r["reviews"] and avg >= r["rating"],
        "orders_pct": min(round(orders / r["orders"] * 100), 100) if r["orders"] else 100,
    }


def qualifies(seller):
    if seller.status != "approved":
        return False
    p = progress(seller)
    good_service = seller.performance_level in ("", "new", "excellent", "good")
    return p["orders_ok"] and p["rating_ok"] and good_service


def update_all():
    """Turns the Top seller badge on/off for every seller. Returns (gained, lost)."""
    from .models import Notification, SellerAccount
    gained = lost = 0
    for seller in SellerAccount.objects.select_related("user"):
        now_top = qualifies(seller)
        if now_top == seller.top_seller:
            continue
        SellerAccount.objects.filter(pk=seller.pk).update(top_seller=now_top)
        if now_top:
            gained += 1
            Notification.objects.create(user=seller.user, link="/seller/dashboard/",
                                        message="Congratulations! Your store earned the Top seller badge.")
        else:
            lost += 1
    return gained, lost
