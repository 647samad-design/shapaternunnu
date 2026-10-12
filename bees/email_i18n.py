"""Emails in the customer's own language.

The language comes from the order (picked when it was placed), otherwise
the person's profile (saved whenever they switch language on the site).
The rendered email is run through the same translator as the shop pages
(built-in pack + Admin > Translations), with right-to-left layout for
Arabic and Urdu. Names, addresses and messages are never translated.
"""
import html as html_lib
import re

from .translations import RTL_LANGUAGES, TRANSLATIONS


def request_language(request):
    """The language the shopper is browsing in ("en" if none)."""
    from .models import SiteSettings
    lang = getattr(request, "shop_lang", None)
    if not lang:
        try:
            show = SiteSettings.load().show_language_menu
        except Exception:
            show = False
        lang = request.session.get("site_lang") if show else "en"
    return lang if lang in TRANSLATIONS else "en"


def remember(user, lang):
    """Saves the language on the user's profile for future emails."""
    if not user or not getattr(user, "is_authenticated", False) or lang not in TRANSLATIONS:
        return
    from .models import Profile
    profile, _ = Profile.objects.get_or_create(user=user)
    if profile.language != lang:
        Profile.objects.filter(pk=profile.pk).update(language=lang)


def language_for(email=None, user=None, order=None):
    lang = ""
    if order is not None:
        lang = order.language or ""
        if not lang and order.user_id:
            user = order.user
    if not lang and user is None and email:
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.filter(email__iexact=email).select_related("profile").first()
    if not lang and user is not None:
        profile = getattr(user, "profile", None)
        lang = getattr(profile, "language", "") or ""
    return lang if lang in TRANSLATIONS and lang != "en" else ""


def localize(html, subject, lang):
    """Returns (html, subject) translated into ``lang``."""
    if not lang:
        return html, subject
    from .i18n import translate_html
    from .models import SiteSettings
    store = SiteSettings.load().site_name
    html = translate_html(html, lang, store)
    direction = "rtl" if lang in RTL_LANGUAGES else "ltr"
    html = re.sub(r"<html lang=\"[^\"]*\">", f'<html lang="{lang}" dir="{direction}">', html, count=1)
    if direction == "rtl":
        html = html.replace("<body style=\"", "<body dir=\"rtl\" style=\"direction:rtl;text-align:right;", 1)
    subject = translate_subject(subject, lang, store)
    return html, subject


def translate_subject(subject, lang, store):
    from .i18n import translate_html
    prefix = ""
    if subject.startswith(f"{store}: "):
        prefix, subject = f"{store}: ", subject[len(store) + 2:]
    out = translate_html(f"<p>{html_lib.escape(subject)}</p>", lang, store)
    text = html_lib.unescape(re.sub(r"<[^>]+>", "", out)).strip()
    return prefix + (text or subject)
