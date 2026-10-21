"""Restore a backup made by backup_data.

    python manage.py restore_backup --list          show available backups
    python manage.py restore_backup                 restore the newest one
    python manage.py restore_backup backup-20261007-0300.tar.gz

The current database is copied to db.sqlite3.before-restore first.
Reload the website afterwards (PythonAnywhere: Web tab > Reload).
"""
import os
import shutil
import tarfile
import tempfile

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from .backup_data import database, folder, list_backups, local_dir, media_folders, remote_storage


class Command(BaseCommand):
    help = "Restore the database (and local images) from a backup."

    def add_arguments(self, parser):
        parser.add_argument("name", nargs="?")
        parser.add_argument("--list", action="store_true")
        parser.add_argument("--yes", action="store_true", help="Don't ask for confirmation.")

    def handle(self, *args, **options):
        backups = list_backups()
        if options["list"]:
            for b in backups:
                self.stdout.write(b)
            if not backups:
                self.stdout.write("No backups yet.")
            return
        if not backups:
            raise CommandError("No backups found.")
        name = options["name"] or backups[0]
        if name not in backups:
            raise CommandError(f"{name} not found. Use --list to see backups.")
        if not options["yes"]:
            answer = input(f"Replace the current data with {name}? Type yes: ")
            if answer.strip().lower() != "yes":
                self.stdout.write("Cancelled.")
                return
        with tempfile.TemporaryDirectory() as tmp:
            archive = os.path.join(tmp, name)
            storage = remote_storage()
            if storage:
                with storage.open(f"{folder()}/{name}", "rb") as src, open(archive, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            else:
                shutil.copy(os.path.join(local_dir(), name), archive)
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(tmp, filter="data")
            db = dict(database())
            if os.path.exists(os.path.join(tmp, "db.sqlite3")):
                if not db["ENGINE"].endswith("sqlite3"):
                    raise CommandError("This backup is from a SQLite database but the site now uses another database.")
                current = str(db["NAME"])
                if os.path.exists(current):
                    shutil.copy(current, current + ".before-restore")
                from django.db import connections
                connections.close_all()
                shutil.copy(os.path.join(tmp, "db.sqlite3"), current)
            else:
                call_command("flush", "--no-input")
                call_command("loaddata", os.path.join(tmp, "data.json"))
            for target, arc in media_folders():
                src = os.path.join(tmp, arc)
                if os.path.isdir(src):
                    shutil.copytree(src, target, dirs_exist_ok=True)
        self.stdout.write(self.style.SUCCESS(f"Restored {name}. Now reload the website."))
