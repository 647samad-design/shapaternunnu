"""Pages for live shows, videos, group buys, the prize wheel, blog, gift
registries and address suggestions."""
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from .ratelimit import ratelimit

logger = logging.getLogger(__name__)


def _add_to_cart_with_marker(request, product_id, marker, value):
    """Adds the product through the normal cart rules, then remembers which
    group / registry line this cart line belongs to."""
    from .cart import make_key
    from .models import Product
    from .views import add_to_cart
    before = dict(request.session.get("cart", {}) or {})
    response = add_to_cart(request, product_id)
    after = request.session.get("cart", {}) or {}
    product = Product.objects.get(pk=product_id)
    vid = request.POST.get("variant", "")
    key = make_key(product.pk, int(vid) if vid.isdigit() and product.has_variants else None)
    if after.get(key, 0) > before.get(key, 0) or (key in after and key not in before):
        marks = request.session.get(marker) or {}
        marks[key] = value
        request.session[marker] = marks
        request.session.modified = True
        return True, response
    return False, response


# ---------------------------------------------------------------- group buy

@login_required
@require_POST
def group_start(request, pk):
    from .models import Product
    product = get_object_or_404(Product.objects.live(), pk=pk, group_size__gte=2, group_percent__gte=1)
    ok, response = _add_to_cart_with_marker(request, product.pk, "group_for", "new")
    if ok:
        messages.info(request, f"Your group starts when you place the order. Then share the link: when {product.group_size} people "
                               f"have bought within 24 hours, everyone gets {product.group_percent}% back.")
        return redirect("cart")
    return response


@login_required
@require_POST
def group_join(request, code):
    from .models import GroupBuy
    group = get_object_or_404(GroupBuy.objects.select_related("product"), code=code)
    if not group.is_open or group.joined >= group.size:
        messages.error(request, "This group is already full or has ended. You can start a new one.")
        return redirect("product_detail", pk=group.product_id)
    if group.members.filter(user=request.user).exists():
        messages.info(request, "You're already in this group.")
        return redirect("group_page", code=code)
    ok, response = _add_to_cart_with_marker(request, group.product_id, "group_for", group.code)
    if ok:
        messages.success(request, "Added to your cart. You join the group when you place the order.")
        return redirect("cart")
    return response


def group_page(request, code):
    from .models import GroupBuy
    group = get_object_or_404(GroupBuy.objects.select_related("product", "leader"), code=code)
    return render(request, "bees/group.html", {"group": group, "product": group.product,
                                               "share_url": request.build_absolute_uri(reverse("group_page", args=[code])),
                                               "left": max(group.size - group.joined, 0)})


# ---------------------------------------------------------------- prize wheel

def spin_page(request):
    from .models import SiteSettings
    from .shopper import can_spin, prizes, spin
    site = SiteSettings.load()
    table = prizes(site.spin_prizes)
    if not site.spin_enabled or not table:
        raise Http404
    if request.method == "POST":
        if not request.user.is_authenticated:
            return JsonResponse({"ok": False, "error": "Sign in to spin."}, status=403)
        try:
            result, index = spin(request.user)
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        return JsonResponse({"ok": True, "index": index, "prize": result.prize,
                             "code": result.coupon.code if result.coupon else "", "points": result.points})
    return render(request, "bees/spin.html", {"prizes": table, "can_spin": request.user.is_authenticated and can_spin(request.user)})


# ---------------------------------------------------------------- videos & live

def videos(request):
    from .models import Product
    items = [p for p in Product.objects.listed().exclude(video_url="").select_related("seller_account").order_by("-updated_at")[:60]
             if p.video()]
    return render(request, "bees/videos.html", {"items": items})


def live_list(request):
    from .models import LiveShow
    now = timezone.now()
    shows = LiveShow.objects.select_related("seller_account")
    return render(request, "bees/live_list.html", {
        "live": shows.filter(started_at__isnull=False, ended_at=None),
        "upcoming": shows.filter(started_at=None, ended_at=None, starts_at__gte=now - timedelta(hours=2)).order_by("starts_at")[:20],
        "past": shows.filter(ended_at__isnull=False).exclude(stream_url="")[:12],
    })


def live_detail(request, pk):
    from .models import LiveReminder, LiveShow
    show = get_object_or_404(LiveShow.objects.select_related("seller_account"), pk=pk)
    reminded = request.user.is_authenticated and LiveReminder.objects.filter(show=show, user=request.user).exists()
    return render(request, "bees/live_detail.html", {
        "show": show, "products": show.products.live()[:20], "comments": list(show.comments.order_by("-id")[:50])[::-1],
        "reminded": reminded, "host": _is_host(request.user, show), "twitch_parent": request.get_host().split(":")[0],
    })


def _is_host(user, show):
    if not user.is_authenticated:
        return False
    if user.is_staff:
        return True
    from .views import get_seller_account_for_user
    seller, _ = get_seller_account_for_user(user)
    return bool(seller and show.seller_account_id == seller.id)


@ratelimit("live_chat", rate_limit=20, window_seconds=60)
def live_comments(request, pk):
    from .models import LiveComment, LiveShow
    show = get_object_or_404(LiveShow, pk=pk)
    if request.method == "POST":
        if not request.user.is_authenticated:
            return JsonResponse({"ok": False, "error": "Sign in to chat."}, status=403)
        if show.status != "live":
            return JsonResponse({"ok": False, "error": "The chat opens when the show is live."}, status=400)
        body = " ".join(request.POST.get("body", "").split())[:300]
        if not body:
            return JsonResponse({"ok": False, "error": "Write a message."}, status=400)
        user = request.user
        LiveComment.objects.create(show=show, user=user, name=(user.first_name or user.username)[:60], body=body,
                                   from_host=_is_host(user, show))
    after = request.GET.get("after", "0")
    after = int(after) if after.isdigit() else 0
    rows = show.comments.filter(id__gt=after).order_by("id")[:100]
    return JsonResponse({"status": show.status, "comments": [
        {"id": c.id, "name": c.name, "body": c.body, "host": c.from_host} for c in rows]})


@login_required
@require_POST
def live_remind(request, pk):
    from .models import LiveReminder, LiveShow
    show = get_object_or_404(LiveShow, pk=pk, started_at=None, ended_at=None)
    LiveReminder.objects.get_or_create(show=show, user=request.user)
    messages.success(request, "We'll let you know when the show starts.")
    return redirect("live_detail", pk=pk)


def _show_fields(request, seller):
    from .media import embed
    from .models import Product
    title = " ".join(request.POST.get("title", "").split())[:120]
    if not title:
        raise ValidationError("Give the show a title.")
    try:
        starts = timezone.make_aware(datetime.fromisoformat(request.POST.get("starts_at", "").strip()))
    except ValueError:
        raise ValidationError("Choose when the show starts.")
    url = request.POST.get("stream_url", "").strip()[:300]
    if url and not embed(url, live=True):
        raise ValidationError("Stream link: paste a YouTube (Live), Vimeo or Twitch link.")
    ids = [int(i) for i in request.POST.getlist("products") if i.isdigit()][:20]
    qs = Product.objects.filter(pk__in=ids)
    qs = qs.filter(seller_account=seller) if seller else qs.filter(seller_account__isnull=True)
    return {"title": title, "description": request.POST.get("description", "").strip()[:1000], "starts_at": starts,
            "stream_url": url}, list(qs)


def seller_live(request):
    """Seller Center > Live shows."""
    from .models import LiveShow, Product
    from .seller_views import _render
    seller = request.seller
    if request.method == "POST":
        action = request.POST.get("action", "save")
        if action == "save":
            try:
                fields, products = _show_fields(request, seller)
            except ValidationError as exc:
                messages.error(request, exc.messages[0])
                return redirect("seller_live")
            pk = request.POST.get("id", "")
            show = get_object_or_404(LiveShow, pk=pk, seller_account=seller) if pk.isdigit() else LiveShow(seller_account=seller)
            for k, v in fields.items():
                setattr(show, k, v)
            show.save()
            show.products.set(products)
            messages.success(request, "Show saved. Shoppers can sign up for a reminder on the Live page.")
        else:
            show = get_object_or_404(LiveShow, pk=request.POST.get("id"), seller_account=seller)
            _host_action(request, show, action)
        return redirect("seller_live")
    shows = LiveShow.objects.filter(seller_account=seller).prefetch_related("products")
    edit = shows.filter(pk=request.GET.get("edit")).first() if str(request.GET.get("edit", "")).isdigit() else None
    return _render(request, "live.html", {"section": "live", "shows": shows[:30], "edit": edit,
                                          "products": Product.objects.filter(seller_account=seller).order_by("name")[:300]})


def _host_action(request, show, action):
    from .shopper import go_live
    if action == "start":
        if not show.stream_url:
            messages.error(request, "Add the stream link first.")
            return
        go_live(show)
        messages.success(request, "You're live! Followers and reminder sign-ups were told.")
    elif action == "end" and show.started_at and not show.ended_at:
        show.ended_at = timezone.now()
        show.save(update_fields=["ended_at"])
        from django.core.cache import cache
        cache.delete("live_on")
        messages.success(request, "Show ended. It stays on the Live page as a replay.")
    elif action == "delete":
        show.delete()
        messages.success(request, "Show deleted.")


# ---------------------------------------------------------------- blog

def blog_list(request):
    from .models import BlogPost
    posts = BlogPost.objects.filter(is_published=True, published_at__lte=timezone.now())
    page = Paginator(posts, 12).get_page(request.GET.get("page"))
    return render(request, "bees/blog_list.html", {"posts": page, "page_obj": page})


HEADING = re.compile(r"^##\s+(.+)$")


def render_body(text):
    """Tiny, safe formatting: blank-line paragraphs, '## ' headings,
    '- ' lists and https links. Everything is escaped first."""
    out = []
    for block in re.split(r"\n\s*\n", (text or "").strip()):
        lines = [l.rstrip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        if len(lines) == 1 and HEADING.match(lines[0]):
            out.append(f"<h2>{escape(HEADING.match(lines[0]).group(1))}</h2>")
        elif all(l.lstrip().startswith("- ") for l in lines):
            out.append("<ul>" + "".join(f"<li>{_links(escape(l.lstrip()[2:]))}</li>" for l in lines) + "</ul>")
        else:
            out.append("<p>" + "<br>".join(_links(escape(l)) for l in lines) + "</p>")
    return "\n".join(out)


def _links(text):
    return re.sub(r"(https://[^\s<]+)", r'<a href="\1" rel="noopener" target="_blank">\1</a>', text)


def blog_detail(request, slug):
    from django.utils.safestring import mark_safe
    from .models import BlogPost
    post = get_object_or_404(BlogPost, slug=slug)
    if not post.is_published and not request.user.is_staff:
        raise Http404
    more = BlogPost.objects.filter(is_published=True).exclude(pk=post.pk)[:3]
    return render(request, "bees/blog_detail.html", {"post": post, "body": mark_safe(render_body(post.body)),
                                                     "products": post.products.live()[:8], "more": more})


def blog_rss(request):
    from .models import BlogPost, SiteSettings
    site = SiteSettings.load()
    items = []
    for p in BlogPost.objects.filter(is_published=True, published_at__lte=timezone.now())[:20]:
        link = request.build_absolute_uri(p.get_absolute_url())
        items.append(f"<item><title>{escape(p.title)}</title><link>{escape(link)}</link><guid>{escape(link)}</guid>"
                     f"<pubDate>{p.published_at.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
                     f"<description>{escape(p.excerpt or p.body[:300])}</description></item>")
    body = (f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{escape(site.site_name)} blog</title>'
            f"<link>{escape(request.build_absolute_uri('/blog/'))}</link><description>{escape(site.tagline)}</description>"
            + "".join(items) + "</channel></rss>")
    return HttpResponse(body, content_type="application/rss+xml; charset=utf-8")


def manage_blog(request):
    from .manage_views import _log, _render
    from .models import BlogPost, Product
    if request.method == "POST":
        action = request.POST.get("action", "save")
        pk = request.POST.get("id", "")
        post = get_object_or_404(BlogPost, pk=pk) if pk.isdigit() else BlogPost(author=request.user)
        if action == "delete" and post.pk:
            post.delete()
            _log(request, f"Deleted blog post {post.title}")
            messages.success(request, "Post deleted.")
            return redirect("manage_blog")
        title = " ".join(request.POST.get("title", "").split())[:160]
        body = request.POST.get("body", "").strip()[:50000]
        if not title or not body:
            messages.error(request, "A post needs a title and some text.")
            return redirect(f"{reverse('manage_blog')}?edit={pk or 'new'}")
        cover = request.POST.get("cover_url", "").strip()[:500]
        if cover and not cover.startswith(("https://", "/media/", "/static/")):
            messages.error(request, "Cover image: paste an https:// link.")
            return redirect(f"{reverse('manage_blog')}?edit={pk or 'new'}")
        post.title, post.body, post.cover_url = title, body, cover
        post.excerpt = " ".join(request.POST.get("excerpt", "").split())[:300]
        if not post.slug:
            base = slugify(title)[:150] or "post"
            slug, n = base, 2
            while BlogPost.objects.filter(slug=slug).exclude(pk=post.pk).exists():
                slug, n = f"{base}-{n}", n + 1
            post.slug = slug
        publish = bool(request.POST.get("is_published"))
        if publish and not post.published_at:
            post.published_at = timezone.now()
        post.is_published = publish
        post.save()
        ids = [int(i) for i in request.POST.getlist("products") if i.isdigit()][:12]
        post.products.set(Product.objects.filter(pk__in=ids))
        _log(request, f"Saved blog post {post.title}")
        messages.success(request, "Post published." if publish else "Draft saved.")
        return redirect("manage_blog")
    edit = request.GET.get("edit")
    post = None
    if edit == "new":
        post = BlogPost()
    elif edit and edit.isdigit():
        post = get_object_or_404(BlogPost, pk=edit)
    return _render(request, "blog.html", {"section": "blog", "posts": BlogPost.objects.all()[:100], "post": post,
                                          "all_products": Product.objects.live().order_by("name")[:400]})


# ---------------------------------------------------------------- gift registries

@login_required
def registries(request):
    from .models import GiftRegistry
    from .shopper import new_registry_token
    if request.method == "POST":
        title = " ".join(request.POST.get("title", "").split())[:100]
        if not title:
            messages.error(request, "Give your registry a name, e.g. 'Sara & Ali's wedding'.")
            return redirect("registries")
        if request.user.registries.count() >= 10:
            messages.error(request, "You can have up to 10 registries.")
            return redirect("registries")
        event = request.POST.get("event", "other")
        reg = GiftRegistry.objects.create(owner=request.user, title=title, token=new_registry_token(),
                                          event=event if event in dict(GiftRegistry.EVENTS) else "other",
                                          event_date=_date(request.POST.get("event_date")),
                                          message=request.POST.get("message", "").strip()[:600])
        messages.success(request, "Registry created. Add gifts from any product page with 'Add to registry'.")
        return redirect("registry_manage", pk=reg.pk)
    return render(request, "bees/account/registries.html", {"tab": "registries", "registries": request.user.registries.all(),
                                                            "events": GiftRegistry.EVENTS})


def _date(value):
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


@login_required
def registry_manage(request, pk):
    from .models import GiftRegistry
    reg = get_object_or_404(GiftRegistry, pk=pk, owner=request.user)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "delete":
            reg.delete()
            messages.success(request, "Registry deleted.")
            return redirect("registries")
        if action == "remove":
            reg.items.filter(pk=request.POST.get("item")).delete()
        elif action == "wanted":
            line = get_object_or_404(reg.items, pk=request.POST.get("item"))
            try:
                line.wanted = max(1, min(int(request.POST.get("wanted", "1")), 50))
            except ValueError:
                pass
            line.note = " ".join(request.POST.get("note", "").split())[:120]
            line.save(update_fields=["wanted", "note"])
        elif action == "details":
            reg.title = " ".join(request.POST.get("title", "").split())[:100] or reg.title
            reg.event_date = _date(request.POST.get("event_date"))
            reg.message = request.POST.get("message", "").strip()[:600]
            reg.save()
        messages.success(request, "Saved.")
        return redirect("registry_manage", pk=pk)
    return render(request, "bees/account/registry.html", {
        "tab": "registries", "reg": reg, "items": reg.items.select_related("product", "variant"),
        "share_url": request.build_absolute_uri(reverse("registry_public", args=[reg.token]))})


@login_required
@require_POST
def registry_add(request, pk):
    from .models import Product, ProductVariant, RegistryItem
    product = get_object_or_404(Product.objects.live(), pk=pk)
    reg = get_object_or_404(request.user.registries, pk=request.POST.get("registry") if str(request.POST.get("registry", "")).isdigit() else 0)
    variant = None
    if product.has_variants:
        vid = request.POST.get("variant", "")
        variant = ProductVariant.objects.filter(pk=vid, product=product).first() if vid.isdigit() else None
        if not variant:
            messages.error(request, "Choose a size/colour first.")
            return redirect("product_detail", pk=pk)
    line, created = RegistryItem.objects.get_or_create(registry=reg, product=product, variant=variant)
    if not created:
        line.wanted = min(line.wanted + 1, 50)
        line.save(update_fields=["wanted"])
    messages.success(request, f"Added to '{reg.title}'.")
    return redirect("product_detail", pk=pk)


def registry_public(request, token):
    from .models import GiftRegistry
    reg = get_object_or_404(GiftRegistry.objects.select_related("owner"), token=token)
    return render(request, "bees/registry_public.html", {"reg": reg, "items": reg.items.select_related("product", "variant")
                                                         .filter(product__approval_status="approved")})


@require_POST
def registry_buy(request, item_id):
    from .models import RegistryItem
    line = get_object_or_404(RegistryItem.objects.select_related("registry", "product"), pk=item_id)
    if line.remaining <= 0:
        messages.info(request, "That gift has already been bought.")
        return redirect("registry_public", token=line.registry.token)
    request.POST = request.POST.copy()
    if line.variant_id:
        request.POST["variant"] = str(line.variant_id)
    request.POST["quantity"] = "1"
    ok, response = _add_to_cart_with_marker(request, line.product_id, "registry_for", line.pk)
    if ok:
        messages.success(request, f"Added for {line.registry.title}. Ship it to yourself or to them at checkout.")
        return redirect("cart")
    return response


# ---------------------------------------------------------------- address suggestions

def _maps_key():
    from .multistore import env
    return env("GOOGLE_MAPS_API_KEY")


def address_enabled():
    return bool(_maps_key())


@ratelimit("addr", rate_limit=60, window_seconds=60, methods=("GET",))
def address_suggest(request):
    """Address suggestions from Google Places (New); the key stays on the server."""
    key, q = _maps_key(), request.GET.get("q", "").strip()[:120]
    if not key or len(q) < 3:
        return JsonResponse({"suggestions": []})
    body = {"input": q, "includedPrimaryTypes": ["street_address", "premise", "subpremise", "route"],
            "sessionToken": request.GET.get("session", "")[:64] or None, "languageCode": getattr(request, "shop_lang", "en")[:2]}
    country = request.GET.get("country", "").strip().upper()
    if re.fullmatch(r"[A-Z]{2}", country):
        body["includedRegionCodes"] = [country.lower()]
    req = urllib.request.Request("https://places.googleapis.com/v1/places:autocomplete", method="POST",
                                 data=json.dumps({k: v for k, v in body.items() if v}).encode(),
                                 headers={"Content-Type": "application/json", "X-Goog-Api-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        logger.exception("Address suggestions failed")
        return JsonResponse({"suggestions": []})
    out = []
    for s in data.get("suggestions", [])[:5]:
        pred = s.get("placePrediction") or {}
        if pred.get("placeId"):
            out.append({"id": pred["placeId"], "text": (pred.get("text") or {}).get("text", "")})
    return JsonResponse({"suggestions": out})


@ratelimit("addr", rate_limit=60, window_seconds=60, methods=("GET",))
def address_details(request):
    key, pid = _maps_key(), request.GET.get("id", "")
    if not key or not re.fullmatch(r"[A-Za-z0-9_-]{10,300}", pid):
        return JsonResponse({}, status=400)
    params = urllib.parse.urlencode({k: v for k, v in {"sessionToken": request.GET.get("session", "")[:64]}.items() if v})
    req = urllib.request.Request(f"https://places.googleapis.com/v1/places/{pid}" + (f"?{params}" if params else ""),
                                 headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": "addressComponents"})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        logger.exception("Address details failed")
        return JsonResponse({}, status=502)
    return JsonResponse(parse_components(data.get("addressComponents") or []))


def parse_components(parts):
    """Google address components -> our checkout fields."""
    got = {}
    for c in parts:
        for t in c.get("types", []):
            got.setdefault(t, c)
    long_ = lambda t: (got.get(t) or {}).get("longText", "")  # noqa: E731
    short = lambda t: (got.get(t) or {}).get("shortText", "")  # noqa: E731
    street = " ".join(x for x in (long_("street_number"), long_("route")) if x)
    if short("country") in ("DE", "NL", "BE", "AT", "CH", "ES", "IT", "PT", "PL", "SE", "DK", "NO", "FI", "FR") and long_("street_number"):
        street = f"{long_('route')} {long_('street_number')}".strip()
    if long_("subpremise"):
        street = f"{street}, {long_('subpremise')}"
    return {"address": street or long_("premise"), "city": long_("locality") or long_("postal_town") or long_("administrative_area_level_2"),
            "state": long_("administrative_area_level_1"), "postal_code": long_("postal_code"), "country": short("country")}
