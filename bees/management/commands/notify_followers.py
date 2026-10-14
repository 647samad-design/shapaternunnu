"""Tells shoppers about new products from stores they follow (once a day,
one notification per store)."""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone


class Command(BaseCommand):
    help = "Notify store followers about new products from the last day."

    def handle(self, *args, **options):
        from bees.models import Notification, Product, StoreFollow
        since = timezone.now() - timedelta(days=1)
        new = (Product.objects.live().filter(created_at__gte=since, seller_account__isnull=False)
               .values("seller_account_id").annotate(n=Count("id")))
        sent = 0
        for row in new:
            follows = StoreFollow.objects.filter(seller_account_id=row["seller_account_id"]).select_related("seller_account")
            notes = []
            for f in follows:
                store = f.seller_account
                notes.append(Notification(user_id=f.user_id, link=f"/store/{store.display_name}/",
                                          message=f"{store.display_name} added {row['n']} new product{'s' if row['n'] != 1 else ''}"[:255]))
            Notification.objects.bulk_create(notes)
            sent += len(notes)
        self.stdout.write(f"Sent {sent} new-product notifications.")
