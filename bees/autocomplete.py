"""Search-as-you-type and spelling help.

* ``suggest(q)``: matching products (with photo and price), categories and
  popular searches for the dropdown under the search box.
* ``correct(q)``: fixes typos ("moisturiser" -> "moisturizer", "headfones" ->
  "headphones") using the words that actually appear in live product names,
  categories and popular searches, so it works in any language the shop uses.
"""
import difflib
import re

from django.core.cache import cache
from django.db.models import Sum

WORD = re.compile(r"[^\W\d_][\w'-]+", re.UNICODE)
VOCAB_KEY = "search:vocab:v1"


def vocabulary():
    words = cache.get(VOCAB_KEY)
    if words is None:
        from .models import DailyStat, Product
        bag = {}
        for name, category in Product.objects.live().values_list("name", "category")[:5000]:
            for w in WORD.findall(f"{name} {category.replace('-', ' ')}".lower()):
                if len(w) > 2:
                    bag[w] = bag.get(w, 0) + 1
        for label in dict(Product.CATEGORY_CHOICES).values():
            for w in WORD.findall(label.lower()):
                if len(w) > 2:
                    bag[w] = bag.get(w, 0) + 3
        for key, n in DailyStat.objects.filter(kind="search").values("key").annotate(n=Sum("count")).order_by("-n")[:300].values_list("key", "n"):
            for w in WORD.findall(key):
                if len(w) > 2 and w in bag:
                    bag[w] += n
        words = bag
        cache.set(VOCAB_KEY, words, 600)
    return words


def correct(query):
    """Returns a corrected query, or '' when nothing needed fixing."""
    words = vocabulary()
    if not words:
        return ""
    vocab = list(words)
    changed, out = False, []
    for token in query.lower().split():
        clean = token.strip(".,!?\"'")
        if len(clean) < 4 or clean in words or not WORD.fullmatch(clean) or clean.isdigit():
            out.append(token)
            continue
        close = difflib.get_close_matches(clean, vocab, n=3, cutoff=0.78)
        if close:
            best = max(close, key=lambda w: (difflib.SequenceMatcher(None, clean, w).ratio(), words[w]))
            out.append(best)
            changed = True
        else:
            out.append(token)
    return " ".join(out) if changed else ""


def suggest(query, limit=5):
    from .models import DailyStat, Product
    from .templatetags.bees_extras import price as price_filter
    q = " ".join(query.split())[:80]
    if len(q) < 2:
        popular = [r["key"] for r in DailyStat.objects.filter(kind="search").values("key").annotate(n=Sum("count")).order_by("-n")[:6]]
        return {"terms": popular, "products": [], "categories": [], "did_you_mean": ""}
    lowered = q.lower()
    qs = Product.objects.live()
    for word in lowered.split()[:4]:
        qs = qs.filter(name__icontains=word)
    products = list(qs.order_by("-stock", "-created_at")[:limit].values("id", "name", "image_url", "price"))
    did_you_mean = ""
    if not products:
        fixed = correct(q)
        if fixed:
            qs2 = Product.objects.live()
            for word in fixed.split()[:4]:
                qs2 = qs2.filter(name__icontains=word)
            products = list(qs2.order_by("-stock", "-created_at")[:limit].values("id", "name", "image_url", "price"))
            if products:
                did_you_mean = fixed
    cats = [{"label": label, "url": f"/category/{code}/"} for code, label in Product.CATEGORY_CHOICES
            if lowered in label.lower() or lowered in code][:3]
    terms = [r["key"] for r in DailyStat.objects.filter(kind="search", key__startswith=lowered).exclude(key=lowered)
             .values("key").annotate(n=Sum("count")).order_by("-n")[:4]]
    return {
        "terms": terms,
        "products": [{"name": p["name"], "url": f"/product/{p['id']}/", "image": p["image_url"], "price": str(price_filter(p["price"]))} for p in products],
        "categories": cats,
        "did_you_mean": did_you_mean,
    }
