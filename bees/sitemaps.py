from django.contrib.sitemaps import Sitemap
from django.urls import reverse
from .models import Product
from .categories import choices as category_choices


class ProductSitemap(Sitemap):
    changefreq = "daily"
    priority = 0.8

    def items(self):
        return Product.objects.live().order_by("-id")

    def location(self, obj):
        return reverse("product_detail", args=[obj.id])

    def lastmod(self, obj):
        return obj.updated_at or obj.created_at


class CategorySitemap(Sitemap):
    changefreq = "daily"
    priority = 0.7

    def items(self):
        # Pulled directly from the model so this never goes stale if
        # categories are added or renamed.
        return [choice[0] for choice in category_choices()]

    def location(self, item):
        return reverse("category_products", args=[item])


class StaticViewSitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.5

    def items(self):
        return ["home", "all_products", "help_support", "about_us", "terms_page", "privacy_page", "blog_list", "live_list", "videos"]

    def location(self, item):
        return reverse(item)


class SellerSitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.6

    def items(self):
        from .models import SellerAccount
        return [s for s in SellerAccount.objects.filter(status="approved", vacation_mode=False) if s.display_name]

    def location(self, obj):
        return reverse("store_page", args=[obj.display_name])


class BlogSitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.6

    def items(self):
        from django.utils import timezone
        from .models import BlogPost
        return BlogPost.objects.filter(is_published=True, published_at__lte=timezone.now())

    def location(self, obj):
        return obj.get_absolute_url()

    def lastmod(self, obj):
        return obj.updated_at
