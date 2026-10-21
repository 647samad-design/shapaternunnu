from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Email customers a few days after delivery asking for a review."

    def handle(self, *args, **options):
        from bees.review_requests import send
        self.stdout.write(f"Sent {send()} review requests.")
