"""Structured data (schema.org JSON-LD) for rich results in Google:
product price/stock/rating stars, breadcrumbs, return policy and shipping,
product lists on category pages."""
from datetime import timedelta

from django.conf import settings
from django.urls import reverse
from django.utils import timezone


def _abs(request, url):
    if not url:
        return ""
    return url if url.startswith("http") else request.build_absolute_uri(url)


def breadcrumbs(request, trail):
    """trail: [(name, url), ...]"""
    return {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "name": name, "item": _abs(request, url)} for i, (name, url) in enumerate(trail)]}


def product(request, product, reviews):
    from .models import SiteSettings
    site = SiteSettings.load()
    url = request.build_absolute_uri(reverse("product_detail", args=[product.pk]))
    images = [_abs(request, product.image_url)] + [_abs(request, i.image_url) for i in product.extra_images.all()[:5]]
    currency = settings.STORE_CURRENCY.upper()
    offer = {
        "@type": "Offer", "url": url, "priceCurrency": currency, "price": f"{product.price:.2f}",
        "priceValidUntil": (timezone.localdate() + timedelta(days=30)).isoformat(),
        "availability": "https://schema.org/InStock" if product.stock > 0 else "https://schema.org/OutOfStock",
        "itemCondition": "https://schema.org/NewCondition",
        "seller": {"@type": "Organization", "name": product.seller_account.display_name if product.seller_account_id else site.site_name},
        "hasMerchantReturnPolicy": {
            "@type": "MerchantReturnPolicy", "applicableCountry": (site.ship_from_country or "US").upper(),
            "returnPolicyCategory": "https://schema.org/MerchantReturnFiniteReturnWindow",
            "merchantReturnDays": site.return_days, "returnMethod": "https://schema.org/ReturnByMail",
        },
    }
    if site.delivery_days:
        offer["shippingDetails"] = {"@type": "OfferShippingDetails", "deliveryTime": {
            "@type": "ShippingDeliveryTime",
            "handlingTime": {"@type": "QuantitativeValue", "minValue": 0, "maxValue": 2, "unitCode": "DAY"},
            "transitTime": {"@type": "QuantitativeValue", "minValue": 1, "maxValue": int(site.delivery_days), "unitCode": "DAY"}}}
    data = {
        "@context": "https://schema.org", "@type": "Product", "@id": url + "#product", "name": product.name, "url": url,
        "image": [i for i in images if i], "description": (product.seo_description or product.description or product.name)[:5000],
        "sku": str(product.pk), "category": product.get_category_display(),
        "brand": {"@type": "Brand", "name": product.seller_name or site.site_name}, "offers": offer,
    }
    if getattr(product, "weight_grams", None):
        data["weight"] = {"@type": "QuantitativeValue", "value": product.weight_grams, "unitCode": "GRM"}
    reviews = list(reviews[:5])
    if product.rating_count:
        data["aggregateRating"] = {"@type": "AggregateRating", "ratingValue": str(product.average_rating),
                                   "reviewCount": product.rating_count, "bestRating": "5", "worstRating": "1"}
        data["review"] = [{"@type": "Review", "author": {"@type": "Person", "name": r.username or "Customer"},
                           "datePublished": r.created_at.date().isoformat(), "reviewBody": r.comment[:1000],
                           "reviewRating": {"@type": "Rating", "ratingValue": r.rating, "bestRating": 5}} for r in reviews]
    return data


def item_list(request, products, name):
    return {"@context": "https://schema.org", "@type": "ItemList", "name": name, "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "url": request.build_absolute_uri(reverse("product_detail", args=[p.pk])), "name": p.name}
        for i, p in enumerate(products[:30])]}
