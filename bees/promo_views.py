"""Discount codes and timed sales: Seller Center > Promotions (a seller's own
products) and Manage > Coupons > Sales (the store's own products)."""
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone

from . import promotions
from .models import AuditLog, Coupon, Product, Promotion
from .seller_views import _render as seller_render, seller_required

CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,29}$")


def _int(value, lo, hi):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _when(value):
    try:
        dt = datetime.strptime(value or "", "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


def handle_post(request, seller):
    """Shared POST actions. Returns a message-free redirect target name."""
    action = request.POST.get("action")
    owner = {"seller_account": seller}
    if action == "coupon":
        code = request.POST.get("code", "").strip().upper()
        pct = _int(request.POST.get("percent_off"), 1, 90)
        if not CODE_RE.match(code):
            messages.error(request, "Codes are 3-30 letters or numbers, e.g. SUMMER20.")
        elif pct is None:
            messages.error(request, "Discount must be between 1 and 90%.")
        elif Coupon.objects.filter(code__iexact=code).exists():
            messages.error(request, "That code is already taken. Try another.")
        else:
            try:
                min_order = Decimal(request.POST.get("min_order_value") or "0").quantize(Decimal("0.01"))
            except InvalidOperation:
                min_order = Decimal("0")
            expiry = request.POST.get("expiry_date") or None
            try:
                expiry = datetime.strptime(expiry, "%Y-%m-%d").date() if expiry else None
            except ValueError:
                expiry = None
            Coupon.objects.create(code=code, percent_off=pct, min_order_value=max(min_order, Decimal("0")), expiry_date=expiry,
                                  usage_limit=_int(request.POST.get("usage_limit"), 1, 1_000_000),
                                  per_user_limit=_int(request.POST.get("per_user_limit"), 1, 100) or 1, **owner)
            AuditLog.objects.create(user=request.user, action=f"Created coupon {code} ({pct}%)" + (f" for {seller.display_name}" if seller else ""))
            messages.success(request, f"Code {code} is live: {pct}% off" + (" your products." if seller else " the whole store."))
    elif action in ("coupon_toggle", "coupon_delete"):
        coupon = get_object_or_404(Coupon, pk=request.POST.get("id"), **owner)
        if action == "coupon_delete":
            coupon.delete()
            messages.success(request, f"Code {coupon.code} deleted.")
        else:
            coupon.active = not coupon.active
            coupon.save(update_fields=["active"])
            messages.success(request, f"Code {coupon.code} is now {'on' if coupon.active else 'paused'}.")
    elif action == "sale":
        name = request.POST.get("name", "").strip()[:80] or "Sale"
        pct = _int(request.POST.get("percent_off"), 1, 90)
        starts, ends = _when(request.POST.get("starts_at")), _when(request.POST.get("ends_at"))
        category = request.POST.get("category", "")
        if category and category not in dict(Product.CATEGORY_CHOICES):
            category = ""
        if pct is None:
            messages.error(request, "Discount must be between 1 and 90%.")
        elif not starts or not ends or ends <= starts:
            messages.error(request, "Choose when the sale starts and ends (end after start).")
        elif ends - starts > timedelta(days=60):
            messages.error(request, "Sales can run for up to 60 days.")
        elif ends <= timezone.now():
            messages.error(request, "That end time has already passed.")
        else:
            promo = Promotion.objects.create(name=name, percent_off=pct, category=category, starts_at=starts, ends_at=ends,
                                             created_by=request.user, **owner)
            count = promotions.products_for(promo).count()
            if not count:
                promo.delete()
                messages.error(request, "No products match that sale.")
            else:
                promotions.tick()
                promo.refresh_from_db()
                AuditLog.objects.create(user=request.user, action=f"Sale '{name}' {pct}% ({count} products)")
                messages.success(request, f"'{name}' {'has started' if promo.status == 'active' else 'is scheduled'}: {pct}% off {count} product{'s' if count != 1 else ''}.")
    elif action == "sale_stop":
        promo = get_object_or_404(Promotion, pk=request.POST.get("id"), **owner)
        restored = promotions.stop(promo, status="cancelled" if promo.status == "scheduled" else "ended")
        messages.success(request, f"'{promo.name}' stopped." + (f" {restored} price{'s' if restored != 1 else ''} put back." if restored else ""))
    else:
        messages.error(request, "Unknown action.")


def context(seller):
    owner = {"seller_account": seller}
    coupons = list(Coupon.objects.filter(purpose="", **owner).order_by("-id")[:100])
    for c in coupons:
        c.used = c.times_used()
    return {
        "coupons": coupons, "sales": Promotion.objects.filter(**owner)[:50], "categories": Product.CATEGORY_CHOICES,
        "today": timezone.localdate(),
        "tz_name": timezone.get_current_timezone_name(),
        "now_local": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
        "week_local": (timezone.localtime() + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M"),
    }


@seller_required(approved=True)
def seller_promotions(request):
    if request.method == "POST":
        handle_post(request, request.seller)
        return redirect("seller_promotions")
    promotions.tick()
    return seller_render(request, "promotions.html", {"section": "promotions", **context(request.seller)})


# ---------------------------------------------------------------------------
# Sponsored products
# ---------------------------------------------------------------------------

def _money_value(value, lo, hi):
    try:
        v = Decimal(value or "").quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    return v if lo <= v <= hi else None


@seller_required(approved=True)
def seller_ads(request):
    from . import ads
    from .models import AdCampaign, SiteSettings
    from .templatetags.bees_extras import money
    seller = request.seller
    site = SiteSettings.load()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create":
            product = Product.objects.live().filter(pk=request.POST.get("product"), seller_account=seller).first()
            bid = _money_value(request.POST.get("bid"), site.ads_min_bid, Decimal("100"))
            budget = _money_value(request.POST.get("daily_budget"), Decimal("1"), Decimal("10000"))
            if not site.ads_enabled:
                messages.error(request, "Sponsored products are turned off on this store.")
            elif product is None:
                messages.error(request, "Choose one of your live products.")
            elif bid is None:
                messages.error(request, f"Cost per click must be at least {money(site.ads_min_bid)}.")
            elif budget is None or budget < bid:
                messages.error(request, "The daily budget must be at least one click.")
            elif AdCampaign.objects.filter(product=product, status__in=["active", "paused"]).exists():
                messages.error(request, "That product already has an ad. Change its bid or budget below.")
            else:
                AdCampaign.objects.create(seller_account=seller, product=product, bid=bid, daily_budget=budget)
                AuditLog.objects.create(user=request.user, action=f"Ad started for '{product.name}' ({bid}/click, {budget}/day)")
                messages.success(request, f"'{product.name}' is now sponsored. You pay {money(bid)} per click, up to {money(budget)} a day, from your earnings.")
        else:
            campaign = get_object_or_404(AdCampaign, pk=request.POST.get("id"), seller_account=seller)
            if action == "update":
                bid = _money_value(request.POST.get("bid"), site.ads_min_bid, Decimal("100"))
                budget = _money_value(request.POST.get("daily_budget"), Decimal("1"), Decimal("10000"))
                if bid is None or budget is None or budget < bid:
                    messages.error(request, f"Use a cost per click of at least {money(site.ads_min_bid)} and a budget of at least one click.")
                else:
                    AdCampaign.objects.filter(pk=campaign.pk).update(bid=bid, daily_budget=budget)
                    messages.success(request, "Ad updated.")
            elif action == "pause" and campaign.status == "active":
                AdCampaign.objects.filter(pk=campaign.pk).update(status="paused")
                messages.success(request, "Ad paused.")
            elif action == "resume" and campaign.status == "paused":
                if campaign.paused_by_staff:
                    messages.error(request, "The store team paused this ad. Contact support.")
                else:
                    AdCampaign.objects.filter(pk=campaign.pk).update(status="active")
                    messages.success(request, "Ad running again.")
            elif action == "end":
                AdCampaign.objects.filter(pk=campaign.pk).update(status="ended")
                messages.success(request, "Ad ended.")
        from django.core.cache import cache
        cache.delete("ads:eligible")
        return redirect("seller_ads")
    campaigns = ads.totals(list(AdCampaign.objects.filter(seller_account=seller).select_related("product")))
    from . import seller_center as sc
    return seller_render(request, "ads.html", {
        "section": "ads", "campaigns": campaigns, "site": site,
        "products": Product.objects.live().filter(seller_account=seller).exclude(ad_campaigns__status__in=["active", "paused"]).order_by("name"),
        "available": sc.balance(seller)["available"], "owed": Decimal(str(seller.amount_owed)),
    })


def manage_ads(request):
    from django.db.models import Sum
    from . import ads
    from .manage_views import _log, _render
    from .models import AdCampaign, AdStat
    if request.method == "POST":
        campaign = get_object_or_404(AdCampaign, pk=request.POST.get("id"))
        action = request.POST.get("action")
        if action == "pause":
            AdCampaign.objects.filter(pk=campaign.pk).update(status="paused", paused_by_staff=True)
            _log(request, f"Paused ad #{campaign.pk}")
            messages.success(request, "Ad paused. The seller can't restart it.")
        elif action == "resume":
            AdCampaign.objects.filter(pk=campaign.pk).update(status="active", paused_by_staff=False)
            _log(request, f"Resumed ad #{campaign.pk}")
            messages.success(request, "Ad running again.")
        from django.core.cache import cache
        cache.delete("ads:eligible")
        return redirect("manage_ads")
    campaigns = ads.totals(list(AdCampaign.objects.select_related("product", "seller_account").exclude(status="ended")[:200]))
    since = timezone.localdate() - timedelta(days=29)
    income = AdStat.objects.filter(date__gte=since).aggregate(s=Sum("spend"), c=Sum("clicks"), i=Sum("impressions"), o=Sum("sales"))
    return _render(request, "ads.html", {"section": "ads", "campaigns": campaigns, "income": income})


def manage_promotions(request):
    """Timed sales and codes for the store's own products."""
    from .manage_views import _render
    if request.method == "POST":
        handle_post(request, None)
        return redirect("manage_promotions")
    promotions.tick()
    ctx = context(None)
    return _render(request, "promotions.html", {"section": "coupons", **ctx})
