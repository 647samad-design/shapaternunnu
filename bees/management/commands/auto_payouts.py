"""Pays sellers automatically (Store settings > Automatic seller payouts),
checks PayPal payouts still on their way, and tidies old statistics."""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Send automatic seller payouts and check pending ones."

    def add_arguments(self, parser):
        parser.add_argument("--now", action="store_true", help="Pay now even if today isn't a payout day.")

    def handle(self, *args, **options):
        from bees import payouts
        from bees.models import DailyStat
        checked = payouts.refresh_processing()
        self.stdout.write(f"  checked {checked} payout(s) on their way")
        if options["now"] or payouts.due_today():
            sent, total, errors = payouts.pay_all()
            self.stdout.write(f"  sent {sent} payout(s), total {total}")
            for e in errors:
                self.stderr.write(f"  {e}")
        removed, _ = DailyStat.objects.filter(date__lt=timezone.localdate() - timedelta(days=760)).delete()
        if removed:
            self.stdout.write(f"  removed {removed} old statistic rows")
        # Housekeeping so tables don't grow forever.
        from bees.models import GiftCard, Notification
        old = timezone.now() - timedelta(days=180)
        n, _ = Notification.objects.filter(is_read=True, created_at__lt=old).delete()
        g, _ = GiftCard.objects.filter(status="pending", created_at__lt=timezone.now() - timedelta(days=7)).delete()
        if n or g:
            self.stdout.write(f"  removed {n} old read notifications, {g} unpaid gift card checkouts")
