"""Runs every seller's auto-repricer rules (also happens instantly when a
competing offer changes price). Part of daily_tasks."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Apply auto-repricer rules."

    def handle(self, *args, **options):
        from bees.repricer import run_all
        self.stdout.write(f"Repriced {run_all()} listing(s).")
