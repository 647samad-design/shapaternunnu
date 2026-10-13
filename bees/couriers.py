"""Courier services: book a pickup, buy a label and track parcels.

Works with any courier service that has a provider class here. Today:

* EasyPost  - one account, 100+ carriers worldwide (DHL Express, FedEx,
              UPS, USPS, Aramex, Canada Post, Royal Mail, Australia Post...)
* Shippo    - DHL Express, UPS, FedEx, USPS and more

The owner picks the service in Store settings > Shipping labels and pastes
its API key in Store settings > Keys & passwords. Until then everything
works by hand (courier name + tracking number), exactly as before.

Each provider implements:  quote(shipment) -> rates,  buy(shipment, rate_id)
and refresh(shipment) -> (status, carrier_status). Status updates come
from the courier's webhook (which only triggers a re-check with our own API
key, so a forged call can't change anything) and from the daily job.
"""
import base64
import json
import logging
import os
import urllib.error
import urllib.request
from decimal import Decimal

logger = logging.getLogger(__name__)
TIMEOUT = 25


class CourierError(Exception):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def setting(name):
    """Keys live in .env (edited from Store settings > Keys & passwords)."""
    return os.environ.get(name, "").strip()


def _request(method, url, headers, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "LumenMarket/1.0", **headers})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        message = _error_text(payload) or f"HTTP {exc.code}"
        raise CourierError(message) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CourierError(f"Couldn't reach the courier service ({exc}). If you're on PythonAnywhere's free plan, "
                           "outside connections are blocked - a paid plan is needed.") from None
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        raise CourierError("The courier service sent an unreadable answer.") from None


def _error_text(payload):
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            return err.get("message") or err.get("code")
        if isinstance(err, str):
            return err
        if payload.get("detail"):
            return str(payload["detail"])
        msgs = payload.get("messages")
        if msgs:
            return "; ".join(str(m.get("text", m)) if isinstance(m, dict) else str(m) for m in msgs)[:400]
    return ""


def _country_code(value):
    from .countries import COUNTRIES
    value = (value or "").strip()
    if len(value) == 2:
        return value.upper()
    for code, name in COUNTRIES:
        if name.lower() == value.lower():
            return code
    return value[:2].upper()


def parcel_for(items):
    """Total weight (grams) and the largest box among the items (cm)."""
    from .models import SiteSettings
    default = SiteSettings.load().default_weight_grams or 500
    weight, dims = 0, [Decimal("0"), Decimal("0"), Decimal("0")]
    for item in items:
        product = item.product
        weight += (getattr(product, "weight_grams", None) or default) * item.quantity
        if product:
            for i, field in enumerate(("length_cm", "width_cm", "height_cm")):
                value = getattr(product, field, None) or 0
                dims[i] = max(dims[i], Decimal(str(value)))
    if not all(dims):
        dims = [Decimal("30"), Decimal("20"), Decimal("10")]
    return max(weight, 50), dims


def addresses(order, seller):
    """(from, to) as plain dicts."""
    from .models import SiteSettings
    site = SiteSettings.load()
    if seller:
        sender = {"name": seller.display_name, "street1": seller.business_address, "city": seller.city,
                  "state": seller.pickup_state, "zip": seller.pickup_postal_code,
                  "country": _country_code(seller.country), "phone": seller.phone, "email": seller.user.email}
    else:
        sender = {"name": site.ship_from_name or site.site_name, "street1": site.ship_from_street, "city": site.ship_from_city,
                  "state": site.ship_from_state, "zip": site.ship_from_postal_code, "country": _country_code(site.ship_from_country),
                  "phone": site.ship_from_phone, "email": site.support_email}
    receiver = {"name": order.full_name, "street1": order.address, "city": order.city, "state": order.state or "",
                "zip": order.postal_code, "country": _country_code(order.country), "phone": order.phone, "email": order.contact_email}
    return sender, receiver


def missing_sender_fields(sender):
    labels = {"street1": "street address", "city": "city", "country": "country", "phone": "phone"}
    return [label for key, label in labels.items() if not sender.get(key)]


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

class EasyPost:
    key_name = "EASYPOST_API_KEY"
    name = "EasyPost"
    base = "https://api.easypost.com/v2"
    STATUS = {"pre_transit": "booked", "in_transit": "in_transit", "out_for_delivery": "out_for_delivery",
              "available_for_pickup": "out_for_delivery", "delivered": "delivered", "return_to_sender": "returned",
              "failure": "failed", "error": "failed", "cancelled": "cancelled"}

    def __init__(self, key):
        self.key = key

    def _call(self, method, path, body=None):
        token = base64.b64encode(f"{self.key}:".encode()).decode()
        return _request(method, self.base + path, {"Authorization": f"Basic {token}"}, body)

    def quote(self, shipment, items, sender, receiver):
        grams, dims = parcel_for(items)
        body = {"shipment": {
            "to_address": receiver, "from_address": sender,
            "parcel": {"weight": round(grams / 28.3495, 1), "length": float(round(dims[0] / Decimal("2.54"), 1)),
                       "width": float(round(dims[1] / Decimal("2.54"), 1)), "height": float(round(dims[2] / Decimal("2.54"), 1))},
            "reference": f"order-{shipment.order_id}",
        }}
        if sender["country"] != receiver["country"]:
            body["shipment"]["customs_info"] = {
                "contents_type": "merchandise", "customs_certify": True, "customs_signer": sender["name"][:35],
                "eel_pfc": "NOEEI 30.37(a)", "non_delivery_option": "return",
                "customs_items": [{"description": i.product_name[:45], "quantity": i.quantity, "value": float(i.subtotal),
                                   "weight": round(((getattr(i.product, "weight_grams", None) or 500) * i.quantity) / 28.3495, 1),
                                   "origin_country": sender["country"]} for i in items],
            }
        data = self._call("POST", "/shipments", body)
        shipment.external_id = data.get("id", "")
        rates = [{"id": r["id"], "carrier": r.get("carrier", ""), "service": r.get("service", ""),
                  "amount": r.get("rate"), "currency": r.get("currency", ""), "days": r.get("delivery_days") or r.get("est_delivery_days")}
                 for r in data.get("rates") or []]
        if not rates:
            msgs = data.get("messages") or []
            raise CourierError("No courier offered a rate for this parcel." + (" " + "; ".join(m.get("message", "") for m in msgs if isinstance(m, dict))[:300] if msgs else ""))
        return rates

    def buy(self, shipment, rate_id):
        data = self._call("POST", f"/shipments/{shipment.external_id}/buy", {"rate": {"id": rate_id}})
        label = data.get("postage_label") or {}
        tracker = data.get("tracker") or {}
        rate = data.get("selected_rate") or {}
        return {"tracking_number": data.get("tracking_code", ""), "label_url": label.get("label_pdf_url") or label.get("label_url", ""),
                "tracking_url": tracker.get("public_url", ""), "tracker_id": tracker.get("id", ""),
                "carrier": rate.get("carrier", ""), "service": rate.get("service", ""), "cost": rate.get("rate"),
                "currency": rate.get("currency", ""), "transaction_id": data.get("id", "")}

    def refresh(self, shipment):
        if not shipment.tracker_id:
            return None, ""
        data = self._call("GET", f"/trackers/{shipment.tracker_id}")
        raw = data.get("status", "")
        return self.STATUS.get(raw), raw


class Shippo:
    key_name = "SHIPPO_API_TOKEN"
    name = "Shippo"
    base = "https://api.goshippo.com"
    STATUS = {"PRE_TRANSIT": "booked", "TRANSIT": "in_transit", "DELIVERED": "delivered", "RETURNED": "returned", "FAILURE": "failed"}

    def __init__(self, key):
        self.key = key

    def _call(self, method, path, body=None):
        return _request(method, self.base + path, {"Authorization": f"ShippoToken {self.key}"}, body)

    def quote(self, shipment, items, sender, receiver):
        grams, dims = parcel_for(items)
        body = {"address_from": sender, "address_to": receiver, "async": False,
                "parcels": [{"length": str(dims[0]), "width": str(dims[1]), "height": str(dims[2]), "distance_unit": "cm",
                             "weight": str(grams), "mass_unit": "g"}],
                "metadata": f"order-{shipment.order_id}"}
        if sender["country"] != receiver["country"]:
            customs = self._call("POST", "/customs/declarations/", {
                "contents_type": "MERCHANDISE", "non_delivery_option": "RETURN", "certify": True, "certify_signer": sender["name"][:35],
                "items": [{"description": i.product_name[:45], "quantity": i.quantity, "net_weight": str((getattr(i.product, "weight_grams", None) or 500) * i.quantity),
                           "mass_unit": "g", "value_amount": str(i.subtotal), "value_currency": shipment.order.currency.upper() if getattr(shipment.order, "currency", "") else "USD",
                           "origin_country": sender["country"]} for i in items],
            })
            body["customs_declaration"] = customs.get("object_id")
        data = self._call("POST", "/shipments/", body)
        shipment.external_id = data.get("object_id", "")
        rates = [{"id": r["object_id"], "carrier": r.get("provider", ""), "service": (r.get("servicelevel") or {}).get("name", ""),
                  "amount": r.get("amount"), "currency": r.get("currency", ""), "days": r.get("estimated_days")}
                 for r in data.get("rates") or []]
        if not rates:
            raise CourierError("No courier offered a rate for this parcel. " + _error_text(data))
        return rates

    def buy(self, shipment, rate_id):
        data = self._call("POST", "/transactions/", {"rate": rate_id, "label_file_type": "PDF", "async": False})
        if data.get("status") != "SUCCESS":
            raise CourierError(_error_text(data) or "The courier couldn't create the label.")
        chosen = next((r for r in shipment.rates if r.get("id") == rate_id), {})
        return {"tracking_number": data.get("tracking_number", ""), "label_url": data.get("label_url", ""),
                "tracking_url": data.get("tracking_url_provider", ""), "tracker_id": "", "transaction_id": data.get("object_id", ""),
                "carrier": chosen.get("carrier", ""), "service": chosen.get("service", ""), "cost": chosen.get("amount"),
                "currency": chosen.get("currency", "")}

    def refresh(self, shipment):
        if not shipment.transaction_id:
            return None, ""
        data = self._call("GET", f"/transactions/{shipment.transaction_id}")
        raw = data.get("tracking_status")
        if isinstance(raw, dict):
            raw = raw.get("status", "")
        raw = raw or ""
        return self.STATUS.get(raw.upper()), raw


PROVIDERS = {"easypost": EasyPost, "shippo": Shippo}


def active():
    """The configured provider, or None (manual tracking)."""
    from .models import SiteSettings
    key = SiteSettings.load().shipping_provider
    cls = PROVIDERS.get(key)
    if not cls:
        return None
    api_key = setting(cls.key_name)
    return cls(api_key) if api_key else None


def status_line():
    """For the admin: what's set up."""
    from .models import SiteSettings
    key = SiteSettings.load().shipping_provider
    cls = PROVIDERS.get(key)
    if not cls:
        return "off", "Courier service is off - sellers add tracking numbers by hand."
    if not setting(cls.key_name):
        return "missing", f"{cls.name} is chosen but {cls.key_name} isn't set. Add it under Store settings > Keys & passwords."
    return "ok", f"{cls.name} is connected."


# ---------------------------------------------------------------------------
# Booking flow
# ---------------------------------------------------------------------------

def get_rates(order, items, seller, user):
    from .models import Shipment
    provider = active()
    if provider is None:
        raise CourierError("No courier service is connected yet.")
    sender, receiver = addresses(order, seller)
    missing = missing_sender_fields(sender)
    if missing:
        where = "Seller Center > Store settings" if seller else "Store settings > Shipping labels"
        raise CourierError(f"Add your pickup {', '.join(missing)} in {where} first.")
    shipment = Shipment(order=order, seller=seller, item_ids=[i.pk for i in items], provider=type(provider).__name__.lower(), created_by=user)
    shipment.rates = provider.quote(shipment, items, sender, receiver)
    shipment.rates.sort(key=lambda r: float(r.get("amount") or 0))
    shipment.save()
    return shipment


def buy(shipment, rate_id, user=None):
    from django.utils import timezone
    from . import fulfilment
    from .models import Order, OrderEvent, OrderItem
    provider = active()
    if provider is None:
        raise CourierError("No courier service is connected yet.")
    if shipment.status != "rated":
        raise CourierError("This parcel is already booked.")
    if not any(r.get("id") == rate_id for r in shipment.rates):
        raise CourierError("Please choose one of the offered rates.")
    result = provider.buy(shipment, rate_id)
    shipment.tracking_number = result["tracking_number"][:80]
    shipment.tracking_url = (result["tracking_url"] or "")[:500]
    shipment.label_url = (result["label_url"] or "")[:500]
    shipment.tracker_id = result.get("tracker_id", "")[:100]
    shipment.transaction_id = result.get("transaction_id", "")[:100]
    shipment.carrier = (result.get("carrier") or "")[:60]
    shipment.service = (result.get("service") or "")[:80]
    if result.get("cost") not in (None, ""):
        shipment.cost = Decimal(str(result["cost"]))
    shipment.cost_currency = (result.get("currency") or "")[:3]
    shipment.status = "booked"
    shipment.checked_at = timezone.now()
    shipment.save()
    items = list(OrderItem.objects.filter(pk__in=shipment.item_ids))
    order = shipment.order
    if not order.items.exclude(pk__in=shipment.item_ids).exists():
        Order.objects.filter(pk=order.pk).update(courier_name=shipment.carrier[:60], tracking_number=shipment.tracking_number[:60],
                                                 tracking_url=shipment.tracking_url[:300] if shipment.tracking_url.startswith("https://") else "")
    OrderEvent.objects.create(order=order, kind="note", public=False, text=f"Label booked with {shipment.carrier} ({shipment.tracking_number})"[:200])
    try:
        fulfilment.set_items_status(items, "handed_to_courier", user=user, by_staff=user is not None and user.is_staff, forward_only=True)
    except fulfilment.FulfilmentError:
        pass
    return shipment


def refresh(shipment):
    """Asks the courier for the latest status and moves items along."""
    from django.utils import timezone
    from . import fulfilment
    from .alerts import notify_staff
    from .models import OrderItem
    provider = active()
    if provider is None or not shipment.active:
        return shipment.status
    status, raw = provider.refresh(shipment)
    shipment.checked_at = timezone.now()
    shipment.carrier_status = (raw or "")[:40]
    if status and status != shipment.status:
        shipment.status = status
        items = list(OrderItem.objects.filter(pk__in=shipment.item_ids))
        if status == "delivered":
            try:
                fulfilment.set_items_status(items, "delivered", by_staff=True, forward_only=True)
            except fulfilment.FulfilmentError:
                pass
        elif status in ("returned", "failed"):
            notify_staff(f"Courier reports a problem with order #{shipment.order_id} ({shipment.tracking_number}): {raw}.",
                         link=f"/manage/orders/{shipment.order_id}/")
            if shipment.seller_id:
                from .seller_center import _seller_users
                from .models import Notification
                Notification.objects.bulk_create([Notification(user=u, link=f"/seller/orders/{shipment.order_id}/",
                                                               message=f"Courier problem with order #{shipment.order_id}: {raw}.")
                                                  for u in _seller_users(shipment.seller)])
            order = shipment.order
            if status == "returned" and order.payment_method == "cod" and not order.cod_collected_at:
                from . import cod
                cod.mark_refused(order, reason=f"Courier returned the cash on delivery parcel ({shipment.tracking_number})")
    shipment.save()
    return shipment.status


def refresh_active(limit=200):
    from .models import Shipment
    done = 0
    for shipment in Shipment.objects.filter(status__in=["booked", "in_transit", "out_for_delivery"]).order_by("checked_at")[:limit]:
        try:
            refresh(shipment)
            done += 1
        except CourierError as exc:
            logger.warning("Tracking refresh failed for shipment %s: %s", shipment.pk, exc)
    return done
