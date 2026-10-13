"""Pages for bulk product import / export (Seller Center and store admin)."""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.shortcuts import redirect

from . import bulk
from .manage_views import _log, _render as manage_render, staff_required
from .models import Product
from .seller_views import _render as seller_render, seller_required


def _handle(request, products, *, seller=None, store=False, back, render):
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "upload":
            upload = request.FILES.get("file")
            if not upload:
                messages.error(request, "Choose a CSV or Excel file first.")
                return redirect(back)
            try:
                rows = bulk.read_rows(upload)
                result = bulk.plan(rows, products, seller=seller, store=store)
            except bulk.BulkError as exc:
                messages.error(request, str(exc))
                return redirect(back)
            bulk.stash(request.user, result)
            return render({"preview": result, "file_name": upload.name[:80]})
        if action == "confirm":
            entries = bulk.unstash(request.user)
            if not entries:
                messages.error(request, "That import expired. Please upload the file again.")
                return redirect(back)
            created, updated, review = bulk.apply(entries, products, seller=seller, store=store, user=request.user)
            msg = f"Done: {created} added, {updated} updated."
            if review:
                msg += f" {review} will go live after a quick review."
            messages.success(request, msg)
            return redirect(back)
        if action == "change":
            mode = request.POST.get("mode")
            try:
                value = Decimal(request.POST.get("value", "").strip())
            except InvalidOperation:
                value = None
            category = request.POST.get("category", "")
            if category and category not in dict(Product.CATEGORY_CHOICES):
                category = ""
            if value is None or mode not in ("price_pct", "price_add", "stock_set") \
                    or (mode == "price_pct" and not Decimal("-90") <= value <= Decimal("500")) \
                    or (mode == "stock_set" and (value < 0 or value > 1_000_000 or value != int(value))):
                messages.error(request, "Check the number: percent between -90 and 500, stock a whole number 0 or more.")
                return redirect(back)
            changed = bulk.bulk_change(products, mode, value, category)
            if store:
                _log(request, f"Bulk change {mode} {value} ({category or 'all'}): {changed} products")
            messages.success(request, f"{changed} product{'s' if changed != 1 else ''} updated.")
            return redirect(back)
        messages.error(request, "Unknown action.")
        return redirect(back)
    fmt = request.GET.get("download")
    if fmt in ("csv", "xlsx"):
        if request.GET.get("what") == "template":
            return bulk.template(fmt)
        name = f"products-seller-{seller.id}" if seller else "products-store"
        return bulk.export(products.exclude(approval_status="archived"), fmt, name, request)
    return render({})


@seller_required(approved=True)
def seller_bulk(request):
    seller = request.seller
    return _handle(request, Product.objects.filter(seller_account=seller), seller=seller, back="seller_bulk",
                   render=lambda ctx: seller_render(request, "bulk.html", {"section": "products", "categories": Product.CATEGORY_CHOICES,
                                                                           "count": seller.products.exclude(approval_status="archived").count(), **ctx}))


@staff_required
def manage_bulk(request):
    scope = request.GET.get("scope") or request.POST.get("scope") or "store"
    products = Product.objects.all() if scope == "all" else Product.objects.filter(seller_account__isnull=True)
    if request.method == "POST" and scope == "all" and request.POST.get("action") == "change":
        messages.error(request, "Bulk price/stock changes only apply to the store's own products. Sellers set their own prices.")
        return redirect("manage_bulk")
    return _handle(request, products, store=True, back="manage_bulk",
                   render=lambda ctx: manage_render(request, "bulk.html", {"section": "products", "categories": Product.CATEGORY_CHOICES,
                                                                          "scope": scope, "count": products.exclude(approval_status="archived").count(), **ctx}))
