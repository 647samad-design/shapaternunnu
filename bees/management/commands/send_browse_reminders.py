from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Email signed-in shoppers about products they looked at but didn't buy (at most weekly)."

    def handle(self, *args, **options):
        from bees.shopper import send_browse_reminders
        self.stdout.write(f"Sent {send_browse_reminders()} 'still looking?' emails.")
