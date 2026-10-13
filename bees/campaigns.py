"""Email marketing: campaigns to newsletter subscribers.

Only people who opted in (newsletter box, Privacy page) get campaigns, every
email has a one-click unsubscribe link (and a List-Unsubscribe header), and
emails go out in batches after the page has loaded so big lists never slow
the site. Opens and clicks are counted anonymously per campaign.
"""
import hashlib
import hmac
import logging
from datetime import timedelta
from urllib.parse import urlencode, urlparse

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.core.mail import EmailMultiAlternatives
from django.db.models import F
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.html import strip_tags
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)
BATCH = 200
PIXEL = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,"
         b"\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")


def _sig(*parts):
    return hmac.new(settings.SECRET_KEY.encode(), ":".join(str(p) for p in parts).encode(), hashlib.sha256).hexdigest()[:20]


def _absolute(path):
    from .order_emails import absolute
    return absolute(path)


def unsubscribe_url(email):
    return _absolute(reverse("unsubscribe") + "?" + urlencode({"e": email, "t": _sig("unsub", email.lower())}))


def audience_emails(audience):
    from .models import NewsletterSubscriber, Order
    subs = set(e.lower() for e in NewsletterSubscriber.objects.values_list("email", flat=True))
    if audience == "all":
        return sorted(subs)
    counted = Order.objects.exclude(status="cancelled").exclude(payment_status__in=["pending", "failed"])
    buyers = set(e.lower() for e in counted.exclude(email="").values_list("email", flat=True))
    if audience == "buyers":
        return sorted(subs & buyers)
    if audience == "new":
        return sorted(subs - buyers)
    if audience == "lapsed":
        recent = set(e.lower() for e in counted.filter(created_at__gte=timezone.now() - timedelta(days=90)).values_list("email", flat=True))
        return sorted((subs & buyers) - recent)
    return []


def _context(campaign, email, preview=False):
    from .models import Product
    from .views import with_ratings
    paragraphs = [p.strip() for p in campaign.body.replace("\r", "").split("\n\n") if p.strip()]
    ids = [int(i) for i in campaign.product_ids if str(i).isdigit()][:4]
    products = list(with_ratings(Product.objects.live().filter(id__in=ids))) if ids else []
    track = lambda url: _absolute(reverse("campaign_click", args=[campaign.id]) + "?" + urlencode({"u": url, "t": _sig("c", campaign.id, url)}))
    for p in products:
        p.track_url = track(_absolute(reverse("product_detail", args=[p.id])))
    button = ""
    if campaign.button_text and campaign.button_url:
        button = track(campaign.button_url if campaign.button_url.startswith("http") else _absolute(campaign.button_url))
    return {
        "campaign": campaign, "paragraphs": paragraphs, "products": products, "button_url": button,
        "shop_url": track(_absolute("/")), "unsubscribe_url": unsubscribe_url(email) if email else "",
        "pixel": "" if preview else _absolute(reverse("campaign_open", args=[campaign.id])) + f"?t={_sig('o', campaign.id)}",
    }


def render_email(campaign, email, preview=False):
    from .email_i18n import language_for, localize
    from .models import SiteSettings
    html = render_to_string("bees/emails/campaign.html", {"brand": SiteSettings.load(), **_context(campaign, email, preview)})
    html, subject = localize(html, campaign.subject, language_for(email=email))
    return subject, html


def send_one(campaign, email):
    subject, html = render_email(campaign, email)
    msg = EmailMultiAlternatives(subject, strip_tags(html), None, [email],
                                 headers={"List-Unsubscribe": f"<{unsubscribe_url(email)}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
    msg.attach_alternative(html, "text/html")
    msg.send()


def start(campaign):
    """Freezes the recipient list and begins sending."""
    from .models import EmailCampaign
    emails = audience_emails(campaign.audience)
    EmailCampaign.objects.filter(pk=campaign.pk, status="draft").update(
        status="sending", recipients=emails, started_at=timezone.now(), sent_count=0, failed_count=0)
    campaign.refresh_from_db()
    from .background import after_response
    after_response(send_batch, campaign.pk)
    return len(emails)


def send_batch(campaign_id, size=BATCH):
    """Sends the next batch. Safe to run from several places at once."""
    from .models import EmailCampaign
    if not cache.add(f"campaign-lock:{campaign_id}", 1, 600):
        return 0
    try:
        campaign = EmailCampaign.objects.filter(pk=campaign_id, status="sending").first()
        if not campaign:
            return 0
        done = campaign.sent_count + campaign.failed_count
        batch = campaign.recipients[done:done + size]
        sent = failed = 0
        for email in batch:
            try:
                send_one(campaign, email)
                sent += 1
            except Exception as exc:  # one bad address shouldn't stop the rest
                failed += 1
                logger.warning("Campaign %s to %s failed: %s", campaign.id, email[:3], exc)
        EmailCampaign.objects.filter(pk=campaign.pk).update(sent_count=F("sent_count") + sent, failed_count=F("failed_count") + failed)
        if done + len(batch) >= len(campaign.recipients):
            EmailCampaign.objects.filter(pk=campaign.pk).update(status="sent", finished_at=timezone.now())
        return sent
    finally:
        cache.delete(f"campaign-lock:{campaign_id}")


def continue_sending():
    from .models import EmailCampaign
    for cid in EmailCampaign.objects.filter(status="sending").values_list("id", flat=True):
        send_batch(cid)


# ---------------------------------------------------------------------------
# Public endpoints
# ---------------------------------------------------------------------------

@csrf_exempt  # mail apps post here for one-click unsubscribe; protected by the signed link
def unsubscribe(request):
    from .models import NewsletterSubscriber
    email = request.GET.get("e", "") or request.POST.get("e", "")
    token = request.GET.get("t", "") or request.POST.get("t", "")
    if not email or not hmac.compare_digest(token, _sig("unsub", email.lower())):
        raise Http404
    if request.method == "POST":  # one-click (mail apps) or the button
        NewsletterSubscriber.objects.filter(email__iexact=email).delete()
        if "List-Unsubscribe" in request.POST:
            return HttpResponse("ok")
        return render(request, "bees/unsubscribed.html", {"done": True, "email": email})
    return render(request, "bees/unsubscribed.html", {"done": False, "email": email, "token": token})


def track_open(request, campaign_id):
    from .models import EmailCampaign
    if hmac.compare_digest(request.GET.get("t", ""), _sig("o", campaign_id)):
        EmailCampaign.objects.filter(pk=campaign_id).update(opens=F("opens") + 1)
    resp = HttpResponse(PIXEL, content_type="image/gif")
    resp["Cache-Control"] = "no-store"
    return resp


def track_click(request, campaign_id):
    from .models import EmailCampaign
    url = request.GET.get("u", "")
    if not url or not hmac.compare_digest(request.GET.get("t", ""), _sig("c", campaign_id, url)):
        raise Http404
    if urlparse(url).scheme not in ("http", "https"):
        raise Http404
    EmailCampaign.objects.filter(pk=campaign_id).update(clicks=F("clicks") + 1)
    return redirect(url)


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

def admin_page(request, pk=None):
    from .manage_views import _log, _render
    from .models import EmailCampaign, NewsletterSubscriber, Product
    campaign = get_object_or_404(EmailCampaign, pk=pk) if pk else None
    if request.method == "POST":
        action = request.POST.get("action")
        if action in ("save", "test", "send"):
            if campaign and campaign.status != "draft":
                messages.error(request, "This campaign has already been sent.")
                return redirect("manage_campaign", pk=campaign.pk)
            fields = {k: request.POST.get(k, "").strip() for k in ("subject", "heading", "body", "button_text", "button_url", "coupon_code")}
            audience = request.POST.get("audience", "all")
            ids = [i for i in request.POST.get("product_ids", "").replace(" ", "").split(",") if i.isdigit()][:4]
            if not fields["subject"] or not fields["heading"] or not fields["body"]:
                messages.error(request, "Add a subject, a heading and the message.")
                return render_form(request, campaign, fields, audience, ids)
            if fields["button_url"] and not (fields["button_url"].startswith(("https://", "http://", "/"))):
                messages.error(request, "The button link must start with https:// or /.")
                return render_form(request, campaign, fields, audience, ids)
            data = {"subject": fields["subject"][:150], "heading": fields["heading"][:150], "body": fields["body"][:10000],
                    "button_text": fields["button_text"][:40], "button_url": fields["button_url"][:300], "coupon_code": fields["coupon_code"][:30].upper(),
                    "audience": audience if audience in dict(EmailCampaign.AUDIENCES) else "all", "product_ids": ids}
            if campaign:
                EmailCampaign.objects.filter(pk=campaign.pk).update(**data)
                campaign.refresh_from_db()
            else:
                campaign = EmailCampaign.objects.create(created_by=request.user, **data)
            if action == "test":
                try:
                    send_one(campaign, request.user.email)
                    messages.success(request, f"Test sent to {request.user.email}.")
                except Exception as exc:
                    messages.error(request, f"Couldn't send the test: {exc}")
            elif action == "send":
                n = start(campaign)
                _log(request, f"Sent campaign '{campaign.subject}' to {n} subscribers")
                messages.success(request, f"Sending to {n} subscriber{'s' if n != 1 else ''}. Big lists go out in batches over the next minutes.")
            else:
                messages.success(request, "Draft saved.")
            return redirect("manage_campaign", pk=campaign.pk)
        if action == "delete" and campaign and campaign.status == "draft":
            campaign.delete()
            messages.success(request, "Draft deleted.")
            return redirect("manage_campaigns")
    if campaign is None and request.GET.get("new") is None:
        counts = {a: len(audience_emails(a)) for a, _l in EmailCampaign.AUDIENCES}
        return _render(request, "campaigns.html", {"section": "campaigns", "campaigns": EmailCampaign.objects.all()[:100],
                                                   "subscribers": NewsletterSubscriber.objects.count(), "counts": counts})
    if campaign:
        fields = {k: getattr(campaign, k) for k in ("subject", "heading", "body", "button_text", "button_url", "coupon_code")}
        return render_form(request, campaign, fields, campaign.audience, campaign.product_ids)
    return render_form(request, None, {"button_text": "Shop now", "button_url": "/"}, "all", [])


def render_form(request, campaign, fields, audience, ids):
    from .manage_views import _render
    from .models import EmailCampaign
    preview = ""
    if campaign:
        preview = render_email(campaign, request.user.email or "preview@example.com", preview=True)[1]
    counts = {a: len(audience_emails(a)) for a, _l in EmailCampaign.AUDIENCES}
    return _render(request, "campaign_form.html", {"section": "campaigns", "campaign": campaign, "f": fields, "audience": audience,
                                                   "ids": ",".join(str(i) for i in ids), "audiences": EmailCampaign.AUDIENCES,
                                                   "counts": counts, "preview": preview})
