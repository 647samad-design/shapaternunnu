"""Morning summary emails: the owner sees yesterday's sales and what needs
doing; each seller sees orders to ship and questions waiting. Sent by
daily_tasks (Store settings > Save time > Daily summary emails)."""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone


def owner_summary():
    from .manage_views import attention_counts
    from django.contrib.auth import get_user_model
    from .models import Order, Product
    User = get_user_model()
    end = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=1)
    orders = Order.objects.filter(created_at__gte=start, created_at__lt=end).exclude(status="cancelled")
    revenue = sum((o.total for o in orders), Decimal("0"))
    todo = attention_counts(force=True)
    return {
        "day": start.date(), "orders": orders.count(), "revenue": revenue,
        "customers": User.objects.filter(date_joined__gte=start, date_joined__lt=end).count(),
        "todo": [(label, todo.get(key, 0), url) for key, label, url in [
            ("orders", "orders to send", "/manage/orders/"), ("products", "products to review", "/manage/products/?state=pending"),
            ("sellers", "sellers / payouts waiting", "/manage/sellers/"), ("returns", "return requests", "/manage/returns/"),
            ("messages", "customer messages", "/manage/support/"), ("claims", "escalated problems", "/manage/problems/")] if todo.get(key)],
        "low_stock": list(Product.objects.listed().filter(seller_account__isnull=True, stock__lte=3, has_variants=False)
                          .values_list("name", "stock")[:8]),
    }


def seller_summary(seller):
    from . import seller_center as sc
    from .models import Question, QuoteRequest, ReturnRequest
    items = sc.shippable(sc.sold_items(seller))
    to_ship = items.filter(fulfillment_status="pending").values("order_id").distinct().count()
    yesterday = timezone.now() - timedelta(days=1)
    sold = sc.sold_items(seller).filter(order__created_at__gte=yesterday).aggregate(n=Sum("quantity"))["n"] or 0
    return {
        "to_ship": to_ship, "sold": sold,
        "questions": Question.objects.filter(product__seller_account=seller, answer="").count(),
        "quotes": QuoteRequest.objects.filter(seller_account=seller, status="open").count(),
        "returns": ReturnRequest.objects.filter(order_item__seller_account=seller, status="requested").count(),
        "low_stock": list(seller.products.exclude(approval_status="archived").filter(stock__lte=3, has_variants=False)
                          .values_list("name", "stock")[:8]),
    }


def send_all():
    from django.contrib.auth import get_user_model
    from .models import SellerAccount, SiteSettings
    User = get_user_model()
    from .order_emails import absolute
    from .views import _send_html_email
    if not SiteSettings.load().daily_digest:
        return 0
    sent = 0
    owner = owner_summary()
    for user in User.objects.filter(is_superuser=True, is_active=True).exclude(email=""):
        _send_html_email(f"Yesterday: {owner['orders']} orders · your to-do list", "bees/emails/digest_owner.html",
                         {"s": owner, "admin_url": absolute("/manage/")}, user.email)
        sent += 1
    for seller in SellerAccount.objects.filter(status="approved", digest_emails=True).select_related("user"):
        s = seller_summary(seller)
        if not (s["to_ship"] or s["questions"] or s["quotes"] or s["returns"] or s["low_stock"] or s["sold"]):
            continue  # nothing to say - no email
        if seller.user.email:
            _send_html_email(f"Today: {s['to_ship']} order{'s' if s['to_ship'] != 1 else ''} to ship", "bees/emails/digest_seller.html",
                             {"s": s, "seller": seller, "center_url": absolute("/seller/dashboard/")}, seller.user.email)
            sent += 1
    return sent
