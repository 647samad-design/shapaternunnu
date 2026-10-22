"""Doorstep delivery code for cash-on-delivery orders.

The customer gets a 4-digit code (My orders, tracking page, the "on its
way" email and text). The seller or rider asks for it at the door and
enters it to mark the parcel delivered - so a parcel can't be marked
delivered (and the cash counted as collected) without the customer.
Five wrong tries lock the code and alert the store team. Couriers that
report delivery through their API, the customer's own "I got it" and the
store team don't need the code.
"""
import secrets

from django.db import transaction
from django.db.models import F

MAX_TRIES = 5


def enabled():
    from .models import SiteSettings
    return SiteSettings.load().delivery_codes


def assign(order):
    """Gives a cash order its code (once)."""
    from .models import Order
    if order.payment_method != "cod" or order.delivery_code or not enabled():
        return ""
    code = f"{secrets.randbelow(10000):04d}"
    Order.objects.filter(pk=order.pk, delivery_code="").update(delivery_code=code)
    order.delivery_code = code
    return code


def required(order):
    return bool(order.payment_method == "cod" and order.delivery_code and order.status != "cancelled" and enabled())


def locked(order):
    return order.delivery_code_tries >= MAX_TRIES


def check(order, code):
    """(ok, message). Counts wrong tries; locks after MAX_TRIES."""
    from .models import Order
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    with transaction.atomic():
        fresh = Order.objects.select_for_update().get(pk=order.pk)
        if locked(fresh):
            return False, (f"Too many wrong codes for order #{order.id}. The store team has been told - "
                           "ask them to confirm the delivery.")
        if code and secrets.compare_digest(code, fresh.delivery_code):
            return True, ""
        if not code:
            return False, (f"Order #{order.id} is paid in cash at the door: ask the customer for their 4-digit "
                           "delivery code and enter it under 'Ship it' on the order page, then press 'Mark all delivered'.")
        Order.objects.filter(pk=order.pk).update(delivery_code_tries=F("delivery_code_tries") + 1)
        tries = fresh.delivery_code_tries + 1
    if tries >= MAX_TRIES:
        from .alerts import notify_staff
        notify_staff(f"Order #{order.id}: {MAX_TRIES} wrong delivery codes were entered. Check with the customer before "
                     "treating it as delivered.", link=f"/manage/orders/{order.id}/")
    left = MAX_TRIES - tries
    return False, f"That delivery code isn't right ({left} {'try' if left == 1 else 'tries'} left). Ask the customer to read it from their order."
