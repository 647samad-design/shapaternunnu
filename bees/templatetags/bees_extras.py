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
