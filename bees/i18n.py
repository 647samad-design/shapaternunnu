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


def translatable(text):
    if not text or len(text) > 400 or not _HAS_LETTERS.search(text):
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
        try:
            from .models import UIText
            for src, value in UIText.objects.exclude(**{f"{lang}": ""}).values_list("source", lang):
                data[src] = value
        except Exception:
            pass
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
        UIText.objects.bulk_create([UIText(source=t[:400]) for t in batch], ignore_conflicts=True)
    except Exception:
        pass


# Numbers, prices and quoted words change from page to page ("6 items",
# "$15.49 each", "Results for “lamp”"), so they're swapped for {0}, {1}...
# before looking a sentence up, and put back afterwards.
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December")
_MON = "|".join(sorted({m[:3] for m in _MONTHS} | set(_MONTHS), key=len, reverse=True))
_DATE = r"\b(?:" + _MON + r")\.? \d{1,2}(?:, \d{4})?(?: · \d{1,2}:\d{2})?"
_CODE = r"\b[A-Z]{2,4}-\d{4}-[A-Z0-9]{5,}\b"
_NUMBER = r"(?:[A-Z]{2,3}\$|[$€£₹¥]|Rs |AED |SAR )?(?<![A-Za-z\d])\d[\d,.]*(?![A-Za-z\d])(?:\+|%)?"
_EMAIL = r"[\w.+-]+@[\w-]+\.[\w.-]+"
_VALUE = re.compile("|".join((_EMAIL, _DATE, _CODE, r"“[^”]{1,80}”", _NUMBER)))
_MONTH_WORD = re.compile(r"\b(" + _MON + r")\b")


def _localize_value(value, lookup):
    """Translates the month name inside a date value ("Oct 8, 2026")."""
    return _MONTH_WORD.sub(lambda m: lookup.get(m.group(1), m.group(1)), value)


_SPLITTERS = (" — ", " · ", " | ")


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
    # The store name is part of some sentences ("Sell on Acme"): look the
    # sentence up with {store} so translations work for any store name.
    branded = text.replace(store, "{store}") if store and store in text else text
    tpl, values = template_of(branded)
    if values or branded != text:
        tr = lookup.get(tpl)
        if tr is not None:
            try:
                return tr.replace("{store}", "\x00").format(*[_localize_value(v, lookup) for v in values]).replace("\x00", store)
            except (IndexError, KeyError, ValueError):
                return None
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
