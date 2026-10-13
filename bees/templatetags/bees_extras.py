from decimal import Decimal, InvalidOperation

from django import template
from django.conf import settings

register = template.Library()

CURRENCY_SYMBOLS = {
    "usd": "$", "eur": "€", "gbp": "£", "cad": "CA$", "aud": "A$",
    "aed": "AED ", "sar": "SAR ", "inr": "₹", "pkr": "Rs ", "jpy": "¥",
}
# Currencies Stripe charges in whole units (no cents).
ZERO_DECIMAL = {"bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg", "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf"}


@register.filter
def get_item(dictionary, key):
    if not dictionary:
        return ""
    return dictionary.get(key, "")


@register.filter
def money(value):
    """Formats an amount in the store currency, e.g. 1234.5 -> $1,234.50."""
    currency = getattr(settings, "STORE_CURRENCY", "usd").lower()
    symbol = CURRENCY_SYMBOLS.get(currency, currency.upper() + " ")
    try:
        amount = Decimal(str(value if value not in (None, "") else 0))
    except (InvalidOperation, ValueError):
        return value
    if currency in ZERO_DECIMAL:
        return f"{symbol}{amount:,.0f}"
    return f"{symbol}{amount:,.2f}"


@register.simple_tag
def currency_code():
    return getattr(settings, "STORE_CURRENCY", "usd").upper()


@register.filter
def stars(value):
    """Rating (0-5) -> '★★★★☆' style string."""
    try:
        full = int(round(float(value or 0)))
    except (TypeError, ValueError):
        full = 0
    full = max(0, min(5, full))
    return "★" * full + "☆" * (5 - full)


@register.filter
def country_name(code):
    from ..countries import COUNTRY_NAMES
    return COUNTRY_NAMES.get((code or "").upper(), code)


@register.inclusion_tag("bees/partials/country_select.html")
def country_select(name="country", selected="", field_id="id_country", required=True):
    from ..countries import COUNTRIES
    return {"countries": COUNTRIES, "name": name, "selected": (selected or "").upper(), "field_id": field_id, "required": required}


@register.simple_tag
def site_brand():
    """SiteSettings for templates rendered without a request (emails)."""
    from ..models import SiteSettings
    try:
        return SiteSettings.load()
    except Exception:
        return SiteSettings()


@register.filter
def abs_url(value):
    """Full https:// address for links and pictures in emails (email apps
    can't open site-relative paths like /media/x.jpg)."""
    value = str(value or "")
    if value.startswith("/") and not value.startswith("//"):
        from ..order_emails import absolute
        return absolute(value)
    return value


@register.filter
def price(value):
    """Product price in the currency the shopper picked (store currency
    otherwise). Display only - checkout always charges the store currency."""
    from bees import currency
    cur = currency.current()
    if cur is None:
        return money(value)
    try:
        return currency.fmt(value, cur)
    except (InvalidOperation, ValueError, TypeError):
        return money(value)


@register.filter
def approx(value):
    """'≈ €12.30' in the shopper's currency, or '' when they use the store
    currency. For cart and checkout totals, which stay in store currency."""
    from bees import currency
    cur = currency.current()
    if cur is None:
        return ""
    return "≈ " + currency.fmt(value, cur)


@register.filter
def fill(text, product):
    """Fills {n} / {p} in a quantity-offer sentence from a product."""
    try:
        return str(text).replace("{n}", str(product.bulk_min_qty)).replace("{p}", str(product.bulk_percent))
    except AttributeError:
        return text


@register.simple_tag
def courier_names():
    from bees.tracking import courier_names as names
    return names()


@register.filter
def date_add_days(value, days):
    """{{ some_datetime|date_add_days:3 }}"""
    from datetime import timedelta
    try:
        return value + timedelta(days=int(days))
    except (TypeError, ValueError):
        return value


@register.simple_tag
def countries():
    from bees.countries import COUNTRIES
    return COUNTRIES


HREFLANG = {"en": "en", "ur": "ur", "roman": "ur-Latn", "ar": "ar", "es": "es", "fr": "fr"}
_INDEXABLE_PARAMS = {"page", "lang"}


@register.simple_tag(takes_context=True)
def seo_urls(context):
    """Canonical URL, hreflang alternates and whether this URL should be
    indexed (search results, filters and sorting are not)."""
    request = context.get("request")
    if request is None:
        return {"canonical": "", "alternates": [], "index": True}
    from urllib.parse import urlencode
    base = f"{request.scheme}://{request.get_host()}{request.path}"
    page = request.GET.get("page", "")
    lang = context.get("current_lang", "en")
    keep = {}
    if page.isdigit() and int(page) > 1:
        keep["page"] = page
    canonical_params = dict(keep)
    if request.GET.get("lang") and lang != "en":
        canonical_params["lang"] = lang
    canonical = base + ("?" + urlencode(canonical_params) if canonical_params else "")
    alternates = []
    brand = context.get("brand")
    if brand is not None and getattr(brand, "show_language_menu", False):
        for code, tag in HREFLANG.items():
            params = dict(keep)
            if code != "en":
                params["lang"] = code
            alternates.append((tag, base + ("?" + urlencode(params) if params else "")))
        alternates.append(("x-default", base + ("?" + urlencode(keep) if keep else "")))
    extra = set(request.GET.keys()) - _INDEXABLE_PARAMS
    return {"canonical": canonical, "alternates": alternates, "index": not extra}


@register.filter
def jsonld(value):
    """Safe JSON for <script type="application/ld+json">."""
    import json
    from django.utils.safestring import mark_safe
    text = json.dumps(value, ensure_ascii=False, default=str)
    return mark_safe(text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


@register.simple_tag(takes_context=True)
def site_jsonld(context):
    """Organization + WebSite (with site search) for every page."""
    request = context.get("request")
    brand = context.get("brand")
    if request is None or brand is None:
        return {}
    root = f"{request.scheme}://{request.get_host()}"
    org = {"@type": "Organization", "@id": root + "/#org", "name": brand.site_name, "url": root + "/"}
    logo = getattr(brand, "logo", "") or ""
    if logo:
        org["logo"] = logo if logo.startswith("http") else root + logo
    same = [u for u in (getattr(brand, f, "") for f in ("facebook_url", "instagram_url", "twitter_url", "youtube_url")) if u]
    if same:
        org["sameAs"] = same
    if getattr(brand, "support_email", "") or getattr(brand, "support_phone", ""):
        cp = {"@type": "ContactPoint", "contactType": "customer support"}
        if brand.support_email:
            cp["email"] = brand.support_email
        if getattr(brand, "support_phone", ""):
            cp["telephone"] = brand.support_phone
        org["contactPoint"] = cp
    site = {"@type": "WebSite", "@id": root + "/#site", "name": brand.site_name, "url": root + "/", "publisher": {"@id": root + "/#org"},
            "inLanguage": context.get("current_lang", "en"),
            "potentialAction": {"@type": "SearchAction", "target": {"@type": "EntryPoint", "urlTemplate": root + "/search/?q={search_term_string}"},
                                "query-input": "required name=search_term_string"}}
    return {"@context": "https://schema.org", "@graph": [org, site]}


@register.simple_tag(takes_context=True)
def listing_jsonld(context, products, name):
    request = context.get("request")
    if request is None:
        return {}
    from bees import seo
    return seo.item_list(request, list(products), str(name))


@register.simple_tag
def sitemap_languages():
    from bees.models import SiteSettings
    if not SiteSettings.load().show_language_menu:
        return []
    return list(HREFLANG.items())


@register.simple_tag
def social_providers():
    from bees import social
    return social.providers()
