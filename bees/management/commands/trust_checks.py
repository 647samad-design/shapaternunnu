"""Daily trust checks: copied listing photos, suspicious reviews and
fake-order networks. Findings go to Admin > Trust & safety."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Look for copied listings, fake reviews and linked risky orders."

    def handle(self, *args, **options):
        from bees.trustcheck import run_checks
        result = run_checks()
        self.stdout.write("Trust checks: " + ", ".join(f"{k} {v}" for k, v in result.items()))
