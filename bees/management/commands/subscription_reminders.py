from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Subscribe & Save: tell shoppers when their reorder is due."

    def handle(self, *args, **options):
        from bees.market import remind_subscriptions
        self.stdout.write(f"Sent {remind_subscriptions()} subscription reminders.")
