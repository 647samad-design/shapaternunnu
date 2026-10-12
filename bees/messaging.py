"""Buyer <-> seller messages.

A shopper can message any approved seller from a product or store page.
Each shopper/seller pair has one conversation; the seller's whole team can
answer it from Seller Center > Messages. New messages show up live (the
page checks every few seconds), as a notification, and by email when the
other side hasn't been emailed in the last 30 minutes.
"""
from datetime import timedelta

from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import transaction
from django.db.models import F, OuterRef, Q, Subquery, Sum
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.timesince import timesince
from django.views.decorators.http import require_POST

from .models import AuditLog, Conversation, Message, Notification, Product, SellerAccount

MAX_LENGTH = 2000
SEND_LIMIT = 30          # messages per user ...
SEND_WINDOW = 10 * 60    # ... per 10 minutes
EMAIL_GAP = timedelta(minutes=30)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def seller_for(user):
    from .views import get_seller_account_for_user
    seller, _role = get_seller_account_for_user(user)
    return seller


def side_for(user, conv):
    """'buyer', 'seller' or None for this user in this conversation."""
    if conv.buyer_id == user.id:
        return "buyer"
    seller = seller_for(user)
    if seller and seller.id == conv.seller_id:
        return "seller"
    return None


def unread_for_buyer(user):
    return Conversation.objects.filter(buyer=user).aggregate(n=Sum("buyer_unread"))["n"] or 0


def unread_for_seller(seller):
    return Conversation.objects.filter(seller=seller).aggregate(n=Sum("seller_unread"))["n"] or 0


def with_preview(qs):
    """Adds the latest message text to each conversation for the list."""
    latest = Message.objects.filter(conversation=OuterRef("pk")).order_by("-id")
    return qs.annotate(preview=Subquery(latest.values("body")[:1]),
                       preview_from_seller=Subquery(latest.values("from_seller")[:1]))


def mark_read(conv, side):
    field = f"{side}_unread"
    if getattr(conv, field):
        Conversation.objects.filter(pk=conv.pk).update(**{field: 0})
        setattr(conv, field, 0)


def clean_photo(uploaded):
    """Checks a chat photo and re-saves it without hidden data (GPS etc.),
    at most 1600 px wide. Raises ValueError with a friendly message."""
    from django.core.exceptions import ValidationError
    from django.core.files.base import ContentFile
    from PIL import Image, ImageOps
    from .security import validate_image_upload
    try:
        validate_image_upload(uploaded)
    except ValidationError as exc:
        raise ValueError(exc.messages[0])
    if uploaded.size > 8 * 1024 * 1024:
        raise ValueError("Photos must be 8 MB or smaller.")
    import io
    uploaded.seek(0)
    with Image.open(uploaded) as img:
        if getattr(img, "is_animated", False):
            uploaded.seek(0)
            return ContentFile(uploaded.read(), name="photo.gif")
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1])
            img = background
        img.thumbnail((1600, 1600))
        out = io.BytesIO()
        img.convert("RGB").save(out, "JPEG", quality=85, optimize=True)
    return ContentFile(out.getvalue(), name="photo.jpg")


def send(conv, user, side, body, product=None, request=None, photo=None):
    """Saves a message (text and/or a photo) and lets the other side know."""
    body = (body or "").strip()[:MAX_LENGTH]
    if photo is not None:
        photo = clean_photo(photo)
    if not body and photo is None:
        raise ValueError("Write a message first.")
    key = f"msg-rate:{user.pk}"
    used = cache.get(key, 0)
    if used >= SEND_LIMIT:
        raise ValueError("You're sending messages very quickly. Please wait a few minutes.")
    cache.set(key, used + 1, SEND_WINDOW)
    from_seller = side == "seller"
    other = "buyer" if from_seller else "seller"
    now = timezone.now()
    with transaction.atomic():
        msg = Message(conversation=conv, sender=user, from_seller=from_seller, body=body, product=product)
        if photo is not None:
            msg.image.save(photo.name, photo, save=False)
        msg.save()
        Conversation.objects.filter(pk=conv.pk).update(
            last_message_at=now, **{f"{other}_unread": F(f"{other}_unread") + 1})
    conv.refresh_from_db()
    from . import live
    live.clear_typing(conv.pk, side)
    live.touch(user)
    _notify(conv, other, msg, request)
    return msg


def _notify(conv, other, msg, request):
    """In-site alert for everyone on the receiving side (the seller's whole
    team), plus an email to the main contact at most every 30 minutes."""
    from . import live
    from .seller_center import _seller_users
    from .views import _send_html_email, _store_name
    if other == "seller":
        recipients = _seller_users(conv.seller)
        email_to = conv.seller.user
        link = reverse("seller_conversation", args=[conv.id])
        sender_name = conv.buyer.first_name or conv.buyer.username
    else:
        recipients = [conv.buyer]
        email_to = conv.buyer
        link = reverse("conversation", args=[conv.id])
        sender_name = conv.seller.display_name
    if any(live.is_viewing(u.pk, conv.pk) for u in recipients):
        return  # someone on that side has the chat open and sees it straight away
    preview = msg.body[:80] if msg.body else "📷 Photo"
    # One alert per batch of unread messages, not one per message.
    if getattr(conv, f"{other}_unread") == 1:
        Notification.objects.bulk_create([
            Notification(user=u, link=link, message=f"New message from {sender_name}: {preview}"[:255]) for u in recipients
        ])
    emailed_field = f"{other}_emailed_at"
    last = getattr(conv, emailed_field)
    if email_to.email and (last is None or timezone.now() - last > EMAIL_GAP):
        url = request.build_absolute_uri(link) if request else link
        _send_html_email(f"New message from {sender_name} on {_store_name()}", "bees/emails/new_message.html",
                         {"sender_name": sender_name, "body": msg.body or "📷 Photo", "url": url, "product": msg.product}, email_to.email)
        Conversation.objects.filter(pk=conv.pk).update(**{emailed_field: timezone.now()})


def serialize(msgs, side):
    out = []
    for m in msgs:
        mine = (m.from_seller and side == "seller") or (not m.from_seller and side == "buyer")
        out.append({
            "id": m.id, "body": m.body, "mine": mine,
            "time": timezone.localtime(m.created_at).strftime("%b %d, %H:%M"),
            "product": {"name": m.product.name, "url": reverse("product_detail", args=[m.product_id])} if m.product_id and m.product else None,
            "image": reverse("message_image", args=[m.id]) if m.image else None,
        })
    return out


# ---------------------------------------------------------------------------
# Shopper pages
# ---------------------------------------------------------------------------

def _start(request, seller, product=None):
    if not seller or seller.status != "approved":
        raise Http404("This seller isn't available.")
    mine = seller_for(request.user)
    if mine and mine.id == seller.id:
        flash.info(request, "That's your own store. Your customers' messages are in Seller Center > Messages.")
        return redirect("seller_messages")
    conv, _ = Conversation.objects.get_or_create(buyer=request.user, seller=seller)
    url = reverse("conversation", args=[conv.id])
    return redirect(f"{url}?about={product.id}" if product else url)


@login_required
def message_about_product(request, pk):
    product = get_object_or_404(Product, pk=pk)
    return _start(request, product.seller_account, product)


@login_required
def message_store(request, seller_id):
    return _start(request, get_object_or_404(SellerAccount, pk=seller_id))


@login_required
def inbox(request):
    convs = with_preview(Conversation.objects.filter(buyer=request.user).select_related("seller", "product").exclude(last_message_at=None))
    return render(request, "bees/messages/inbox.html", {"conversations": convs, "conv": None})


@login_required
def conversation(request, pk):
    conv = get_object_or_404(Conversation.objects.select_related("seller", "buyer"), pk=pk, buyer=request.user)
    mark_read(conv, "buyer")
    about = None
    if request.GET.get("about", "").isdigit():
        about = Product.objects.filter(pk=request.GET["about"], seller_account=conv.seller).first()
    convs = with_preview(Conversation.objects.filter(buyer=request.user).select_related("seller").filter(
        Q(pk=conv.pk) | ~Q(last_message_at=None)))
    return render(request, "bees/messages/inbox.html", {
        "conversations": convs, "conv": conv, "about": about,
        "thread": serialize(conv.messages.select_related("product"), "buyer"),
    })


# ---------------------------------------------------------------------------
# Shared JSON endpoints (both sides)
# ---------------------------------------------------------------------------

@login_required
@require_POST
def send_view(request, pk):
    conv = get_object_or_404(Conversation.objects.select_related("seller__user", "buyer"), pk=pk)
    side = side_for(request.user, conv)
    if not side:
        raise Http404
    product = None
    about = request.POST.get("about", "")
    if about.isdigit():
        product = Product.objects.filter(pk=about, seller_account=conv.seller).first()
    try:
        msg = send(conv, request.user, side, request.POST.get("body", ""), product, request, request.FILES.get("photo"))
    except ValueError as exc:
        if request.headers.get("x-requested-with"):
            return JsonResponse({"error": str(exc)}, status=400)
        flash.error(request, str(exc))
        from .security import safe_next_url
        return redirect(safe_next_url(request, request.POST.get("next"), reverse("messages_inbox")))
    if request.headers.get("x-requested-with"):
        return JsonResponse({"message": serialize([msg], side)[0]})
    return redirect("seller_conversation" if side == "seller" else "conversation", pk=conv.pk)


@login_required
def poll_view(request, pk):
    conv = get_object_or_404(Conversation, pk=pk)
    side = side_for(request.user, conv)
    if not side:
        raise Http404
    after = request.GET.get("after", "0")
    after = int(after) if after.isdigit() else 0
    from . import live
    live.touch(request.user)
    if request.GET.get("visible") != "0":
        live.set_viewing(request.user.pk, conv.pk)
    new = list(conv.messages.filter(id__gt=after).select_related("product")[:50])
    if new and request.GET.get("visible") != "0":
        mark_read(conv, side)
    other = "buyer" if side == "seller" else "seller"
    conv.refresh_from_db(fields=["buyer_unread", "seller_unread"])
    status, ago = live.presence(conv.buyer_id if other == "buyer" else conv.seller.user_id)
    response = JsonResponse({
        "messages": serialize(new, side),
        "typing": live.is_typing(conv.pk, other),
        "seen": getattr(conv, f"{other}_unread") == 0,
        "presence": status, "ago": ago,
    })
    response["Cache-Control"] = "no-store"
    return response


@login_required
def message_image(request, pk):
    """A chat photo, for the two people in the conversation (and staff)."""
    msg = get_object_or_404(Message.objects.select_related("conversation"), pk=pk)
    if not msg.image or not (request.user.is_staff or side_for(request.user, msg.conversation)):
        raise Http404
    from django.conf import settings
    if getattr(settings, "USE_SUPABASE_STORAGE", False):
        return redirect(msg.image.url)  # short-lived signed link
    from django.http import FileResponse
    response = FileResponse(msg.image.open("rb"), content_type="image/gif" if msg.image.name.endswith(".gif") else "image/jpeg")
    response["Cache-Control"] = "private, max-age=86400"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@require_POST
def quick_replies_view(request):
    """Seller saves their reply shortcuts (one per line)."""
    seller = seller_for(request.user)
    if not seller:
        raise Http404
    lines = [line.strip()[:300] for line in request.POST.get("quick_replies", "").splitlines() if line.strip()][:12]
    SellerAccount.objects.filter(pk=seller.pk).update(quick_replies="\n".join(lines))
    flash.success(request, "Saved replies updated.")
    from .security import safe_next_url
    return redirect(safe_next_url(request, request.POST.get("next"), reverse("seller_messages")))


@login_required
@require_POST
def typing_view(request, pk):
    conv = get_object_or_404(Conversation, pk=pk)
    side = side_for(request.user, conv)
    if not side:
        raise Http404
    from . import live
    live.set_typing(conv.pk, side)
    return JsonResponse({"ok": True})


@login_required
@require_POST
def report_view(request, pk):
    conv = get_object_or_404(Conversation, pk=pk)
    side = side_for(request.user, conv)
    if not side:
        raise Http404
    Conversation.objects.filter(pk=pk).update(reported=True)
    AuditLog.objects.create(user=request.user, action=f"Reported conversation #{pk} ({side})")
    from .manage_views import _refresh_attention
    _refresh_attention()
    flash.success(request, "Thanks. The store team will review this conversation.")
    return redirect("seller_conversation" if side == "seller" else "conversation", pk=pk)


def since(dt):
    return timesince(dt).split(",")[0] if dt else ""


def reply_time(seller):
    """Typical time the seller takes to answer a shopper, as text
    ("within an hour", "within a day"...), or "" without enough data.
    Cached for an hour."""
    key = f"seller-reply-time:{seller.pk}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    since = timezone.now() - timedelta(days=90)
    gaps = []
    for conv in Conversation.objects.filter(seller=seller, last_message_at__gte=since)[:50]:
        waiting = None
        for m in conv.messages.filter(created_at__gte=since).only("from_seller", "created_at"):
            if not m.from_seller and waiting is None:
                waiting = m.created_at
            elif m.from_seller and waiting is not None:
                gaps.append((m.created_at - waiting).total_seconds())
                waiting = None
    text = ""
    if len(gaps) >= 2:
        gaps.sort()
        median = gaps[len(gaps) // 2]
        text = ("within an hour" if median <= 3600 else "within a few hours" if median <= 6 * 3600
                else "within a day" if median <= 86400 else "within a few days")
    cache.set(key, text, 3600)
    return text
