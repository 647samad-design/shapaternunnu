"""Request for quotation (B2B): buyers ask sellers for a price for larger
quantities, sellers offer, buyers accept (goes to the cart at that price),
counter or decline."""
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Notification, Product, QuoteRequest
from .ratelimit import ratelimit

OPEN_STATES = ("open", "offered", "accepted")


def _decimal(value):
    try:
        number = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return number.quantize(Decimal("0.01")) if number > 0 else None


def _min_qty(product):
    return max(2, product.order_limits()[0])


def _tell_seller(quote, message):
    link = f"/seller/quotes/#q{quote.id}" if quote.seller_account_id else f"/manage/quotes/#q{quote.id}"
    if quote.seller_account_id:
        from .models import OrganizationMember
        users = {quote.seller_account.user_id} | set(
            OrganizationMember.objects.filter(organization=quote.seller_account).values_list("user_id", flat=True))
        for uid in users:
            Notification.objects.create(user_id=uid, message=message[:255], link=link)
    else:
        from .alerts import notify_staff
        notify_staff(message, link=link)


def _tell_buyer(quote, message):
    Notification.objects.create(user_id=quote.buyer_id, message=message[:255], link=f"/account/quotes/#q{quote.id}")
    from .views import _send_html_email
    _send_html_email(message[:120], "bees/emails/quote_update.html", {"quote": quote, "text": message}, quote.buyer.email)


@login_required
@ratelimit("quote", rate_limit=10, window_seconds=3600, redirect_to="home",
           message="You've sent a lot of quote requests. Please wait a while and try again.")
def request_quote(request, pk):
    product = get_object_or_404(Product.objects.live(), pk=pk)
    if not product.quote_only:
        raise Http404
    if product.seller_account_id and product.seller_account.user_id == request.user.id:
        messages.error(request, "That's your own product.")
        return redirect("product_detail", pk=pk)
    low = _min_qty(product)
    variants = list(product.variants.all()) if product.has_variants else []
    form = {"quantity": max(low, 10), "company": "", "message": "", "target_price": "", "variant": ""}
    if request.method == "POST":
        form = {k: request.POST.get(k, "").strip() for k in form}
        error = None
        try:
            qty = int(form["quantity"])
        except ValueError:
            qty = 0
        variant = None
        if variants:
            variant = next((v for v in variants if str(v.pk) == form["variant"]), None)
            if variant is None:
                error = "Choose the size/colour you need."
        target = _decimal(form["target_price"]) if form["target_price"] else None
        if qty < low or qty > 1_000_000:
            error = f"Ask for at least {low} pieces."
        elif form["target_price"] and target is None:
            error = "Your target price must be a number above 0."
        elif QuoteRequest.objects.filter(buyer=request.user, product=product, status__in=("open", "offered")).exists():
            error = "You already have an open quote for this product - see My quotes."
        if error:
            messages.error(request, error)
        else:
            quote = QuoteRequest(product=product, variant=variant, buyer=request.user, seller_account=product.seller_account,
                                 quantity=qty, target_price=target, company=form["company"][:120], message=form["message"][:2000])
            quote.log("buyer", f"Asked for {qty} pcs" + (f" at {target} each" if target else "") + (f": {quote.message}" if quote.message else ""))
            quote.save()
            _tell_seller(quote, f"New quote request: {qty} x {product.name}")
            messages.success(request, "Quote request sent. You'll get a notification and an email when the seller replies.")
            return redirect("my_quotes")
    return render(request, "bees/quote_request.html", {"product": product, "form": form, "min_qty": low, "variants": variants})


@login_required
def my_quotes(request):
    if request.method == "POST":
        return _buyer_action(request)
    qs = QuoteRequest.objects.filter(buyer=request.user).select_related("product", "variant", "seller_account")
    page = Paginator(qs, 15).get_page(request.GET.get("page"))
    return render(request, "bees/my_quotes.html", {"page_obj": page, "quotes": page.object_list})


def _buyer_action(request):
    action = request.POST.get("action")
    with transaction.atomic():
        quote = get_object_or_404(QuoteRequest.objects.select_for_update(), pk=request.POST.get("id"), buyer=request.user)
        if action == "readd" and quote.status == "accepted" and quote.offer_valid:
            cart = request.session.get("cart", {})
            cart[f"q{quote.id}"] = quote.quantity
            request.session["cart"] = cart
            request.session.modified = True
            from .cart import persist
            persist(request)
            return redirect("cart")
        if action == "accept":
            if quote.status != "offered" or not quote.offer_valid:
                messages.error(request, "This offer can't be accepted any more. Ask the seller for a new one.")
                return redirect("my_quotes")
            if not quote.product.is_live:
                messages.error(request, "This product isn't for sale any more.")
                return redirect("my_quotes")
            stock = quote.variant.stock if quote.variant_id else quote.product.stock
            if quote.quantity > stock:
                messages.error(request, f"Only {stock} are in stock now - counter-offer a smaller quantity or ask the seller.")
                return redirect("my_quotes")
            quote.status = "accepted"
            quote.log("buyer", "Accepted the offer")
            quote.save()
            cart = request.session.get("cart", {})
            cart[f"q{quote.id}"] = quote.quantity
            request.session["cart"] = cart
            request.session.modified = True
            from .cart import persist
            persist(request)
            _tell_seller(quote, f"Quote accepted: {quote.quantity} x {quote.product.name} at {quote.offer_price}")
            messages.success(request, "Offer accepted and added to your cart at the agreed price.")
            return redirect("cart")
        if action == "counter":
            if quote.status not in ("offered", "open"):
                messages.error(request, "You can't change this quote any more.")
                return redirect("my_quotes")
            target = _decimal(request.POST.get("target_price", ""))
            try:
                qty = int(request.POST.get("quantity") or quote.quantity)
            except ValueError:
                qty = quote.quantity
            note = request.POST.get("note", "").strip()[:1000]
            if target is None or qty < _min_qty(quote.product):
                messages.error(request, f"Enter your price (above 0) and at least {_min_qty(quote.product)} pieces.")
                return redirect("my_quotes")
            quote.quantity, quote.target_price, quote.status = qty, target, "open"
            quote.log("buyer", f"Counter-offer: {qty} pcs at {target} each" + (f" - {note}" if note else ""))
            quote.save()
            _tell_seller(quote, f"Counter-offer on quote #{quote.id}: {qty} x {quote.product.name} at {target}")
            messages.success(request, "Counter-offer sent.")
        elif action == "cancel":
            if quote.status in OPEN_STATES:
                quote.status = "cancelled"
                quote.log("buyer", "Cancelled the request")
                quote.save()
                cart = request.session.get("cart", {})
                if cart.pop(f"q{quote.id}", None) is not None:
                    request.session["cart"] = cart
                    request.session.modified = True
                messages.success(request, "Quote cancelled.")
    return redirect("my_quotes")


# ---------------------------------------------------------------------------
# Seller Center and store admin
# ---------------------------------------------------------------------------

def _reply(request, qs, redirect_name, who):
    action = request.POST.get("action")
    with transaction.atomic():
        quote = get_object_or_404(qs.select_for_update(), pk=request.POST.get("id"))
        if action == "offer":
            if quote.status not in ("open", "offered"):
                messages.error(request, "This quote is closed.")
                return redirect(redirect_name)
            price = _decimal(request.POST.get("offer_price", ""))
            try:
                days = max(1, min(int(request.POST.get("valid_days") or 7), 90))
            except ValueError:
                days = 7
            if price is None:
                messages.error(request, "Enter your price per piece.")
                return redirect(redirect_name)
            quote.offer_price, quote.status = price, "offered"
            quote.offer_note = request.POST.get("offer_note", "").strip()[:2000]
            quote.offer_expires_at = timezone.now() + timedelta(days=days)
            quote.log(who, f"Offered {price} each, valid {days} days" + (f" - {quote.offer_note}" if quote.offer_note else ""))
            quote.save()
            _tell_buyer(quote, f"You have an offer for {quote.quantity} x {quote.product.name}: {price} each")
            messages.success(request, "Offer sent to the buyer.")
        elif action == "decline":
            if quote.status in ("open", "offered"):
                quote.status = "declined"
                quote.log(who, "Declined" + (f": {request.POST.get('offer_note', '').strip()[:500]}" if request.POST.get("offer_note") else ""))
                quote.save()
                _tell_buyer(quote, f"Your quote request for {quote.product.name} was declined")
                messages.success(request, "Quote declined.")
    return redirect(redirect_name)


TABS = [("open", "To answer"), ("offered", "Offer sent"), ("accepted", "Accepted"), ("ordered", "Ordered"), ("closed", "Closed")]


def _list(request, qs):
    tab = request.GET.get("tab", "open")
    counts = {t: (qs.filter(status__in=("declined", "cancelled")) if t == "closed" else qs.filter(status=t)).count() for t, _l in TABS}
    shown = qs.filter(status__in=("declined", "cancelled")) if tab == "closed" else qs.filter(status=tab if tab in dict(TABS) else "open")
    page = Paginator(shown.select_related("product", "variant", "buyer"), 15).get_page(request.GET.get("page"))
    return {"tabs": [(t, label, counts[t]) for t, label in TABS], "tab": tab, "page_obj": page, "quotes": page.object_list}


def seller_quotes(request):
    from .seller_views import _render
    qs = QuoteRequest.objects.filter(seller_account=request.seller)
    if request.method == "POST":
        return _reply(request, qs, "seller_quotes", "seller")
    return _render(request, "quotes.html", {"section": "quotes", **_list(request, qs)})


def manage_quotes(request):
    from .manage_views import _render
    qs = QuoteRequest.objects.filter(seller_account__isnull=True)
    if request.method == "POST":
        return _reply(request, qs, "manage_quotes", "store")
    return _render(request, "quotes.html", {"section": "quotes", **_list(request, qs)})


# ---------------------------------------------------------------------------
# Digital downloads and following stores
# ---------------------------------------------------------------------------

def download(request, item_id):
    """Buyer downloads a digital product they paid for. Signed-in owners,
    or guests with their order's private link (?t=token)."""
    import secrets
    from django.conf import settings
    from django.db.models import F
    from django.http import FileResponse
    from .models import OrderItem
    item = get_object_or_404(OrderItem.objects.select_related("order", "product"), pk=item_id)
    order = item.order
    owner = request.user.is_authenticated and order.user_id == request.user.id
    token = request.GET.get("t", "")
    guest = not order.user_id and token and secrets.compare_digest(token, order.access_token)
    if not (owner or guest):
        raise Http404
    if not item.is_digital or not item.product.digital_file:
        raise Http404
    if order.payment_status != "paid" or order.status == "cancelled":
        messages.error(request, "The download is ready as soon as the payment is confirmed.")
        return redirect("my_orders")
    if not OrderItem.objects.filter(pk=item.pk, downloads__lt=item.product.download_limit).update(downloads=F("downloads") + 1):
        messages.error(request, f"You've used all {item.product.download_limit} downloads. Message the seller if you need it again.")
        return redirect("my_orders")
    file = item.product.digital_file
    if getattr(settings, "USE_SUPABASE_STORAGE", False):
        return redirect(file.url)  # short-lived signed URL
    return FileResponse(file.open("rb"), as_attachment=True, filename=file.name.rsplit("/", 1)[-1])


@require_POST
@login_required
def follow_store(request, seller_id):
    from .models import SellerAccount, StoreFollow
    from .security import safe_next_url
    seller = get_object_or_404(SellerAccount, pk=seller_id, status="approved")
    follow, created = StoreFollow.objects.get_or_create(user=request.user, seller_account=seller)
    if not created:
        follow.delete()
        messages.success(request, f"You unfollowed {seller.display_name}.")
    else:
        messages.success(request, f"You follow {seller.display_name}. We'll tell you about their new products.")
    return redirect(safe_next_url(request, request.POST.get("next"), "/"))
