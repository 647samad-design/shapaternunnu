"""Pages for the marketplace features the big sites have (see market.py)."""
import secrets
import time
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import market
from .models import (Coupon, CouponClaim, MembershipPayment, Notification, Product, QuoteRequest, Review,
                     ReviewVote, SiteSettings, Subscription, Wishlist)
from .ratelimit import ratelimit
from .security import safe_next_url


def _decimal(value):
    try:
        number = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return number.quantize(Decimal("0.01")) if number > 0 else None


def _back(request, fallback="/"):
    return redirect(safe_next_url(request, request.POST.get("next") or request.META.get("HTTP_REFERER"), fallback))


def _add_to_cart(request, key, qty):
    cart = request.session.get("cart", {})
    cart[key] = qty
    request.session["cart"] = cart
    request.session.modified = True
    from .cart import persist
    persist(request)


# ---------------------------------------------------------------------------
# Reviews: helpful votes
# ---------------------------------------------------------------------------

@require_POST
@login_required
def review_helpful(request, review_id):
    review = get_object_or_404(Review, pk=review_id)
    if review.user_id == request.user.id:
        messages.info(request, "You can't vote on your own review.")
    else:
        try:
            with transaction.atomic():
                ReviewVote.objects.create(review=review, user=request.user)
                Review.objects.filter(pk=review.pk).update(helpful_count=F("helpful_count") + 1)
        except IntegrityError:
            ReviewVote.objects.filter(review=review, user=request.user).delete()
            Review.objects.filter(pk=review.pk, helpful_count__gt=0).update(helpful_count=F("helpful_count") - 1)
    return redirect(reverse("product_detail", args=[review.product_id]) + "#reviews")


# ---------------------------------------------------------------------------
# Make an offer and auctions
# ---------------------------------------------------------------------------

@login_required
@ratelimit("offer", rate_limit=10, window_seconds=3600, redirect_to="home",
           message="You've made a lot of offers. Please wait a while and try again.")
def make_offer(request, pk):
    from .quote_views import _tell_buyer, _tell_seller
    product = get_object_or_404(Product.objects.live(), pk=pk)
    if not product.accepts_offers or product.is_auction:
        raise Http404
    if product.seller_account_id and product.seller_account.user_id == request.user.id:
        messages.error(request, "That's your own product.")
        return redirect("product_detail", pk=pk)
    variants = list(product.variants.all()) if product.has_variants else []
    if request.method == "POST":
        price = _decimal(request.POST.get("price", ""))
        try:
            qty = max(1, min(int(request.POST.get("quantity") or 1), 1000))
        except ValueError:
            qty = 1
        variant = next((v for v in variants if str(v.pk) == request.POST.get("variant")), None) if variants else None
        error = None
        if price is None:
            error = "Enter the price you'd like to pay."
        elif variants and variant is None:
            error = "Choose the size/colour."
        elif price >= product.price:
            error = f"That's not below the price ({product.price}) - just buy it now."
        elif QuoteRequest.objects.filter(buyer=request.user, product=product, status__in=("open", "offered")).exists():
            error = "You already have an offer waiting on this item - see My quotes."
        if error:
            messages.error(request, error)
            return render(request, "bees/make_offer.html", {"product": product, "variants": variants, "form": request.POST})
        quote = QuoteRequest(product=product, variant=variant, buyer=request.user, seller_account=product.seller_account,
                             kind="offer", quantity=qty, target_price=price, message=request.POST.get("message", "").strip()[:1000])
        quote.log("buyer", f"Offered {price} each for {qty}")
        if product.offer_floor and price < product.offer_floor:
            quote.status = "declined"
            quote.log("system", "Automatically declined: below the seller's minimum")
            quote.save()
            messages.info(request, "Sorry - the seller doesn't accept offers that low. Try a higher price.")
            return redirect("product_detail", pk=pk)
        quote.save()
        _tell_seller(quote, f"New offer: {price} for {product.name}")
        messages.success(request, "Offer sent. You'll hear back when the seller answers (usually within 48 hours).")
        return redirect("my_quotes")
    return render(request, "bees/make_offer.html", {"product": product, "variants": variants, "form": {}})


@require_POST
@login_required
@ratelimit("bid", rate_limit=30, window_seconds=600, redirect_to="home")
def place_bid(request, pk):
    amount = _decimal(request.POST.get("amount", ""))
    try:
        bid = market.place_bid(request.user, pk, amount)
        messages.success(request, f"You're the highest bidder at {bid.amount}.")
    except market.BidError as exc:
        messages.error(request, str(exc))
    except Product.DoesNotExist:
        raise Http404
    return redirect(reverse("product_detail", args=[pk]) + "#auction")


# ---------------------------------------------------------------------------
# Subscribe & Save
# ---------------------------------------------------------------------------

@require_POST
@login_required
def subscribe(request, pk):
    product = get_object_or_404(Product.objects.live(), pk=pk)
    if not product.subscribe_percent:
        raise Http404
    variant = None
    if product.has_variants:
        variant = product.variants.filter(pk=request.POST.get("variant") or 0).first()
        if variant is None:
            messages.error(request, "Choose a size/colour first.")
            return redirect("product_detail", pk=pk)
    try:
        weeks = int(request.POST.get("every_weeks") or 4)
        qty = max(1, min(int(request.POST.get("quantity") or 1), 20))
    except ValueError:
        weeks, qty = 4, 1
    if weeks not in (1, 2, 4, 8, 12):
        weeks = 4
    sub = Subscription.objects.create(user=request.user, product=product, variant=variant, quantity=qty, every_weeks=weeks,
                                      next_date=timezone.localdate() + timedelta(weeks=weeks))
    _add_to_cart(request, f"s{sub.id}", qty)
    messages.success(request, f"Subscribed: {product.subscribe_percent}% off this order and every {weeks} weeks after. "
                              "We'll remind you before each one - change or cancel anytime.")
    return redirect("cart")


@login_required
def subscriptions(request):
    subs = Subscription.objects.filter(user=request.user).select_related("product", "variant")
    if request.method == "POST":
        sub = get_object_or_404(subs, pk=request.POST.get("id"))
        action = request.POST.get("action")
        if action == "cancel":
            sub.active = False
            sub.save(update_fields=["active"])
            messages.success(request, "Subscription cancelled.")
        elif action == "resume":
            sub.active = True
            sub.next_date = max(sub.next_date, timezone.localdate())
            sub.save(update_fields=["active", "next_date"])
            messages.success(request, "Subscription restarted.")
        elif action == "change":
            try:
                weeks = int(request.POST.get("every_weeks"))
                qty = max(1, min(int(request.POST.get("quantity") or sub.quantity), 20))
            except (TypeError, ValueError):
                weeks, qty = sub.every_weeks, sub.quantity
            if weeks in (1, 2, 4, 8, 12):
                sub.every_weeks, sub.quantity = weeks, qty
                sub.save(update_fields=["every_weeks", "quantity"])
                messages.success(request, "Subscription updated.")
        elif action == "order_now":
            _add_to_cart(request, f"s{sub.id}", sub.quantity)
            return redirect("cart")
        return redirect("subscriptions")
    rows = [(s, market.subscription_price(s)) for s in subs]
    return render(request, "bees/subscriptions.html", {"rows": rows, "tab": "subscriptions"})


@login_required
def subscription_reorder(request, sub_id):
    sub = get_object_or_404(Subscription, pk=sub_id, user=request.user, active=True)
    _add_to_cart(request, f"s{sub.id}", sub.quantity)
    messages.success(request, f"Your {sub.product.name} refill is in your cart at the subscription price.")
    return redirect("cart")


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------

def membership(request):
    site = SiteSettings.load()
    if not site.membership_enabled:
        raise Http404
    from . import payments
    profile = getattr(request.user, "profile", None) if request.user.is_authenticated else None
    if request.method == "POST":
        if not request.user.is_authenticated:
            return redirect(f"{reverse('login')}?next={reverse('membership')}")
        if not payments.is_configured():
            messages.error(request, "Card payments aren't set up yet, so membership can't be bought right now.")
            return redirect("membership")
        pay = MembershipPayment.objects.create(user=request.user, amount=site.membership_price, days=site.membership_days)
        stripe = payments._stripe()
        currency = payments.settings.STORE_CURRENCY.lower()
        try:
            session = stripe.checkout.Session.create(
                idempotency_key=f"member-{pay.id}-{uuid.uuid4().hex}", mode="payment",
                line_items=[{"quantity": 1, "price_data": {"currency": currency, "unit_amount": payments.to_cents(pay.amount, currency),
                                                           "product_data": {"name": f"{site.site_name} {site.membership_name} - {pay.days} days"}}}],
                client_reference_id=f"member-{pay.id}", metadata={"membership_payment_id": str(pay.id)},
                customer_email=request.user.email or None,
                success_url=request.build_absolute_uri(reverse("membership_done", args=[pay.id])) + "?session_id={CHECKOUT_SESSION_ID}",
                cancel_url=request.build_absolute_uri(reverse("membership")),
                expires_at=int(time.time()) + payments.CHECKOUT_SESSION_LIFETIME_SECONDS)
        except Exception:
            messages.error(request, "Card payment is temporarily unavailable. Please try again in a few minutes.")
            return redirect("membership")
        MembershipPayment.objects.filter(pk=pay.pk).update(stripe_session_id=session.id)
        return redirect(session.url)
    return render(request, "bees/membership.html", {
        "site": site, "is_member": market.is_member(request.user), "member_until": getattr(profile, "member_until", None)})


@login_required
def membership_done(request, pay_id):
    from . import payments
    pay = get_object_or_404(MembershipPayment, pk=pay_id, user=request.user)
    if pay.status != "paid":
        session_id = request.GET.get("session_id", "")
        try:
            session = payments._stripe().checkout.Session.retrieve(session_id) if session_id == pay.stripe_session_id else None
        except Exception:
            session = None
        if session and session.get("payment_status") == "paid" and \
                int(session.get("amount_total") or 0) >= payments.to_cents(pay.amount, payments.settings.STORE_CURRENCY.lower()):
            with transaction.atomic():
                if MembershipPayment.objects.filter(pk=pay.pk, status="pending").update(status="paid", paid_at=timezone.now()):
                    until = market.extend_membership(request.user, pay.days)
                    Notification.objects.create(user=request.user, link="/membership/",
                                                message=f"Welcome to {SiteSettings.load().membership_name}! Free shipping until {until:%d %b %Y}.")
            messages.success(request, "Payment received - your membership is active.")
        else:
            messages.info(request, "We're waiting for the payment confirmation. Refresh in a moment.")
    return redirect("membership")


# ---------------------------------------------------------------------------
# Daily check-in coins
# ---------------------------------------------------------------------------

@require_POST
@login_required
def check_in(request):
    coins, streak = market.check_in(request.user)
    if coins:
        messages.success(request, f"+{coins} coins! Day {streak} in a row" + (" - double coins for a full week!" if streak % 7 == 0 else ".")
                         + " Coins become store credit on your profile.")
    else:
        messages.info(request, "You've already collected today's coins. Come back tomorrow!")
    return _back(request)


# ---------------------------------------------------------------------------
# Search by photo
# ---------------------------------------------------------------------------

@require_POST
@ratelimit("photo_search", rate_limit=10, window_seconds=600, redirect_to="home",
           message="Too many photo searches. Please wait a few minutes.")
def photo_search(request):
    from django.core.exceptions import ValidationError
    from .security import validate_image_upload
    f = request.FILES.get("photo")
    if not f or not market.image_search_enabled():
        messages.error(request, "Choose a photo to search with.")
        return _back(request)
    try:
        validate_image_upload(f)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return _back(request)
    from .images import optimize
    opt = optimize(f, 800)
    opt.seek(0)
    media_type = getattr(opt, "content_type", "") or "image/jpeg"
    query = market.describe_image(opt.read(), media_type if media_type in ("image/jpeg", "image/png", "image/webp", "image/gif") else "image/jpeg")
    if not query:
        messages.error(request, "We couldn't recognise that photo. Try another one, or type what you're looking for.")
        return _back(request)
    from urllib.parse import urlencode
    return redirect(f"{reverse('search_products')}?{urlencode({'q': query, 'from_photo': 1})}")


# ---------------------------------------------------------------------------
# Voucher centre
# ---------------------------------------------------------------------------

def _public_coupons():
    today = timezone.localdate()
    return (Coupon.objects.filter(is_public=True, active=True).filter(Q(expiry_date__isnull=True) | Q(expiry_date__gte=today))
            .select_related("seller_account").order_by("-percent_off"))


def vouchers(request):
    coupons = list(_public_coupons()[:60])
    mine = set(CouponClaim.objects.filter(user=request.user).values_list("coupon_id", flat=True)) if request.user.is_authenticated else set()
    return render(request, "bees/vouchers.html", {"coupons": coupons, "collected": mine})


@require_POST
@login_required
def collect_voucher(request, coupon_id):
    coupon = get_object_or_404(_public_coupons(), pk=coupon_id)
    CouponClaim.objects.get_or_create(user=request.user, coupon=coupon)
    messages.success(request, f"Voucher {coupon.code} collected - choose it at checkout.")
    return _back(request, reverse("vouchers"))


# ---------------------------------------------------------------------------
# Wishlists: save for later, lists and sharing
# ---------------------------------------------------------------------------

@require_POST
@login_required
def save_for_later(request, key):
    from .cart import lines
    cart = request.session.get("cart", {})
    row = next(iter(lines({key: cart[key]})), None) if key in cart else None
    if row:
        Wishlist.objects.get_or_create(user=request.user, product=row["product"], defaults={"list_name": "Saved for later"})
        del cart[key]
        request.session["cart"] = cart
        request.session.modified = True
        from .cart import persist
        persist(request)
        messages.success(request, f"{row['product'].name} moved to Saved for later.")
    return redirect("cart")


@require_POST
@login_required
def wishlist_manage(request):
    action = request.POST.get("action")
    if action == "move":
        item = get_object_or_404(Wishlist, pk=request.POST.get("id"), user=request.user)
        item.list_name = " ".join(request.POST.get("list_name", "").split())[:40]
        item.save(update_fields=["list_name"])
    elif action == "to_cart":
        item = get_object_or_404(Wishlist, pk=request.POST.get("id"), user=request.user)
        if item.product.is_live and not item.product.has_variants and item.product.stock > 0 and not item.product.is_auction:
            cart = request.session.get("cart", {})
            cart[str(item.product_id)] = cart.get(str(item.product_id), 0) + max(1, item.product.order_limits()[0])
            request.session["cart"] = cart
            request.session.modified = True
            from .cart import persist
            persist(request)
            item.delete()
            messages.success(request, f"{item.product.name} moved to your cart.")
        else:
            return redirect("product_detail", pk=item.product_id)
    elif action in ("share", "unshare"):
        from .models import Profile
        profile, _ = Profile.objects.get_or_create(user=request.user)
        profile.wishlist_token = secrets.token_urlsafe(12)[:16] if action == "share" else ""
        profile.save(update_fields=["wishlist_token"])
        messages.success(request, "Your wishlist link is ready - copy it below." if action == "share" else "Your wishlist is private again.")
    return redirect("wishlist")


def shared_wishlist(request, token):
    from .models import Profile
    if len(token) < 10:
        raise Http404
    profile = get_object_or_404(Profile.objects.select_related("user"), wishlist_token=token)
    items = Wishlist.objects.filter(user=profile.user).select_related("product").filter(product__in=Product.objects.live())
    name = profile.user.first_name or "A shopper"
    return render(request, "bees/wishlist_shared.html", {"items": items, "owner": name})


@require_POST
def add_many(request):
    """'Frequently bought together': adds the ticked products to the cart."""
    cart = request.session.get("cart", {})
    added = 0
    ids = [int(i) for i in request.POST.getlist("ids") if i.isdigit()][:5]
    for product in Product.objects.live().filter(pk__in=ids, has_variants=False, is_auction=False, stock__gt=0):
        if product.personalization_required:
            continue
        key = str(product.pk)
        cart[key] = min(product.stock, max(cart.get(key, 0) + 1, product.order_limits()[0]))
        added += 1
    request.session["cart"] = cart
    request.session.modified = True
    from .cart import persist
    persist(request)
    if added:
        messages.success(request, f"Added {added} item{'s' if added != 1 else ''} to your cart.")
    return redirect("cart")


@require_POST
@login_required
def price_alert(request, pk):
    from .models import PriceAlert
    product = get_object_or_404(Product.objects.live(), pk=pk)
    if request.POST.get("action") == "remove":
        PriceAlert.objects.filter(user=request.user, product=product).delete()
        messages.info(request, "Price alert removed.")
    else:
        target = _decimal(request.POST.get("target_price", ""))
        if target is None or target >= product.price:
            messages.error(request, f"Choose a price below the current {product.price}.")
        else:
            PriceAlert.objects.update_or_create(user=request.user, product=product,
                                                defaults={"target_price": target, "notified_at": None})
            messages.success(request, f"We'll tell you when {product.name} drops to {target} or less.")
    return redirect(reverse("product_detail", args=[pk]))


# ---------------------------------------------------------------------------
# Deals page and AI shopping assistant
# ---------------------------------------------------------------------------

def deals(request):
    from .views import with_ratings
    from .categories import top_level, with_children
    cat = request.GET.get("cat", "")
    base = Product.objects.listed().filter(stock__gt=0)
    if cat:
        base = base.filter(category__in=with_children(cat))
    flash = with_ratings(base.filter(is_flash_sale=True).filter(Q(flash_sale_ends__isnull=True) | Q(flash_sale_ends__gt=timezone.now()))
                         .order_by("flash_sale_ends"))[:12]
    discounts = with_ratings(base.filter(discount_percent__gt=0).order_by("-discount_percent"))[:24]
    auctions = with_ratings(base.filter(is_auction=True, auction_closed=False, auction_ends_at__gt=timezone.now()).order_by("auction_ends_at"))[:8]
    vouchers_count = _public_coupons().count()
    return render(request, "bees/deals.html", {"flash": flash, "discounts": discounts, "auctions": auctions, "cat": cat,
                                               "cats": top_level(), "vouchers_count": vouchers_count})


@ratelimit("shop_assistant", rate_limit=20, window_seconds=600, redirect_to="home", methods=("POST",),
           message="You've asked a lot of questions. Please wait a few minutes.")
def shop_assistant(request):
    """'Ask Lumen': describe what you need, get matching products and a
    short answer (written by Claude when the AI key is set)."""
    from . import shopping_ai
    question = (request.POST.get("q") or "").strip()[:300]
    result = shopping_ai.answer(question, request) if question else None
    return render(request, "bees/assistant.html", {"question": question, "result": result,
                                                   "examples": shopping_ai.EXAMPLES})


def _can_list(request):
    from .views import get_seller_account_for_user
    if not request.user.is_authenticated:
        return False
    if request.user.is_staff:
        return True
    seller, _role = get_seller_account_for_user(request.user)
    return bool(seller and seller.status == "approved")


@require_POST
@ratelimit("listing_photo", rate_limit=30, window_seconds=3600, redirect_to="home")
def listing_from_photo(request):
    from django.core.exceptions import ValidationError
    from django.http import JsonResponse
    from .categories import choices
    from .images import optimize
    from .security import validate_image_upload
    if not _can_list(request):
        return JsonResponse({"error": "Only approved sellers and store staff can use this."}, status=403)
    f = request.FILES.get("photo")
    if not f:
        return JsonResponse({"error": "Choose a photo."}, status=400)
    if not market.image_search_enabled():
        return JsonResponse({"error": "The AI helper isn't turned on for this store (add the AI key under Keys & passwords)."}, status=400)
    try:
        validate_image_upload(f)
    except ValidationError as exc:
        return JsonResponse({"error": " ".join(exc.messages)}, status=400)
    opt = optimize(f, 900)
    opt.seek(0)
    media = getattr(opt, "content_type", "") or "image/jpeg"
    draft = market.listing_from_photo(opt.read(), media if media in ("image/jpeg", "image/png", "image/webp") else "image/jpeg", choices())
    if not draft:
        return JsonResponse({"error": "We couldn't read that photo. Try a clearer one, on a plain background."}, status=422)
    return JsonResponse({"draft": draft})


def price_tips(request):
    from django.http import JsonResponse
    from .templatetags.bees_extras import money
    if not _can_list(request):
        return JsonResponse({}, status=403)
    pid = request.GET.get("product", "")
    tips = market.price_tips(request.GET.get("category", ""), int(pid) if pid.isdigit() else None)
    if not tips:
        return JsonResponse({"tips": None})
    return JsonResponse({"tips": {k: (money(v) if k != "count" else v) for k, v in tips.items()}})
