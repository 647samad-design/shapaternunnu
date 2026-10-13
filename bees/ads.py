"""Sponsored products: sellers pay per click to appear at the top of search
results, category pages and the homepage.

* Clicks cost the seller's bid and are paid from their earnings (their
  available balance), never more than their daily budget, and only while
  they have enough earnings to cover the click.
* Bots, staff, the seller's own team and repeat clicks from the same visitor
  on the same day are free.
* Sales from a shopper who clicked an ad in the last 7 days are counted on the
  campaign so sellers see what their ads earn.
"""
import hashlib
import hmac
import time
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone

ATTRIBUTION_DAYS = 7


def _site():
    from .models import SiteSettings
    return SiteSettings.load()


def enabled():
    return _site().ads_enabled


def _today_stat(campaign_id, day=None):
    from .models import AdStat
    day = day or timezone.localdate()
    stat = AdStat.objects.filter(campaign_id=campaign_id, date=day).first()
    if stat is None:
        try:
            with transaction.atomic():
                stat = AdStat.objects.create(campaign_id=campaign_id, date=day)
        except IntegrityError:
            stat = AdStat.objects.get(campaign_id=campaign_id, date=day)
    return stat


def spent_today(campaign_id):
    from .models import AdStat
    return AdStat.objects.filter(campaign_id=campaign_id, date=timezone.localdate()).values_list("spend", flat=True).first() or Decimal("0")


def eligible(queryset=None):
    """Running campaigns whose product is live and in stock, with budget and
    earnings left for at least one more click today."""
    from .models import AdCampaign, AdStat
    qs = AdCampaign.objects.filter(status="active", product__approval_status="approved", product__stock__gt=0,
                                   seller_account__status="approved").select_related("product", "seller_account")
    if queryset is not None:
        qs = qs.filter(product__in=queryset)
    out = []
    today = timezone.localdate()
    spend = {r["campaign_id"]: r["spend"] for r in
             AdStat.objects.filter(date=today, campaign__in=qs).values("campaign_id", "spend")}
    for c in qs.order_by("-bid", "created_at")[:50]:
        if spend.get(c.id, Decimal("0")) + c.bid > c.daily_budget:
            continue
        if Decimal(str(c.seller_account.amount_owed)) < c.bid:
            continue
        out.append(c)
    return out


def pick(queryset=None, limit=None, exclude_product_ids=()):
    """Up to ``limit`` sponsored products for a page (highest bid first, one
    per product). Counts an impression for each."""
    if not enabled():
        return []
    limit = _site().ads_slots if limit is None else limit
    if not limit:
        return []
    chosen, seen = [], set(exclude_product_ids)
    for c in eligible(queryset):
        if c.product_id in seen:
            continue
        seen.add(c.product_id)
        chosen.append(c)
        if len(chosen) >= limit:
            break
    if chosen:
        from .background import after_response
        after_response(_count_impressions, [c.id for c in chosen])
    for c in chosen:
        c.product.ad_url = click_url(c)
    return chosen


def _count_impressions(ids):
    from .models import AdStat
    for cid in ids:
        _today_stat(cid)
        AdStat.objects.filter(campaign_id=cid, date=timezone.localdate()).update(impressions=F("impressions") + 1)


def _sign(cid):
    return hmac.new(settings.SECRET_KEY.encode(), f"ad:{cid}".encode(), hashlib.sha256).hexdigest()[:16]


def click_url(campaign):
    return reverse("ad_click", args=[campaign.id]) + f"?s={_sign(campaign.id)}"


def _free_click(request, campaign):
    from .analytics import is_human
    if not is_human(request):
        return True
    user = request.user
    if user.is_authenticated:
        if user.is_staff:
            return True
        from .views import get_seller_account_for_user
        seller, _role = get_seller_account_for_user(user)
        if seller and seller.pk == campaign.seller_account_id:
            return True
    day = timezone.localdate().isoformat()
    seen = request.session.get("ad_seen", {})
    if seen.get(str(campaign.id)) == day:
        return True
    seen = {k: v for k, v in seen.items() if v == day}
    seen[str(campaign.id)] = day
    request.session["ad_seen"] = seen
    return False


def charge_click(campaign):
    """Charges one click if budget and earnings allow. Returns True if charged."""
    from .models import AdCampaign, SellerAccount
    with transaction.atomic():
        seller = SellerAccount.objects.select_for_update().get(pk=campaign.seller_account_id)
        campaign = AdCampaign.objects.select_for_update().get(pk=campaign.pk)
        if campaign.status != "active":
            return False
        stat = _today_stat(campaign.id)
        stat = type(stat).objects.select_for_update().get(pk=stat.pk)
        if stat.spend + campaign.bid > campaign.daily_budget or Decimal(str(seller.amount_owed)) < campaign.bid:
            stat.clicks += 1
            stat.save(update_fields=["clicks"])
            return False
        stat.clicks += 1
        stat.spend += campaign.bid
        stat.save(update_fields=["clicks", "spend"])
        AdCampaign.objects.filter(pk=campaign.pk).update(spent=F("spent") + campaign.bid)
        SellerAccount.objects.filter(pk=seller.pk).update(ad_spend=F("ad_spend") + campaign.bid)
    return True


def click(request, campaign_id):
    """/ad/<id>/click/ - counts and charges the click, then opens the product."""
    from .models import AdCampaign
    campaign = get_object_or_404(AdCampaign.objects.select_related("product"), pk=campaign_id)
    if not hmac.compare_digest(request.GET.get("s", ""), _sign(campaign.id)):
        raise Http404
    if not _free_click(request, campaign):
        charge_click(campaign)
        clicks = request.session.get("ad_clicks", {})
        clicks[str(campaign.product_id)] = [campaign.id, int(time.time())]
        request.session["ad_clicks"] = dict(list(clicks.items())[-20:])
    return redirect("product_detail", pk=campaign.product_id)


def attribute_order(request, order):
    """Counts this order's lines on the ads the shopper clicked (7 days)."""
    from .models import AdStat
    clicks = request.session.get("ad_clicks") or {}
    if not clicks:
        return
    cutoff = time.time() - ATTRIBUTION_DAYS * 86400
    for item in order.items.all():
        hit = clicks.get(str(item.product_id))
        if not hit or hit[1] < cutoff:
            continue
        _today_stat(hit[0])
        AdStat.objects.filter(campaign_id=hit[0], date=timezone.localdate()).update(
            orders=F("orders") + 1, sales=F("sales") + item.price * item.quantity)


def totals(campaigns):
    """Adds impressions/clicks/spend/orders/sales (all time + today) to each campaign."""
    from .models import AdStat
    ids = [c.id for c in campaigns]
    agg = {r["campaign_id"]: r for r in AdStat.objects.filter(campaign_id__in=ids).values("campaign_id").annotate(
        impressions=Sum("impressions"), clicks=Sum("clicks"), spend=Sum("spend"), orders=Sum("orders"), sales=Sum("sales"))}
    today = {r["campaign_id"]: r["spend"] for r in AdStat.objects.filter(campaign_id__in=ids, date=timezone.localdate()).values("campaign_id", "spend")}
    for c in campaigns:
        r = agg.get(c.id) or {}
        c.impressions, c.clicks = r.get("impressions") or 0, r.get("clicks") or 0
        c.spend_total, c.orders, c.sales = r.get("spend") or Decimal("0"), r.get("orders") or 0, r.get("sales") or Decimal("0")
        c.ctr = round(c.clicks * 100 / c.impressions, 1) if c.impressions else 0
        c.roas = round(float(c.sales) / float(c.spend_total), 1) if c.spend_total else None
        c.today = today.get(c.id) or Decimal("0")
    return campaigns
