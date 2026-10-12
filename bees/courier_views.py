"""Pages for booking couriers (Seller Center and store admin) and the
courier webhook."""
import json
import secrets

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import couriers
from .fulfilment import blocked_reason
from .models import Order, OrderItem, Shipment


def _unshipped(items):
    booked = set()
    for s in Shipment.objects.filter(order_id=items[0].order_id).exclude(status__in=["rated", "cancelled"]) if items else []:
        booked.update(s.item_ids)
    return [i for i in items if i.fulfillment_status in ("pending", "packed") and i.pk not in booked]


def _rates(request, order, items, seller, back):
    reason = blocked_reason(order)
    if reason:
        messages.error(request, reason)
        return redirect(back)
    items = _unshipped(items)
    if not items:
        messages.info(request, "Everything in this order is already on its way.")
        return redirect(back)
    try:
        shipment = couriers.get_rates(order, items, seller, request.user)
    except couriers.CourierError as exc:
        messages.error(request, f"Courier: {exc}")
        return redirect(back)
    return redirect(f"{back}?shipment={shipment.id}#courier")


def _buy(request, shipment, back):
    try:
        couriers.buy(shipment, request.POST.get("rate", ""), user=request.user)
    except couriers.CourierError as exc:
        messages.error(request, f"Courier: {exc}")
        return redirect(f"{back}?shipment={shipment.id}#courier")
    messages.success(request, f"Booked with {shipment.carrier}. Tracking number {shipment.tracking_number}. Print the label and stick it on the parcel.")
    return redirect(f"{back}#courier")


# --- Seller Center ----------------------------------------------------------

def _seller(request):
    from .views import get_seller_account_for_user
    seller, _role = get_seller_account_for_user(request.user)
    if not seller or seller.status != "approved":
        raise Http404
    return seller


@login_required
@require_POST
def seller_rates(request, order_id):
    seller = _seller(request)
    items = list(OrderItem.objects.filter(order_id=order_id, seller_account=seller).select_related("product", "order"))
    if not items:
        raise Http404
    return _rates(request, items[0].order, items, seller, reverse("seller_order", args=[order_id]))


@login_required
@require_POST
def seller_buy(request, pk):
    seller = _seller(request)
    shipment = get_object_or_404(Shipment, pk=pk, seller=seller)
    return _buy(request, shipment, reverse("seller_order", args=[shipment.order_id]))


@login_required
def seller_label(request, pk):
    seller = _seller(request)
    shipment = get_object_or_404(Shipment, pk=pk, seller=seller)
    if not shipment.label_url:
        raise Http404
    return redirect(shipment.label_url)


# --- Store admin --------------------------------------------------------------

def _staff(request):
    if not request.user.is_staff:
        raise Http404


@login_required
@require_POST
def admin_rates(request, order_id):
    _staff(request)
    order = get_object_or_404(Order, pk=order_id)
    seller_id = request.POST.get("seller", "")
    items = list(order.items.select_related("product", "seller_account"))
    if seller_id == "store":
        items = [i for i in items if not i.seller_account_id]
        seller = None
    else:
        items = [i for i in items if str(i.seller_account_id) == seller_id]
        seller = items[0].seller_account if items else None
    if not items:
        messages.error(request, "Nothing to ship for that seller.")
        return redirect("manage_order", pk=order_id)
    return _rates(request, order, items, seller, reverse("manage_order", args=[order_id]))


@login_required
@require_POST
def admin_buy(request, pk):
    _staff(request)
    shipment = get_object_or_404(Shipment, pk=pk)
    return _buy(request, shipment, reverse("manage_order", args=[shipment.order_id]))


@login_required
def admin_label(request, pk):
    _staff(request)
    shipment = get_object_or_404(Shipment, pk=pk)
    if not shipment.label_url:
        raise Http404
    return redirect(shipment.label_url)


@login_required
@require_POST
def admin_refresh(request, pk):
    _staff(request)
    shipment = get_object_or_404(Shipment, pk=pk)
    try:
        couriers.refresh(shipment)
        messages.success(request, f"Tracking updated: {shipment.get_status_display()}.")
    except couriers.CourierError as exc:
        messages.error(request, f"Courier: {exc}")
    return redirect(f"{reverse('manage_order', args=[shipment.order_id])}#courier")


# --- Webhook --------------------------------------------------------------------

@csrf_exempt
@require_POST
def webhook(request, token):
    """Courier status update. The URL carries a secret token; the body is
    only used to find which parcel changed - we then ask the courier for
    the real status with our own API key."""
    expected = couriers.setting("SHIPPING_WEBHOOK_TOKEN")
    if not expected or not secrets.compare_digest(token, expected):
        raise Http404
    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        return HttpResponse(status=400)
    result = payload.get("result") or payload.get("data") or {}
    keys = {str(v) for v in (result.get("id"), result.get("tracking_code"), result.get("tracking_number")) if v}
    shipment = (Shipment.objects.filter(tracker_id__in=keys).first()
                or Shipment.objects.filter(tracking_number__in=keys).first()) if keys else None
    if shipment:
        try:
            couriers.refresh(shipment)
        except couriers.CourierError:
            return HttpResponse(status=502)  # the courier will retry
    return HttpResponse(status=200)
