"""JSON shapes of the REST API. Money is returned as strings ("19.99") in the
store currency, dates in ISO 8601 (UTC)."""
from decimal import Decimal

from django.conf import settings
from django.urls import reverse
from rest_framework import serializers

from ..models import Order, OrderItem, Product, ProductVariant, Review, SellerAccount
from ..categories import choices as category_choices


def absolute(request, url):
    url = str(url or "")
    if not url:
        return ""
    if url.startswith("/") and not url.startswith("//") and request is not None:
        return request.build_absolute_uri(url)
    return url


def store_currency():
    return getattr(settings, "STORE_CURRENCY", "usd").upper()


def money(value):
    if value is None:
        return None
    return str(Decimal(value).quantize(Decimal("0.01")))


class CategorySerializer(serializers.Serializer):
    code = serializers.CharField()
    label = serializers.CharField()
    product_count = serializers.IntegerField()


class SellerBriefSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    url = serializers.CharField()
    verified = serializers.BooleanField()
    top_seller = serializers.BooleanField()

    def to_representation(self, seller):
        request = self.context.get("request")
        return {
            "id": seller.id, "name": seller.display_name,
            "url": absolute(request, reverse("store_page", args=[seller.display_name])),
            "verified": bool(seller.is_verified), "top_seller": bool(seller.top_seller),
        }


class VariantSerializer(serializers.ModelSerializer):
    label = serializers.CharField(read_only=True)
    price = serializers.SerializerMethodField()

    class Meta:
        model = ProductVariant
        fields = ["id", "label", "size", "color", "price", "stock"]

    def get_price(self, v) -> str:
        return money(v.unit_price)


class ProductListSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()
    image = serializers.SerializerMethodField()
    price = serializers.SerializerMethodField()
    old_price = serializers.SerializerMethodField()
    currency = serializers.SerializerMethodField()
    category = serializers.SerializerMethodField()
    in_stock = serializers.SerializerMethodField()
    rating = serializers.SerializerMethodField()
    seller = serializers.SerializerMethodField()
    deal = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = ["id", "name", "url", "image", "price", "old_price", "currency", "category", "in_stock",
                  "has_variants", "rating", "seller", "deal", "created_at"]

    def get_specifications(self, p) -> list:
        return [{"name": n, "value": v} for n, v in p.spec_rows()]

    def get_video(self, p) -> str | None:
        v = p.video()
        return p.video_url if v else None

    def get_order_limits(self, p) -> dict:
        low, high = p.order_limits()
        return {"min": low, "max": high}

    def get_product_safety(self, p) -> dict | None:
        if not p.has_safety_info:
            return None
        return {"manufacturer": p.manufacturer, "eu_responsible_person": p.eu_responsible_person, "safety_info": p.safety_info}

    def get_url(self, p) -> str:
        return absolute(self.context.get("request"), reverse("product_detail", args=[p.pk]))

    def get_image(self, p) -> str:
        return absolute(self.context.get("request"), p.image_url)

    def get_price(self, p) -> str:
        return money(p.price)

    def get_old_price(self, p) -> str | None:
        return money(p.old_price) if p.old_price and p.old_price > p.price else None

    def get_currency(self, p) -> str:
        return store_currency()

    def get_category(self, p) -> dict:
        return {"code": p.category, "label": p.get_category_display()}

    def get_in_stock(self, p) -> bool:
        return p.stock > 0

    def get_rating(self, p) -> dict:
        return {"average": float(p.average_rating or 0), "count": int(p.rating_count or 0)}

    def get_seller(self, p) -> dict | None:
        if not p.seller_account_id:
            return None
        return SellerBriefSerializer(p.seller_account, context=self.context).data

    def get_deal(self, p) -> dict | None:
        out = {}
        if p.flash_active:
            out["flash_sale"] = True
            out["ends_at"] = p.flash_sale_ends.isoformat() if p.flash_sale_ends else None
        if p.has_bulk_offer:
            out["quantity_offer"] = {"min_quantity": p.bulk_min_qty, "percent_off": p.bulk_percent}
        return out or None


class ProductDetailSerializer(ProductListSerializer):
    brand = serializers.CharField()
    specifications = serializers.SerializerMethodField()
    video = serializers.SerializerMethodField()
    order_limits = serializers.SerializerMethodField()
    digital = serializers.BooleanField(source="is_digital")
    quotes = serializers.BooleanField(source="quote_only", help_text="Buyers can request a quote for bulk quantities.")
    product_safety = serializers.SerializerMethodField()
    images = serializers.SerializerMethodField()
    variants = serializers.SerializerMethodField()
    stock = serializers.IntegerField()
    guide = serializers.SerializerMethodField()

    class Meta(ProductListSerializer.Meta):
        fields = ProductListSerializer.Meta.fields + ["description", "images", "stock", "variants", "guide",
                                                      "brand", "specifications", "video", "order_limits", "digital",
                                                      "quotes", "product_safety", "weight_grams", "updated_at"]

    def get_images(self, p) -> list[str]:
        request = self.context.get("request")
        return [absolute(request, p.image_url)] + [absolute(request, i.image_url) for i in p.extra_images.all()]

    def get_variants(self, p) -> list[dict]:
        return VariantSerializer(p.variants.all(), many=True).data if p.has_variants else []

    def get_guide(self, p) -> dict | None:
        return p.guide


class ReviewSerializer(serializers.ModelSerializer):
    author = serializers.CharField(source="username")
    verified_purchase = serializers.BooleanField(source="is_verified_purchase")

    class Meta:
        model = Review
        fields = ["id", "author", "rating", "comment", "verified_purchase", "seller_reply", "created_at"]


class SellerPublicSerializer(serializers.Serializer):
    def to_representation(self, s):
        request = self.context.get("request")
        data = SellerBriefSerializer(s, context=self.context).data
        data.update({
            "description": s.store_description,
            "country": s.country,
            "logo": absolute(request, s.store_logo.url) if s.store_logo else "",
            "banner": absolute(request, s.store_banner.url) if s.store_banner else "",
            "rating": {"average": float(s.average_rating or 0), "count": s.seller_reviews.count()},
            "on_vacation": s.vacation_mode,
            "vacation_message": s.vacation_message if s.vacation_mode else "",
            "member_since": s.created_at.isoformat(),
            "products": absolute(request, reverse("api:products") + f"?seller_id={s.id}"),
        })
        return data


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

class OrderItemSerializer(serializers.ModelSerializer):
    product_id = serializers.IntegerField(allow_null=True)
    name = serializers.CharField(source="product_name")
    option = serializers.SerializerMethodField()
    price = serializers.SerializerMethodField()
    subtotal = serializers.SerializerMethodField()
    status = serializers.CharField(source="fulfillment_status")
    seller = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = ["id", "product_id", "name", "option", "quantity", "price", "subtotal", "status", "seller",
                  "shipped_at", "delivered_at"]

    def get_option(self, i) -> str:
        return i.variant.label if i.variant_id and i.variant else ""

    def get_price(self, i) -> str:
        return money(i.price)

    def get_subtotal(self, i) -> str:
        return money(i.subtotal)

    def get_seller(self, i) -> str:
        return i.seller_account.display_name if i.seller_account_id else ""


def _order_common(order, request):
    return {
        "id": order.id,
        "number": order.shipment_number,
        "status": order.status,
        "status_label": order.get_status_display(),
        "payment": {"method": order.payment_method, "status": order.payment_status},
        "currency": (order.currency or "usd").upper(),
        "created_at": order.created_at.isoformat(),
        "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
        "tracking": {
            "courier": order.courier_name, "number": order.tracking_number or order.shipment_number,
            "url": absolute(request, order.tracking_link) if order.status in ("confirmed", "shipped", "delivered") else "",
        },
    }


class OrderSerializer(serializers.Serializer):
    """An order as its customer sees it."""

    def to_representation(self, order):
        request = self.context.get("request")
        data = _order_common(order, request)
        data.update({
            "items": OrderItemSerializer(order.items.all(), many=True).data,
            "totals": {
                "subtotal": money(order.subtotal), "discount": money(order.discount_amount),
                "shipping": money(order.shipping_amount), "tax": money(order.added_tax),
                "store_credit": money(order.credit_used), "total": money(order.total),
            },
            "ship_to": {"name": order.full_name, "address": order.address, "city": order.city, "state": order.state,
                        "postal_code": order.postal_code, "country": order.country, "phone": order.phone},
            "can_cancel": order.is_cancellable,
            "invoice_url": absolute(request, reverse("invoice_pdf", args=[order.id])),
            "timeline": [{"kind": e.kind, "text": e.text, "at": e.created_at.isoformat()}
                         for e in order.events.all() if e.public],
        })
        return data


class SellerOrderSerializer(serializers.Serializer):
    """An order as a seller sees it: only their own items and earnings."""

    def to_representation(self, order):
        request = self.context.get("request")
        seller = self.context["seller"]
        show_money = self.context.get("show_money", True)
        items = [i for i in order.items.all() if i.seller_account_id == seller.id]
        data = _order_common(order, request)
        data["items"] = OrderItemSerializer(items, many=True).data
        sale = sum((i.subtotal for i in items), Decimal("0"))
        data["your_sale"] = money(sale)
        if show_money:
            data["your_commission"] = money(sum((i.commission_amount for i in items), Decimal("0")))
            data["your_earnings"] = money(sum((i.seller_earning for i in items), Decimal("0")))
        data["ship_to"] = {"name": order.full_name, "address": order.address, "city": order.city, "state": order.state,
                           "postal_code": order.postal_code, "country": order.country, "phone": order.phone}
        data["cash_on_delivery"] = order.payment_method == "cod"
        from ..fulfilment import blocked_reason
        data["can_ship"] = not blocked_reason(order)
        data["ship_blocked_reason"] = blocked_reason(order)
        return data


# ---------------------------------------------------------------------------
# Seller products (writable)
# ---------------------------------------------------------------------------

class SellerProductSerializer(ProductDetailSerializer):
    status = serializers.CharField(source="approval_status")
    status_label = serializers.CharField(source="get_approval_status_display")

    class Meta(ProductDetailSerializer.Meta):
        fields = ProductDetailSerializer.Meta.fields + ["status", "status_label", "seo_title", "seo_description"]


class SellerProductWriteSerializer(serializers.Serializer):
    """Fields a seller may set. Same rules as the Seller Center and bulk
    import: new products, and changes to name / category / photo /
    description, go to the store's review; price and stock are live at once."""
    name = serializers.CharField(max_length=255, required=False)
    category = serializers.CharField(max_length=30, required=False)

    def validate_category(self, value):
        if value not in dict(category_choices()):
            raise serializers.ValidationError("Unknown category. See GET /api/v1/categories/.")
        return value
    price = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"), required=False)
    old_price = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0"), required=False, allow_null=True)
    stock = serializers.IntegerField(min_value=0, max_value=1_000_000, required=False)
    description = serializers.CharField(max_length=10000, required=False, allow_blank=True)
    image_url = serializers.URLField(max_length=500, required=False)
    weight_grams = serializers.IntegerField(min_value=0, max_value=100000, required=False)
    seo_title = serializers.CharField(max_length=70, required=False, allow_blank=True)
    seo_description = serializers.CharField(max_length=170, required=False, allow_blank=True)

    def as_row(self):
        """The validated data as a bulk-import row (strings), so the bulk
        rules decide what happens."""
        row = {}
        for key, value in self.validated_data.items():
            row[key] = "" if value is None else str(value)
        return row


class FulfilSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[c for c, _l in OrderItem.FULFILLMENT_CHOICES if c != "pending"],
                                     default="handed_to_courier")
    courier_name = serializers.CharField(max_length=60, required=False, allow_blank=True)
    tracking_number = serializers.CharField(max_length=60, required=False, allow_blank=True)


class TokenRequestSerializer(serializers.Serializer):
    username = serializers.CharField(help_text="Username or email address")
    password = serializers.CharField(style={"input_type": "password"}, trim_whitespace=False)
    code = serializers.CharField(required=False, allow_blank=True,
                                 help_text="6-digit authenticator code or a backup code (accounts with two-step sign-in)")
    name = serializers.CharField(required=False, max_length=60, help_text="A label for this token, e.g. 'iPhone app'")


class WishlistAddSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()


_ = SellerAccount  # imported for type references in docs
