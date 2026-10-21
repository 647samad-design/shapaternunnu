"""'How was it?' emails: a few days after delivery, ask the customer to
review what they bought (with reward points for a photo review)."""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

WAIT_DAYS = 3      # give them time to try it
LATEST_DAYS = 30   # don't ask about old orders


def due_orders(now=None):
    from .models import Order
    now = now or timezone.now()
    start, end = now - timedelta(days=LATEST_DAYS), now - timedelta(days=WAIT_DAYS)
    return (Order.objects.filter(review_request_sent=False, user__isnull=False)
            .filter(Q(status="delivered", delivered_at__range=(start, end)) | Q(items__delivered_at__range=(start, end)))
            .select_related("user").distinct())


def send(now=None):
    from .models import Order, Profile, Review, SiteSettings
    from .order_emails import absolute
    from .views import _send_html_email
    site = SiteSettings.load()
    if not site.review_requests:
        return 0
    now = now or timezone.now()
    sent = 0
    for order in due_orders(now)[:500]:
        # Claim it first so two runs can't both send.
        if not Order.objects.filter(pk=order.pk, review_request_sent=False).update(review_request_sent=True):
            continue
        user = order.user
        profile = Profile.objects.filter(user=user).first()
        if not user.email or not user.is_active or (profile and not profile.review_emails):
            continue
        reviewed = set(Review.objects.filter(user=user).values_list("product_id", flat=True))
        products, seen = [], set()
        for item in order.items.select_related("product"):
            p = item.product
            if not p or p.id in seen or p.id in reviewed or not item.received_at or item.received_at > now - timedelta(days=WAIT_DAYS):
                continue
            seen.add(p.id)
            products.append({"product": p, "url": absolute(f"/product/{p.id}/#write-review")})
        if not products:
            continue
        _send_html_email("How was your order? Leave a quick review", "bees/emails/review_request.html", {
            "user": user, "order": order, "products": products[:6], "points": site.review_photo_points,
            "prefs_url": absolute("/account/privacy/")}, user.email, lang=profile.language if profile else None)
        sent += 1
    return sent
