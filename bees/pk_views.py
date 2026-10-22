"""Pages for JazzCash and Easypaisa / bank transfer payments (pk_payments.py)."""
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import pk_payments
from .ratelimit import ratelimit


@csrf_exempt
def jazzcash_return(request):
    """JazzCash posts the signed result here. Nothing is trusted until the
    signature and amount check out. This cross-site POST arrives without
    the shopper's cookies, so the session isn't touched here: the result
    is handed to jazzcash_done in a signed link."""
    from django.core import signing
    data = request.POST if request.method == "POST" else request.GET
    if not data.get("pp_TxnRefNo"):
        raise Http404
    order, state = pk_payments.handle_response({k: v for k, v in data.items()})
    if order is None:
        return HttpResponse("This payment reply couldn't be verified.", status=400, content_type="text/plain")
    if state == "failed":
        from . import payments
        payments.release_unpaid_order(order.id)
    token = signing.dumps({"o": order.id, "s": state}, salt="jazzcash-done")
    return redirect(reverse("jazzcash_done") + "?d=" + token, permanent=False)


def jazzcash_done(request):
    from django.core import signing
    from .models import Order
    try:
        data = signing.loads(request.GET.get("d", ""), salt="jazzcash-done", max_age=3600)
    except signing.BadSignature:
        raise Http404
    order = get_object_or_404(Order, pk=data["o"])
    state = data["s"]
    token = f"?t={order.access_token}" if order.is_guest else ""
    if state == "paid":
        return redirect(reverse("order_success", args=[order.id]) + token)
    if state == "pending":
        messages.info(request, "JazzCash is waiting for your payment (for example a voucher to pay at a shop). The order is confirmed as soon as it's paid.")
    elif state == "failed":
        owner = (request.user.is_authenticated and order.user_id == request.user.id) or order.pk in request.session.get("guest_orders", [])
        done = request.session.get("jc_restored", [])
        if owner and order.pk not in done:
            from .views import _restore_cart_from_order
            _restore_cart_from_order(request, order)
            request.session["jc_restored"] = (done + [order.pk])[-20:]
        messages.error(request, "The JazzCash payment wasn't completed. Your cart is back - try again or choose another way to pay.")
        return redirect("cart")
    else:
        messages.error(request, "We couldn't confirm this JazzCash payment. If money was taken, our team will check it.")
    return redirect(reverse("my_orders") if order.user_id else order.tracking_path())


@ratelimit("pay_transfer", rate_limit=8, window_seconds=600)
def pay_transfer(request, order_id):
    from .views import _order_access
    from .security import validate_image_upload
    order = _order_access(request, order_id)
    if order.payment_method != "transfer":
        raise Http404
    if request.method == "POST" and order.payment_status == "pending" and order.status == "pending":
        try:
            proof = request.FILES.get("proof")
            if proof is not None:
                validate_image_upload(proof)
            pk_payments.submit_transfer(order, request.POST.get("account", ""), request.POST.get("reference", ""), proof)
            messages.success(request, "Thanks! We're checking your payment and will confirm the order shortly.")
        except (ValueError, ValidationError) as exc:
            messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))
        token = f"?t={order.access_token}" if order.is_guest else ""
        return redirect(reverse("pay_transfer", args=[order.id]) + token)
    from .models import SiteSettings
    return render(request, "bees/pay_transfer.html", {
        "order": order, "accounts": pk_payments.transfer_accounts(), "hours": SiteSettings.load().transfer_hours,
        "token": order.access_token if order.is_guest else "",
    })


def transfer_proof(request, order_id):
    from .models import Order
    order = get_object_or_404(Order, pk=order_id)
    if not request.user.is_authenticated or not (request.user.is_staff or order.user_id == request.user.id) or not order.transfer_proof:
        raise Http404
    from django.conf import settings
    if getattr(settings, "USE_SUPABASE_STORAGE", False):
        return redirect(order.transfer_proof.url)
    from django.http import FileResponse
    response = FileResponse(order.transfer_proof.open("rb"), content_type="image/jpeg")
    response["Cache-Control"] = "private, max-age=3600"
    return response


def manage_transfers(request):
    from .manage_views import _render
    from .models import Order
    if request.method == "POST":
        order = get_object_or_404(Order, pk=request.POST.get("order"), payment_method="transfer")
        if request.POST.get("action") == "approve":
            if pk_payments.approve_transfer(order, request.user):
                messages.success(request, f"Order #{order.id} is paid and confirmed. The customer and seller have been told.")
            else:
                messages.info(request, f"Order #{order.id} was already confirmed.")
        elif request.POST.get("action") == "reject":
            pk_payments.reject_transfer(order, request.user, request.POST.get("reason", ""))
            messages.success(request, f"Order #{order.id}: the customer was asked to check the payment.")
        return redirect("manage_transfers")
    waiting = Order.objects.filter(payment_method="transfer", payment_status="pending", status="pending").order_by("transfer_submitted_at", "created_at")
    return _render(request, "transfers.html", {
        "section": "orders", "to_check": [o for o in waiting if o.transfer_submitted_at],
        "not_sent": [o for o in waiting if not o.transfer_submitted_at][:50],
        "recent": Order.objects.filter(payment_method="transfer", payment_status="paid").order_by("-created_at")[:20],
        "accounts": pk_payments.transfer_accounts(),
    })


# ---------------------------------------------------------------- WhatsApp

@csrf_exempt
@require_POST
def whatsapp_incoming(request):
    """Twilio forwards WhatsApp messages here; the answer goes back as TwiML."""
    from . import whatsapp
    from .order_emails import absolute
    if not whatsapp.valid_signature(request):
        return HttpResponse(status=403)
    phone = request.POST.get("From", "").replace("whatsapp:", "")
    text = whatsapp.reply_for(phone, request.POST.get("Body", ""), absolute)
    return HttpResponse(whatsapp.twiml(text), content_type="text/xml")


@ratelimit("wa_cart", rate_limit=20, window_seconds=600, methods=("GET",))
def whatsapp_cart(request, token):
    """One-tap link from WhatsApp: puts the products in the cart and opens it."""
    from django.core import signing
    from . import whatsapp
    from .cart import make_key
    from .models import Product, ProductVariant
    try:
        items = whatsapp.read_token(token)
    except signing.BadSignature:
        messages.error(request, "That link has expired. Send us the products again on WhatsApp for a new one.")
        return redirect("home")
    cart = request.session.get("cart", {})
    added = 0
    for pid, vid, qty in items:
        product = Product.objects.live().filter(pk=pid).first()
        if not product or product.quote_only:
            continue
        variant = ProductVariant.objects.filter(pk=vid, product=product).first() if vid else None
        if product.has_variants and not variant:
            continue
        stock = variant.stock if variant else product.stock
        qty = max(product.min_order_qty or 1, min(int(qty), stock or 0, product.max_order_qty or 99))
        if qty > stock:
            continue
        if not stock:
            continue
        cart[make_key(pid, variant.pk if variant else None)] = qty
        added += 1
    request.session["cart"] = cart
    from .views import _persist_cart
    _persist_cart(request)
    if added:
        messages.success(request, "Your WhatsApp order is in the cart. Check it and continue to checkout.")
    else:
        messages.error(request, "Sorry, those products are sold out or no longer available.")
    return redirect("cart")
