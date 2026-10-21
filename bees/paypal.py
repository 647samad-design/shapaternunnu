"""PayPal Checkout (Orders API v2): pay with a PayPal account or any card
through PayPal, in 200+ countries.

Switched on by adding PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET in
Store settings > Keys & passwords (PAYPAL_MODE=sandbox for testing,
live for real money). Optional PAYPAL_WEBHOOK_ID lets PayPal confirm
payments even if the shopper closes the tab before coming back.

Flow: create order (amount = what the shopper pays) -> shopper approves on
PayPal -> we capture on return (or from the webhook) -> order paid. Every
state change is confirmed with PayPal's API using our own credentials, and
the captured amount/currency must match the order exactly.
"""
import base64
import json
import logging
import os
import urllib.error
import urllib.request
from decimal import Decimal

from django.core.cache import cache
from django.db import transaction
from django.urls import reverse
from .multistore import store_settings  # per-store values in multi-store mode

logger = logging.getLogger(__name__)
TIMEOUT = 25
# Currencies PayPal can charge in.
SUPPORTED = {"AUD", "BRL", "CAD", "CNY", "CZK", "DKK", "EUR", "HKD", "HUF", "ILS", "JPY", "MYR", "MXN", "TWD",
             "NZD", "NOK", "PHP", "PLN", "GBP", "SGD", "SEK", "CHF", "THB", "USD"}


class PayPalError(Exception):
    pass


def _env(name):
    from .multistore import env as _store_env
    return _store_env(name)


def base_url():
    return "https://api-m.paypal.com" if _env("PAYPAL_MODE").lower() == "live" else "https://api-m.sandbox.paypal.com"


def is_configured():
    from django.conf import settings
    return bool(_env("PAYPAL_CLIENT_ID") and _env("PAYPAL_CLIENT_SECRET")) and store_settings.STORE_CURRENCY.upper() in SUPPORTED


def status_line():
    from django.conf import settings
    if not (_env("PAYPAL_CLIENT_ID") and _env("PAYPAL_CLIENT_SECRET")):
        return "off", "PayPal is off - add PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET under Keys & passwords to turn it on."
    if store_settings.STORE_CURRENCY.upper() not in SUPPORTED:
        return "missing", f"PayPal can't charge in {store_settings.STORE_CURRENCY.upper()}. Use a currency like USD, EUR or GBP for PayPal."
    mode = "live" if _env("PAYPAL_MODE").lower() == "live" else "sandbox (test)"
    return "ok", f"PayPal is on ({mode})."


def _http(method, path, body=None, headers=None, auth=None, form=None):
    data = None
    hdrs = {"Accept": "application/json", "User-Agent": "LumenMarket/1.0", **(headers or {})}
    if form is not None:
        data = form.encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    if auth:
        hdrs["Authorization"] = auth
    req = urllib.request.Request(base_url() + path, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        details = payload.get("details") or []
        issue = details[0].get("issue") if details and isinstance(details[0], dict) else ""
        raise PayPalError(issue or payload.get("message") or payload.get("error_description") or f"HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PayPalError(f"Couldn't reach PayPal ({exc}).") from None
    return json.loads(raw or b"{}")


def _token():
    key = f"paypal-token:{base_url()}:{_env('PAYPAL_CLIENT_ID')[-6:]}"
    token = cache.get(key)
    if token:
        return token
    basic = base64.b64encode(f"{_env('PAYPAL_CLIENT_ID')}:{_env('PAYPAL_CLIENT_SECRET')}".encode()).decode()
    data = _http("POST", "/v1/oauth2/token", form="grant_type=client_credentials", auth=f"Basic {basic}")
    token = data.get("access_token")
    if not token:
        raise PayPalError("PayPal didn't accept the client ID / secret.")
    cache.set(key, token, max(int(data.get("expires_in", 3000)) - 120, 60))
    return token


def api(method, path, body=None, request_id=None):
    headers = {"PayPal-Request-Id": request_id} if request_id else {}
    if method == "POST":
        headers["Prefer"] = "return=representation"
    return _http(method, path, body, headers=headers, auth=f"Bearer {_token()}")


def _amount(order):
    from .templatetags.bees_extras import ZERO_DECIMAL
    cents = order.pay_cents
    currency = order.pay_currency
    value = Decimal(cents) if currency in ZERO_DECIMAL else Decimal(cents) / 100
    return {"currency_code": currency.upper(), "value": f"{value:.0f}" if currency in ZERO_DECIMAL else f"{value:.2f}"}


def create_order(request, order):
    """Creates the PayPal order and returns the page to send the shopper to."""
    from .models import Order, SiteSettings
    site = SiteSettings.load()
    return_url = request.build_absolute_uri(reverse("paypal_return", args=[order.id]))
    cancel_url = request.build_absolute_uri(reverse("paypal_cancel", args=[order.id]))
    body = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": f"order-{order.id}", "custom_id": str(order.id), "invoice_id": f"{order.shipment_number or order.id}",
            "description": f"{site.site_name} order #{order.id}"[:127], "amount": _amount(order),
        }],
        "payment_source": {"paypal": {"experience_context": {
            "brand_name": site.site_name[:127], "user_action": "PAY_NOW", "shipping_preference": "NO_SHIPPING",
            "return_url": return_url, "cancel_url": cancel_url,
        }}},
    }
    data = api("POST", "/v2/checkout/orders", body, request_id=f"lumen-order-{order.id}-{order.pay_currency}-{order.pay_cents}")
    link = next((l["href"] for l in data.get("links", []) if l.get("rel") in ("payer-action", "approve")), "")
    if not data.get("id") or not link:
        raise PayPalError("PayPal didn't return a payment page.")
    Order.objects.filter(pk=order.pk).update(paypal_order_id=data["id"])
    order.paypal_order_id = data["id"]
    return link


def _capture_info(data):
    unit = (data.get("purchase_units") or [{}])[0]
    capture = ((unit.get("payments") or {}).get("captures") or [{}])[0]
    return unit, capture


def capture(order_id):
    """Captures (or re-checks) the PayPal payment for our order. Returns
    (order, newly_paid). Safe to call twice (return page + webhook)."""
    from .models import Order, OrderEvent
    order = Order.objects.filter(pk=order_id).first()
    if not order or not order.paypal_order_id:
        return order, False
    if order.payment_status == "paid":
        return order, False
    data = api("GET", f"/v2/checkout/orders/{order.paypal_order_id}")
    if data.get("status") == "APPROVED":
        data = api("POST", f"/v2/checkout/orders/{order.paypal_order_id}/capture", {}, request_id=f"lumen-capture-{order.id}")
    if data.get("status") != "COMPLETED":
        return order, False
    unit, cap = _capture_info(data)
    amount = cap.get("amount") or unit.get("amount") or {}
    expected = _amount(order)
    if cap.get("status") not in ("COMPLETED", "PENDING") or unit.get("custom_id", str(order.id)) != str(order.id) \
            or amount.get("currency_code") != expected["currency_code"] or Decimal(amount.get("value", "0")) != Decimal(expected["value"]):
        logger.error("PayPal capture mismatch for order %s: %s", order.id, amount)
        from .alerts import notify_staff
        notify_staff(f"PayPal payment for order #{order.id} didn't match the order total - check it in PayPal.", link=f"/manage/orders/{order.id}/")
        return order, False
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        if order.payment_status == "paid":
            return order, False
        order.paypal_capture_id = cap.get("id", "")[:64]
        order.payment_method = "paypal"
        order.cod_fallback = False
        if order.status == "cancelled":
            order.payment_status = "paid"
            order.save(update_fields=["paypal_capture_id", "payment_method", "cod_fallback", "payment_status"])
            refunded = refund(order)
            Order.objects.filter(pk=order.pk).update(payment_status="refunded" if refunded else "paid")
            return order, False
        order.payment_status = "paid"
        if order.status == "pending":
            order.status = "confirmed"
        if not order.estimated_delivery:
            from .order_emails import default_delivery_date
            order.estimated_delivery = default_delivery_date(order)
        order._skip_status_email = True
        order.save(update_fields=["paypal_capture_id", "payment_method", "cod_fallback", "payment_status", "status", "estimated_delivery"])
        order._skip_status_email = False
        OrderEvent.objects.create(order=order, kind="paid", text="Payment received with PayPal")
    return order, True


def refund(order, amount=None, key=None):
    """Full refund (amount=None) or part of it, back to the PayPal account /
    card used. Returns True on success."""
    from django.utils import timezone
    from .models import Order, OrderEvent
    if not order.paypal_capture_id:
        return False
    body = {}
    if amount is not None:
        body["amount"] = {"currency_code": order.pay_currency.upper(), "value": f"{order.to_charge_currency(amount):.2f}"}
    try:
        data = api("POST", f"/v2/payments/captures/{order.paypal_capture_id}/refund", body,
                   request_id=f"lumen-refund-{key or order.id}")
    except PayPalError:
        logger.exception("PayPal refund failed for order %s", order.id)
        return False
    if data.get("status") not in ("COMPLETED", "PENDING"):
        return False
    if amount is None:
        Order.objects.filter(pk=order.pk).update(refund_id=str(data.get("id", ""))[:80], refunded_at=timezone.now())
        OrderEvent.objects.create(order=order, kind="refunded", text=f"Refund of {order.pay_amount} {order.pay_currency.upper()} sent to PayPal")
    return True


def verify_webhook(request, event):
    """Asks PayPal whether this webhook call is genuine."""
    webhook_id = _env("PAYPAL_WEBHOOK_ID")
    if not webhook_id:
        return False
    h = request.headers
    body = {
        "auth_algo": h.get("PAYPAL-AUTH-ALGO", ""), "cert_url": h.get("PAYPAL-CERT-URL", ""),
        "transmission_id": h.get("PAYPAL-TRANSMISSION-ID", ""), "transmission_sig": h.get("PAYPAL-TRANSMISSION-SIG", ""),
        "transmission_time": h.get("PAYPAL-TRANSMISSION-TIME", ""), "webhook_id": webhook_id, "webhook_event": event,
    }
    try:
        return api("POST", "/v1/notifications/verify-webhook-signature", body).get("verification_status") == "SUCCESS"
    except PayPalError:
        return False
