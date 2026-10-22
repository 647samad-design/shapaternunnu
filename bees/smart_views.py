"""Pages for the smart store tools (see smart.py): Restock, Customer
segments and the Store copilot."""
import csv

from django.contrib import messages
from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import redirect

from . import smart


def _restock_page(request, products, render, extra=None):
    status = request.GET.get("show", "attention")
    rows = smart.restock_plan(products)
    counts = {k: sum(1 for r in rows if r["status"] == k) for k in ("out", "urgent", "soon", "ok", "slow")}
    if status == "attention":
        shown = [r for r in rows if r["status"] in ("out", "urgent", "soon")]
    elif status in counts:
        shown = [r for r in rows if r["status"] == status]
    else:
        shown = rows
    if request.GET.get("export") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="restock.csv"'
        w = csv.writer(response)
        w.writerow(["product_id", "product", "stock", "sold_30_days", "per_week", "days_left", "reorder", "status"])
        for r in rows:
            w.writerow([r["product"].pk, r["product"].name, r["stock"], r["sold"], r["per_week"],
                        "" if r["days_left"] is None else r["days_left"], r["reorder"], r["status"]])
        return response
    return render(request, "restock.html", {"section": "restock", "rows": shown[:300], "counts": counts, "show": status,
                                            "lead_days": smart.LEAD_DAYS, "cover_days": smart.COVER_DAYS, **(extra or {})})


def manage_restock(request):
    from .manage_views import _render
    from .models import Product
    products = Product.objects.exclude(approval_status="rejected").select_related("seller_account")
    return _restock_page(request, list(products[:5000]), _render)


def seller_restock(request):
    from .models import Product
    from .seller_views import _render
    products = Product.objects.filter(seller_account=request.seller)
    return _restock_page(request, list(products[:5000]), _render)


def manage_segments(request):
    from .manage_views import _render
    people = smart.customers()
    export = request.GET.get("export", "")
    if export:
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="customers-{export}.csv"'
        w = csv.writer(response)
        w.writerow(["email", "name", "segment", "orders", "spent", "last_order", "days_since"])
        for p in sorted(people.values(), key=lambda p: -p["spent"]):
            if export == "all" or p["segment"] == export:
                w.writerow([p["email"], p["name"], smart.SEGMENT_LABELS[p["segment"]], p["orders"], f"{p['spent']:.2f}",
                            p["last"].strftime("%Y-%m-%d"), p["days"]])
        return response
    return _render(request, "segments.html", {"section": "customers", "segments": smart.segment_summary(people),
                                              "total": len(people)})


def manage_copilot(request):
    from .manage_views import _render
    from .multistore import store_settings
    data = cache.get("smart:snapshot")
    if data is None or request.GET.get("refresh"):
        data = smart.snapshot()
        cache.set("smart:snapshot", data, 600)
    answer, used_ai, question = None, False, ""
    if request.method == "POST":
        question = request.POST.get("question", "").strip()[:500]
        key = f"copilot:{request.user.pk}"
        hits = cache.get(key, 0)
        if not question:
            messages.error(request, "Type a question first.")
            return redirect("manage_copilot")
        if hits >= 30:
            messages.error(request, "That's a lot of questions for one hour - try again a little later.")
            return redirect("manage_copilot")
        cache.set(key, hits + 1, 3600)
        answer, used_ai = smart.ask(question, data)
    return _render(request, "copilot.html", {
        "section": "copilot", "insights": smart.quick_insights(data), "answer": answer, "used_ai": used_ai, "question": question,
        "ai_on": bool(getattr(store_settings, "AI_API_KEY", "")), "data": data,
        "examples": ["Which products should I promote this week?", "Why did sales change compared to last week?",
                     "Who are my best customers and how do I keep them?", "What should I restock first?"],
    })
