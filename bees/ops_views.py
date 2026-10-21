"""Pages for business accounts, stock locations, integrations (webhooks
and accounting export), customer order changes and seller ID checks."""
import csv
import io
import secrets
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import F, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .ratelimit import ratelimit


# ---------------------------------------------------------------- customer: change address / cancel one item

ADDRESS_FIELDS = (("full_name", 150), ("address", 255), ("city", 100), ("state", 100), ("postal_code", 20), ("phone", 30))


def _own_order(request, pk):
    from .models import Order
    return get_object_or_404(Order, pk=pk, user=request.user)


def address_locked(order):
    """Why the address can't change any more ("" if it can)."""
    if order.status in ("shipped", "delivered", "cancelled"):
        return "This order has already shipped." if order.status != "cancelled" else "This order was cancelled."
    if order.items.filter(fulfillment_status__in=["handed_to_courier", "delivered"]).exists():
        return "Part of this order has already shipped."
    if order.pickup_point_id:
        return "This order goes to a collection point."
    from .models import Shipment
    if Shipment.objects.filter(order=order).exists():
        return "A shipping label was already made. Please message the seller."
    return ""


@login_required
def change_address(request, pk):
    from .models import Notification, OrderEvent
    from .seller_center import _seller_users
    order = _own_order(request, pk)
    locked = address_locked(order)
    if request.method == "POST":
        if locked:
            messages.error(request, locked)
            return redirect("my_orders")
        data = {f: " ".join(request.POST.get(f, "").split())[:n] for f, n in ADDRESS_FIELDS}
        if not all(data[f] for f in ("full_name", "address", "city", "postal_code", "phone")):
            messages.error(request, "Please fill in name, street, city, postal code and phone.")
            return redirect("change_address", pk=pk)
        with transaction.atomic():
            for f, v in data.items():
                setattr(order, f, v)
            order.save(update_fields=[f for f, _n in ADDRESS_FIELDS])
            OrderEvent.objects.create(order=order, kind="note", text="Delivery address changed by the customer")
            sellers = {i.seller_account for i in order.items.select_related("seller_account") if i.seller_account_id}
            for s in sellers:
                Notification.objects.bulk_create([Notification(user=u, link=f"/seller/orders/{order.id}/",
                                                               message=f"Order #{order.id}: the customer changed the delivery address. Use the new one.")
                                                  for u in _seller_users(s)])
        messages.success(request, "Address updated. The seller has been told.")
        return redirect("my_orders")
    return render(request, "bees/change_address.html", {"order": order, "locked": locked})


def cancel_item_reason(item):
    order = item.order
    if item.cancelled_at:
        return "This item is already cancelled."
    if order.status == "cancelled":
        return "This order was cancelled."
    if item.fulfillment_status != "pending":
        return "The seller has already packed or shipped this item."
    if not (order.payment_method in ("card", "paypal") and order.payment_status == "paid"):
        return "For cash and invoice orders, cancel the whole order or message the seller."
    if order.items.filter(cancelled_at=None).exclude(pk=item.pk).count() == 0:
        return "This is the last item - cancel the whole order instead."
    return ""


@login_required
@require_POST
@ratelimit("cancel_item", rate_limit=10, window_seconds=300)
def cancel_item(request, item_id):
    from .models import Notification, OrderEvent, OrderItem, ReturnRequest
    from .refunds import RefundError, describe, refund_return
    from .seller_center import _seller_users
    item = get_object_or_404(OrderItem.objects.select_related("order", "seller_account"), pk=item_id, order__user=request.user)
    reason = cancel_item_reason(item)
    if reason:
        messages.error(request, reason)
        return redirect("my_orders")
    with transaction.atomic():
        locked = OrderItem.objects.select_for_update(of=("self",)).get(pk=item.pk)
        if locked.cancelled_at or locked.fulfillment_status != "pending":
            messages.error(request, "The seller has already started on this item.")
            return redirect("my_orders")
        rr = ReturnRequest.objects.create(order_item=locked, user=request.user, reason="Cancelled before shipping", status="approved")
        try:
            plan = refund_return(rr, restock=True, note="Cancelled by the customer before shipping")
        except RefundError as exc:
            transaction.set_rollback(True)
            messages.error(request, str(exc))
            return redirect("my_orders")
        locked.cancelled_at = timezone.now()
        locked.save(update_fields=["cancelled_at"])
        OrderEvent.objects.create(order=locked.order, kind="note", text=f"Cancelled by the customer: {locked.product_name}"[:200])
    if item.seller_account_id:
        Notification.objects.bulk_create([Notification(user=u, link=f"/seller/orders/{item.order_id}/",
                                                       message=f"Order #{item.order_id}: the customer cancelled '{item.product_name}'. Don't ship it."[:255])
                                          for u in _seller_users(item.seller_account)])
    messages.success(request, f"'{item.product_name}' cancelled: {describe(plan)}.")
    return redirect("my_orders")


# ---------------------------------------------------------------- business accounts (customer)

@login_required
def company_account(request):
    from .models import Company, CompanyMember, SiteSettings
    from .ops import membership
    if not SiteSettings.load().b2b_enabled:
        raise Http404
    member = membership(request.user)
    if request.method == "POST":
        action = request.POST.get("action", "apply")
        if action == "apply" and not member:
            name = " ".join(request.POST.get("name", "").split())[:150]
            email = request.POST.get("billing_email", "").strip()[:254] or request.user.email
            if not name or "@" not in email:
                messages.error(request, "Add the company name and a billing email.")
                return redirect("company_account")
            company = Company.objects.create(name=name, billing_email=email, tax_id=request.POST.get("tax_id", "").strip()[:40],
                                             country=request.POST.get("country", "").strip().upper()[:2],
                                             billing_address=request.POST.get("billing_address", "").strip()[:500],
                                             tax_exempt_requested=bool(request.POST.get("tax_exempt_requested")),
                                             exemption_note=request.POST.get("exemption_note", "").strip()[:300])
            CompanyMember.objects.create(company=company, user=request.user, role="admin")
            from .alerts import notify_staff
            notify_staff(f"New business account to review: {company.name}", link=f"/manage/companies/{company.pk}/")
            messages.success(request, "Thanks! We'll review your business account, usually within one working day.")
        elif member and member.role == "admin" and action == "add_member":
            email = request.POST.get("email", "").strip().lower()
            user = get_user_model().objects.filter(email__iexact=email).first()
            if not user:
                messages.error(request, "No account with that email yet - ask them to sign up first.")
            elif CompanyMember.objects.filter(user=user).exists():
                messages.error(request, "That person already belongs to a business account.")
            else:
                CompanyMember.objects.create(company=member.company, user=user, role="buyer")
                messages.success(request, f"{email} can now buy for {member.company.name}.")
        elif member and member.role == "admin" and action == "remove_member":
            CompanyMember.objects.filter(company=member.company, pk=request.POST.get("member")).exclude(user=request.user).delete()
            messages.success(request, "Removed.")
        return redirect("company_account")
    ctx = {"tab": "company", "member": member, "today": timezone.localdate()}
    if member:
        company = member.company
        ctx.update({"company": company, "members": company.members.select_related("user"),
                    "orders": company.orders.select_related("user").order_by("-id")[:30],
                    "unpaid": company.unpaid_total(), "room": company.invoice_room()})
    from .countries import COUNTRIES
    ctx["countries"] = COUNTRIES
    return render(request, "bees/account/company.html", ctx)


# ---------------------------------------------------------------- business accounts (admin)

def manage_companies(request):
    from .manage_views import _render
    from .models import Company
    status = request.GET.get("status", "")
    qs = Company.objects.all()
    if status in dict(Company.STATUS):
        qs = qs.filter(status=status)
    from .ops import overdue_invoices
    return _render(request, "companies.html", {"section": "companies", "companies": qs[:200], "status": status,
                                               "overdue": overdue_invoices().select_related("company")[:50]})


def manage_company(request, pk):
    from .manage_views import _log, _render
    from .models import Company, Order, OrderEvent
    company = get_object_or_404(Company, pk=pk)
    if request.method == "POST":
        action = request.POST.get("action", "save")
        if action == "save":
            status = request.POST.get("status", company.status)
            company.status = status if status in dict(Company.STATUS) else company.status
            company.tax_exempt = bool(request.POST.get("tax_exempt"))
            try:
                terms = int(request.POST.get("payment_terms", "0"))
                limit = Decimal(request.POST.get("credit_limit", "0") or "0")
            except (ValueError, InvalidOperation):
                terms, limit = -1, Decimal("-1")
            if terms not in dict(Company.TERMS) or limit < 0:
                messages.error(request, "Choose payment terms and a credit limit of 0 or more.")
                return redirect("manage_company", pk=pk)
            company.payment_terms, company.credit_limit = terms, limit
            company.admin_note = request.POST.get("admin_note", "").strip()[:300]
            company.save()
            _log(request, f"Business account {company.name}: {company.status}, terms {terms}, limit {limit}, exempt {company.tax_exempt}")
            from .models import Notification
            for m in company.members.filter(role="admin"):
                Notification.objects.create(user=m.user, link="/account/business/", message=f"Your business account '{company.name}' is {company.get_status_display().lower()}."[:255])
            messages.success(request, "Saved.")
        elif action == "paid":
            order = get_object_or_404(Order, pk=request.POST.get("order"), company=company, payment_status="invoiced")
            order.payment_status, order.invoice_paid_at = "paid", timezone.now()
            order.save(update_fields=["payment_status", "invoice_paid_at"])
            OrderEvent.objects.create(order=order, kind="paid", text="Invoice paid")
            _log(request, f"Invoice for order #{order.id} marked paid")
            messages.success(request, f"Order #{order.id} marked paid.")
        return redirect("manage_company", pk=pk)
    return _render(request, "company.html", {"section": "companies", "company": company, "members": company.members.select_related("user"),
                                             "orders": company.orders.order_by("-id")[:50], "unpaid": company.unpaid_total(),
                                             "terms": Company.TERMS, "statuses": Company.STATUS})


# ---------------------------------------------------------------- stock locations

def _locations_page(request, seller, template_render, redirect_name):
    from .models import Product, ProductVariant, StockLevel, Warehouse
    from .ops import sync_total
    warehouses = Warehouse.objects.filter(seller_account=seller) if seller else Warehouse.objects.filter(seller_account__isnull=True)
    products = (Product.objects.filter(seller_account=seller) if seller else Product.objects.filter(seller_account__isnull=True))
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add":
            name = " ".join(request.POST.get("name", "").split())[:80]
            country = request.POST.get("country", "").strip().upper()[:2]
            if not name or len(country) != 2:
                messages.error(request, "Give the location a name and a country.")
            elif warehouses.count() >= 20:
                messages.error(request, "Up to 20 locations.")
            else:
                Warehouse.objects.create(seller_account=seller, name=name, country=country,
                                         city=" ".join(request.POST.get("city", "").split())[:80], priority=warehouses.count() + 1)
                messages.success(request, f"Location '{name}' added. Now set how many of each product are there.")
        elif action == "toggle":
            wh = get_object_or_404(warehouses, pk=request.POST.get("id"))
            wh.active = not wh.active
            wh.save(update_fields=["active"])
            for lv in StockLevel.objects.filter(warehouse=wh).values("product_id", "variant_id").distinct():
                sync_total(lv["product_id"], lv["variant_id"])
        elif action == "stock":
            changed = 0
            with transaction.atomic():
                for key, value in request.POST.items():
                    if not key.startswith("q_"):
                        continue
                    parts = key[2:].split("_")  # warehouse_product_variant
                    if len(parts) != 3 or not all(p.isdigit() for p in parts[:2]):
                        continue
                    wid, pid, vid = int(parts[0]), int(parts[1]), int(parts[2]) if parts[2].isdigit() and parts[2] != "0" else None
                    if not warehouses.filter(pk=wid).exists() or not products.filter(pk=pid).exists():
                        continue
                    if vid and not ProductVariant.objects.filter(pk=vid, product_id=pid).exists():
                        continue
                    value = value.strip()
                    if value == "":
                        StockLevel.objects.filter(warehouse_id=wid, product_id=pid, variant_id=vid).delete()
                    else:
                        try:
                            qty = max(0, min(int(value), 1000000))
                        except ValueError:
                            continue
                        StockLevel.objects.update_or_create(warehouse_id=wid, product_id=pid, variant_id=vid, defaults={"quantity": qty})
                    sync_total(pid, vid)
                    changed += 1
            messages.success(request, "Stock saved. Product stock is now the total of all locations." if changed else "Nothing changed.")
        return redirect(redirect_name)
    rows = []
    levels = {(lv.warehouse_id, lv.product_id, lv.variant_id): lv.quantity
              for lv in StockLevel.objects.filter(product__in=products)}
    active = [w for w in warehouses if w.active]
    for p in products.order_by("name")[:300]:
        variants = list(p.variants.all()) if p.has_variants else [None]
        for v in variants:
            rows.append({"product": p, "variant": v, "label": f"{p.name} ({v.label})" if v else p.name,
                         "cells": [(w, levels.get((w.id, p.id, v.id if v else None))) for w in active],
                         "key": f"{p.id}_{v.id if v else 0}"})
    from .countries import COUNTRIES
    return template_render(request, "locations.html", {"section": "locations", "warehouses": warehouses, "active": active,
                                                       "rows": rows, "countries": COUNTRIES})


def seller_locations(request):
    from .seller_views import _render
    return _locations_page(request, request.seller, _render, "seller_locations")


def manage_locations(request):
    from .manage_views import _render
    return _locations_page(request, None, _render, "manage_locations")


# ---------------------------------------------------------------- integrations (webhooks + accounting)

def manage_integrations(request):
    from .manage_views import _log, _render
    from .models import WebhookEndpoint
    from .ops import send_test
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add":
            url = request.POST.get("url", "").strip()[:500]
            events = [e for e in request.POST.getlist("events") if e in dict(WebhookEndpoint.EVENTS)]
            if not url.startswith("https://") or not events:
                messages.error(request, "Use an https:// address and pick at least one event.")
            else:
                ep = WebhookEndpoint.objects.create(url=url, events=events, secret=secrets.token_hex(24))
                _log(request, f"Added webhook {url[:80]}")
                messages.success(request, "Webhook added. Copy its signing secret to check messages really come from your store.")
                return redirect(f"{reverse('manage_integrations')}?new={ep.pk}")
        else:
            ep = get_object_or_404(WebhookEndpoint, pk=request.POST.get("id"))
            if action == "delete":
                ep.delete()
                _log(request, f"Deleted webhook {ep.url[:80]}")
                messages.success(request, "Webhook deleted.")
            elif action == "toggle":
                ep.active = not ep.active
                ep.failures = 0 if ep.active else ep.failures
                ep.save(update_fields=["active", "failures"])
            elif action == "test":
                ok = send_test(ep)
                (messages.success if ok else messages.error)(request, "Test delivered." if ok else "The test didn't arrive - check the address. Details below.")
        return redirect("manage_integrations")
    endpoints = list(WebhookEndpoint.objects.order_by("-id"))
    for ep in endpoints:
        ep.recent = list(ep.deliveries.all()[:5])
    new = request.GET.get("new", "")
    return _render(request, "integrations.html", {
        "section": "integrations", "endpoints": endpoints, "event_choices": WebhookEndpoint.EVENTS,
        "new_id": int(new) if new.isdigit() else None,
        "today": timezone.localdate(), "month_start": timezone.localdate().replace(day=1)})


def _date(value, default):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return default


def accounting_export(request):
    """Sales as invoices for Xero or QuickBooks Online import (CSV)."""
    from .models import Order
    fmt = request.GET.get("format", "xero")
    today = timezone.localdate()
    start = _date(request.GET.get("from"), today.replace(day=1))
    end = _date(request.GET.get("to"), today)
    orders = (Order.objects.filter(created_at__date__gte=start, created_at__date__lte=end).exclude(status="cancelled")
              .exclude(payment_status__in=["pending", "failed"]).prefetch_related("items").order_by("id"))
    buf = io.StringIO()
    w = csv.writer(buf)
    if fmt == "quickbooks":
        w.writerow(["InvoiceNo", "Customer", "InvoiceDate", "DueDate", "Item(Product/Service)", "ItemDescription", "ItemQuantity",
                    "ItemRate", "ItemAmount", "ItemTaxAmount", "Currency"])
    else:
        fmt = "xero"
        w.writerow(["*ContactName", "EmailAddress", "POCountry", "*InvoiceNumber", "Reference", "*InvoiceDate", "*DueDate",
                    "Description", "*Quantity", "*UnitAmount", "Discount", "*AccountCode", "*TaxType", "Currency"])
    for o in orders:
        date = o.created_at.date()
        due = o.payment_due or date
        name = (o.company.name if o.company_id else "") or o.full_name or o.email
        lines = [(i.product_name, i.quantity, i.price) for i in o.items.all() if not i.cancelled_at]
        if o.shipping_amount:
            lines.append(("Shipping", 1, o.shipping_amount))
        if o.discount_amount:
            lines.append(("Discount", 1, -o.discount_amount))
        for n, (desc, qty, rate) in enumerate(lines):
            tax = o.added_tax if n == 0 else ""
            if fmt == "quickbooks":
                w.writerow([f"LM-{o.id}", name, date.strftime("%m/%d/%Y"), due.strftime("%m/%d/%Y"), "Sales", desc, qty, rate,
                            Decimal(rate) * qty, tax, o.currency.upper()])
            else:
                w.writerow([name, o.email, o.country, f"LM-{o.id}", o.po_number or "", date.strftime("%d/%m/%Y"), due.strftime("%d/%m/%Y"),
                            desc, qty, rate, "", request.GET.get("account", "200")[:10], "Tax on Sales" if o.tax_amount else "Tax Exempt",
                            o.currency.upper()])
    response = HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="sales-{fmt}-{start}-{end}.csv"'
    return response


# ---------------------------------------------------------------- seller ID check

@require_POST
def seller_identity_start(request):
    from .ops import identity_available, start_identity
    seller = request.seller
    if not identity_available():
        raise Http404
    if seller.identity_status == "verified":
        messages.info(request, "Your ID is already verified.")
        return redirect("seller_store_settings")
    try:
        url = start_identity(request, seller)
    except Exception:
        messages.error(request, "The ID check couldn't start right now. Please try again in a few minutes.")
        return redirect("seller_store_settings")
    return redirect(url)
