"""Product feeds for shopping channels.

* /feeds/google.xml   Google Merchant Center (Shopping ads, free listings,
                      and Google's AI shopping in Search and Gemini)
* /feeds/products.csv Meta Commerce Manager (Facebook & Instagram Shops)
                      and TikTok Shop catalogs - both read this format

The channel fetches the address on a schedule (e.g. daily), so prices and
stock stay in sync without uploads. Auctions, quote-only and digital items
are left out: their price isn't a fixed shop price.
"""
import csv
import io
from xml.sax.saxutils import escape

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse
from django.utils.html import strip_tags

CACHE_SECONDS = 3600
CONDITION = {"new": "new", "refurbished": "refurbished"}  # every "used_*" is "used"


def feed_products():
    from .models import Product
    return (Product.objects.listed().filter(is_auction=False, quote_only=False, is_digital=False)
            .exclude(image_url="").select_related("seller_account").prefetch_related("extra_images").order_by("id"))


def _abs(request, url):
    if not url:
        return ""
    return url if url.startswith("http") else request.build_absolute_uri(url)


def _text(value, limit):
    return " ".join(strip_tags(value or "").split())[:limit]


def _money(amount):
    return f"{amount:.2f} {settings.STORE_CURRENCY.upper()}"


def item(request, p):
    """One product as feed fields (shared by both formats)."""
    from .categories import label
    from .models import SiteSettings
    site = SiteSettings.load()
    sale = p.old_price is not None and p.old_price > p.price
    brand = p.brand or (p.seller_account.display_name if p.seller_account_id else site.site_name)
    data = {
        "id": str(p.id),
        "title": _text(p.name, 150),
        "description": _text(p.description or p.name, 5000),
        "link": request.build_absolute_uri(f"/product/{p.id}/"),
        "image_link": _abs(request, p.image_url),
        "additional_image_link": ",".join(_abs(request, i.image_url) for i in list(p.extra_images.all())[:10]),
        "availability": "in_stock" if p.stock > 0 else "out_of_stock",
        "price": _money(p.old_price if sale else p.price),
        "sale_price": _money(p.price) if sale else "",
        "brand": brand[:70],
        "gtin": p.gtin,
        "mpn": p.mpn,
        "condition": CONDITION.get(p.condition, "used"),
        "product_type": label(p.category) or p.category,
        "identifier_exists": "yes" if p.gtin or (p.brand and p.mpn) else "no",
        "shipping_weight": f"{p.weight_grams} g" if p.weight_grams else "",
        "seller": p.seller_account.display_name if p.seller_account_id else site.site_name,
    }
    return data


def readiness():
    """What stops products from being approved by Google (for the admin)."""
    products = list(feed_products())
    missing_id = [p for p in products if not p.gtin and not (p.brand and p.mpn)]
    short = [p for p in products if len(_text(p.description, 5000)) < 50]
    return {"count": len(products), "no_identifier": len(missing_id), "short_description": len(short),
            "examples": missing_id[:5]}


def _cached(key, build):
    body = cache.get(key)
    if body is None:
        body = build()
        cache.set(key, body, CACHE_SECONDS)
    return body


def google_xml(request):
    from .models import SiteSettings
    site = SiteSettings.load()

    def build():
        out = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0"><channel>',
               f"<title>{escape(site.site_name)}</title><link>{escape(request.build_absolute_uri('/'))}</link>",
               f"<description>{escape(site.tagline or site.site_name)}</description>"]
        for p in feed_products():
            d = item(request, p)
            out.append("<item>")
            out.append(f"<g:id>{escape(d['id'])}</g:id><title>{escape(d['title'])}</title>"
                       f"<description>{escape(d['description'])}</description><link>{escape(d['link'])}</link>")
            out.append(f"<g:image_link>{escape(d['image_link'])}</g:image_link>")
            for extra in filter(None, d["additional_image_link"].split(",")):
                out.append(f"<g:additional_image_link>{escape(extra)}</g:additional_image_link>")
            for key in ("availability", "price", "sale_price", "brand", "gtin", "mpn", "condition", "product_type",
                        "identifier_exists", "shipping_weight"):
                if d[key]:
                    out.append(f"<g:{key}>{escape(d[key])}</g:{key}>")
            out.append("</item>")
        out.append("</channel></rss>")
        return "\n".join(out)

    body = _cached(f"feed:google:{request.get_host()}", build)
    return HttpResponse(body, content_type="application/xml; charset=utf-8")


META_COLUMNS = ["id", "title", "description", "availability", "condition", "price", "sale_price", "link", "image_link",
                "additional_image_link", "brand", "gtin", "mpn", "product_type"]


def products_csv(request):
    def build():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(META_COLUMNS)
        for p in feed_products():
            d = item(request, p)
            d["availability"] = d["availability"].replace("_", " ")  # Meta and TikTok spell it "in stock"
            writer.writerow([d[c] for c in META_COLUMNS])
        return buf.getvalue()

    body = _cached(f"feed:csv:{request.get_host()}", build)
    response = HttpResponse(body, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'inline; filename="products.csv"'
    return response
