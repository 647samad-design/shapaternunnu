"""Refunding one item (a return or a problem report)."""
from django.db import transaction
from django.db.models import F


class RefundError(Exception):
    pass


def refund_return(rr, as_credit=False, restock=True, note=""):
    """Refunds a ReturnRequest: card (Stripe) first, store credit for the
    rest, or a manual payback for cash orders. Takes back the loyalty points
    the item earned and (optionally) puts it back in stock. Returns the plan
    dict. Raises RefundError if Stripe refuses or it was already refunded."""
    from . import payments
    from .models import Profile, ReturnRequest
    from .templatetags.bees_extras import money
    with transaction.atomic():
        rr = ReturnRequest.objects.select_for_update(of=("self",)).select_related("order_item__order").get(pk=rr.pk)
        if rr.status == "refunded":
            raise RefundError("This item has already been refunded.")
        if note:
            rr.admin_note = note[:255]
        order = rr.order_item.order
        plan = rr.refund_plan(as_credit=as_credit)
        if plan["card"] and not payments.refund_amount(order, plan["card"], f"return-{rr.id}"):
            raise RefundError("Stripe couldn't process the refund. Try again, or refund from the Stripe dashboard and then mark it refunded.")
        if plan["credit"]:
            Profile.objects.get_or_create(user=rr.user)
            Profile.objects.filter(user=rr.user).update(store_credit=F("store_credit") + plan["credit"])
        if plan["manual"] and not rr.admin_note:
            rr.admin_note = f"Paid back {money(plan['manual'])} directly (cash on delivery order)."
        Profile.objects.filter(user=rr.user, loyalty_points__gte=int(plan["value"])).update(
            loyalty_points=F("loyalty_points") - int(plan["value"]))
        rr.card_refund_amount = plan["card"]
        rr.credit_refund_amount = plan["credit"]
        rr.status = "refunded"
        rr.save()
        if restock:
            payments.restock_item(rr.order_item, rr.order_item.quantity)
    return plan


def describe(plan):
    from .templatetags.bees_extras import money
    parts = []
    if plan["card"]:
        parts.append(f"{money(plan['card'])} sent back to the card")
    if plan["credit"]:
        parts.append(f"{money(plan['credit'])} added as store credit")
    if plan["manual"]:
        parts.append(f"remember to pay the customer {money(plan['manual'])} yourself (cash on delivery order)")
    return ", ".join(parts)
