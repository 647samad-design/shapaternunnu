"""Moves running price tests to their other price for the day, and ends
finished ones (keeping the better price). Part of daily_tasks."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Advance price tests by one day."

    def handle(self, *args, **options):
        from bees.pricetest import tick
        self.stdout.write(f"Price tests updated: {tick()}")
