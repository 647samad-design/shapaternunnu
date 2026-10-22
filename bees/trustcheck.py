"""Trust checks that run every day and put findings in Admin > Trust & safety.

* Copied listings - every product photo gets a fingerprint (a 64-bit
  "difference hash" that survives resizing, re-compressing and small
  edits). The same photo used by two different sellers is flagged: one of
  them is probably copying the other's listing (or selling a fake).
* Suspicious reviews - groups of 5-star reviews for one seller from brand
  new accounts without a purchase, or reviews with near-identical text.
* Linked risky orders - one phone number or address used by several
  accounts with refused or cancelled cash orders (a fake-order network).

Nothing is removed automatically: a person decides on each finding.
"""
import hashlib
import io
import logging
import re
from collections import defaultdict
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)
NEAR = 6  # bits that may differ for two photos to count as the same


# ---------------------------------------------------------------- photo fingerprints

def dhash(image):
    """64-bit difference hash of a PIL image, as 16 hex characters."""
    from PIL import Image
    small = image.convert("L").resize((9, 8), Image.LANCZOS)
    px = list(small.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (1 if px[row * 9 + col] > px[row * 9 + col + 1] else 0)
    return f"{bits:016x}"


def distance(a, b):
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def open_stored(url):
    """Opens an image that lives in this store's own storage (never fetches
    other websites). Returns a PIL image or None."""
    from django.conf import settings
    from django.core.files.storage import default_storage
    from PIL import Image
    if not url:
        return None
    name = None
    media = settings.MEDIA_URL or "/media/"
    if url.startswith(media):
        name = url[len(media):]
    else:
        base = default_storage.url("x")[:-1]
        if base.startswith("http") and url.startswith(base):
            name = url[len(base):]
    if not name or ".." in name:
        return None
    try:
        with default_storage.open(name, "rb") as fh:
            data = fh.read(15 * 1024 * 1024)
        img = Image.open(io.BytesIO(data))
        img.load()
        return img
    except Exception:
        logger.info("Could not open stored image %s", url, exc_info=True)
        return None


def fingerprint(product):
    img = open_stored(product.image_url)
    return dhash(img) if img is not None else ""


def fingerprint_products(limit=400):
    """Fingerprints products that don't have one yet ('-' = no stored photo)."""
    from .models import Product
    done = 0
    for p in Product.objects.filter(image_hash="").exclude(approval_status="archived").only("pk", "image_url")[:limit]:
        value = fingerprint(p) or "-"
        Product.objects.filter(pk=p.pk).update(image_hash=value)
        done += 1
    return done


def _flag(kind, key, title, detail, score, **extra):
    from .models import TrustFlag
    flag, created = TrustFlag.objects.get_or_create(key=key[:120], defaults={
        "kind": kind, "title": title[:200], "detail": detail, "score": min(score, 100), **extra})
    return created


def find_copied_listings():
    from .models import Product
    rows = list(Product.objects.exclude(image_hash__in=["", "-"]).exclude(approval_status__in=["archived", "rejected"])
                .exclude(seller_account__isnull=True)
                .values_list("pk", "image_hash", "seller_account_id", "offer_of_id", "created_at", "name")[:5000])
    by_prefix = defaultdict(list)
    for r in rows:
        by_prefix[r[1][:2]].append(r)  # cheap pre-grouping; exact and near matches share the first byte almost always
    found = 0
    seen = set()
    for group in by_prefix.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if a[2] == b[2]:
                    continue  # same seller
                if a[3] == b[0] or b[3] == a[0] or (a[3] and a[3] == b[3]):
                    continue  # listed as "another seller of the same product" on purpose
                d = distance(a[1], b[1])
                if d > NEAR:
                    continue
                newer, older = (a, b) if a[4] >= b[4] else (b, a)
                key = f"copy:{min(a[0], b[0])}:{max(a[0], b[0])}"
                if key in seen:
                    continue
                seen.add(key)
                same = "the same photo" if d == 0 else "an almost identical photo"
                found += _flag("copied_listing", key, f"'{newer[5]}' uses {same} as '{older[5]}' from another seller",
                               "Check whether the newer listing copied the photo (or is a fake of the original). "
                               "If so, take it down and warn the seller.", 80 - d * 5,
                               product_id=newer[0], other_product_id=older[0])
    return found


# ---------------------------------------------------------------- reviews

def _shingles(text):
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {" ".join(words[i:i + 3]) for i in range(max(len(words) - 2, 1))} if words else set()


def _similar(a, b):
    sa, sb = _shingles(a), _shingles(b)
    if len(sa) < 3 or len(sb) < 3:
        return False
    return len(sa & sb) / len(sa | sb) >= 0.6


def find_review_rings(days=90):
    from .models import Review
    since = timezone.now() - timedelta(days=days)
    reviews = list(Review.objects.filter(created_at__gte=since).select_related("user", "product")[:5000])
    found = 0
    # 1) Several new-account, no-purchase 5-star reviews for one seller in two weeks.
    by_seller = defaultdict(list)
    for r in reviews:
        if r.rating < 5 or r.is_verified_purchase or r.user is None:
            continue
        if r.created_at - r.user.date_joined > timedelta(days=3):
            continue
        by_seller[r.product.seller_account_id or 0].append(r)
    for seller_id, group in by_seller.items():
        group.sort(key=lambda r: r.created_at)
        for i in range(len(group)):
            window = [r for r in group[i:] if r.created_at - group[i].created_at <= timedelta(days=14)]
            if len(window) >= 3:
                ids = sorted(r.pk for r in window)
                key = "reviews:new:" + hashlib.sha1(",".join(map(str, ids)).encode()).hexdigest()[:20]
                name = window[0].product.seller_account.display_name if seller_id else "the store"
                found += _flag("review_ring", key, f"{len(window)} five-star reviews for {name} from brand-new accounts with no purchase",
                               "Posted within two weeks by accounts that signed up shortly before. Typical of paid or fake reviews.",
                               40 + 10 * len(window), product=window[0].product, review_ids=ids)
                break
    # 2) Near-identical review texts from different people.
    texts = [r for r in reviews if len(r.comment or "") >= 40]
    for i, a in enumerate(texts):
        for b in texts[i + 1:i + 400]:
            if a.user_id == b.user_id or not _similar(a.comment, b.comment):
                continue
            ids = sorted([a.pk, b.pk])
            key = f"reviews:copy:{ids[0]}:{ids[1]}"
            found += _flag("review_ring", key, f"Two reviews with almost the same words ({a.username} and {b.username})",
                           f"On '{a.product.name}' and '{b.product.name}'. Copied wording often means the reviews were written by one person.",
                           55, product=a.product, other_product=b.product if b.product_id != a.product_id else None, review_ids=ids)
    return found


# ---------------------------------------------------------------- order networks

def phone_tail(phone):
    digits = "".join(ch for ch in phone or "" if ch.isdigit())
    return digits[-10:] if len(digits) >= 8 else ""


def address_key(order):
    raw = f"{order.address} {order.city}".lower()
    return re.sub(r"[^a-z0-9]", "", raw)[:80]


def linked_orders(order, days=180):
    """Other recent orders sharing this order's phone or address, placed by
    a different person. Returns (by_phone, by_address) lists."""
    from .models import Order
    since = timezone.now() - timedelta(days=days)
    me = (order.email or order.guest_email or "").lower()
    tail = phone_tail(order.phone)
    phone_hits, addr_hits = [], []
    if tail:
        for o in Order.objects.filter(created_at__gte=since, phone__contains=tail[-7:]).exclude(pk=order.pk)[:200]:
            if phone_tail(o.phone) == tail and (o.email or o.guest_email or "").lower() != me:
                phone_hits.append(o)
    key = address_key(order)
    if len(key) >= 12:
        for o in Order.objects.filter(created_at__gte=since, city__iexact=order.city).exclude(pk=order.pk)[:500]:
            if address_key(o) == key and (o.email or o.guest_email or "").lower() != me:
                addr_hits.append(o)
    return phone_hits, addr_hits


def _bad(o):
    return o.cod_refused or (o.status == "cancelled" and o.payment_method == "cod") or o.payment_status == "failed"


def network_signals(order):
    """[(points, reason)] for the order risk score."""
    phone_hits, addr_hits = linked_orders(order)
    out = []
    people = {(o.email or o.guest_email or "").lower() for o in phone_hits}
    if len(people) >= 2:
        out.append((15, f"Phone number also used by {len(people)} other customers"))
    bad = [o for o in phone_hits + addr_hits if _bad(o)]
    if bad:
        out.append((25 if len(bad) >= 2 else 15, f"Phone or address linked to {len(bad)} refused or failed order{'s' if len(bad) != 1 else ''} by other customers"))
    return out


def find_order_networks(days=60):
    from .models import Order
    since = timezone.now() - timedelta(days=days)
    groups = defaultdict(list)
    for o in Order.objects.filter(created_at__gte=since).only("pk", "phone", "email", "guest_email", "status", "payment_method",
                                                               "payment_status", "cod_refused")[:20000]:
        tail = phone_tail(o.phone)
        if tail:
            groups[tail].append(o)
    found = 0
    for tail, orders in groups.items():
        people = {(o.email or o.guest_email or "").lower() for o in orders}
        bad = [o for o in orders if _bad(o)]
        if len(people) >= 3 and len(bad) >= 2:
            ids = sorted(o.pk for o in orders)[:50]
            found += _flag("order_network", f"phone:{tail}:{len(ids)}",
                           f"One phone number (…{tail[-4:]}) used by {len(people)} customers, {len(bad)} refused or failed orders",
                           "Several accounts sharing a phone and refusing cash parcels is a common fake-order pattern. "
                           "Consider blocking cash on delivery for this number (Store settings > Cash on delivery).",
                           50 + 10 * len(bad), order_ids=ids)
    return found


def run_checks():
    return {"fingerprinted": fingerprint_products(), "copied": find_copied_listings(),
            "reviews": find_review_rings(), "networks": find_order_networks()}
