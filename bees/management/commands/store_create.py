"""Adds a store in multi-store mode: its folder, database, keys file and
owner account."""
import json
import os
import secrets

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "Create a new store (multi-store mode): python manage.py store_create acme --host shop.acme.com --name 'Acme' --owner-email you@acme.com"

    def add_arguments(self, parser):
        parser.add_argument("slug", help="Short name: lowercase letters, numbers and dashes, e.g. acme")
        parser.add_argument("--host", action="append", required=True, help="Web address, e.g. shop.acme.com (repeat for several)")
        parser.add_argument("--name", required=True, help="Store name shown to shoppers")
        parser.add_argument("--owner-email", required=True)
        parser.add_argument("--database", default="", help="Database URL (default: its own SQLite file). PostgreSQL: create the empty database first.")
        parser.add_argument("--currency", default="", help="Store currency, e.g. EUR (default: the main store's)")
        parser.add_argument("--demo", action="store_true", help="Fill it with sample products")

    def handle(self, *args, slug, host, name, owner_email, database, currency, demo, **options):
        from bees import multistore
        if not multistore.enabled():
            raise CommandError("Turn on multi-store mode first: add MULTISTORE=True to .env and reload the app.")
        if not multistore.SLUG.match(slug):
            raise CommandError("Use 2-41 lowercase letters, numbers or dashes for the store's short name.")
        stores = multistore.load(force=True)
        if slug in stores:
            raise CommandError(f"A store called '{slug}' already exists.")
        hosts = [h.strip().lower() for h in host if h.strip()]
        taken = {h for s in stores.values() for h in s.hosts}
        if set(hosts) & taken:
            raise CommandError(f"Already used by another store: {', '.join(sorted(set(hosts) & taken))}")
        currency = (currency or settings.STORE_CURRENCY).upper()
        if len(currency) != 3:
            raise CommandError("Currency: a 3-letter code like EUR.")
        folder = multistore.root() / slug
        folder.mkdir(parents=True, exist_ok=False)
        (folder / "store.json").write_text(json.dumps({"name": name, "hosts": hosts, "database": database, "active": True,
                                                       "created": timezone.now().isoformat()}, indent=2), encoding="utf-8")
        env_path = folder / ".env"
        env_path.write_text(f"# Keys for {name} only. Anything not set here comes from the main .env.\nSTORE_CURRENCY={currency}\n"
                            f"SITE_URL=https://{hosts[0]}\n", encoding="utf-8")
        os.chmod(env_path, 0o600)
        store = multistore.load(force=True)[slug]
        password = secrets.token_urlsafe(12)
        with multistore.use(store):
            self.stdout.write(f"Setting up the database for {name}...")
            call_command("migrate", interactive=False, verbosity=0)
            from django.contrib.auth import get_user_model
            from bees.models import SiteSettings
            User = get_user_model()
            User.objects.create_superuser(username=owner_email.split("@")[0][:30] or "owner", email=owner_email, password=password)
            site = SiteSettings.load()
            site.site_name = name[:100]
            site.support_email = owner_email
            site.save()
            if demo:
                call_command("seed_data", stdout=self.stdout)
        self.stdout.write(self.style.SUCCESS(f"Store '{name}' is ready."))
        self.stdout.write(f"  Address:  https://{hosts[0]}/login/  (point the domain at this server first)")
        self.stdout.write(f"  Owner:    {owner_email}")
        self.stdout.write(f"  Password: {password}   <- shown once; the owner should change it after signing in")
        self.stdout.write("  Next: reload the web app so the new address is accepted, then the owner runs the setup wizard "
                          "and adds their own Stripe/PayPal/email keys under Keys & passwords.")
