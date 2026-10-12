"""Tax by destination country (VAT / GST / sales tax).

* Each country can have its own rate (Store settings > Tax rates); others
  use the default rate from Store settings.
* Prices can be shown tax-inclusive (Europe, UK, Australia: tax is shown
  on the receipt but not added) or tax-exclusive (US: added at checkout).
* VAT usually applies to shipping too (per-rate switch).
"""
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")

# Standard rates as a starting point - always check them with your
# accountant; rates change.
PRESETS = [
    ("GB", "VAT", "20"), ("IE", "VAT", "23"), ("DE", "VAT", "19"), ("FR", "TVA", "20"), ("ES", "IVA", "21"),
    ("IT", "IVA", "22"), ("NL", "BTW", "21"), ("BE", "BTW", "21"), ("AT", "USt", "20"), ("PT", "IVA", "23"),
    ("SE", "Moms", "25"), ("DK", "Moms", "25"), ("FI", "ALV", "25.5"), ("PL", "VAT", "23"), ("NO", "MVA", "25"),
    ("CH", "MWST", "8.1"), ("AE", "VAT", "5"), ("SA", "VAT", "15"), ("BH", "VAT", "10"), ("OM", "VAT", "5"),
    ("AU", "GST", "10"), ("NZ", "GST", "15"), ("SG", "GST", "9"), ("IN", "GST", "18"), ("PK", "GST", "18"),
    ("CA", "GST", "5"), ("JP", "Consumption tax", "10"),
]


def rule_for(country):
    """(name, rate, includes_shipping) for a destination, or None."""
    from .models import SiteSettings, TaxRate
    code = (country or "").upper()
    if code:
        rate = TaxRate.objects.filter(country=code, active=True).first()
        if rate:
            return rate.name, rate.rate, rate.includes_shipping
    default = SiteSettings.load().tax_percent or Decimal("0")
    return ("Tax", Decimal(default), False) if default else None


def compute(goods, shipping, country):
    """Tax for an order. Returns dict(amount, name, rate, included)."""
    from .models import SiteSettings
    included = SiteSettings.load().prices_include_tax
    rule = rule_for(country)
    if not rule:
        return {"amount": Decimal("0"), "name": "", "rate": None, "included": included}
    name, rate, on_shipping = rule
    base = Decimal(goods) + (Decimal(shipping or 0) if on_shipping else 0)
    if included:
        amount = base - base / (1 + Decimal(rate) / 100)
    else:
        amount = base * Decimal(rate) / 100
    return {"amount": amount.quantize(CENT, rounding=ROUND_HALF_UP), "name": name, "rate": Decimal(rate), "included": included}


def table():
    """For the checkout page script."""
    from .models import SiteSettings, TaxRate
    site = SiteSettings.load()
    default = float(site.tax_percent or 0)
    return {
        "included": site.prices_include_tax,
        "default": {"name": "Tax", "rate": default, "ship": False} if default else None,
        "countries": {t.country: {"name": t.name, "rate": float(t.rate), "ship": t.includes_shipping}
                      for t in TaxRate.objects.filter(active=True)},
        "home": (site.ship_from_country or "").upper(),
        "duties": site.duties_policy,
    }
