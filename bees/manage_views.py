"""
Store admin ("/manage/"): the owner's day-to-day back office, built with the
same design system as the storefront. The full Django admin stays available
at /admin/ for anything not covered here.
"""
import os
import csv
from datetime import date, timedelta
from decimal import Decimal
from functools import wraps
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, F, Q, Sum, Exists, OuterRef
from django.db.models.functions import TruncDate
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import badges as seller_badges
from . import payments
from .models import (
    AuditLog, ChatMessage, ChatThread, Claim, Conversation, Coupon, Currency, Notification, Order, OrderItem,
    Payout, Product, Profile, Question, ReturnRequest, Review, SellerAccount, SellerPlan, ShippingZone, SiteSettings,
)
from .templatetags.bees_extras import money
from .security import safe_next_url
from .seller_views import PLAN_MONTH_CHOICES
from .categories import choices as category_choices


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def staff_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"{reverse('login')}?{urlencode({'next': request.get_full_path()})}")
        if not request.user.is_staff:
            return HttpResponseForbidden("Store staff only.")
        return view(request, *args, **kwargs)
    return wrapped


def _open_listing_reports():
    from .models import ListingReport
    return ListingReport.objects.filter(status="open").count()


def attention_counts(force=False):
    """Numbers shown as badges in the admin sidebar and header icon.
    Cached for 30 seconds so they don't add queries to every page."""
    data = None if force else cache.get("manage:attention")
    if data is None:
        unread_user_msgs = ChatMessage.objects.filter(thread=OuterRef("pk"), sender="user", is_read=False)
        data = {
            "orders": Order.objects.filter(status__in=["pending", "confirmed"]).exclude(payment_status__in=["pending", "failed"]).count(),
            "products": Product.objects.filter(approval_status="pending").count() + _open_listing_reports(),
            "sellers": SellerAccount.objects.filter(status="pending").count() + Payout.objects.filter(status="requested").count(),
            "returns": ReturnRequest.objects.filter(status="requested").count(),
            "questions": Question.objects.filter(answer="").count(),
            "messages": ChatThread.objects.filter(is_resolved=False).filter(Exists(unread_user_msgs)).count(),
            "reported": Conversation.objects.filter(reported=True).count(),
            "claims": Claim.objects.filter(status="escalated").count(),
        }
        data["messages"] += data["reported"]
        data["total"] = data["orders"] + data["products"] + data["sellers"] + data["returns"] + data["messages"] + data["claims"]
        cache.set("manage:attention", data, 30)
    return data


def _refresh_attention():
    cache.delete("manage:attention")


def _log(request, action):
    AuditLog.objects.create(user=request.user, action=action[:255])


def _page(request, qs, per_page=25):
    return Paginator(qs, per_page).get_page(request.GET.get("page"))


def _render(request, template, context):
    from . import staff_roles
    context.setdefault("attention", attention_counts())
    context.setdefault("staff_areas", staff_roles.areas(request.user))
    return render(request, f"bees/manage/{template}", context)


def _back(request, fallback):
    return redirect(safe_next_url(request, request.POST.get("next"), fallback))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def _auto_backup():
    """Safety net when the PythonAnywhere daily task isn't set up: the first
    time staff open the admin each day, make a backup if the newest one is
    more than a day old. Never breaks the page."""
    from django.conf import settings as dj
    if not getattr(dj, "AUTO_BACKUP", False) or cache.get("manage:auto_backup"):
        return
    cache.set("manage:auto_backup", True, 6 * 3600)
    try:
        from datetime import datetime
        from django.core.management import call_command
        from .management.commands.backup_data import list_backups
        newest = list_backups()[:1]
        if newest:
            stamp = datetime.strptime(newest[0][7:20], "%Y%m%d-%H%M")
            if (timezone.now().replace(tzinfo=None) - stamp) < timedelta(hours=24):
                return
        call_command("backup_data")
    except Exception as exc:
        from .alerts import log
        log(f"Automatic backup failed: {str(exc)[:200]}")


@staff_required
def dashboard(request):
    from django.conf import settings as dj
    setup_pending = request.user.is_superuser and not SiteSettings.load().setup_completed
    if setup_pending and getattr(dj, "SETUP_WIZARD_REDIRECT", False) and not request.session.get("setup_seen"):
        request.session["setup_seen"] = True
        return redirect("manage_setup")
    _auto_backup()
    today = timezone.localdate()
    days = 30 if request.GET.get("range") == "30" else 7
    start = today - timedelta(days=days - 1)
    counted = Order.objects.exclude(status="cancelled").exclude(payment_status__in=["pending", "failed", "refunded"])

    items_by_day = {
        r["day"]: r["v"] or 0
        for r in counted.filter(created_at__date__gte=start).annotate(day=TruncDate("created_at"))
        .values("day").annotate(v=Sum(F("items__price") * F("items__quantity")))
    }
    extra_by_day = {
        r["day"]: (r["extra"] or 0) - (r["disc"] or 0)
        for r in counted.filter(created_at__date__gte=start).annotate(day=TruncDate("created_at"))
        .values("day").annotate(extra=Sum(F("shipping_amount") + F("tax_amount")), disc=Sum("discount_amount"))
    }
    orders_by_day = {
        r["day"]: r["n"]
        for r in counted.filter(created_at__date__gte=start).annotate(day=TruncDate("created_at"))
        .values("day").annotate(n=Count("id"))
    }
    series = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        amount = max(float(items_by_day.get(d, 0)) + float(extra_by_day.get(d, 0)), 0)
        series.append({"date": d, "amount": amount, "orders": orders_by_day.get(d, 0)})
    peak = max([s["amount"] for s in series] or [0]) or 1
    for s in series:
        s["pct"] = round(s["amount"] / peak * 100, 1)
    period_revenue = sum(s["amount"] for s in series)
    period_orders = sum(s["orders"] for s in series)

    prev_start = start - timedelta(days=days)
    prev = counted.filter(created_at__date__gte=prev_start, created_at__date__lt=start)
    prev_items = prev.aggregate(v=Sum(F("items__price") * F("items__quantity")))["v"] or 0
    prev_extra = prev.aggregate(e=Sum(F("shipping_amount") + F("tax_amount")), d=Sum("discount_amount"))
    prev_revenue = max(float(prev_items) + float(prev_extra["e"] or 0) - float(prev_extra["d"] or 0), 0)
    change = None
    if prev_revenue:
        change = round((period_revenue - prev_revenue) / prev_revenue * 100)

    commission = (
        OrderItem.objects.counted().filter(order__created_at__date__gte=start)
        .aggregate(c=Sum("commission_amount"))["c"] or 0
    )

    top_products = (
        OrderItem.objects.filter(order__in=counted.filter(created_at__date__gte=start))
        .values("product_name").annotate(units=Sum("quantity"), revenue=Sum(F("price") * F("quantity")))
        .order_by("-revenue")[:5]
    )
    recent_orders = Order.objects.select_related("user").prefetch_related("items").order_by("-created_at")[:8]
    low_stock = Product.objects.filter(stock__lte=5).order_by("stock", "name")[:8]

    return _render(request, "dashboard.html", {
        "section": "dashboard",
        "setup_pending": setup_pending,
        "health": quick_health(),
        "days": days,
        "series": series,
        "period_revenue": period_revenue,
        "period_orders": period_orders,
        "avg_order": (period_revenue / period_orders) if period_orders else 0,
        "commission": commission,
        "change": change,
        "customers": User.objects.filter(is_staff=False).count(),
        "new_customers": User.objects.filter(is_staff=False, date_joined__date__gte=start).count(),
        "top_products": top_products,
        "recent_orders": recent_orders,
        "low_stock": low_stock,
        "attention": attention_counts(force=True),
    })


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def _filtered_orders(request):
    qs = Order.objects.select_related("user").prefetch_related("items").order_by("-created_at")
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    payment = request.GET.get("payment", "")
    if q:
        cond = Q(full_name__icontains=q) | Q(email__icontains=q) | Q(user__email__icontains=q) | Q(user__username__icontains=q) | Q(phone__icontains=q) | Q(tracking_number__icontains=q)
        if q.lstrip("#").isdigit():
            cond |= Q(pk=int(q.lstrip("#")))
        qs = qs.filter(cond)
    if status == "open":
        qs = qs.filter(status__in=["pending", "confirmed"]).exclude(payment_status__in=["pending", "failed"])
    elif status in dict(Order.STATUS_CHOICES):
        qs = qs.filter(status=status)
    if payment in dict(Order.PAYMENT_STATUS_CHOICES):
        qs = qs.filter(payment_status=payment)
    return qs, q, status, payment


@staff_required
def orders(request):
    qs, q, status, payment = _filtered_orders(request)
    if request.GET.get("export") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="orders.csv"'
        w = csv.writer(response)
        w.writerow(["order", "date", "customer", "email", "country", "status", "payment", "method", "items", "total"])
        for o in qs[:5000]:
            w.writerow([o.id, o.created_at.strftime("%Y-%m-%d %H:%M"), o.full_name, o.contact_email, o.country,
                        o.status, o.payment_status, o.payment_method, sum(i.quantity for i in o.items.all()), f"{o.total:.2f}"])
        return response
    return _render(request, "orders.html", {
        "section": "orders", "page_obj": _page(request, qs), "q": q, "status": status, "payment": payment,
        "status_choices": Order.STATUS_CHOICES, "payment_choices": Order.PAYMENT_STATUS_CHOICES,
    })


@staff_required
def order_detail(request, pk):
    order = get_object_or_404(Order.objects.select_related("user"), pk=pk)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "cancel" and order.status == "delivered":
            messages.error(request, "Delivered orders can't be cancelled. Refund items from Returns instead.")
        elif action == "cancel":
            from .admin import cancel_order_with_side_effects
            error = cancel_order_with_side_effects(order, request)
            if error:
                messages.error(request, error)
            else:
                _log(request, f"Cancelled order #{order.id} from store admin")
                messages.success(request, f"Order #{order.id} cancelled. Stock returned" + (" and payment refunded." if order.payment_status == "refunded" else "."))
        elif action == "cod_refused":
            from . import cod
            if order.payment_method != "cod" or order.cod_collected_at or order.status == "delivered":
                messages.error(request, "Only cash on delivery orders that weren't paid can be marked as refused.")
            elif cod.mark_refused(order, user=request.user):
                blocked = cod.blocked(order.contact_email, order.phone)
                messages.success(request, f"Order #{order.id} marked as refused and cancelled. Stock returned."
                                 + (" This customer can now only pay online." if blocked else ""))
        elif action == "item_status":
            from . import fulfilment
            item = get_object_or_404(order.items, pk=request.POST.get("item"))
            try:
                changed, moved = fulfilment.set_items_status([item], request.POST.get("fulfillment_status", ""), user=request.user, by_staff=True)
            except fulfilment.FulfilmentError as exc:
                messages.error(request, str(exc))
            else:
                if changed:
                    messages.success(request, f"'{item.product_name}' updated." + (f" The order is now {moved[0][1]} and the customer has been told." if moved else ""))
        elif action == "update":
            new_status = request.POST.get("status", order.status)
            if new_status not in dict(Order.STATUS_CHOICES) or new_status == "cancelled":
                new_status = order.status
            was = order.status
            if was == "delivered" and new_status != "delivered":
                messages.error(request, "A delivered order can't go back to an earlier step. Use Returns if the customer sends something back.")
                return redirect("manage_order", pk=pk)
            if new_status in ("shipped", "delivered") and new_status != was:
                from .fulfilment import blocked_reason
                reason = blocked_reason(order)
                if reason:
                    messages.error(request, reason)
                    return redirect("manage_order", pk=pk)
            order.tracking_number = request.POST.get("tracking_number", "").strip()[:60]
            order.courier_name = request.POST.get("courier_name", "").strip()[:60]
            turl = request.POST.get("tracking_url", "").strip()[:300]
            order.tracking_url = turl if turl.startswith("https://") else ""
            eta = request.POST.get("estimated_delivery", "").strip()
            try:
                order.estimated_delivery = date.fromisoformat(eta) if eta else None
            except ValueError:
                messages.error(request, "Estimated delivery must be a valid date.")
                return redirect("manage_order", pk=pk)
            if new_status in ("confirmed", "shipped") and not order.estimated_delivery:
                from .order_emails import default_delivery_date
                order.estimated_delivery = default_delivery_date(order)
            order.status = new_status
            order.save()  # notifies + emails the customer when the status changes
            if new_status != was:
                _log(request, f"Order #{order.id}: {was} -> {new_status}")
                if new_status == "shipped":
                    order.items.exclude(fulfillment_status="delivered").update(fulfillment_status="handed_to_courier", shipped_at=timezone.now())
                if new_status == "delivered":
                    order.items.filter(shipped_at=None).update(shipped_at=timezone.now())
                    order.items.exclude(fulfillment_status="delivered").update(fulfillment_status="delivered", delivered_at=timezone.now())
            emailed = new_status != was and new_status in ("confirmed", "shipped", "delivered") and order.contact_email
            messages.success(request, "Order updated." + (" The customer has been emailed." if emailed else ""))
        _refresh_attention()
        return redirect("manage_order", pk=pk)
    items = order.items.select_related("product", "seller_account__user")
    from . import couriers
    from .models import Shipment
    provider = couriers.active()
    shipments = list(Shipment.objects.filter(order=order))
    quote = next((s for s in shipments if s.status == "rated" and str(s.id) == request.GET.get("shipment", "")), None)
    pending = [i for i in items if i.fulfillment_status in ("pending", "packed")]
    choices = []
    for i in pending:
        value = str(i.seller_account_id) if i.seller_account_id else "store"
        label = i.seller_account.display_name if i.seller_account_id else "Your store's items"
        if (value, label) not in choices:
            choices.append((value, label))
    return _render(request, "order_detail.html", {
        "courier_on": provider is not None, "courier_name": getattr(provider, "name", ""), "shipments": shipments, "quote": quote,
        "can_book": bool(pending) and order.status != "cancelled", "seller_choices": choices,
        "section": "orders", "order": order, "items": items,
        "status_choices": [c for c in Order.STATUS_CHOICES if c[0] != "cancelled"],
        "fulfilment_choices": OrderItem.FULFILLMENT_CHOICES,
        "returns": ReturnRequest.objects.filter(order_item__order=order).select_related("order_item"),
        "stripe_test": str(getattr(payments.settings, "STRIPE_SECRET_KEY", "")).startswith("sk_test"),
    })


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

@staff_required
def products(request):
    qs = Product.objects.select_related("seller_account").order_by("-id")
    q = request.GET.get("q", "").strip()
    category = request.GET.get("category", "")
    state = request.GET.get("state", "")
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(seller_name__icontains=q))
    if category in dict(category_choices()):
        from .categories import with_children
        qs = qs.filter(category__in=with_children(category))
    if state in ("pending", "approved", "rejected"):
        qs = qs.filter(approval_status=state)
    elif state == "low":
        qs = qs.filter(stock__gt=0, stock__lte=5)
    elif state == "out":
        qs = qs.filter(stock__lte=0)
    elif state == "deals":
        qs = qs.filter(is_flash_sale=True)
    return _render(request, "products.html", {
        "section": "products", "page_obj": _page(request, qs), "q": q, "category": category, "state": state,
        "categories": category_choices(),
    })


def _notify_seller(product, message):
    if product.seller_account_id:
        Notification.objects.create(user=product.seller_account.user, message=message[:255], link="/seller/products/")


@staff_required
@require_POST
def products_bulk(request):
    ids = [int(i) for i in request.POST.getlist("ids") if i.isdigit()]
    action = request.POST.get("action")
    qs = Product.objects.filter(pk__in=ids).select_related("seller_account")
    if not ids:
        messages.warning(request, "Select at least one product first.")
        return _back(request, reverse("manage_products"))
    count = qs.count()
    if action in ("approve", "reject"):
        status = "approved" if action == "approve" else "rejected"
        for p in qs:
            if p.approval_status != status:
                p.approval_status = status
                p.save(update_fields=["approval_status"])
                _notify_seller(p, f"'{p.name}' was {'approved and is now live' if status == 'approved' else 'not approved'}.")
        messages.success(request, f"{count} product(s) {'approved' if status == 'approved' else 'rejected'}.")
    elif action in ("deal_on", "deal_off"):
        qs.update(is_flash_sale=(action == "deal_on"))
        messages.success(request, f"{count} product(s) {'added to' if action == 'deal_on' else 'removed from'} today's deals.")
    elif action == "delete":
        names = ", ".join(qs.values_list("name", flat=True)[:5])
        qs.delete()
        messages.success(request, f"Deleted {count} product(s).")
        _log(request, f"Deleted products: {names}")
    else:
        messages.error(request, "Choose an action.")
    _log(request, f"Bulk {action} on {count} product(s)")
    _refresh_attention()
    return _back(request, reverse("manage_products"))


@staff_required
def product_form(request, pk=None):
    from .views import (_product_fields_from_post, check_digital, parse_variant_rows, apply_variant_rows, variant_rows_for_form,
                        gallery_from_post, save_gallery)
    product = get_object_or_404(Product, pk=pk) if pk else None
    if request.method == "POST":
        try:
            variant_rows = parse_variant_rows(request)
            gallery = gallery_from_post(request, product)
            fields = _product_fields_from_post(request)
            check_digital(fields, product)
            if not fields["image_url"]:
                if product:
                    fields["image_url"] = product.image_url
                else:
                    raise ValidationError("Please add a product image (upload a file or paste an https:// link).")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return _render(request, "product_form.html", {
                "section": "products", "product": product, "form": request.POST, "categories": category_choices(),
                "approval_choices": Product.APPROVAL_CHOICES, "variant_rows": variant_rows_for_form(request, product),
            })
        approval = request.POST.get("approval_status", "approved")
        if approval not in dict(Product.APPROVAL_CHOICES):
            approval = "approved"
        is_new = product is None
        old_approval = product.approval_status if product else None
        product = product or Product()
        for key, value in fields.items():
            setattr(product, key, value)
        product.seller_name = (request.POST.get("seller_name", "").strip() or product.seller_name or "Official Store")[:100]
        product.is_flash_sale = bool(request.POST.get("is_flash_sale"))
        ends = request.POST.get("flash_sale_ends", "").strip()
        product.flash_sale_ends = None
        if ends and product.is_flash_sale:
            from datetime import datetime
            try:
                product.flash_sale_ends = timezone.make_aware(datetime.fromisoformat(ends))
            except ValueError:
                messages.error(request, "Deal end time wasn't a valid date and time, so the deal has no end date.")
        product.approval_status = approval
        with transaction.atomic():
            product.save()
            apply_variant_rows(product, variant_rows)
            save_gallery(product, *gallery)
        if old_approval and old_approval != approval and approval in ("approved", "rejected"):
            _notify_seller(product, f"'{product.name}' was {'approved and is now live' if approval == 'approved' else 'not approved'}.")
        _log(request, f"{'Created' if is_new else 'Edited'} product #{product.id} ({product.name})")
        _refresh_attention()
        messages.success(request, "Product saved." if not is_new else "Product created.")
        return redirect("manage_product_edit", pk=product.pk)
    return _render(request, "product_form.html", {
        "section": "products", "product": product, "categories": category_choices(),
        "approval_choices": Product.APPROVAL_CHOICES,
        "variant_rows": variant_rows_for_form(request, product),
    })


# ---------------------------------------------------------------------------
# Sellers
# ---------------------------------------------------------------------------

@staff_required
def sellers(request):
    if request.method == "POST" and request.POST.get("action") == "pay_all":
        from . import payouts as auto
        sent, total, errors = auto.pay_all(user=request.user)
        _log(request, f"Paid all sellers: {sent} payouts, {money(total)}")
        if sent:
            messages.success(request, f"Sent {sent} payout{'s' if sent != 1 else ''} ({money(total)}).")
        elif not errors:
            messages.info(request, "Nobody to pay right now: no seller with PayPal/Stripe payouts has an available balance above the minimum.")
        for e in errors[:5]:
            messages.error(request, e)
        return redirect("manage_sellers")
    qs = SellerAccount.objects.select_related("user").annotate(product_count=Count("products")).order_by("-created_at")
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    if q:
        qs = qs.filter(Q(business_name__icontains=q) | Q(organization_name__icontains=q) | Q(full_name__icontains=q) | Q(user__username__icontains=q) | Q(user__email__icontains=q))
    if status in dict(SellerAccount.STATUS_CHOICES):
        qs = qs.filter(status=status)
    elif status == "payout":
        qs = qs.filter(payouts__status="requested").distinct()
    return _render(request, "sellers.html", {
        "payout_requests": Payout.objects.filter(status="requested").count(),
        "auto_sellers": SellerAccount.objects.filter(payout_method__in=["paypal", "stripe"]).count(),
        "pending_count": SellerAccount.objects.filter(status="pending").count(),
        "section": "sellers", "page_obj": _page(request, qs), "q": q, "status": status, "status_choices": SellerAccount.STATUS_CHOICES,
    })


@staff_required
def seller_detail(request, pk):
    seller = get_object_or_404(SellerAccount.objects.select_related("user"), pk=pk)
    if request.method == "POST":
        action = request.POST.get("action")
        note = request.POST.get("note", "").strip()[:255]
        if action in ("approved", "rejected", "suspended"):
            seller.status = action
            if note:
                seller.admin_note = note
            seller.save(update_fields=["status", "admin_note"])
            text = {
                "approved": "Your seller account has been approved! You can now list products.",
                "rejected": "Your seller application was not approved." + (f" {note}" if note else ""),
                "suspended": "Your seller account has been suspended." + (f" {note}" if note else ""),
            }[action]
            Notification.objects.create(user=seller.user, message=text[:255], link="/seller/dashboard/")
            _log(request, f"Seller #{seller.id} ({seller.display_name}) -> {action}")
            messages.success(request, f"{seller.display_name} is now {seller.get_status_display().lower()}.")
        elif action == "commission":
            try:
                rate = Decimal(request.POST.get("commission_rate", ""))
                if not (0 <= rate <= 90):
                    raise ValueError
            except Exception:
                messages.error(request, "Commission must be a number between 0 and 90.")
                return redirect("manage_seller", pk=pk)
            seller.commission_rate = rate
            seller.save(update_fields=["commission_rate"])
            _log(request, f"Seller #{seller.id} commission set to {rate}%")
            messages.success(request, "Commission rate saved.")
        elif action in ("verify", "unverify"):
            seller.verified_at = timezone.now() if action == "verify" else None
            seller.save(update_fields=["verified_at"])
            if action == "verify":
                Notification.objects.create(user=seller.user, link="/seller/dashboard/",
                                            message="Your store is now a Verified seller. The badge shows on your store and products.")
            _log(request, f"Seller #{seller.id} {'verified' if action == 'verify' else 'verification removed'}")
            messages.success(request, f"{seller.display_name} is {'now verified' if action == 'verify' else 'no longer verified'}.")
        elif action == "grant_plan":
            from .models import PlanPayment
            plan = SellerPlan.objects.filter(pk=request.POST.get("plan"), price__gt=0).first()
            try:
                months = int(request.POST.get("months", "1"))
            except ValueError:
                months = 0
            if not plan or months not in PLAN_MONTH_CHOICES:
                messages.error(request, "Choose a plan and how many months.")
                return redirect("manage_seller", pk=pk)
            free = bool(request.POST.get("free"))
            payment = PlanPayment.objects.create(
                seller=seller, plan=plan, plan_name=plan.name, months=months, method="manual",
                amount=Decimal("0") if free else plan.price * months, created_by=request.user,
            )
            payment.activate()
            seller.refresh_from_db()
            _log(request, f"Gave seller #{seller.id} the {plan.name} plan for {months} month(s){' free' if free else ''}")
            messages.success(request, f"{seller.display_name} is on {plan.name} until {seller.plan_expires_at:%b %d, %Y}.")
        elif action == "payout":
            try:
                amount = Decimal(request.POST.get("amount", "")).quantize(Decimal("0.01"))
                if amount <= 0:
                    raise ValueError
            except Exception:
                messages.error(request, "Enter the payout amount as a positive number.")
                return redirect("manage_seller", pk=pk)
            method = request.POST.get("method", "")
            if method not in dict(Payout.METHOD_CHOICES):
                method = "bank"
            with transaction.atomic():
                seller = SellerAccount.objects.select_for_update().get(pk=seller.pk)
                payout = seller.payouts.filter(status="requested").first() or Payout(seller=seller)
                payout.amount = amount
                payout.status = "paid"
                payout.method = method
                payout.reference = request.POST.get("reference", "").strip()[:120]
                payout.note = note
                payout.recorded_by = request.user
                payout.paid_at = timezone.now()
                payout.save()
                seller.total_paid_out = (seller.total_paid_out or 0) + amount
                seller.save(update_fields=["total_paid_out"])
            Notification.objects.create(user=seller.user, message=f"A payout of {money(amount)} has been sent to you ({payout.get_method_display()}).", link="/seller/earnings/")
            from .views import _send_html_email
            transaction.on_commit(lambda: _send_html_email(
                f"{SiteSettings.load().site_name}: payout of {money(amount)} sent", "bees/emails/seller_payout.html",
                {"seller": seller, "payout": payout, "url": request.build_absolute_uri(reverse("seller_earnings"))},
                seller.user.email,
            ))
            _log(request, f"Recorded payout of {money(amount)} to seller #{seller.id}")
            messages.success(request, f"Recorded a payout of {money(amount)}.")
        elif action == "auto_payout":
            from . import payouts as auto
            try:
                payout = auto.send(seller, user=request.user, request_payout=seller.payouts.filter(status="requested").first())
            except auto.PayoutError as exc:
                messages.error(request, str(exc))
            else:
                _log(request, f"Sent automatic payout of {money(payout.amount)} to seller #{seller.id} ({payout.method})")
                messages.success(request, f"{money(payout.amount)} {'sent' if payout.status == 'paid' else 'is on its way'} to {seller.display_name}.")
        elif action == "decline_payout":
            payout = seller.payouts.filter(status="requested").first()
            if payout:
                payout.status = "cancelled"
                payout.note = note or "Declined by the store"
                payout.recorded_by = request.user
                payout.save(update_fields=["status", "note", "recorded_by"])
                Notification.objects.create(user=seller.user, link="/seller/earnings/",
                                            message=f"Your payout request of {money(payout.amount)} was declined. {payout.note}"[:255])
                _log(request, f"Declined payout request #{payout.pk} from seller #{seller.id}")
                messages.success(request, "Payout request declined and the seller was told.")
        _refresh_attention()
        return redirect("manage_seller", pk=pk)
    products_qs = seller.products.order_by("-id")[:12]
    from .seller_center import balance
    from .performance import measure
    return _render(request, "seller_detail.html", {
        "section": "sellers", "seller": seller, "products": products_qs,
        "perf": measure(seller) if seller.status == "approved" else None,
        "product_count": seller.products.count(),
        "payouts": seller.payouts.select_related("recorded_by")[:20],
        "payout_request": seller.payouts.filter(status="requested").first(),
        "auto_ready": _auto_ready(seller),
        "balance": balance(seller), "method_choices": Payout.METHOD_CHOICES,
        "badge_rules": seller_badges.rules(),
        "paid_plans": SellerPlan.objects.filter(active=True, price__gt=0),
        "month_choices": PLAN_MONTH_CHOICES,
    })


# ---------------------------------------------------------------------------
# Seller plans
# ---------------------------------------------------------------------------

class SellerPlanForm(forms.ModelForm):
    class Meta:
        model = SellerPlan
        fields = ["name", "price", "commission_discount", "product_limit", "badge", "highlight", "position", "active", "perks"]
        widgets = {"perks": forms.Textarea(attrs={"rows": 4})}

    def clean_commission_discount(self):
        value = self.cleaned_data["commission_discount"]
        if value < 0 or value > 50:
            raise ValidationError("Use a number between 0 and 50.")
        return value

    def clean_price(self):
        value = self.cleaned_data["price"]
        if value < 0:
            raise ValidationError("Price can't be negative.")
        return value


@staff_required
def plans(request):
    edit = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "delete":
            plan = get_object_or_404(SellerPlan, pk=request.POST.get("id"))
            if plan.sellers.exists():
                plan.active = False
                plan.save(update_fields=["active"])
                messages.info(request, f"{plan.name} is in use, so it was hidden instead of deleted. Sellers on it keep it until it ends.")
            else:
                _log(request, f"Deleted seller plan {plan.name}")
                plan.delete()
                messages.success(request, "Plan deleted.")
            return redirect("manage_plans")
        instance = get_object_or_404(SellerPlan, pk=request.POST.get("id")) if request.POST.get("id") else None
        form = SellerPlanForm(request.POST, instance=instance)
        if form.is_valid():
            plan = form.save()
            _log(request, f"{'Updated' if instance else 'Created'} seller plan {plan.name}")
            messages.success(request, f"{plan.name} saved.")
            return redirect("manage_plans")
        messages.error(request, "Please fix the plan details below.")
        edit = form
    elif request.GET.get("edit"):
        edit = SellerPlanForm(instance=get_object_or_404(SellerPlan, pk=request.GET["edit"]))
    elif request.GET.get("new"):
        edit = SellerPlanForm(initial={"position": SellerPlan.objects.count() + 1, "active": True})

    from .models import PlanPayment
    now = timezone.now()
    rows = list(SellerPlan.objects.all())
    free = SellerPlan.free_plan()
    counts = {}
    for plan_id, price, active, expires in SellerAccount.objects.values_list("plan_id", "plan__price", "plan__active", "plan_expires_at"):
        current = plan_id if plan_id and active and (price <= 0 or (expires and expires > now)) else (free.id if free else None)
        counts[current] = counts.get(current, 0) + 1
    for p in rows:
        p.seller_count = counts.get(p.id, 0)
    paid = PlanPayment.objects.filter(status="paid")
    stats = {
        "month": paid.filter(paid_at__gte=now - timedelta(days=30)).aggregate(v=Sum("amount"))["v"] or 0,
        "total": paid.aggregate(v=Sum("amount"))["v"] or 0,
        "paying": SellerAccount.objects.filter(plan__price__gt=0, plan_expires_at__gt=now).count(),
    }
    return _render(request, "plans.html", {
        "section": "plans", "plans": rows, "edit": edit, "stats": stats,
        "payments": PlanPayment.objects.select_related("seller").exclude(status="pending")[:25],
    })


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

@staff_required
def customers(request):
    qs = User.objects.annotate(order_count=Count("orders", distinct=True)).order_by("-date_joined")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(username__icontains=q) | Q(email__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q))
    page_obj = _page(request, qs)
    users = list(page_obj.object_list)
    spend = {
        r["order__user"]: r["v"]
        for r in OrderItem.objects.filter(order__user__in=users).exclude(order__status="cancelled")
        .exclude(order__payment_status__in=["pending", "failed"])
        .values("order__user").annotate(v=Sum(F("price") * F("quantity")))
    }
    sellers_ids = set(SellerAccount.objects.filter(user__in=users).values_list("user_id", flat=True))
    for u in users:
        u.spend = spend.get(u.pk, 0)
        u.is_seller = u.pk in sellers_ids
    return _render(request, "customers.html", {"section": "customers", "page_obj": page_obj, "users": users, "q": q})


# ---------------------------------------------------------------------------
# Coupons
# ---------------------------------------------------------------------------

class CouponForm(forms.ModelForm):
    class Meta:
        model = Coupon
        fields = ["code", "percent_off", "min_order_value", "expiry_date", "usage_limit", "per_user_limit", "active"]
        widgets = {"expiry_date": forms.DateInput(attrs={"type": "date"})}

    def clean_code(self):
        return self.cleaned_data["code"].strip().upper()

    def clean_percent_off(self):
        v = self.cleaned_data["percent_off"]
        if not 1 <= v <= 100:
            raise ValidationError("Use a value from 1 to 100.")
        return v


@staff_required
def coupons(request):
    form = CouponForm(request.POST or None, initial={"percent_off": 10, "per_user_limit": 1, "active": True})
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create":
            if form.is_valid():
                c = form.save()
                _log(request, f"Created coupon {c.code}")
                messages.success(request, f"Coupon {c.code} created.")
                return redirect("manage_coupons")
            messages.error(request, "Please fix the coupon details below.")
        elif action in ("toggle", "delete"):
            c = get_object_or_404(Coupon, pk=request.POST.get("id"))
            if action == "toggle":
                c.active = not c.active
                c.save(update_fields=["active"])
                messages.success(request, f"{c.code} is now {'active' if c.active else 'paused'}.")
            else:
                _log(request, f"Deleted coupon {c.code}")
                c.delete()
                messages.success(request, "Coupon deleted.")
            return redirect("manage_coupons")
    rows = list(Coupon.objects.order_by("-id"))
    usage = dict(
        Order.objects.exclude(status="cancelled").exclude(coupon_code="")
        .values_list("coupon_code").annotate(n=Count("id"))
    )
    usage = {k.upper(): v for k, v in usage.items()}
    for c in rows:
        c.used = usage.get(c.code.upper(), 0)
    return _render(request, "coupons.html", {"section": "coupons", "coupons": rows, "form": form})


# ---------------------------------------------------------------------------
# System check
# ---------------------------------------------------------------------------

def _stripe_error_text(exc):
    return payments.explain_error(exc)


@staff_required
def system_check(request):
    from django.conf import settings as dj
    from django.core.mail import EmailMultiAlternatives
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    from .alerts import EMAIL_FAILED, PAYMENT_FAILED, PAYMENT_OK, REFUND_FAILED, STRIPE_EVENT, WEBHOOK_REJECTED
    from .management.commands.backup_data import list_backups

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "email":
            to = request.POST.get("to", "").strip() or request.user.email
            try:
                msg = EmailMultiAlternatives(f"Test email from {SiteSettings.load().site_name}",
                                             "If you can read this, your store can send emails.", None, [to])
                msg.send(fail_silently=False)
                if dj.EMAIL_BACKEND.endswith("console.EmailBackend"):
                    messages.error(request, "Email isn't set up: messages are only printed to the server log. Add EMAIL_HOST_USER and EMAIL_HOST_PASSWORD (Gmail app password) to .env and reload.")
                else:
                    messages.success(request, f"Test email sent to {to}. Check the inbox (and spam folder).")
            except Exception as exc:
                hint = ""
                if "535" in str(exc) or "Username and Password not accepted" in str(exc):
                    hint = " Gmail refused the login: use a 16-letter App Password (not your normal password) and make sure 2-Step Verification is on."
                messages.error(request, f"Sending failed: {type(exc).__name__}: {str(exc)[:300]}.{hint}")
        elif action == "stripe":
            if not payments.is_configured():
                messages.error(request, "STRIPE_SECRET_KEY isn't set in .env.")
            else:
                try:
                    payments._stripe().Balance.retrieve()
                    from .alerts import payments_working
                    payments_working("connection test passed")
                    messages.success(request, "Stripe connection works" + (" through the Supabase relay." if dj.STRIPE_API_BASE else "."))
                except Exception as exc:
                    messages.error(request, _stripe_error_text(exc))
        elif action == "stripe_full":
            if not payments.is_configured():
                messages.error(request, "STRIPE_SECRET_KEY isn't set in .env.")
            else:
                steps = payments.self_test(request)
                request.session["stripe_test"] = [[n, ok, d] for n, ok, d in steps]
                if all(ok for _, ok, _ in steps):
                    messages.success(request, "Full payment test passed: every Stripe step works.")
                else:
                    messages.error(request, "The payment test found a problem. See the results below.")
                _log(request, "Ran the full payment test")
        elif action in ("backup", "daily"):
            from io import StringIO
            from django.core.management import call_command
            out = StringIO()
            try:
                call_command("backup_data" if action == "backup" else "daily_tasks", stdout=out, stderr=out)
                messages.success(request, "Backup saved." if action == "backup" else "Daily tasks finished: unpaid orders released, cart reminders sent, backup saved.")
                _log(request, "Ran " + ("a backup" if action == "backup" else "the daily tasks") + " from System check")
            except (Exception, SystemExit) as exc:
                messages.error(request, f"That didn't finish: {str(exc)[:200] or out.getvalue()[-300:]}")
            cache.delete("manage:auto_backup")
        elif action == "storage":
            from django.core.files.base import ContentFile
            from django.core.files.storage import default_storage
            try:
                name = default_storage.save("healthcheck/test.txt", ContentFile(b"ok"))
                default_storage.delete(name)
                messages.success(request, "Image storage works (" + ("Supabase Storage" if dj.USE_SUPABASE_STORAGE else "this server's disk") + ").")
            except Exception as exc:
                messages.error(request, f"Image storage failed: {str(exc)[:300]}")
        return redirect("manage_system")

    checks = []

    def add(group, name, ok, detail, level=None):
        checks.append({"group": group, "name": name, "level": level or ("ok" if ok else "bad"), "detail": detail})

    # Database
    db = dj.DATABASES["default"]
    engine = "SQLite file on this server" if db["ENGINE"].endswith("sqlite3") else "PostgreSQL (Supabase)" if "postgres" in db["ENGINE"] else db["ENGINE"]
    try:
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        add("Database", "Connection", True, f"{engine}. {Order.objects.count()} orders, {Product.objects.count()} products, {User.objects.count()} accounts.")
        add("Database", "Up to date", not pending, "All updates applied." if not pending else f"{len(pending)} update(s) not applied. Run: python manage.py migrate")
    except Exception as exc:
        add("Database", "Connection", False, str(exc)[:300])

    # Email
    console = dj.EMAIL_BACKEND.endswith("console.EmailBackend")
    add("Email", "Sending", not console,
        "Not set up - customers get NO emails. Add EMAIL_HOST_USER and EMAIL_HOST_PASSWORD to .env." if console
        else f"Sending through {getattr(dj, 'EMAIL_HOST', '')} as {dj.DEFAULT_FROM_EMAIL}.")
    week_ago = timezone.now() - timedelta(days=7)
    failures = AuditLog.objects.filter(action__startswith=EMAIL_FAILED, created_at__gte=week_ago)
    last_fail = failures.order_by("-created_at").first()
    add("Email", "Failures (7 days)", not failures.exists(),
        "None." if not last_fail else f"{failures.count()} failed. Latest: {last_fail.action[len(EMAIL_FAILED) + 2:][:220]}",
        level=None if not last_fail else "bad")

    # Payments
    stripe_on = payments.is_configured()
    key = getattr(dj, "STRIPE_SECRET_KEY", "")
    add("Payments", "Stripe", stripe_on,
        ("Live mode - real cards are charged." if key.startswith(("sk_live", "rk_live")) else "Test mode - use card 4242 4242 4242 4242.") if stripe_on
        else "Not set up - only cash on delivery is offered.", level=None if stripe_on else "warn")
    if stripe_on:
        add("Payments", "Webhook secret", payments.webhook_configured(),
            "Set." if payments.webhook_configured() else "STRIPE_WEBHOOK_SECRET missing - paid orders won't be confirmed automatically.")
        last = SiteSettings.objects.filter(pk=1).values_list("stripe_last_webhook", flat=True).first()
        rejected = AuditLog.objects.filter(action__startswith=WEBHOOK_REJECTED).order_by("-created_at").first()
        hook_url = request.build_absolute_uri(reverse("stripe_webhook")).replace("http://", "https://")
        events = "checkout.session.completed, checkout.session.async_payment_succeeded, checkout.session.async_payment_failed, checkout.session.expired, charge.refunded, charge.dispute.created"
        if rejected and (not last or rejected.created_at > last):
            add("Payments", "Last message from Stripe", False,
                f"Stripe is reaching your site ({timezone.localtime(rejected.created_at):%b %d, %H:%M}) but the webhook secret doesn't match. "
                "In Stripe > Developers > Webhooks, open the endpoint, reveal its Signing secret (whsec_...) and put it in "
                "STRIPE_WEBHOOK_SECRET in .env, then reload the web app.")
        elif last:
            add("Payments", "Last message from Stripe", True, timezone.localtime(last).strftime("%b %d, %Y %H:%M"))
        else:
            add("Payments", "Last message from Stripe", False,
                f"None yet. Payments still work (the order is confirmed when the customer returns), but Stripe should also notify the site. "
                f"In Stripe > Developers > Webhooks add the endpoint {hook_url} with these events: {events}. "
                "Copy its Signing secret into STRIPE_WEBHOOK_SECRET, reload, then make a test payment.", level="warn")
        add("Payments", "Route", True, "Through the Supabase relay (PythonAnywhere free)." if dj.STRIPE_API_BASE else "Direct to api.stripe.com.", level="ok")
    stuck = Order.objects.filter(payment_status="pending", created_at__lt=timezone.now() - timedelta(hours=2)).count()
    add("Payments", "Unpaid card orders older than 2 hours", stuck == 0,
        "None." if not stuck else f"{stuck} - the daily task or Stripe's 'expired' webhook releases them.", level=None if not stuck else "warn")
    pay_fail = AuditLog.objects.filter(action__startswith=PAYMENT_FAILED, created_at__gte=week_ago).order_by("-created_at")

    def _open_failures():
        # Errors from before payments last worked have already been fixed.
        worked = [t for t in (
            AuditLog.objects.filter(action__startswith=PAYMENT_OK).order_by("-created_at").values_list("created_at", flat=True).first(),
            SiteSettings.objects.filter(pk=1).values_list("stripe_last_webhook", flat=True).first(),
        ) if t]
        when = max(worked) if worked else None
        return (pay_fail.filter(created_at__gt=when) if when else pay_fail), when

    open_fail, last_worked = _open_failures()
    if stripe_on and open_fail.exists() and not cache.get("manage:stripe_autocheck"):
        # Re-test the connection once (cached 5 minutes): if Stripe works now,
        # the old errors were already fixed.
        cache.set("manage:stripe_autocheck", True, 300)
        try:
            payments._stripe().Balance.retrieve()
            from .alerts import payments_working
            payments_working("connection test passed (automatic)")
            open_fail, last_worked = _open_failures()
        except Exception:
            pass
    latest_pay_fail = open_fail.first()
    if latest_pay_fail is None:
        earlier = pay_fail.count()
        detail = "None." if not earlier else (
            f"None since payments last worked ({timezone.localtime(last_worked):%b %d, %H:%M}). "
            f"{earlier} earlier error{'s were' if earlier != 1 else ' was'} already fixed.")
    else:
        recent = "; ".join(
            f"{timezone.localtime(a.created_at):%b %d %H:%M}: {a.action.split(' - ', 1)[-1][:160]}" for a in open_fail[:3])
        detail = f"{open_fail.count()}. Latest: {recent}. Press 'Full payment test' above to see which step fails."
    add("Payments", "Payment page errors (7 days)", latest_pay_fail is None, detail)
    refund_fail = AuditLog.objects.filter(action__startswith=REFUND_FAILED, created_at__gte=week_ago)
    add("Payments", "Failed refunds (7 days)", not refund_fail.exists(),
        "None." if not refund_fail.exists() else "; ".join(a.action[len(REFUND_FAILED) + 2:][:120] for a in refund_fail[:3]))
    stripe_events = AuditLog.objects.filter(action__startswith=STRIPE_EVENT, created_at__gte=week_ago)
    if stripe_events.exists():
        add("Payments", "Stripe notices (7 days)", False, "; ".join(a.action[len(STRIPE_EVENT) + 2:][:120] for a in stripe_events[:3]), level="warn")

    # Files & backups
    add("Files & backups", "Image storage", True, "Supabase Storage." if dj.USE_SUPABASE_STORAGE else "This server's disk.", level="ok")
    try:
        backups = list_backups()
    except Exception as exc:
        backups = []
        add("Files & backups", "Backups", False, f"Couldn't list backups: {str(exc)[:200]}")
    else:
        if backups:
            from datetime import datetime
            stamp = datetime.strptime(backups[0][7:20], "%Y%m%d-%H%M")
            age = (timezone.now().replace(tzinfo=None) - stamp).days
            add("Files & backups", "Backups", age <= 2, f"{len(backups)} kept. Newest: {stamp:%b %d, %Y %H:%M} UTC." + (" Older than 2 days - is the daily task running?" if age > 2 else ""))
        else:
            add("Files & backups", "Backups", False, "No backups yet. Press 'Back up now' above. For automatic daily backups, add the daily task on PythonAnywhere (Tasks tab): cd ~/Lumen-Market && venv/bin/python manage.py daily_tasks")

    # Security
    add("Security", "Debug mode", not dj.DEBUG, "Off." if not dj.DEBUG else "ON - turn DEBUG off in .env on the live site.")
    weak_key = dj.SECRET_KEY.startswith("django-insecure") or len(dj.SECRET_KEY) < 40
    add("Security", "Secret key", not weak_key, "Strong." if not weak_key else "Weak or default SECRET_KEY - set a long random one in .env.")
    add("Security", "HTTPS", dj.DEBUG or getattr(dj, "SECURE_SSL_REDIRECT", False),
        "Visitors are always sent to https://." if getattr(dj, "SECURE_SSL_REDIRECT", False) else "Not forced - set SECURE_SSL_REDIRECT=True in .env.",
        level=None if (dj.DEBUG or getattr(dj, "SECURE_SSL_REDIRECT", False)) else "warn")
    add("Security", "Allowed hosts", "*" not in dj.ALLOWED_HOSTS, ", ".join(dj.ALLOWED_HOSTS)[:200] if "*" not in dj.ALLOWED_HOSTS else "'*' allows any domain - list your domain(s) in ALLOWED_HOSTS.")
    if getattr(dj, "DEMO_MODE", False):
        add("Security", "Demo mode", False, "DEMO_MODE is on: anyone can sign in as the demo admin. Only use it on a separate demo copy.", level="warn")
    staff = User.objects.filter(is_staff=True, is_active=True)
    without = [u.username for u in staff if not getattr(getattr(u, "profile", None), "totp_enabled", False)]
    import ipaddress
    remote = request.META.get("REMOTE_ADDR", "")
    try:
        private = ipaddress.ip_address(remote).is_private or ipaddress.ip_address(remote).is_loopback
    except ValueError:
        private = False
    if private and request.META.get("HTTP_X_FORWARDED_FOR") and not getattr(dj, "NUM_PROXIES", 0):
        add("Security", "Visitor addresses", False, "Every visitor looks like the same address (the hosting's proxy), so sign-in and checkout limits "
            "would block everyone at once. Set NUM_PROXIES=1 in the .env file and reload.")
    add("Security", "Staff two-step sign-in", not without, "All staff use it." if not without else f"Not set up: {', '.join(without[:5])}.",
        level=None if not without else "warn")

    for c in operations_checks(dj):
        checks.append(c)

    groups = {}
    for c in checks:
        groups.setdefault(c["group"], []).append(c)
    stripe_test = request.session.pop("stripe_test", None)
    return _render(request, "system.html", {
        "stripe_test": stripe_test,
        "section": "system", "groups": groups,
        "problems": sum(1 for c in checks if c["level"] == "bad"),
        "warnings": sum(1 for c in checks if c["level"] == "warn"),
    })


# ---------------------------------------------------------------------------
# Shipping zones
# ---------------------------------------------------------------------------

EU = "AT, BE, BG, HR, CY, CZ, DK, EE, FI, FR, DE, GR, HU, IE, IT, LV, LT, LU, MT, NL, PL, PT, RO, SK, SI, ES, SE"


class ShippingZoneForm(forms.ModelForm):
    class Meta:
        model = ShippingZone
        fields = ["name", "countries", "courier", "fee", "per_item_fee", "free_over", "delivery_days", "active"]
        widgets = {"countries": forms.Textarea(attrs={"rows": 3, "placeholder": "US, CA  — or * for all other countries"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["per_item_fee"].required = False

    def clean_per_item_fee(self):
        value = self.cleaned_data.get("per_item_fee") or Decimal("0")
        if value < 0:
            raise ValidationError("Can't be negative.")
        return value

    def clean_countries(self):
        from .countries import COUNTRIES
        raw = self.cleaned_data["countries"].strip()
        if raw == "*":
            return raw
        valid = {code for code, _ in COUNTRIES}
        codes = [c.strip().upper() for c in raw.replace("\n", ",").split(",") if c.strip()]
        bad = [c for c in codes if c not in valid]
        if not codes:
            raise ValidationError("Add at least one country code, or * for every other country.")
        if bad:
            raise ValidationError(f"Unknown country code(s): {', '.join(bad)}. Use two-letter codes like US, GB, DE.")
        return ", ".join(dict.fromkeys(codes))


@staff_required
def shipping(request):
    zone = get_object_or_404(ShippingZone, pk=request.GET["edit"]) if request.GET.get("edit") else None
    form = ShippingZoneForm(request.POST or None, instance=zone, initial=None if zone else {"active": True})
    if request.method == "POST":
        action = request.POST.get("action", "save")
        if action == "delete":
            z = get_object_or_404(ShippingZone, pk=request.POST.get("id"))
            _log(request, f"Deleted shipping zone {z.name}")
            z.delete()
            messages.success(request, "Shipping zone deleted.")
            return redirect("manage_shipping")
        if action == "starter":
            if not ShippingZone.objects.exists():
                ShippingZone.objects.bulk_create([
                    ShippingZone(name="United States", countries="US", fee=Decimal("5.00"), free_over=Decimal("50"), delivery_days=5),
                    ShippingZone(name="Europe", countries=EU + ", GB, CH, NO", fee=Decimal("12.00"), free_over=Decimal("100"), delivery_days=8),
                    ShippingZone(name="Rest of world", countries="*", fee=Decimal("20.00"), delivery_days=12),
                ])
                from .shipping import clear_cache
                clear_cache()
                messages.success(request, "Starter zones added. Adjust the fees to match your courier.")
            return redirect("manage_shipping")
        if form.is_valid():
            z = form.save()
            _log(request, f"Saved shipping zone {z.name}")
            messages.success(request, f"Shipping zone '{z.name}' saved.")
            return redirect("manage_shipping")
        messages.error(request, "Please fix the details below.")
    zones = list(ShippingZone.objects.all())
    claimed = {}
    for z in zones:
        for code in z.country_codes:
            claimed.setdefault(code, []).append(z.name)
    duplicates = {c: n for c, n in claimed.items() if len(n) > 1}
    return _render(request, "shipping.html", {
        "section": "shipping", "zones": zones, "form": form, "editing": zone,
        "has_rest": any(z.is_rest_of_world and z.active for z in zones), "duplicates": duplicates,
        "brand": SiteSettings.load(),
    })


# ---------------------------------------------------------------------------
# Reviews & questions
# ---------------------------------------------------------------------------

@staff_required
def reviews(request):
    tab = "questions" if request.GET.get("tab") == "questions" else "reviews"
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "delete_review":
            r = get_object_or_404(Review, pk=request.POST.get("id"))
            _log(request, f"Deleted review #{r.id} on {r.product.name}")
            r.delete()
            messages.success(request, "Review deleted.")
        elif action == "answer":
            qn = get_object_or_404(Question.objects.select_related("product"), pk=request.POST.get("id"))
            had_answer = bool(qn.answer)
            qn.answer = request.POST.get("answer", "").strip()[:2000]
            qn.save(update_fields=["answer"])
            if qn.answer and not had_answer:
                from .views import notify_question_answered
                notify_question_answered(qn)
            messages.success(request, "Answer published." if qn.answer else "Answer removed.")
        elif action == "delete_question":
            get_object_or_404(Question, pk=request.POST.get("id")).delete()
            messages.success(request, "Question deleted.")
        _refresh_attention()
        return redirect(f"{reverse('manage_reviews')}?tab={tab}")
    if tab == "questions":
        qs = sorted(
            Question.objects.select_related("product").order_by("-created_at")[:300],
            key=lambda x: bool(x.answer),  # unanswered first, newest first within each group
        )
        page_obj = Paginator(qs, 25).get_page(request.GET.get("page"))
    else:
        rating = request.GET.get("rating", "")
        qs = Review.objects.select_related("product").order_by("-created_at")
        if rating.isdigit():
            qs = qs.filter(rating=int(rating))
        page_obj = _page(request, qs)
    return _render(request, "reviews.html", {"section": "reviews", "tab": tab, "page_obj": page_obj, "rating": request.GET.get("rating", "")})


# ---------------------------------------------------------------------------
# Returns
# ---------------------------------------------------------------------------

@staff_required
def returns(request):
    if request.method == "POST":
        rr = get_object_or_404(ReturnRequest.objects.select_related("order_item__order"), pk=request.POST.get("id"))
        action = request.POST.get("action")
        note = request.POST.get("note", "").strip()[:255]
        if note:
            rr.admin_note = note
        if action in ("approve", "approve_label"):
            rr.status = "approved"
            rr.save()
            messages.success(request, "Return approved. The customer has been notified.")
        if action in ("approve_label", "return_label"):
            from . import couriers
            try:
                label = couriers.return_label(rr, user=request.user)
            except couriers.CourierError as exc:
                messages.error(request, f"Couldn't book the return label: {exc}")
            else:
                messages.success(request, f"Prepaid return label emailed to the customer ({label.carrier} {label.tracking_number}"
                                 + (f", {label.cost} {label.cost_currency}" if label.cost else "") + ").")
        elif action == "reject":
            rr.status = "rejected"
            rr.save()
            messages.success(request, "Return rejected. The customer has been notified.")
        elif action in ("refund", "refund_credit"):
            from .refunds import RefundError, describe, refund_return
            try:
                plan = refund_return(rr, as_credit=action == "refund_credit", note=note)
            except RefundError as exc:
                messages.error(request, str(exc))
                return redirect("manage_returns")
            messages.success(request, "Refunded: " + describe(plan) + ". Stock returned.")
        _log(request, f"Return #{rr.id}: {action}")
        _refresh_attention()
        return redirect("manage_returns")
    status = request.GET.get("status", "requested")
    qs = ReturnRequest.objects.select_related("order_item", "order_item__order", "user").order_by("-created_at")
    if status in dict(ReturnRequest.STATUS_CHOICES):
        qs = qs.filter(status=status)
    page_obj = _page(request, qs)
    for r in page_obj:
        if r.status in ("requested", "approved"):
            r.plan = r.refund_plan()
    from . import couriers
    return _render(request, "returns.html", {"section": "returns", "page_obj": page_obj, "status": status, "status_choices": ReturnRequest.STATUS_CHOICES,
                                             "courier_on": couriers.active() is not None})


# ---------------------------------------------------------------------------
# Tax rates
# ---------------------------------------------------------------------------

@staff_required
def tax_rates(request):
    from .countries import COUNTRIES
    from .models import TaxRate
    from .taxes import PRESETS
    names = dict(COUNTRIES)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "presets":
            added = 0
            for code, name, rate in PRESETS:
                _obj, created = TaxRate.objects.get_or_create(country=code, defaults={"name": name, "rate": Decimal(rate)})
                added += int(created)
            _log(request, f"Added {added} preset tax rates")
            messages.success(request, f"Added {added} common rates. Please check them with your accountant - rates change.")
        elif action == "save":
            code = request.POST.get("country", "").strip().upper()
            try:
                rate = Decimal(request.POST.get("rate", "").strip())
            except Exception:
                rate = None
            if code not in names:
                messages.error(request, "Choose a country.")
            elif rate is None or rate < 0 or rate > 50:
                messages.error(request, "Use a rate between 0 and 50%.")
            else:
                TaxRate.objects.update_or_create(country=code, defaults={
                    "name": (request.POST.get("name", "").strip() or "VAT")[:20], "rate": rate,
                    "includes_shipping": bool(request.POST.get("includes_shipping")), "active": True})
                _log(request, f"Tax rate {code} = {rate}%")
                messages.success(request, f"{names[code]}: {rate}% saved.")
        elif action in ("toggle", "delete"):
            obj = get_object_or_404(TaxRate, pk=request.POST.get("id"))
            if action == "delete":
                obj.delete()
            else:
                obj.active = not obj.active
                obj.save(update_fields=["active"])
            _log(request, f"Tax rate {obj.country}: {action}")
        return redirect("manage_taxes")
    rates = [(t, names.get(t.country, t.country)) for t in TaxRate.objects.all()]
    return _render(request, "taxes.html", {"section": "settings", "rates": rates, "countries": COUNTRIES,
                                           "site": SiteSettings.load()})


# ---------------------------------------------------------------------------
# Keys & passwords (.env editor)
# ---------------------------------------------------------------------------

@staff_required
def keys(request):
    import secrets as _secrets
    from django.conf import settings as dj
    from django.urls import reverse as _reverse
    from . import envfile
    if not request.user.is_superuser:
        messages.error(request, "Only the store owner (superuser) can change keys and passwords.")
        return redirect("manage_settings")
    if getattr(dj, "DEMO_MODE", False):
        messages.info(request, "Keys can't be changed on the demo.")
        return redirect("manage_settings")
    if request.method == "POST":
        if not request.user.check_password(request.POST.get("confirm_password", "")):
            messages.error(request, "Your password wasn't right, so nothing was changed.")
            return redirect("manage_keys")
        changes = {}
        for key in envfile.EDITABLE:
            if request.POST.get(f"remove_{key}"):
                changes[key] = None
                continue
            value = request.POST.get(key, "").strip()
            if key == "APPLE_PRIVATE_KEY":  # multi-line key stored on one line
                value = "\\n".join(line.strip() for line in value.replace("\r", "").split("\n") if line.strip())
            if value:
                changes[key] = value
        if request.POST.get("new_webhook_token"):
            changes["SHIPPING_WEBHOOK_TOKEN"] = _secrets.token_urlsafe(24)
        if request.POST.get("new_push_keys"):
            from .webpush import generate_keys
            changes["VAPID_PUBLIC_KEY"], changes["VAPID_PRIVATE_KEY"] = generate_keys()
        if not changes:
            messages.info(request, "Nothing to change.")
            return redirect("manage_keys")
        try:
            envfile.write(changes)
        except envfile.EnvError as exc:
            messages.error(request, str(exc))
            return redirect("manage_keys")
        except OSError as exc:
            messages.error(request, f"Couldn't save the .env file: {exc}")
            return redirect("manage_keys")
        _log(request, "Keys changed: " + ", ".join(sorted(changes)))
        if envfile.reload_app():
            messages.success(request, f"Saved {len(changes)} setting(s). The site is restarting to use them - refresh in a few seconds.")
        else:
            messages.success(request, f"Saved {len(changes)} setting(s). Now press Reload on your hosting's Web tab so the site uses them.")
        return redirect("manage_keys")
    current = envfile.read()
    groups = []
    for title, hint, rows in envfile.GROUPS:
        groups.append((title, hint, [{"key": key, "label": label, "secret": secret, "set": bool(current.get(key)),
                                      "shown": envfile.mask(current.get(key, ""), secret)} for key, label, secret, _p in rows]))
    paypal_webhook = request.build_absolute_uri(_reverse("paypal_webhook"))
    social_redirects = {"google": request.build_absolute_uri(_reverse("social_callback", args=["google"])),
                        "apple": request.build_absolute_uri(_reverse("social_callback", args=["apple"]))}
    token = current.get("SHIPPING_WEBHOOK_TOKEN", "")
    webhook = request.build_absolute_uri(_reverse("courier_webhook", args=[token])) if token else ""
    return _render(request, "keys.html", {
        "section": "keys", "groups": groups, "webhook_url": webhook, "paypal_webhook": paypal_webhook, "social_redirects": social_redirects, "push_ready": bool(current.get("VAPID_PRIVATE_KEY")), "env_exists": os.path.exists(envfile.path()),
        "locked": [(k, bool(current.get(k))) for k in envfile.LOCKED],
    })


# ---------------------------------------------------------------------------
# Problem reports (claims)
# ---------------------------------------------------------------------------

@staff_required
def claims(request):
    show = request.GET.get("show", "escalated")
    qs = Claim.objects.select_related("order_item__order", "order_item__seller_account", "user")
    tabs = [("escalated", "Needs a decision"), ("waiting", "Waiting for seller"), ("closed", "Closed"), ("all", "All")]
    if show == "escalated":
        qs = qs.filter(status="escalated")
    elif show == "waiting":
        qs = qs.filter(status__in=["open", "seller_replied"])
    elif show == "closed":
        qs = qs.exclude(status__in=Claim.OPEN_STATUSES)
    return _render(request, "claims.html", {"section": "claims", "page_obj": _page(request, qs), "show": show, "tabs": tabs})


@staff_required
def claim_detail(request, pk):
    claim = get_object_or_404(Claim.objects.select_related("order_item__order", "order_item__seller_account", "user"), pk=pk)
    if request.method == "POST":
        from .claims import decide
        from .refunds import RefundError
        action = request.POST.get("action")
        if not claim.is_open:
            messages.error(request, "This report is already closed.")
        elif action in ("refund", "reject"):
            try:
                result = decide(claim, action, request.user, note=request.POST.get("note", "").strip(),
                                as_credit=request.POST.get("as_credit") == "1", restock=request.POST.get("restock") == "1")
            except RefundError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, result)
        _refresh_attention()
        return redirect("manage_claim", pk=pk)
    plan = None
    if claim.is_open:
        plan = ReturnRequest(order_item=claim.order_item, user=claim.user).refund_plan()
    return _render(request, "claim_detail.html", {"section": "claims", "claim": claim, "plan": plan,
                                                  "events": claim.order_item.order.events.all()})


# ---------------------------------------------------------------------------
# Support messages
# ---------------------------------------------------------------------------

@staff_required
def support(request, pk=None):
    thread = get_object_or_404(ChatThread.objects.select_related("user"), pk=pk) if pk else None
    if request.method == "POST" and thread:
        action = request.POST.get("action")
        if action == "reply":
            text = request.POST.get("message", "").strip()[:2000]
            if text:
                # is_read on a team reply = the customer has seen it.
                ChatMessage.objects.create(thread=thread, sender="support", message=text, is_read=False)
                thread.is_resolved = False
                thread.save(update_fields=["is_resolved"])
                if thread.user_id:
                    Notification.objects.create(user=thread.user, message="Our team replied to your message. Open the chat to read it.", link="/?chat=1")
                    if thread.user.email:
                        from .views import _send_html_email
                        from .order_emails import absolute
                        _send_html_email("You have a reply from our support team", "bees/emails/support_reply.html",
                                         {"user": thread.user, "reply": text, "url": absolute("/?chat=1")}, thread.user.email)
                messages.success(request, "Reply sent.")
        elif action in ("resolve", "reopen"):
            thread.is_resolved = action == "resolve"
            thread.save(update_fields=["is_resolved"])
            messages.success(request, "Conversation closed." if thread.is_resolved else "Conversation reopened.")
        _refresh_attention()
        return redirect("manage_support_thread", pk=thread.pk)

    show = request.GET.get("show", "open")
    threads = ChatThread.objects.select_related("user").annotate(
        msg_count=Count("messages"),
        unread=Count("messages", filter=Q(messages__sender="user", messages__is_read=False)),
    ).filter(msg_count__gt=0)
    threads = threads.filter(is_resolved=(show == "closed")).order_by("-unread", "-created_at")[:100]
    last = {}
    for m in ChatMessage.objects.filter(thread__in=[t.pk for t in threads]).order_by("thread_id", "-created_at"):
        last.setdefault(m.thread_id, m)
    for t in threads:
        t.last = last.get(t.pk)
    chat = []
    if thread:
        chat = list(thread.messages.order_by("created_at"))
        thread.messages.filter(sender="user", is_read=False).update(is_read=True)
        _refresh_attention()
    return _render(request, "support.html", {"section": "support", "threads": threads, "thread": thread, "chat": chat, "show": show})


# ---------------------------------------------------------------------------
# Store settings
# ---------------------------------------------------------------------------

class SiteSettingsForm(forms.ModelForm):
    class Meta:
        model = SiteSettings
        exclude = []
        widgets = {
            "primary_color": forms.TextInput(attrs={"type": "color"}),
            "accent_color": forms.TextInput(attrs={"type": "color"}),
            "hero_subtitle": forms.Textarea(attrs={"rows": 3}),
        }

    GROUPS = [
        ("Brand", "Your store's name, logo and colours. Changes show on every page.",
         ["site_name", "tagline", "logo_file", "logo_url", "favicon_url", "primary_color", "accent_color"]),
        ("Homepage", "The large banner at the top of the homepage.", ["hero_title", "hero_subtitle", "hero_image_url"]),
        ("Announcement bar", "A message across the top of every page, e.g. a sale.", ["banner_active", "banner_text", "banner_link"]),
        ("Selling", "Commission taken from each sale. Applies to sellers who join from now on; change an existing seller's rate on their page.",
         ["commission_individual", "commission_organization"]),
        ("Shipping & payment", "Applied at checkout.", ["shipping_flat_fee", "free_shipping_threshold", "delivery_days", "return_days", "allow_cash_on_delivery", "allow_guest_checkout"]),
        ("Privacy, analytics & search", "Analytics and ad pixels only load after a visitor accepts cookies. Verify your site in Google Search Console with its meta-tag code.",
         ["cookie_banner", "ga4_id", "meta_pixel_id", "google_site_verification"]),
        ("Taxes & duties", "Set rates per country under Tax rates. The default below is used for every other country.",
         ["prices_include_tax", "tax_percent", "store_tax_id", "duties_policy"]),
                ("Shipping labels", "Book couriers and print labels from the order page. Pick a service, then add its API key under Keys & passwords. The address below is used for the store's own products; sellers set theirs in Seller Center.",
         ["shipping_provider", "ship_from_name", "ship_from_street", "ship_from_city", "ship_from_state", "ship_from_postal_code", "ship_from_country", "ship_from_phone", "default_weight_grams"]),
                ("Order rules", "Automatic steps that keep orders moving and protect your money. Runs with the daily tasks.",
         ["auto_cancel_days", "auto_deliver_days", "claim_reply_days"]),
                ("Contact & social", "Shown in the footer, emails and help page.",
         ["support_email", "support_phone", "company_address", "facebook_url", "instagram_url", "twitter_url", "youtube_url"]),
        ("Seller payouts", "Sellers choose PayPal or Stripe in Seller Center > Earnings. Only money past the return window is sent. Bank-transfer sellers are still paid by hand.",
         ["auto_payouts", "min_auto_payout"]),
        ("Sponsored products", "Sellers pay per click to show products at the top of search, category pages and the homepage. Manage under Sponsored products.",
         ["ads_enabled", "ads_min_bid", "ads_slots"]),
        ("Gift cards", "Sell digital gift cards on /gift-cards/. Manage and send free ones under Gift cards.", ["gift_cards_enabled"]),
        ("Referral program", "Customers invite friends with their own link. Discount codes are created automatically.",
         ["referral_enabled", "referral_friend_percent", "referral_reward_percent"]),
        ("Cash on delivery", "Stops fake cash orders. Blocked customers and refused parcels: Manage > Cash on delivery protection.",
         ["cod_phone_check", "cod_max_order", "cod_refusal_limit"]),
        ("Text messages (SMS)", "Sent to mobile numbers through Twilio (add its keys under Keys & passwords). Customers can turn them off on their Privacy page.",
         ["sms_customer_updates", "sms_seller_alerts"]),
        ("Security", "Protects the admin and sellers' earnings even if a password is stolen.", ["require_staff_2fa", "require_seller_2fa"]),
        ("Language & currency", "Show a menu where shoppers pick their language (English, Arabic, Spanish, French, Urdu) and currency. Add currencies under Store settings > Currencies.", ["show_language_menu", "charge_local_currency"]),
    ]

    def groups(self):
        return [(title, hint, [self[name] for name in names]) for title, hint, names in self.GROUPS]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.OPTIONAL:
            if name in self.fields:
                self.fields[name].required = False  # left out = keep the current value

    OPTIONAL = ("commission_individual", "commission_organization", "referral_friend_percent", "referral_reward_percent",
                "auto_cancel_days", "auto_deliver_days", "claim_reply_days", "default_weight_grams", "duties_policy",
                "cod_max_order", "cod_refusal_limit", "min_auto_payout", "auto_payouts", "ads_min_bid", "ads_slots")

    def _clean_id(self, name, pattern, example):
        import re as _re
        value = (self.cleaned_data.get(name) or "").strip()
        if value and not _re.fullmatch(pattern, value):
            raise ValidationError(f"That doesn't look right - it should look like {example}.")
        return value

    def clean_ga4_id(self):
        return self._clean_id("ga4_id", r"G-[A-Z0-9]{4,15}", "G-ABC123XYZ")

    def clean_meta_pixel_id(self):
        return self._clean_id("meta_pixel_id", r"\d{6,20}", "1234567890")

    def clean_google_site_verification(self):
        return self._clean_id("google_site_verification", r"[A-Za-z0-9_-]{10,100}", "the code inside content=\"...\"")

    def clean_duties_policy(self):
        return self.cleaned_data.get("duties_policy") or self.instance.duties_policy or "customer"

    def clean_default_weight_grams(self):
        return self._keep("default_weight_grams") or 500

    def clean_ship_from_country(self):
        value = (self.cleaned_data.get("ship_from_country") or "").strip().upper()
        if value and (len(value) != 2 or not value.isalpha()):
            raise ValidationError("Use the 2-letter country code, e.g. PK, AE, GB, US.")
        return value

    def _keep(self, name):
        value = self.cleaned_data.get(name)
        return getattr(self.instance, name) if value is None else value

    def clean_ads_min_bid(self):
        return self._keep("ads_min_bid")

    def clean_ads_slots(self):
        value = self._keep("ads_slots")
        if value is not None and value > 6:
            raise ValidationError("Use 0 to 6.")
        return value

    def clean_min_auto_payout(self):
        return self._keep("min_auto_payout")

    def clean_auto_payouts(self):
        return self.cleaned_data.get("auto_payouts") or self.instance.auto_payouts or "off"

    def clean_cod_max_order(self):
        return self._keep("cod_max_order")

    def clean_cod_refusal_limit(self):
        return self._keep("cod_refusal_limit")

    def clean_auto_cancel_days(self):
        return self._keep("auto_cancel_days")

    def clean_auto_deliver_days(self):
        value = self._keep("auto_deliver_days")
        if value and value < 3:
            raise ValidationError("Use at least 3 days (or 0 to turn it off).")
        return value

    def clean_claim_reply_days(self):
        return self._keep("claim_reply_days") or 1

    def _clean_percent(self, name):
        value = self.cleaned_data.get(name)
        if value is None:
            return getattr(self.instance, name)
        if value > 90:
            raise ValidationError("Use a number between 0 and 90.")
        return value

    def clean_referral_friend_percent(self):
        return self._clean_percent("referral_friend_percent")

    def clean_referral_reward_percent(self):
        return self._clean_percent("referral_reward_percent")

    def _clean_rate(self, name):
        value = self.cleaned_data[name]
        if value is None:
            return getattr(self.instance, name)
        if value is None or value < 0 or value > 90:
            raise ValidationError("Use a number between 0 and 90.")
        return value

    def clean_commission_individual(self):
        return self._clean_rate("commission_individual")

    def clean_commission_organization(self):
        return self._clean_rate("commission_organization")


@staff_required
def store_settings(request):
    obj = SiteSettings.objects.get_or_create(pk=1)[0]
    files = request.FILES or None
    upload_error = None
    if request.method == "POST" and request.FILES.get("logo_file"):
        from .images import LOGO_MAX, optimize
        from .security import validate_image_upload
        try:
            validate_image_upload(request.FILES["logo_file"])
            files = request.FILES.copy()
            files["logo_file"] = optimize(request.FILES["logo_file"], LOGO_MAX)
        except ValidationError as exc:
            upload_error = exc
    form = SiteSettingsForm(request.POST or None, files, instance=obj)
    if request.method == "POST":
        if upload_error:
            form.add_error("logo_file", upload_error)
        if form.is_valid():
            form.save()
            _log(request, "Updated store settings")
            messages.success(request, "Settings saved. Your store has been updated.")
            return redirect("manage_settings")
        messages.error(request, "Please fix the highlighted fields.")
    from .couriers import status_line
    return _render(request, "settings.html", {"section": "settings", "form": form, "courier_status": status_line()})


# ---------------------------------------------------------------------------
# Setup wizard: the first thing a new owner sees. Five short steps that
# cover everything needed to open the store; each step saves on its own,
# so it can be left and resumed.
# ---------------------------------------------------------------------------

SETUP_STEPS = [
    ("basics", "Your store", "The name and contact details customers see.",
     ["site_name", "tagline", "support_email", "support_phone", "company_address"]),
    ("look", "Look & feel", "Your logo, colours and homepage headline.",
     ["logo_file", "logo_url", "primary_color", "accent_color", "hero_title", "hero_subtitle"]),
    ("selling", "Selling rules", "What you earn from every sale your sellers make.",
     ["commission_individual", "commission_organization"]),
    ("checkout", "Payments & delivery", "How customers pay and what delivery costs.",
     ["allow_cash_on_delivery", "shipping_flat_fee", "free_shipping_threshold", "tax_percent", "delivery_days", "return_days"]),
    ("launch", "Launch", "A last check before you open the doors.", []),
]

COLOR_PRESETS = [
    ("Lagoon", "#0E3B43", "#F2B33D"), ("Midnight", "#1E2A4A", "#7FD1B9"), ("Forest", "#1F4D2B", "#E9C46A"),
    ("Plum", "#4A1942", "#F4A259"), ("Charcoal", "#22252A", "#FF6B5B"), ("Ocean", "#0B4F8A", "#FFD166"),
]


def _setup_form(fields, data=None, files=None, instance=None):
    form_class = forms.modelform_factory(SiteSettings, form=SiteSettingsForm, fields=fields)
    form = form_class(data, files, instance=instance)
    labels = {"logo_file": "Upload your logo", "logo_url": "…or paste a logo link", "primary_color": "Main colour",
              "accent_color": "Accent colour", "hero_title": "Homepage headline", "hero_subtitle": "Homepage sub-heading"}
    for name, label in labels.items():
        if name in form.fields:
            form.fields[name].label = label
    return form


def _launch_checklist(site):
    from django.conf import settings as dj
    return [
        ("Store name and contact email", bool(site.site_name and site.support_email), "manage_setup", "?step=basics"),
        ("Logo added", bool(site.logo), "manage_setup", "?step=look"),
        ("Card payments (Stripe) connected", payments.is_configured(), "manage_system", ""),
        ("Stripe webhook set", payments.is_configured() and payments.webhook_configured(), "manage_system", ""),
        ("Emails can be sent", "console" not in getattr(dj, "EMAIL_BACKEND", "console"), "manage_system", ""),
        ("At least one product live", Product.objects.live().exists(), "manage_product_new", ""),
        ("Seller plans ready", SellerPlan.objects.filter(active=True).exists(), "manage_plans", ""),
        ("Two-step sign-in for staff", site.require_staff_2fa, "manage_settings", ""),
    ]


@staff_required
def setup_wizard(request):
    if not request.user.is_superuser:
        messages.error(request, "Only the store owner can run the setup.")
        return redirect("manage_dashboard")
    site = SiteSettings.objects.get_or_create(pk=1)[0]
    keys = [s[0] for s in SETUP_STEPS]
    key = request.GET.get("step") or request.POST.get("step") or keys[0]
    if key not in keys:
        key = keys[0]
    index = keys.index(key)
    _, title, hint, fields = SETUP_STEPS[index]

    if request.method == "POST" and request.POST.get("finish"):
        site.setup_completed = True
        site.save()
        _log(request, "Finished store setup")
        messages.success(request, "Your store is ready. Welcome aboard!")
        return redirect("manage_dashboard")
    if request.method == "POST" and request.POST.get("skip_all"):
        site.setup_completed = True
        site.save()
        messages.info(request, "Setup skipped. You can run it again any time from Store settings.")
        return redirect("manage_dashboard")

    form = None
    if fields:
        files = request.FILES or None
        upload_error = None
        if request.method == "POST" and request.FILES.get("logo_file"):
            from .images import LOGO_MAX, optimize
            from .security import validate_image_upload
            try:
                validate_image_upload(request.FILES["logo_file"])
                files = request.FILES.copy()
                files["logo_file"] = optimize(request.FILES["logo_file"], LOGO_MAX)
            except ValidationError as exc:
                upload_error = exc
        form = _setup_form(fields, request.POST if request.method == "POST" else None, files, site)
        if request.method == "POST":
            if upload_error:
                form.add_error("logo_file", upload_error)
            if form.is_valid():
                form.save()
                _log(request, f"Setup: saved '{title}'")
                return redirect(f"{reverse('manage_setup')}?step={keys[index + 1]}")
            messages.error(request, "Please fix the highlighted fields.")

    from django.conf import settings as dj
    return _render(request, "setup.html", {
        "section": "settings", "steps": SETUP_STEPS, "step_key": key, "step_index": index,
        "step_title": title, "step_hint": hint, "form": form, "site": site,
        "prev_key": keys[index - 1] if index else None,
        "presets": COLOR_PRESETS, "currency": dj.STORE_CURRENCY.upper(),
        "stripe_ready": payments.is_configured(), "webhook_ready": payments.webhook_configured(),
        "checklist": _launch_checklist(site) if key == "launch" else None,
        "plans": SellerPlan.objects.filter(active=True) if key == "selling" else None,
    })


# ---------------------------------------------------------------------------
# Buyer-seller messages (moderation)
# ---------------------------------------------------------------------------

@staff_required
def conversations(request):
    from .models import Conversation
    qs = Conversation.objects.select_related("buyer", "seller").exclude(last_message_at=None).order_by("-reported", "-last_message_at")
    if request.GET.get("tab") == "reported":
        qs = qs.filter(reported=True)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(buyer__username__icontains=q) | Q(buyer__email__icontains=q) | Q(seller__business_name__icontains=q)
                       | Q(seller__organization_name__icontains=q) | Q(messages__body__icontains=q)).distinct()
    return _render(request, "conversations.html", {
        "section": "support", "page_obj": _page(request, qs), "q": q, "tab": request.GET.get("tab", ""),
        "reported_count": Conversation.objects.filter(reported=True).count(),
    })


@staff_required
def conversation_detail(request, pk):
    from . import messaging
    from .models import Conversation
    conv = get_object_or_404(Conversation.objects.select_related("buyer", "seller"), pk=pk)
    if request.method == "POST" and request.POST.get("action") == "resolve":
        Conversation.objects.filter(pk=pk).update(reported=False)
        _log(request, f"Reviewed reported conversation #{pk}")
        _refresh_attention()
        messages.success(request, "Marked as reviewed.")
        return redirect("manage_conversations")
    return _render(request, "conversation.html", {
        "section": "support", "conv": conv,
        "thread": messaging.serialize(conv.messages.select_related("product"), "buyer"),
    })


# ---------------------------------------------------------------------------
# Currencies
# ---------------------------------------------------------------------------

class CurrencyForm(forms.ModelForm):
    class Meta:
        model = Currency
        fields = ["code", "symbol", "rate", "decimals", "position", "active"]

    def clean_code(self):
        code = self.cleaned_data["code"].strip().upper()
        if len(code) != 3 or not code.isalpha():
            raise ValidationError("Use the three-letter code, e.g. EUR.")
        from . import currency as cur
        if code == cur.store_code():
            raise ValidationError("That's the store currency already.")
        return code

    def clean_rate(self):
        rate = self.cleaned_data["rate"]
        if rate is None or rate <= 0:
            raise ValidationError("The rate must be more than 0.")
        return rate


@staff_required
def currencies(request):
    from . import currency as cur
    edit = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "update_rates":
            updated, error = cur.update_rates()
            if error:
                messages.error(request, error)
            else:
                _log(request, f"Updated {updated} currency rates")
                messages.success(request, f"{updated} rate{'s' if updated != 1 else ''} updated from today's market rates.")
            return redirect("manage_currencies")
        if action in ("toggle", "delete"):
            c = get_object_or_404(Currency, pk=request.POST.get("id"))
            if action == "toggle":
                c.active = not c.active
                c.save()
                messages.success(request, f"{c.code} is now {'shown in the shop' if c.active else 'hidden'}.")
            else:
                c.delete()
                from django.core.cache import cache as _cache
                _cache.delete(cur.CACHE_KEY)
                messages.success(request, "Currency removed.")
            return redirect("manage_currencies")
        instance = get_object_or_404(Currency, pk=request.POST.get("id")) if request.POST.get("id") else None
        edit = CurrencyForm(request.POST, instance=instance)
        if edit.is_valid():
            c = edit.save()
            _log(request, f"Saved currency {c.code}")
            messages.success(request, f"{c.code} saved.")
            return redirect("manage_currencies")
        messages.error(request, "Please fix the currency details below.")
    elif request.GET.get("edit"):
        edit = CurrencyForm(instance=get_object_or_404(Currency, pk=request.GET["edit"]))
    elif request.GET.get("new"):
        edit = CurrencyForm(initial={"decimals": 2, "active": True, "position": Currency.objects.count() + 1})
    rows = list(Currency.objects.all())
    for c in rows:
        c.example = cur.fmt(Decimal("100"), c)
    return _render(request, "currencies.html", {
        "section": "settings", "rows": rows, "edit": edit, "store_code": cur.store_code(),
    })


# ---------------------------------------------------------------------------
# Referral program
# ---------------------------------------------------------------------------

@staff_required
def referrals(request):
    referred = Profile.objects.exclude(referred_by="")
    by_code = {}
    for code, rewarded in referred.values_list("referred_by", "referral_rewarded"):
        row = by_code.setdefault(code, {"joined": 0, "bought": 0})
        row["joined"] += 1
        row["bought"] += 1 if rewarded else 0
    referrers = {p.referral_code: p for p in Profile.objects.filter(referral_code__in=by_code).select_related("user")}
    top = sorted(
        ({"profile": referrers[c], **v} for c, v in by_code.items() if c in referrers),
        key=lambda r: (-r["bought"], -r["joined"]),
    )[:50]
    welcome = Coupon.objects.filter(purpose="referral_welcome")
    rewards = Coupon.objects.filter(purpose="referral_reward")
    used = set(Order.objects.exclude(status="cancelled").exclude(coupon_code="").values_list("coupon_code", flat=True))
    stats = {
        "joined": referred.count(),
        "bought": referred.filter(referral_rewarded=True).count(),
        "rewards": rewards.count(),
        "rewards_used": rewards.filter(code__in=used).count(),
        "welcome_used": welcome.filter(code__in=used).count(),
    }
    return _render(request, "referrals.html", {"section": "customers", "top": top, "stats": stats, "brand": SiteSettings.load()})


# ---------------------------------------------------------------------------
# Translations of the shop's own text
# ---------------------------------------------------------------------------

TRANSLATION_LANGS = [("ur", "Urdu"), ("roman", "Roman Urdu"), ("ar", "Arabic"), ("es", "Spanish"), ("fr", "French")]


@staff_required
def translations(request):
    from django.conf import settings as dj
    from . import ai, i18n
    from .i18n_pack import PACK
    from .models import UIText
    lang = request.GET.get("lang") or request.POST.get("lang") or "ar"
    if lang not in dict(TRANSLATION_LANGS):
        lang = "ar"
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save":
            row = get_object_or_404(UIText, pk=request.POST.get("id"))
            setattr(row, lang, request.POST.get("text", "").strip()[:2000])
            row.by_ai = False
            row.save()
            messages.success(request, "Translation saved.")
        elif action == "add":
            source = i18n.normalize(request.POST.get("source", ""))[:400]
            if source:
                row, _ = UIText.objects.get_or_create(source=source)
                setattr(row, lang, request.POST.get("text", "").strip()[:2000])
                row.save()
                messages.success(request, "Translation added.")
        elif action == "ai":
            todo = [r for r in UIText.objects.filter(**{lang: ""}) if r.source not in PACK][:60]
            if not todo:
                messages.info(request, "Everything is already translated.")
            else:
                done = 0
                for start in range(0, len(todo), 30):
                    batch = todo[start:start + 30]
                    result = ai.translate_texts([r.source for r in batch], lang)
                    if result is None:
                        messages.error(request, "AI translation isn't available. Add ANTHROPIC_API_KEY to .env, or type translations by hand.")
                        break
                    for row, text in zip(batch, result):
                        if text:
                            setattr(row, lang, text)
                            row.by_ai = True
                            row.save()
                            done += 1
                if done:
                    _log(request, f"AI-translated {done} shop texts into {lang}")
                    messages.success(request, f"{done} text{'s' if done != 1 else ''} translated. Check a few, then press again for more.")
        elif action == "keep":
            row = get_object_or_404(UIText, pk=request.POST.get("id"))
            setattr(row, lang, row.source)
            row.by_ai = False
            row.save()
            messages.success(request, "Kept in English for this language.")
        elif action == "keep_all":
            rows = [r for r in UIText.objects.filter(**{lang: ""}) if r.source not in PACK]
            for row in rows:
                setattr(row, lang, row.source)
                row.by_ai = False
            UIText.objects.bulk_update(rows, [lang, "by_ai"])
            i18n.clear_cache()
            cache.delete("manage:health")
            _log(request, f"Kept {len(rows)} shop texts in English for {lang}")
            messages.success(request, f"{len(rows)} text{'s' if len(rows) != 1 else ''} kept in English.")
        elif action == "delete":
            UIText.objects.filter(pk=request.POST.get("id")).delete()
            i18n.clear_cache()
        return redirect(f"{reverse('manage_translations')}?lang={lang}&show={request.POST.get('show', 'missing')}")

    show = request.GET.get("show", "missing")
    q = request.GET.get("q", "").strip()
    rows = UIText.objects.all()
    if q:
        rows = rows.filter(Q(source__icontains=q) | Q(**{f"{lang}__icontains": q}))
    rows = [r for r in rows if r.source not in PACK]
    missing = [r for r in rows if not getattr(r, lang)]
    listing = missing if show == "missing" else rows
    for r in listing:
        r.current = getattr(r, lang)
    return _render(request, "translations.html", {
        "section": "settings", "langs": TRANSLATION_LANGS, "lang": lang, "show": show, "q": q,
        "page_obj": _page(request, listing, 30), "missing_count": len(missing), "builtin_count": len(PACK),
        "custom_count": len(rows), "ai_ready": bool(dj.AI_API_KEY),
    })



def operations_checks(dj=None):
    """Day-to-day health of the store: scheduled jobs, crashes, late orders,
    things waiting for staff, disk space, currencies and translations.
    Returns check dicts like system_check's."""
    import shutil
    from django.conf import settings as _settings
    from .alerts import DAILY_TASKS, SERVER_ERROR
    from .models import Conversation, Currency, OrderItem, UIText
    dj = dj or _settings
    now = timezone.now()
    out = []

    def add(group, name, ok, detail, level=None):
        out.append({"group": group, "name": name, "level": level or ("ok" if ok else "bad"), "detail": detail})

    # Scheduled jobs & crashes
    ran = AuditLog.objects.filter(action__startswith=DAILY_TASKS).order_by("-created_at").first()
    if ran:
        age = now - ran.created_at
        ok = age <= timedelta(hours=36) and ran.action.endswith(": ok")
        add("Daily jobs & errors", "Daily tasks", ok,
            f"Last run {timezone.localtime(ran.created_at):%b %d, %H:%M}" + ("." if ok else f" - {ran.action.split(': ', 1)[-1]}" if age <= timedelta(hours=36) else " - more than a day ago. Is the PythonAnywhere task still set?"),
            level=None if ok else ("bad" if age > timedelta(hours=36) else "warn"))
    else:
        add("Daily jobs & errors", "Daily tasks", False,
            "Never ran. On PythonAnywhere > Tasks add a daily task: cd ~/Lumen-Market && venv/bin/python manage.py daily_tasks "
            "(releases unpaid orders, cancels unconfirmed cash orders, sends reminders, updates badges and currency rates, backs up).",
            level="warn")
    errors = AuditLog.objects.filter(action__startswith=SERVER_ERROR, created_at__gte=now - timedelta(days=1))
    latest = errors.order_by("-created_at").first()
    add("Daily jobs & errors", "Page crashes (24 hours)", latest is None,
        "None." if latest is None else f"{errors.count()}. Latest {timezone.localtime(latest.created_at):%H:%M}: {latest.action[len(SERVER_ERROR) + 2:][:200]}")

    # Orders that need someone
    late = Order.objects.filter(status="confirmed", created_at__lt=now - timedelta(days=3)).exclude(payment_status__in=["pending", "failed"]).count()
    add("Orders", "Not shipped after 3 days", late == 0, "None." if not late else f"{late} order(s) - remind the sellers (Orders > filter 'Confirmed').",
        level=None if not late else "warn")
    overdue = Order.objects.filter(status="shipped", estimated_delivery__lt=timezone.localdate() - timedelta(days=5)).count()
    add("Orders", "Shipped but overdue", overdue == 0, "None." if not overdue else f"{overdue} parcel(s) 5+ days past the expected date - check with the courier.",
        level=None if not overdue else "warn")
    unconfirmed = Order.objects.filter(payment_method="cod", cod_confirmed=False, status="pending").count()
    add("Orders", "Cash orders waiting for the customer", True,
        "None." if not unconfirmed else f"{unconfirmed} - customers must click the link in their email; unconfirmed orders cancel after 48 hours.", level="ok")
    stuck_items = OrderItem.objects.filter(order__status="delivered").exclude(fulfillment_status="delivered").count()
    if stuck_items:
        add("Orders", "Items out of sync", False, f"{stuck_items} item(s) in delivered orders aren't marked delivered.", level="warn")

    # Waiting for staff
    waits = [
        ("Seller applications", SellerAccount.objects.filter(status="pending", created_at__lt=now - timedelta(days=2)).count(), "pending 2+ days"),
        ("Products to review", Product.objects.filter(approval_status="pending", created_at__lt=now - timedelta(days=2)).count(), "pending 2+ days"),
        ("Return requests", ReturnRequest.objects.filter(status="requested", created_at__lt=now - timedelta(days=3)).count(), "open 3+ days"),
        ("Payout requests", Payout.objects.filter(status="requested", created_at__lt=now - timedelta(days=3)).count(), "waiting 3+ days"),
        ("Reported conversations", Conversation.objects.filter(reported=True).count(), "to review"),
        ("Unanswered product questions", Question.objects.filter(answer="", created_at__lt=now - timedelta(days=2)).count(), "2+ days old"),
    ]
    for name, count, label in waits:
        add("Waiting for you", name, count == 0, "None." if not count else f"{count} {label}.", level=None if not count else "warn")

    # Catalogue
    out_of_stock = Product.objects.live().filter(stock__lte=0).count()
    add("Catalogue", "Out of stock", True, f"{out_of_stock} live product(s) can't be bought right now." if out_of_stock else "Everything live is in stock.",
        level="ok" if not out_of_stock else "warn")

    # Server
    try:
        usage = shutil.disk_usage(str(_settings.BASE_DIR))
        free_mb = usage.free // (1024 * 1024)
        add("Server", "Free disk space", free_mb > 300, f"{free_mb:,} MB free." + ("" if free_mb > 300 else " Delete old backups or move images to Supabase Storage."),
            level=None if free_mb > 300 else ("bad" if free_mb < 100 else "warn"))
    except Exception:
        pass
    import django
    import sys
    add("Server", "Software", True, f"Python {sys.version.split()[0]}, Django {django.get_version()}.", level="ok")

    # Languages, currencies, AI
    active = Currency.objects.filter(active=True)
    stale = [c.code for c in active if not c.updated_at or c.updated_at < now - timedelta(days=3)]
    if active.exists():
        add("Languages & currencies", "Currency rates", not stale,
            "Up to date." if not stale else f"Not updated for 3+ days: {', '.join(stale)}. Press 'Update rates now' in Currencies or type them in.",
            level=None if not stale else "warn")
    # Built-in texts are translated by the pack even when their row is blank.
    from .i18n_pack import PACK
    gaps = {}
    for row in UIText.objects.all():
        if row.source in PACK:
            continue
        for code, name in TRANSLATION_LANGS:
            if not getattr(row, code):
                gaps[name] = gaps.get(name, 0) + 1
    if not gaps:
        add("Languages & currencies", "Untranslated shop text", True, "Everything shoppers have seen is translated.", level="ok")
    else:
        detail = ", ".join(f"{name} {count}" for name, count in gaps.items())
        add("Languages & currencies", "Untranslated shop text", True,
            f"Still showing in English: {detail}. Usually product or brand names - open Admin > Translations and "
            "type a translation, press 'Translate missing with AI', or press 'Keep the rest in English' for names that shouldn't change.",
            level="warn")
    from .paypal import status_line as paypal_status
    p_state, p_text = paypal_status()
    add("Payments", "PayPal", p_state != "missing", p_text, level="ok" if p_state != "missing" else "warn")
    from .couriers import status_line
    c_state, c_text = status_line()
    add("Shipping", "Courier service", c_state != "missing", c_text, level="ok" if c_state != "missing" else "warn")
    from . import sms, social
    site = SiteSettings.load()
    if sms.is_configured():
        add("Messages", "Text messages (SMS)", True, "Twilio is connected: " + ", ".join(
            [w for w, on in (("order updates to customers", site.sms_customer_updates), ("new-order alerts to sellers", site.sms_seller_alerts),
                             ("cash order codes", site.cod_phone_check)) if on] or ["all SMS options are off in Store settings"]) + ".", level="ok")
    else:
        add("Messages", "Text messages (SMS)", False, "Off: add the Twilio keys under Keys & passwords to text customers and sellers."
            + (" Cash order phone check is on but can't work until then - the email link is used instead." if site.cod_phone_check else ""),
            level="warn" if site.cod_phone_check else "ok")
    providers = [p.title() for p in social.providers()]
    add("Security", "Sign in with Google / Apple", True,
        ("On: " + " and ".join(providers) + ".") if providers else "Off: add Google or Apple keys under Keys & passwords for one-tap sign in.", level="ok")
    add("Languages & currencies", "AI assistant", True,
        "On: listing writer, buying guides, shopper Q&A and one-click translation use Claude." if getattr(dj, "AI_API_KEY", "")
        else "Off: add ANTHROPIC_API_KEY to .env for AI buying guides, shopper Q&A and one-click translation (built-in writer still works).", level="ok")
    return out


def quick_health():
    """Number of problems for the admin dashboard banner (cached 10 min)."""
    data = cache.get("manage:health")
    if data is None:
        from django.conf import settings as dj
        checks = operations_checks(dj)
        problems = [c for c in checks if c["level"] == "bad"]
        if dj.EMAIL_BACKEND.endswith("console.EmailBackend"):
            problems.append({"name": "Email isn't set up"})
        data = {"bad": len(problems), "warn": sum(1 for c in checks if c["level"] == "warn"), "names": [p["name"] for p in problems][:3]}
        cache.set("manage:health", data, 600)
    return data


# ---------------------------------------------------------------------------
# Cash on delivery protection (block list)
# ---------------------------------------------------------------------------

def _auto_ready(seller):
    from . import payouts as auto
    return auto.method_ready(seller)


@staff_required
def cod_protection(request):
    from . import cod
    from .models import CodBlock
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add":
            kind = request.POST.get("kind")
            raw = request.POST.get("value", "").strip()
            value = cod.email_key(raw) if kind == "email" else cod.phone_key(raw)
            if kind not in ("email", "phone") or not value or (kind == "email" and "@" not in value):
                messages.error(request, "Enter a valid email address or phone number.")
            else:
                CodBlock.objects.get_or_create(kind=kind, value=value, defaults={
                    "reason": request.POST.get("reason", "").strip()[:200] or "Added by staff", "created_by": request.user})
                _log(request, f"COD block added: {kind} {value[:4]}…")
                messages.success(request, "Added. They can still buy, but only by paying online.")
        elif action == "delete":
            block = get_object_or_404(CodBlock, pk=request.POST.get("id"))
            block.delete()
            _log(request, f"COD block removed: {block.kind}")
            messages.success(request, "Removed. Cash on delivery is available to them again.")
        return redirect("manage_cod")
    site = SiteSettings.load()
    from . import sms
    refused = Order.objects.filter(cod_refused=True).order_by("-created_at")[:20]
    since = timezone.now() - timedelta(days=90)
    cod_orders = Order.objects.filter(payment_method="cod", created_at__gte=since)
    stats = {
        "orders": cod_orders.count(),
        "refused": cod_orders.filter(cod_refused=True).count(),
        "unconfirmed": cod_orders.filter(cod_confirmed=False, status="cancelled").count(),
    }
    stats["rate"] = round(stats["refused"] * 100 / stats["orders"], 1) if stats["orders"] else 0
    return _render(request, "cod.html", {
        "section": "settings", "blocks": CodBlock.objects.select_related("created_by")[:300], "refused": refused,
        "site": site, "sms_ready": sms.is_configured(), "stats": stats,
    })


# ---------------------------------------------------------------------------
# Business reports
# ---------------------------------------------------------------------------

REPORT_RANGES = [("7", "7 days"), ("30", "30 days"), ("90", "90 days"), ("365", "12 months")]


def _report_dates(request):
    from datetime import datetime
    today = timezone.localdate()
    try:
        start = datetime.strptime(request.GET.get("from", ""), "%Y-%m-%d").date()
        end = datetime.strptime(request.GET.get("to", ""), "%Y-%m-%d").date()
        if start > end or (end - start).days > 731:
            raise ValueError
        return start, min(end, today), "custom"
    except ValueError:
        days = request.GET.get("range", "30")
        days = days if days in dict(REPORT_RANGES) else "30"
        return today - timedelta(days=int(days) - 1), today, days


@staff_required
def reports(request):
    from . import analytics
    start, end, chosen = _report_dates(request)
    data = analytics.report(start, end)
    export = request.GET.get("export")
    if export:
        return _report_csv(export, data, start, end)
    return _render(request, "reports.html", {"section": "reports", "r": data, "ranges": REPORT_RANGES, "chosen": chosen,
                                             "query": request.GET.urlencode()})


def _report_csv(kind, data, start, end):
    from .bulk import _safe_cell
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="report-{kind}-{start}-{end}.csv"'
    resp.write("﻿")
    w = csv.writer(resp)
    if kind == "orders":
        w.writerow(["order", "date", "status", "payment", "payment method", "country", "items", "subtotal", "discount", "shipping", "tax", "total", "currency"])
        qs = Order.objects.filter(created_at__date__gte=start, created_at__date__lte=end).prefetch_related("items").order_by("created_at")
        for o in qs.iterator(chunk_size=500):
            w.writerow([o.id, timezone.localtime(o.created_at).strftime("%Y-%m-%d %H:%M"), o.status, o.payment_status, o.payment_method, o.country,
                        sum(i.quantity for i in o.items.all()), o.subtotal, o.discount_amount, o.shipping_amount, o.tax_amount, o.grand_total, o.currency.upper()])
    elif kind == "products":
        w.writerow(["product", "units", "revenue"])
        for r in data["top_products"]:
            w.writerow([_safe_cell(r["product_name"]), r["units"], r["revenue"]])
    elif kind == "sellers":
        w.writerow(["seller", "units", "revenue", "commission"])
        for r in data["by_seller"]:
            w.writerow([_safe_cell(r["seller_account__business_name"] or "Store"), r["units"], r["revenue"], r["commission"]])
    elif kind == "searches":
        w.writerow(["search", "times", "no results"])
        zero = {r["key"]: r["n"] for r in data["zero_searches"]}
        for r in data["searches"]:
            w.writerow([_safe_cell(r["key"]), r["n"], zero.get(r["key"], 0)])
    else:
        w.writerow(["metric", "value"])
        for key in ("revenue", "orders", "aov", "items_sold", "customers", "repeat_rate", "refund_rate", "return_rate", "cancel_rate", "commission", "conversion"):
            w.writerow([key, data[key]])
        w.writerow([])
        w.writerow(["date", "orders", "revenue"])
        for s in data["series"]:
            w.writerow([s["date"], s["orders"], s["revenue"]])
        w.writerow([])
        w.writerow(["country", "orders", "revenue"])
        for r in data["by_country"]:
            w.writerow([r["name"], r["orders"], r["revenue"]])
    return resp


# ---------------------------------------------------------------------------
# Store team (staff accounts and roles)
# ---------------------------------------------------------------------------

@staff_required
def team(request):
    from . import staff_roles
    if not request.user.is_superuser:
        messages.error(request, "Only the store owner can manage the team.")
        return redirect("manage_dashboard")
    roles = dict(staff_roles.ROLES)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add":
            email = request.POST.get("email", "").strip().lower()
            role = request.POST.get("role", "")
            if role not in roles:
                messages.error(request, "Choose a role.")
                return redirect("manage_team")
            try:
                from django.core.validators import validate_email
                validate_email(email)
            except ValidationError:
                messages.error(request, "Enter a valid email address.")
                return redirect("manage_team")
            user = User.objects.filter(email__iexact=email).order_by("id").first()
            created = False
            if user is None:
                base = "".join(ch for ch in email.split("@")[0] if ch.isalnum() or ch in "._-")[:20] or "staff"
                username, n = base, 1
                while User.objects.filter(username__iexact=username).exists():
                    n += 1
                    username = f"{base}{n}"
                user = User(username=username, email=email)
                user.set_unusable_password()
                user.save()
                created = True
            if user.is_superuser:
                messages.info(request, f"{email} is already a store owner.")
                return redirect("manage_team")
            user.is_staff = True
            user.save(update_fields=["is_staff"])
            Profile.objects.get_or_create(user=user)
            Profile.objects.filter(user=user).update(staff_role="" if role == "manager" else role)
            _send_team_invite(request, user, roles[role], created)
            _log(request, f"Added {user.username} to the team as {role}")
            messages.success(request, f"{email} added as {roles[role].split(' - ')[0]}." + (" We emailed them a link to set a password." if created else " They can sign in with their account."))
        elif action in ("role", "remove"):
            member = get_object_or_404(User, pk=request.POST.get("user"), is_staff=True, is_superuser=False)
            if action == "remove":
                member.is_staff = False
                member.save(update_fields=["is_staff"])
                Profile.objects.filter(user=member).update(staff_role="")
                _log(request, f"Removed {member.username} from the team")
                messages.success(request, f"{member.username} no longer has admin access.")
            else:
                role = request.POST.get("role", "")
                if role in roles:
                    Profile.objects.get_or_create(user=member)
                    Profile.objects.filter(user=member).update(staff_role="" if role == "manager" else role)
                    _log(request, f"{member.username}: team role -> {role}")
                    messages.success(request, f"{member.username} is now {roles[role].split(' - ')[0]}.")
        return redirect("manage_team")
    members = list(User.objects.filter(is_staff=True).select_related("profile").order_by("-is_superuser", "username"))
    for m in members:
        m.role = staff_roles.role_of(m)
        m.two_fa = Profile.objects.filter(user=m, totp_enabled=True).exists()
    return _render(request, "team.html", {"section": "team", "members": members, "roles": staff_roles.ROLES,
                                          "role_help": staff_roles.ROLE_HELP})


def _send_team_invite(request, user, role_label, created):
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode
    from .views import _send_html_email
    if created:
        url = request.build_absolute_uri(reverse("password_reset_confirm", args=[urlsafe_base64_encode(force_bytes(user.pk)), default_token_generator.make_token(user)]))
    else:
        url = request.build_absolute_uri(reverse("manage_dashboard"))
    _send_html_email(f"You've been added to the {SiteSettings.load().site_name} team", "bees/emails/team_invite.html",
                     {"user": user, "role": role_label, "url": url, "created": created, "inviter": request.user}, user.email)
