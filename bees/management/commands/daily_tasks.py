"""Everything the store needs once a day, in one command - so a single
PythonAnywhere scheduled task covers it:

    cd ~/Lumen-Market && venv/bin/python manage.py daily_tasks

  1. cancel card orders that were never paid (safety net for webhooks)
  2. keep orders moving: remind sellers of late orders, cancel + refund
     orders nobody shipped, mark long-shipped items delivered, send
     unanswered problem reports to the store team
  3. email cart reminders
  3. back up the database and images
  4. remind sellers whose paid plan ends soon, refresh Top seller badges
     and currency rates
  5. delete expired sign-in sessions
  (on a demo copy with DEMO_MODE=True: also reset the demo accounts)
Each step runs even if an earlier one fails.
"""
from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run the store's daily maintenance jobs."

    def handle(self, *args, **options):
        failed = []
        from django.conf import settings
        jobs = [("release_unpaid_orders",), ("order_automation",), ("send_cart_reminders",), ("plan_reminders",), ("update_seller_badges",), ("update_currency_rates",), ("backup_data",), ("clearsessions",)]
        if getattr(settings, "DEMO_MODE", False):
            jobs.append(("demo_setup", "--reset"))
        for job in jobs:
            name = job[0]
            self.stdout.write(f"- {name}")
            try:
                call_command(*job, stdout=self.stdout)
            except Exception as exc:  # keep going, report at the end
                failed.append(name)
                self.stderr.write(f"  {name} failed: {exc}")
        from bees.alerts import DAILY_TASKS, log_once
        log_once(DAILY_TASKS, "ok" if not failed else "errors in " + ", ".join(failed), minutes=0)
        if failed:
            self.stderr.write(self.style.ERROR(f"Finished with errors in: {', '.join(failed)}"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("Daily tasks finished."))
