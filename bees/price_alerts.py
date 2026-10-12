"""Wishlist price-drop alerts (daily, part of daily_tasks).

When something a shopper saved gets at least ``MIN_DROP`` cheaper than the
price they last saw, they get one notification per item and one email
listing everything that dropped. The new price becomes the reference, so
nobody is alerted twice for the same drop.
"""
from collections import defaultdict
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

MIN_DROP = Decimal("0.05")  # 5%


def run():
    from .models import Notification, Product, Wishlist
    from .order_emails import absolute
    from .views import _send_html_email
    live_ids = Product.objects.live().values_list("pk", flat=True)
    rows = (Wishlist.objects.filter(product_id__in=live_ids, price_seen__isnull=False, product__stock__gt=0)
            .select_related("product", "user", "user__profile"))
    per_user = defaultdict(list)
    for w in rows:
        old, new = w.price_seen, w.product.price
        if old and new <= old * (1 - MIN_DROP):
            per_user[w.user].append((w, old, new))
    now = timezone.now()
    sent = 0
    for user, drops in per_user.items():
        for w, old, new in drops:
            pct = int(round((old - new) / old * 100))
            Notification.objects.create(user=user, link=reverse("product_detail", args=[w.product_id]),
                                        message=f"Price drop: '{w.product.name}' is now {pct}% cheaper."[:255])
            Wishlist.objects.filter(pk=w.pk).update(price_seen=new, alerted_at=now)
        profile = getattr(user, "profile", None)
        if user.email and (profile is None or profile.price_drop_alerts):
            items = [{"product": w.product, "old": old, "new": new, "pct": int(round((old - new) / old * 100)),
                      "url": absolute(reverse("product_detail", args=[w.product_id]))} for w, old, new in drops]
            _send_html_email("Price dropped on your wishlist", "bees/emails/price_drop.html",
                             {"user": user, "items": items, "wishlist_url": absolute(reverse("wishlist")),
                              "prefs_url": absolute(reverse("account_privacy"))}, user.email)
        sent += 1
    return sent
