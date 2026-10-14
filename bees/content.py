"""Store-written pages and home-page banners (Manage > Pages & banners)."""
from django.core.cache import cache
from django.utils import timezone


def parse_body(text):
    """Plain text -> blocks for the page template.
    "## Heading" -> heading, "- item" lines -> bullet list, blank line ->
    new paragraph. No HTML is ever taken from the text."""
    blocks, para, items = [], [], []

    def flush():
        if para:
            blocks.append(("p", " ".join(para)))
            para.clear()
        if items:
            blocks.append(("ul", list(items)))
            items.clear()

    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            flush()
        elif line.startswith("## ") or line.startswith("# "):
            flush()
            blocks.append(("h", line.lstrip("#").strip()))
        elif line.startswith("- ") or line.startswith("* "):
            if para:
                blocks.append(("p", " ".join(para)))
                para.clear()
            items.append(line[2:].strip())
        else:
            if items:
                blocks.append(("ul", list(items)))
                items.clear()
            para.append(line)
    flush()
    return blocks


def sections_to_body(sections):
    """Built-in page text -> editable body (to start editing from)."""
    return "\n\n".join(f"## {h}\n{b}" for h, b in sections)


def published_page(slug):
    from .models import Page
    return Page.objects.filter(slug=slug, is_published=True).first()


def footer_pages():
    pages = cache.get("content:footer_pages")
    if pages is None:
        from .models import Page
        built_in = [s for s, _t in Page.BUILT_IN]
        pages = list(Page.objects.filter(is_published=True, show_in_footer=True).exclude(slug__in=built_in)
                     .values_list("slug", "title")[:8])
        cache.set("content:footer_pages", pages, 3600)
    return pages


def home_banners():
    rows = cache.get("content:banners")
    if rows is None:
        from .models import HomeBanner
        rows = list(HomeBanner.objects.filter(is_active=True).values(
            "id", "title", "subtitle", "image_url", "link", "button_label", "starts_at", "ends_at"))
        cache.set("content:banners", rows, 300)
    now = timezone.now()
    return [b for b in rows if (not b["starts_at"] or b["starts_at"] <= now) and (not b["ends_at"] or b["ends_at"] > now)][:3]
