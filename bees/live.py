"""Live updates without refreshing: unread counts, new alerts, who is online
and who is typing. Pages ask every few seconds (works on any host, no
websockets needed)."""
from django.contrib.auth.decorators import login_required
from django.core.cache import caches
from django.http import JsonResponse
from django.utils import timezone

ONLINE_SECONDS = 60
TYPING_SECONDS = 6
VIEWING_SECONDS = 20


def _cache():
    return caches["live"]


def touch(user):
    """Marks the user as online right now."""
    if user and user.is_authenticated:
        _cache().set(f"seen:{user.pk}", timezone.now().timestamp(), 7 * 86400)


def last_seen(user_id):
    ts = _cache().get(f"seen:{user_id}")
    return timezone.datetime.fromtimestamp(ts, tz=timezone.get_current_timezone()) if ts else None


def presence(user_id):
    """("online", "") / ("away", "21:40" or "12/10") / ("", "")."""
    seen = last_seen(user_id)
    if not seen:
        return "", ""
    age = (timezone.now() - seen).total_seconds()
    if age <= ONLINE_SECONDS:
        return "online", ""
    local = timezone.localtime(seen)
    same_day = local.date() == timezone.localtime().date()
    return "away", local.strftime("%H:%M" if same_day else "%d/%m")


def set_typing(conv_id, side):
    _cache().set(f"typing:{conv_id}:{side}", 1, TYPING_SECONDS)


def clear_typing(conv_id, side):
    _cache().delete(f"typing:{conv_id}:{side}")


def is_typing(conv_id, side):
    return bool(_cache().get(f"typing:{conv_id}:{side}"))


def set_viewing(user_id, conv_id):
    _cache().set(f"viewing:{user_id}:{conv_id}", 1, VIEWING_SECONDS)


def is_viewing(user_id, conv_id):
    return bool(_cache().get(f"viewing:{user_id}:{conv_id}"))


@login_required
def live_view(request):
    """Counts for the header badges plus alerts newer than ?since=<id>."""
    from .messaging import seller_for, unread_for_buyer, unread_for_seller
    from .models import Notification
    user = request.user
    touch(user)
    since = request.GET.get("since", "")
    unread = Notification.objects.filter(user=user, is_read=False)
    data = {
        "notifications": unread.count(),
        "messages": unread_for_buyer(user),
        "seller_messages": 0,
        "latest": 0,
        "new": [],
    }
    seller = seller_for(user)
    if seller:
        data["seller_messages"] = unread_for_seller(seller)
    newest = Notification.objects.filter(user=user).order_by("-id").values_list("id", flat=True).first()
    data["latest"] = newest or 0
    if since.isdigit():
        data["new"] = [
            {"id": n.id, "text": n.message, "link": n.link}
            for n in unread.filter(id__gt=int(since)).order_by("id")[:5]
        ]
    response = JsonResponse(data)
    response["Cache-Control"] = "no-store"
    return response
