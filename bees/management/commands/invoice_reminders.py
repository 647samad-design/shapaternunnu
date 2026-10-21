from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Remind business customers about invoices that are due soon or overdue."

    def handle(self, *args, **options):
        from bees.ops import remind_invoices
        self.stdout.write(f"Sent {remind_invoices()} invoice reminders.")
