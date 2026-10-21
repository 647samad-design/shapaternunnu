"""Runs a command in the main store and every live store, e.g.
``python manage.py for_each_store daily_tasks``, ``for_each_store migrate``
or ``for_each_store --stores-only backup_data --no-media``."""
import argparse

from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run a management command once per store (main store first)."

    def add_arguments(self, parser):
        parser.add_argument("command_name")
        parser.add_argument("--stores-only", action="store_true", help="Skip the main store (put it before the command name)")
        parser.add_argument("args", nargs=argparse.REMAINDER, help="Options for the command, e.g. --keep 7")

    def handle(self, *args, **options):
        command_name, stores_only = options["command_name"], options["stores_only"]
        from bees import multistore
        targets = ([] if stores_only else [None]) + [s for s in multistore.load(force=True).values() if s.active]
        failed = []
        for store in targets:
            label = store.slug if store else "main store"
            self.stdout.write(self.style.MIGRATE_HEADING(f"== {label}"))
            try:
                with multistore.use(store):
                    extra = {"interactive": False} if command_name == "migrate" else {}
                    call_command(command_name, *args, stdout=self.stdout, stderr=self.stderr, **extra)
            except SystemExit as exc:
                if exc.code:
                    failed.append(label)
            except Exception as exc:  # one broken store must not stop the others
                failed.append(label)
                self.stderr.write(f"  {label}: {exc}")
        if failed:
            self.stderr.write(self.style.ERROR(f"Problems in: {', '.join(failed)}"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS(f"Done in {len(targets)} store{'s' if len(targets) != 1 else ''}."))
