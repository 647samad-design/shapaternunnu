"""Problem reports ("claims"): the customer says an item didn't arrive, is
damaged or isn't as described.

    customer opens it  ->  seller answers (and can offer a refund)
                       ->  store team decides: refund or close
If the seller doesn't answer within ``claim_reply_days`` it goes to the
store team automatically (bees.automation). While a claim is open the
seller's earnings for that item stay on hold.
"""
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Claim, Notification, OrderItem, ReturnRequest, SiteSettings

CLAIM_EXTRA_DAYS = 30  # after the return window, problems can still be reported for this long


def _seller_users(item):
    if not item.seller_account_id:
        return []
    from .seller_center import _seller_users as users
    return users(item.seller_account)


def _notify(users, message, link):
    Notification.objects.bulk_create([Notification(user=u, message=message[:255], link=link) for u in users])


def _staff(message, link):
    from .alerts import notify_staff
    notify_staff(message, link=link)


def can_open(item):
    """("", ok) or (reason why not)."""
    order = item.order
    if order.status in ("cancelled", "pending"):
        return "This order isn't on its way yet."
    if item.fulfillment_status not in ("handed_to_courier", "delivered") and order.status != "delivered":
        return "You can report a problem once the item has shipped."
    received = item.received_at
    if received and timezone.now() > received + timedelta(days=SiteSettings.load().return_days + CLAIM_EXTRA_DAYS):
        return "It's been too long since delivery to open a report. Please contact support."
    if any(c.is_open for c in item.claims.all()):
        return "You already have an open report for this item."
    if item.return_requests.filter(status__in=["requested", "approved", "refunded"]).exists():
        return "This item already has a return in progress."
    return ""


# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------

@login_required
def open_claim(request, item_id):
    item = get_object_or_404(OrderItem.objects.select_related("order", "seller_account"), pk=item_id, order__user=request.user)
    reason_block = can_open(item)
    if reason_block:
        messages.error(request, reason_block)
        return redirect("my_orders")
    reasons = Claim.REASONS if item.received_at else [r for r in Claim.REASONS if r[0] in ("not_received", "other")]
    if request.method == "POST":
        reason = request.POST.get("reason", "")
        details = request.POST.get("details", "").strip()[:2000]
        photo = request.FILES.get("photo")
        error = ""
        if reason not in dict(reasons):
            error = "Please choose what went wrong."
        elif len(details) < 10:
            error = "Please tell us a little more (at least a sentence)."
        cleaned = None
        if not error and photo:
            from .messaging import clean_photo
            try:
                cleaned = clean_photo(photo)
            except ValueError as exc:
                error = str(exc)
        if error:
            messages.error(request, error)
        else:
            claim = Claim(order_item=item, user=request.user, reason=reason, details=details,
                          status="open" if item.seller_account_id else "escalated")
            if cleaned is not None:
                claim.photo.save(cleaned.name, cleaned, save=False)
            claim.save()
            from .models import OrderEvent
            OrderEvent.objects.create(order=item.order, kind="note", public=False,
                                      text=f"Problem reported: {item.product_name} - {claim.get_reason_display()}"[:200])
            text = f"Problem reported on order #{item.order_id}: '{item.product_name}' - {claim.get_reason_display()}."
            if item.seller_account_id:
                _notify(_seller_users(item), text + " Please answer within "
                        f"{SiteSettings.load().claim_reply_days} days.", reverse("seller_claims"))
            _staff(text, reverse("manage_claim", args=[claim.id]))
            messages.success(request, "Thanks - we've told the seller. You'll get an answer here and by notification.")
            return redirect("claim_detail", pk=claim.pk)
    return render(request, "bees/claim_form.html", {"item": item, "reasons": reasons, "reply_days": SiteSettings.load().claim_reply_days})


@login_required
def claim_detail(request, pk):
    claim = get_object_or_404(Claim.objects.select_related("order_item__order", "order_item__seller_account"), pk=pk, user=request.user)
    if request.method == "POST" and request.POST.get("action") == "withdraw" and claim.is_open:
        claim.status = "withdrawn"
        claim.save(update_fields=["status", "updated_at"])
        _notify(_seller_users(claim.order_item), f"The customer withdrew their problem report on '{claim.order_item.product_name}'.", reverse("seller_claims"))
        messages.success(request, "Report withdrawn.")
        return redirect("claim_detail", pk=pk)
    if request.method == "POST" and request.POST.get("action") == "escalate" and claim.status == "seller_replied":
        claim.status = "escalated"
        claim.save(update_fields=["status", "updated_at"])
        _staff(f"Customer asked the store team to decide claim #{claim.id} ('{claim.order_item.product_name}').", reverse("manage_claim", args=[claim.id]))
        messages.success(request, "The store team will look at it and decide.")
        return redirect("claim_detail", pk=pk)
    return render(request, "bees/claim_detail.html", {"claim": claim})


@login_required
def claim_photo(request, pk):
    claim = get_object_or_404(Claim.objects.select_related("order_item"), pk=pk)
    seller_ok = request.user in _seller_users(claim.order_item)
    if not claim.photo or not (request.user.is_staff or claim.user_id == request.user.id or seller_ok):
        raise Http404
    from django.conf import settings
    if getattr(settings, "USE_SUPABASE_STORAGE", False):
        return redirect(claim.photo.url)
    from django.http import FileResponse
    response = FileResponse(claim.photo.open("rb"), content_type="image/jpeg")
    response["Cache-Control"] = "private, max-age=3600"
    return response


# ---------------------------------------------------------------------------
# Seller
# ---------------------------------------------------------------------------

def seller_claims(request):
    """Seller Center > Problems (wrapped by seller_required in seller_views)."""
    seller = request.seller
    qs = Claim.objects.filter(order_item__seller_account=seller).select_related("order_item__order", "user")
    if request.method == "POST":
        claim = get_object_or_404(qs, pk=request.POST.get("id"))
        reply = request.POST.get("reply", "").strip()[:2000]
        if not claim.is_open:
            messages.error(request, "This report is already closed.")
        elif not reply:
            messages.error(request, "Write a reply for the customer.")
        else:
            claim.seller_reply = reply
            claim.seller_offers_refund = request.POST.get("offer_refund") == "1"
            claim.seller_replied_at = timezone.now()
            if claim.status == "open":
                claim.status = "seller_replied"
            claim.save()
            link = reverse("claim_detail", args=[claim.id])
            _notify([claim.user], f"{seller.display_name} answered your report about '{claim.order_item.product_name}'.", link)
            if claim.seller_offers_refund:
                claim.status = "escalated"
                claim.save(update_fields=["status"])
                _staff(f"{seller.display_name} agreed to refund '{claim.order_item.product_name}' (claim #{claim.id}). Please process it.",
                       reverse("manage_claim", args=[claim.id]))
            messages.success(request, "Reply sent to the customer.")
        return redirect("seller_claims")
    show = request.GET.get("show", "open")
    listing = qs.filter(status__in=Claim.OPEN_STATUSES) if show == "open" else qs
    from .seller_views import _render
    return _render(request, "claims.html", {"section": "claims", "claims": listing[:100], "show": show,
                                            "open_count": qs.filter(status__in=Claim.OPEN_STATUSES).count()})


# ---------------------------------------------------------------------------
# Store team
# ---------------------------------------------------------------------------

def decide(claim, action, user, note="", as_credit=False, restock=False):
    """Refund or close a claim. Returns a message for the admin. Raises
    RefundError if the payment provider refuses."""
    from .refunds import describe, refund_return
    item = claim.order_item
    if action == "refund":
        rr = ReturnRequest.objects.filter(order_item=item).exclude(status="rejected").first()
        if rr is None:
            rr = ReturnRequest.objects.create(order_item=item, user=claim.user, status="approved",
                                              reason=f"Problem report #{claim.id}: {claim.get_reason_display()}. {claim.details}"[:2000],
                                              refund_method="store_credit" if as_credit else "original_payment")
        plan = refund_return(rr, as_credit=as_credit, restock=restock, note=note)
        claim.return_request = rr
        claim.status = "refunded"
        result = "Refunded: " + describe(plan) + "."
    else:
        claim.status = "rejected"
        result = "Report closed without a refund."
    claim.decision_note = note[:500]
    claim.decided_at = timezone.now()
    claim.save()
    link = reverse("claim_detail", args=[claim.id])
    verdict = "refunded" if claim.status == "refunded" else "closed without a refund"
    _notify([claim.user], f"Your report about '{item.product_name}' was {verdict}." + (f" {note}" if note else ""), link)
    _notify(_seller_users(item), f"Claim on '{item.product_name}' (order #{item.order_id}) was {verdict} by the store team.", reverse("seller_claims"))
    from .models import AuditLog
    AuditLog.objects.create(user=user, action=f"Claim #{claim.id}: {claim.status}"[:255])
    return result
