"""Tell shoppers when something in their wishlist gets cheaper (daily)."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Send wishlist price-drop alerts."

    def handle(self, *args, **options):
        from bees.price_alerts import run
        self.stdout.write(f"Price-drop alerts sent to {run()} shopper(s).")
