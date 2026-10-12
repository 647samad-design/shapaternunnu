"""Tracking links for parcels.

Known couriers get a direct link to their tracking page; any other courier
falls back to 17TRACK, which looks a number up across 2,000+ carriers.
"""
from urllib.parse import quote

COURIERS = [
    ("DHL", "https://www.dhl.com/global-en/home/tracking/tracking-express.html?tracking-id={n}"),
    ("FedEx", "https://www.fedex.com/fedextrack/?trknbr={n}"),
    ("UPS", "https://www.ups.com/track?tracknum={n}"),
    ("USPS", "https://tools.usps.com/go/TrackConfirmAction?tLabels={n}"),
    ("Royal Mail", "https://www.royalmail.com/track-your-item#/tracking-results/{n}"),
    ("Canada Post", "https://www.canadapost-postescanada.ca/track-reperage/en#/search?searchFor={n}"),
    ("Australia Post", "https://auspost.com.au/mypost/track/details/{n}"),
    ("Aramex", "https://www.aramex.com/us/en/track/results?ShipmentNumber={n}"),
    ("DPD", "https://track.dpd.co.uk/search?reference={n}"),
    ("TNT", "https://www.tnt.com/express/en_gc/site/shipping-tools/tracking.html?searchType=con&cons={n}"),
    ("Emirates Post", "https://www.epg.gov.ae/_layouts/EPG/TrackAndTrace.aspx?ShipmentNumber={n}"),
    ("TCS", "https://www.tcsexpress.com/track/{n}"),
    ("Leopards", "https://www.leopardscourier.com/tracking?cn={n}"),
    ("Pakistan Post", "https://ep.gov.pk/track.asp?textfield={n}"),
    ("Blue Dart", "https://www.bluedart.com/tracking?trackFor=0&trackNo={n}"),
]
_BY_NAME = {name.lower(): url for name, url in COURIERS}
FALLBACK = "https://t.17track.net/en#nums={n}"


def courier_names():
    return [name for name, _ in COURIERS]


def tracking_link(courier, number, override=""):
    """URL where the customer can follow the parcel, or "" if there's no
    tracking number. ``override`` (a full https link) always wins."""
    if override and override.startswith("https://"):
        return override
    number = (number or "").strip()
    if not number:
        return ""
    template = _BY_NAME.get((courier or "").strip().lower(), FALLBACK)
    return template.format(n=quote(number, safe=""))


# Best-known courier for each destination, used when a shipping zone
# doesn't name one. The seller can always change it per order.
COUNTRY_COURIER = {
    "PK": "TCS", "IN": "Blue Dart", "US": "USPS", "CA": "Canada Post", "AU": "Australia Post", "GB": "Royal Mail",
    "AE": "Aramex", "SA": "Aramex", "QA": "Aramex", "KW": "Aramex", "BH": "Aramex", "OM": "Aramex", "JO": "Aramex",
    "EG": "Aramex", "LB": "Aramex",
}
EU = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU",
      "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"}
DEFAULT_COURIER = "DHL"


def suggest_courier(country):
    """Courier for a destination: the shipping zone's choice, else the
    best-known courier for that country, else DHL (ships everywhere)."""
    country = (country or "").upper()
    try:
        from .shipping import zone_for
        zone = zone_for(country)
        if zone and zone.courier:
            return zone.courier
    except Exception:
        pass
    if country in COUNTRY_COURIER:
        return COUNTRY_COURIER[country]
    if country in EU:
        return "DPD"
    return DEFAULT_COURIER


_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O/1/I - easy to read out on the phone


def shipment_prefix():
    from django.conf import settings
    prefix = getattr(settings, "SHIPMENT_PREFIX", "")
    if prefix:
        return prefix.upper()[:4]
    try:
        from .models import SiteSettings
        words = [w for w in SiteSettings.load().site_name.split() if w[:1].isalnum()]
        initials = "".join(w[0] for w in words)[:3].upper()
        return initials if initials.isalnum() and initials.isascii() else "SH"
    except Exception:
        return "SH"


def new_shipment_number():
    """Unique, easy-to-read number like LM-2610-7K3QX9 (prefix, year+month, random)."""
    import secrets
    from django.utils import timezone
    from .models import Order
    stamp = timezone.localdate().strftime("%y%m")
    for _ in range(10):
        code = "".join(secrets.choice(_ALPHABET) for _ in range(6))
        number = f"{shipment_prefix()}-{stamp}-{code}"
        if not Order.objects.filter(shipment_number=number).exists():
            return number
    return f"{shipment_prefix()}-{stamp}-{secrets.token_hex(5).upper()}"
