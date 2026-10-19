from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Morning summary emails for the store owner and sellers."

    def handle(self, *args, **options):
        from bees.digests import send_all
        self.stdout.write(f"Sent {send_all()} summary emails.")
