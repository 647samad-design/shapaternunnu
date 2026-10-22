"""Everything the store needs once a day, in one command - so a single
PythonAnywhere scheduled task covers it:

    cd ~/Lumen-Market && venv/bin/python manage.py daily_tasks

  1. cancel card orders that were never paid (safety net for webhooks)
  2. keep orders moving: remind sellers of late orders, cancel + refund
     orders nobody shipped, mark long-shipped items delivered, send
     unanswered problem reports to the store team
  3. email cart reminders and wishlist price-drop alerts
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

    def add_arguments(self, parser):
        parser.add_argument("--catchup", action="store_true",
                            help="Started automatically because the scheduled run was missed (skips the demo reset).")
        parser.add_argument("--skip-demo-reset", action="store_true",
                            help="Don't reset the demo accounts (used by the 'Run daily tasks now' button).")

    def handle(self, *args, **options):
        failed = []
        from django.conf import settings
        jobs = [("release_unpaid_orders",), ("order_automation",), ("send_cart_reminders",), ("price_drop_alerts",), ("notify_followers",), ("subscription_reminders",), ("send_digests",), ("send_review_requests",), ("send_browse_reminders",), ("invoice_reminders",), ("run_promotions",), ("auto_payouts",), ("plan_reminders",), ("update_seller_badges",), ("update_currency_rates",), ("prune_translations",), ("backup_data",), ("clearsessions",)]
        if getattr(settings, "DEMO_MODE", False) and not options.get("catchup") and not options.get("skip_demo_reset"):
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
        note = "ok" if not failed else "errors in " + ", ".join(failed)
        log_once(DAILY_TASKS, note + (" (automatic catch-up)" if options.get("catchup") and not failed else ""), minutes=0)
        if failed:
            self.stderr.write(self.style.ERROR(f"Finished with errors in: {', '.join(failed)}"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("Daily tasks finished."))
