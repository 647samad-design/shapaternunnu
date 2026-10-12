"""Seller performance score (0-100), like the big marketplaces use.

Measured over the last 90 days:
  * late shipping   - shipped more than 2 days after the order
  * cancellations   - orders cancelled because the seller never shipped
  * problems        - items refunded after a customer's problem report
  * reply time      - typical time to answer a customer message
  * rating          - product reviews
Levels: Excellent (90+), Good (75+), Fair (60+), At risk (<60). Sellers
with fewer than 3 orders are "New" and aren't judged yet. Refreshed daily;
At-risk sellers and the store team are warned (at most once a month).
"""
from datetime import timedelta

from django.db.models import Avg, Count
from django.utils import timezone

WINDOW_DAYS = 90
SHIP_WITHIN = timedelta(days=2)
MIN_ORDERS = 3
LEVELS = [(90, "excellent", "Excellent"), (75, "good", "Good"), (60, "fair", "Fair"), (0, "at_risk", "At risk")]
LABELS = {key: label for _, key, label in LEVELS} | {"new": "New seller"}


def reply_hours(seller, since):
    from .models import Conversation
    gaps = []
    for conv in Conversation.objects.filter(seller=seller, last_message_at__gte=since)[:60]:
        waiting = None
        for m in conv.messages.filter(created_at__gte=since).only("from_seller", "created_at"):
            if not m.from_seller and waiting is None:
                waiting = m.created_at
            elif m.from_seller and waiting is not None:
                gaps.append((m.created_at - waiting).total_seconds() / 3600)
                waiting = None
    if len(gaps) < 2:
        return None
    gaps.sort()
    return round(gaps[len(gaps) // 2], 1)


def measure(seller, now=None):
    from .models import Claim, OrderEvent, OrderItem, Review
    now = now or timezone.now()
    since = now - timedelta(days=WINDOW_DAYS)
    items = (OrderItem.objects.filter(seller_account=seller, order__created_at__gte=since)
             .exclude(order__payment_status__in=["pending", "failed"]).select_related("order"))
    items = list(items)
    order_ids = {i.order_id for i in items}
    # Items shipped before ship dates were recorded count as on time.
    shipped = [i for i in items if i.shipped_at or i.fulfillment_status in ("handed_to_courier", "delivered")]
    late = [i for i in shipped if i.shipped_at and i.shipped_at - i.order.created_at > SHIP_WITHIN]
    # still unshipped and already late counts too
    overdue = [i for i in items if not i.shipped_at and i.order.status in ("pending", "confirmed")
               and i.fulfillment_status in ("pending", "packed") and now - i.order.created_at > SHIP_WITHIN]
    ship_base = len(shipped) + len(overdue)
    late_rate = (len(late) + len(overdue)) / ship_base if ship_base else 0
    cancelled = OrderEvent.objects.filter(order_id__in=order_ids, text__startswith="Cancelled automatically").values("order_id").distinct().count()
    cancel_rate = cancelled / len(order_ids) if order_ids else 0
    problems = Claim.objects.filter(order_item__in=[i.pk for i in items], status="refunded").count()
    problem_rate = problems / len(items) if items else 0
    hours = reply_hours(seller, since)
    rating = Review.objects.filter(product__seller_account=seller, created_at__gte=since).aggregate(avg=Avg("rating"), n=Count("id"))

    score = 100.0
    score -= min(40, late_rate * 80)
    score -= min(30, cancel_rate * 150)
    score -= min(30, problem_rate * 200)
    if hours is not None:
        score -= 15 if hours > 48 else 10 if hours > 24 else 0
    if (rating["n"] or 0) >= 3:
        score -= 20 if rating["avg"] < 3 else 10 if rating["avg"] < 3.5 else 0
    score = max(0, int(round(score)))
    if len(order_ids) < MIN_ORDERS:
        level = "new"
    else:
        level = next(key for floor, key, _ in LEVELS if score >= floor)

    tips = []
    if late_rate > 0.1:
        tips.append("Ship within 2 days of the order - mark items 'Handed to courier' as soon as they leave.")
    if cancel_rate > 0:
        tips.append("Orders were cancelled because nobody shipped them. Turn on holiday mode when you can't ship.")
    if problem_rate > 0.03:
        tips.append("Customers reported problems. Check photos and descriptions match the product, and pack carefully.")
    if hours is not None and hours > 24:
        tips.append("Answer customer messages within a day - saved replies in Messages make it quick.")
    if (rating["n"] or 0) >= 3 and rating["avg"] < 3.5:
        tips.append("Your rating is low. Reply to reviews and fix what customers mention.")
    return {
        "score": score, "level": level, "label": LABELS[level],
        "orders": len(order_ids), "items": len(items),
        "late_pct": round(late_rate * 100), "late": len(late) + len(overdue),
        "cancel_pct": round(cancel_rate * 100), "cancelled": cancelled,
        "problem_pct": round(problem_rate * 100, 1), "problems": problems,
        "reply_hours": hours,
        "rating": round(rating["avg"], 1) if rating["avg"] else None, "reviews": rating["n"] or 0,
        "tips": tips, "updated": now.isoformat(),
    }


def update_all():
    """Refreshes every approved seller. Returns how many are at risk."""
    from .alerts import notify_staff
    from .models import Notification, SellerAccount
    from .seller_center import _seller_users
    now = timezone.now()
    at_risk = 0
    for seller in SellerAccount.objects.filter(status="approved").select_related("user"):
        data = measure(seller, now)
        fields = {"performance_score": data["score"], "performance_level": data["level"], "performance_data": data}
        if data["level"] == "at_risk":
            at_risk += 1
            if not seller.performance_warned_at or now - seller.performance_warned_at > timedelta(days=30):
                fields["performance_warned_at"] = now
                Notification.objects.bulk_create([
                    Notification(user=u, link="/seller/dashboard/#performance",
                                 message=f"Your seller score dropped to {data['score']}/100 (At risk). See how to improve it on your dashboard.")
                    for u in _seller_users(seller)])
                notify_staff(f"{seller.display_name} is at risk: seller score {data['score']}/100.", link=f"/manage/sellers/{seller.id}/")
        SellerAccount.objects.filter(pk=seller.pk).update(**fields)
    return at_risk
