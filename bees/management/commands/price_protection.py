"""Gives shoppers the difference back as store credit when a product
they bought gets cheaper within the price drop protection window
(Store settings). Runs in daily_tasks."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Credit price drops on recent orders (price drop protection)."

    def handle(self, *args, **options):
        from bees.smart import price_protection
        self.stdout.write(f"Price drop credits: {price_protection()}")
