from .translations import TRANSLATIONS, LANGUAGE_NAMES, RTL_LANGUAGES


def cart_count(request):
    """Counts only items the cart page will actually show (products that
    still exist and are live), so the badge and the cart agree."""
    cart = request.session.get("cart", {})
    if not cart:
        return {"cart_count": 0}
    from .cart import lines, totals
    return {"cart_count": totals(lines(cart))[1]}


def wishlist_ids(request):
    if request.user.is_authenticated:
        from .models import Wishlist
        return {"wishlist_ids": set(Wishlist.objects.filter(user=request.user).values_list("product_id", flat=True))}
    return {"wishlist_ids": set()}


def _site_settings(request):
    """Loads SiteSettings once per request (several processors need it)."""
    if not hasattr(request, "_site_settings_cache"):
        from .models import SiteSettings
        try:
            request._site_settings_cache = SiteSettings.load()
        except Exception:
            request._site_settings_cache = SiteSettings()
    return request._site_settings_cache


def site_language(request):
    brand_obj = _site_settings(request)
    lang = "en"
    if brand_obj.show_language_menu:
        # ?lang=xx links (used by search engines via hreflang) pick the language.
        url_lang = request.GET.get("lang", "")
        if url_lang in TRANSLATIONS:
            request.session["site_lang"] = url_lang
        lang = request.session.get("site_lang") or _browser_language(request)
    if lang not in TRANSLATIONS:
        lang = "en"
    request.shop_lang = lang  # used by i18n.PageTranslationMiddleware
    store = brand_obj.site_name
    strings = {key: value.replace("{store}", store) for key, value in TRANSLATIONS[lang].items()}
    if lang != "en":
        # Fall back to English for any key a translation is missing.
        strings = {**{k: v.replace("{store}", store) for k, v in TRANSLATIONS["en"].items()}, **strings}
    return {
        "t": strings,
        "current_lang": lang,
        "language_names": LANGUAGE_NAMES,
        "text_dir": "rtl" if lang in RTL_LANGUAGES else "ltr",
    }


def _browser_language(request):
    """First visit: use the browser's language if the store has it."""
    header = request.META.get("HTTP_ACCEPT_LANGUAGE", "")
    for part in header.split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in TRANSLATIONS and code != "roman":
            return code
        if code == "en":
            return "en"
    return "en"


def currencies(request):
    from . import currency
    try:
        active = currency.active_currencies()
    except Exception:
        active = []
    cur = currency.current()
    return {
        "currencies": active,
        "current_currency": cur.code if cur else currency.store_code(),
        "store_currency_code": currency.store_code(),
        "shopper_currency": cur,
    }


def trending_searches(request):
    """Kept for template compatibility; evaluated only if a template uses it."""
    from django.utils.functional import SimpleLazyObject
    from .models import SearchLog
    return {"trending_searches": SimpleLazyObject(lambda: list(SearchLog.objects.all()[:5]))}


def roles(request):
    """Cheap role flags for the header: is the user a seller, and (for
    staff) how many things in the store admin need attention."""
    user = request.user
    if not user.is_authenticated:
        return {"is_seller": False, "admin_attention": 0}
    from django.core.cache import cache
    from .models import SellerAccount, OrganizationMember
    is_seller = cache.get(f"is_seller:{user.pk}")
    if is_seller is None:
        is_seller = (
            SellerAccount.objects.filter(user=user).exists()
            or OrganizationMember.objects.filter(user=user).exists()
        )
        cache.set(f"is_seller:{user.pk}", is_seller, 300)
    attention = 0
    if user.is_staff:
        from .manage_views import attention_counts
        attention = attention_counts()["total"]
    from .messaging import seller_for, unread_for_buyer, unread_for_seller
    seller_unread = 0
    if is_seller:
        seller = seller_for(user)
        seller_unread = unread_for_seller(seller) if seller else 0
    buyer_unread = unread_for_buyer(user)
    return {"is_seller": is_seller, "admin_attention": attention, "buyer_unread_messages": buyer_unread,
            "seller_unread_messages": seller_unread, "header_messages": buyer_unread + seller_unread}


def unread_notifications(request):
    if request.user.is_authenticated:
        from .models import Notification
        return {"unread_notifications_count": Notification.objects.filter(user=request.user, is_read=False).count()}
    return {"unread_notifications_count": 0}


def push_key(request):
    """Public web-push key (only for signed-in people; empty = push is off)."""
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    from .webpush import public_key, is_configured
    return {"push_public_key": public_key() if is_configured() else ""}


def compare_count(request):
    return {"compare_count": len(request.session.get("compare", [])), "compare_ids": request.session.get("compare", [])}


def site_banner(request):
    settings_obj = _site_settings(request)
    if settings_obj.banner_active and settings_obj.banner_text:
        return {"site_banner": settings_obj}
    return {"site_banner": None}


def brand(request):
    """Exposes the white-label brand settings (store name, logo, colours,
    contact details) to every template as {{ brand.* }}."""
    from django.conf import settings as dj_settings
    from . import payments
    from .models import Product
    brand_obj = _site_settings(request)
    return {
        "brand": brand_obj,
        "stripe_enabled": payments.is_configured(),
        "store_currency": dj_settings.STORE_CURRENCY.upper(),
        "nav_categories": Product.CATEGORY_CHOICES,
        "demo_mode": getattr(dj_settings, "DEMO_MODE", False),
        "staging": getattr(dj_settings, "STAGING", False),
    }
