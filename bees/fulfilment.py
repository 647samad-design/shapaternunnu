"""One place for moving order items through packing, shipping and delivery.

Used by Seller Center (one item, a whole order, bulk actions) and by the
store admin, so the same rules apply everywhere:

* nothing ships while an order is cancelled, unpaid, refunded or is a cash
  order the customer hasn't confirmed yet;
* a seller can't undo "delivered" (only the store team can);
* the customer hears about every package that ships or arrives - a
  notification and an email - and can review an item as soon as it's
  delivered, even if other sellers' items in the same order are still on
  their way;
* the store team is told when a seller marks something delivered;
* every change lands on the order timeline.
"""
from collections import OrderedDict

from django.db import transaction
from django.utils import timezone

STEPS = ("pending", "packed", "handed_to_courier", "delivered")


class FulfilmentError(ValueError):
    pass


def blocked_reason(order):
    """Why items in this order can't be packed/shipped yet ("" if they can)."""
    if order.status == "cancelled":
        return f"Order #{order.id} was cancelled - don't ship it."
    if order.payment_status in ("pending", "failed"):
        return f"Order #{order.id} hasn't been paid yet - wait before shipping."
    if order.payment_status == "refunded":
        return f"Order #{order.id} was refunded - don't ship it."
    if order.payment_method == "cod" and not order.cod_confirmed:
        return f"Order #{order.id} is a cash order the customer hasn't confirmed yet - wait for the confirmation."
    return ""


def _who(item):
    return item.seller_account.display_name if item.seller_account_id else ""


def set_items_status(items, new_status, user=None, by_staff=False, forward_only=False, by_customer=False, delivery_code=None):
    """Moves ``items`` to ``new_status``. Returns (changed_items, moved_orders)
    where moved_orders is a list of (order_id, new order status label).
    Raises FulfilmentError when nothing could change and there's a reason
    worth telling the person."""
    from .models import OrderItem
    if new_status not in STEPS:
        raise FulfilmentError("Unknown status.")
    rank = {s: i for i, s in enumerate(STEPS)}
    changed, reasons = [], []
    code_ok = {}  # order id -> (ok, message), checked once per order
    now = timezone.now()
    with transaction.atomic():
        for item in items:
            item = OrderItem.objects.select_for_update(of=("self",)).select_related("order", "seller_account").get(pk=item.pk)
            reason = blocked_reason(item.order) or (
                f"The customer cancelled '{item.product_name}' - don't ship it." if item.cancelled_at else "")
            if reason:
                reasons.append(reason)
                continue
            current = item.fulfillment_status
            if current == new_status:
                continue
            if current == "delivered" and not by_staff:
                reasons.append(f"'{item.product_name}' is already delivered. Contact the store team if that was a mistake.")
                continue
            if forward_only and rank[new_status] < rank[current]:
                continue
            if new_status == "delivered" and not by_staff and not by_customer:
                from . import delivery_code as dc
                if dc.required(item.order):
                    if item.order_id not in code_ok:
                        code_ok[item.order_id] = dc.check(item.order, delivery_code)
                    ok, why = code_ok[item.order_id]
                    if not ok:
                        reasons.append(why)
                        continue
            item.fulfillment_status = new_status
            item.delivered_at = now if new_status == "delivered" else None
            if new_status in ("handed_to_courier", "delivered"):
                item.shipped_at = item.shipped_at or now
            else:
                item.shipped_at = None
            item.save(update_fields=["fulfillment_status", "delivered_at", "shipped_at"])
            changed.append(item)
    if not changed and reasons:
        raise FulfilmentError(reasons[0])
    moved = []
    by_order = OrderedDict()
    for item in changed:
        by_order.setdefault(item.order_id, []).append(item)
    for order_id, order_items in by_order.items():
        result = _after_change(order_items, new_status, user, by_staff, by_customer)
        if result:
            moved.append((order_id, result))
    return changed, moved


def _after_change(items, new_status, user, by_staff, by_customer=False):
    from .models import Notification, Order, OrderEvent
    from .views import sync_order_status_from_items
    order = Order.objects.get(pk=items[0].order_id)
    names = ", ".join(i.product_name for i in items[:3]) + (f" +{len(items) - 3} more" if len(items) > 3 else "")
    seller = _who(items[0])
    by = " (confirmed by the customer)" if by_customer else f" ({seller})" if seller else ""
    kind = {"packed": "packed", "handed_to_courier": "shipped", "delivered": "delivered"}.get(new_status, "note")
    text = {
        "packed": f"Packed: {names}{by}",
        "handed_to_courier": f"On its way: {names}{by}",
        "delivered": f"Delivered: {names}{by}",
        "pending": f"Back to pending: {names}{by}",
    }[new_status]
    OrderEvent.objects.create(order=order, kind=kind, text=text[:200])

    result = sync_order_status_from_items(order)  # whole order moved: Order.save() tells the customer
    if not result and not by_customer and new_status in ("handed_to_courier", "delivered"):
        # Only part of the order moved (other sellers' items are still on
        # their way): tell the customer about this package.
        if order.user_id:
            msg = (f"{names} from order #{order.id} has been delivered. Tell others what you think - write a review!"
                   if new_status == "delivered" else f"{names} from order #{order.id} is on its way.")
            Notification.objects.create(user_id=order.user_id, message=msg[:255], link="/my-orders/")
        from . import order_emails
        order_emails.send_later(order, "item_delivered" if new_status == "delivered" else "item_shipped", [i.pk for i in items])
        from django.db import transaction
        from . import sms
        transaction.on_commit(lambda: sms.package_update(order, names, new_status))

    if not by_staff and new_status == "delivered":
        from .alerts import notify_staff
        done = " The whole order is now delivered." if result == "delivered" else ""
        who = "The customer confirmed receiving" if by_customer else f"{seller or 'A seller'} marked"
        notify_staff(f"Order #{order.id}: {who} {names}{'' if by_customer else ' as delivered'}.{done}",
                     link=f"/manage/orders/{order.id}/")
        if by_customer:
            from .seller_center import _seller_users
            sellers = {i.seller_account for i in items if i.seller_account_id}
            for s_acc in sellers:
                Notification.objects.bulk_create([
                    Notification(user=u, link=f"/seller/orders/{order.id}/",
                                 message=f"Order #{order.id}: the customer confirmed they received their items.")
                    for u in _seller_users(s_acc)])
    if user is not None:
        from .models import AuditLog
        AuditLog.objects.create(user=user, action=f"Order #{order.id}: {names} -> {new_status}"[:255])
    return result
