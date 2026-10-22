"""Clears 'untranslated' shop texts that no longer need a translation
(now built in, or product/store names). Runs in daily_tasks."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Remove untranslated texts that are now covered or are names."

    def handle(self, *args, **options):
        from bees.i18n import prune_missing
        self.stdout.write(f"Removed {prune_missing()} text(s) that no longer need translating.")
