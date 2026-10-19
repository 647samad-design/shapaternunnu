"""Loyalty tiers (like Daraz / AliExpress levels) and affiliate commission.

Tiers come from what a customer spent on delivered orders in the last 12
months and multiply the loyalty points they earn. Affiliates: anyone who
shares a product link with their code (?ref=CODE) earns store credit when
an order placed through it is delivered (Store settings > Shopper perks).
"""
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import F, Sum
from django.utils import timezone

# (minimum 12-month spend, key, name, points multiplier)
TIERS = [
    (Decimal("1500"), "platinum", "Platinum", Decimal("2")),
    (Decimal("500"), "gold", "Gold", Decimal("1.5")),
    (Decimal("100"), "silver", "Silver", Decimal("1.25")),
    (Decimal("0"), "bronze", "Bronze", Decimal("1")),
]


def spend_12m(user):
    from .models import Order
    since = timezone.now() - timedelta(days=365)
    rows = Order.objects.filter(user=user, status="delivered", created_at__gte=since)
    return sum((o.total for o in rows.only("id")), Decimal("0")) if rows.exists() else Decimal("0")


def tier_for(user):
    """{"key", "name", "multiplier", "spend", "next": (name, amount still to spend) or None}"""
    spend = spend_12m(user)
    for i, (minimum, key, name, mult) in enumerate(TIERS):
        if spend >= minimum:
            nxt = None
            if i > 0:
                up_min, _k, up_name, _m = TIERS[i - 1]
                nxt = (up_name, (up_min - spend).quantize(Decimal("0.01")))
            return {"key": key, "name": name, "multiplier": mult, "spend": spend, "next": nxt}
    return {"key": "bronze", "name": "Bronze", "multiplier": Decimal("1"), "spend": spend, "next": None}


def points_for(order):
    base = int(order.total)
    if base <= 0 or not order.user_id:
        return 0
    return int((Decimal(base) * tier_for(order.user)["multiplier"]).to_integral_value(rounding=ROUND_HALF_UP))


def pay_affiliate(order):
    """Credits the affiliate once the order is delivered. Idempotent."""
    from .models import Notification, Order, Profile, SiteSettings
    pct = SiteSettings.load().affiliate_percent
    if not order.affiliate_code or not pct:
        return None
    goods = sum((i.price * i.quantity for i in order.items.all()), Decimal("0")) - (order.discount_amount or 0)
    amount = (max(goods, Decimal("0")) * Decimal(pct) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount <= 0:
        return None
    if not Order.objects.filter(pk=order.pk, affiliate_paid=False).update(affiliate_paid=True, affiliate_amount=amount):
        return None
    profile = Profile.objects.filter(referral_code=order.affiliate_code).exclude(user_id=order.user_id).first()
    if profile is None:
        return None
    Profile.objects.filter(pk=profile.pk).update(store_credit=F("store_credit") + amount)
    Notification.objects.create(user_id=profile.user_id, link="/account/referrals/",
                                message=f"You earned {amount} store credit: an order from your link was delivered.")
    return amount


def affiliate_stats(profile):
    from .models import Order
    qs = Order.objects.filter(affiliate_code=profile.referral_code).exclude(status="cancelled") if profile.referral_code else Order.objects.none()
    return {"orders": qs.count(), "earned": qs.filter(affiliate_paid=True).aggregate(s=Sum("affiliate_amount"))["s"] or Decimal("0"),
            "pending": qs.filter(affiliate_paid=False).count()}
