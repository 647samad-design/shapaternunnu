"""Customer emails for every step of an order.

Order.save() calls ``status_changed`` whenever an order's status changes -
from the store admin, Django admin, a seller's fulfilment update or the
customer cancelling - so the customer is always told:

    confirmed  -> "Your order is confirmed, arrives by <date>"
    shipped    -> "Your order is on its way" (+ tracking number)
    delivered  -> "Your order has been delivered" (+ review / return links)
    cancelled  -> "Your order has been cancelled" (+ refund details)

The "order placed" / "payment received" email is sent separately by the
checkout and payment views (see views._send_order_confirmation).

Emails go out after the database transaction commits, so a rolled-back
change never emails anyone, and a mail-server problem never breaks the
status change itself.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.html import strip_tags

logger = logging.getLogger(__name__)

SUBJECTS = {
    "confirmed": "Your order #{id} is confirmed",
    "shipped": "Your order #{id} is on its way",
    "delivered": "Your order #{id} has been delivered",
    "cancelled": "Your order #{id} has been cancelled",
    "item_shipped": "Part of your order #{id} is on its way",
    "item_delivered": "Part of your order #{id} has been delivered",
}


def site_url():
    """Public base URL for links in emails sent outside a request."""
    configured = getattr(settings, "SITE_URL", "")
    if configured:
        return configured.rstrip("/")
    for host in settings.ALLOWED_HOSTS:
        host = host.strip()
        if host and host not in ("*", "localhost", "127.0.0.1") and not host.startswith("."):
            return f"https://{host}"
    return "http://localhost:8000"


def absolute(path):
    return site_url() + path


def add_business_days(start, days):
    current = start
    added = 0
    while added < days:
        current += timedelta(days=1)
        if current.weekday() < 5:  # Mon-Fri
            added += 1
    return current


def default_delivery_date(order=None, country=None):
    from .models import SiteSettings
    from .shipping import delivery_days
    days = delivery_days(country or (order.country if order else None)) or SiteSettings.load().delivery_days or 5
    start = timezone.localdate(order.created_at) if order and order.created_at else timezone.localdate()
    return add_business_days(start, days)


def send(order, kind, item_ids=None):
    """Renders and sends one order email. Never raises. ``item_ids`` limits
    the email to some items (one seller's package)."""
    recipient = order.contact_email
    if not recipient or kind not in SUBJECTS:
        return False
    try:
        from .models import SiteSettings
        brand = SiteSettings.load()
        context = {
            "brand": brand,
            "order": order,
            "kind": kind,
            "orders_url": absolute(order.tracking_path() if order.is_guest else reverse("my_orders")),
            "invoice_url": absolute(reverse("invoice_pdf", args=[order.id])) + (f"?t={order.access_token}" if order.is_guest else ""),
            "shop_url": absolute(reverse("home")),
            "items": [
                {
                    "name": item.product_name,
                    "quantity": item.quantity,
                    "subtotal": item.subtotal,
                    "review_url": absolute(reverse("product_detail", args=[item.product_id])) if item.product_id else "",
                }
                for item in order.items.all()
                if not item_ids or item.pk in item_ids
            ],
        }
        html = render_to_string("bees/emails/order_update.html", context)
        subject = f"{brand.site_name}: " + SUBJECTS[kind].format(id=order.id)
        from .email_i18n import language_for, localize
        html, subject = localize(html, subject, language_for(order=order))
        email = EmailMultiAlternatives(subject, strip_tags(html), None, [recipient])
        email.attach_alternative(html, "text/html")
        email.send(fail_silently=False)
        return True
    except Exception as exc:
        from .alerts import email_failed
        email_failed(SUBJECTS.get(kind, kind).format(id=order.pk), recipient, exc)
        return False


def send_later(order, kind, item_ids=None):
    """Sends after the database change is saved and the page has been
    delivered, so nobody waits for the mail server."""
    order_id = order.pk

    def _queue():
        from .background import after_response
        from .models import Order

        def _send():
            fresh = Order.objects.filter(pk=order_id).first()
            if fresh:
                send(fresh, kind, item_ids)
        after_response(_send)

    transaction.on_commit(_queue)


def status_changed(order, old_status, new_status):
    """Called by Order.save() after a status change."""
    if getattr(order, "_skip_status_email", False):
        return
    if new_status not in SUBJECTS:
        return
    order_id = order.pk

    def _send():
        from .models import Order
        fresh = Order.objects.filter(pk=order_id).first()
        if fresh and fresh.status == new_status:
            send(fresh, new_status)

    def _queue():
        from .background import after_response
        after_response(_send)

    transaction.on_commit(_queue)
