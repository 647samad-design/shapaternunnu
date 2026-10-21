"""Shopper features: group buys, spin the wheel, gift registries, live
shows and 'still looking?' emails."""
import random
import re
import secrets
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import F
from django.utils import timezone

GROUP_HOURS = 24
CENT = Decimal("0.01")


# ---------------------------------------------------------------- group buy

def open_groups(product, limit=3):
    from .models import GroupBuy
    return [g for g in GroupBuy.objects.filter(product=product, completed_at=None, expires_at__gt=timezone.now())
            .select_related("leader").order_by("expires_at")[:limit * 2] if g.joined < g.size][:limit]


def _new_code():
    from .models import GroupBuy
    while True:
        code = secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")[:8]
        if not GroupBuy.objects.filter(code=code).exists():
            return code


def attach_group(request, order, item, key):
    """Called for each new order item: joins (or starts) the group the
    shopper chose for this cart line."""
    from .models import GroupBuy, GroupMember
    wanted = (request.session.get("group_for") or {}).get(key)
    product = item.product
    if not wanted or not product or not product.group_size or not product.group_percent:
        return None
    if wanted == "new":
        group = GroupBuy.objects.create(product=product, code=_new_code(), leader=order.user, size=product.group_size,
                                        percent=product.group_percent, expires_at=timezone.now() + timedelta(hours=GROUP_HOURS))
    else:
        group = GroupBuy.objects.select_for_update().filter(code=wanted, product=product).first()
        if not group or not group.is_open or group.joined >= group.size:
            return None
        if order.user_id and group.members.filter(user=order.user).exists():
            return None  # one place per person
    GroupMember.objects.create(group=group, order_item=item, user=order.user)
    if group.joined >= group.size:
        group.completed_at = timezone.now()
        group.save(update_fields=["completed_at"])
        _tell_group(group)
    return group


def _tell_group(group):
    from .models import Notification
    users = {m.user_id for m in group.members.all() if m.user_id}
    Notification.objects.bulk_create([
        Notification(user_id=uid, link="/my-orders/",
                     message=f"Your group for '{group.product.name}' is full! You'll get {group.percent}% back as store credit after delivery."[:255])
        for uid in users])


def settle_groups():
    """Gives full groups' members their money back once their item is
    delivered (and not returned or cancelled). Returns how many."""
    from .models import GroupMember, Notification, Profile
    paid = 0
    due = (GroupMember.objects.filter(credited=None, group__completed_at__isnull=False, user__isnull=False)
           .select_related("group", "order_item__order", "user"))
    for member in due[:500]:
        item = member.order_item
        if item.order.status == "cancelled" or item.fulfillment_status == "cancelled" or not item.received_at:
            continue
        if item.return_requests.exclude(status="rejected").exists():
            continue
        amount = (item.price * item.quantity * member.group.percent / 100).quantize(CENT, rounding=ROUND_HALF_UP)
        with transaction.atomic():
            if not type(member).objects.filter(pk=member.pk, credited=None).update(credited=amount):
                continue
            profile, _ = Profile.objects.get_or_create(user=member.user)
            Profile.objects.filter(pk=profile.pk).update(store_credit=F("store_credit") + amount)
        Notification.objects.create(user=member.user, link="/profile/",
                                    message=f"Group buy reward: {amount} added to your store credit for '{item.product_name}'."[:255])
        paid += 1
    return paid


# ---------------------------------------------------------------- spin the wheel

PRIZE = re.compile(r"^\s*(.+?)\s*:\s*(\d+)\s*$")


def prizes(text):
    """[(label, weight)] from the settings text; bad lines are skipped."""
    out = []
    for line in (text or "").splitlines():
        m = PRIZE.match(line)
        if not m or int(m.group(2)) <= 0:
            continue
        label = m.group(1)[:30]
        if _kind(label)[0] is None:
            continue
        out.append((label, int(m.group(2))))
    return out[:10]


def _kind(label):
    low = label.lower()
    m = re.match(r"^(\d{1,2})\s*% off$", low)
    if m and 1 <= int(m.group(1)) <= 50:
        return "percent", int(m.group(1))
    m = re.match(r"^(\d{1,4})\s*points?$", low)
    if m:
        return "points", int(m.group(1))
    if low == "free shipping":
        return "shipping", 0
    if low == "try again":
        return "none", 0
    return None, 0


def can_spin(user):
    from .models import Profile
    profile = Profile.objects.filter(user=user).first()
    return not (profile and profile.last_spin_on == timezone.localdate())


def spin(user, rng=None):
    """Spins once for today. Returns (SpinResult, index) or raises ValueError."""
    from .models import Coupon, Profile, SiteSettings, SpinResult
    site = SiteSettings.load()
    table = prizes(site.spin_prizes)
    if not site.spin_enabled or not table:
        raise ValueError("The wheel is resting today.")
    rng = rng or random.SystemRandom()
    with transaction.atomic():
        profile, _ = Profile.objects.get_or_create(user=user)
        profile = Profile.objects.select_for_update().get(pk=profile.pk)
        today = timezone.localdate()
        if profile.last_spin_on == today:
            raise ValueError("You've already spun today. Come back tomorrow!")
        index = rng.choices(range(len(table)), weights=[w for _l, w in table])[0]
        label = table[index][0]
        kind, value = _kind(label)
        coupon, points = None, 0
        if kind in ("percent", "shipping"):
            code = "SPIN" + secrets.token_hex(3).upper()
            coupon = Coupon.objects.create(code=code, owner=user, purpose="spin", percent_off=value if kind == "percent" else 0,
                                           free_shipping=kind == "shipping", usage_limit=1, per_user_limit=1,
                                           expiry_date=today + timedelta(days=7), title=f"Wheel prize: {label}")
        elif kind == "points":
            points = value
            Profile.objects.filter(pk=profile.pk).update(loyalty_points=F("loyalty_points") + value)
        Profile.objects.filter(pk=profile.pk).update(last_spin_on=today)
        result = SpinResult.objects.create(user=user, prize=label, coupon=coupon, points=points)
    return result, index


# ---------------------------------------------------------------- gift registry

def new_registry_token():
    from .models import GiftRegistry
    while True:
        token = secrets.token_urlsafe(10)
        if not GiftRegistry.objects.filter(token=token).exists():
            return token


def attach_registry(request, order, item, key):
    """Marks a registry gift as bought and tells the registry owner."""
    from .models import Notification, RegistryItem
    item_id = (request.session.get("registry_for") or {}).get(key)
    if not item_id:
        return None
    line = RegistryItem.objects.select_related("registry").filter(pk=item_id, product=item.product).first()
    if not line:
        return None
    item.registry_item = line
    item.save(update_fields=["registry_item"])
    RegistryItem.objects.filter(pk=line.pk).update(bought=F("bought") + item.quantity)
    buyer = order.full_name.split(" ")[0] if order.full_name else "Someone"
    Notification.objects.create(user=line.registry.owner, link=f"/account/registries/{line.registry_id}/",
                                message=f"{buyer} bought '{item.product_name}' from your registry '{line.registry.title}'."[:255])
    return line


def after_item(request, order, item, key):
    """Hooks run for every new order item (inside the order transaction)."""
    attach_group(request, order, item, key)
    attach_registry(request, order, item, key)


def clear_markers(request):
    for name in ("group_for", "registry_for"):
        request.session.pop(name, None)


# ---------------------------------------------------------------- live shows

def live_now():
    from .models import LiveShow
    return LiveShow.objects.filter(started_at__isnull=False, ended_at=None).select_related("seller_account")


def go_live(show):
    """Starts the show and tells reminder sign-ups and store followers."""
    from .models import LiveReminder, Notification, StoreFollow
    if show.started_at:
        return
    show.started_at = timezone.now()
    show.save(update_fields=["started_at"])
    from django.core.cache import cache
    cache.delete("live_on")
    users = set(LiveReminder.objects.filter(show=show, sent=False).values_list("user_id", flat=True))
    if show.seller_account_id:
        users |= set(StoreFollow.objects.filter(seller_account_id=show.seller_account_id).values_list("user_id", flat=True))
    Notification.objects.bulk_create([Notification(user_id=u, link=f"/live-shopping/{show.pk}/", message=f"🔴 Live now: {show.title}"[:255])
                                      for u in users])
    LiveReminder.objects.filter(show=show).update(sent=True)


# ---------------------------------------------------------------- 'still looking?' emails

def log_view(request, product):
    """Remembers what a signed-in shopper looked at (once per product per
    session)."""
    if not request.user.is_authenticated or request.user.is_staff:
        return
    seen = request.session.get("_pv") or []
    if product.pk in seen:
        return
    from .models import ProductView
    ProductView.objects.update_or_create(user=request.user, product=product, defaults={"viewed_at": timezone.now()})
    request.session["_pv"] = (seen + [product.pk])[-50:]


def send_browse_reminders(now=None):
    from .models import OrderItem, Product, Profile, ProductView, SiteSettings
    from .order_emails import absolute
    from .views import _send_html_email
    site = SiteSettings.load()
    if not site.browse_reminders:
        return 0
    now = now or timezone.now()
    week_ago = now - timedelta(days=7)
    views = (ProductView.objects.filter(viewed_at__range=(now - timedelta(days=3), now - timedelta(hours=20)))
             .values_list("user_id", flat=True).distinct())
    sent = 0
    for uid in list(views)[:500]:
        profile, _ = Profile.objects.select_related("user").get_or_create(user_id=uid)
        if not profile.cart_reminders or not profile.user.email or not profile.user.is_active:
            continue
        if profile.browse_reminder_at and profile.browse_reminder_at > week_ago:
            continue
        since = now - timedelta(days=3)
        bought = set(OrderItem.objects.filter(order__user_id=uid, order__created_at__gte=since - timedelta(days=30))
                     .values_list("product_id", flat=True))
        in_cart = {int(str(k).split("-")[0]) for k in (profile.saved_cart or {}) if str(k).split("-")[0].isdigit()}
        ids = [pid for pid in ProductView.objects.filter(user_id=uid, viewed_at__gte=since).order_by("-viewed_at")
               .values_list("product_id", flat=True) if pid not in bought and pid not in in_cart][:8]
        products = [p for p in Product.objects.listed().filter(pk__in=ids, stock__gt=0)][:4]
        if not products:
            continue
        if not Profile.objects.filter(pk=profile.pk).exclude(browse_reminder_at__gt=week_ago).update(browse_reminder_at=now):
            continue
        _send_html_email("Still thinking about these?", "bees/emails/browse_reminder.html", {
            "user": profile.user, "products": [{"product": p, "url": absolute(f"/product/{p.id}/")} for p in products],
            "prefs_url": absolute("/account/privacy/")}, profile.user.email, lang=profile.language or None)
        sent += 1
    return sent
