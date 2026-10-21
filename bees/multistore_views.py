"""Admin > Stores: the platform owner's list of client stores (multi-store
mode, main store only)."""
from datetime import timedelta

from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect
from django.utils import timezone

from . import multistore


def platform_admin(request):
    return multistore.enabled() and multistore.current() is None and request.user.is_superuser


def stores_page(request):
    from .management.commands.stores import set_active
    from .manage_views import _log, _render
    if not platform_admin(request):
        raise Http404
    stores = multistore.load(force=True)
    if request.method == "POST":
        store = stores.get(request.POST.get("slug", ""))
        if store:
            active = request.POST.get("action") == "resume"
            set_active(store, active)
            _log(request, f"Store {store.slug} {'resumed' if active else 'paused'}")
            messages.success(request, f"{store.name} is {'live again' if active else 'paused - shoppers see a short-break page'}.")
        return redirect("manage_stores")
    rows = []
    since = timezone.now() - timedelta(days=30)
    from .models import Order, Product
    for store in stores.values():
        row = {"store": store, "orders": None, "recent": None, "products": None, "error": ""}
        try:
            with multistore.use(store):
                row["orders"] = Order.objects.count()
                row["recent"] = Order.objects.filter(created_at__gte=since).count()
                row["products"] = Product.objects.count()
        except Exception as exc:
            row["error"] = exc.__class__.__name__
        rows.append(row)
    return _render(request, "stores.html", {"section": "stores", "rows": rows})
