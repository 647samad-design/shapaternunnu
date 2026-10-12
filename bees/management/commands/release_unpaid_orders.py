"""
Safety net for card orders whose customers never finished paying.

Stripe's ``checkout.session.expired`` webhook normally cancels these and
returns their stock. If webhooks aren't configured (or one was missed),
run this every 15-30 minutes, e.g. as a cron job / scheduled task:

    python manage.py release_unpaid_orders
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from bees import payments
from bees.models import Order


class Command(BaseCommand):
    help = "Cancel card orders left unpaid past the Stripe session lifetime and restock their items."

    def add_arguments(self, parser):
        parser.add_argument("--minutes", type=int, default=70, help="Age after which an unpaid order is released (default 70, just after the 60-minute payment page expires).")

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(minutes=options["minutes"])
        stale = Order.objects.filter(payment_method="card", payment_status="pending", created_at__lt=cutoff)
        released = paid = 0
        for order in stale:
            session = payments.retrieve_session(order.stripe_session_id) if order.stripe_session_id and payments.is_configured() else None
            if session and session.get("payment_status") == "paid":
                _, newly_paid = payments.mark_order_paid(order.id, session)
                paid += int(newly_paid)
                continue
            if session and session.get("status") == "open":
                continue  # still payable; Stripe will expire it
            payments.release_unpaid_order(order.id, reason="failed")
            released += 1
        # Cash on delivery orders the customer never confirmed by email.
        from bees.views import COD_CONFIRM_HOURS
        cod_cutoff = timezone.now() - timedelta(hours=COD_CONFIRM_HOURS)
        unconfirmed = 0
        for order in Order.objects.filter(payment_method="cod", cod_confirmed=False, status="pending", created_at__lt=cod_cutoff):
            order.cancel_reason = "not confirmed in time"
            order.status = "cancelled"
            order.save(update_fields=["status", "cancel_reason"])
            payments.restock(order)
            unconfirmed += 1
        self.stdout.write(self.style.SUCCESS(
            f"Released {released} unpaid order(s); confirmed {paid} paid order(s); cancelled {unconfirmed} unconfirmed cash order(s)."))
