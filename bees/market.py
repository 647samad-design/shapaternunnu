"""Marketplace features the big sites have: EU lowest-price rule, other
sellers of the same product, auctions, make-an-offer, daily check-in coins,
membership, Subscribe & Save and image search."""
import base64
import json
import logging
import urllib.request
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)
CENT = Decimal("0.01")


# ---------------------------------------------------------------------------
# EU Omnibus: lowest price in the 30 days before a discount
# ---------------------------------------------------------------------------

def lowest_price_30d(product):
    """Lowest price the product had in the 30 days before now (the reference
    shoppers in the EU must see next to a discount). None if unknown."""
    from .models import PriceHistory
    since = timezone.now() - timedelta(days=30)
    rows = list(PriceHistory.objects.filter(product=product).order_by("-recorded_at").values_list("price", "recorded_at")[:200])
    if not rows:
        return None
    prices = [price for price, at in rows if at >= since]
    # The price that was live when the window started counts too.
    before = next((price for price, at in rows if at < since), None)
    if before is not None:
        prices.append(before)
    return min(prices) if prices else None


# ---------------------------------------------------------------------------
# Other sellers of the same product (one product page, many offers)
# ---------------------------------------------------------------------------

def master_of(product):
    return product.offer_of if product.offer_of_id else product


def other_offers(product):
    """Live listings of the same product by other sellers, cheapest first."""
    from .models import Product
    master = master_of(product)
    qs = Product.objects.live().filter(stock__gt=0).select_related("seller_account").exclude(pk=product.pk)
    from django.db.models import Q
    qs = qs.filter(Q(offer_of=master) | Q(pk=master.pk))
    return list(qs.order_by("price")[:10])


# ---------------------------------------------------------------------------
# Auctions
# ---------------------------------------------------------------------------

SNIPE_WINDOW = timedelta(minutes=2)


def top_bid(product):
    return product.bids.order_by("-amount", "created_at").first()


def increment(amount):
    step = (Decimal(amount) * Decimal("0.05")).quantize(CENT, rounding=ROUND_HALF_UP)
    return max(Decimal("0.50"), step)


def next_min_bid(product, top=None):
    top = top if top is not None else top_bid(product)
    if top is None:
        return product.auction_start_price or Decimal("0.01")
    return (top.amount + increment(top.amount)).quantize(CENT)


def auction_open(product):
    return bool(product.is_auction and not product.auction_closed and product.auction_ends_at
                and product.auction_ends_at > timezone.now())


class BidError(Exception):
    pass


def place_bid(user, product_id, amount):
    from .models import Bid, Notification, Product
    with transaction.atomic():
        product = Product.objects.select_for_update().get(pk=product_id)
        if not auction_open(product):
            raise BidError("This auction has ended.")
        if product.seller_account_id and product.seller_account.user_id == user.id:
            raise BidError("You can't bid on your own item.")
        top = top_bid(product)
        minimum = next_min_bid(product, top)
        if amount is None or amount < minimum:
            raise BidError(f"Bid at least {minimum}.")
        if top and top.user_id == user.id:
            raise BidError("You're already the highest bidder.")
        bid = Bid.objects.create(product=product, user=user, amount=amount.quantize(CENT))
        now = timezone.now()
        if product.auction_ends_at - now < SNIPE_WINDOW:
            # Bids in the last two minutes add time, so nobody wins by sniping.
            product.auction_ends_at = now + SNIPE_WINDOW
            product.save(update_fields=["auction_ends_at"])
        if top and top.user_id != user.id:
            Notification.objects.create(user_id=top.user_id, link=f"/product/{product.id}/",
                                        message=f"You've been outbid on {product.name}. Current bid: {bid.amount}"[:255])
    return bid


def close_auctions():
    """Ends auctions whose time is up. The winner gets the item at the
    winning bid (it waits under My quotes, ready to pay for 3 days)."""
    from .models import Notification, Product, QuoteRequest
    closed = 0
    for product in Product.objects.filter(is_auction=True, auction_closed=False, auction_ends_at__lte=timezone.now()):
        with transaction.atomic():
            product = Product.objects.select_for_update().get(pk=product.pk)
            if product.auction_closed:
                continue
            top = top_bid(product)
            seller_user = product.seller_account.user_id if product.seller_account_id else None
            if top and (product.auction_reserve is None or top.amount >= product.auction_reserve):
                quote = QuoteRequest(product=product, buyer_id=top.user_id, seller_account=product.seller_account, kind="auction",
                                     quantity=1, status="accepted", offer_price=top.amount,
                                     offer_expires_at=timezone.now() + timedelta(days=3))
                quote.log("system", f"Won the auction with {top.amount}")
                quote.save()
                Notification.objects.create(user_id=top.user_id, link=f"/account/quotes/#q{quote.id}",
                                            message=f"You won {product.name} for {top.amount}! Pay within 3 days."[:255])
                if seller_user:
                    Notification.objects.create(user_id=seller_user, link="/seller/quotes/?tab=accepted",
                                                message=f"Auction ended: {product.name} sold for {top.amount}"[:255])
            elif seller_user:
                Notification.objects.create(user_id=seller_user, link=f"/seller/product/{product.id}/edit/",
                                            message=f"Auction ended without a sale: {product.name}" + (" (reserve not met)" if top else ""))
            product.auction_closed = True
            product.save(update_fields=["auction_closed"])
            closed += 1
    return closed


# ---------------------------------------------------------------------------
# Daily check-in coins (reward points)
# ---------------------------------------------------------------------------

def check_in(user):
    """Gives today's coins once a day. Returns (coins given, streak) or
    (0, streak) if already checked in today."""
    from .models import Profile, SiteSettings
    base = SiteSettings.load().checkin_points
    if not base:
        return 0, 0
    today = timezone.localdate()
    with transaction.atomic():
        profile, _ = Profile.objects.select_for_update().get_or_create(user=user)
        if profile.last_checkin == today:
            return 0, profile.checkin_streak
        streak = profile.checkin_streak + 1 if profile.last_checkin == today - timedelta(days=1) else 1
        coins = base * 2 if streak % 7 == 0 else base
        profile.last_checkin, profile.checkin_streak = today, streak
        profile.loyalty_points += coins
        profile.save(update_fields=["last_checkin", "checkin_streak", "loyalty_points"])
    return coins, streak


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------

def is_member(user):
    if not getattr(user, "is_authenticated", False):
        return False
    profile = getattr(user, "profile", None)
    until = getattr(profile, "member_until", None)
    return bool(until and until > timezone.now())


def extend_membership(user, days):
    from .models import Profile
    profile, _ = Profile.objects.get_or_create(user=user)
    start = profile.member_until if profile.member_until and profile.member_until > timezone.now() else timezone.now()
    profile.member_until = start + timedelta(days=days)
    profile.save(update_fields=["member_until"])
    return profile.member_until


# ---------------------------------------------------------------------------
# Subscribe & Save
# ---------------------------------------------------------------------------

def subscription_price(sub):
    base = sub.variant.unit_price if sub.variant_id and sub.variant else sub.product.price
    return (base * (100 - sub.product.subscribe_percent) / 100).quantize(CENT, rounding=ROUND_HALF_UP)


def remind_subscriptions():
    """Each subscription that is due: tell the shopper their reorder is
    ready (one click puts it in the cart at the subscription price)."""
    from .models import Notification, Subscription
    today = timezone.localdate()
    sent = 0
    for sub in Subscription.objects.filter(active=True, next_date__lte=today).select_related("product", "user"):
        Notification.objects.create(user_id=sub.user_id, link=f"/subscriptions/{sub.id}/reorder/",
                                    message=f"Time for your {sub.product.name} refill - {sub.product.subscribe_percent}% off, one click to order."[:255])
        from .views import _send_html_email
        from .order_emails import absolute
        _send_html_email(f"Your {sub.product.name} refill is ready", "bees/emails/subscription_due.html",
                         {"sub": sub, "price": subscription_price(sub), "url": absolute(f"/subscriptions/{sub.id}/reorder/")},
                         sub.user.email)
        sub.next_date = today + timedelta(weeks=sub.every_weeks)
        sub.save(update_fields=["next_date"])
        sent += 1
    return sent


# ---------------------------------------------------------------------------
# Search by photo
# ---------------------------------------------------------------------------

def image_search_enabled():
    return bool(getattr(settings, "AI_API_KEY", ""))


def describe_image(data, media_type):
    """Asks Claude for a short shopping search for the photo, e.g.
    'black leather ankle boots'. Returns '' on any problem."""
    if not image_search_enabled():
        return ""
    body = json.dumps({"model": settings.AI_MODEL, "max_tokens": 40, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode()}},
        {"type": "text", "text": "This photo was uploaded to an online shop's search. Reply with only a short search query "
                                 "(2 to 5 words, lowercase, no punctuation) for the main product in it, e.g. 'red running shoes'."},
    ]}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST", headers={
        "x-api-key": settings.AI_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            out = json.loads(resp.read().decode())
    except Exception:
        logger.exception("Image search failed")
        return ""
    text = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
    words = [w for w in "".join(ch if ch.isalnum() or ch == " " else " " for ch in text.lower()).split()][:6]
    return " ".join(words)


def listing_from_photo(data, media_type, categories):
    """Drafts a listing from a product photo: {"name", "category",
    "description", "brand", "specs"}. Empty dict on any problem."""
    if not image_search_enabled():
        return {}
    cats = ", ".join(f"{slug} ({name})" for slug, name in categories)[:3000]
    prompt = ("You help a seller list this product in an online shop. Look at the photo and reply with only JSON: "
              '{"name": "clear product title, max 80 chars", "category": "one slug from the list", '
              '"brand": "brand if visible, else empty", "description": "3-5 short sentences for shoppers, no claims you cannot see", '
              '"specs": ["Name: value", "..."] (2-5 facts you can see, like colour or material)}. Categories: ' + cats)
    body = json.dumps({"model": settings.AI_MODEL, "max_tokens": 500, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode()}},
        {"type": "text", "text": prompt}]}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST", headers={
        "x-api-key": settings.AI_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            out = json.loads(resp.read().decode())
        text = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
        draft = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except Exception:
        logger.exception("Listing from photo failed")
        return {}
    valid = {slug for slug, _n in categories}
    specs = draft.get("specs") or []
    return {
        "name": str(draft.get("name") or "")[:120], "brand": str(draft.get("brand") or "")[:80],
        "category": draft.get("category") if draft.get("category") in valid else "",
        "description": str(draft.get("description") or "")[:2000],
        "specs": "\n".join(str(s)[:120] for s in specs if ":" in str(s))[:1000] if isinstance(specs, list) else "",
    }


def price_tips(category, exclude_id=None):
    """Prices of live products in the same category: {"low", "typical",
    "high", "count"} or None if there are fewer than 3."""
    from .models import Product
    from .categories import with_children
    qs = Product.objects.listed().filter(category__in=with_children(category))
    if exclude_id:
        qs = qs.exclude(pk=exclude_id)
    prices = sorted(qs.values_list("price", flat=True)[:500])
    if len(prices) < 3:
        return None
    n = len(prices)
    return {"low": prices[n // 10], "typical": prices[n // 2], "high": prices[min(n - 1, n * 9 // 10)], "count": n}
