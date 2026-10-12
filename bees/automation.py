"""Daily order automation (run by ``manage.py daily_tasks``):

1. Late shipping - paid orders nobody has shipped:
   day 2: remind the seller(s) · day 4: alert the store team ·
   ``auto_cancel_days``: cancel + refund if nothing in it has shipped.
2. Arrival - items "on the way": remind the customer 2 days before
   ``auto_deliver_days``, then mark them delivered automatically (unless the
   customer reported a problem). Earnings, reviews and returns then follow
   the normal path.
3. Problem reports the seller hasn't answered within ``claim_reply_days``
   go to the store team.
"""
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

REMIND_SELLER_AFTER = timedelta(days=2)
ESCALATE_AFTER = timedelta(days=4)


def _notify(users, message, link):
    from .models import Notification
    Notification.objects.bulk_create([Notification(user=u, message=message[:255], link=link) for u in users])


def _sellers_of(items):
    from .seller_center import _seller_users
    users = []
    for seller in {i.seller_account for i in items if i.seller_account_id}:
        users += _seller_users(seller)
    return users


def late_shipping(now, site, summary):
    from .admin import cancel_order_with_side_effects
    from .alerts import notify_staff
    from .fulfilment import blocked_reason
    from .models import Order, OrderEvent
    orders = (Order.objects.filter(status__in=["pending", "confirmed"], items__fulfillment_status__in=["pending", "packed"])
              .exclude(payment_status__in=["pending", "failed", "refunded"]).distinct().prefetch_related("items__seller_account"))
    for order in orders:
        if blocked_reason(order):
            continue
        age = now - order.created_at
        items = list(order.items.all())
        waiting = [i for i in items if i.fulfillment_status in ("pending", "packed")]
        if not waiting:
            continue
        names = ", ".join(i.product_name for i in waiting[:3])
        if site.auto_cancel_days and age >= timedelta(days=site.auto_cancel_days) and len(waiting) == len(items):
            error = cancel_order_with_side_effects(order)
            if error:
                notify_staff(f"Couldn't auto-cancel late order #{order.id}: {error}", link=f"/manage/orders/{order.id}/")
                continue
            OrderEvent.objects.create(order=order, kind="note", text="Cancelled automatically: not shipped in time")
            _notify(_sellers_of(waiting), f"Order #{order.id} was cancelled because it wasn't shipped within {site.auto_cancel_days} days.",
                    f"/seller/orders/{order.id}/")
            notify_staff(f"Order #{order.id} was cancelled automatically (not shipped in {site.auto_cancel_days} days).", link=f"/manage/orders/{order.id}/")
            summary["auto_cancelled"] += 1
            continue
        if age >= ESCALATE_AFTER and not order.late_escalated_at:
            notify_staff(f"Order #{order.id} is late: {names} still not shipped after {age.days} days.", link=f"/manage/orders/{order.id}/")
            Order.objects.filter(pk=order.pk).update(late_escalated_at=now)
            summary["escalated"] += 1
        if age >= REMIND_SELLER_AFTER and not order.late_reminded_at:
            left = f" It will be cancelled automatically after {site.auto_cancel_days} days." if site.auto_cancel_days else ""
            _notify(_sellers_of(waiting), f"Order #{order.id} is waiting to ship ({names}). Please send it today.{left}",
                    f"/seller/orders/{order.id}/")
            Order.objects.filter(pk=order.pk).update(late_reminded_at=now)
            summary["seller_reminders"] += 1


def arrivals(now, site, summary):
    if not site.auto_deliver_days:
        return
    from . import fulfilment
    from .models import Claim, Order, OrderItem
    remind_at = timedelta(days=max(site.auto_deliver_days - 2, 1))
    deliver_at = timedelta(days=site.auto_deliver_days)
    items = (OrderItem.objects.filter(fulfillment_status="handed_to_courier")
             .exclude(order__status="cancelled").select_related("order"))
    by_order = {}
    for item in items:
        by_order.setdefault(item.order_id, []).append(item)
    claimed = set(Claim.objects.filter(order_item__in=items, status__in=Claim.OPEN_STATUSES).values_list("order_item_id", flat=True))
    for order_id, order_items in by_order.items():
        order = order_items[0].order
        ages = {i.pk: now - (i.shipped_at or order.created_at) for i in order_items}
        due = [i for i in order_items if ages[i.pk] >= deliver_at and i.pk not in claimed]
        if due:
            try:
                fulfilment.set_items_status(due, "delivered", by_staff=True)
            except fulfilment.FulfilmentError:
                continue
            summary["auto_delivered"] += len(due)
            continue
        if order.user_id and not order.arrival_reminded_at and max(ages.values()) >= remind_at:
            _notify([order.user], f"Has order #{order.id} arrived? Tap 'I've received it' - or report a problem. "
                                  f"We'll mark it delivered in 2 days otherwise.", reverse("my_orders") + f"#order-{order.id}")
            Order.objects.filter(pk=order.pk).update(arrival_reminded_at=now)
            summary["arrival_reminders"] += 1


def unanswered_claims(now, site, summary):
    from .alerts import notify_staff
    from .models import Claim
    late = Claim.objects.filter(status="open", seller_replied_at=None,
                                created_at__lte=now - timedelta(days=site.claim_reply_days)).select_related("order_item", "user")
    for claim in late:
        claim.status = "escalated"
        claim.save(update_fields=["status", "updated_at"])
        notify_staff(f"Seller didn't answer problem #{claim.id} ('{claim.order_item.product_name}') in time - please decide.",
                     link=reverse("manage_claim", args=[claim.id]))
        _notify([claim.user], f"The seller didn't answer in time, so our team will now decide your report about '{claim.order_item.product_name}'.",
                reverse("claim_detail", args=[claim.id]))
        summary["claims_escalated"] += 1


def run(now=None):
    from .models import SiteSettings
    now = now or timezone.now()
    site = SiteSettings.load()
    summary = {k: 0 for k in ("seller_reminders", "escalated", "auto_cancelled", "arrival_reminders", "auto_delivered", "claims_escalated")}
    late_shipping(now, site, summary)
    arrivals(now, site, summary)
    unanswered_claims(now, site, summary)
    try:
        from .couriers import refresh_active
        summary["tracking_checked"] = refresh_active()
    except Exception:  # courier problems never stop the other jobs
        summary["tracking_checked"] = 0
    from .manage_views import _refresh_attention
    _refresh_attention()
    return summary
