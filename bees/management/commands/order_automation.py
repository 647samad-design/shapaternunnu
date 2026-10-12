"""Late-shipping reminders, auto-cancel, auto-delivery and problem-report
escalation. Part of ``daily_tasks``; safe to run more often."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Keep orders moving: reminders, auto-cancel, auto-delivery, claim escalation."

    def handle(self, *args, **options):
        from bees.automation import run
        summary = run()
        self.stdout.write(", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in summary.items()))
