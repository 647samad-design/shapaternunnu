import logging
import random
import secrets
import string
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum, Avg, Count, F
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.html import strip_tags
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import ai as ai_tools, order_emails, payments, tracking
from .models import (
    Product, Review, Order, OrderItem, Wishlist, Coupon,
    ProductImage, Profile, Address, Question, NewsletterSubscriber,
    Notification, SearchLog, SellerAccount, SellerReview, ReturnRequest, SiteSettings, AuditLog,
    OrganizationMember, ChatThread, ChatMessage, ProductVariant, OrderEvent,
)
from .cart import persist as _persist_cart
from .images import BANNER_MAX, LOGO_MAX, optimize
from .ratelimit import ratelimit
from .security import (
    safe_next_url, redirect_back, validate_image_upload, validate_document_upload, random_upload_name,
)
from .templatetags.bees_extras import country_name, money, price as display_price
from .categories import all_rows as category_rows, choices as category_choices

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def with_ratings(queryset):
    """Annotate a Product queryset with avg_rating/review_count in one query,
    instead of each product template tag hitting the DB separately (N+1)."""
    return queryset.select_related("seller_account").annotate(
        avg_rating=Avg("reviews__rating"),
        review_count=Count("reviews", distinct=True),
    )


def get_seller_account_for_user(user):
    """Returns the SellerAccount this user can act on behalf of - either
    because they own it, or because an organization added them as a team
    member. Used so seller dashboard/product/order views work the same way
    for both the account owner and any team member."""
    account = SellerAccount.objects.filter(user=user).first()
    if account:
        return account, "owner"
    membership = user.organization_memberships.select_related("organization").first()
    if membership:
        return membership.organization, membership.role
    return None, None


def is_speculative(request):
    """True for browser prefetch/prerender requests (sent when a visitor
    hovers a link). Those shouldn't count as views or searches."""
    return request.headers.get("Sec-Purpose", "").startswith("prefetch") or request.headers.get("Purpose") == "prefetch"


def _brand():
    return SiteSettings.load()


def _store_name():
    return _brand().site_name


def _parse_decimal(value, field, minimum=Decimal("0"), required=True):
    if value in (None, ""):
        if required:
            raise ValidationError(f"{field} is required.")
        return None
    try:
        number = Decimal(str(value).strip()).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise ValidationError(f"{field} must be a number.")
    if number < minimum:
        raise ValidationError(f"{field} must be at least {minimum}.")
    return number


def _parse_int(value, field, minimum=0, maximum=None, default=0):
    if value in (None, ""):
        return default
    try:
        number = int(str(value).strip())
    except ValueError:
        raise ValidationError(f"{field} must be a whole number.")
    if number < minimum or (maximum is not None and number > maximum):
        raise ValidationError(f"{field} must be between {minimum} and {maximum}." if maximum is not None else f"{field} must be at least {minimum}.")
    return number


def _apply_sort(qs, sort):
    if sort == "price_asc":
        return qs.order_by("price")
    if sort == "price_desc":
        return qs.order_by("-price")
    if sort == "newest":
        return qs.order_by("-created_at")
    if sort == "rating":
        return qs.order_by(F("avg_rating").desc(nulls_last=True), "-id")
    return qs.order_by("-id")


def _apply_filters(qs, request):
    """Applies price range, minimum rating, seller and in-stock filters
    from GET params, shared across all_products/category_products/search."""
    min_price = request.GET.get("min_price")
    max_price = request.GET.get("max_price")
    min_rating = request.GET.get("min_rating")
    seller = request.GET.get("seller")
    in_stock = request.GET.get("in_stock")

    if min_price:
        try:
            qs = qs.filter(price__gte=Decimal(min_price))
        except (InvalidOperation, ValueError):
            pass
    if max_price:
        try:
            qs = qs.filter(price__lte=Decimal(max_price))
        except (InvalidOperation, ValueError):
            pass
    if seller:
        qs = qs.filter(seller_name=seller)
    brands = [b for b in request.GET.getlist("brand") if b][:20]
    if brands:
        qs = qs.filter(brand__in=brands)
    for kind in ("color", "size"):
        picked = [v for v in request.GET.getlist(kind) if v][:20]
        if picked:
            q = Q()
            for value in picked:
                q |= Q(**{f"{kind}__iexact": value})
            # A subquery, not a join: keeps the rating counts right.
            qs = qs.filter(id__in=ProductVariant.objects.filter(q, stock__gt=0).values("product_id"))
    if request.GET.get("digital"):
        qs = qs.filter(is_digital=True)
    if in_stock:
        qs = qs.filter(stock__gt=0)
    if min_rating:
        try:
            qs = qs.filter(avg_rating__gte=float(min_rating))
        except ValueError:
            pass
    return qs


SIZE_ORDER = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "2XL", "XXXL", "3XL", "4XL"]


def _sort_sizes(sizes):
    def key(size):
        up = size.upper()
        if up in SIZE_ORDER:
            return (0, SIZE_ORDER.index(up), "")
        try:
            return (1, float(size.replace(",", ".")), "")
        except ValueError:
            return (2, 0, up)
    return sorted(sizes, key=key)


def _filter_context(request, base_qs):
    """Sellers list for the filter sidebar, and the current filter values
    so the form and pagination links can stay populated across requests."""
    ids = base_qs.values("id")
    brands = list(base_qs.exclude(brand="").order_by("brand").values_list("brand", flat=True).distinct()[:40])
    colors = sorted({c.strip().capitalize() for c in ProductVariant.objects.filter(product_id__in=ids, stock__gt=0)
                     .exclude(color="").values_list("color", flat=True).distinct()[:200]})[:30]
    sizes = _sort_sizes({s.strip() for s in ProductVariant.objects.filter(product_id__in=ids, stock__gt=0)
                         .exclude(size="").values_list("size", flat=True).distinct()[:200]})[:30]
    return {
        "f_brands": brands, "f_colors": colors, "f_sizes": sizes,
        "f_brand": request.GET.getlist("brand"), "f_color": request.GET.getlist("color"), "f_size": request.GET.getlist("size"),
        "f_digital": request.GET.get("digital", ""),
        "f_has_digital": base_qs.filter(is_digital=True).exists(),
        "sellers": base_qs.order_by("seller_name").values_list("seller_name", flat=True).distinct(),
        "f_min_price": request.GET.get("min_price", ""),
        "f_max_price": request.GET.get("max_price", ""),
        "f_min_rating": request.GET.get("min_rating", ""),
        "f_seller": request.GET.get("seller", ""),
        "f_in_stock": request.GET.get("in_stock", ""),
    }




def _can_view_unapproved(user, product):
    if not user.is_authenticated:
        return False
    if user.is_staff:
        return True
    seller, _ = get_seller_account_for_user(user)
    return bool(seller and product.seller_account_id == seller.id)


def _send_html_email(subject, template, context, recipient, lang=None):
    if not recipient:
        return
    try:
        context = {"brand": _brand(), **context}
        html_body = render_to_string(template, context)
        from .email_i18n import language_for, localize
        html_body, subject = localize(html_body, subject, lang if lang is not None else language_for(email=recipient))
        email = EmailMultiAlternatives(subject, strip_tags(html_body), None, [recipient])
        email.attach_alternative(html_body, "text/html")
    except Exception as exc:
        from .alerts import email_failed
        email_failed(subject, recipient, exc)
        return
    # The mail server can take seconds to answer: send after the page is
    # delivered so shoppers and sellers never wait for it.
    from .background import after_response
    after_response(_deliver_email, email, subject, recipient)


def _deliver_email(email, subject, recipient):
    try:
        email.send(fail_silently=False)
    except Exception as exc:
        # Never break the page for a mail problem, but make it visible to
        # the owner (Admin > System check).
        from .alerts import email_failed
        email_failed(subject, recipient, exc)


def _order_lang(order):
    from .email_i18n import language_for
    return language_for(order=order)


def _email_verified(user):
    return Profile.objects.filter(user=user, email_verified=True).exists()


def _send_order_confirmation(request, order):
    if order.payment_method == "cod" and not order.cod_confirmed:
        if not order.cod_otp_hash:  # phone-code orders are confirmed on the site
            _send_cod_confirmation(request, order)
        return
    invoice_url = request.build_absolute_uri(reverse("invoice_pdf", args=[order.id]))
    if order.is_guest:
        invoice_url += f"?t={order.access_token}"
    track_url = request.build_absolute_uri(order.tracking_path() if order.is_guest else reverse("my_orders"))
    if order.status == "pending":
        subject = f"We've received your {_store_name()} order #{order.id}"
    else:
        subject = f"Your {_store_name()} order #{order.id} is confirmed"
    _send_html_email(
        subject,
        "bees/emails/order_confirmation.html",
        {"order": order, "invoice_url": invoice_url, "track_url": track_url},
        order.contact_email,
        lang=_order_lang(order),
    )
    from . import seller_center
    seller_center.notify_new_order(order)


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------

def home(request):
    query = request.GET.get("q", "").strip()
    if query:
        return redirect(f"{reverse('search_products')}?{urlencode({'q': query})}")
    flash_sale_products = with_ratings(Product.objects.flash())[:10]
    just_for_you_products = with_ratings(Product.objects.listed()).order_by("-created_at")[:15]
    from . import ads, content
    return render(request, "bees/index.html", {
        "sponsored": ads.pick(limit=4) if not is_speculative(request) else [],
        "flash_sale_products": flash_sale_products,
        "just_for_you_products": just_for_you_products,
        "categories": _category_tiles(),
        "banners": content.home_banners(),
    })


def _category_tiles():
    """One tile per top-level category that has products (its own or its
    subcategories'), pictured with the category's own picture or one of its
    products (so the picture always matches). Cached for 5 minutes."""
    tiles = cache.get("home:category_tiles")
    if tiles is None:
        images = {}
        for category, image in (
            Product.objects.live().exclude(image_url="")
            .order_by("category", "id").values_list("category", "image_url")
        ):
            images.setdefault(category, image)
        rows = category_rows()
        tiles = []
        for top in [r for r in rows if not r["parent"]]:
            slugs = [top["slug"]] + [r["slug"] for r in rows if r["parent"] == top["slug"]]
            found = next((images[x] for x in slugs if x in images), None)
            if found:
                tiles.append({"slug": top["slug"], "label": top["name"], "image": top["image_url"] or found})
        cache.set("home:category_tiles", tiles, 300)
    return tiles




@ratelimit("review", rate_limit=10, window_seconds=600,
           message="Too many reviews in a short time. Please wait a few minutes.")
def product_detail(request, pk):
    # Rating + review count in the same query (the page shows them several
    # times: stars, summary, structured data).
    product = get_object_or_404(with_ratings(Product.objects.all()), pk=pk)

    if not product.is_live and not _can_view_unapproved(request.user, product):
        bought = (product.approval_status == "archived" and request.user.is_authenticated
                  and OrderItem.objects.filter(order__user=request.user, product=product).exists())
        if not bought:  # past buyers can still open it to review it
            raise Http404("This product is not available.")

    if request.method == "POST":
        return _submit_review(request, product)

    reviews = product.reviews.prefetch_related("photos")
    review_stars = request.GET.get("stars", "")
    if review_stars in ("1", "2", "3", "4", "5"):
        reviews = reviews.filter(rating=int(review_stars))
    if request.GET.get("with_photos"):
        reviews = reviews.filter(photos__isnull=False).distinct()
    if request.GET.get("review_sort") == "helpful":
        reviews = reviews.order_by("-helpful_count", "-created_at")
    voted_reviews = set()
    if request.user.is_authenticated:
        from .models import ReviewVote
        voted_reviews = set(ReviewVote.objects.filter(user=request.user, review__product=product).values_list("review_id", flat=True))
    in_wishlist = False
    can_review = False
    review_block = ""
    if request.user.is_authenticated:
        in_wishlist = Wishlist.objects.filter(user=request.user, product=product).exists()
        review_block = _review_block_reason(request.user, product)
        can_review = not review_block

    gallery = [product.image_url] + list(product.extra_images.values_list("image_url", flat=True))
    variants = list(product.variants.all()) if product.has_variants else []
    variant_sizes = list(dict.fromkeys(v.size for v in variants if v.size))
    variant_colors = list(dict.fromkeys(v.color for v in variants if v.color))
    variants_json = [
        {"id": v.id, "size": v.size, "color": v.color, "stock": v.stock, "price": display_price(v.unit_price), "label": v.label}
        for v in variants
    ]
    from . import shipping as shipping_rules
    ship_table = shipping_rules.table()
    has_shipping_zones = "flat" not in ship_table
    ship_country = request.session.get("ship_country", "")
    if not ship_country and request.user.is_authenticated:
        ship_country = Address.objects.filter(user=request.user, is_default=True).values_list("country", flat=True).first() or ""
    from .recommend import also_bought
    also = also_bought([product.id], limit=6)
    related_products = with_ratings(Product.objects.listed().filter(category=product.category).exclude(pk=product.pk)
                                    .exclude(pk__in=[p.id for p in also]))[:6]
    questions = product.questions.all()

    recent_ids = request.session.get("recently_viewed", [])
    if not is_speculative(request):
        from . import analytics
        analytics.track(request, "view", str(product.id))
        recent_ids = [i for i in recent_ids if i != product.id]
        recent_ids.insert(0, product.id)
        request.session["recently_viewed"] = recent_ids[:10]
        request.session.modified = True
    else:
        recent_ids = [product.id] + [i for i in recent_ids if i != product.id]
    recently_viewed = with_ratings(Product.objects.live().filter(id__in=recent_ids[1:7]))

    from . import seo
    from . import categories as cats_mod
    cat_row = cats_mod.get(product.category)
    category_path = [(product.category, product.get_category_display())]
    if cat_row and cat_row["parent"]:
        category_path.insert(0, (cat_row["parent"], cats_mod.label(cat_row["parent"])))
    return render(request, "bees/product_detail.html", {
        "product_ld": seo.product(request, product, reviews),
        "breadcrumb_ld": seo.breadcrumbs(request, [(_brand().site_name, reverse("home"))]
                                         + [(name, reverse("category_products", args=[slug])) for slug, name in category_path]
                                         + [(product.name, reverse("product_detail", args=[product.pk]))]),
        "category_path": category_path,
        "product": product,
        "reviews": reviews,
        "review_stars": review_stars, "review_photos_only": bool(request.GET.get("with_photos")),
        "review_sort": request.GET.get("review_sort", ""),
        "voted_reviews": voted_reviews,
        "in_wishlist": in_wishlist,
        "can_review": can_review,
        "review_block": review_block,
        "gallery": gallery,
        "variant_sizes": variant_sizes,
        "variant_colors": variant_colors,
        "variants_json": variants_json,
        "has_shipping_zones": has_shipping_zones,
        "ship_table": {**ship_table, "default_days": _brand().delivery_days},
        "ship_country": ship_country,
        "seller_reply": _seller_reply(product),
        "following": bool(request.user.is_authenticated and product.seller_account_id and
                          product.seller_account.followers.filter(user=request.user).exists()),
        "size_chart": product.size_chart(),
        "video": product.video(),
        "order_limits": product.order_limits(),
        **_market_context(request, product, also),
        "ai_enabled": bool(settings.AI_API_KEY),
        "review_summary": ai_tools.review_summary(product),
        "related_products": related_products,
        "also_bought": also,
        "questions": questions,
        "recently_viewed": recently_viewed,
    })


def _review_block_reason(user, product):
    """Why ``user`` can't review ``product`` (empty string if they can).
    Only customers who received the product can review it, and sellers
    can't review their own listings."""
    seller, _ = get_seller_account_for_user(user)
    if seller and product.seller_account_id == seller.id:
        return "You can't review your own product."
    received = OrderItem.objects.filter(order__user=user, product=product).filter(
        Q(order__status="delivered") | Q(fulfillment_status="delivered")).exclude(order__status="cancelled")
    if not received.exists():
        return "Only customers who bought this product can review it. You'll be able to once your order is delivered."
    return ""


def _market_context(request, product, also):
    """Product page extras: EU lowest price, other sellers, auction, bundle,
    instalments, Subscribe & Save and membership."""
    from . import market
    brand = _brand()
    ctx = {"other_offers": market.other_offers(product), "is_member": market.is_member(request.user)}
    if product.old_price or product.discount_percent:
        ctx["lowest_30d"] = market.lowest_price_30d(product)
    if product.is_auction:
        top = market.top_bid(product)
        ctx["auction"] = {"open": market.auction_open(product), "top": top, "next_min": market.next_min_bid(product, top),
                          "count": product.bids.count(), "mine": bool(top and request.user.is_authenticated and top.user_id == request.user.id),
                          "reserve_met": product.auction_reserve is None or (top is not None and top.amount >= product.auction_reserve)}
    if brand.bnpl_enabled and brand.bnpl_instalments > 1 and product.price >= 20:
        ctx["bnpl_part"] = (product.price / brand.bnpl_instalments).quantize(Decimal("0.01"))
    if product.subscribe_percent:
        ctx["subscribe_price"] = (product.price * (100 - product.subscribe_percent) / 100).quantize(Decimal("0.01"))
    bundle = [p for p in also if not p.has_variants and not p.is_auction and p.stock > 0 and not p.personalization_required][:2]
    if bundle and not product.has_variants and not product.is_auction and product.stock > 0:
        ctx["bundle"] = bundle
        ctx["bundle_total"] = product.price + sum(p.price for p in bundle)
    return ctx


def _submit_review(request, product):
    """One review per signed-in customer per product; a second submission
    updates the first. Marked 'verified purchase' if they bought it."""
    if not request.user.is_authenticated:
        messages.error(request, "Please sign in to write a review.")
        return redirect(f"{reverse('login')}?{urlencode({'next': reverse('product_detail', args=[product.pk])})}")
    try:
        rating = _parse_int(request.POST.get("rating"), "Rating", minimum=1, maximum=5, default=5)
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
        return redirect("product_detail", pk=product.pk)
    comment = request.POST.get("comment", "").strip()[:2000]
    if not comment:
        messages.error(request, "Please write a few words about the product.")
        return redirect("product_detail", pk=product.pk)
    blocked = _review_block_reason(request.user, product)
    if blocked:
        messages.error(request, blocked)
        return redirect("product_detail", pk=product.pk)
    photos = request.FILES.getlist("photos")[:3]
    try:
        for f in photos:
            validate_image_upload(f)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("product_detail", pk=product.pk)
    verified = True
    review, created = Review.objects.update_or_create(
        product=product, user=request.user,
        defaults={
            "username": request.user.get_full_name() or request.user.username,
            "rating": rating,
            "comment": comment,
            "is_verified_purchase": verified,
        },
    )
    if photos:
        from .images import save_public
        from .models import ReviewPhoto
        review.photos.all().delete()
        for f in photos:
            ReviewPhoto.objects.create(review=review, image_url=save_public(f, "reviews"))
    if product.seller_account_id:
        from .seller_center import _seller_users
        verb = "New" if created else "Updated"
        Notification.objects.bulk_create([
            Notification(user=u, link="/seller/reviews/?tab=reviews",
                         message=f"{verb} {rating}★ review on '{product.name}': {comment[:60]}"[:255])
            for u in _seller_users(product.seller_account)])
    messages.success(request, "Thanks - your review has been posted.")
    return redirect("product_detail", pk=product.pk)


def _listing(request, base_qs, per_page):
    products = _apply_filters(with_ratings(base_qs), request)
    sort = request.GET.get("sort", "")
    products = _apply_sort(products, sort)
    page_obj = Paginator(products, per_page).get_page(request.GET.get("page"))
    context = {"products": page_obj, "page_obj": page_obj, "current_sort": sort}
    context.update(_filter_context(request, base_qs))
    return context


def category_products(request, category):
    from . import categories
    row = categories.get(category)
    if row is None or not row["visible"]:
        raise Http404("Unknown category.")
    base = Product.objects.listed().filter(category__in=categories.with_children(category))
    context = _listing(request, base, 12)
    parent = categories.get(row["parent"]) if row["parent"] else None
    context.update({"category": category, "category_label": row["name"], "category_info": row,
                    "category_parent": parent, "subcategories": categories.children(category),
                    "sibling_categories": categories.children(parent["slug"]) if parent else []})
    if request.GET.get("page", "1") == "1" and not is_speculative(request):
        from . import ads
        context["sponsored"] = ads.pick(base)
    return render(request, "bees/category.html", context)


def search_products(request):
    query = request.GET.get("q", "").strip()[:150]
    if query and not is_speculative(request):
        log, created = SearchLog.objects.get_or_create(query__iexact=query, defaults={"query": query})
        if not created:
            SearchLog.objects.filter(pk=log.pk).update(count=F("count") + 1)
    base_qs, smart = _smart_search(query)
    corrected = ""
    if query and not request.GET.get("exact") and not base_qs.exists():
        from .autocomplete import correct
        fixed = correct(query)
        if fixed:
            fixed_qs, fixed_smart = _smart_search(fixed)
            if fixed_qs.exists():
                base_qs, smart, corrected = fixed_qs, fixed_smart, fixed
    if smart.get("cheap") and not request.GET.get("sort"):
        request.GET = request.GET.copy()
        request.GET["sort"] = "price_asc"
    context = _listing(request, base_qs, 12)
    context["query"] = query
    context["smart"] = smart
    context["corrected"] = corrected
    context["shown_query"] = corrected or query
    if query and request.GET.get("page", "1") == "1" and not is_speculative(request):
        from . import ads
        context["sponsored"] = ads.pick(base_qs)
    if query and not is_speculative(request) and request.GET.get("page", "1") == "1":
        from . import analytics
        key = " ".join(query.lower().split())[:150]
        analytics.track(request, "search", key)
        page = context.get("page_obj")
        if page is not None and not page.paginator.count:
            analytics.track(request, "search_zero", key)
    return render(request, "bees/search.html", context)


def _smart_search(query):
    """Understands searches like 'red hoodie under 40': every word must
    match the name, description, category or seller (plural or not), and
    price words become a price range. If nothing matches every word, the
    closest matches (any word) are shown instead."""
    from . import ai
    if not query:
        return Product.objects.none(), {}
    parsed = ai.parse_search(query)
    labels = {r["slug"]: r["name"] for r in category_rows()}

    def word_q(word):
        stem = ai.singular(word)
        q = Q(name__icontains=stem) | Q(description__icontains=stem) | Q(seller_name__icontains=stem)
        cats = [slug for slug, label in labels.items() if stem in label.lower() or stem in slug]
        if cats:
            q |= Q(category__in=cats)
        return q

    qs = Product.objects.listed()
    if parsed["min"] is not None:
        qs = qs.filter(price__gte=parsed["min"])
    if parsed["max"] is not None:
        qs = qs.filter(price__lte=parsed["max"])
    words = parsed["words"]
    smart = {"min": parsed["min"], "max": parsed["max"], "cheap": parsed["cheap"], "fallback": False,
             "price": parsed["min"] is not None or parsed["max"] is not None}
    if not words:
        if smart["price"] or parsed["cheap"]:
            return qs, smart
        return qs.filter(Q(name__icontains=query) | Q(description__icontains=query)), smart
    strict = qs
    for word in words:
        strict = strict.filter(word_q(word))
    if len(words) > 1 and not strict.exists():
        any_q = Q()
        for word in words:
            any_q |= word_q(word)
        smart["fallback"] = True
        return qs.filter(any_q), smart
    return strict, smart


def all_products(request):
    context = _listing(request, Product.objects.listed(), 16)
    return render(request, "bees/all_products.html", context)


def product_quick_view(request, pk):
    product = get_object_or_404(Product, pk=pk)
    if not product.is_live and not _can_view_unapproved(request.user, product):
        bought = (product.approval_status == "archived" and request.user.is_authenticated
                  and OrderItem.objects.filter(order__user=request.user, product=product).exists())
        if not bought:  # past buyers can still open it to review it
            raise Http404("This product is not available.")
    return render(request, "bees/partials/quick_view.html", {"product": product})


def search_suggest(request):
    from .autocomplete import suggest
    data = suggest(request.GET.get("q", "").strip())
    data["results"] = [p["name"] for p in data["products"]]  # older clients
    resp = JsonResponse(data)
    resp["Cache-Control"] = "private, max-age=60"
    return resp


def store_page(request, seller_name):
    seller_account = SellerAccount.objects.filter(
        Q(business_name=seller_name) | Q(organization_name=seller_name),
        status="approved",
    ).first()
    base = Product.objects.live().filter(seller_name=seller_name)
    total_count = base.count()
    products = base
    q = request.GET.get("q", "").strip()
    if q:
        products = products.filter(Q(name__icontains=q) | Q(description__icontains=q))
    sort = request.GET.get("sort", "")
    products = _apply_sort(with_ratings(products), sort)
    if not sort:
        products = products.order_by("-created_at")
    page_obj = Paginator(products, 24).get_page(request.GET.get("page"))
    stats = {}
    if seller_account:
        stats = Review.objects.filter(product__seller_account=seller_account).aggregate(avg=Avg("rating"), n=Count("id"))
        stats["sold"] = OrderItem.objects.filter(seller_account=seller_account).counted().aggregate(n=Sum("quantity"))["n"] or 0
        stats["store_reviews"] = seller_account.seller_reviews.select_related("user")[:3]
        stats["followers"] = seller_account.followers.count()
        from .market_views import _public_coupons
        store_vouchers = list(_public_coupons().filter(seller_account=seller_account)[:4])
        stats["following"] = request.user.is_authenticated and seller_account.followers.filter(user=request.user).exists()
    return render(request, "bees/store.html", {
        "seller_name": seller_name,
        "products": page_obj,
        "page_obj": page_obj,
        "total_count": total_count,
        "seller_account": seller_account,
        "stats": stats,
        "store_vouchers": store_vouchers if seller_account else [],
        "q": q,
        "current_sort": sort,
    })


@require_POST
@ratelimit("stock_alert", rate_limit=10, window_seconds=600, redirect_to="home")
def stock_alert(request, pk):
    from .models import StockAlert
    product = get_object_or_404(Product.objects.live(), pk=pk)
    email = (request.POST.get("email") or (request.user.email if request.user.is_authenticated else "")).strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, "Please enter a valid email address.")
        return redirect("product_detail", pk=pk)
    variant = None
    vid = request.POST.get("variant", "")
    if vid.isdigit():
        variant = product.variants.filter(pk=vid).first()
    StockAlert.objects.get_or_create(
        product=product, variant=variant, email=email, notified_at=None,
        defaults={"user": request.user if request.user.is_authenticated else None},
    )
    what = f"{product.name} ({variant.label})" if variant else product.name
    messages.success(request, f"Done! We'll email {email} when {what} is back in stock.")
    return redirect("product_detail", pk=pk)


@ratelimit("ask_question", rate_limit=10, window_seconds=600, redirect_to="home")
def ask_question(request, pk):
    product = get_object_or_404(Product.objects.live(), pk=pk)
    if request.method == "POST":
        if not request.user.is_authenticated:
            messages.error(request, "Please sign in to ask a question.")
            return redirect(f"{reverse('login')}?{urlencode({'next': reverse('product_detail', args=[pk])})}")
        text = request.POST.get("question", "").strip()[:1000]
        if text:
            Question.objects.create(product=product, user=request.user, username=request.user.username, question=text)
            if product.seller_account_id:
                Notification.objects.create(
                    user_id=product.seller_account.user_id, link="/seller/reviews/",
                    message=f"New customer question on '{product.name[:120]}'.",
                )
            messages.success(request, "Your question has been posted. We'll notify you when it's answered.")
    return redirect("product_detail", pk=pk)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

_username_validator = UnicodeUsernameValidator()


@ratelimit("signup", rate_limit=5, window_seconds=300, redirect_to="signup",
           message="Too many signup attempts from this connection. Please wait a few minutes and try again.")
def signup_view(request):
    if request.user.is_authenticated:
        return redirect("home")

    form = {}
    if request.method == "POST":
        form = request.POST
        username = request.POST.get("username", "").strip()
        email = request.POST.get("email", "").strip().lower()
        password = request.POST.get("password", "")
        confirm = request.POST.get("confirm_password", "")
        user_type = request.POST.get("user_type", "buyer")
        if user_type not in ("buyer", "individual", "organization"):
            user_type = "buyer"

        error = None
        try:
            if not username or not password or not email:
                raise ValidationError("Username, email and password are required.")
            _username_validator(username)
            if "@" in username:
                raise ValidationError("Usernames can't contain '@'. Use letters, numbers and . _ - only.")
            validate_email(email)
            if password != confirm:
                raise ValidationError("Passwords do not match.")
            if User.objects.filter(username__iexact=username).exists():
                raise ValidationError("That username is already taken.")
            if User.objects.filter(email__iexact=email).exists():
                raise ValidationError("An account with that email already exists. Try signing in or resetting your password.")
            if user_type in ("individual", "organization") and not request.POST.get("phone", "").strip():
                raise ValidationError("Phone number is required for seller accounts.")
            validate_password(password, User(username=username, email=email))
            for field in ("business_certificate", "id_document"):
                validate_document_upload(request.FILES.get(field))
            for field in ("store_logo", "store_banner"):
                validate_image_upload(request.FILES.get(field))
        except ValidationError as exc:
            error = " ".join(exc.messages)

        if error:
            messages.error(request, error)
        else:
            with transaction.atomic():
                user = User.objects.create_user(username=username, email=email, password=password)
                code = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
                ref = (request.GET.get("ref") or request.POST.get("ref", "") or request.session.get("ref", ""))[:12].strip().upper()
                if not _brand().referral_enabled or not Profile.objects.filter(referral_code=ref).exists():
                    ref = ""
                Profile.objects.create(user=user, referral_code=code, referred_by=ref)
                if ref:
                    _reward_referral(ref, user)

                if user_type in ("individual", "organization"):
                    SellerAccount.objects.create(
                        user=user,
                        account_type=user_type,
                        full_name=request.POST.get("full_name", "")[:150],
                        business_name=request.POST.get("business_name", "")[:150],
                        organization_name=request.POST.get("organization_name", "")[:150],
                        phone=request.POST.get("phone", "")[:30],
                        cnic=request.POST.get("cnic", "")[:30],
                        business_address=request.POST.get("business_address", "")[:255],
                        city=request.POST.get("city", "")[:100],
                        country=request.POST.get("country", "")[:100],
                        store_description=request.POST.get("store_description", ""),
                        product_categories=request.POST.get("product_categories", "")[:255],
                        brand_info=request.POST.get("brand_info", ""),
                        tax_info=request.POST.get("tax_info", "")[:100],
                        bank_details=request.POST.get("bank_details", "")[:255],
                        business_certificate=request.FILES.get("business_certificate"),
                        id_document=request.FILES.get("id_document"),
                        store_logo=optimize(request.FILES.get("store_logo"), LOGO_MAX),
                        store_banner=optimize(request.FILES.get("store_banner"), BANNER_MAX),
                    )
                    AuditLog.objects.create(user=user, action=f"Submitted {user_type} seller application")
                AuditLog.objects.create(user=user, action="Account created")

            auth_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            _send_verification_email(request, user)

            if user_type in ("individual", "organization"):
                messages.success(request, "Account created! Your seller application is pending review.")
                return redirect("seller_dashboard")

            messages.success(request, f"Welcome to {_store_name()}! We've emailed you a code to verify your address.")
            return redirect(safe_next_url(request, request.POST.get("next") or request.GET.get("next"), reverse("home")))

    return render(request, "bees/signup.html", {
        "next": safe_next_url(request, request.POST.get("next") or request.GET.get("next"), ""),
        "ref_code": request.GET.get("ref", "") or request.POST.get("ref", ""),
        "categories": category_choices(),
        "form": form,
    })


def _reward_referral(ref, new_user):
    """New customer who signed up with a referral link gets a single-use
    welcome coupon. The referrer is rewarded later, when this customer's
    first order is delivered (see models._reward_referrer_for)."""
    from datetime import timedelta
    from django.utils import timezone
    referrer_profile = Profile.objects.filter(referral_code=ref).exclude(user=new_user).first()
    if not referrer_profile:
        return
    percent = _brand().referral_friend_percent
    if not percent:
        return
    new_user_coupon_code = "WELCOME-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    Coupon.objects.create(code=new_user_coupon_code, percent_off=percent, usage_limit=1, per_user_limit=1,
                          expiry_date=timezone.localdate() + timedelta(days=60),
                          owner=new_user, purpose="referral_welcome")
    Notification.objects.create(
        user=new_user,
        message=f"Welcome! Here's {percent}% off your first order: {new_user_coupon_code}",
        link="/cart/",
    )


def _send_verification_email(request, user):
    if not user.email:
        return
    code = f"{secrets.randbelow(1_000_000):06d}"
    cache.set(f"email_verify_code:{user.id}", code, timeout=900)  # valid for 15 minutes
    cache.delete(f"email_verify_attempts:{user.id}")
    _send_html_email(
        f"Your {_store_name()} verification code",
        "bees/emails/verify_email.html",
        {"user": user, "code": code},
        user.email,
    )


MAX_VERIFY_ATTEMPTS = 5


@login_required
def verify_email_code(request):
    profile, _ = Profile.objects.get_or_create(user=request.user, defaults={"referral_code": secrets.token_hex(4).upper()})
    if profile.email_verified:
        messages.info(request, "Your email is already verified.")
        return redirect("profile")

    if request.method == "POST":
        attempts_key = f"email_verify_attempts:{request.user.id}"
        entered = request.POST.get("code", "").strip()
        stored = cache.get(f"email_verify_code:{request.user.id}")
        attempts = cache.get(attempts_key, 0)
        if not stored:
            messages.error(request, "That code has expired. Please request a new one.")
        elif attempts >= MAX_VERIFY_ATTEMPTS:
            cache.delete(f"email_verify_code:{request.user.id}")
            messages.error(request, "Too many incorrect attempts. Please request a new code.")
        elif secrets.compare_digest(entered, stored):
            profile.email_verified = True
            profile.save(update_fields=["email_verified"])
            # Guest orders placed with this (now proven) email become theirs.
            email = request.user.email
            if email:
                linked = Order.objects.filter(user__isnull=True).filter(
                    Q(guest_email__iexact=email) | Q(email__iexact=email)).update(user=request.user)
                if linked:
                    messages.info(request, f"{linked} earlier order{'s' if linked != 1 else ''} placed as a guest {'are' if linked != 1 else 'is'} now in My orders.")
            cache.delete(f"email_verify_code:{request.user.id}")
            cache.delete(attempts_key)
            messages.success(request, "Your email has been verified.")
            return redirect("profile")
        else:
            cache.set(attempts_key, attempts + 1, timeout=900)
            messages.error(request, "That code isn't right. Please check your email and try again.")

    return render(request, "bees/verify_email_code.html")


@login_required
@ratelimit("resend_verification", rate_limit=3, window_seconds=300, redirect_to="profile",
           message="Please wait a few minutes before requesting another code.")
def resend_verification(request):
    if request.method != "POST":
        return redirect("verify_email")
    _send_verification_email(request, request.user)
    messages.success(request, "A new verification code has been sent to your email.")
    return redirect("verify_email")


@ratelimit("login", rate_limit=8, window_seconds=300, redirect_to="login",
           message="Too many sign-in attempts from this connection. Please wait a few minutes and try again.")
def login_view(request):
    next_url = safe_next_url(request, request.POST.get("next") or request.GET.get("next"), reverse("home"))
    if request.user.is_authenticated and not _is_demo_user(request.user):
        return redirect(next_url)

    if request.method == "POST":
        identifier = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        username = identifier
        if "@" in identifier:
            matches = list(User.objects.filter(email__iexact=identifier).values_list("username", flat=True)[:2])
            if len(matches) == 1:
                username = matches[0]
        user = authenticate(request, username=username, password=password)
        if user is not None:
            if Profile.objects.filter(user=user, totp_enabled=True).exists():
                # Password is right; now ask for the authenticator code.
                from django.utils import timezone as _tz
                request.session["2fa_pending"] = {
                    "uid": user.pk, "backend": user.backend, "next": next_url,
                    "remember": bool(request.POST.get("remember")), "tries": 0,
                    "expires": _tz.now().timestamp() + 300,
                }
                return redirect("login_code")
            auth_login(request, user)
            if not request.POST.get("remember"):
                request.session.set_expiry(0)  # signed out when the browser closes
            return redirect(next_url)
        messages.error(request, "Incorrect username/email or password.")

    return render(request, "bees/login.html", {"next": next_url, "demo_roles": DEMO_ROLES})


def _is_demo_user(user):
    from django.conf import settings as dj
    from .management.commands.demo_setup import DEMO_USERNAMES
    return getattr(dj, "DEMO_MODE", False) and user.is_authenticated and user.username in DEMO_USERNAMES


DEMO_ROLES = [
    ("shopper", "Shop as a customer", "Browse, use smart search, add to cart and check out."),
    ("seller", "Open the Seller Center", "Sales, orders to ship, products, plan and payouts."),
    ("admin", "Run the marketplace", "Store admin: orders, sellers, plans, coupons and reports."),
]


def logout_view(request):
    if request.method == "POST":
        auth_logout(request)
        messages.info(request, "You've been signed out.")
    return redirect("home")


@login_required
@require_POST
def redeem_points(request):
    """Converts reward points into store credit in whole units."""
    with transaction.atomic():
        profile = Profile.objects.select_for_update().filter(user=request.user).first()
        units = profile.loyalty_points // Profile.POINTS_PER_UNIT if profile else 0
        if not units:
            messages.error(request, f"You need at least {Profile.POINTS_PER_UNIT} points to redeem.")
            return redirect("profile")
        profile.loyalty_points -= units * Profile.POINTS_PER_UNIT
        profile.store_credit += Decimal(units)
        profile.save(update_fields=["loyalty_points", "store_credit"])
    messages.success(request, f"{money(Decimal(units))} added to your store credit. It'll be applied at your next checkout.")
    return redirect("profile")


@login_required
def profile_view(request):
    profile, _ = Profile.objects.get_or_create(user=request.user, defaults={"referral_code": secrets.token_hex(4).upper()})
    if request.method == "POST":
        new_email = request.POST.get("email", "").strip().lower()
        try:
            if new_email:
                validate_email(new_email)
                if User.objects.filter(email__iexact=new_email).exclude(pk=request.user.pk).exists():
                    raise ValidationError("That email is already used by another account.")
            if new_email != (request.user.email or "").lower():
                if not new_email:
                    raise ValidationError("Your account needs an email address for receipts and sign-in help.")
                # Someone using a stolen session must not be able to take over
                # the account by changing the email and resetting the password.
                if request.user.has_usable_password() and not request.user.check_password(request.POST.get("current_password", "")):
                    raise ValidationError("To change your email, type your current password too.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect("profile")
        old_email = request.user.email or ""
        email_changed = new_email != old_email.lower()
        request.user.first_name = request.POST.get("first_name", "")[:150]
        request.user.email = new_email
        request.user.save(update_fields=["first_name", "email"])
        profile.phone = request.POST.get("phone", "")[:30]
        if email_changed:
            profile.email_verified = False
        profile.save()
        if email_changed and old_email:
            # Tell the old address, so a hijacked account doesn't go unnoticed.
            _send_html_email("Your email address was changed", "bees/emails/security_notice.html", {
                "user": request.user, "event": "The email address on your account was changed",
                "detail": f"New address: {new_email or '(removed)'}. If this wasn't you, reset your password straight away and contact us.",
                "reset_url": request.build_absolute_uri(reverse("password_reset")),
            }, old_email)
            AuditLog.objects.create(user=request.user, action="Changed email address")
        if email_changed and new_email:
            _send_verification_email(request, request.user)
            messages.success(request, "Profile updated. We've sent a code to verify your new email.")
        else:
            messages.success(request, "Profile updated.")
        return redirect("profile")
    addresses = Address.objects.filter(user=request.user)
    orders_count = Order.objects.filter(user=request.user).count()
    redeemable_points = profile.loyalty_points - profile.loyalty_points % Profile.POINTS_PER_UNIT
    return render(request, "bees/profile.html", {
        "profile": profile,
        "points_per_unit": Profile.POINTS_PER_UNIT,
        "redeemable_points": redeemable_points,
        "redeemable_credit": Decimal(redeemable_points // Profile.POINTS_PER_UNIT),
        "addresses": addresses,
        "orders_count": orders_count,
    })


ADDRESS_FIELDS = ("full_name", "phone", "address", "city", "state", "postal_code", "country")


@login_required
@require_POST
def add_address(request):
    data = {f: request.POST.get(f, "").strip() for f in ADDRESS_FIELDS}
    data["country"] = data["country"].upper()[:2]
    if not all(data[f] for f in ("full_name", "address", "city", "country")):
        messages.error(request, "Please fill in name, street address, city and country.")
        return redirect("profile")
    first = not Address.objects.filter(user=request.user).exists()
    Address.objects.create(user=request.user, label=request.POST.get("label", "Home")[:30] or "Home", is_default=first, **data)
    messages.success(request, "Address saved.")
    return redirect("profile")


@login_required
@require_POST
def delete_address(request, pk):
    Address.objects.filter(pk=pk, user=request.user).delete()
    messages.success(request, "Address removed.")
    return redirect("profile")


@login_required
def notifications_list(request):
    notifications = list(request.user.notifications.all()[:30])
    if not is_speculative(request):  # a hover-prefetch isn't the user reading them
        request.user.notifications.filter(is_read=False).update(is_read=True)
    return render(request, "bees/notifications.html", {"notifications": notifications})


def set_currency(request, code):
    from . import currency
    code = code.upper()
    if code == currency.store_code():
        request.session.pop(currency.SESSION_KEY, None)
    elif currency.get(code):
        request.session[currency.SESSION_KEY] = code
    return redirect_back(request, "home")


def set_language(request, lang_code):
    from .translations import TRANSLATIONS
    if lang_code in TRANSLATIONS:
        request.session["site_lang"] = lang_code
        from .email_i18n import remember
        remember(request.user, lang_code)
    return redirect_back(request, "home")


# ---------------------------------------------------------------------------
# Cart
# ---------------------------------------------------------------------------

def _get_cart_items(request):
    """Reads the session cart and returns (items, total, count). Each item
    has product, variant (or None), qty, unit_price, subtotal, label, key.
    Unavailable products and invalid quantities are dropped."""
    from .cart import lines, totals
    items = lines(request.session.get("cart", {}))
    notes = request.session.get("cart_notes", {}) or {}
    for row in items:
        row["personalization"] = notes.get(row["key"], "")
    total, count = totals(items)
    return items, total, count


def cart_count(request):
    from .context_processors import cart_count as _count
    return _count(request)["cart_count"]


def _is_ajax(request):
    return (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
    )


@require_POST
def add_to_cart(request, pk):
    from .cart import make_key
    product = get_object_or_404(Product.objects.live(), pk=pk)

    def fail(msg):
        if _is_ajax(request):
            return JsonResponse({"ok": False, "error": msg}, status=400)
        messages.error(request, msg)
        return redirect("product_detail", pk=pk) if product.has_variants else redirect_back(request, "cart")

    if product.is_auction:
        return fail(f"{product.name} is sold by auction - place a bid instead.")
    note = " ".join(request.POST.get("personalization", "").split())[:product.personalization_max]
    if product.personalization_prompt and product.personalization_required and not note:
        return fail(f"Please fill in: {product.personalization_prompt}")
    variant = None
    if product.has_variants:
        vid = request.POST.get("variant", "")
        variant = product.variants.filter(pk=vid).first() if vid.isdigit() else None
        if not variant:
            return fail(f"Please choose a size/colour for {product.name}.")
    stock = variant.stock if variant else product.stock
    name = f"{product.name} ({variant.label})" if variant else product.name
    if stock <= 0:
        return fail(f"{name} is out of stock.")

    cart = request.session.get("cart", {})
    key = make_key(pk, variant.pk if variant else None)
    try:
        qty = int(request.POST.get("quantity", 1))
    except (TypeError, ValueError):
        qty = 1
    qty = max(1, min(qty, 99))
    new_qty = cart.get(key, 0) + qty
    warning = None
    low, high = product.order_limits()
    if low > stock:
        return fail(f"{name} is sold in quantities of at least {low}, and only {stock} are left.")
    if new_qty < low:
        new_qty = low
        warning = f"{name} has a minimum order of {low}, so {low} were added."
    if high and new_qty > high:
        new_qty = high
        warning = f"You can buy at most {high} of {name} per order."
    if new_qty > stock:
        new_qty = stock
        warning = f"Only {stock} of {name} left in stock."
    cart[key] = new_qty
    request.session["cart"] = cart
    if product.personalization_prompt:
        notes = request.session.get("cart_notes", {}) or {}
        if note:
            notes[key] = note
        request.session["cart_notes"] = notes
    _persist_cart(request)
    request.session.modified = True
    from . import analytics
    analytics.track(request, "cart")

    if _is_ajax(request):
        return JsonResponse({
            "ok": True,
            "message": f"{name} added to cart.",
            "warning": warning,
            "cart_count": cart_count(request),
            "product_id": product.id,
            "product_qty": new_qty,
        })

    if warning:
        messages.warning(request, warning)
    messages.success(request, f"{name} added to cart.")
    return redirect_back(request, "cart")



@require_POST
def update_cart_item(request, key):
    from .cart import lines
    cart = request.session.get("cart", {})
    action = request.POST.get("action")
    warning = None
    if key in cart and key[:1] in ("q", "s") and action in ("increase", "decrease"):
        warning = "The quantity of a quote was agreed with the seller. Remove it, or ask for a new quote."
    elif key in cart:
        if action == "increase":
            row = next(iter(lines({key: cart[key]})), None)
            high = row["product"].order_limits()[1] if row else None
            if row and cart[key] >= row["stock"]:
                warning = f"Only {row['stock']} of {row['product'].name} available."
            elif row and high and cart[key] >= high:
                warning = f"You can buy at most {high} of {row['product'].name} per order."
            elif row:
                cart[key] += 1
        elif action == "decrease":
            row = next(iter(lines({key: cart[key]})), None)
            low = row["product"].order_limits()[0] if row else 1
            cart[key] -= 1
            if cart[key] < low and cart[key] > 0:
                del cart[key]
                warning = f"{row['product'].name} has a minimum order of {low}, so it was removed from your cart."
            elif cart[key] <= 0:
                del cart[key]
        elif action == "remove":
            del cart[key]
    request.session["cart"] = cart
    _persist_cart(request)
    request.session.modified = True

    if _is_ajax(request):
        items, total, count = _get_cart_items(request)
        row = next((i for i in items if i["key"] == key), None)
        return JsonResponse({
            "ok": True,
            "warning": warning,
            "cart_count": count,
            "cart_total": str(total),
            "cart_total_display": money(total),
            "removed": row is None,
            "key": key,
            "product_qty": row["qty"] if row else 0,
            "product_subtotal": str(row["subtotal"]) if row else "0",
            "product_subtotal_display": money(row["subtotal"]) if row else money(0),
        })
    return redirect("cart")


@require_POST
def cart_note(request, key):
    """Saves the personalization text for a cart line."""
    from .cart import lines
    cart = request.session.get("cart", {})
    row = next(iter(lines({key: cart[key]})), None) if key in cart else None
    if row and row["product"].personalization_prompt:
        notes = request.session.get("cart_notes", {}) or {}
        notes[key] = " ".join(request.POST.get("personalization", "").split())[:row["product"].personalization_max]
        request.session["cart_notes"] = notes
        request.session.modified = True
        messages.success(request, "Saved.")
    return redirect("cart")


def cart_view(request):
    items, total, count = _get_cart_items(request)
    recs = []
    if items:
        from .recommend import also_bought
        in_cart = {i["product"].id for i in items}
        recs = [p for p in also_bought(sorted(in_cart), limit=8) if p.id not in in_cart][:4]
    return render(request, "bees/cart.html", {
        "recommended": recs,
        "items": items,
        "total": total,
        "count": count,
    })


@require_POST
def cart_bulk_remove(request):
    cart = request.session.get("cart", {})
    for pid in request.POST.getlist("selected"):
        cart.pop(pid, None)
    request.session["cart"] = cart
    _persist_cart(request)
    request.session.modified = True
    messages.success(request, "Selected items removed from cart.")
    return redirect("cart")


def toggle_compare(request, pk):
    compare = request.session.get("compare", [])
    if pk in compare:
        compare.remove(pk)
    else:
        if len(compare) >= 4:
            compare.pop(0)
        compare.append(pk)
    request.session["compare"] = compare
    request.session.modified = True
    return redirect_back(request, "home")


def compare_page(request):
    compare_ids = request.session.get("compare", [])
    products = with_ratings(Product.objects.live().filter(id__in=compare_ids))
    return render(request, "bees/compare.html", {"products": products})


@login_required
def toggle_wishlist(request, pk):
    product = get_object_or_404(Product, pk=pk)
    if request.method == "POST":
        item, created = Wishlist.objects.get_or_create(user=request.user, product=product)
        if not created:
            item.delete()
            messages.info(request, f"Removed {product.name} from your wishlist.")
        else:
            messages.success(request, f"Added {product.name} to your wishlist.")
    return redirect_back(request, "home")


@login_required
def wishlist_view(request):
    items = list(Wishlist.objects.filter(user=request.user).select_related("product").order_by("list_name", "-created_at"))
    groups = {}
    for item in items:
        groups.setdefault(item.list_name or "", []).append(item)
    profile = getattr(request.user, "profile", None)
    token = getattr(profile, "wishlist_token", "")
    return render(request, "bees/wishlist.html", {
        "items": items, "groups": sorted(groups.items(), key=lambda g: (g[0] != "", g[0])),
        "list_names": sorted({i.list_name for i in items if i.list_name}),
        "share_url": request.build_absolute_uri(reverse("shared_wishlist", args=[token])) if token else ""})


# ---------------------------------------------------------------------------
# Checkout & orders
# ---------------------------------------------------------------------------

def _payment_methods():
    methods = []
    if payments.is_configured():
        methods.append("card")
    from . import paypal
    if paypal.is_configured():
        methods.append("paypal")
    if _brand().allow_cash_on_delivery:
        methods.append("cod")
    return methods


def _price_cart(subtotal, coupon=None, country=None, items=1, lines=None, digital_only=False,
                member=False, pickup=None, free_ship=False):
    """Returns the full price breakdown for a cart subtotal. Shipping
    depends on the destination country; ``shipping`` is None while the
    country isn't known, and ``ships`` is False if we don't deliver there."""
    from . import shipping as shipping_rules
    brand = _brand()
    discount = Decimal("0")
    if coupon and lines is not None:
        discount = coupon.discount_for(lines)[1]
    elif coupon:
        discount = (subtotal * Decimal(coupon.percent_off) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    member_discount = Decimal("0")
    if member and brand.membership_discount:
        member_discount = ((subtotal - discount) * Decimal(brand.membership_discount) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        discount += member_discount
    discounted = max(subtotal - discount, Decimal("0"))
    free_ship = free_ship or bool(coupon and getattr(coupon, "free_shipping", False))
    # Free-shipping thresholds are checked against what the customer
    # actually pays for the goods (after the coupon).
    if digital_only or not subtotal:
        quote = {"ships": True, "fee": Decimal("0"), "zone": None}  # downloads don't ship
    elif pickup is not None:
        quote = {"ships": True, "fee": Decimal("0") if (member or free_ship) else pickup.fee, "zone": None}
    else:
        quote = shipping_rules.quote(country, discounted, items)
        if quote["ships"] and (member or free_ship) and quote["fee"] is not None:
            quote["fee"] = Decimal("0")
    shipping = quote["fee"]
    from . import taxes
    tax = taxes.compute(discounted, shipping or Decimal("0"), country)
    return {
        "subtotal": subtotal,
        "discount": discount,
        "member_discount": member_discount,
        "member": member,
        "ships": quote["ships"],
        "shipping_zone": quote["zone"],
        "shipping": shipping,
        "tax": tax["amount"],
        "tax_name": tax["name"] or "Tax",
        "tax_rate": tax["rate"],
        "tax_included": tax["included"],
        "total": discounted + (shipping or Decimal("0")) + (Decimal("0") if tax["included"] else tax["amount"]),
        "tax_percent": tax["rate"] if tax["rate"] is not None else brand.tax_percent,
    }


def _all_digital(products):
    products = list(products)
    return bool(products) and all(getattr(p, "is_digital", False) for p in products)


def _cart_lines(items):
    return [(i["product"], i["unit_price"], i["qty"]) for i in items]


def _coupon_check(coupon, user, lines, email=None):
    """Store codes check the whole cart; a seller's code needs their products in it."""
    eligible, _off = coupon.discount_for(lines)
    if coupon.seller_account_id and not eligible:
        return False, f"That code is for products from {coupon.seller_account.display_name} only."
    return coupon.is_valid_for(user, eligible, email=email)


def _session_coupon(request, subtotal, items=None):
    code = request.session.get("coupon_code", "")
    if not code:
        return None, None
    coupon = Coupon.objects.filter(code__iexact=code).first()
    if not coupon:
        return None, "That coupon code no longer exists."
    if items is not None:
        is_valid, error = _coupon_check(coupon, request.user, _cart_lines(items))
    else:
        is_valid, error = coupon.is_valid_for(request.user, subtotal)
    return (coupon, None) if is_valid else (None, error)


CHECKOUT_REQUIRED = ("full_name", "email", "phone", "address", "city", "postal_code", "country")


class CheckoutError(Exception):
    pass


@ratelimit("checkout", rate_limit=10, window_seconds=300, redirect_to="cart",
           message="Too many checkout attempts. Please wait a few minutes and try again.")
def checkout_view(request):
    items, subtotal, count = _get_cart_items(request)
    if not items:
        messages.error(request, "Your cart is empty.")
        return redirect("cart")
    if not request.user.is_authenticated and not _brand().allow_guest_checkout:
        messages.info(request, "Please sign in or create a free account to place your order. Your cart is saved.")
        return redirect(f"{reverse('login')}?{urlencode({'next': reverse('checkout')})}")

    coupon, coupon_error = _session_coupon(request, subtotal, items)
    default_address = Address.objects.filter(user=request.user, is_default=True).first() if request.user.is_authenticated else None
    from . import market
    member = market.is_member(request.user)
    pricing = _price_cart(subtotal, coupon, default_address.country if default_address else None, count, lines=_cart_lines(items),
                          digital_only=_all_digital(i["product"] for i in items), member=member)
    methods = _payment_methods()
    cod_note = ""
    if any(i["product"].is_digital for i in items):
        methods = [m for m in methods if m != "cod"]  # downloads are paid online
    if "cod" in methods:
        from . import cod
        cod_note = cod.problem(pricing["total"], request.user.email if request.user.is_authenticated else "",
                               default_address.phone if default_address else "")
        if cod_note and request.method != "POST":
            methods = [m for m in methods if m != "cod"]

    if request.method == "POST":
        if coupon_error:
            request.session["coupon_code"] = ""
            messages.error(request, coupon_error)
            return redirect("checkout")
        payment_method = request.POST.get("payment_method") or (methods[0] if methods else "")
        if payment_method not in methods:
            messages.error(request, "Please choose an available payment method.")
            return redirect("checkout")

        data = {f: request.POST.get(f, "").strip() for f in CHECKOUT_REQUIRED + ("state",)}
        data["country"] = data["country"].upper()[:2]
        pickup = None
        if request.POST.get("delivery") == "pickup":
            from .models import PickupPoint
            pid = request.POST.get("pickup_point", "")
            pickup = PickupPoint.objects.filter(active=True, pk=pid if pid.isdigit() else 0).first()
            if pickup is not None:
                data.update({"address": f"Collect at {pickup.name}, {pickup.address}"[:255], "city": pickup.city,
                             "postal_code": pickup.postal_code or "-", "country": pickup.country, "state": ""})
        data["pickup_point"] = pickup
        data["delivery"] = request.POST.get("delivery", "")
        data["member"] = member
        data["gift_wrap"] = bool(request.POST.get("gift_wrap")) and _brand().gift_wrap_enabled
        data["gift_message"] = " ".join(request.POST.get("gift_message", "").split())[:300] if data["gift_wrap"] else ""
        missing = [f.replace("_", " ") for f in CHECKOUT_REQUIRED if not data[f]]
        pricing = _price_cart(subtotal, coupon, data["country"], count, lines=_cart_lines(items),
                              digital_only=_all_digital(i["product"] for i in items), member=member, pickup=pickup)
        try:
            if request.POST.get("delivery") == "pickup" and pickup is None:
                raise ValidationError("Choose a collection point.")
            if missing:
                raise ValidationError(f"Please fill in: {', '.join(missing)}.")
            from .countries import COUNTRIES
            if data["country"] not in dict(COUNTRIES):
                raise ValidationError("Please choose your country from the list.")
            if len("".join(ch for ch in data["phone"] if ch.isdigit())) < 6:
                raise ValidationError("Please enter a phone number the courier can call.")
            validate_email(data["email"])
            if not pricing["ships"]:
                raise ValidationError(f"Sorry, we don't deliver to {country_name(data['country'])} yet.")
            if payment_method == "cod":
                from . import cod
                cod_note = cod.problem(pricing["total"], data["email"], data["phone"]) or (
                    cod.problem(pricing["total"], request.user.email) if request.user.is_authenticated else "")
                if cod_note:
                    methods = [m for m in methods if m != "cod"]
                    raise ValidationError(cod_note)
                if cod.phone_check_on():
                    from . import sms
                    if not sms.normalize(data["phone"], data["country"]):
                        raise ValidationError("Please enter a mobile number we can text a confirmation code to (with country code, e.g. +92 300 1234567).")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            ctx = _checkout_context(request, items, pricing, coupon, methods, data)
            ctx["cod_note"] = cod_note
            return render(request, "bees/checkout.html", ctx)

        try:
            order = _place_order(request, items, data, payment_method, use_credit=request.POST.get("use_credit") == "1")
        except CheckoutError as exc:
            messages.error(request, str(exc))
            return redirect("cart")

        request.session["cart"] = {}
        _persist_cart(request)
        request.session["coupon_code"] = ""
        if not order.user_id:
            _remember_guest_order(request, order)
        from . import ads
        ads.attribute_order(request, order)
        request.session.modified = True

        if payment_method in ("card", "paypal") and order.total_cents > 0:
            from .currency import apply_presentment
            apply_presentment(order, payment_method)
        if payment_method == "card" and order.total_cents > 0:
            try:
                return redirect(payments.create_checkout_session(request, order))
            except payments.PaymentError as exc:
                payments.release_unpaid_order(order.id)
                _restore_cart_from_order(request, order)
                messages.error(request, str(exc))
                return redirect("cart")
        if payment_method == "paypal" and order.total_cents > 0:
            from . import paypal
            try:
                return redirect(paypal.create_order(request, order))
            except paypal.PayPalError as exc:
                payments.release_unpaid_order(order.id)
                _restore_cart_from_order(request, order)
                messages.error(request, f"PayPal isn't available right now ({exc}). Please try again or choose another way to pay.")
                return redirect("cart")

        if order.total_cents == 0:  # covered by coupon / store credit, nothing to charge
            order.payment_status = "paid"
            order.status = "confirmed"
            order._skip_status_email = True  # the confirmation email below covers it
            order.save(update_fields=["payment_status", "status"])

        if payment_method == "cod" and not order.cod_confirmed and order.total_cents:
            from . import cod
            if cod.phone_check_on():
                cod.send_code(order)
        _send_order_confirmation(request, order)
        if order.user_id:
            Notification.objects.create(user=request.user, message=f"Order #{order.id} placed successfully.", link="/my-orders/")
        return redirect("order_success", order_id=order.id)

    ctx = _checkout_context(request, items, pricing, coupon, methods)
    ctx["cod_note"] = cod_note
    from . import analytics
    marker = f"{timezone.localdate()}:{count}"
    if request.session.get("_co") != marker:
        request.session["_co"] = marker
        analytics.track(request, "checkout")
    return render(request, "bees/checkout.html", ctx)


def _store_credit(user):
    profile = Profile.objects.filter(user=user).only("store_credit").first()
    return profile.store_credit if profile else Decimal("0")


def taxes_table():
    from . import taxes
    return taxes.table()


def _checkout_context(request, items, pricing, coupon, methods, form=None):
    signed_in = request.user.is_authenticated
    if form is None:
        form = {"email": request.user.email, "full_name": request.user.get_full_name()} if signed_in else {}
        default = Address.objects.filter(user=request.user, is_default=True).first() if signed_in else None
        if default:
            form.update({f: getattr(default, f) for f in ("full_name", "phone", "address", "city", "state", "postal_code", "country")})
    from . import shipping as shipping_rules
    credit = _store_credit(request.user) if request.user.is_authenticated else Decimal("0")
    credit_applicable = min(credit, pricing["total"])
    checkout_data = {
        "subtotal": str(pricing["subtotal"]), "discount": str(pricing["discount"]), "tax": str(pricing["tax"]),
        "credit": str(credit), "currency": settings.STORE_CURRENCY.upper(),
        "shipping": shipping_rules.table(), "card": "card" in methods or "paypal" in methods, "items": sum(i["qty"] for i in items),
        "taxes": taxes_table(),
    }
    if _all_digital(i["product"] for i in items):
        checkout_data["shipping"] = {"flat": {"fee": "0", "free_over": None}}  # downloads don't ship
    from . import market
    from .models import CouponClaim, PickupPoint
    pickups = list(PickupPoint.objects.filter(active=True)[:200])
    checkout_data["member"] = bool(pricing.get("member"))
    checkout_data["free_ship"] = bool(coupon and coupon.free_shipping)
    checkout_data["pickups"] = {str(p.id): {"fee": str(p.fee), "country": p.country} for p in pickups}
    my_vouchers = []
    if request.user.is_authenticated:
        today = timezone.localdate()
        my_vouchers = [c.coupon for c in CouponClaim.objects.filter(user=request.user, coupon__active=True).select_related("coupon")
                       if not c.coupon.expiry_date or c.coupon.expiry_date >= today][:8]
    sellers = []
    for row in items:
        sa = row["product"].seller_account
        if sa and sa not in sellers:
            sellers.append(sa)
    from .currency import charge_currency_for
    charge_in = charge_currency_for("card") or charge_currency_for("paypal")
    return {
        "has_digital_only": _all_digital(i["product"] for i in items), "pickups": pickups, "my_vouchers": my_vouchers, "is_member": bool(pricing.get("member")),
        "has_digital": any(i["product"].is_digital for i in items),
        "charge_in": charge_in,
        "charge_paypal_ok": bool(charge_currency_for("paypal")),
        "checkout_sellers": sellers,
        "checkout_data": checkout_data,
        "store_credit": credit,
        "credit_applicable": credit_applicable,
        "total_after_credit": pricing["total"] - credit_applicable,
        "items": items,
        "pricing": pricing,
        "total": pricing["subtotal"],
        "discount_amount": pricing["discount"],
        "final_total": pricing["total"],
        "coupon": coupon,
        "addresses": Address.objects.filter(user=request.user) if signed_in else [],
        "guest": not signed_in,
        "payment_methods": methods,
        "form": form,
    }


def _cod_phone_check():
    from . import cod
    return cod.phone_check_on()


def _place_order(request, items, data, payment_method, use_credit=False):
    """Creates the order atomically: locks the product rows so two buyers
    can't both take the last unit, re-checks stock and the coupon inside
    the transaction, then decrements stock."""
    with transaction.atomic():
        product_ids = sorted({item["product"].id for item in items})
        locked = {p.id: p for p in Product.objects.select_for_update().filter(id__in=product_ids).order_by("id")}
        variant_ids = sorted(item["variant"].id for item in items if item.get("variant"))
        locked_variants = {v.id: v for v in ProductVariant.objects.select_for_update().filter(id__in=variant_ids).order_by("id")}

        subtotal = Decimal("0")
        lines = []
        line_quotes = []
        for item in items:
            product = locked.get(item["product"].id)
            if not product or not product.is_live:
                raise CheckoutError(f"{item['product'].name} is no longer available.")
            quote = None
            if item.get("quote"):
                from .models import QuoteRequest
                quote = QuoteRequest.objects.select_for_update().filter(pk=item["quote"].pk).first()
                if (quote is None or quote.status != "accepted" or not quote.offer_valid or quote.product_id != product.id
                        or not request.user.is_authenticated or quote.buyer_id != request.user.id):
                    raise CheckoutError(f"The quote for {product.name} is no longer valid. Please remove it from your cart.")
            sub = None
            if item.get("subscription"):
                from .models import Subscription
                sub = Subscription.objects.filter(pk=item["subscription"].pk, active=True, product_id=product.id).first()
                if sub is None or not request.user.is_authenticated or sub.user_id != request.user.id or not product.subscribe_percent:
                    raise CheckoutError(f"Your subscription for {product.name} is no longer active. Please remove it from your cart.")
            note = (item.get("personalization") or "").strip()
            if product.personalization_prompt and product.personalization_required and not note:
                raise CheckoutError(f"{product.name} needs your personalization ({product.personalization_prompt}). Please add it in your cart.")
            low, high = product.order_limits()
            if quote is not None or sub is not None:
                low, high = 1, None  # the quantity was agreed with the seller
            if item["qty"] < low:
                raise CheckoutError(f"{product.name} has a minimum order of {low}. Please update your cart.")
            if high and item["qty"] > high:
                raise CheckoutError(f"You can buy at most {high} of {product.name} per order. Please update your cart.")
            if product.is_digital and payment_method == "cod":
                raise CheckoutError(f"{product.name} is a digital download, so it has to be paid online.")
            variant = None
            if item.get("variant"):
                variant = locked_variants.get(item["variant"].id)
                if not variant or variant.product_id != product.id:
                    raise CheckoutError(f"That option of {product.name} is no longer available. Please update your cart.")
                if item["qty"] > variant.stock:
                    raise CheckoutError(f"Sorry, only {variant.stock} of {product.name} ({variant.label}) left in stock. Please update your cart.")
                price = variant.price if variant.price is not None else product.price
            else:
                if product.has_variants:
                    raise CheckoutError(f"Please choose a size/colour for {product.name}.")
                if item["qty"] > product.stock:
                    raise CheckoutError(f"Sorry, only {product.stock} of {product.name} left in stock. Please update your cart.")
                price = product.price
            if quote is not None:
                price = quote.offer_price
            elif sub is not None:
                from .market import subscription_price
                sub.product, sub.variant = product, variant
                price = subscription_price(sub)
            else:
                price = product.bulk_unit_price(price, item["qty"])
            subtotal += price * item["qty"]
            lines.append((product, variant, price, item["qty"]))
            line_quotes.append((quote, note[:product.personalization_max] if product.personalization_prompt else ""))

        coupon = None
        code = request.session.get("coupon_code", "")
        if code:
            coupon = Coupon.objects.select_for_update().filter(code__iexact=code).first()
            if coupon:
                is_valid, error = _coupon_check(coupon, request.user, [(p, price, qty) for p, _v, price, qty in lines], email=data["email"])
                if not is_valid:
                    raise CheckoutError(error)

        if coupon and coupon.seller_account_id:
            # A seller's code lowers that seller's line prices (the seller
            # pays for it); the order itself has no store discount.
            lines = [(p, v, coupon.unit_after(price) if coupon.applies_to(p) else price, qty) for p, v, price, qty in lines]
            subtotal = sum((price * qty for _p, _v, price, qty in lines), Decimal("0"))
            pricing = _price_cart(subtotal, None, data["country"], sum(qty for _p, _v, _price, qty in lines),
                                  digital_only=_all_digital(p for p, _v, _price, _q in lines), member=data.get("member", False),
                                  pickup=data.get("pickup_point"), free_ship=coupon.free_shipping)
        else:
            pricing = _price_cart(subtotal, coupon, data["country"], sum(qty for _p, _v, _price, qty in lines),
                                  digital_only=_all_digital(p for p, _v, _price, _q in lines), member=data.get("member", False),
                                  pickup=data.get("pickup_point"))
        if not pricing["ships"]:
            raise CheckoutError("Sorry, we don't deliver to that country yet.")
        is_card = payment_method in ("card", "paypal")  # paid online before it ships
        signed_in = request.user.is_authenticated
        from .email_i18n import remember, request_language
        shop_lang = request_language(request)
        remember(request.user, shop_lang)
        order = Order.objects.create(
            language=shop_lang,
            user=request.user if signed_in else None,
            guest_email="" if signed_in else data["email"],
            email=data["email"],
            full_name=data["full_name"][:150],
            address=data["address"][:255],
            city=data["city"][:100],
            state=data.get("state", "")[:100],
            postal_code=data["postal_code"][:20],
            country=data["country"],
            phone=data["phone"][:30],
            payment_method=payment_method,
            payment_status="pending" if is_card else "not_applicable",
            currency=settings.STORE_CURRENCY,
            coupon_code=coupon.code if coupon else "",
            discount_amount=pricing["discount"],
            shipping_amount=pricing["shipping"],
            tax_amount=pricing["tax"],
            tax_name=pricing["tax_name"][:30] if pricing["tax"] else "",
            tax_rate=pricing["tax_rate"] if pricing["tax"] else None,
            tax_included=pricing["tax_included"],
            customer_tax_id=request.POST.get("customer_tax_id", "").strip()[:40],
            gift_wrap=data.get("gift_wrap", False), gift_message=data.get("gift_message", ""),
            pickup_point=data.get("pickup_point"),
            estimated_delivery=order_emails.default_delivery_date(country=data["country"]),
            courier_name=tracking.suggest_courier(data["country"]),
            # Cash orders from accounts with an unconfirmed email need one
            # click on an emailed link before sellers pack them (stops fake orders).
            cod_confirmed=is_card or (not _cod_phone_check() and (not signed_in or _email_verified(request.user))),
        )
        seller_cache = {}
        for (product, variant, price, qty), (quote, note) in zip(lines, line_quotes):
            name = f"{product.name} ({variant.label})" if variant else product.name
            item = OrderItem(order=order, product=product, variant=variant, product_name=name[:255], price=price, quantity=qty,
                             quote=quote, personalization=note)
            if quote is not None:
                quote.status = "ordered"
                quote.log("system", f"Ordered in order #{order.id}")
                quote.save(update_fields=["status", "history", "updated_at"])
            seller = None
            if product.seller_account_id:
                if product.seller_account_id not in seller_cache:
                    seller_cache[product.seller_account_id] = product.seller_account
                seller = seller_cache[product.seller_account_id]
            item.apply_commission(seller)
            item.save()
            if variant:
                variant.stock -= qty
                variant.save(update_fields=["stock"])
            product.stock = max(product.stock - qty, 0)
            product.save(update_fields=["stock"])  # save() sends low-stock alerts
        if use_credit and signed_in:
            profile = Profile.objects.select_for_update().filter(user=request.user).first()
            if profile and profile.store_credit > 0:
                credit = min(profile.store_credit, order.grand_total)
                profile.store_credit -= credit
                profile.save(update_fields=["store_credit"])
                order.credit_used = credit
                order.save(update_fields=["credit_used"])
    return order


def _restore_cart_from_order(request, order):
    from .cart import make_key
    cart = request.session.get("cart", {})
    for item in order.items.all():
        if item.quote_id:
            from .models import QuoteRequest
            QuoteRequest.objects.filter(pk=item.quote_id, status="ordered").update(status="accepted")
            cart[f"q{item.quote_id}"] = item.quantity
        elif item.product_id:
            key = make_key(item.product_id, item.variant_id)
            cart[key] = cart.get(key, 0) + item.quantity
    request.session["cart"] = cart
    _persist_cart(request)
    if order.coupon_code and not request.session.get("coupon_code"):
        request.session["coupon_code"] = order.coupon_code
    request.session.modified = True


@require_POST
@ratelimit("coupon", rate_limit=15, window_seconds=600, redirect_to="checkout",
           message="Too many coupon tries. Please wait a few minutes.")
def apply_coupon(request):
    code = request.POST.get("coupon_code", "").strip()[:30]
    request.session["coupon_code"] = code
    request.session.modified = True
    if code:
        coupon = Coupon.objects.filter(code__iexact=code).first()
        if not coupon:
            request.session["coupon_code"] = ""
            messages.error(request, "That coupon code isn't valid.")
        else:
            cart_items, total, _ = _get_cart_items(request)
            is_valid, error = _coupon_check(coupon, request.user, _cart_lines(cart_items))
            if not is_valid:
                request.session["coupon_code"] = ""
                messages.error(request, error)
            else:
                messages.success(request, "Coupon applied: " + " + ".join(
                    ([f"{coupon.percent_off}% off"] if coupon.percent_off else []) + (["free shipping"] if coupon.free_shipping else []))
                                 + (f" products from {coupon.seller_account.display_name}." if coupon.seller_account_id else "."))
    return redirect("checkout")


def _remember_guest_order(request, order):
    ids = request.session.get("guest_orders", [])
    if order.pk not in ids:
        request.session["guest_orders"] = (ids + [order.pk])[-20:]


def _order_access(request, order_id, owner_only=False, lock=False):
    """The order if this visitor may see it: its owner, staff (unless
    ``owner_only``), or - for guest orders - the browser that placed it or
    anyone with its private tracking link (?t=token)."""
    qs = Order.objects.select_for_update() if lock else Order.objects.all()
    order = get_object_or_404(qs, pk=order_id)
    user = request.user
    if user.is_authenticated and order.user_id == user.id:
        return order
    if user.is_authenticated and user.is_staff and not owner_only:
        return order
    if order.user_id is None:
        token = request.GET.get("t", "")
        if order.pk in request.session.get("guest_orders", []) or (token and secrets.compare_digest(token, order.access_token)):
            _remember_guest_order(request, order)
            return order
    raise Http404


def _get_order_for_viewer(request, order_id):
    return _order_access(request, order_id)


def _orders_page(request, order):
    """Where to send a customer after an order action."""
    if order.user_id and request.user.is_authenticated:
        return redirect("my_orders")
    return redirect(order.tracking_path())


def order_success(request, order_id):
    order = _order_access(request, order_id)
    return render(request, "bees/order_success.html", {"order": order})


def order_track(request, order_id, token):
    """Private tracking page for guest orders (link is in every email)."""
    order = get_object_or_404(Order, pk=order_id)
    if not token or not secrets.compare_digest(token, order.access_token):
        raise Http404
    if order.user_id:
        if request.user.is_authenticated and request.user.id == order.user_id:
            return redirect("my_orders")
        return redirect(f"{reverse('login')}?{urlencode({'next': reverse('my_orders')})}")
    _remember_guest_order(request, order)
    order = Order.objects.prefetch_related("items", "items__product", "items__return_requests").get(pk=order.pk)
    return render(request, "bees/my_orders.html", {"orders": [order], "guest": True, "cancel_reasons": CANCEL_REASONS})


@login_required
def my_orders(request):
    orders = Order.objects.filter(user=request.user).prefetch_related(
        "items", "items__product", "items__return_requests", "items__seller_account", "items__claims"
    ).order_by("-created_at")
    reviewed = set(Review.objects.filter(user=request.user).values_list("product_id", flat=True))
    page_obj = Paginator(orders, 15).get_page(request.GET.get("page"))
    return render(request, "bees/my_orders.html", {"orders": page_obj, "page_obj": page_obj, "cancel_reasons": CANCEL_REASONS, "reviewed": reviewed})


@login_required
@require_POST
def seller_reply_review(request, pk):
    """A seller answers a review of their product (shown under it)."""
    seller, role = _approved_seller_or_404(request.user)
    review = get_object_or_404(Review, pk=pk, product__seller_account=seller)
    reply = request.POST.get("reply", "").strip()[:1000]
    review.seller_reply = reply
    review.seller_reply_at = timezone.now() if reply else None
    review.save(update_fields=["seller_reply", "seller_reply_at"])
    if reply and review.user_id:
        Notification.objects.create(user_id=review.user_id, link=f"/product/{review.product_id}/#reviews",
                                    message=f"{seller.display_name} replied to your review of '{review.product.name}'."[:255])
    messages.success(request, "Reply published." if reply else "Reply removed.")
    return redirect_back(request, "seller_reviews")


@login_required
@require_POST
def confirm_received(request, pk):
    """The customer confirms their parcel arrived (items already on their
    way). Marks them delivered so they can review them right away."""
    from . import fulfilment
    order = get_object_or_404(Order, pk=pk, user=request.user)
    items = list(order.items.filter(fulfillment_status="handed_to_courier"))
    if not items and order.status == "shipped":
        items = list(order.items.exclude(fulfillment_status="delivered"))
    if not items:
        messages.info(request, "There's nothing on its way in this order.")
        return redirect("my_orders")
    try:
        fulfilment.set_items_status(items, "delivered", user=request.user, by_customer=True)
    except fulfilment.FulfilmentError as exc:
        messages.error(request, str(exc))
        return redirect("my_orders")
    messages.success(request, "Thanks for confirming! You can now rate your items.")
    return redirect(f"{reverse('my_orders')}#order-{order.id}")


@require_POST
def cancel_order(request, pk):
    with transaction.atomic():
        order = _order_access(request, pk, owner_only=True, lock=True)
        if not order.is_cancellable:
            messages.error(request, "This order can no longer be cancelled.")
            return _orders_page(request, order)
        reason = request.POST.get("reason", "").strip()
        order.cancel_reason = reason if reason in CANCEL_REASONS else ""
        if order.payment_status == "paid":
            if not payments.refund_order(order):
                from .alerts import REFUND_FAILED, notify_staff
                notify_staff(f"Customer tried to cancel order #{order.id} but the Stripe refund failed. Refund it in Stripe, then cancel it here.",
                             link=f"/manage/orders/{order.id}/", category=REFUND_FAILED)
                messages.error(request, "We couldn't process the refund automatically. Our team has been alerted and will sort it out shortly.")
                return _orders_page(request, order)
            order.payment_status = "refunded"
        payments.void_pending_payment(order)
        order.status = "cancelled"
        order.save(update_fields=["status", "payment_status", "payment_method", "cod_fallback", "cancel_reason"])
        payments.restock(order)
    if order.payment_status == "refunded":
        card = order.card_label or "your card"
        messages.success(request, f"Order #{order.id} has been cancelled and {money(order.total)} refunded to {card}. Banks usually show it within 5-10 business days.")
    else:
        messages.success(request, f"Order #{order.id} has been cancelled.")
    return _orders_page(request, order)


CANCEL_REASONS = [
    "Ordered by mistake", "Found a better price", "Delivery takes too long", "Want to change the order",
    "Changed my mind", "Other",
]


@login_required
def request_return(request, item_id):
    item = get_object_or_404(OrderItem.objects.select_related("order"), pk=item_id, order__user=request.user)
    if not item.received_at:
        messages.error(request, "You can return an item once it has been delivered.")
        return redirect("my_orders")
    if not item.can_return:
        messages.error(request, f"The {_brand().return_days}-day return window for '{item.product_name}' has closed. Use 'Report a problem' if something is wrong.")
        return redirect("my_orders")
    if item.return_requests.exclude(status="rejected").exists():
        messages.error(request, "A return request already exists for this item.")
        return redirect("my_orders")

    if request.method == "POST":
        reason = request.POST.get("reason", "").strip()[:2000]
        refund_method = request.POST.get("refund_method", "original_payment")
        if refund_method not in dict(ReturnRequest.REFUND_METHOD_CHOICES):
            refund_method = "original_payment"
        if not reason:
            messages.error(request, "Please describe the reason for your return.")
        else:
            ReturnRequest.objects.create(
                order_item=item, user=request.user, reason=reason, refund_method=refund_method,
            )
            from .alerts import notify_staff
            notify_staff(f"Return requested for '{item.product_name}' (order #{item.order_id}).", link="/manage/returns/")
            if item.seller_account_id:
                from .seller_center import _seller_users
                Notification.objects.bulk_create([
                    Notification(user=u, link="/seller/returns/",
                                 message=f"Return requested for '{item.product_name}' (order #{item.order_id}).")
                    for u in _seller_users(item.seller_account)])
            messages.success(request, "Return request submitted. We'll review it shortly.")
            return redirect("my_orders")

    order = item.order
    return render(request, "bees/request_return.html", {
        "item": item,
        "refund_value": order.refund_value(item),
        "card_order": order.payment_status == "paid" and bool(order.stripe_payment_intent),
        "deadline": item.return_deadline(),
    })


@login_required
@require_POST
def buy_again(request, order_id):
    order = get_object_or_404(Order, pk=order_id, user=request.user)
    cart = request.session.get("cart", {})
    added, skipped = 0, 0
    from .cart import make_key
    for item in order.items.select_related("product", "variant"):
        product, variant = item.product, item.variant
        if product and product.has_variants and (not variant or variant.product_id != product.id):
            variant = None
            product = None  # that size/colour no longer exists
        stock = variant.stock if variant else (product.stock if product else 0)
        if not product or stock <= 0 or not product.is_live:
            skipped += 1
            continue
        key = make_key(product.id, variant.id if variant else None)
        cart[key] = min(cart.get(key, 0) + item.quantity, stock)
        added += 1
    request.session["cart"] = cart
    _persist_cart(request)
    request.session.modified = True
    if added:
        messages.success(request, f"{added} item(s) added back to your cart.")
    if skipped:
        messages.warning(request, f"{skipped} item(s) are no longer available and were skipped.")
    return redirect("cart")


def invoice_pdf(request, order_id):
    order = _get_order_for_viewer(request, order_id)

    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    brand = _brand()
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    def hex_to_rgb(value, fallback=(0.07, 0.09, 0.15)):
        try:
            value = value.lstrip("#")
            return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))
        except Exception:
            return fallback

    p.setFillColorRGB(*hex_to_rgb(brand.primary_color))
    p.rect(0, height - 64, width, 64, fill=1, stroke=0)
    p.setFillColorRGB(1, 1, 1)
    p.setFont("Helvetica-Bold", 20)
    p.drawString(40, height - 42, brand.site_name[:40])
    p.setFont("Helvetica", 10)
    p.drawRightString(width - 40, height - 40, "INVOICE")

    p.setFillColorRGB(0, 0, 0)
    p.setFont("Helvetica-Bold", 14)
    p.drawString(40, height - 96, f"Order #{order.id}")
    p.setFont("Helvetica", 10)
    lines = [
        f"Date: {order.created_at.strftime('%d %b %Y')}",
        f"Status: {order.get_status_display()}   Payment: {order.get_payment_status_display()}",
        f"Bill to: {order.full_name}",
        f"{order.address}, {order.city} {order.state} {order.postal_code} {order.country}".strip(),
        f"Phone: {order.phone}   Email: {order.contact_email}",
    ]
    if order.customer_tax_id:
        lines.append(f"Customer VAT / tax no.: {order.customer_tax_id}")
    if brand.store_tax_id:
        lines.append(f"Seller VAT / tax no.: {brand.store_tax_id}")
    y = height - 116
    for line in lines:
        p.drawString(40, y, line[:110])
        y -= 15

    y -= 20
    p.setFont("Helvetica-Bold", 10)
    p.drawString(40, y, "Item")
    p.drawString(320, y, "Qty")
    p.drawString(370, y, "Price")
    p.drawString(460, y, "Subtotal")
    y -= 16
    p.setFont("Helvetica", 10)
    for item in order.items.all():
        p.drawString(40, y, item.product_name[:45])
        p.drawString(320, y, str(item.quantity))
        p.drawString(370, y, money(item.price))
        p.drawString(460, y, money(item.subtotal))
        y -= 16
        if y < 120:
            p.showPage()
            y = height - 60

    y -= 6
    p.line(40, y, width - 40, y)
    y -= 18
    summary = [("Subtotal", order.subtotal)]
    if order.discount_amount:
        summary.append(("Discount", -order.discount_amount))
    summary.append(("Shipping", order.shipping_amount))
    if order.tax_amount:
        summary.append((order.tax_label, order.tax_amount))
    if order.credit_used:
        summary.append(("Store credit", -order.credit_used))
    for label, value in summary:
        p.drawString(370, y, f"{label}:")
        p.drawString(460, y, money(value))
        y -= 15
    p.setFont("Helvetica-Bold", 12)
    p.drawString(370, y - 4, "Total:")
    p.drawString(460, y - 4, money(order.total))

    if order.tax_included and order.tax_amount:
        p.setFont("Helvetica", 8)
        p.drawString(370, y - 20, f"Prices include {order.tax_label.replace(' (included)', '')}.")
    if brand.support_email or brand.company_address:
        p.setFont("Helvetica", 8)
        p.setFillColorRGB(0.4, 0.4, 0.4)
        p.drawString(40, 40, " · ".join(filter(None, [brand.company_address, brand.support_email]))[:140])

    p.showPage()
    p.save()
    buffer.seek(0)
    response = HttpResponse(buffer, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="invoice-{order.id}.pdf"'
    return response


# ---------------------------------------------------------------------------
# Stripe payments
# ---------------------------------------------------------------------------

def payment_success(request):
    session_id = request.GET.get("session_id", "")
    session = payments.retrieve_session(session_id) if session_id and payments.is_configured() else None
    if not session:
        messages.info(request, "We're confirming your payment. We'll email you as soon as it's done.")
        return redirect("my_orders" if request.user.is_authenticated else "home")
    order_id = (session.get("metadata") or {}).get("order_id")
    if not str(order_id or "").isdigit():
        raise Http404
    order = _order_access(request, int(order_id), owner_only=True)
    order, newly_paid = payments.mark_order_paid(order.id, session)
    if newly_paid:
        _send_order_confirmation(request, order)
        if order.user_id:
            Notification.objects.create(user=order.user, message=f"Payment received for order #{order.id}.", link="/my-orders/")
    if order.payment_status == "paid":
        messages.success(request, "Payment received - thank you!")
    else:
        messages.info(request, "Your payment is processing. We'll email you as soon as it's confirmed.")
    return redirect("order_success", order_id=order.id)


@require_POST
def pay_online(request, order_id):
    """Lets a customer pay a cash-on-delivery order online in advance."""
    with transaction.atomic():
        order = _order_access(request, order_id, owner_only=True, lock=True)
        if not payments.is_configured():
            messages.error(request, "Online payment isn't available right now. You can pay cash on delivery.")
            return _orders_page(request, order)
        if order.payment_method != "cod" or order.payment_status != "not_applicable" or order.status not in ("pending", "confirmed") or not order.total_cents:
            messages.info(request, "This order can't be paid online.")
            return _orders_page(request, order)
        order.cod_fallback = True
        order.payment_method = "card"
        order.payment_status = "pending"
        order.save(update_fields=["cod_fallback", "payment_method", "payment_status"])
    from .currency import apply_presentment
    apply_presentment(order, "card")
    try:
        return redirect(payments.create_checkout_session(request, order))
    except payments.PaymentError as exc:
        payments.release_unpaid_order(order.id)
        messages.error(request, str(exc))
        return _orders_page(request, order)


def payment_cancel(request, order_id):
    order = _order_access(request, order_id, owner_only=True)
    if order.payment_status == "pending" and order.stripe_session_id and payments.is_configured():
        session = payments.retrieve_session(order.stripe_session_id)
        if session and session.get("payment_status") == "paid":
            return redirect(f"{reverse('payment_success')}?session_id={order.stripe_session_id}")
        try:
            payments._stripe().checkout.Session.expire(order.stripe_session_id)
        except Exception:
            pass
    if order.payment_status == "pending":
        was_cod = order.cod_fallback
        payments.release_unpaid_order(order.id, reason="failed")
        if was_cod:
            messages.info(request, "Online payment cancelled - nothing was charged. Your order stays on cash on delivery.")
            return _orders_page(request, order)
        _restore_cart_from_order(request, order)
        messages.info(request, "Payment cancelled - nothing was charged. Your items are back in your cart.")
        return redirect("cart")
    return _orders_page(request, order)


def resume_payment(request, order_id):
    order = _order_access(request, order_id, owner_only=True)
    if order.payment_status == "pending" and order.payment_method == "paypal" and order.status != "cancelled":
        from . import paypal
        try:
            return redirect(paypal.create_order(request, order))
        except paypal.PayPalError as exc:
            messages.error(request, f"PayPal isn't available right now ({exc}).")
            return _orders_page(request, order)
    if order.payment_status != "pending" or not order.stripe_session_id:
        return _orders_page(request, order)
    session = payments.retrieve_session(order.stripe_session_id)
    if session and session.get("status") == "open" and session.get("url"):
        return redirect(session["url"])
    if session and session.get("payment_status") == "paid":
        return redirect(f"{reverse('payment_success')}?session_id={order.stripe_session_id}")
    messages.error(request, "That payment link has expired. Please place the order again.")
    return _orders_page(request, order)


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """Stripe calls this for payment events. Signature-verified, idempotent."""
    if not (payments.is_configured() and payments.webhook_configured()):
        return HttpResponse(status=503)
    try:
        event = payments.parse_webhook(request.body, request.META.get("HTTP_STRIPE_SIGNATURE", ""))
    except ValueError:
        # Stripe (or someone) reached the webhook but the signature didn't
        # match: almost always the wrong STRIPE_WEBHOOK_SECRET in .env.
        from .alerts import WEBHOOK_REJECTED, log_once
        log_once(WEBHOOK_REJECTED, "a message arrived but its signature didn't match STRIPE_WEBHOOK_SECRET")
        return HttpResponse(status=400)

    event_type = event.get("type", "")
    SiteSettings.objects.filter(pk=1).update(stripe_last_webhook=timezone.now())
    from .alerts import payments_working
    payments_working("message received from Stripe")
    session = (event.get("data") or {}).get("object") or {}
    if event_type.startswith("charge."):
        _stripe_charge_event(event_type, session)
        return HttpResponse(status=200)
    gift_card_id = (session.get("metadata") or {}).get("gift_card_id")
    if gift_card_id and event_type.startswith("checkout.session."):
        if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            from . import giftcards
            with transaction.atomic():
                giftcards.confirm_stripe(gift_card_id, session)
        return HttpResponse(status=200)
    plan_payment_id = (session.get("metadata") or {}).get("plan_payment_id")
    if plan_payment_id and event_type.startswith("checkout.session."):
        from .models import PlanPayment
        if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            payments.confirm_plan_payment(plan_payment_id, session)
        elif event_type in ("checkout.session.expired", "checkout.session.async_payment_failed"):
            PlanPayment.objects.filter(pk=plan_payment_id, status="pending").update(status="failed")
        return HttpResponse(status=200)
    order_id = (session.get("metadata") or {}).get("order_id")
    if not order_id:
        return HttpResponse(status=200)

    if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        order, newly_paid = payments.mark_order_paid(order_id, session)
        if order and newly_paid:
            _send_order_confirmation(request, order)
            if order.user:
                Notification.objects.create(user=order.user, message=f"Payment received for order #{order.id}.", link="/my-orders/")
    elif event_type in ("checkout.session.expired", "checkout.session.async_payment_failed"):
        order = Order.objects.filter(pk=order_id).first()
        if order and order.stripe_session_id == session.get("id"):
            payments.release_unpaid_order(order.id, reason="failed")
    return HttpResponse(status=200)


def _stripe_charge_event(event_type, charge):
    """Refunds and disputes that happen in the Stripe dashboard."""
    from .alerts import STRIPE_EVENT, notify_staff
    intent = charge.get("payment_intent") or ""
    if event_type.startswith("charge.dispute"):
        intent = charge.get("payment_intent") or ""
        order = Order.objects.filter(stripe_payment_intent=intent).first() if intent else None
        amount = money(Decimal(charge.get("amount", 0)) / 100)
        notify_staff(f"A customer disputed a payment of {amount}{f' (order #{order.id})' if order else ''}. Respond in the Stripe dashboard before the deadline.",
                     link=f"/manage/orders/{order.id}/" if order else "/manage/orders/", category=STRIPE_EVENT)
        return
    order = Order.objects.filter(stripe_payment_intent=intent).first() if intent else None
    if not order:
        return
    if charge.get("refunded") and order.payment_status != "refunded":
        Order.objects.filter(pk=order.pk).update(payment_status="refunded")
        if order.status not in ("cancelled", "delivered"):
            notify_staff(f"Order #{order.id} was fully refunded in Stripe. Cancel it here if it shouldn't ship.",
                         link=f"/manage/orders/{order.id}/", category=STRIPE_EVENT)
        else:
            from .alerts import log
            log(f"{STRIPE_EVENT}: order #{order.id} fully refunded in Stripe")


# ---------------------------------------------------------------------------
# Content pages
# ---------------------------------------------------------------------------

def help_support(request):
    name = _store_name()
    brand = _brand()
    pay = "secure card payments (Visa, Mastercard, American Express, Apple Pay and Google Pay)"
    if brand.allow_cash_on_delivery:
        pay += " and cash on delivery where available"
    faqs = [
        ("How do I place an order?", "Add products to your cart, open your cart and choose 'Checkout'. Enter your shipping details, pick a payment method and confirm."),
        ("Which payment methods do you accept?", f"We accept {pay}. Card payments are processed by Stripe - we never see or store your card number."),
        ("How can I track my order?", "Sign in and open 'My orders', or use the tracking link in your order email if you checked out as a guest. You'll see the status and tracking details once it ships."),
        ("Can I return a product?", f"Yes. Open the order in 'My orders' and choose 'Request return' within {brand.return_days} days of delivery. Our team reviews requests within 1-2 business days."),
        ("Can I cancel an order?", "You can cancel from 'My orders' until it ships. Paid orders are refunded to your card automatically."),
        ("I forgot my password - what now?", "Choose 'Forgot password?' on the sign-in page and we'll email you a secure reset link."),
    ]
    return render(request, "bees/help.html", {"faqs": faqs, "store_name": name})


def _sell_content():
    name = _store_name()
    return {
        "page_title": f"Sell on {name}",
        "sections": [
            ("Reach more customers", f"Open your shop on {name} and put your products in front of shoppers browsing every category."),
            ("Simple, transparent fees", "Individual sellers pay a 10% commission per sale and registered businesses 20%. Your rate drops automatically as your sales grow."),
            ("Everything in one dashboard", "Get approved, list products, fulfil orders, invite your team and track earnings from your seller dashboard."),
        ],
        "cta_url": reverse("become_seller"),
        "cta_label": "Apply to become a seller",
    }


def _about_content():
    name = _store_name()
    return {
        "page_title": f"About {name}",
        "sections": [
            ("Who we are", f"{name} is an online marketplace connecting customers with trusted brands and independent sellers across beauty, electronics, fashion, home and more."),
            ("Our promise", "Carefully reviewed sellers, secure checkout, honest reviews from verified buyers, and support that actually answers."),
        ],
    }


def _terms_content():
    name = _store_name()
    return {
        "page_title": "Terms of Service",
        "sections": [
            (f"Using {name}", f"By creating an account or placing an order on {name}, you agree to these terms. Please use the platform responsibly and in line with applicable laws."),
            ("Accounts", "You're responsible for keeping your account credentials secure. Seller accounts are reviewed and approved before going live."),
            ("Orders and payments", "An order is confirmed once payment is received (or, for cash on delivery, once you confirm it). Prices and availability may change without notice. Card payments are processed by Stripe and PayPal payments by PayPal; we never see your full card number. Some cash on delivery orders must be confirmed with a code sent to your phone."),
            ("Marketplace sellers", f"Many products are sold by independent sellers, shown on each product. {name} runs the platform, takes payments and handles returns and problem reports between you and the seller."),
            ("Returns and refunds", f"Eligible items can be returned within {_brand().return_days} days of delivery. Approved refunds go back to the original payment method, or as store credit if you choose it. Cash on delivery refunds are paid back directly or as store credit."),
            ("Gift cards and store credit", "Gift cards are valid for 3 years from purchase, are added to your account as store credit, can't be exchanged for cash (except where the law requires it) and are like cash - keep the code private. Store credit is used automatically at checkout."),
            ("Sponsored products", "Products marked \"Sponsored\" are paid placements by sellers. They're shown in addition to normal results."),
            ("Seller terms", f"Sellers must list only items they're allowed to sell, describe them honestly, ship on time and follow the law of the countries they sell to. {name} keeps the commission shown in Seller Center on each sale. Earnings become available once an item is delivered and its return window has passed, and are paid out by bank transfer, PayPal or Stripe; sponsored-product costs are taken from earnings. {name} may hold earnings for open returns, problem reports or suspected fraud, and may suspend accounts that break these terms."),
            ("Liability", "We do our best to keep the site running and information correct, but it's provided as-is. Nothing in these terms limits rights you have under consumer law in your country."),
        ],
    }


def _privacy_content():
    name = _store_name()
    return {
        "page_title": "Privacy Policy",
        "sections": [
            ("What we collect", "Information you give us when you create an account, place an order or apply to sell - such as your name, email, shipping address and phone number. Card details are entered directly with Stripe and never reach our servers."),
            ("How we use it", f"To process and deliver orders, provide support, prevent fraud and improve {name}. We never sell your personal data."),
            ("Who we share it with", "Only the services needed to run the store: the seller who ships your order, payment providers (Stripe, PayPal), couriers, our email provider and hosting. Each receives only what it needs."),
            ("Sign-in, texts and notifications", "If you sign in with Google or Apple we receive your name and email from them. With Twilio we text order updates and confirmation codes to your phone (turn them off on your Privacy page). Phone notifications are only sent to devices where you allowed them."),
            ("Statistics", "We count visits, product views and searches as anonymous daily totals - without cookies, IP addresses or anything that identifies you."),
            ("Cookies", "Essential cookies keep you signed in, remember your cart, language and currency. Analytics or marketing cookies (if the store uses them) are only set after you accept them in the cookie banner - you can change your choice any time from 'Cookie settings' at the bottom of every page."),
            ("How long we keep it", "Account details while your account is open. Order and invoice records for as long as tax law requires (usually 6-10 years). You can close your account at any time from Account > Privacy."),
            ("Your rights (GDPR, UK GDPR, CCPA and similar laws)", "You can see, correct, download (Account > Privacy > Download your data) or delete your data, object to marketing, and withdraw consent at any time. You can also complain to your local data-protection authority. We never sell personal data."),
            ("International transfers", "Our service providers may process data outside your country. When they do, they use recognised safeguards such as standard contractual clauses."),
            ("Contact", f"Questions or requests: {_brand().support_email or 'use the Help page'}."),
        ],
    }


BUILT_IN_PAGES = {"sell": lambda: _sell_content(), "about": lambda: _about_content(),
                  "terms": lambda: _terms_content(), "privacy": lambda: _privacy_content()}


def _static_page(request, slug, default):
    """Built-in information page, or the store's own version of it
    (Manage > Pages)."""
    from . import content
    page = content.published_page(slug)
    if page is not None:
        return render(request, "bees/static_page.html", {
            "page_title": page.title, "blocks": content.parse_body(page.body), "page": page,
            "cta_url": default.get("cta_url"), "cta_label": default.get("cta_label")})
    return render(request, "bees/static_page.html", default)


def custom_page(request, slug):
    from . import content
    page = content.published_page(slug)
    if page is None or page.is_built_in:
        raise Http404("Page not found.")
    return render(request, "bees/static_page.html", {"page_title": page.title, "blocks": content.parse_body(page.body), "page": page})


def sell_on_bees(request):
    return _static_page(request, "sell", _sell_content())


def about_us(request):
    return _static_page(request, "about", _about_content())


def terms_page(request):
    return _static_page(request, "terms", _terms_content())


def privacy_page(request):
    return _static_page(request, "privacy", _privacy_content())


@require_POST
@ratelimit("newsletter", rate_limit=5, window_seconds=600, redirect_to="home")
def newsletter_subscribe(request):
    email = request.POST.get("email", "").strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, "Please enter a valid email address.")
        return redirect_back(request, "home")
    NewsletterSubscriber.objects.get_or_create(email=email)
    messages.success(request, "Thanks for subscribing!")
    return redirect_back(request, "home")


# ---------------------------------------------------------------------------
# Sellers
# ---------------------------------------------------------------------------

@login_required
def become_seller(request):
    existing = SellerAccount.objects.filter(user=request.user).first()
    if existing:
        return redirect("seller_dashboard")

    form = {}
    if request.method == "POST":
        from .countries import COUNTRIES
        form = {k: request.POST.get(k, "").strip() for k in ("account_type", "business_name", "phone", "country", "agree")}
        account_type = form["account_type"] if form["account_type"] in dict(SellerAccount.ACCOUNT_TYPE_CHOICES) else "individual"
        name = " ".join(form["business_name"].split())[:150]
        error = ""
        if len(name) < 2:
            error = "Please enter your store name."
        elif SellerAccount.objects.filter(Q(business_name__iexact=name) | Q(organization_name__iexact=name)).exists() \
                or name.lower() in ("official store", "store", "admin", "support"):
            error = "That store name is already taken. Please choose another."
        elif len("".join(ch for ch in form["phone"] if ch.isdigit())) < 7:
            error = "Please enter a phone number with the country code."
        elif form["country"] not in {cname for _code, cname in COUNTRIES}:
            error = "Please choose your country."
        elif not form["agree"]:
            error = "Please accept the seller terms."
        if error:
            messages.error(request, error)
            return render(request, "bees/become_seller.html", {"form": form})
        try:
            with transaction.atomic():
                SellerAccount.objects.create(user=request.user, account_type=account_type, business_name=name,
                                             phone=form["phone"][:30], country=form["country"][:100], terms_accepted_at=timezone.now())
        except IntegrityError:
            return redirect("seller_dashboard")  # double click: the account already exists
        from .alerts import notify_staff
        notify_staff(f"New seller application: {name} ({form['country']}).", link="/manage/sellers/?status=pending")
        messages.success(request, "Your seller application has been submitted. We'll review it shortly.")
        return redirect("seller_dashboard")

    return render(request, "bees/become_seller.html", {"form": form})


@login_required
@require_POST
def update_fulfillment_status(request, item_id):
    """Seller Center: change one item's step (packed, shipped, delivered)."""
    from . import fulfilment
    seller, role = get_seller_account_for_user(request.user)
    if not seller or seller.status != "approved":
        messages.error(request, "Only approved sellers can update orders.")
        return redirect_back(request, "seller_orders")
    item = get_object_or_404(OrderItem.objects.select_related("order"), pk=item_id, seller_account=seller)
    new_status = request.POST.get("fulfillment_status")
    if new_status not in dict(OrderItem.FULFILLMENT_CHOICES):
        messages.error(request, "Please choose a valid status.")
        return redirect_back(request, "seller_orders")
    try:
        changed, moved = fulfilment.set_items_status([item], new_status, user=request.user)
    except fulfilment.FulfilmentError as exc:
        messages.error(request, str(exc))
        return redirect_back(request, "seller_orders")
    if not changed:
        messages.info(request, "Nothing to change.")
    elif moved:
        messages.success(request, f"Marked '{item.product_name}' as {dict(OrderItem.FULFILLMENT_CHOICES)[new_status].lower()}. "
                                  f"Order #{item.order_id} is now {moved[0][1]} and the customer has been told.")
    else:
        told = " The customer has been told." if new_status in ("handed_to_courier", "delivered") else ""
        messages.success(request, f"Marked '{item.product_name}' as {dict(OrderItem.FULFILLMENT_CHOICES)[new_status].lower()}.{told}")
    return redirect_back(request, "seller_orders")


def sync_order_status_from_items(order):
    """Moves the order forward once every item has reached the next step
    (one order can contain items from several sellers): all handed to the
    courier -> shipped, all delivered -> delivered. Returns the new status
    label, or None if nothing changed."""
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        if order.status in ("cancelled", "delivered"):
            return None
        states = set(order.items.values_list("fulfillment_status", flat=True))
        if not states:
            return None
        if states == {"delivered"}:
            order.status = "delivered"
        elif states <= {"handed_to_courier", "delivered"} and order.status in ("pending", "confirmed"):
            order.status = "shipped"
        else:
            return None
        if not order.estimated_delivery:
            order.estimated_delivery = order_emails.default_delivery_date(order)
        order.save(update_fields=["status", "estimated_delivery"])
    return order.get_status_display().lower()


def _seller_form(request, seller, role, context):
    """Shows the product form inside the Seller Center layout."""
    from .seller_views import _render as seller_render
    request.seller, request.seller_role = seller, role
    context["section"] = "products"
    context.update(product_form_extras())
    return seller_render(request, "product_form.html", context)


def product_form_extras():
    from .countries import COUNTRIES
    return {"conditions": Product.CONDITIONS, "country_list": COUNTRIES}


def _approved_seller_or_404(user):
    seller, role = get_seller_account_for_user(user)
    if not seller or seller.status != "approved":
        raise Http404("No approved seller account found for this user.")
    return seller, role


def _product_fields_from_post(request):
    """Validates the seller product form. Returns a dict of clean values."""
    name = request.POST.get("name", "").strip()[:255]
    if not name:
        raise ValidationError("Product name is required.")
    category = request.POST.get("category", "")
    if category not in dict(category_choices()):
        raise ValidationError("Please choose a valid category.")
    price = _parse_decimal(request.POST.get("price"), "Price", minimum=Decimal("0.01"))
    old_price = _parse_decimal(request.POST.get("old_price"), "Original price", required=False)
    if old_price is not None and old_price <= price:
        old_price = None
    discount = _parse_int(request.POST.get("discount_percent"), "Discount", minimum=0, maximum=95)
    stock = _parse_int(request.POST.get("stock"), "Stock", minimum=0, maximum=1_000_000)
    image_url = request.POST.get("image_url", "").strip()[:500]
    if image_url and not is_image_link(image_url):
        raise ValidationError("Image link must start with https:// (a direct link to the picture).")
    uploaded = request.FILES.get("image_file")
    if uploaded:
        validate_image_upload(uploaded)
        from .images import save_public
        image_url = save_public(uploaded, "products")
    bulk_qty = _parse_int(request.POST.get("bulk_min_qty") or "0", "Quantity offer: minimum quantity", minimum=0, maximum=1000)
    bulk_pct = _parse_int(request.POST.get("bulk_percent") or "0", "Quantity offer: discount", minimum=0, maximum=50)
    if bulk_qty == 1:
        raise ValidationError("Quantity offer: the minimum quantity should be 2 or more.")
    if bool(bulk_qty) != bool(bulk_pct):
        raise ValidationError("Quantity offer: fill in both the quantity and the discount, or leave both empty.")
    return {
        "name": name, "category": category, "price": price, "old_price": old_price,
        "discount_percent": discount, "stock": stock, "image_url": image_url,
        "description": request.POST.get("description", "").strip()[:10000],
        "bulk_min_qty": bulk_qty or None, "bulk_percent": bulk_pct,
        "weight_grams": _parse_int(request.POST.get("weight_grams"), "Weight", minimum=1, maximum=100000, default=None),
        "length_cm": _parse_decimal(request.POST.get("length_cm"), "Length", required=False),
        "width_cm": _parse_decimal(request.POST.get("width_cm"), "Width", required=False),
        "height_cm": _parse_decimal(request.POST.get("height_cm"), "Height", required=False),
        "seo_title": request.POST.get("seo_title", "").strip()[:70],
        "seo_description": " ".join(request.POST.get("seo_description", "").split())[:170],
        **_guide_fields_from_post(request),
        **_extra_product_fields(request),
    }


def _selling_fields(request):
    """Condition, origin, personalization, offers, Subscribe & Save and auctions."""
    from datetime import datetime, timedelta
    from .countries import COUNTRIES
    from .models import Product
    condition = request.POST.get("condition") or "new"
    if condition not in dict(Product.CONDITIONS):
        raise ValidationError("Choose the item's condition.")
    ships_from = request.POST.get("ships_from", "").strip().upper()[:2]
    if ships_from and ships_from not in dict(COUNTRIES):
        raise ValidationError("Ships from: choose a country.")
    floor = _parse_decimal(request.POST.get("offer_floor"), "Auto-decline offers below", required=False)
    fields = {
        "condition": condition, "ships_from": ships_from,
        "personalization_prompt": " ".join(request.POST.get("personalization_prompt", "").split())[:120],
        "personalization_required": bool(request.POST.get("personalization_required")),
        "personalization_max": _parse_int(request.POST.get("personalization_max") or "60", "Personalization length", minimum=1, maximum=300),
        "accepts_offers": bool(request.POST.get("accepts_offers")), "offer_floor": floor,
        "subscribe_percent": _parse_int(request.POST.get("subscribe_percent") or "0", "Subscribe & Save discount", minimum=0, maximum=50),
        "is_auction": bool(request.POST.get("is_auction")),
    }
    if fields["is_auction"]:
        start = _parse_decimal(request.POST.get("auction_start_price"), "Starting bid", minimum=Decimal("0.01"))
        reserve = _parse_decimal(request.POST.get("auction_reserve"), "Reserve price", required=False)
        try:
            ends = timezone.make_aware(datetime.fromisoformat(request.POST.get("auction_ends_at", "").strip()))
        except ValueError:
            raise ValidationError("Auction: choose when bidding ends.")
        if ends <= timezone.now() + timedelta(hours=1) or ends > timezone.now() + timedelta(days=30):
            raise ValidationError("Auction: the end must be between 1 hour and 30 days from now.")
        if reserve is not None and reserve < start:
            raise ValidationError("Auction: the reserve price can't be lower than the starting bid.")
        fields.update({"auction_start_price": start, "auction_reserve": reserve, "auction_ends_at": ends})
    return fields


def check_digital(fields, product=None):
    """A digital product needs its file."""
    if fields.get("is_digital"):
        has_file = fields["digital_file"] if "digital_file" in fields else (product.digital_file if product else None)
        if not has_file:
            raise ValidationError("Digital download: choose the file buyers will download.")


DIGITAL_TYPES = (".pdf", ".zip", ".epub", ".mp3", ".mp4", ".m4a", ".wav", ".png", ".jpg", ".jpeg", ".svg", ".psd",
                 ".ai", ".docx", ".xlsx", ".pptx", ".txt", ".csv", ".stl", ".obj", ".3mf", ".ttf", ".otf")
DIGITAL_MAX_MB = 200


def _extra_product_fields(request):
    """Brand, specifications, video, order limits, quotes, digital download
    and EU product-safety details (seller and store product forms)."""
    from .models import Product
    video = request.POST.get("video_url", "").strip()[:300]
    if video and Product(video_url=video).video() is None:
        raise ValidationError("Video: paste a YouTube or Vimeo link, or an https:// address ending in .mp4.")
    low = _parse_int(request.POST.get("min_order_qty") or "1", "Minimum order", minimum=1, maximum=10000)
    high = _parse_int(request.POST.get("max_order_qty"), "Maximum order", minimum=1, maximum=10000, default=None)
    if high is not None and high < low:
        raise ValidationError("Maximum order can't be lower than the minimum order.")
    specs = "\n".join(line.strip()[:360] for line in request.POST.get("specs", "").splitlines() if line.strip())[:4000]
    if specs and not all(":" in line for line in specs.splitlines()):
        raise ValidationError("Specifications: write one per line as 'Name: value', e.g. 'Material: Cotton'.")
    fields = {
        "brand": " ".join(request.POST.get("brand", "").split())[:80],
        "specs": specs, "video_url": video, "min_order_qty": low, "max_order_qty": high,
        "quote_only": bool(request.POST.get("quote_only")),
        "manufacturer": " ".join(request.POST.get("manufacturer", "").split())[:300],
        "eu_responsible_person": " ".join(request.POST.get("eu_responsible_person", "").split())[:300],
        "safety_info": request.POST.get("safety_info", "").strip()[:3000],
        "is_digital": bool(request.POST.get("is_digital")),
        "download_limit": _parse_int(request.POST.get("download_limit") or "5", "Download limit", minimum=1, maximum=100),
    }
    fields.update(_selling_fields(request))
    upload = request.FILES.get("digital_file")
    if upload:
        name = upload.name.lower()
        if not name.endswith(DIGITAL_TYPES):
            raise ValidationError("Digital file: use PDF, ZIP, EPUB, MP3, MP4, images, Office files, fonts or 3D-print files.")
        if upload.size > DIGITAL_MAX_MB * 1024 * 1024:
            raise ValidationError(f"Digital file: the largest allowed is {DIGITAL_MAX_MB} MB.")
        fields["digital_file"] = upload
    elif request.POST.get("remove_digital_file"):
        fields["digital_file"] = None
    return fields


def _guide_fields_from_post(request):
    fields = {f"guide_{k}": "\n".join(line.strip() for line in request.POST.get(f"guide_{k}", "").splitlines() if line.strip())[:2000]
              for k in ("uses", "pros", "cons")}
    source = request.POST.get("guide_source", "")
    fields["guide_source"] = (source if source in ("ai", "seller") else "seller") if any(fields.values()) else ""
    return fields


MAX_GALLERY = 8


def is_image_link(url):
    return url.startswith("https://") or url.startswith(settings.MEDIA_URL)


def gallery_from_post(request, product=None):
    """Extra product pictures after this form: the existing ones minus any
    ticked "Remove", plus pasted links and uploaded files (up to 8).
    Validates everything before saving any file."""
    remove = {int(i) for i in request.POST.getlist("remove_extra") if i.isdigit()}
    urls = [img.image_url for img in product.extra_images.all() if img.id not in remove] if product and product.pk else []
    # Older admin form: one textarea with every gallery link.
    legacy = "extra_images" in request.POST and "extra_image_links" not in request.POST
    if legacy:
        urls = []
        links = request.POST.get("extra_images", "")
    else:
        links = request.POST.get("extra_image_links", "")
    for line in links.splitlines():
        line = line.strip()[:500]
        if not line or (legacy and not is_image_link(line)):
            continue
        if not is_image_link(line):
            raise ValidationError(f"Gallery links must start with https:// ({line[:60]}).")
        if line not in urls:
            urls.append(line)
    files = [f for f in request.FILES.getlist("extra_files") if f]
    if len(urls) + len(files) > MAX_GALLERY:
        raise ValidationError(f"The gallery can hold up to {MAX_GALLERY} extra pictures.")
    for f in files:
        validate_image_upload(f)
    return urls, files


def save_gallery(product, urls, files):
    from .images import save_public
    urls = list(urls) + [save_public(f, "products") for f in files]
    product.extra_images.all().delete()
    ProductImage.objects.bulk_create([ProductImage(product=product, image_url=u[:500]) for u in urls[:MAX_GALLERY]])
    return urls


def parse_variant_rows(request):
    """Reads the size/colour rows from a product form. Blank rows are
    ignored. Raises ValidationError for bad values or duplicates."""
    post = request.POST
    ids, sizes, colors = post.getlist("variant_id"), post.getlist("variant_size"), post.getlist("variant_color")
    prices, stocks, skus = post.getlist("variant_price"), post.getlist("variant_stock"), post.getlist("variant_sku")
    rows, seen = [], set()
    for i, size in enumerate(sizes):
        size = size.strip()[:40]
        color = (colors[i] if i < len(colors) else "").strip()[:40]
        if not size and not color:
            continue
        combo = (size.lower(), color.lower())
        if combo in seen:
            raise ValidationError(f"The option {' / '.join(p for p in (size, color) if p)} is listed twice.")
        seen.add(combo)
        price = _parse_decimal(prices[i] if i < len(prices) else "", f"Price for {size or color}", required=False, minimum=Decimal("0.01"))
        stock = _parse_int(stocks[i] if i < len(stocks) else "0", f"Stock for {size or color}", minimum=0, maximum=1_000_000)
        vid = ids[i] if i < len(ids) else ""
        rows.append({"id": int(vid) if vid.isdigit() else None, "size": size, "color": color, "price": price,
                     "stock": stock, "sku": (skus[i] if i < len(skus) else "").strip()[:60]})
    return rows


def apply_variant_rows(product, rows):
    """Saves the rows as the product's variants (update / add / remove)
    and refreshes the product's total stock."""
    existing = {v.id: v for v in product.variants.all()}
    keep = set()
    for position, row in enumerate(rows):
        variant = existing.get(row["id"]) or ProductVariant(product=product)
        for field in ("size", "color", "price", "stock", "sku"):
            setattr(variant, field, row[field])
        variant.position = position
        variant.save()
        keep.add(variant.id)
    ProductVariant.objects.filter(product=product).exclude(id__in=keep).delete()
    product.sync_variants()


def variant_rows_for_form(request, product=None):
    if request.method == "POST":
        try:
            return parse_variant_rows(request)
        except ValidationError:
            post = request.POST
            return [{"id": "", "size": s, "color": c, "price": p, "stock": st, "sku": k} for s, c, p, st, k in zip(
                post.getlist("variant_size"), post.getlist("variant_color"), post.getlist("variant_price"),
                post.getlist("variant_stock"), post.getlist("variant_sku"))]
    return list(product.variants.all()) if product else []


@login_required
def seller_add_product(request):
    seller, role = _approved_seller_or_404(request.user)
    if not seller.can_add_product:
        messages.error(request, f"Your {seller.active_plan.name} plan allows {seller.product_limit} products. "
                                "Upgrade your plan to add more, or remove a product you no longer sell.")
        return redirect("seller_plan")
    oid = request.POST.get("offer_of") or request.GET.get("offer_of") or ""
    master = Product.objects.live().filter(pk=oid if str(oid).isdigit() else 0, offer_of__isnull=True).exclude(seller_account=seller).first()
    if request.method == "POST":
        try:
            variant_rows = parse_variant_rows(request)
            gallery = gallery_from_post(request)
            fields = _product_fields_from_post(request)
            if master is not None:
                fields["offer_of"] = master
            check_digital(fields)
            if not fields["image_url"]:
                raise ValidationError("Please add a product image (upload a file or paste an https:// link).")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return _seller_form(request, seller, role, {
                "categories": category_choices(), "p": request.POST, "variant_rows": variant_rows_for_form(request),
                "offer_master": master,
            })
        with transaction.atomic():
            product = Product.objects.create(
                **fields,
                seller_account=seller,
                seller_name=seller.display_name,
                approval_status="pending",
            )
            if variant_rows:
                apply_variant_rows(product, variant_rows)
            save_gallery(product, *gallery)
        messages.success(request, "Product submitted for review. It will go live once approved by an admin.")
        return redirect("seller_products")
    start = {"stock": 10, "discount_percent": 0}
    if master is not None:
        # Selling the same product as another seller: start from its listing.
        start.update({f: getattr(master, f) for f in ("name", "category", "image_url", "description", "brand", "specs",
                                                      "weight_grams", "manufacturer", "eu_responsible_person", "safety_info")})
        start["price"] = master.price
    return _seller_form(request, seller, role, {
        "categories": category_choices(), "p": start, "variant_rows": [], "offer_master": master,
    })


# Changing any of these sends the product back for moderation; price and
# stock updates go live immediately.
REVIEWED_FIELDS = ("name", "category", "image_url", "description", "guide_uses", "guide_pros", "guide_cons",
                   "brand", "specs", "video_url", "is_digital")


@login_required
def seller_edit_product(request, pk):
    seller, role = _approved_seller_or_404(request.user)
    product = get_object_or_404(Product, pk=pk, seller_account=seller)
    if request.method == "POST":
        try:
            variant_rows = parse_variant_rows(request)
            gallery = gallery_from_post(request, product)
            fields = _product_fields_from_post(request)
            check_digital(fields, product)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return _seller_form(request, seller, role, {
                "categories": category_choices(), "product": product, "p": request.POST,
                "variant_rows": variant_rows_for_form(request, product),
            })
        if not fields["image_url"]:
            fields["image_url"] = product.image_url
        old_gallery = list(product.extra_images.values_list("image_url", flat=True))
        needs_review = any(getattr(product, f) != fields[f] for f in REVIEWED_FIELDS) or bool(gallery[1]) or bool(set(gallery[0]) - set(old_gallery))
        for key, value in fields.items():
            setattr(product, key, value)
        if needs_review and product.approval_status != "pending":
            product.approval_status = "pending"
            messages.success(request, "Product updated. Because the listing details changed, it will go live again after a quick review.")
        else:
            messages.success(request, "Product updated.")
        with transaction.atomic():
            product.save()
            apply_variant_rows(product, variant_rows)
            save_gallery(product, *gallery)
        return redirect("seller_products")
    return _seller_form(request, seller, role, {
        "categories": category_choices(),
        "product": product, "p": product, "variant_rows": variant_rows_for_form(request, product),
    })


@login_required
@require_POST
def seller_delete_product(request, pk):
    seller, role = _approved_seller_or_404(request.user)
    product = get_object_or_404(Product, pk=pk, seller_account=seller)
    if OrderItem.objects.filter(product=product).exists():
        # Sold before: keep it for order history, reviews and returns, but
        # take it off the shop for good.
        Product.objects.filter(pk=product.pk).update(approval_status="archived", stock=0, is_flash_sale=False)
    else:
        product.delete()
    AuditLog.objects.create(user=request.user, action=f"Seller {seller.display_name} removed product #{pk} ({product.name})"[:255])
    messages.success(request, "Product removed from your store.")
    return redirect_back(request, "seller_products")


@login_required
@require_POST
def add_team_member(request):
    seller, role = get_seller_account_for_user(request.user)
    if not seller or role not in ("owner", "admin"):
        messages.error(request, "Only the account owner or a team admin can manage team members.")
        return redirect("seller_team")
    if seller.account_type != "organization":
        messages.error(request, "Team members are only available for organization accounts.")
        return redirect("seller_team")
    identifier = request.POST.get("username_or_email", "").strip()
    member_role = request.POST.get("role", "staff")
    if member_role not in dict(OrganizationMember.ROLE_CHOICES):
        member_role = "staff"
    user = User.objects.filter(Q(username__iexact=identifier) | Q(email__iexact=identifier)).first() if identifier else None
    if not user:
        messages.error(request, f"No user found with username/email '{identifier}'. They need to create an account first.")
    elif user == seller.user:
        messages.error(request, "That's already the account owner.")
    elif OrganizationMember.objects.filter(organization=seller, user=user).exists():
        messages.error(request, f"{user.username} is already on the team.")
    elif SellerAccount.objects.filter(user=user).exists() or OrganizationMember.objects.filter(user=user).exists():
        messages.error(request, f"{user.username} already sells or works with another store. One account can only belong to one store.")
    else:
        OrganizationMember.objects.create(organization=seller, user=user, role=member_role)
        Notification.objects.create(
            user=user,
            message=f"You've been added to {seller.display_name}'s team. You can now help manage their products and orders.",
            link="/seller/dashboard/",
        )
        messages.success(request, f"{user.username} added to the team.")
    return redirect("seller_team")


def notify_question_answered(question):
    if question.user_id and question.answer:
        Notification.objects.create(
            user_id=question.user_id, link=f"/product/{question.product_id}/#questions",
            message=f"Your question about '{question.product.name[:120]}' has been answered.",
        )


@login_required
@require_POST
def seller_answer_question(request, pk):
    seller, role = _approved_seller_or_404(request.user)
    question = get_object_or_404(Question.objects.select_related("product"), pk=pk, product__seller_account=seller)
    answer = request.POST.get("answer", "").strip()[:2000]
    if not answer:
        messages.error(request, "Please write an answer.")
    else:
        had_answer = bool(question.answer)
        question.answer = answer
        question.save(update_fields=["answer"])
        if not had_answer:
            notify_question_answered(question)
        messages.success(request, "Answer published on the product page.")
    return redirect_back(request, "seller_reviews")


@login_required
@require_POST
def remove_team_member(request, member_id):
    seller, role = get_seller_account_for_user(request.user)
    if not seller or role not in ("owner", "admin"):
        messages.error(request, "Only the account owner or a team admin can manage team members.")
        return redirect("seller_team")
    member = get_object_or_404(OrganizationMember, pk=member_id, organization=seller)
    member.delete()
    messages.success(request, "Team member removed.")
    return redirect("seller_team")


# ---------------------------------------------------------------------------
# Staff
# ---------------------------------------------------------------------------

def _is_owner(user):
    return user.is_authenticated and user.is_staff


@login_required
def seller_document(request, seller_id, field):
    """Staff-only access to private seller verification documents."""
    from .staff_roles import can
    if not request.user.is_staff or not can(request.user, "sellers"):
        return HttpResponseForbidden("Staff access only.")
    if field not in ("business_certificate", "id_document"):
        raise Http404
    seller = get_object_or_404(SellerAccount, pk=seller_id)
    file = getattr(seller, field)
    if not file:
        raise Http404
    if getattr(settings, "USE_SUPABASE_STORAGE", False):
        return redirect(file.url)  # short-lived signed URL
    from django.http import FileResponse
    return FileResponse(file.open("rb"), as_attachment=False, filename=file.name.rsplit("/", 1)[-1])


def paypal_return(request, order_id):
    """The shopper approved the payment on PayPal: capture it."""
    from . import paypal
    order = _order_access(request, order_id)
    if request.GET.get("token") and order.paypal_order_id and request.GET["token"] != order.paypal_order_id:
        raise Http404
    try:
        order, newly_paid = paypal.capture(order.id)
    except paypal.PayPalError as exc:
        messages.error(request, f"We couldn't confirm the PayPal payment yet ({exc}). If money was taken, the order updates automatically within minutes.")
        return _orders_page(request, order)
    if newly_paid:
        _send_order_confirmation(request, order)
        if order.user_id:
            Notification.objects.create(user_id=order.user_id, message=f"Payment received for order #{order.id}.", link="/my-orders/")
    if order.payment_status != "paid":
        messages.error(request, "The PayPal payment wasn't completed. You can try again from My orders.")
        return _orders_page(request, order)
    return redirect("order_success", order_id=order.id)


def paypal_cancel(request, order_id):
    """The shopper went back from PayPal without paying."""
    order = _order_access(request, order_id)
    if order.payment_status == "pending" and order.status != "cancelled":
        payments.release_unpaid_order(order.id)
        _restore_cart_from_order(request, order)
        messages.info(request, "PayPal payment cancelled - your cart is back. Choose how you'd like to pay.")
        return redirect("cart")
    return _orders_page(request, order)


@csrf_exempt
@require_POST
def paypal_webhook(request):
    """PayPal tells us about approved / completed / refunded payments. The
    call is verified with PayPal, and the order is then re-checked with the
    API before anything changes."""
    import json as _json
    from . import paypal
    try:
        event = _json.loads(request.body or b"{}")
    except ValueError:
        return HttpResponse(status=400)
    if not paypal.verify_webhook(request, event):
        return HttpResponse(status=400)
    resource = event.get("resource") or {}
    kind = event.get("event_type", "")
    order = None
    if kind.startswith("PAYMENT.PAYOUTS"):
        from . import payouts as auto
        from .models import Payout
        batch_id = resource.get("payout_batch_id") or (resource.get("batch_header") or {}).get("payout_batch_id", "")
        payout = Payout.objects.filter(status="processing", reference=f"PayPal {batch_id}").first() if batch_id else None
        if payout:
            auto.refresh_paypal(payout)
        return HttpResponse(status=200)
    if kind in ("CHECKOUT.ORDER.APPROVED", "PAYMENT.CAPTURE.COMPLETED"):
        from .models import GiftCard
        pp_id = resource.get("id", "") if kind.startswith("CHECKOUT") else \
            ((resource.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id", "")
        card = GiftCard.objects.filter(paypal_order_id=pp_id, status="pending").first() if pp_id else None
        if card:
            from . import giftcards
            try:
                with transaction.atomic():
                    giftcards.capture_paypal(card)
            except paypal.PayPalError:
                return HttpResponse(status=503)
            return HttpResponse(status=200)
    if kind.startswith("CHECKOUT.ORDER"):
        order = Order.objects.filter(paypal_order_id=resource.get("id", "")).first()
    elif kind.startswith("PAYMENT.CAPTURE"):
        related = ((resource.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id", "")
        order = (Order.objects.filter(paypal_order_id=related).first() if related else None) \
            or Order.objects.filter(paypal_capture_id=resource.get("id", "")).first()
    if order and kind in ("CHECKOUT.ORDER.APPROVED", "PAYMENT.CAPTURE.COMPLETED"):
        try:
            order, newly_paid = paypal.capture(order.id)
        except paypal.PayPalError:
            return HttpResponse(status=503)  # PayPal retries
        if newly_paid:
            _send_order_confirmation(request, order)
    elif order and kind == "PAYMENT.CAPTURE.REFUNDED" and order.payment_status == "paid" and order.status not in ("cancelled", "delivered"):
        from .alerts import notify_staff
        notify_staff(f"Order #{order.id} was refunded in PayPal. Cancel it here if it shouldn't ship.", link=f"/manage/orders/{order.id}/")
    elif order and kind in ("PAYMENT.CAPTURE.DENIED", "PAYMENT.CAPTURE.REVERSED"):
        from .alerts import notify_staff
        notify_staff(f"PayPal reports {kind.split('.')[-1].lower()} payment for order #{order.id}. Don't ship until it's sorted.", link=f"/manage/orders/{order.id}/")
    return HttpResponse(status=200)


@login_required
def owner_dashboard(request):
    """Old URL - the store dashboard now lives in the store admin."""
    return redirect("manage_dashboard")


# ---------------------------------------------------------------------------
# Support chat
# ---------------------------------------------------------------------------

def _get_or_create_chat_thread(request):
    if request.user.is_authenticated:
        guest_id = request.session.pop("guest_chat_thread", None)
        guest = ChatThread.objects.filter(pk=guest_id, user__isnull=True).first() if guest_id else None
        thread = ChatThread.objects.filter(user=request.user).first()
        if guest and not thread:
            # Keep the conversation the visitor started before signing in.
            guest.user = request.user
            guest.save(update_fields=["user"])
            return guest
        if not thread:
            thread = ChatThread.objects.create(user=request.user)
        if guest:
            guest.messages.update(thread=thread)
            guest.delete()
        return thread
    if not request.session.session_key:
        request.session.create()
    thread, _ = ChatThread.objects.get_or_create(
        user=None, session_key=request.session.session_key,
    )
    request.session["guest_chat_thread"] = thread.id
    return thread


def _chat_json(m):
    return {
        "sender": m.sender, "message": m.message, "created_at": m.created_at.strftime("%H:%M"),
        "auto": m.is_auto, "links": (m.extra or {}).get("links", []), "options": (m.extra or {}).get("options", []),
    }


def chat_messages(request):
    """This visitor's chat history as JSON, polled by the widget."""
    thread = _get_or_create_chat_thread(request)
    data = [_chat_json(m) for m in thread.messages.order_by("created_at")]
    if not is_speculative(request):
        thread.messages.filter(sender="support", is_read=False).update(is_read=True)
    return JsonResponse({"messages": data})


def chat_topic(request):
    """Instant answer for a help topic button (not stored - it's a FAQ)."""
    from . import assistant
    return JsonResponse(assistant.answer(request.GET.get("key", "menu"), request))


def chat_unread(request):
    """Unread replies from the team, for the badge on the chat button."""
    if not request.session.session_key and not request.user.is_authenticated:
        return JsonResponse({"unread": 0})
    thread = (ChatThread.objects.filter(user=request.user).first() if request.user.is_authenticated
              else ChatThread.objects.filter(user=None, session_key=request.session.session_key).first())
    n = thread.messages.filter(sender="support", is_auto=False, is_read=False).count() if thread else 0
    return JsonResponse({"unread": n})


@ratelimit("chat_send", rate_limit=20, window_seconds=300)
def chat_send(request):
    """Saves the visitor's message. If the help assistant recognises the
    question it answers straight away; the conversation also appears in
    Admin > Messages so the team can follow up."""
    from . import assistant
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    text = request.POST.get("message", "").strip()[:2000]
    if not text:
        return JsonResponse({"error": "Empty message"}, status=400)

    thread = _get_or_create_chat_thread(request)
    ChatMessage.objects.create(thread=thread, sender="user", message=text)
    if thread.is_resolved:
        thread.is_resolved = False
        thread.save(update_fields=["is_resolved"])

    key = assistant.match(text)
    if key and key not in ("human",):
        reply = assistant.answer(key, request)
        reply["message"] += "\n\nStill need help? Just reply here and someone from our team will get back to you."
    else:
        signed_in = request.user.is_authenticated
        reply = {
            "message": ("Thanks, we've got your message and a member of our team will reply right here, usually within a few hours"
                        + (". You'll also get a notification." if signed_in else ". Sign in so you don't miss the reply, or keep this page open.")),
            "links": [] if signed_in else [{"label": "Sign in", "url": reverse("login")}],
            "options": [{"key": "menu", "label": "Main menu"}],
        }
    saved = ChatMessage.objects.create(
        thread=thread, sender="support", message=reply["message"], is_auto=True, is_read=True,
        extra={"links": reply.get("links", []), "options": reply.get("options", [])},
    )
    return JsonResponse({"reply": _chat_json(saved)})


@login_required
@require_POST
def ai_write_listing(request):
    """Writes a product description for the add/edit product forms."""
    from django.core.cache import cache
    from . import ai
    seller, _role = get_seller_account_for_user(request.user)
    if not (request.user.is_staff or (seller and seller.status == "approved")):
        return JsonResponse({"error": "Only approved sellers and store staff can use the writer."}, status=403)
    key = f"ai-writer:{request.user.pk}"
    used = cache.get(key, 0)
    if used >= 40:
        return JsonResponse({"error": "You've used the writer a lot in the last hour. Please try again a bit later."}, status=429)
    cache.set(key, used + 1, 3600)
    name = request.POST.get("name", "").strip()
    if len(name) < 3:
        return JsonResponse({"error": "Type the product name first."}, status=400)
    category = request.POST.get("category", "")
    label = dict(category_choices()).get(category, "")
    label = label.split(" › ")[-1]
    text, source = ai.write_listing(name, category, request.POST.get("notes", ""), label)
    return JsonResponse({"description": text, "source": source})


@require_POST
def demo_login(request, role):
    """One-click sign-in to the demo accounts (DEMO_MODE only)."""
    from django.conf import settings as dj
    from .management.commands.demo_setup import DEMO_USERS
    if not getattr(dj, "DEMO_MODE", False) or role not in DEMO_USERS:
        raise Http404
    user = User.objects.filter(username=DEMO_USERS[role]["username"], is_active=True).first()
    if not user:
        messages.error(request, "The demo isn't ready yet. Please try again in a minute.")
        return redirect("login")
    auth_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    request.session.set_expiry(60 * 60 * 4)
    target = {"shopper": "home", "seller": "seller_dashboard", "admin": "manage_dashboard"}[role]
    messages.success(request, {
        "shopper": "You're browsing as a demo shopper. Add something to the cart and try checkout (use Cash on delivery).",
        "seller": "Welcome to the Seller Center demo. Try adding a product with the AI writer, or ship an order.",
        "admin": "You're in the store admin demo. Store settings are read-only here; everything else works.",
    }[role])
    return redirect(target)


@require_POST
def remember_ship_country(request):
    """Remembers the country picked in the product page delivery estimate."""
    code = request.POST.get("country", "").upper()[:2]
    from .countries import COUNTRIES
    if code in dict(COUNTRIES) or code == "":
        request.session["ship_country"] = code
    return JsonResponse({"ok": True})


# ---------------------------------------------------------------------------
# Cash on delivery confirmation and public shipment tracking
# ---------------------------------------------------------------------------

COD_CONFIRM_HOURS = 48


def _send_cod_confirmation(request, order):
    url = request.build_absolute_uri(reverse("confirm_cod", args=[order.id, order.access_token]))
    _send_html_email(
        f"Please confirm your {_store_name()} order #{order.id}",
        "bees/emails/cod_confirm.html",
        {"order": order, "url": url, "hours": COD_CONFIRM_HOURS},
        order.contact_email,
        lang=_order_lang(order),
    )


def confirm_cod(request, order_id, token):
    """One click from the email confirms a cash-on-delivery order; sellers
    are told to ship it straight away."""
    order = get_object_or_404(Order, pk=order_id)
    if not secrets.compare_digest(token, order.access_token):
        raise Http404
    if order.status == "cancelled":
        messages.error(request, f"Order #{order.id} was cancelled because it wasn't confirmed in time. Please order again.")
        return redirect("home")
    if not order.cod_confirmed and order.cod_otp_hash:
        messages.info(request, f"Please confirm order #{order.id} with the code we texted to your phone.")
        return redirect("order_success", order_id=order.id) if _can_see_order(request, order) else redirect("home")
    if not order.cod_confirmed and request.method != "POST":
        # Email scanners open links automatically, so the link only shows a
        # button; the customer's own click (POST) confirms the order.
        return render(request, "bees/confirm_cod.html", {"order": order, "token": token})
    if not order.cod_confirmed and Order.objects.filter(pk=order.pk, cod_confirmed=False).update(cod_confirmed=True):
        order.cod_confirmed = True
        OrderEvent.objects.create(order=order, kind="note", text="Cash on delivery order confirmed by the customer")
        if order.user_id:
            Profile.objects.filter(user_id=order.user_id).update(email_verified=True)
        _send_order_confirmation(request, order)
    messages.success(request, f"Thank you! Order #{order.id} is confirmed. Please keep {money(order.total)} ready in cash for the courier.")
    return redirect("my_orders" if request.user.is_authenticated else "home")


def _can_see_order(request, order):
    try:
        _order_access(request, order.id)
        return True
    except Http404:
        return False


@require_POST
@ratelimit("cod_code", rate_limit=12, window_seconds=600, redirect_to="home",
           message="Too many attempts. Please wait a few minutes and try again.")
def cod_verify(request, order_id):
    """The customer types the 6-digit code we texted (cash orders)."""
    from . import cod
    order = _order_access(request, order_id, owner_only=True)
    back = request.POST.get("next") or ""
    if order.payment_method != "cod" or order.status == "cancelled":
        return redirect("order_success", order_id=order.id)
    if request.POST.get("resend"):
        if order.cod_confirmed:
            pass
        elif not cod.can_resend(order):
            messages.error(request, "Please wait a minute before asking for another code.")
        elif cod.send_code(order):
            messages.success(request, "We've sent a new code to your phone.")
        else:
            messages.error(request, "We couldn't text that number. Please contact support.")
    else:
        ok, error = cod.check_code(order, request.POST.get("code", ""))
        if ok:
            _send_order_confirmation(request, order)
            messages.success(request, f"Thank you! Order #{order.id} is confirmed. Please keep {money(order.total)} ready in cash for the courier.")
        else:
            messages.error(request, error)
    if back == "orders":
        return _orders_page(request, order)
    return redirect("order_success", order_id=order.id)


@require_POST
def resend_cod_confirmation(request, order_id):
    order = _order_access(request, order_id, owner_only=True)
    if order.payment_method == "cod" and not order.cod_confirmed and order.status != "cancelled":
        _send_cod_confirmation(request, order)
        messages.success(request, f"We've sent the confirmation link again to {order.contact_email}.")
    return _orders_page(request, order)


@ratelimit("track", rate_limit=30, window_seconds=300, redirect_to="track_lookup",
           message="Too many tracking lookups. Please wait a few minutes.")
def track_lookup(request):
    number = request.GET.get("n", "").strip().upper()
    if number:
        if Order.objects.filter(shipment_number=number).exists() or Order.objects.filter(tracking_number__iexact=number).exists():
            order = Order.objects.filter(Q(shipment_number=number) | Q(tracking_number__iexact=number)).first()
            return redirect("track_shipment", order.shipment_number)
        messages.error(request, "We couldn't find a shipment with that number. Check it and try again.")
    return render(request, "bees/track.html", {"order": None, "number": number})


def track_shipment(request, number):
    """Public tracking page: status, timeline and courier - no personal
    details beyond the destination city."""
    order = get_object_or_404(Order.objects.prefetch_related("events"), shipment_number=number.upper())
    steps = []
    order_steps = [("placed", "Placed"), ("confirmed", "Confirmed"), ("shipped", "With courier"), ("delivered", "Delivered")]
    reached = {"pending": 0, "confirmed": 1, "shipped": 2, "delivered": 3}.get(order.status, 0)
    for i, (key, label) in enumerate(order_steps):
        steps.append({"key": key, "label": label, "done": order.status != "cancelled" and i <= reached})
    courier_link = "" if order.uses_store_tracking else tracking.tracking_link(order.courier_name, order.tracking_number, order.tracking_url)
    return render(request, "bees/track.html", {
        "order": order, "steps": steps, "events": [e for e in order.events.all() if e.public],
        "courier_link": courier_link, "number": order.shipment_number,
    })


def _seller_reply(product):
    if not product.seller_account_id:
        return ""
    from .messaging import reply_time
    try:
        return reply_time(product.seller_account)
    except Exception:
        return ""


def _ai_rate_ok(request, name, limit, window):
    key = f"ai-{name}:{request.user.pk if request.user.is_authenticated else request.META.get('REMOTE_ADDR', '')}"
    used = cache.get(key, 0)
    if used >= limit:
        return False
    cache.set(key, used + 1, window)
    return True


@login_required
@require_POST
def ai_buying_guide_draft(request):
    """Drafts a buying guide for the add/edit product forms."""
    from . import ai
    seller, _role = get_seller_account_for_user(request.user)
    if not (request.user.is_staff or (seller and seller.status == "approved")):
        return JsonResponse({"error": "Only approved sellers and store staff can use this."}, status=403)
    if not _ai_rate_ok(request, "guide-draft", 40, 3600):
        return JsonResponse({"error": "Please wait a little before trying again."}, status=429)
    name = request.POST.get("name", "").strip()[:255]
    if len(name) < 3:
        return JsonResponse({"error": "Type the product name first."}, status=400)
    category = request.POST.get("category", "")
    product = Product(name=name, category=category if category in dict(category_choices()) else "grocery",
                      price=_safe_decimal(request.POST.get("price")), description=request.POST.get("description", "")[:4000])
    guide, source = ai.buying_guide(product)
    return JsonResponse({"guide": guide, "source": source})


def _safe_decimal(value):
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


@require_POST
def ai_product_guide(request, pk):
    """Shopper asks for an AI buying guide on a product that has none yet.
    Saved on the product, so it's created once and then shown to everyone."""
    from . import ai
    product = get_object_or_404(Product.objects.live(), pk=pk)
    if product.guide:
        return JsonResponse({"guide": product.guide, "source": product.guide_source})
    if not settings.AI_API_KEY:
        return JsonResponse({"error": "Not available."}, status=404)
    if not _ai_rate_ok(request, "guide", 20, 3600):
        return JsonResponse({"error": "Please try again in a little while."}, status=429)
    guide, source = ai.buying_guide(product)
    if source != "ai":
        return JsonResponse({"error": "The assistant is busy. Please try again shortly."}, status=503)
    Product.objects.filter(pk=pk, guide_pros="").update(
        guide_uses="\n".join(guide["uses"]), guide_pros="\n".join(guide["pros"]),
        guide_cons="\n".join(guide["cons"]), guide_source="ai")
    return JsonResponse({"guide": guide, "source": "ai"})


@require_POST
def ai_product_question(request, pk):
    """Instant AI answer to a shopper's question about a product."""
    from . import ai
    product = get_object_or_404(Product.objects.live(), pk=pk)
    question = request.POST.get("question", "").strip()
    if len(question) < 4:
        return JsonResponse({"error": "Type your question first."}, status=400)
    if not _ai_rate_ok(request, "ask", 15, 600):
        return JsonResponse({"error": "You've asked a lot of questions. Please wait a few minutes."}, status=429)
    answer = ai.answer_question(product, question)
    if not answer:
        return JsonResponse({"error": "The assistant can't answer right now. Ask the seller instead."}, status=503)
    return JsonResponse({"answer": answer})


def health(request):
    """For uptime monitors (UptimeRobot, Better Stack...): 200 when the site
    and its database answer, 503 otherwise. No private details."""
    from django.db import connection
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception:
        return JsonResponse({"ok": False, "database": "down"}, status=503)
    resp = JsonResponse({"ok": True})
    resp["Cache-Control"] = "no-store"
    return resp
