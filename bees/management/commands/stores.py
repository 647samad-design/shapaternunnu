"""Lists the stores (multi-store mode), and pauses or resumes one."""
import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "List stores, or --pause / --resume one (e.g. when a client stops paying)."

    def add_arguments(self, parser):
        parser.add_argument("--pause", metavar="SLUG")
        parser.add_argument("--resume", metavar="SLUG")

    def handle(self, *args, pause=None, resume=None, **options):
        from bees import multistore
        stores = multistore.load(force=True)
        for slug, active in ((pause, False), (resume, True)):
            if not slug:
                continue
            store = stores.get(slug)
            if not store:
                raise CommandError(f"No store called '{slug}'.")
            set_active(store, active)
            self.stdout.write(self.style.SUCCESS(f"{store.name} {'paused' if not active else 'is live again'}."))
            return
        if not stores:
            self.stdout.write("No stores yet. Create one with: python manage.py store_create")
            return
        from bees.models import Order
        for store in stores.values():
            with multistore.use(store):
                try:
                    orders = Order.objects.count()
                except Exception as exc:
                    orders = f"database error: {exc.__class__.__name__}"
            self.stdout.write(f"{store.slug:<16} {'live  ' if store.active else 'PAUSED'} {store.name:<30} {', '.join(store.hosts):<40} orders: {orders}")


def set_active(store, active):
    path = store.folder / "store.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["active"] = active
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    from bees import multistore
    multistore.load(force=True)
