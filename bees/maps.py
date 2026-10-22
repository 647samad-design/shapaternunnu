"""Delivery and pickup locations on a map.

Customers drop a pin on their door at checkout (or let the browser find
them); sellers set their pickup point when they sign up. Riders and
sellers get "Open in Google Maps" / directions links and a QR code on the
packing slip. The map is Google Maps when the store sets a browser key
(GOOGLE_MAPS_BROWSER_KEY, restricted to the store's website), otherwise
OpenStreetMap through Leaflet - free and with no key.
"""
import base64
import io
from decimal import Decimal, InvalidOperation


def _env(name):
    from .multistore import env
    return env(name)


def browser_key():
    return _env("GOOGLE_MAPS_BROWSER_KEY")


def parse(lat, lng):
    """(Decimal, Decimal) for a valid point, else (None, None)."""
    try:
        la = Decimal(str(lat).strip()).quantize(Decimal("0.000001"))
        ln = Decimal(str(lng).strip()).quantize(Decimal("0.000001"))
    except (InvalidOperation, ValueError, TypeError):
        return None, None
    if not (-90 <= la <= 90 and -180 <= ln <= 180) or (la == 0 and ln == 0):
        return None, None
    return la, ln


def from_post(request, prefix=""):
    return parse(request.POST.get(f"{prefix}latitude", ""), request.POST.get(f"{prefix}longitude", ""))


def pin_link(lat, lng):
    return f"https://www.google.com/maps/search/?api=1&query={lat},{lng}" if lat is not None and lng is not None else ""


def directions_link(lat, lng, origin=None):
    if lat is None or lng is None:
        return ""
    link = f"https://www.google.com/maps/dir/?api=1&destination={lat},{lng}&travelmode=driving"
    if origin and origin[0] is not None:
        link += f"&origin={origin[0]},{origin[1]}"
    return link


def qr_data_uri(text):
    """A small PNG QR code (for packing slips) as a data: URI."""
    import qrcode
    img = qrcode.make(text, box_size=4, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def allow_google(request):
    """Lets this page load Google Maps (Content-Security-Policy)."""
    if browser_key():
        request.csp_script = list(getattr(request, "csp_script", None) or []) + ["https://maps.googleapis.com", "https://maps.gstatic.com"]
        request.csp_connect = list(getattr(request, "csp_connect", None) or []) + ["https://maps.googleapis.com", "https://maps.gstatic.com"]


def context(request):
    allow_google(request)
    return {"map_key": browser_key()}
