"""Starts and ends timed sales (also runs automatically during the day)."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Start scheduled sales and end finished ones."

    def handle(self, *args, **options):
        from bees.promotions import tick
        tick()
        from bees.campaigns import continue_sending
        continue_sending()
        self.stdout.write("  sales checked, email campaigns continued")
