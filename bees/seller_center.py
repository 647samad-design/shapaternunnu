"""Numbers and alerts for the Seller Center (/seller/).

Everything a seller sees about money is worked out from the OrderItem rows
of their sales: the price and commission were frozen on each item when the
order was placed, so a later commission change never rewrites history.
Only items that ``counted()`` (not cancelled, unpaid or refunded) are sales.
"""
import logging
from collections import OrderedDict, defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

RANGES = OrderedDict([("7", "7 days"), ("30", "30 days"), ("90", "90 days"), ("365", "12 months")])
ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------

class Period:
    """A date range picked on the dashboard, plus the same-length range just
    before it (for "+12% vs previous period")."""

    def __init__(self, key="30"):
        self.key = key if key in RANGES else "30"
        self.days = int(self.key)
        self.end = timezone.localdate()
        self.start = self.end - timedelta(days=self.days - 1)
        self.prev_end = self.start - timedelta(days=1)
        self.prev_start = self.prev_end - timedelta(days=self.days - 1)
        self.label = RANGES[self.key]

    @staticmethod
    def _bounds(start, end):
        tz = timezone.get_current_timezone()
        return (timezone.make_aware(datetime.combine(start, time.min), tz),
                timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz))

    def filter(self, qs, field="order__created_at", previous=False):
        lo, hi = self._bounds(*(self.prev_start, self.prev_end) if previous else (self.start, self.end))
        return qs.filter(**{f"{field}__gte": lo, f"{field}__lt": hi})


# ---------------------------------------------------------------------------
# Sales figures
# ---------------------------------------------------------------------------

def sold_items(seller):
    from .models import OrderItem
    return OrderItem.objects.filter(seller_account=seller)


def summarize(items):
    """Totals for a queryset of counted OrderItems."""
    agg = items.aggregate(
        sales=Sum(F("price") * F("quantity")), commission=Sum("commission_amount"), units=Sum("quantity"),
    )
    sales = agg["sales"] or ZERO
    commission = agg["commission"] or ZERO
    orders = items.values("order_id").distinct().count()
    return {
        "sales": sales,
        "commission": commission,
        "net": sales - commission,
        "units": agg["units"] or 0,
        "orders": orders,
        "aov": (sales / orders).quantize(Decimal("0.01")) if orders else ZERO,
    }


def change(current, previous):
    """Percentage change, or None when there's nothing to compare with."""
    current, previous = Decimal(str(current or 0)), Decimal(str(previous or 0))
    if previous == 0:
        return None
    return round(float((current - previous) / previous * 100), 1)


def chart(items, period):
    """Revenue buckets for the bar chart: daily up to 31 days, weekly for
    90 days and monthly for a year."""
    rows = items.values_list("order__created_at", "price", "quantity")
    if period.days <= 31:
        keys = [period.start + timedelta(days=i) for i in range(period.days)]
        bucket = lambda d: d  # noqa: E731
        label = (lambda d: d.strftime("%a")) if period.days <= 7 else (lambda d: d.strftime("%-d %b"))
    elif period.days <= 120:
        first = period.start - timedelta(days=period.start.weekday())
        keys = []
        cur = first
        while cur <= period.end:
            keys.append(cur)
            cur += timedelta(days=7)
        bucket = lambda d: d - timedelta(days=d.weekday())  # noqa: E731
        label = lambda d: d.strftime("%-d %b")  # noqa: E731
    else:
        keys = []
        y, m = period.start.year, period.start.month
        while (y, m) <= (period.end.year, period.end.month):
            keys.append(period.start.replace(year=y, month=m, day=1))
            m += 1
            if m > 12:
                y, m = y + 1, 1
        bucket = lambda d: d.replace(day=1)  # noqa: E731
        label = lambda d: d.strftime("%b")  # noqa: E731
    totals = defaultdict(lambda: ZERO)
    for created, price, qty in rows:
        totals[bucket(timezone.localdate(created))] += price * qty
    top = max([totals[k] for k in keys] or [ZERO]) or Decimal("1")
    return [
        {"label": label(k), "date": k, "amount": totals[k], "pct": round(float(totals[k] / top * 100), 1)}
        for k in keys
    ]


def top_products(items, limit=5):
    rows = (
        items.values("product_id", "product_name")
        .annotate(units=Sum("quantity"), revenue=Sum(F("price") * F("quantity")))
        .order_by("-revenue")[:limit]
    )
    rows = list(rows)
    top = max([r["revenue"] for r in rows] or [ZERO]) or Decimal("1")
    for r in rows:
        r["pct"] = round(float(r["revenue"] / top * 100), 1)
    return rows


def by_country(items, limit=6):
    from .countries import COUNTRY_NAMES
    rows = list(
        items.values("order__country").annotate(revenue=Sum(F("price") * F("quantity")), units=Sum("quantity"))
        .order_by("-revenue")
    )
    total = sum((r["revenue"] for r in rows), ZERO) or Decimal("1")
    out = []
    for r in rows[:limit]:
        code = (r["order__country"] or "").upper()
        out.append({
            "code": code, "name": COUNTRY_NAMES.get(code, code or "Not given"),
            "revenue": r["revenue"], "units": r["units"], "share": round(float(r["revenue"] / total * 100), 1),
        })
    return out


# ---------------------------------------------------------------------------
# Fulfilment queue
# ---------------------------------------------------------------------------

def shippable(items):
    """Items the seller should act on: order not cancelled and either paid
    or cash on delivery."""
    return items.exclude(order__status="cancelled").exclude(order__payment_status__in=["pending", "failed", "refunded"])


def queue_counts(seller):
    items = shippable(sold_items(seller))
    late_before = timezone.now() - timedelta(days=2)
    return {
        "to_pack": items.filter(fulfillment_status="pending").count(),
        "packed": items.filter(fulfillment_status="packed").count(),
        "in_transit": items.filter(fulfillment_status="handed_to_courier").count(),
        "late": items.filter(fulfillment_status__in=["pending", "packed"], order__created_at__lt=late_before).count(),
    }


# ---------------------------------------------------------------------------
# Balance and payouts
# ---------------------------------------------------------------------------

def on_hold_items(seller):
    """Sold items whose earnings can't be paid out yet."""
    from datetime import timedelta
    from django.db.models import Q
    from .models import SiteSettings
    cutoff = timezone.now() - timedelta(days=SiteSettings.load().return_days)
    items = sold_items(seller).counted().select_related("order").only(
        "price", "quantity", "commission_amount", "delivered_at", "order__delivered_at", "order__status")
    settled = (Q(delivered_at__lte=cutoff) | Q(delivered_at=None, order__status="delivered", order__delivered_at__lte=cutoff))
    disputed = (Q(return_requests__status__in=["requested", "approved"])
                | Q(claims__status__in=["open", "seller_replied", "escalated"]))
    return items.filter(~settled | disputed).distinct()


def release_date(item):
    """When an item's earnings become available (None if not delivered)."""
    from datetime import timedelta
    from .models import SiteSettings
    received = item.delivered_at or (item.order.delivered_at if item.order.status == "delivered" else None)
    return received + timedelta(days=SiteSettings.load().return_days) if received else None


def balance(seller):
    """What the seller has earned, been paid and is still owed."""
    requested = seller.payouts.filter(status="requested").aggregate(n=Sum("amount"))["n"] or ZERO
    owed = Decimal(str(seller.amount_owed))
    # Earnings stay "pending" until the item has been delivered AND its
    # return window has passed with no open return or problem report - so
    # the store never pays out money it may have to refund.
    pending = ZERO
    for item in on_hold_items(seller):
        pending += item.seller_earning
    return {
        "earned": Decimal(str(seller.net_earnings)),
        "paid": seller.total_paid_out or ZERO,
        "owed": owed,
        "pending": min(pending, owed),
        "available": max(owed - min(pending, owed), ZERO),
        "requested": requested,
    }


def setup_steps(seller):
    """Getting-started checklist shown until everything is done."""
    has_product = seller.products.exists()
    approved = seller.status == "approved"
    steps = [
        ("Add a store logo", bool(seller.store_logo), "seller_store_settings"),
        ("Describe your store", bool(seller.store_description.strip()), "seller_store_settings"),
        ("Add payout details", bool(seller.bank_details.strip()), "seller_store_settings"),
        ("List your first product", has_product, "seller_add_product" if approved else None),
        ("Get a product approved", seller.products.filter(approval_status="approved").exists(), "seller_products" if approved else None),
    ]
    done = sum(1 for _, ok, _ in steps if ok)
    return {"steps": steps, "done": done, "total": len(steps), "pct": round(done / len(steps) * 100)}


# ---------------------------------------------------------------------------
# Alerts to sellers
# ---------------------------------------------------------------------------

def _seller_users(seller):
    users = [seller.user]
    users += [m.user for m in seller.team_members.select_related("user")]
    return users


def notify_new_order(order):
    """Tells every seller in the order that they have something to ship.
    Runs once per order, as soon as it is ready to ship (cash on delivery
    placed, or card payment received)."""
    from .models import Notification, Order
    if order.status == "cancelled" or order.payment_status in ("pending", "failed", "refunded"):
        return
    if order.payment_method == "cod" and not order.cod_confirmed:
        return  # waiting for the customer to confirm by email
    with transaction.atomic():
        updated = Order.objects.filter(pk=order.pk, sellers_notified=False).update(sellers_notified=True)
    if not updated:
        return
    by_seller = defaultdict(list)
    for item in order.items.select_related("seller_account__user"):
        if item.seller_account_id:
            by_seller[item.seller_account].append(item)
    for seller, items in by_seller.items():
        units = sum(i.quantity for i in items)
        earning = sum((i.seller_earning for i in items), ZERO)
        link = f"/seller/orders/{order.id}/"
        for user in _seller_users(seller):
            Notification.objects.create(
                user=user, link=link,
                message=f"New order #{order.id}: {units} item{'s' if units != 1 else ''} to ship. Pack it within 2 days.",
            )
        transaction.on_commit(lambda s=seller, its=items, e=earning: _email_new_order(order.pk, s.pk, [i.pk for i in its], e))
        from . import sms
        transaction.on_commit(lambda s=seller, u=units: sms.seller_new_order(s, order, u))


def _email_new_order(order_id, seller_id, item_ids, earning):
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string
    from django.utils.html import strip_tags

    from .models import Order, OrderItem, SellerAccount, SiteSettings
    from .order_emails import absolute
    seller = SellerAccount.objects.select_related("user").filter(pk=seller_id).first()
    order = Order.objects.filter(pk=order_id).first()
    recipient = seller and seller.user.email
    if not (seller and order and recipient):
        return
    try:
        brand = SiteSettings.load()
        html = render_to_string("bees/emails/seller_new_order.html", {
            "brand": brand, "seller": seller, "order": order, "earning": earning,
            "items": OrderItem.objects.filter(pk__in=item_ids),
            "url": absolute(f"/seller/orders/{order.id}/"),
        })
        subject = f"{brand.site_name}: new order #{order.id} to ship"
        email = EmailMultiAlternatives(subject, strip_tags(html), None, [recipient])
        email.attach_alternative(html, "text/html")
        email.send(fail_silently=False)
    except Exception as exc:  # never break checkout for a mail problem
        from .alerts import email_failed
        email_failed(f"New order #{order_id} (seller)", recipient, exc)


def order_status_changed(order, old_status, new_status):
    """Called by Order.save(): tells sellers when an order they were asked
    to ship is cancelled, so they don't send it."""
    if new_status != "cancelled" or not order.sellers_notified:
        return
    from .models import Notification, SellerAccount
    sellers = SellerAccount.objects.filter(sold_items__order=order).distinct()
    for seller in sellers:
        for user in _seller_users(seller):
            Notification.objects.create(
                user=user, link=f"/seller/orders/{order.id}/",
                message=f"Order #{order.id} was cancelled. Don't ship it - if it's already sent, contact support.",
            )
