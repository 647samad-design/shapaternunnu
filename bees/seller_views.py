"""
Seller Center (/seller/): where individual sellers and organizations run
their store - sales and earnings, orders to ship, products, payouts,
returns, reviews, settings and team. Uses the same layout as the store
admin (/manage/).

Who can see what:
    owner / team admin  -> everything
    team staff          -> overview, orders, products, returns, reviews
                           (no money pages, settings or team)
"""
import csv
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg, Count, F, Prefetch, Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import badges as seller_badges
from . import seller_center as sc
from .messaging import unread_for_seller
from . import performance, statements
from .models import (
    AuditLog, Claim, Notification, Order, OrderItem, Payout, Product, ProductImage, Question, ReturnRequest, Review,
    SellerReview,
)
from .security import safe_next_url
from .templatetags.bees_extras import money

MONEY_ROLES = ("owner", "admin")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def seller_required(roles=None, approved=False):
    """Loads the seller account the user works for into ``request.seller`` /
    ``request.seller_role``. Users without one are sent to the sign-up page."""
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect(f"{reverse('login')}?{urlencode({'next': request.get_full_path()})}")
            from .views import get_seller_account_for_user
            seller, role = get_seller_account_for_user(request.user)
            if not seller:
                return redirect("become_seller")
            if roles and role not in roles:
                messages.error(request, "Only the store owner or a team admin can open that page.")
                return redirect("seller_dashboard")
            if approved and seller.status != "approved":
                messages.error(request, "This becomes available once your seller account is approved.")
                return redirect("seller_dashboard")
            request.seller, request.seller_role = seller, role
            return view(request, *args, **kwargs)
        return wrapped
    return decorator


def _nav_counts(seller):
    items = sc.shippable(sc.sold_items(seller))
    return {
        "orders": items.filter(fulfillment_status="pending").values("order_id").distinct().count(),
        "products": seller.products.filter(Q(stock__lte=0) | Q(approval_status="rejected")).count(),
        "returns": ReturnRequest.objects.filter(order_item__seller_account=seller, status="requested").count(),
        "questions": Question.objects.filter(product__seller_account=seller, answer="").count(),
        "messages": unread_for_seller(seller),
        "claims": Claim.objects.filter(order_item__seller_account=seller, status="open").count(),
    }


@seller_required()
def claims(request):
    from .claims import seller_claims
    return seller_claims(request)


def _render(request, template, context):
    seller, role = request.seller, request.seller_role
    context.update({
        "seller": seller, "role": role, "can_money": role in MONEY_ROLES,
        "nav": _nav_counts(seller),
    })
    return render(request, f"bees/seller/{template}", context)


def _page(request, qs, per_page=20):
    return Paginator(qs, per_page).get_page(request.GET.get("page"))


def _back(request, fallback):
    return redirect(safe_next_url(request, request.POST.get("next") or request.GET.get("next"), fallback))


def _csv_response(filename):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("﻿")  # so Excel opens accented names correctly
    return response


def _csv_safe(value):
    """Stops spreadsheet apps from running text that looks like a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def item_state(item):
    """One human label for where a sold item stands."""
    order = item.order
    if order.status == "cancelled":
        return "cancelled", "Cancelled"
    if order.payment_status in ("pending", "failed"):
        return "unpaid", "Awaiting payment"
    if order.payment_status == "refunded" or getattr(item, "was_refunded", False):
        return "refunded", "Refunded"
    return item.fulfillment_status, item.get_fulfillment_status_display()


def tier_progress(seller):
    """Where the seller stands on the volume-discount ladder."""
    base = float(seller.commission_rate)
    sales = float(seller.lifetime_sales)
    tiers = sorted(getattr(settings, "COMMISSION_TIERS", []))
    nxt = next(((t, r) for t, r in tiers if sales < t), None)
    if not nxt:
        return {"current": seller.effective_commission_rate, "base": base, "next": None}
    threshold, reduction = nxt
    return {
        "current": seller.effective_commission_rate, "base": base,
        "next": {"rate": max(base - reduction, 3), "threshold": threshold, "remaining": threshold - sales,
                 "pct": round(min(sales / threshold * 100, 100), 1)},
    }


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

@seller_required()
def overview(request):
    seller = request.seller
    period = sc.Period(request.GET.get("range", "30"))
    counted = sc.sold_items(seller).counted()
    current = period.filter(counted)
    now = sc.summarize(current)
    before = sc.summarize(period.filter(counted, previous=True))
    for key in ("sales", "net", "units", "orders", "commission", "aov"):
        now[f"{key}_change"] = sc.change(now[key], before[key])

    products = seller.products.all()
    rating = Review.objects.filter(product__seller_account=seller).aggregate(avg=Avg("rating"), n=Count("id"))
    recent = (
        sc.sold_items(seller).select_related("order").order_by("-order__created_at")[:8]
    )
    for item in recent:
        item.state, item.state_label = item_state(item)
    return _render(request, "overview.html", {
        "section": "overview",
        "period": period, "ranges": sc.RANGES.items(),
        "stats": now,
        "chart": sc.chart(current, period),
        "top_products": sc.top_products(current),
        "countries": sc.by_country(current),
        "queue": sc.queue_counts(seller),
        "balance": sc.balance(seller) if request.seller_role in MONEY_ROLES else None,
        "tier": tier_progress(seller),
        "badge": seller_badges.progress(seller),
        "perf": performance.measure(seller),
        "setup": sc.setup_steps(seller),
        "recent": recent,
        "live_count": products.live().count(),
        "product_count": products.count(),
        "low_stock": products.filter(stock__gt=0, stock__lte=5).order_by("stock")[:5],
        "out_of_stock": products.filter(stock__lte=0).count(),
        "rating": rating,
        "open_questions": Question.objects.filter(product__seller_account=seller, answer="").count(),
    })


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

ORDER_TABS = [
    ("to_pack", "To pack"), ("packed", "Packed"), ("in_transit", "In transit"),
    ("delivered", "Delivered"), ("unpaid", "Awaiting payment"), ("cancelled", "Cancelled"), ("all", "All"),
]


def _filter_items(items, tab):
    shippable = sc.shippable(items)
    return {
        "to_pack": shippable.filter(fulfillment_status="pending"),
        "packed": shippable.filter(fulfillment_status="packed"),
        "in_transit": shippable.filter(fulfillment_status="handed_to_courier"),
        "delivered": items.exclude(order__status="cancelled").filter(fulfillment_status="delivered"),
        "unpaid": items.exclude(order__status="cancelled").filter(order__payment_status__in=["pending", "failed"]),
        "cancelled": items.filter(order__status="cancelled"),
    }.get(tab, items)


@seller_required()
def orders(request):
    seller = request.seller
    tab = request.GET.get("tab", "to_pack")
    if tab not in dict(ORDER_TABS):
        tab = "to_pack"
    q = request.GET.get("q", "").strip().lstrip("#")
    items = sc.sold_items(seller)
    tab_counts = {key: _filter_items(items, key).values("order_id").distinct().count() for key, _ in ORDER_TABS}
    items = _filter_items(items, tab)
    if q:
        filt = Q(order__full_name__icontains=q) | Q(product_name__icontains=q) | Q(order__email__icontains=q)
        if q.isdigit():
            filt |= Q(order_id=int(q))
        items = items.filter(filt)

    if request.GET.get("export") == "csv":
        return _orders_csv(items.select_related("order", "variant").order_by("-order__created_at"))

    my_items = OrderItem.objects.filter(seller_account=seller).select_related("variant", "product")
    qs = (
        Order.objects.filter(id__in=items.values("order_id"))
        .prefetch_related(Prefetch("items", queryset=my_items, to_attr="my_items"))
        .order_by("-created_at")
    )
    page_obj = _page(request, qs)
    for order in page_obj.object_list:
        for item in order.my_items:
            item.order = order
            item.state, item.state_label = item_state(item)
        order.my_units = sum(i.quantity for i in order.my_items)
        order.my_sales = sum((i.subtotal for i in order.my_items), Decimal("0"))
        order.my_earning = sum((i.seller_earning for i in order.my_items), Decimal("0"))
        states = {i.state for i in order.my_items}
        order.my_state = order.my_items[0].state_label if len(states) == 1 else "Mixed"
        order.my_state_key = order.my_items[0].state if len(states) == 1 else "mixed"
        order.can_ship = (order.status != "cancelled" and order.payment_status not in ("pending", "failed", "refunded")
                          and any(i.fulfillment_status != "delivered" for i in order.my_items))
    return _render(request, "orders.html", {
        "section": "orders", "tabs": [(k, label, tab_counts[k]) for k, label in ORDER_TABS], "tab": tab,
        "q": request.GET.get("q", ""), "page_obj": page_obj,
    })


def _orders_csv(items):
    from .templatetags.bees_extras import country_name
    response = _csv_response(f"orders-{timezone.localdate():%Y-%m-%d}.csv")
    writer = csv.writer(response)
    writer.writerow(["Order", "Date", "Customer", "City", "Country", "Product", "Option", "SKU", "Qty",
                     "Unit price", "Sale", "Commission %", "Commission", "You earn", "Payment", "Status"])
    for item in items:
        o = item.order
        writer.writerow([
            o.id, timezone.localtime(o.created_at).strftime("%Y-%m-%d %H:%M"), _csv_safe(o.full_name), _csv_safe(o.city),
            country_name(o.country), _csv_safe(item.product_name), item.variant.label if item.variant else "",
            _csv_safe(item.variant.sku if item.variant else ""), item.quantity, item.price, item.subtotal,
            item.commission_rate, item.commission_amount, item.seller_earning, o.get_payment_method_display(),
            item_state(item)[1],
        ])
    return response


@seller_required()
def order_detail(request, order_id):
    seller = request.seller
    my_items = list(
        OrderItem.objects.filter(seller_account=seller, order_id=order_id)
        .select_related("order", "variant", "product").prefetch_related("return_requests")
    )
    if not my_items:
        raise Http404("No items from your store in this order.")
    order = my_items[0].order
    for item in my_items:
        item.order = order
        item.was_refunded = any(r.status == "refunded" for r in item.return_requests.all())
        item.state, item.state_label = item_state(item)
        item.open_return = next((r for r in item.return_requests.all() if r.status in ("requested", "approved")), None)
    only_seller = not order.items.exclude(seller_account=seller).exists()
    can_ship = order.status != "cancelled" and order.payment_status not in ("pending", "failed", "refunded")
    totals = {
        "units": sum(i.quantity for i in my_items),
        "sales": sum((i.subtotal for i in my_items), Decimal("0")),
        "commission": sum((i.commission_amount for i in my_items), Decimal("0")),
        "earning": sum((i.seller_earning for i in my_items), Decimal("0")),
    }
    steps = [
        ("Placed", True, order.created_at),
        ("Paid" if order.payment_method == "card" else "Cash on delivery", order.payment_status in ("paid", "not_applicable", "refunded"), None),
        ("Packed", all(i.fulfillment_status in ("packed", "handed_to_courier", "delivered") for i in my_items), None),
        ("Shipped", all(i.fulfillment_status in ("handed_to_courier", "delivered") for i in my_items), None),
        ("Delivered", all(i.fulfillment_status == "delivered" for i in my_items), order.delivered_at),
    ]
    from . import couriers
    from .models import Shipment
    provider = couriers.active()
    shipments = list(Shipment.objects.filter(order=order, seller=seller))
    quote = next((s for s in shipments if s.status == "rated" and str(s.id) == request.GET.get("shipment", "")), None)
    return _render(request, "order_detail.html", {
        "courier_on": provider is not None, "courier_name": getattr(provider, "name", ""), "shipments": shipments, "quote": quote,
        "can_book": can_ship and any(i.fulfillment_status in ("pending", "packed") for i in my_items),
        "section": "orders", "order": order, "items": my_items, "totals": totals, "steps": steps,
        "only_seller": only_seller, "can_ship": can_ship, "fulfilment_choices": OrderItem.FULFILLMENT_CHOICES,
        "other_sellers": not only_seller,
    })


def _set_items_status(request, items, new_status):
    """Moves the given items forward to ``new_status`` (bulk actions never
    move an item back). Returns how many changed and the orders that moved
    forward as a result."""
    from . import fulfilment
    try:
        changed, moved = fulfilment.set_items_status(items, new_status, user=request.user, forward_only=True)
    except fulfilment.FulfilmentError:
        return 0, []
    return len(changed), moved


@seller_required(approved=True)
@require_POST
def ship_order(request, order_id):
    """Ships all of this seller's items in one order. When the whole order
    is theirs, the courier and tracking number are saved on the order so
    the customer's "on its way" email includes them."""
    seller = request.seller
    items = list(OrderItem.objects.filter(seller_account=seller, order_id=order_id).select_related("order"))
    if not items:
        raise Http404
    order = items[0].order
    new_status = request.POST.get("status", "handed_to_courier")
    if new_status not in dict(OrderItem.FULFILLMENT_CHOICES):
        new_status = "handed_to_courier"
    if order.status == "cancelled":
        messages.error(request, f"Order #{order.id} was cancelled - don't ship it.")
        return redirect("seller_order", order_id=order.id)
    if order.payment_status in ("pending", "failed", "refunded"):
        messages.error(request, f"Order #{order.id} hasn't been paid - wait before shipping.")
        return redirect("seller_order", order_id=order.id)
    if order.payment_method == "cod" and not order.cod_confirmed:
        messages.error(request, f"Order #{order.id} is a cash order the customer hasn't confirmed yet - wait for the confirmation.")
        return redirect("seller_order", order_id=order.id)
    only_seller = not order.items.exclude(seller_account=seller).exists()
    courier = request.POST.get("courier_name", "").strip()[:60]
    tracking = request.POST.get("tracking_number", "").strip()[:60]
    if only_seller and (courier or tracking):
        Order.objects.filter(pk=order.pk).update(
            courier_name=courier or order.courier_name, tracking_number=tracking or order.tracking_number,
        )
    changed, moved = _set_items_status(request, items, new_status)
    label = dict(OrderItem.FULFILLMENT_CHOICES)[new_status].lower()
    note = (f" The order is now {moved[0][1]} and the customer has been told." if moved
            else " The customer has been told." if changed and new_status in ("handed_to_courier", "delivered") else "")
    if changed:
        messages.success(request, f"Marked {changed} item{'s' if changed != 1 else ''} as {label}.{note}")
    elif courier or tracking:
        messages.success(request, "Tracking details saved.")
    else:
        messages.info(request, "Nothing to change.")
    return redirect("seller_order", order_id=order.id)


@seller_required(approved=True)
@require_POST
def bulk_fulfilment(request):
    seller = request.seller
    new_status = request.POST.get("status")
    ids = [int(i) for i in request.POST.getlist("order") if i.isdigit()]
    if new_status not in dict(OrderItem.FULFILLMENT_CHOICES) or not ids:
        messages.error(request, "Pick at least one order and what to mark it as.")
        return _back(request, "seller_orders")
    items = OrderItem.objects.filter(seller_account=seller, order_id__in=ids).select_related("order")
    changed, moved = _set_items_status(request, items, new_status)
    label = dict(OrderItem.FULFILLMENT_CHOICES)[new_status].lower()
    messages.success(request, f"Marked {changed} item{'s' if changed != 1 else ''} as {label}." + (
        f" {len(moved)} order{'s' if len(moved) != 1 else ''} moved forward and the customers were emailed." if moved else ""))
    return _back(request, "seller_orders")


@seller_required()
def packing_slip(request, order_id):
    items = list(OrderItem.objects.filter(seller_account=request.seller, order_id=order_id).select_related("order", "variant"))
    if not items:
        raise Http404
    return render(request, "bees/seller/packing_slip.html", {
        "seller": request.seller, "order": items[0].order, "items": items,
    })


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

PRODUCT_TABS = [
    ("all", "All"), ("live", "Live"), ("review", "In review"), ("rejected", "Rejected"),
    ("low", "Low stock"), ("out", "Out of stock"),
]


@seller_required()
def products(request):
    seller = request.seller
    base = seller.products.exclude(approval_status="archived")
    filters = {
        "all": base, "live": base.live(), "review": base.filter(approval_status="pending"),
        "rejected": base.filter(approval_status="rejected"), "low": base.filter(stock__gt=0, stock__lte=5),
        "out": base.filter(stock__lte=0),
    }
    tab = request.GET.get("tab", "all")
    if tab not in filters:
        tab = "all"
    qs = filters[tab]
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(variants__sku__icontains=q)).distinct()
    sort = request.GET.get("sort", "new")
    counted = OrderItem.objects.counted().filter(seller_account=seller)
    sold = {r["product_id"]: r for r in counted.values("product_id").annotate(
        units=Sum("quantity"), revenue=Sum(F("price") * F("quantity")))}
    ratings = {r["product_id"]: r for r in Review.objects.filter(product__seller_account=seller)
               .values("product_id").annotate(avg=Avg("rating"), n=Count("id"))}
    order_by = {"new": "-created_at", "name": "name", "price": "-price", "stock": "stock"}.get(sort, "-created_at")
    qs = qs.annotate(variant_count=Count("variants", distinct=True))
    page_obj = _page(request, qs.order_by(order_by, "-id"), per_page=25)
    for p in page_obj.object_list:
        p.units_sold = (sold.get(p.id) or {}).get("units") or 0
        p.revenue = (sold.get(p.id) or {}).get("revenue") or 0
        r = ratings.get(p.id) or {}
        p.rating_avg, p.rating_n = r.get("avg"), r.get("n") or 0
    return _render(request, "products.html", {
        "section": "products", "page_obj": page_obj, "tab": tab, "q": q, "sort": sort,
        "tabs": [(k, label, filters[k].count()) for k, label in PRODUCT_TABS],
    })


@seller_required(approved=True)
@require_POST
def quick_update(request, pk):
    """Price and stock straight from the product list (no re-review)."""
    product = get_object_or_404(Product, pk=pk, seller_account=request.seller)
    try:
        price = Decimal(request.POST.get("price", "")).quantize(Decimal("0.01"))
        if price < Decimal("0.01") or price > Decimal("99999999"):
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        messages.error(request, "Enter a price above zero.")
        return _back(request, "seller_products")
    fields = {"price": price}
    if not product.has_variants:
        try:
            stock = int(request.POST.get("stock", ""))
            if not 0 <= stock <= 1_000_000:
                raise ValueError
        except ValueError:
            messages.error(request, "Stock must be a whole number, 0 or more.")
            return _back(request, "seller_products")
        fields["stock"] = stock
    for key, value in fields.items():
        setattr(product, key, value)
    if product.old_price and product.old_price <= product.price:
        product.old_price = None
    product.save()  # back-in-stock emails go out from the post_save signal
    messages.success(request, f"Updated '{product.name}'.")
    return _back(request, "seller_products")


@seller_required(approved=True)
@require_POST
def duplicate_product(request, pk):
    """Copies a listing (with its sizes/colours and photos) as a new draft
    that goes through review like any new product."""
    original = get_object_or_404(Product, pk=pk, seller_account=request.seller)
    if not request.seller.can_add_product:
        messages.error(request, f"Your plan allows {request.seller.product_limit} products. Upgrade your plan to add more.")
        return redirect("seller_plan")
    with transaction.atomic():
        variants = list(original.variants.all())
        images = list(ProductImage.objects.filter(product=original))
        copy = Product.objects.get(pk=original.pk)
        copy.pk = None
        copy.id = None
        copy.name = f"{original.name} (copy)"[:255]
        copy.approval_status = "pending"
        copy.is_flash_sale = False
        copy.save()
        for v in variants:
            v.pk = None
            v.product = copy
            v.save()
        for img in images:
            img.pk = None
            img.product = copy
            img.save()
        if variants:
            copy.sync_variants()
    messages.success(request, f"Copied as '{copy.name}'. Change what you need, then it goes live after review.")
    return redirect("seller_edit_product", pk=copy.pk)


# ---------------------------------------------------------------------------
# Earnings and payouts
# ---------------------------------------------------------------------------

def _ledger(seller, period):
    items = period.filter(sc.sold_items(seller)).select_related("order", "variant").prefetch_related("return_requests")
    return items.order_by("-order__created_at", "-id")


def _decorate(items):
    for item in items:
        item.was_refunded = any(r.status == "refunded" for r in item.return_requests.all())
        item.state, item.state_label = item_state(item)
        item.counts = item.state not in ("cancelled", "unpaid", "refunded")
    return items


@seller_required(roles=MONEY_ROLES)
def monthly_statement(request):
    from django.http import HttpResponse
    from . import statements
    content, month = statements.pdf(request.seller, request.GET.get("month", ""))
    response = HttpResponse(content, content_type="application/pdf")
    slug = "".join(c for c in request.seller.display_name if c.isalnum())[:30] or "store"
    response["Content-Disposition"] = f'attachment; filename="{slug}-statement-{month:%Y-%m}.pdf"'
    return response


@seller_required(roles=MONEY_ROLES)
def earnings(request):
    seller = request.seller
    period = sc.Period(request.GET.get("range", "30"))
    ledger = _ledger(seller, period)
    if request.GET.get("export") == "csv":
        return _statement_csv(seller, period, _decorate(list(ledger)))
    stats = sc.summarize(period.filter(sc.sold_items(seller).counted()))
    page_obj = _page(request, ledger, per_page=25)
    _decorate(page_obj.object_list)
    balance = sc.balance(seller)
    min_payout = Decimal(str(getattr(settings, "MIN_PAYOUT", 10)))
    return _render(request, "earnings.html", {
        "section": "earnings", "period": period, "ranges": sc.RANGES.items(),
        "stats": stats, "page_obj": page_obj, "balance": balance, "tier": tier_progress(seller),
        "payouts": seller.payouts.all()[:50], "min_payout": min_payout,
        "statement_months": statements.months_with_sales(seller),
        "open_request": seller.payouts.filter(status="requested").first(),
        "sending": seller.payouts.filter(status="processing").first(),
        "payout_options": _payout_options(), "auto_ready": _auto_ready(seller),
        "lifetime": {"sales": seller.lifetime_sales, "commission": seller.commission_total, "net": seller.net_earnings},
    })


def _payout_options():
    from . import payouts as auto
    return auto.options()


def _auto_ready(seller):
    from . import payouts as auto
    return auto.method_ready(seller)


def _statement_csv(seller, period, items):
    response = _csv_response(f"statement-{seller.pk}-{period.start:%Y%m%d}-{period.end:%Y%m%d}.csv")
    writer = csv.writer(response)
    writer.writerow([f"Statement for {seller.display_name}", f"{period.start} to {period.end}"])
    writer.writerow([])
    writer.writerow(["Date", "Order", "Product", "Option", "Qty", "Sale", "Commission %", "Commission", "You earn", "Status", "Counts"])
    totals = [Decimal("0")] * 3
    for item in items:
        writer.writerow([
            timezone.localtime(item.order.created_at).strftime("%Y-%m-%d"), item.order_id, _csv_safe(item.product_name),
            item.variant.label if item.variant else "", item.quantity, item.subtotal, item.commission_rate,
            item.commission_amount, item.seller_earning, item.state_label, "yes" if item.counts else "no",
        ])
        if item.counts:
            totals[0] += item.subtotal
            totals[1] += item.commission_amount
            totals[2] += item.seller_earning
    writer.writerow([])
    writer.writerow(["Totals (counted sales only)", "", "", "", "", totals[0], "", totals[1], totals[2]])
    writer.writerow([])
    writer.writerow(["Payouts in this period"])
    lo, hi = period._bounds(period.start, period.end)
    for p in seller.payouts.filter(status="paid", paid_at__gte=lo, paid_at__lt=hi):
        writer.writerow([timezone.localtime(p.paid_at).strftime("%Y-%m-%d"), p.get_method_display(), _csv_safe(p.reference), p.amount])
    return response


@seller_required(roles=MONEY_ROLES, approved=True)
@require_POST
def request_payout(request):
    seller = request.seller
    min_payout = Decimal(str(getattr(settings, "MIN_PAYOUT", 10)))
    with transaction.atomic():
        from .models import SellerAccount
        SellerAccount.objects.select_for_update().filter(pk=seller.pk).first()
        if seller.payouts.filter(status="requested").exists():
            messages.error(request, "You already have a payout request waiting. We'll process it soon.")
            return redirect("seller_earnings")
        from . import payouts as auto
        automatic = auto.method_ready(seller)
        if not automatic and not seller.bank_details.strip():
            messages.error(request, "Add your payout details (bank / PayPal) in Store settings first.")
            return redirect("seller_earnings")
        available = sc.balance(seller)["available"]
        try:
            amount = Decimal(request.POST.get("amount", "")).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            amount = Decimal("0")
        if amount < min_payout:
            messages.error(request, f"The minimum payout is {money(min_payout)}.")
            return redirect("seller_earnings")
        if amount > available:
            messages.error(request, f"You can request up to {money(available)}. Earnings become available once an item is delivered and its return window has passed.")
            return redirect("seller_earnings")
        if not automatic:
            payout = Payout.objects.create(seller=seller, amount=amount, status="requested", requested_by=request.user)
    if automatic:
        try:
            payout = auto.send(seller, amount, user=request.user)
        except auto.PayoutError as exc:
            messages.error(request, f"We couldn't send it: {exc}")
            return redirect("seller_earnings")
        AuditLog.objects.create(user=request.user, action=f"Seller {seller.display_name} withdrew {money(amount)} ({payout.method})")
        messages.success(request, f"{money(amount)} is on its way to your {payout.get_method_display()} account."
                         if payout.status == "processing" else f"{money(amount)} has been sent to your {payout.get_method_display()} account.")
        return redirect("seller_earnings")
    from .alerts import notify_staff
    notify_staff(f"{seller.display_name} asked for a payout of {money(amount)}.", link=f"/manage/sellers/{seller.pk}/")
    from .manage_views import _refresh_attention
    _refresh_attention()
    AuditLog.objects.create(user=request.user, action=f"Seller {seller.display_name} requested payout #{payout.pk} of {money(amount)}")
    messages.success(request, f"Payout of {money(amount)} requested. We'll send it to your payout account and let you know.")
    return redirect("seller_earnings")


@seller_required(roles=MONEY_ROLES)
@require_POST
def payout_settings(request):
    """How the seller wants to be paid: manual bank transfer, PayPal or Stripe."""
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email
    from . import payouts as auto
    seller = request.seller
    method = request.POST.get("payout_method", "manual")
    if method not in dict(seller.PAYOUT_METHODS) or (method != "manual" and not auto.options().get(method)):
        messages.error(request, "That payout method isn't available.")
        return redirect("seller_earnings")
    fields = ["payout_method"]
    if method == "paypal":
        email = request.POST.get("paypal_email", "").strip().lower()
        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, "Enter the email address of your PayPal account.")
            return redirect("seller_earnings")
        seller.paypal_email = email
        fields.append("paypal_email")
    if method == "stripe" and not (seller.stripe_account_id and seller.stripe_payouts_ready):
        return redirect("seller_stripe_connect")
    seller.payout_method = method
    seller.save(update_fields=fields)
    AuditLog.objects.create(user=request.user, action=f"Seller {seller.display_name}: payout method {method}")
    messages.success(request, "Payout method saved." + (" Payouts now go to your PayPal automatically." if method == "paypal" else ""))
    return redirect("seller_earnings")


@seller_required(roles=MONEY_ROLES, approved=True)
def stripe_connect(request):
    from . import payouts as auto
    if not auto.options()["stripe"]:
        messages.error(request, "Stripe payouts aren't available on this store.")
        return redirect("seller_earnings")
    try:
        return redirect(auto.stripe_onboarding_link(request, request.seller))
    except auto.PayoutError as exc:
        messages.error(request, f"Stripe couldn't set up your account: {exc}. Stripe may not support payouts to your country yet - choose PayPal or bank transfer instead.")
        return redirect("seller_earnings")


@seller_required(roles=MONEY_ROLES)
def stripe_return(request):
    from . import payouts as auto
    seller = request.seller
    if auto.refresh_stripe_account(seller):
        type(seller).objects.filter(pk=seller.pk).update(payout_method="stripe")
        messages.success(request, "Stripe is connected. Your earnings will be paid to your bank through Stripe.")
    else:
        messages.info(request, "Stripe still needs a few details before it can pay you. Press 'Continue Stripe setup' to finish.")
    return redirect("seller_earnings")


@seller_required(roles=MONEY_ROLES)
@require_POST
def cancel_payout_request(request, pk):
    payout = get_object_or_404(Payout, pk=pk, seller=request.seller, status="requested")
    payout.status = "cancelled"
    payout.save(update_fields=["status"])
    from .manage_views import _refresh_attention
    _refresh_attention()
    messages.success(request, "Payout request cancelled.")
    return redirect("seller_earnings")


# ---------------------------------------------------------------------------
# Returns, reviews and questions
# ---------------------------------------------------------------------------

@seller_required()
def returns(request):
    qs = ReturnRequest.objects.filter(order_item__seller_account=request.seller).select_related("order_item__order", "order_item__product")
    status = request.GET.get("status", "")
    counts = {s: qs.filter(status=s).count() for s, _ in ReturnRequest.STATUS_CHOICES}
    if status in dict(ReturnRequest.STATUS_CHOICES):
        qs = qs.filter(status=status)
    return _render(request, "returns.html", {
        "section": "returns", "page_obj": _page(request, qs), "status": status,
        "tabs": [(s, label, counts[s]) for s, label in ReturnRequest.STATUS_CHOICES],
        "total": sum(counts.values()),
    })


@seller_required()
def reviews(request):
    seller = request.seller
    tab = request.GET.get("tab", "questions")
    product_reviews = Review.objects.filter(product__seller_account=seller).select_related("product")
    agg = product_reviews.aggregate(avg=Avg("rating"), n=Count("id"))
    breakdown = {r["rating"]: r["n"] for r in product_reviews.values("rating").annotate(n=Count("id"))}
    total = agg["n"] or 0
    stars = [{"stars": s, "n": breakdown.get(s, 0), "pct": round(breakdown.get(s, 0) / total * 100) if total else 0} for s in (5, 4, 3, 2, 1)]
    questions = Question.objects.filter(product__seller_account=seller).select_related("product")
    open_q = questions.filter(answer="").order_by("created_at")
    if tab == "reviews":
        page_obj = _page(request, product_reviews)
    elif tab == "store":
        page_obj = _page(request, SellerReview.objects.filter(seller=seller).select_related("user"))
    elif tab == "answered":
        page_obj = _page(request, questions.exclude(answer="").order_by("-created_at"))
    else:
        tab = "questions"
        page_obj = _page(request, open_q)
    return _render(request, "reviews.html", {
        "section": "reviews", "tab": tab, "page_obj": page_obj, "agg": agg, "stars": stars,
        "store_rating": seller.average_rating, "store_count": seller.seller_reviews.count(),
        "open_count": open_q.count(),
    })


# ---------------------------------------------------------------------------
# Team
# ---------------------------------------------------------------------------

@seller_required(roles=MONEY_ROLES)
def team(request):
    if request.seller.account_type != "organization":
        messages.info(request, "Teams are for organization accounts.")
        return redirect("seller_dashboard")
    return _render(request, "team.html", {
        "section": "team", "members": request.seller.team_members.select_related("user"),
    })


@seller_required(roles=MONEY_ROLES)
@require_POST
def vacation(request):
    seller = request.seller
    turn_on = request.POST.get("vacation_mode") == "on"
    seller.vacation_mode = turn_on
    seller.vacation_message = request.POST.get("vacation_message", "").strip()[:200] if turn_on else seller.vacation_message
    seller.save(update_fields=["vacation_mode", "vacation_message"])
    from django.core.cache import cache
    cache.delete("home:category_tiles")
    AuditLog.objects.create(user=request.user, action=f"Seller {seller.display_name} holiday mode {'on' if turn_on else 'off'}")
    if turn_on:
        messages.success(request, "Holiday mode is on: your products are hidden from the shop until you switch it off.")
    else:
        messages.success(request, "Welcome back! Your products are visible in the shop again.")
    return _back(request, "seller_store_settings")


# ---------------------------------------------------------------------------
# Seller plans
# ---------------------------------------------------------------------------

PLAN_MONTH_CHOICES = (1, 3, 6, 12)


@seller_required(roles=MONEY_ROLES)
def plan(request):
    from . import payments
    from .models import PlanPayment, SellerPlan
    seller = request.seller
    plans = list(SellerPlan.objects.filter(active=True))
    current = seller.active_plan
    used = seller.products.count()
    limit = seller.product_limit
    return _render(request, "plan.html", {
        "section": "plan", "plans": plans, "current": current,
        "paid_until": seller.plan_expires_at if current and not current.is_free else None,
        "used": used, "limit": limit,
        "used_pct": min(round(used / limit * 100), 100) if limit else 0,
        "rate": seller.effective_commission_rate,
        "history": PlanPayment.objects.filter(seller=seller).exclude(status="pending")[:12],
        "card_ready": payments.is_configured(),
        "month_choices": PLAN_MONTH_CHOICES,
    })


@seller_required(roles=MONEY_ROLES)
@require_POST
def buy_plan(request, pk):
    from . import payments
    from .models import PlanPayment, SellerPlan
    seller = request.seller
    chosen = get_object_or_404(SellerPlan, pk=pk, active=True)
    if chosen.is_free:
        current = seller.active_plan
        if current and not current.is_free:
            messages.info(request, f"You're on {current.name} until {seller.plan_expires_at:%b %d, %Y}. "
                                   f"You'll move to {chosen.name} automatically when it ends.")
            return redirect("seller_plan")
        seller.plan, seller.plan_expires_at = chosen, None
        seller.save(update_fields=["plan", "plan_expires_at"])
        messages.success(request, f"You're on the {chosen.name} plan.")
        return redirect("seller_plan")
    try:
        months = int(request.POST.get("months", "1"))
    except ValueError:
        months = 1
    if months not in PLAN_MONTH_CHOICES:
        months = 1
    if not payments.is_configured():
        messages.error(request, "Card payments aren't switched on for this store yet. Please contact the store team to upgrade.")
        return redirect("seller_plan")
    payment = PlanPayment.objects.create(
        seller=seller, plan=chosen, plan_name=chosen.name, months=months,
        amount=chosen.price * months, created_by=request.user,
    )
    try:
        url = payments.create_plan_checkout(request, payment)
    except payments.PaymentError as exc:
        payment.status = "failed"
        payment.save(update_fields=["status"])
        messages.error(request, str(exc))
        return redirect("seller_plan")
    return redirect(url)


@seller_required(roles=MONEY_ROLES)
def plan_done(request):
    """Stripe sends the seller back here after paying. The webhook also
    activates the plan; whichever comes first wins."""
    from . import payments
    from .models import PlanPayment
    session_id = request.GET.get("session_id", "")
    payment = PlanPayment.objects.filter(seller=request.seller, stripe_session_id=session_id).first() if session_id else None
    if payment and payment.status != "paid":
        session = payments.retrieve_session(session_id)
        if session:
            payments.confirm_plan_payment(payment.id, session)
            payment.refresh_from_db()
    if payment and payment.status == "paid":
        request.seller.refresh_from_db()
        messages.success(request, f"Thank you! Your {payment.plan_name} plan is active until {request.seller.plan_expires_at:%b %d, %Y}.")
    elif payment:
        messages.info(request, "We're waiting for Stripe to confirm your payment. Your plan switches on as soon as it does.")
    return redirect("seller_plan")


# ---------------------------------------------------------------------------
# Messages from shoppers
# ---------------------------------------------------------------------------

@seller_required()
def messages_inbox(request):
    from .models import Conversation
    from .messaging import with_preview
    convs = with_preview(Conversation.objects.filter(seller=request.seller).select_related("buyer").exclude(last_message_at=None))
    return _render(request, "messages.html", {"section": "messages", "conversations": convs, "conv": None})


@seller_required()
def conversation(request, pk):
    from . import messaging
    from .models import Conversation
    conv = get_object_or_404(Conversation.objects.select_related("buyer", "seller"), pk=pk, seller=request.seller)
    messaging.mark_read(conv, "seller")
    orders = (OrderItem.objects.filter(seller_account=request.seller, order__user=conv.buyer)
              .values("order_id").distinct().count())
    convs = messaging.with_preview(Conversation.objects.filter(seller=request.seller).select_related("buyer").exclude(last_message_at=None))
    return _render(request, "messages.html", {
        "section": "messages", "conversations": convs, "conv": conv,
        "thread": messaging.serialize(conv.messages.select_related("product"), "seller"),
        "buyer_orders": orders,
    })
