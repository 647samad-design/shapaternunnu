"""'Report this listing' (notice and action, as marketplace laws such as the
EU Digital Services Act expect): anyone can report a product; the team
removes it or dismisses the report, and the reporter is told the outcome."""
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST

from .ratelimit import ratelimit


@require_POST
@ratelimit("listing_report", rate_limit=5, window_seconds=3600)
def report(request, pk):
    from .models import ListingReport, Product
    product = get_object_or_404(Product, pk=pk)
    reason = request.POST.get("reason", "")
    details = request.POST.get("details", "").strip()[:2000]
    email = request.POST.get("email", "").strip().lower() if not request.user.is_authenticated else (request.user.email or "")
    if reason not in dict(ListingReport.REASONS):
        messages.error(request, "Please choose what's wrong with this listing.")
        return redirect("product_detail", pk=pk)
    if email:
        try:
            validate_email(email)
        except ValidationError:
            email = ""
    if ListingReport.objects.filter(product=product, status="open", reporter=request.user if request.user.is_authenticated else None,
                                    email=email).exists() and (request.user.is_authenticated or email):
        messages.info(request, "Thanks - you've already reported this listing. Our team is looking at it.")
        return redirect("product_detail", pk=pk)
    ListingReport.objects.create(product=product, reason=reason, details=details, email=email,
                                 reporter=request.user if request.user.is_authenticated else None)
    from .alerts import notify_staff
    notify_staff(f"Listing reported ({dict(ListingReport.REASONS)[reason]}): {product.name[:60]}", link="/manage/listing-reports/")
    messages.success(request, "Thank you. Our team will review this listing" + (" and email you what we decided." if email else "."))
    return redirect("product_detail", pk=pk)


def admin_page(request):
    from .manage_views import _log, _render
    from .models import ListingReport, Notification, Product
    if request.method == "POST":
        rep = get_object_or_404(ListingReport.objects.select_related("product"), pk=request.POST.get("id"))
        action = request.POST.get("action")
        note = request.POST.get("note", "").strip()[:300]
        if action in ("remove", "dismiss") and rep.status == "open":
            same = ListingReport.objects.filter(product=rep.product, status="open")
            if action == "remove":
                Product.objects.filter(pk=rep.product_id).update(approval_status="rejected", is_flash_sale=False)
                if rep.product.seller_account_id:
                    from .seller_center import _seller_users
                    for u in _seller_users(rep.product.seller_account):
                        Notification.objects.create(user=u, link="/seller/products/?tab=rejected",
                                                    message=f"'{rep.product.name[:60]}' was taken down after a report: {rep.get_reason_display()}. {note}"[:255])
            reports = list(same)
            same.update(status="removed" if action == "remove" else "dismissed", decision_note=note, handled_by=request.user, handled_at=timezone.now())
            for r in reports:
                if r.email:
                    from .views import _send_html_email
                    _send_html_email("About the listing you reported", "bees/emails/listing_report.html",
                                     {"report": r, "removed": action == "remove", "note": note}, r.email)
            _log(request, f"Listing report on product #{rep.product_id}: {action}")
            messages.success(request, "Listing taken down and reporters told." if action == "remove" else "Report closed and reporters told.")
        return redirect("manage_listing_reports")
    status = request.GET.get("status", "open")
    qs = ListingReport.objects.select_related("product", "product__seller_account", "reporter")
    if status in dict(ListingReport.STATUS):
        qs = qs.filter(status=status)
    return _render(request, "listing_reports.html", {"section": "products", "reports": qs[:200], "status": status,
                                                     "statuses": ListingReport.STATUS})
