"""Whole-page translation for the shop.

Every storefront page is written in English. When a shopper picks another
language, the finished HTML is translated on the way out: each piece of
visible text (and placeholders, labels, button titles) is looked up in

  1. the store's own translations (Admin > Translations, editable, can be
     filled in one click with AI), then
  2. the built-in translation pack (bees/i18n_pack.py).

Text with no translation yet stays in English and is listed in Admin >
Translations so the owner can translate it. Scripts, styles, form values
and anything inside an element marked ``data-no-translate`` (product
names, prices, addresses...) are never touched.
"""
import html
import re
import threading
import time

from django.core.cache import cache

LANGS = ("ur", "roman", "ar", "es", "fr")
CACHE_KEY = "ui-translations:v1:{lang}"
SKIP_PREFIXES = ("/manage/", "/seller/", "/admin/", "/api/", "/static/", "/media/")

_TAG = re.compile(r"(<[^>]+>)")
_SKIP_OPEN = re.compile(r"<(script|style|textarea|code|pre|svg)\b", re.I)
_ATTR = re.compile(r'\b(placeholder|aria-label|title|alt|data-confirm)="([^"<>]{2,200})"')
_SPACE = re.compile(r"\s+")
_HAS_LETTERS = re.compile(r"[A-Za-z]{2}")


def normalize(text):
    return _SPACE.sub(" ", html.unescape(text)).strip()


_CODE_ONLY = re.compile(r"^[^a-z\s]+$")  # AED, XL, FREESHIP, GIFT-XXXX: codes and sizes stay as they are


def translatable(text):
    if not text or len(text) > 400 or not _HAS_LETTERS.search(text):
        return False
    if _CODE_ONLY.match(text) or text in ("Rs",):
        return False
    if text.startswith(("http://", "https://", "{", "@")) or "{{" in text or "{%" in text:
        return False
    return True


def table(lang):
    """English -> translated text for ``lang`` (built-in pack + the store's
    own translations, which win). Cached for 5 minutes."""
    key = CACHE_KEY.format(lang=lang)
    data = cache.get(key)
    if data is None:
        from .i18n_pack import PACK
        from .translations import TRANSLATIONS
        data = {src: tr[lang] for src, tr in PACK.items() if tr.get(lang)}
        # Text that is already in this language (menu labels) maps to itself.
        for value in TRANSLATIONS.get(lang, {}).values():
            data.setdefault(value, value)
            counter = iter(range(20))
            numbered = re.sub(r"\{\w+\}", lambda _m: "{%d}" % next(counter), value)
            data.setdefault(numbered, numbered)
        try:
            from .models import UIText
            for src, value in UIText.objects.exclude(**{f"{lang}": ""}).values_list("source", lang):
                data[src] = value
        except Exception:
            pass
        # "Search {name}", "This item: {name}": a fixed phrase around a
        # product, store or person's name, which stays as it is.
        data[NAME_KEYS] = [k for k in data if isinstance(k, str) and "{name}" in k]
        cache.set(key, data, 300)
    return data


def clear_cache():
    for lang in LANGS:
        cache.delete(CACHE_KEY.format(lang=lang))


# Strings seen without a translation are collected in memory and written to
# the database at most once a minute (never on every request).
_missing = set()
_missing_lock = threading.Lock()
_last_flush = [0.0]


def _remember_missing(texts):
    if not texts:
        return
    with _missing_lock:
        _missing.update(texts)
        if time.time() - _last_flush[0] < 60 and len(_missing) < 300:
            return
        batch = list(_missing)[:500]
        _missing.clear()
        _last_flush[0] = time.time()
    try:
        from .models import UIText
        batch = _drop_names(batch)
        UIText.objects.bulk_create([UIText(source=t[:400]) for t in batch], ignore_conflicts=True)
    except Exception:
        pass


def _drop_names(texts):
    """Product, store and category names are the sellers' own words, not
    shop text: they're never listed as 'untranslated'."""
    from .models import Category, Product, SellerAccount
    texts = set(texts)
    texts -= set(Product.objects.filter(name__in=texts).values_list("name", flat=True))
    for field in ("brand", "seller_name"):
        texts -= set(Product.objects.filter(**{f"{field}__in": texts}).values_list(field, flat=True))
    if any("{" in t for t in texts):  # "Sunblock SPF {0}": a name with a number in it
        numbered = {template_of(n)[0] for n in Product.objects.filter(name__regex=r"[0-9]").values_list("name", flat=True)[:20000]}
        texts -= numbered
    for field in ("business_name", "organization_name", "full_name"):
        texts -= set(SellerAccount.objects.filter(**{f"{field}__in": texts}).values_list(field, flat=True))
    try:
        texts -= set(Category.objects.filter(name__in=texts).values_list("name", flat=True))
    except Exception:
        pass
    return list(texts)


# Numbers, prices and quoted words change from page to page ("6 items",
# "$15.49 each", "Results for “lamp”"), so they're swapped for {0}, {1}...
# before looking a sentence up, and put back afterwards.
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December")
_MON = "|".join(sorted({m[:3] for m in _MONTHS} | set(_MONTHS), key=len, reverse=True))
_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_WD = "|".join(sorted({d[:3] for d in _DAYS} | set(_DAYS), key=len, reverse=True))
_DATE = (r"\b(?:(?:" + _WD + r"),? )?(?:" + _MON + r")\.? \d{1,2}(?!\d)(?:, \d{4})?(?: · \d{1,2}:\d{2})?"
         r"|\b(?:(?:" + _WD + r"),? )?\d{1,2} (?:" + _MON + r")\b\.?(?: \d{4})?(?:,? \d{1,2}:\d{2})?")
_CODE = r"\b[A-Z]{2,4}-\d{4}-[A-Z0-9]{5,}\b"
_NUMBER = r"(?:[A-Z]{2,3}\$|[$€£₹¥]|Rs |AED |SAR )?(?<![A-Za-z\d])\d[\d,.]*(?![A-Za-z\d])(?:\+|%)?"
_EMAIL = r"[\w.+-]+@[\w-]+\.[\w.-]+"
_QUOTED = r"(?<![\w])'[^'\n<>]{1,80}'(?![\w])"  # 'Merino Wool Beanie' in notifications
_VALUE = re.compile("|".join((_EMAIL, _DATE, _CODE, r"“[^”]{1,80}”", _QUOTED, _NUMBER)))
_MONTH_WORD = re.compile(r"\b(" + _MON + "|" + _WD + r")\b")


def _localize_value(value, lookup):
    """Translates the month and day names inside a date value ("Oct 8, 2026")."""
    return _MONTH_WORD.sub(lambda m: lookup.get(m.group(1), m.group(1)), value)


_SPLITTERS = (" — ", " · ", " | ", " › ", " / ")
_PLACEHOLDER = re.compile(r"\{(\d+)\}")
NAME_KEYS = "\x00name_keys"
# Icons and arrows around a label ("🔥 Deals", "← Blog", "Visit the studio →")
# are kept and only the words are looked up.
_DECOR = re.compile(r"^([^\w{(\[\"'“]+)?(.*?)([\s]*(?:→|·|:|…|›)+|\s*\([^A-Za-z()]*\))?$", re.S)


def _by_name(text, lookup):
    for key in lookup.get(NAME_KEYS, ()):
        pre, _, post = key.partition("{name}")
        if text.startswith(pre) and text.endswith(post) and len(text) > len(pre) + len(post):
            name = text[len(pre):len(text) - len(post)] if post else text[len(pre):]
            if pre or post:
                return lookup[key].replace("{name}", lookup.get(name, name))
    return None


def template_of(text):
    values = []

    def keep(m):
        values.append(m.group(0))
        return "{%d}" % (len(values) - 1)
    return _VALUE.sub(keep, text), values


def lookup_text(text, lookup, missing, store=""):
    tr = lookup.get(text)
    if tr is not None:
        return tr.replace("{store}", store) if store else tr
    m = _DECOR.match(text)
    lead, core, trail = (m.group(1) or "", m.group(2), m.group(3) or "") if m else ("", text, "")
    if (lead or trail) and core:
        if not translatable(core):
            return None  # "· TCS": a name or code with an icon - nothing to translate
        tr = lookup_text(core, lookup, missing, store)
        return None if tr is None else lead + tr + trail
    tr = _by_name(text, lookup)
    if tr is not None:
        return tr.replace("{store}", store) if store else tr
    # The store name is part of some sentences ("Sell on Acme"): look the
    # sentence up with {store} so translations work for any store name.
    branded = text.replace(store, "{store}") if store and store in text else text
    tpl, values = template_of(branded)
    if values and not _HAS_LETTERS.search(_PLACEHOLDER.sub("", tpl)):
        # Only dates/numbers ("Thu 15 Oct 2026", "{0} - {1}"): translate the month and day names.
        return _PLACEHOLDER.sub(lambda m: _localize_value(values[int(m.group(1))], lookup), tpl)
    if values or branded != text:
        tr = lookup.get(tpl)
        if tr is None and values:
            tr = _by_name(tpl, lookup)
        if tr is not None:
            try:
                return tr.replace("{store}", "\x00").format(*[_localize_value(v, lookup) for v in values]).replace("\x00", store)
            except (IndexError, KeyError, ValueError):
                return None
    for sep in (": ", " - "):
        # "Asked for 5 pcs: <the buyer's own note>": translate the fixed start,
        # keep what a person typed as it is.
        if sep in text:
            head, rest = text.split(sep, 1)
            if translatable(head) and len(head) < len(text) - 2:
                tr = lookup_text(head, lookup, set(), store)
                if tr is not None:
                    return tr + sep + rest
    for sep in _SPLITTERS:
        if sep in text:
            pieces = text.split(sep)
            done = [lookup_text(p, lookup, missing, store) if translatable(p) else p for p in pieces]
            if any(d is not None for d in done):
                return sep.join(d if d is not None else p for d, p in zip(done, pieces))
            return None
    if text != store:
        missing.add(tpl)
    return None


def translate_html(content, lang, store=""):
    """Translates the visible text of an HTML document."""
    lookup = table(lang)
    missing = set()
    parts = _TAG.split(content)
    out = []
    skip_tag = None   # inside <script>/<style>/...: copy as is
    skip_depth = 0    # inside data-no-translate: count nested tags
    no_tr_tag = None
    for part in parts:
        if not part:
            continue
        if part.startswith("<"):
            low = part[:12].lower()
            if skip_tag:
                if low.startswith(f"</{skip_tag}"):
                    skip_tag = None
                out.append(part)
                continue
            m = _SKIP_OPEN.match(part)
            if m and not part.endswith("/>"):
                skip_tag = m.group(1).lower()
                out.append(part)
                continue
            if no_tr_tag:
                name = re.match(r"</?\s*([a-zA-Z0-9]+)", part)
                if name and name.group(1).lower() == no_tr_tag and not part.endswith("/>"):
                    skip_depth += -1 if part.startswith("</") else 1
                    if skip_depth == 0:
                        no_tr_tag = None
                out.append(part)
                continue
            if "data-no-translate" in part and not part.startswith("</"):
                name = re.match(r"<\s*([a-zA-Z0-9]+)", part)
                if name and not part.endswith("/>"):
                    no_tr_tag, skip_depth = name.group(1).lower(), 1
                out.append(part)
                continue
            if not part.startswith("</") and not part.startswith("<!"):
                def attr(mm):
                    value = normalize(mm.group(2))
                    if not translatable(value):
                        return mm.group(0)
                    tr = lookup_text(value, lookup, missing, store)
                    if tr is None:
                        return mm.group(0)
                    return f'{mm.group(1)}="{html.escape(tr, quote=True)}"'
                part = _ATTR.sub(attr, part)
            out.append(part)
            continue
        if skip_tag or no_tr_tag:
            out.append(part)
            continue
        text = normalize(part)
        if not translatable(text):
            out.append(part)
            continue
        tr = lookup_text(text, lookup, missing, store)
        if tr is None:
            out.append(part)
            continue
        lead = part[:len(part) - len(part.lstrip())]
        trail = part[len(part.rstrip()):]
        out.append(lead + html.escape(tr, quote=False) + trail)
    _remember_missing(missing)
    return "".join(out)


class PageTranslationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        lang = getattr(request, "shop_lang", None)
        if (lang and lang != "en" and lang in LANGS and response.status_code == 200
                and not getattr(response, "streaming", False)
                and response.get("Content-Type", "").startswith("text/html")
                and not request.path.startswith(SKIP_PREFIXES)):
            try:
                charset = response.charset or "utf-8"
                from .models import SiteSettings
                store = SiteSettings.load().site_name
                response.content = translate_html(response.content.decode(charset), lang, store).encode(charset)
                if response.has_header("Content-Length"):
                    response["Content-Length"] = str(len(response.content))
            except Exception:
                import logging
                logging.getLogger(__name__).exception("Page translation failed for %s", request.path)
        return response
