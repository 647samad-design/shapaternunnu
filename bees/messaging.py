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
from django.db.models import F, Q, Sum
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


def mark_read(conv, side):
    field = f"{side}_unread"
    if getattr(conv, field):
        Conversation.objects.filter(pk=conv.pk).update(**{field: 0})
        setattr(conv, field, 0)


def send(conv, user, side, body, product=None, request=None):
    """Saves a message and lets the other side know."""
    body = (body or "").strip()[:MAX_LENGTH]
    if not body:
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
        msg = Message.objects.create(conversation=conv, sender=user, from_seller=from_seller, body=body, product=product)
        Conversation.objects.filter(pk=conv.pk).update(
            last_message_at=now, **{f"{other}_unread": F(f"{other}_unread") + 1})
    conv.refresh_from_db()
    _notify(conv, other, msg, request)
    return msg


def _notify(conv, other, msg, request):
    from .views import _send_html_email, _store_name
    if other == "seller":
        recipient = conv.seller.user
        link = reverse("seller_conversation", args=[conv.id])
        sender_name = conv.buyer.first_name or conv.buyer.username
    else:
        recipient = conv.buyer
        link = reverse("conversation", args=[conv.id])
        sender_name = conv.seller.display_name
    # One notification per batch of unread messages, not one per message.
    if getattr(conv, f"{other}_unread") == 1:
        Notification.objects.create(user=recipient, link=link, message=f"New message from {sender_name}"[:255])
    emailed_field = f"{other}_emailed_at"
    last = getattr(conv, emailed_field)
    if recipient.email and (last is None or timezone.now() - last > EMAIL_GAP):
        url = request.build_absolute_uri(link) if request else link
        try:
            _send_html_email(f"New message from {sender_name} on {_store_name()}", "bees/emails/new_message.html",
                             {"sender_name": sender_name, "body": msg.body, "url": url, "product": msg.product}, recipient.email)
        except Exception:
            pass
        Conversation.objects.filter(pk=conv.pk).update(**{emailed_field: timezone.now()})


def serialize(msgs, side):
    out = []
    for m in msgs:
        mine = (m.from_seller and side == "seller") or (not m.from_seller and side == "buyer")
        out.append({
            "id": m.id, "body": m.body, "mine": mine,
            "time": timezone.localtime(m.created_at).strftime("%b %d, %H:%M"),
            "product": {"name": m.product.name, "url": reverse("product_detail", args=[m.product_id])} if m.product_id and m.product else None,
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
    convs = Conversation.objects.filter(buyer=request.user).select_related("seller", "product").exclude(last_message_at=None)
    return render(request, "bees/messages/inbox.html", {"conversations": convs, "conv": None})


@login_required
def conversation(request, pk):
    conv = get_object_or_404(Conversation.objects.select_related("seller", "buyer"), pk=pk, buyer=request.user)
    mark_read(conv, "buyer")
    about = None
    if request.GET.get("about", "").isdigit():
        about = Product.objects.filter(pk=request.GET["about"], seller_account=conv.seller).first()
    convs = Conversation.objects.filter(buyer=request.user).select_related("seller").filter(
        Q(pk=conv.pk) | ~Q(last_message_at=None))
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
        msg = send(conv, request.user, side, request.POST.get("body", ""), product, request)
    except ValueError as exc:
        if request.headers.get("x-requested-with"):
            return JsonResponse({"error": str(exc)}, status=400)
        flash.error(request, str(exc))
        return redirect(request.POST.get("next") or "messages_inbox")
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
    new = list(conv.messages.filter(id__gt=after).select_related("product")[:50])
    if new:
        mark_read(conv, side)
    return JsonResponse({"messages": serialize(new, side)})


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
