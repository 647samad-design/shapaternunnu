import uuid

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

hex_color = RegexValidator(r"^#[0-9A-Fa-f]{6}$", "Enter a 6-digit hex colour like #0E3B43.")


def _private_storage():
    """Storage for sensitive seller documents (ID scans, business
    certificates). Points at the private Supabase bucket in production,
    and at a non-public folder locally - never at the public media URL."""
    from django.core.files.storage import storages
    return storages["private"]


def _public_upload_path(folder):
    def _path(instance, filename):
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
        return f"{folder}/{uuid.uuid4().hex}.{ext}"
    _path.__name__ = f"upload_to_{folder.replace('/', '_')}"
    return _path


def seller_cert_path(instance, filename):
    return _public_upload_path("seller_docs/certificates")(instance, filename)


def seller_id_path(instance, filename):
    return _public_upload_path("seller_docs/ids")(instance, filename)


def seller_logo_path(instance, filename):
    return _public_upload_path("seller_docs/logos")(instance, filename)


def message_image_path(instance, filename):
    return _public_upload_path("chat")(instance, filename)


def claim_photo_path(instance, filename):
    return _public_upload_path("claims")(instance, filename)


def seller_banner_path(instance, filename):
    return _public_upload_path("seller_docs/banners")(instance, filename)


class ProductQuerySet(models.QuerySet):
    def live(self):
        """Products customers can see and buy: approved by the store, and
        the seller (if any) isn't pending, rejected or suspended."""
        return self.filter(approval_status="approved").filter(
            models.Q(seller_account__isnull=True)
            | models.Q(seller_account__status="approved", seller_account__vacation_mode=False)
        )

    def listed(self):
        """Live products for browsing and search: other sellers' offers of
        the same product show on its page instead of as duplicates."""
        return self.live().filter(offer_of__isnull=True)

    def flash(self):
        """Live products in a running flash sale (ended deals drop out)."""
        return self.live().filter(is_flash_sale=True).filter(
            models.Q(flash_sale_ends__isnull=True) | models.Q(flash_sale_ends__gt=timezone.now()))


class Category(models.Model):
    """A shop category. Top-level categories show in the menu bar; a
    subcategory's products also show on its parent's page. Managed in
    Manage > Categories (the store can add, rename, nest and hide them)."""
    slug = models.SlugField(max_length=30, unique=True, help_text="Used in the address: /category/<slug>/. Can't be changed later.")
    name = models.CharField(max_length=60)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children",
                               help_text="Empty = top-level category (shown in the menu bar).")
    position = models.PositiveSmallIntegerField(default=0, help_text="Lower numbers come first.")
    image_url = models.CharField(max_length=500, blank=True, help_text="Optional picture for the category tile. Empty = a product photo.")
    description = models.CharField(max_length=300, blank=True, help_text="Shown at the top of the category page and in Google results.")
    is_active = models.BooleanField("Visible", default=True)
    size_chart = models.TextField(blank=True, help_text="Optional size guide for products in this category, one row per line, cells separated by | (first line = headings). Example: Size | Chest (cm) | Length (cm)")

    class Meta:
        ordering = ["position", "name"]
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class Page(models.Model):
    """An information page the store writes itself (Manage > Pages):
    About, Terms, Privacy, Sell with us (these replace the built-in text),
    or any new page such as a shipping policy or FAQ (/pages/<slug>/).
    Body: plain text; a line starting with "## " is a heading, a blank line
    starts a new paragraph, lines starting with "- " are a bullet list."""
    BUILT_IN = [("about", "About us"), ("terms", "Terms of Service"), ("privacy", "Privacy Policy"), ("sell", "Sell with us"),
                ("protection", "Buyer protection")]
    slug = models.SlugField(max_length=60, unique=True)
    title = models.CharField(max_length=120)
    body = models.TextField()
    meta_description = models.CharField(max_length=170, blank=True, help_text="Snippet in Google results. Empty = start of the page.")
    is_published = models.BooleanField("Published", default=True)
    show_in_footer = models.BooleanField(default=True, help_text="Link to it in the site footer.")
    position = models.PositiveSmallIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "title"]

    def __str__(self):
        return self.title

    @property
    def is_built_in(self):
        return self.slug in dict(self.BUILT_IN)


class HomeBanner(models.Model):
    """A promotion card on the home page, under the big welcome banner
    (Manage > Pages & banners). Up to three show at a time."""
    title = models.CharField(max_length=80)
    subtitle = models.CharField(max_length=160, blank=True)
    image_url = models.CharField("Picture URL", max_length=500, blank=True, help_text="Wide picture, about 1200 x 600. Empty = a coloured card.")
    link = models.CharField(max_length=300, help_text="Where it goes, e.g. /category/fashion/ or /search/?q=summer")
    button_label = models.CharField(max_length=30, default="Shop now")
    position = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField("Showing", default=True)
    starts_at = models.DateTimeField(null=True, blank=True, help_text="Optional: show from this time.")
    ends_at = models.DateTimeField(null=True, blank=True, help_text="Optional: hide after this time.")

    class Meta:
        ordering = ["position", "-id"]

    def __str__(self):
        return self.title


class QuoteRequest(models.Model):
    """Request for quotation (B2B): a buyer asks a seller for a price for a
    larger quantity; the seller offers a unit price, the buyer accepts (it
    goes to the cart at that price), counters or declines."""
    STATUS = [("open", "Waiting for the seller"), ("offered", "Offer received"), ("accepted", "Accepted - in cart"),
              ("ordered", "Ordered"), ("declined", "Declined"), ("cancelled", "Cancelled")]
    product = models.ForeignKey("Product", on_delete=models.CASCADE, related_name="quotes")
    variant = models.ForeignKey("ProductVariant", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    buyer = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="quotes")
    seller_account = models.ForeignKey("SellerAccount", null=True, blank=True, on_delete=models.CASCADE, related_name="quotes",
                                       help_text="Empty = the store's own product.")
    KINDS = [("quote", "Quote"), ("offer", "Offer"), ("auction", "Auction win")]
    kind = models.CharField(max_length=8, choices=KINDS, default="quote")
    quantity = models.PositiveIntegerField()
    target_price = models.DecimalField("Buyer's target unit price", max_digits=10, decimal_places=2, null=True, blank=True)
    company = models.CharField(max_length=120, blank=True)
    message = models.TextField(blank=True)
    offer_price = models.DecimalField("Offered unit price", max_digits=10, decimal_places=2, null=True, blank=True)
    offer_note = models.TextField(blank=True)
    offer_expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS, default="open", db_index=True)
    history = models.JSONField(default=list, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"Quote #{self.pk}: {self.quantity} x {self.product}"

    @property
    def offer_valid(self):
        from django.utils import timezone
        return self.offer_price is not None and (self.offer_expires_at is None or self.offer_expires_at > timezone.now())

    @property
    def offer_total(self):
        return (self.offer_price or 0) * self.quantity

    @property
    def list_total(self):
        base = self.variant.unit_price if self.variant_id and self.variant else self.product.price
        return base * self.quantity

    def log(self, who, text):
        from django.utils import timezone
        self.history = (self.history or []) + [{"who": who, "text": text[:600], "at": timezone.now().isoformat()}]


class StoreFollow(models.Model):
    """A shopper follows a seller's store to hear about new products."""
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="followed_stores")
    seller_account = models.ForeignKey("SellerAccount", on_delete=models.CASCADE, related_name="followers")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "seller_account"], name="one_follow_per_store")]


class Product(models.Model):
    name = models.CharField(max_length=255)
    image_url = models.CharField(max_length=500)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    old_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    discount_percent = models.PositiveIntegerField(default=0)
    category = models.CharField(max_length=30, default="grocery", db_index=True,
                                help_text="Category slug (see Manage > Categories).")
    description = models.TextField(blank=True)
    is_flash_sale = models.BooleanField(default=False, db_index=True)
    flash_sale_ends = models.DateTimeField(null=True, blank=True,
                                           help_text="When the deal ends. A countdown is shown until then; empty = no end date.")
    # Quantity offer, e.g. "Buy 3 or more, save 10%".
    bulk_min_qty = models.PositiveSmallIntegerField(null=True, blank=True)
    bulk_percent = models.PositiveSmallIntegerField(default=0)
    # Buying guide shown on the product page: what it's for, pros and cons
    # (one point per line). Written by the seller, or drafted by AI.
    guide_uses = models.TextField(blank=True)
    guide_pros = models.TextField(blank=True)
    guide_cons = models.TextField(blank=True)
    guide_source = models.CharField(max_length=10, blank=True)  # "seller" or "ai"
    stock = models.PositiveIntegerField(default=50)
    seller_name = models.CharField(max_length=100, default="Official Store", db_index=True)
    seller_account = models.ForeignKey("SellerAccount", related_name="products", on_delete=models.SET_NULL, null=True, blank=True)
    APPROVAL_CHOICES = [
        ("approved", "Approved"),
        ("pending", "Pending review"),
        ("rejected", "Rejected"),
        ("archived", "Removed by seller"),
    ]
    weight_grams = models.PositiveIntegerField(null=True, blank=True, help_text="Packed weight in grams, for courier labels and rates.")
    length_cm = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)
    width_cm = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)
    height_cm = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)
    approval_status = models.CharField(max_length=20, choices=APPROVAL_CHOICES, default="approved", db_index=True)
    brand = models.CharField(max_length=80, blank=True, db_index=True, help_text="Brand or maker shown on the product and used in filters.")
    gtin = models.CharField("Barcode (GTIN)", max_length=14, blank=True, help_text="EAN, UPC, ISBN or JAN number from the package. Google Shopping asks for it on branded products.")
    mpn = models.CharField("Manufacturer part number", max_length=70, blank=True, help_text="Used by Google Shopping when there is no barcode.")
    specs = models.TextField("Specifications", blank=True, help_text="One per line as 'Name: value', e.g. 'Material: Cotton'. Shown as a table on the product page.")
    video_url = models.CharField("Video", max_length=300, blank=True, help_text="YouTube or Vimeo link, or a direct .mp4 address. Shown in the product gallery.")
    min_order_qty = models.PositiveSmallIntegerField("Minimum order", default=1, help_text="Fewest a customer can buy in one order.")
    max_order_qty = models.PositiveSmallIntegerField("Maximum order", null=True, blank=True, help_text="Most a customer can buy in one order. Empty = no limit.")
    quote_only = models.BooleanField("Request a quote", default=False, help_text="Business buyers can ask for a price for larger quantities (on top of normal buying).")
    # Digital products: delivered as a download once the order is paid online.
    is_digital = models.BooleanField("Digital download", default=False)
    digital_file = models.FileField(upload_to="digital/%Y/%m/", storage=_private_storage, blank=True, null=True)
    download_limit = models.PositiveSmallIntegerField(default=5, help_text="How many times a buyer can download the file.")
    CONDITIONS = [("new", "New"), ("refurbished", "Refurbished"), ("used_like_new", "Used - like new"),
                  ("used_good", "Used - good"), ("used_fair", "Used - acceptable")]
    condition = models.CharField(max_length=16, choices=CONDITIONS, default="new", db_index=True)
    ships_from = models.CharField("Ships from (country code)", max_length=2, blank=True, help_text="2-letter code, e.g. PT. Empty = the seller's country.")
    personalization_prompt = models.CharField("Personalization", max_length=120, blank=True,
                                              help_text="Ask buyers for custom text, e.g. 'Name to engrave (max 12 letters)'. Empty = off.")
    personalization_required = models.BooleanField(default=False)
    personalization_max = models.PositiveSmallIntegerField(default=60)
    accepts_offers = models.BooleanField("Accept offers", default=False, help_text="Buyers can offer their own price (Make an offer).")
    offer_floor = models.DecimalField("Auto-decline offers below", max_digits=10, decimal_places=2, null=True, blank=True)
    offer_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="other_offers",
                                 help_text="This listing is another seller's offer of that product (shown under 'Other sellers').")
    subscribe_percent = models.PositiveSmallIntegerField("Subscribe & Save discount (%)", default=0, help_text="0 = no subscriptions.")
    is_auction = models.BooleanField("Auction", default=False)
    auction_start_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    auction_reserve = models.DecimalField("Reserve price", max_digits=10, decimal_places=2, null=True, blank=True,
                                          help_text="Lowest price you'll sell for (hidden). Empty = none.")
    auction_ends_at = models.DateTimeField(null=True, blank=True, db_index=True)
    auction_closed = models.BooleanField(default=False, editable=False)
    # EU General Product Safety Regulation (GPSR): who made it and who is responsible in the EU.
    manufacturer = models.CharField(max_length=300, blank=True, help_text="Manufacturer's name, postal address and email or website.")
    eu_responsible_person = models.CharField("EU responsible person", max_length=300, blank=True, help_text="Name, address and contact of the person in the EU responsible for this product (needed when the maker is outside the EU).")
    safety_info = models.TextField("Safety information", blank=True, help_text="Warnings, age limits and safe-use instructions.")
    seo_title = models.CharField("SEO title", max_length=70, blank=True, help_text="Title in Google results (max 70). Empty = product name.")
    seo_description = models.CharField("SEO description", max_length=170, blank=True, help_text="Snippet in Google results (max 160). Empty = start of the description.")
    updated_at = models.DateTimeField(auto_now=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["approval_status", "category"]),
            models.Index(fields=["approval_status", "is_flash_sale"]),
            models.Index(fields=["stock"]),
        ]


    objects = ProductQuerySet.as_manager()
    # True when the product is sold in sizes/colours (see ProductVariant);
    # ``stock`` is then the total of all variants.
    has_variants = models.BooleanField(default=False)

    def spec_rows(self):
        """[(name, value)] from the specifications text."""
        rows = []
        for line in (self.specs or "").splitlines():
            if ":" in line:
                name, value = line.split(":", 1)
                if name.strip() and value.strip():
                    rows.append((name.strip()[:60], value.strip()[:300]))
        return rows

    def video(self):
        """{"kind": "youtube"|"vimeo"|"file", "src": ...} or None."""
        import re
        url = (self.video_url or "").strip()
        if not url.startswith("https://"):
            return None
        m = re.match(r"https://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{6,20})", url)
        if m:
            return {"kind": "youtube", "src": f"https://www.youtube-nocookie.com/embed/{m.group(1)}", "id": m.group(1)}
        m = re.match(r"https://(?:www\.|player\.)?vimeo\.com/(?:video/)?(\d{5,12})", url)
        if m:
            return {"kind": "vimeo", "src": f"https://player.vimeo.com/video/{m.group(1)}"}
        if re.search(r"\.(mp4|webm)(\?.*)?$", url, re.I):
            return {"kind": "file", "src": url}
        return None

    def size_chart(self):
        from . import categories
        return categories.size_chart(self.category)

    @property
    def has_safety_info(self):
        return bool(self.manufacturer or self.eu_responsible_person or self.safety_info)

    def order_limits(self):
        """(minimum, maximum or None) a customer may buy in one order."""
        low = max(1, self.min_order_qty or 1)
        high = self.max_order_qty or None
        if high is not None and high < low:
            high = low
        return low, high

    def get_category_display(self):
        from . import categories
        return categories.label(self.category)

    def sync_variants(self):
        """Keeps ``stock`` and ``has_variants`` in line with the variants."""
        from django.db.models import Sum
        total = self.variants.aggregate(n=Sum("stock"))["n"]
        has = total is not None
        Product.objects.filter(pk=self.pk).update(has_variants=has, **({"stock": total} if has else {}))
        self.has_variants = has
        if has:
            self.stock = total

    @property
    def flash_active(self):
        return self.is_flash_sale and (self.flash_sale_ends is None or self.flash_sale_ends > timezone.now())

    @property
    def flash_countdown(self):
        """True when a deal with an end date is running (show a timer)."""
        return self.flash_active and self.flash_sale_ends is not None

    @staticmethod
    def _lines(text):
        return [line.strip(" •-*\t") for line in (text or "").splitlines() if line.strip(" •-*\t")]

    @property
    def guide(self):
        """{"uses": [...], "pros": [...], "cons": [...]} or None."""
        data = {"uses": self._lines(self.guide_uses), "pros": self._lines(self.guide_pros), "cons": self._lines(self.guide_cons)}
        return data if any(data.values()) else None

    @property
    def has_bulk_offer(self):
        return bool(self.bulk_min_qty and self.bulk_min_qty > 1 and self.bulk_percent)

    def bulk_unit_price(self, unit, qty):
        """Unit price after the quantity offer for ``qty`` pieces."""
        from decimal import Decimal, ROUND_HALF_UP
        if self.has_bulk_offer and qty >= self.bulk_min_qty:
            return (Decimal(unit) * (100 - self.bulk_percent) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return unit

    @property
    def is_live(self):
        if self.approval_status != "approved":
            return False
        if not self.seller_account_id:
            return True
        return self.seller_account.status == "approved" and not self.seller_account.vacation_mode

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        old_stock = None
        if not is_new:
            old_stock = Product.objects.filter(pk=self.pk).values_list("stock", flat=True).first()
        super().save(*args, **kwargs)
        # Notify the seller once when stock crosses into the low-stock zone,
        # not on every save while it stays low.
        if not is_new and old_stock is not None and self.seller_account_id:
            crossed_low = old_stock > 5 and 0 < self.stock <= 5
            crossed_out = old_stock > 0 and self.stock <= 0
            if crossed_out:
                Notification.objects.create(
                    user=self.seller_account.user,
                    message=f"'{self.name}' is now out of stock. Restock it to keep selling.",
                    link="/seller/products/?tab=out",
                )
            elif crossed_low:
                Notification.objects.create(
                    user=self.seller_account.user,
                    message=f"'{self.name}' is running low ({self.stock} left). Consider restocking soon.",
                    link="/seller/products/?tab=low",
                )

    def __str__(self):
        return self.name

    @property
    def average_rating(self):
        if hasattr(self, "avg_rating"):
            return round(self.avg_rating, 1) if self.avg_rating else 0
        reviews = self.reviews.all()
        if not reviews:
            return 0
        return round(sum(r.rating for r in reviews) / len(reviews), 1)

    @property
    def rating_count(self):
        if hasattr(self, "review_count"):
            return self.review_count
        return self.reviews.count()


class ProductVariant(models.Model):
    """One buyable version of a product, e.g. size M in red. Each has its
    own stock and, optionally, its own price."""
    product = models.ForeignKey(Product, related_name="variants", on_delete=models.CASCADE)
    size = models.CharField(max_length=40, blank=True)
    color = models.CharField(max_length=40, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True,
                                help_text="Leave empty to use the product price.")
    stock = models.PositiveIntegerField(default=0)
    sku = models.CharField("SKU", max_length=60, blank=True)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return f"{self.product.name} ({self.label})"

    @property
    def label(self):
        return " / ".join(part for part in (self.size, self.color) if part) or "Standard"

    @property
    def unit_price(self):
        return self.price if self.price is not None else self.product.price


class StockAlert(models.Model):
    """"Email me when it's back" request for a sold-out product (or one
    size/colour of it)."""
    product = models.ForeignKey(Product, related_name="stock_alerts", on_delete=models.CASCADE)
    variant = models.ForeignKey(ProductVariant, null=True, blank=True, related_name="stock_alerts", on_delete=models.CASCADE)
    email = models.EmailField()
    user = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    notified_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.email} -> {self.product.name}"


class Review(models.Model):
    product = models.ForeignKey(Product, related_name="reviews", on_delete=models.CASCADE)
    seller_reply = models.TextField(blank=True, max_length=1000)
    seller_reply_at = models.DateTimeField(null=True, blank=True)
    user = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="reviews")
    username = models.CharField(max_length=100)
    rating = models.PositiveSmallIntegerField(default=5)
    comment = models.TextField()
    is_verified_purchase = models.BooleanField(default=False)
    helpful_count = models.PositiveIntegerField(default=0)
    photo_bonus = models.BooleanField(default=False, help_text="Reward points for adding a photo were given.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.username} on {self.product.name}"


class Order(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("confirmed", "Confirmed"),
        ("shipped", "Shipped"),
        ("delivered", "Delivered"),
        ("cancelled", "Cancelled"),
    ]

    language = models.CharField(max_length=8, blank=True, help_text="Site language when ordering; emails use it.")
    # Automation bookkeeping (see bees.automation): when reminders went out.
    late_reminded_at = models.DateTimeField(null=True, blank=True, editable=False)
    late_escalated_at = models.DateTimeField(null=True, blank=True, editable=False)
    arrival_reminded_at = models.DateTimeField(null=True, blank=True, editable=False)

    user = models.ForeignKey("auth.User", related_name="orders", on_delete=models.CASCADE, null=True, blank=True)
    guest_email = models.EmailField(blank=True)
    email = models.EmailField(blank=True)
    full_name = models.CharField(max_length=150)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField("State / Province / Region", max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=2, blank=True, help_text="ISO 3166-1 alpha-2 country code, e.g. US")
    phone = models.CharField(max_length=30)
    PAYMENT_METHOD_CHOICES = [
        ("card", "Card (Stripe)"),
        ("paypal", "PayPal"),
        ("cod", "Cash on delivery"),
    ]
    payment_method = models.CharField(max_length=30, choices=PAYMENT_METHOD_CHOICES, default="card")
    PAYMENT_STATUS_CHOICES = [
        ("not_applicable", "Not applicable (COD)"),
        ("pending", "Awaiting payment"),
        ("paid", "Paid"),
        ("failed", "Failed"),
        ("refunded", "Refunded"),
    ]
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default="not_applicable", db_index=True)
    stripe_session_id = models.CharField(max_length=255, blank=True, db_index=True)
    # True while a cash-on-delivery order is being paid online in advance:
    # if that payment is abandoned, the order goes back to cash on delivery
    # instead of being cancelled.
    cod_fallback = models.BooleanField(default=False)
    stripe_payment_intent = models.CharField(max_length=255, blank=True)
    paypal_order_id = models.CharField(max_length=64, blank=True, db_index=True)
    cod_otp_hash = models.CharField(max_length=64, blank=True, editable=False)
    cod_otp_sent_at = models.DateTimeField(null=True, blank=True, editable=False)
    cod_otp_tries = models.PositiveSmallIntegerField(default=0, editable=False)
    cod_refused = models.BooleanField(default=False, help_text="Customer refused / didn't collect a cash-on-delivery parcel.")
    paypal_capture_id = models.CharField(max_length=64, blank=True)
    currency = models.CharField(max_length=3, default="usd")
    # Charged in the shopper's own currency (Store settings > "Charge in the
    # shopper's currency"): the converted total and the rate used, so
    # refunds go back in the same currency.
    charge_currency = models.CharField(max_length=3, blank=True)
    charge_rate = models.DecimalField(max_digits=16, decimal_places=6, null=True, blank=True)
    charge_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    shipping_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax_name = models.CharField(max_length=30, blank=True)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    tax_included = models.BooleanField(default=False, help_text="Prices already included this tax (shown, not added).")
    customer_tax_id = models.CharField("Customer VAT / tax number", max_length=40, blank=True)
    review_request_sent = models.BooleanField(default=False, help_text="The 'how was it? leave a review' email went out.")
    affiliate_code = models.CharField(max_length=16, blank=True, db_index=True, help_text="Code of the affiliate whose link brought this order.")
    affiliate_paid = models.BooleanField(default=False, editable=False)
    affiliate_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0, editable=False)
    gift_wrap = models.BooleanField(default=False)
    gift_message = models.CharField(max_length=300, blank=True)
    pickup_point = models.ForeignKey("PickupPoint", null=True, blank=True, on_delete=models.SET_NULL, related_name="orders")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending", db_index=True)
    coupon_code = models.CharField(max_length=30, blank=True)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tracking_number = models.CharField(max_length=60, blank=True)
    courier_name = models.CharField(max_length=60, blank=True)
    tracking_url = models.CharField(max_length=300, blank=True,
                                    help_text="Optional. Leave empty to build the link from the courier and tracking number.")
    estimated_delivery = models.DateField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    # Store credit spent on this order (a payment method, so it doesn't
    # change what the goods cost) and whether it was given back on cancel.
    credit_used = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    credit_returned = models.BooleanField(default=False)
    # Secret for the tracking link emailed to guest customers.
    access_token = models.CharField(max_length=32, blank=True, db_index=True)
    # Set once the sellers in this order have been told to ship it (when a
    # cash-on-delivery order is placed, or a card order is paid).
    sellers_notified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # Shipment: every order gets its own shipment number, which doubles as
    # the tracking number until a courier's own number is added.
    shipment_number = models.CharField(max_length=24, unique=True, null=True, blank=True)
    # Card used (for "refunded to Visa •••• 4242") - from Stripe, never the full number.
    card_brand = models.CharField(max_length=20, blank=True)
    card_last4 = models.CharField(max_length=4, blank=True)
    refund_id = models.CharField(max_length=80, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=160, blank=True)
    # Cash on delivery: confirmed by the customer (unverified emails get a
    # confirmation link) and marked collected when the courier delivers.
    cod_confirmed = models.BooleanField(default=True)
    cod_collected_at = models.DateTimeField(null=True, blank=True)

    CANCELLABLE_STATUSES = ("pending", "confirmed")

    class Meta:
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["email"]),  # guest orders, coupon limits, COD checks, reports
            models.Index(fields=["payment_method", "cod_confirmed", "status"]),
        ]

    @property
    def is_cancellable(self):
        return self.status in self.CANCELLABLE_STATUSES

    @property
    def subtotal(self):
        return sum((item.subtotal for item in self.items.all()), 0)

    @property
    def grand_total(self):
        """Order value: items - discount + shipping + tax."""
        return max(self.subtotal - self.discount_amount, 0) + self.shipping_amount + self.added_tax

    @property
    def added_tax(self):
        """Tax on top of the prices (0 when prices already include it)."""
        return 0 if self.tax_included else self.tax_amount

    @property
    def tax_label(self):
        name = self.tax_name or "Tax"
        rate = f" {self.tax_rate.normalize():f}%" if self.tax_rate else ""
        return f"{name}{rate}" + (" (included)" if self.tax_included else "")

    @property
    def total(self):
        """What the customer pays by card / cash: order value minus any
        store credit used."""
        return max(self.grand_total - self.credit_used, 0)

    @property
    def goods_paid(self):
        """What the items cost after the coupon, including their tax -
        the basis for refunding returned items."""
        return max(self.subtotal - self.discount_amount, 0) + self.added_tax

    def refund_value(self, item):
        """Fair refund for one returned item: its share of what was paid
        for the goods (so a coupon discount isn't refunded as cash)."""
        from decimal import Decimal, ROUND_HALF_UP
        subtotal = Decimal(self.subtotal)
        if not subtotal:
            return Decimal("0.00")
        value = Decimal(self.goods_paid) * Decimal(item.subtotal) / subtotal
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def return_deadline(self):
        from datetime import timedelta
        delivered = self.delivered_at or self.created_at
        return delivered + timedelta(days=SiteSettings.load().return_days)

    @property
    def can_return(self):
        from django.utils import timezone
        return self.status == "delivered" and timezone.now() <= self.return_deadline()

    @property
    def total_cents(self):
        from .payments import to_cents
        return to_cents(self.total, self.currency)

    @property
    def pay_currency(self):
        """Currency the card / PayPal payment is in."""
        return (self.charge_currency or self.currency).lower()

    @property
    def pay_amount(self):
        return self.charge_amount if self.charge_currency and self.charge_amount is not None else self.total

    @property
    def pay_cents(self):
        from .payments import to_cents
        return to_cents(self.pay_amount, self.pay_currency)

    def to_charge_currency(self, amount):
        """A store-currency amount (e.g. a partial refund) in the currency
        the customer paid in, never more than they paid."""
        from decimal import Decimal, ROUND_HALF_UP
        if not self.charge_currency or not self.charge_rate:
            return Decimal(str(amount))
        from .templatetags.bees_extras import ZERO_DECIMAL
        step = Decimal("1") if self.charge_currency.lower() in ZERO_DECIMAL else Decimal("0.01")
        value = (Decimal(str(amount)) * self.charge_rate).quantize(step, rounding=ROUND_HALF_UP)
        return min(value, self.pay_amount)

    @property
    def tracking_link(self):
        from .tracking import tracking_link
        if self.uses_store_tracking:
            from .order_emails import absolute
            return absolute(self.public_tracking_path)
        return tracking_link(self.courier_name, self.tracking_number, self.tracking_url)

    @property
    def uses_store_tracking(self):
        """No courier number yet (or the courier number is our shipment number):
        the customer follows the parcel on our own tracking page."""
        return bool(self.shipment_number) and (not self.tracking_number or self.tracking_number == self.shipment_number) \
            and not self.tracking_url and self.status in ("confirmed", "shipped", "delivered")

    @property
    def public_tracking_path(self):
        from django.urls import reverse
        return reverse("track_shipment", args=[self.shipment_number]) if self.shipment_number else ""

    @property
    def items_on_the_way(self):
        """True when some items have shipped but aren't marked delivered."""
        if self.status in ("cancelled", "delivered", "pending"):
            return False
        return self.status == "shipped" or any(i.fulfillment_status == "handed_to_courier" for i in self.items.all())

    @property
    def card_label(self):
        if self.payment_method == "paypal":
            return "PayPal"
        return f"{self.card_brand.title()} •••• {self.card_last4}" if self.card_last4 else ""

    @property
    def contact_email(self):
        if self.email:
            return self.email
        if self.user and self.user.email:
            return self.user.email
        return self.guest_email

    @property
    def is_guest(self):
        return self.user_id is None

    def tracking_path(self):
        from django.urls import reverse
        return reverse("order_track", args=[self.pk, self.access_token])

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        if not self.shipment_number:
            from .tracking import new_shipment_number
            self.shipment_number = new_shipment_number()
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = list(kwargs["update_fields"]) + ["shipment_number"]
        if self.status == "delivered" and self.payment_method == "cod" and self.payment_status == "not_applicable" and not self.cod_collected_at:
            from django.utils import timezone as _tz
            self.cod_collected_at = _tz.now()
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = list(kwargs["update_fields"]) + ["cod_collected_at"]
        if not self.access_token:
            import secrets
            self.access_token = secrets.token_urlsafe(16)[:32]
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = list(kwargs["update_fields"]) + ["access_token"]
        old_status = None
        if not is_new:
            old_status = Order.objects.filter(pk=self.pk).values_list("status", flat=True).first()
        if self.status == "delivered" and old_status != "delivered" and not self.delivered_at:
            from django.utils import timezone
            self.delivered_at = timezone.now()
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = list(kwargs["update_fields"]) + ["delivered_at"]
        super().save(*args, **kwargs)
        if is_new:
            OrderEvent.objects.create(order=self, kind="placed", text="Order placed")
        elif old_status and old_status != self.status:
            OrderEvent.log_status(self, old_status)
        if not is_new and old_status and old_status != self.status:
            from . import order_emails, seller_center
            order_emails.status_changed(self, old_status, self.status)
            seller_center.order_status_changed(self, old_status, self.status)
            from django.db import transaction as _tx
            from . import sms
            _tx.on_commit(lambda o=self, st=self.status: sms.order_status(o, st))
        if not is_new and old_status != "delivered" and self.status == "delivered" and self.affiliate_code:
            from django.db import transaction as _tx2
            from . import loyalty as _loyalty
            _tx2.on_commit(lambda o=self: _loyalty.pay_affiliate(o))
        if self.user and not is_new and old_status and old_status != self.status:
            Notification.objects.create(
                user=self.user,
                message=f"Order #{self.id} is now {self.get_status_display()}.",
                link=f"/my-orders/",
            )
            if self.status == "delivered" and old_status != "delivered":
                _reward_referrer_for(self.user)
                # 1 loyalty point per whole unit of currency spent, more for higher tiers.
                from . import loyalty
                points_earned = loyalty.points_for(self)
                if points_earned > 0:
                    profile, _ = Profile.objects.get_or_create(user=self.user)
                    profile.loyalty_points += points_earned
                    profile.save(update_fields=["loyalty_points"])
                    Notification.objects.create(
                        user=self.user,
                        message=f"You earned {points_earned} loyalty points from order #{self.id}!",
                        link="/profile/",
                    )

    def __str__(self):
        return f"Order #{self.id} - {self.user.username if self.user else self.contact_email}"


def _reward_referrer_for(user):
    """Gives the person who referred ``user`` a one-time 10% coupon, the
    first time one of ``user``'s orders is delivered."""
    import secrets
    from datetime import timedelta
    from django.utils import timezone
    updated = Profile.objects.filter(user=user, referral_rewarded=False).exclude(referred_by="").update(referral_rewarded=True)
    if not updated:
        return
    ref = Profile.objects.filter(user=user).values_list("referred_by", flat=True).first()
    referrer = Profile.objects.filter(referral_code=ref).exclude(user=user).select_related("user").first()
    if not referrer:
        return
    site = SiteSettings.load()
    if not site.referral_enabled or not site.referral_reward_percent:
        return
    percent = site.referral_reward_percent
    code = "REF-" + secrets.token_hex(3).upper()
    Coupon.objects.create(code=code, percent_off=percent, usage_limit=1, per_user_limit=1,
                          expiry_date=timezone.localdate() + timedelta(days=90),
                          owner=referrer.user, purpose="referral_reward")
    Notification.objects.create(
        user=referrer.user, link="/account/referrals/",
        message=f"{user.username} made their first purchase with your referral link! Here's {percent}% off your next order: {code}",
    )


class OrderEvent(models.Model):
    """Timeline of an order, shown to the customer on the tracking page."""
    KIND_CHOICES = [
        ("placed", "Order placed"), ("paid", "Payment received"), ("confirmed", "Confirmed"),
        ("packed", "Packed"), ("shipped", "Shipped"), ("delivered", "Delivered"), ("cancelled", "Cancelled"),
        ("refunded", "Refunded"), ("cod", "Cash collected"), ("note", "Update"),
    ]
    order = models.ForeignKey("Order", related_name="events", on_delete=models.CASCADE)
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default="note")
    text = models.CharField(max_length=200)
    public = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"#{self.order_id} {self.kind}: {self.text}"

    STATUS_TEXT = {
        "confirmed": "Order confirmed by the seller",
        "shipped": "Handed to the courier",
        "delivered": "Delivered",
        "cancelled": "Order cancelled",
        "pending": "Order back to pending",
    }

    @classmethod
    def log_status(cls, order, old_status):
        text = cls.STATUS_TEXT.get(order.status, f"Status: {order.get_status_display()}")
        if order.status == "shipped" and order.courier_name:
            text = f"Handed to {order.courier_name}"
        if order.status == "cancelled" and order.cancel_reason:
            text = f"Order cancelled ({order.cancel_reason})"
        cls.objects.create(order=order, kind=order.status if order.status in dict(cls.KIND_CHOICES) else "note", text=text[:200])
        if order.status == "delivered" and order.cod_collected_at and order.payment_method == "cod":
            cls.objects.create(order=order, kind="cod", text="Cash payment collected on delivery")


class OrderItemQuerySet(models.QuerySet):
    def counted(self):
        """Items that count as real sales: the order wasn't cancelled, its
        payment didn't fail or get refunded, and the item wasn't refunded
        through a return."""
        return (
            self.exclude(order__status="cancelled")
            .exclude(order__payment_status__in=["pending", "failed", "refunded"])
            .exclude(return_requests__status="refunded")
        )


class OrderItem(models.Model):
    order = models.ForeignKey(Order, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True)
    product_name = models.CharField(max_length=255)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.PositiveIntegerField(default=1)
    # Recorded when the order is placed, so later rate changes or product
    # deletions never rewrite past earnings.
    seller_account = models.ForeignKey(
        "SellerAccount", related_name="sold_items", on_delete=models.SET_NULL, null=True, blank=True,
        help_text="Empty for the store's own products (no commission).",
    )
    commission_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    commission_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    variant = models.ForeignKey("ProductVariant", null=True, blank=True, on_delete=models.SET_NULL, related_name="order_items")

    objects = OrderItemQuerySet.as_manager()
    FULFILLMENT_CHOICES = [
        ("pending", "Pending"),
        ("packed", "Packed"),
        ("handed_to_courier", "Handed to courier"),
        ("delivered", "Delivered"),
    ]
    fulfillment_status = models.CharField(max_length=20, choices=FULFILLMENT_CHOICES, default="pending", db_index=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    shipped_at = models.DateTimeField(null=True, blank=True)
    downloads = models.PositiveSmallIntegerField(default=0, editable=False)
    personalization = models.CharField(max_length=300, blank=True)
    quote = models.ForeignKey("QuoteRequest", null=True, blank=True, on_delete=models.SET_NULL, related_name="order_items")

    @property
    def is_digital(self):
        return bool(self.product_id and self.product and self.product.is_digital)

    @property
    def can_download(self):
        return (self.is_digital and bool(self.product.digital_file) and self.order.payment_status == "paid"
                and self.order.status != "cancelled" and self.downloads < self.product.download_limit)

    @property
    def subtotal(self):
        return self.price * self.quantity

    @property
    def received_at(self):
        """When this item reached the customer (its own delivery, or the
        whole order's), or None."""
        if self.delivered_at:
            return self.delivered_at
        return self.order.delivered_at if self.order.status == "delivered" else None

    def return_deadline(self):
        from datetime import timedelta
        received = self.received_at
        return received + timedelta(days=SiteSettings.load().return_days) if received else None

    @property
    def can_return(self):
        from django.utils import timezone
        if self.is_digital:
            return False  # downloads can't be sent back (buyers agree to this at checkout)
        deadline = self.return_deadline()
        return bool(deadline) and self.order.status != "cancelled" and timezone.now() <= deadline

    @property
    def open_claim(self):
        return next((c for c in self.claims.all() if c.is_open), None)

    @property
    def seller_earning(self):
        return self.subtotal - self.commission_amount

    def apply_commission(self, seller):
        """Sets seller and commission from the seller's current rate."""
        from decimal import Decimal, ROUND_HALF_UP
        self.seller_account = seller
        rate = Decimal(str(seller.effective_commission_rate)) if seller else Decimal("0")
        self.commission_rate = rate
        self.commission_amount = (self.price * self.quantity * rate / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if seller is not None:
            seller.__dict__.pop("_totals_cache", None)  # totals change once this item is saved

    def __str__(self):
        return f"{self.quantity} x {self.product_name}"


class Wishlist(models.Model):
    user = models.ForeignKey("auth.User", related_name="wishlist_items", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    list_name = models.CharField(max_length=40, blank=True, default="", help_text="Empty = the main wishlist.")
    created_at = models.DateTimeField(auto_now_add=True)
    # Price-drop alerts compare against the last price the shopper saw.
    price_seen = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    alerted_at = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        if self.price_seen is None and self.product_id:
            self.price_seen = self.product.price
        super().save(*args, **kwargs)

    class Meta:
        unique_together = ("user", "product")

    def __str__(self):
        return f"{self.user.username} ♥ {self.product.name}"


class Coupon(models.Model):
    PURPOSE_CHOICES = [("", "Store coupon"), ("referral_welcome", "Referral welcome"), ("referral_reward", "Referral reward")]
    code = models.CharField(max_length=30, unique=True)
    # Referral coupons belong to one customer (shown on their referrals page).
    owner = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="owned_coupons")
    purpose = models.CharField(max_length=20, choices=PURPOSE_CHOICES, blank=True, default="")
    # A seller's own code: only their products get cheaper, and the seller
    # pays for it (their line prices are reduced). Empty = store-wide code.
    seller_account = models.ForeignKey("SellerAccount", null=True, blank=True, on_delete=models.CASCADE, related_name="coupons")
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    percent_off = models.PositiveIntegerField(default=10)
    free_shipping = models.BooleanField(default=False, help_text="Also (or only, with 0%) makes shipping free.")
    is_public = models.BooleanField("Show in the voucher centre", default=False, help_text="Shoppers can collect it on /vouchers/ and the store page.")
    title = models.CharField(max_length=80, blank=True, help_text="Shown in the voucher centre, e.g. 'Weekend deal'.")
    active = models.BooleanField(default=True)
    expiry_date = models.DateField(
        null=True, blank=True,
        help_text="Coupon stops working after this date. Leave blank for no expiry.",
    )
    usage_limit = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Maximum number of times this code can be used in total, across all customers. Leave blank for unlimited.",
    )
    per_user_limit = models.PositiveIntegerField(
        default=1,
        help_text="Maximum number of times a single customer can use this code.",
    )
    min_order_value = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Cart total must be at least this much for the code to apply. 0 = no minimum.",
    )

    def __str__(self):
        return f"{self.code} (-{self.percent_off}%)"

    def unit_after(self, price):
        """A seller-coupon line price after the discount."""
        from decimal import Decimal, ROUND_HALF_UP
        return (Decimal(price) * Decimal(100 - self.percent_off) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def applies_to(self, product):
        return not self.seller_account_id or product.seller_account_id == self.seller_account_id

    def discount_for(self, lines):
        """(eligible subtotal, discount) for [(product, unit_price, qty)]."""
        from decimal import Decimal, ROUND_HALF_UP
        eligible = sum((Decimal(price) * qty for product, price, qty in lines if self.applies_to(product)), Decimal("0"))
        if self.seller_account_id:
            off = sum(((Decimal(price) - self.unit_after(price)) * qty for product, price, qty in lines if self.applies_to(product)), Decimal("0"))
        else:
            off = (eligible * Decimal(self.percent_off) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return eligible, off

    def times_used(self):
        return Order.objects.filter(coupon_code__iexact=self.code).exclude(status="cancelled").count()

    def times_used_by(self, user, email=None):
        orders = Order.objects.filter(coupon_code__iexact=self.code).exclude(status="cancelled")
        if user and user.is_authenticated:
            return orders.filter(user=user).count()
        if email:
            # Guests are counted by the email they check out with.
            return orders.filter(models.Q(email__iexact=email) | models.Q(guest_email__iexact=email)).count()
        return 0

    def is_valid_for(self, user, order_total, email=None):
        """Returns (is_valid, error_message). error_message is None if valid."""
        from django.utils import timezone

        if not self.active:
            return False, "This coupon is no longer active."
        if self.expiry_date and timezone.localdate() > self.expiry_date:
            return False, "This coupon has expired."
        if order_total < self.min_order_value:
            from .templatetags.bees_extras import money
            return False, f"This coupon needs a minimum order of {money(self.min_order_value)}."
        if self.usage_limit is not None and self.times_used() >= self.usage_limit:
            return False, "This coupon has reached its usage limit."
        if self.times_used_by(user, email) >= self.per_user_limit:
            return False, "You've already used this coupon the maximum number of times."
        return True, None


class ProductImage(models.Model):
    product = models.ForeignKey(Product, related_name="extra_images", on_delete=models.CASCADE)
    image_url = models.CharField(max_length=500)

    def __str__(self):
        return f"Image for {self.product.name}"


class Profile(models.Model):
    user = models.OneToOneField("auth.User", related_name="profile", on_delete=models.CASCADE)
    phone = models.CharField(max_length=30, blank=True)
    email_verified = models.BooleanField(default=False)
    referral_code = models.CharField(max_length=12, unique=True, blank=True)
    referred_by = models.CharField(max_length=12, blank=True)
    # The referrer is rewarded once, when this customer's first order is
    # delivered (so fake sign-ups earn nothing).
    referral_rewarded = models.BooleanField(default=False)
    loyalty_points = models.PositiveIntegerField(default=0)
    language = models.CharField(max_length=8, blank=True, help_text="Language for emails (the one picked on the site).")
    sms_updates = models.BooleanField(default=True, help_text="Text messages about orders.")
    staff_role = models.CharField(max_length=12, blank=True, help_text="Store team role (only for staff). Empty = manager.")
    price_drop_alerts = models.BooleanField(default=True, help_text="Email when something in the wishlist gets cheaper.")
    store_credit = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    wishlist_token = models.CharField(max_length=24, blank=True, help_text="Set when the wishlist is shared.")
    last_checkin = models.DateField(null=True, blank=True)
    checkin_streak = models.PositiveSmallIntegerField(default=0)
    member_until = models.DateTimeField(null=True, blank=True, help_text="Paid membership ends at this time.")
    # Stripe customer that holds this person's saved cards. Card numbers
    # never touch our servers - Stripe stores them.
    stripe_customer_id = models.CharField(max_length=255, blank=True)
    # Cart kept with the account so it follows the customer between
    # devices, and for the "you left something in your cart" reminder.
    saved_cart = models.JSONField(default=dict, blank=True)
    cart_updated_at = models.DateTimeField(null=True, blank=True, db_index=True)
    cart_reminder_sent = models.BooleanField(default=False)
    cart_reminders = models.BooleanField(default=True, help_text="Email a reminder about items left in the cart.")
    review_emails = models.BooleanField(default=True, help_text="Email asking for a review a few days after delivery.")
    # Two-step sign-in (authenticator app).
    totp_secret = models.CharField(max_length=64, blank=True)
    totp_enabled = models.BooleanField(default=False)
    totp_last_step = models.BigIntegerField(default=0)
    backup_codes = models.JSONField(default=list, blank=True)

    POINTS_PER_UNIT = 100  # 100 reward points = 1.00 of store credit

    def save(self, *args, **kwargs):
        # The referral code is unique, so a blank one would clash as soon as
        # a second profile is created without one (e.g. when points are
        # awarded on delivery to a customer who never opened their profile).
        if not self.referral_code:
            import secrets
            while True:
                code = secrets.token_hex(4).upper()
                if not Profile.objects.filter(referral_code=code).exists():
                    break
            self.referral_code = code
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = list(kwargs["update_fields"]) + ["referral_code"]
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user.username}'s profile"


class Address(models.Model):
    user = models.ForeignKey("auth.User", related_name="addresses", on_delete=models.CASCADE)
    label = models.CharField(max_length=30, default="Home")
    full_name = models.CharField(max_length=150)
    phone = models.CharField(max_length=30)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=2, blank=True)
    is_default = models.BooleanField(default=False)

    class Meta:
        verbose_name_plural = "Addresses"
        ordering = ["-is_default", "id"]

    def __str__(self):
        return f"{self.label} - {self.user.username}"


class Question(models.Model):
    product = models.ForeignKey(Product, related_name="questions", on_delete=models.CASCADE)
    user = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="questions")
    username = models.CharField(max_length=100)
    question = models.TextField()
    answer = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Q on {self.product.name}: {self.question[:40]}"


class NewsletterSubscriber(models.Model):
    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.email


class Notification(models.Model):
    user = models.ForeignKey("auth.User", related_name="notifications", on_delete=models.CASCADE)
    message = models.CharField(max_length=255)
    link = models.CharField(max_length=255, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "is_read"])]

    def __str__(self):
        return self.message


class SearchLog(models.Model):
    query = models.CharField(max_length=150, unique=True)
    count = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["-count"]

    def __str__(self):
        return f"{self.query} ({self.count})"


class SellerAccount(models.Model):
    ACCOUNT_TYPE_CHOICES = [
        ("individual", "Individual seller"),
        ("organization", "Organization / Business"),
    ]
    STATUS_CHOICES = [
        ("pending", "Pending approval"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("suspended", "Suspended"),
    ]

    user = models.OneToOneField("auth.User", related_name="seller_account", on_delete=models.CASCADE)
    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPE_CHOICES, default="individual", db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending", db_index=True)
    commission_rate = models.DecimalField(max_digits=5, decimal_places=2, default=10)
    total_paid_out = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Total amount already paid to this seller (recorded by admin after each payout)."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # Registration details (per marketplace onboarding spec)
    full_name = models.CharField(max_length=150, blank=True)
    business_name = models.CharField(max_length=150, blank=True)
    organization_name = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    cnic = models.CharField("National ID / Passport number", max_length=30, blank=True)
    business_address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    pickup_state = models.CharField("State / province", max_length=100, blank=True)
    pickup_postal_code = models.CharField("Postal code", max_length=20, blank=True)
    store_description = models.TextField(blank=True)
    product_categories = models.CharField(max_length=255, blank=True, help_text="Comma-separated categories the store will sell")
    brand_info = models.TextField(blank=True)
    tax_info = models.CharField(max_length=100, blank=True)
    bank_details = models.CharField(max_length=255, blank=True)
    PAYOUT_METHODS = [("manual", "Bank / manual transfer"), ("paypal", "PayPal (automatic)"), ("stripe", "Stripe (automatic, to your bank)")]
    payout_method = models.CharField(max_length=10, choices=PAYOUT_METHODS, default="manual")
    terms_accepted_at = models.DateTimeField(null=True, blank=True, help_text="When the seller accepted the seller terms.")
    ad_spend = models.DecimalField(max_digits=12, decimal_places=2, default=0, editable=False,
                                   help_text="Sponsored-product clicks charged to this seller (taken from earnings).")
    paypal_email = models.EmailField(blank=True, help_text="PayPal account that receives automatic payouts.")
    stripe_account_id = models.CharField(max_length=64, blank=True, editable=False)
    stripe_payouts_ready = models.BooleanField(default=False, editable=False)

    business_certificate = models.FileField(upload_to=seller_cert_path, storage=_private_storage, blank=True, null=True)
    id_document = models.FileField(upload_to=seller_id_path, storage=_private_storage, blank=True, null=True)
    # Seller performance (bees.performance), refreshed daily.
    performance_score = models.PositiveSmallIntegerField(null=True, blank=True)
    performance_level = models.CharField(max_length=12, blank=True)
    performance_data = models.JSONField(default=dict, blank=True)
    performance_warned_at = models.DateTimeField(null=True, blank=True)
    quick_replies = models.TextField(
        blank=True, default="Thanks for your message! I'll get back to you shortly.\nYes, it's in stock and ready to ship.\nIt usually arrives in 3-7 working days.\nYes, we ship to your country.",
        help_text="Saved replies for Messages, one per line.")
    company_registration = models.CharField("Company registration number", max_length=60, blank=True)
    whatsapp = models.CharField("WhatsApp number", max_length=20, blank=True, help_text="Optional. Shows 'Chat on WhatsApp' on your store page.")
    digest_emails = models.BooleanField("Daily summary email", default=True)
    trader_email = models.EmailField("Public contact email", blank=True)
    trader_declared_at = models.DateTimeField(null=True, blank=True, help_text="When the seller confirmed it only sells products that follow EU law (DSA).")
    store_logo = models.ImageField(upload_to=seller_logo_path, blank=True, null=True)
    store_banner = models.ImageField(upload_to=seller_banner_path, blank=True, null=True)

    admin_note = models.CharField(max_length=255, blank=True, help_text="Internal note, e.g. reason for rejection or requested info")

    # Holiday mode: the store's products are hidden from the shop until the
    # seller switches it off again.
    vacation_mode = models.BooleanField(default=False)
    vacation_message = models.CharField(max_length=200, blank=True)

    # Seller plan (see SellerPlan). A paid plan is active until
    # ``plan_expires_at``; free plans never expire.
    plan = models.ForeignKey("SellerPlan", null=True, blank=True, on_delete=models.SET_NULL, related_name="sellers")
    plan_expires_at = models.DateTimeField(null=True, blank=True)
    plan_reminder_sent = models.BooleanField(default=False)

    # Badges: "Verified" is given by staff after checking ID / business
    # papers; "Top seller" is worked out every day from sales and ratings.
    verified_at = models.DateTimeField(null=True, blank=True)
    top_seller = models.BooleanField(default=False, db_index=True)

    def save(self, *args, **kwargs):
        if self.pk is None and not kwargs.get("update_fields"):
            site = SiteSettings.load()
            self.commission_rate = (
                site.commission_organization if self.account_type == "organization" else site.commission_individual
            )
        super().save(*args, **kwargs)

    @property
    def active_plan(self):
        """The plan the seller is on right now: their paid plan while it
        hasn't run out, otherwise the store's free plan (or None)."""
        if not hasattr(self, "_active_plan"):
            plan = self.plan if self.plan_id and self.plan.active else None
            if plan and plan.price > 0 and not (self.plan_expires_at and self.plan_expires_at > timezone.now()):
                plan = None
            self._active_plan = plan or SellerPlan.free_plan()
        return self._active_plan

    @property
    def product_limit(self):
        """Most products the seller may list (None = no limit)."""
        plan = self.active_plan
        return plan.product_limit if plan else None

    @property
    def can_add_product(self):
        limit = self.product_limit
        return limit is None or self.products.count() < limit

    @property
    def is_verified(self):
        return self.verified_at is not None

    @property
    def has_badge(self):
        plan = self.active_plan
        return bool(plan and plan.badge)

    def _totals(self):
        if not hasattr(self, "_totals_cache"):
            from django.db.models import Sum, F
            agg = OrderItem.objects.filter(seller_account=self).counted().aggregate(
                sales=Sum(F("price") * F("quantity")), commission=Sum("commission_amount"),
            )
            self._totals_cache = (agg["sales"] or 0, agg["commission"] or 0)
        return self._totals_cache

    @property
    def commission_total(self):
        """Commission the platform has earned from this seller's sales."""
        return self._totals()[1]

    @property
    def net_earnings(self):
        """Seller's total earnings after the platform's commission is deducted."""
        sales, commission = self._totals()
        return round(float(sales) - float(commission), 2)

    @property
    def amount_owed(self):
        """Net earnings not yet paid out to the seller (minus sponsored-product
        costs, which are paid from earnings). Never negative."""
        owed = float(self.net_earnings) - float(self.total_paid_out) - float(self.ad_spend or 0)
        return round(max(owed, 0), 2)

    @property
    def display_name(self):
        return self.business_name or self.organization_name or self.full_name or self.user.username

    @property
    def lifetime_sales(self):
        """Value of this seller's completed (not cancelled or refunded) sales."""
        return self._totals()[0]

    @property
    def effective_commission_rate(self):
        """
        Tiered commission: the more a seller sells, the lower their commission rate,
        rewarding high-volume sellers. Base rate is the individual/organization rate;
        thresholds reduce it as lifetime sales grow.
        """
        base = float(self.commission_rate)
        sales = float(self.lifetime_sales)
        discount = 0
        for threshold, reduction in getattr(settings, "COMMISSION_TIERS", []):
            if sales >= threshold:
                discount = reduction
                break
        rate = max(base - discount, 3)
        plan = self.active_plan
        if plan and plan.commission_discount:
            rate = max(rate - float(plan.commission_discount), 0)
        return rate

    @property
    def average_rating(self):
        reviews = self.seller_reviews.all()
        if not reviews:
            return 0
        return round(sum(r.rating for r in reviews) / len(reviews), 1)

    def __str__(self):
        return f"{self.display_name} ({self.get_account_type_display()}, {self.status})"


class SellerPlan(models.Model):
    """Monthly plans sellers can buy for a lower commission, more products
    and a badge. The cheapest free plan is what every seller starts on."""
    name = models.CharField(max_length=40)
    price = models.DecimalField("Price per month", max_digits=8, decimal_places=2, default=0,
                                help_text="0 = free plan.")
    commission_discount = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text="Percentage points taken off the seller's commission, e.g. 3 turns 10% into 7%.")
    product_limit = models.PositiveIntegerField(null=True, blank=True, help_text="Leave empty for unlimited products.")
    badge = models.BooleanField(default=False, help_text="Show a 'Pro seller' badge on the store and its products.")
    perks = models.TextField(blank=True, help_text="Extra benefits shown on the plan card, one per line.")
    highlight = models.BooleanField("Most popular", default=False)
    position = models.PositiveSmallIntegerField(default=0)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["position", "price"]

    def __str__(self):
        return self.name

    @property
    def is_free(self):
        return not self.price or self.price <= 0

    @property
    def perk_list(self):
        return [line.strip() for line in self.perks.splitlines() if line.strip()]

    @classmethod
    def free_plan(cls):
        return cls.objects.filter(active=True, price__lte=0).order_by("position", "id").first()


class PlanPayment(models.Model):
    """One month (or more) of a seller plan, paid by card or recorded by staff."""
    STATUS_CHOICES = [("pending", "Waiting for payment"), ("paid", "Paid"), ("failed", "Not completed")]
    METHOD_CHOICES = [("card", "Card"), ("manual", "Recorded by staff")]

    seller = models.ForeignKey("SellerAccount", related_name="plan_payments", on_delete=models.CASCADE)
    plan = models.ForeignKey(SellerPlan, null=True, on_delete=models.SET_NULL, related_name="payments")
    plan_name = models.CharField(max_length=40)
    months = models.PositiveSmallIntegerField(default=1)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending", db_index=True)
    method = models.CharField(max_length=10, choices=METHOD_CHOICES, default="card")
    stripe_session_id = models.CharField(max_length=255, blank=True, db_index=True)
    created_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.plan_name} x{self.months} for {self.seller} ({self.status})"

    def activate(self):
        """Marks the payment paid and extends the seller's plan. Safe to
        call twice (the second call does nothing). Returns True if it
        changed anything."""
        from datetime import timedelta
        from django.db import transaction
        with transaction.atomic():
            me = PlanPayment.objects.select_for_update().get(pk=self.pk)
            if me.status == "paid":
                return False
            seller = SellerAccount.objects.select_for_update().get(pk=me.seller_id)
            now = timezone.now()
            start = now
            if seller.plan_id == me.plan_id and seller.plan_expires_at and seller.plan_expires_at > now:
                start = seller.plan_expires_at  # renewing early adds to the time left
            seller.plan_id = me.plan_id
            seller.plan_expires_at = start + timedelta(days=30 * me.months)
            seller.plan_reminder_sent = False
            seller.save(update_fields=["plan", "plan_expires_at", "plan_reminder_sent"])
            me.status, me.paid_at = "paid", now
            me.save(update_fields=["status", "paid_at"])
        self.status, self.paid_at = me.status, me.paid_at
        Notification.objects.create(
            user=seller.user,
            message=f"Your {me.plan_name} plan is active until {seller.plan_expires_at:%b %d, %Y}.",
            link="/seller/plan/",
        )
        return True


class Payout(models.Model):
    """Money sent (or asked for) from the store to a seller. Paid payouts add
    up to SellerAccount.total_paid_out."""
    STATUS_CHOICES = [
        ("requested", "Requested"),
        ("processing", "Sending"),
        ("paid", "Paid"),
        ("failed", "Failed"),
        ("cancelled", "Cancelled"),
    ]
    METHOD_CHOICES = [
        ("bank", "Bank transfer"),
        ("paypal", "PayPal"),
        ("stripe", "Stripe"),
        ("wise", "Wise"),
        ("payoneer", "Payoneer"),
        ("other", "Other"),
    ]
    seller = models.ForeignKey(SellerAccount, related_name="payouts", on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="requested", db_index=True)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, blank=True)
    reference = models.CharField("Transfer reference", max_length=120, blank=True)
    note = models.CharField(max_length=255, blank=True)
    automatic = models.BooleanField(default=False)
    error = models.CharField(max_length=255, blank=True)
    requested_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    recorded_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Payout {self.amount} to {self.seller.display_name} ({self.status})"


class OrganizationMember(models.Model):
    """Lets an organization account grant additional team members access to
    its seller dashboard, product management, and order fulfillment -
    without sharing the main login."""
    ROLE_CHOICES = [
        ("admin", "Admin - full access, can manage team"),
        ("staff", "Staff - manage products & orders only"),
    ]
    organization = models.ForeignKey(SellerAccount, related_name="team_members", on_delete=models.CASCADE)
    user = models.ForeignKey("auth.User", related_name="organization_memberships", on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="staff")
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("organization", "user")

    def __str__(self):
        return f"{self.user.username} @ {self.organization.display_name} ({self.role})"


class SellerReview(models.Model):
    seller = models.ForeignKey(SellerAccount, related_name="seller_reviews", on_delete=models.CASCADE)
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE)
    rating = models.PositiveSmallIntegerField(default=5)
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("seller", "user")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.username} rated {self.seller.display_name} {self.rating}/5"


def _money(value):
    from .templatetags.bees_extras import money
    return money(value)


class ReturnRequest(models.Model):
    STATUS_CHOICES = [
        ("requested", "Requested"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("refunded", "Refunded"),
    ]
    REFUND_METHOD_CHOICES = [
        ("original_payment", "Original payment method"),
        ("store_credit", "Store credit"),
    ]
    order_item = models.ForeignKey("OrderItem", related_name="return_requests", on_delete=models.CASCADE)
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE)
    reason = models.TextField()
    refund_method = models.CharField(max_length=20, choices=REFUND_METHOD_CHOICES, default="original_payment")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="requested", db_index=True)
    admin_note = models.CharField(max_length=255, blank=True)
    # Filled in when refunded: how much went back to the card and how much
    # became store credit.
    card_refund_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    credit_refund_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def refund_total(self):
        return self.card_refund_amount + self.credit_refund_amount

    def refund_plan(self, as_credit=False):
        """How this return would be refunded: {"value", "card", "credit",
        "manual"}. ``value`` is the item's share of what was actually paid.
        Card orders go back to the card (up to what's left on it; anything
        paid with store credit goes back as credit). Cash-on-delivery orders
        can't be refunded to a card: ``manual`` is the amount the store pays
        back itself (bank transfer / cash), unless store credit is chosen."""
        from decimal import Decimal
        order = self.order_item.order
        value = order.refund_value(self.order_item)
        plan = {"value": value, "card": Decimal("0"), "credit": Decimal("0"), "manual": Decimal("0")}
        if as_credit or self.refund_method == "store_credit":
            plan["credit"] = value
        elif order.payment_status == "paid" and (order.stripe_payment_intent or order.paypal_capture_id):
            already = sum(
                (r.card_refund_amount for r in ReturnRequest.objects.filter(
                    order_item__order=order, status="refunded").exclude(pk=self.pk)),
                Decimal("0"),
            )
            plan["card"] = min(value, max(order.total - already, Decimal("0")))
            plan["credit"] = value - plan["card"]
        else:
            plan["manual"] = value
        return plan

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        old_status = None
        if not is_new:
            old_status = ReturnRequest.objects.filter(pk=self.pk).values_list("status", flat=True).first()
        super().save(*args, **kwargs)
        if not is_new and old_status and old_status != self.status:
            messages_by_status = {
                "approved": f"Your return for '{self.order_item.product_name}' was approved. Refund is being processed via {self.get_refund_method_display()}.",
                "rejected": f"Your return request for '{self.order_item.product_name}' was rejected." + (f" Note: {self.admin_note}" if self.admin_note else ""),
                "refunded": self._refunded_message(),
            }
            msg = messages_by_status.get(self.status)
            if msg:
                Notification.objects.create(user=self.user, message=msg, link="/my-orders/")

    def _refunded_message(self):
        parts = []
        if self.card_refund_amount:
            parts.append(f"{_money(self.card_refund_amount)} to your card")
        if self.credit_refund_amount:
            parts.append(f"{_money(self.credit_refund_amount)} as store credit")
        how = " and ".join(parts) if parts else "as agreed with our support team"
        return f"Your refund for '{self.order_item.product_name}' has been issued: {how}."

    def __str__(self):
        return f"Return: {self.order_item.product_name} ({self.status})"


class ShippingZone(models.Model):
    """Shipping fee for a group of countries. A zone whose countries are
    "*" covers every country not listed in another zone ("rest of the
    world"). With no zones at all, the flat fee in Settings applies
    everywhere."""
    name = models.CharField(max_length=60, help_text="e.g. United States, Europe, Rest of world")
    countries = models.TextField(help_text="Two-letter country codes separated by commas (US, CA), or * for every other country.")
    fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    courier = models.CharField("Preferred courier", max_length=60, blank=True,
                               help_text="Courier used for this zone. Empty = the best-known courier for each country is suggested.")
    per_item_fee = models.DecimalField("Each extra item", max_digits=10, decimal_places=2, default=0,
                                       help_text="Added for every item after the first. 0 = same fee for any number of items.")
    free_over = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True,
                                    help_text="Orders at or above this amount (after discounts) ship free. Empty = never free.")
    delivery_days = models.PositiveSmallIntegerField(null=True, blank=True,
                                                     help_text="Usual delivery time in business days. Empty = store default.")
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def is_rest_of_world(self):
        return self.countries.strip() == "*"

    @property
    def country_codes(self):
        return [c.strip().upper() for c in self.countries.replace("\n", ",").split(",") if c.strip() and c.strip() != "*"]

    def fee_for(self, amount, items=1):
        from decimal import Decimal
        if self.free_over is not None and amount >= self.free_over:
            return Decimal("0")
        extra = max(int(items or 1) - 1, 0)
        return self.fee + (self.per_item_fee or 0) * extra


_SETTINGS_MEMO = {"obj": None, "until": 0.0}


class SiteSettings(models.Model):
    """A single-row table for site-wide settings, editable from the admin
    panel. Everything brand-related lives here so the store can be
    re-branded (white-labelled) without touching code."""
    # --- Brand ---
    site_name = models.CharField("Store name", max_length=100, default="Lumen Market")
    tagline = models.CharField(max_length=160, default="Thoughtfully chosen products, delivered worldwide.")
    logo_url = models.CharField(
        max_length=500, blank=True,
        help_text="Full URL of your logo (upload it to Supabase Storage or any image host). Leave blank to show the store name as text.",
    )
    logo_file = models.ImageField(upload_to="branding/", blank=True, null=True, help_text="Or upload a logo here (PNG/SVG/JPG).")
    favicon_url = models.CharField(max_length=500, blank=True)
    primary_color = models.CharField(max_length=7, default="#0E3B43", validators=[hex_color], help_text="Main brand colour (buttons, footer). Use a dark colour so white text stays readable. Hex, e.g. #0E3B43")
    accent_color = models.CharField(max_length=7, default="#F2B33D", validators=[hex_color], help_text="Accent colour (badges, highlights, announcement bar). Use a light/bright colour - dark text sits on it. Hex, e.g. #F2B33D")
    hero_title = models.CharField(max_length=120, default="Everyday essentials, beautifully curated")
    hero_subtitle = models.CharField(max_length=240, default="Shop trusted brands and independent sellers. Secure checkout, fast shipping and easy returns.")
    hero_image_url = models.CharField(max_length=500, blank=True)
    # --- Contact & social ---
    support_email = models.EmailField(blank=True)
    support_phone = models.CharField(max_length=40, blank=True)
    company_address = models.CharField(max_length=255, blank=True)
    facebook_url = models.URLField(blank=True)
    instagram_url = models.URLField(blank=True)
    twitter_url = models.URLField("X / Twitter URL", blank=True)
    youtube_url = models.URLField(blank=True)
    # --- Commerce ---
    tax_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0, help_text="Default tax for countries without their own rate (Store settings > Tax rates). 0 = none.")
    sms_customer_updates = models.BooleanField(default=True, help_text="Text customers when an order is confirmed, shipped and delivered (needs Twilio keys).")
    sms_seller_alerts = models.BooleanField(default=True, help_text="Text sellers when they get a new order (needs Twilio keys).")
    cod_phone_check = models.BooleanField(default=False, help_text="Cash on delivery orders must be confirmed with a code sent by SMS to the customer's phone (needs Twilio keys). Stops fake orders.")
    cod_max_order = models.DecimalField(max_digits=10, decimal_places=2, default=0, help_text="Largest order allowed with cash on delivery (0 = no limit). Bigger orders must be paid online.")
    cod_refusal_limit = models.PositiveSmallIntegerField(default=2, help_text="After this many refused cash parcels a customer's phone and email can only pay online (0 = never block).")
    AUTO_PAYOUTS = [("off", "Off - I pay sellers by hand"), ("weekly", "Every Monday"), ("daily", "Every day")]
    auto_payouts = models.CharField("Automatic seller payouts", max_length=8, choices=AUTO_PAYOUTS, default="off",
                                    help_text="Sends each seller's available balance to their PayPal or Stripe account (sellers choose in Seller Center > Earnings). Runs with the daily tasks.")
    min_auto_payout = models.DecimalField(max_digits=10, decimal_places=2, default=20, help_text="Only send automatic payouts of at least this amount.")
    charge_local_currency = models.BooleanField("Charge in the shopper's currency", default=False,
                                                help_text="Card and PayPal payments are taken in the currency the shopper picked (at today's rate in Currencies), so their bank shows exactly that amount. Your Stripe/PayPal account converts it back.")
    ads_enabled = models.BooleanField("Sponsored products", default=True, help_text="Sellers can pay per click to show products at the top of search and category pages. Paid from their earnings.")
    ads_min_bid = models.DecimalField("Lowest cost per click", max_digits=6, decimal_places=2, default="0.10")
    ads_slots = models.PositiveSmallIntegerField("Sponsored places per page", default=2)
    bnpl_enabled = models.BooleanField("Show 'pay in instalments'", default=False,
                                       help_text="Turn on after enabling Klarna / Afterpay / Affirm in your Stripe Dashboard. Shows 'Pay in 4' on product pages.")
    bnpl_instalments = models.PositiveSmallIntegerField("Instalments shown", default=4)
    gift_wrap_enabled = models.BooleanField("Gift options at checkout", default=True, help_text="Free gift wrap and a gift message (prices hidden on the packing slip).")
    membership_enabled = models.BooleanField("Paid membership", default=False, help_text="Shoppers pay for free shipping and member prices (needs card payments).")
    membership_name = models.CharField(max_length=40, default="Plus")
    membership_price = models.DecimalField(max_digits=8, decimal_places=2, default="4.99")
    membership_days = models.PositiveSmallIntegerField("Membership length (days)", default=30)
    membership_discount = models.PositiveSmallIntegerField("Member discount (%)", default=0, help_text="Extra % off for members on every order (the store pays it). 0 = only free shipping.")
    whatsapp_number = models.CharField("Store WhatsApp number", max_length=20, blank=True,
                                       help_text="With country code, e.g. +923001234567. Shows a 'Chat on WhatsApp' button for customers.")
    message_channel = models.CharField("Send order texts by", max_length=10, choices=[("sms", "SMS"), ("whatsapp", "WhatsApp")], default="sms",
                                       help_text="WhatsApp needs a WhatsApp sender in Twilio (TWILIO_WHATSAPP_FROM) and approved message templates.")
    auto_approve_trusted = models.BooleanField("Auto-approve trusted sellers", default=False,
                                               help_text="New and edited products from verified sellers with Good or Excellent performance go live without review.")
    daily_digest = models.BooleanField("Daily summary emails", default=True,
                                       help_text="Every morning: the owner gets yesterday's sales and to-dos, sellers get their orders to ship and questions to answer.")
    affiliate_percent = models.PositiveSmallIntegerField("Affiliate commission (%)", default=0,
                                                         help_text="Anyone who shares a link with their code earns this % of delivered orders as store credit. 0 = off.")
    review_requests = models.BooleanField("Ask for reviews after delivery", default=True, help_text="A few days after delivery, email the customer a link to review what they bought.")
    review_photo_points = models.PositiveSmallIntegerField("Points for a photo review", default=50, help_text="Reward points for a verified review with a photo. 0 = off.")
    ai_agents = models.BooleanField("Let AI shopping assistants use the store", default=True, help_text="Publishes the store for AI agents (Google Gemini, AI Mode and others using the Universal Commerce Protocol). Agents build the cart; the shopper pays on your checkout.")
    checkin_points = models.PositiveSmallIntegerField("Daily check-in coins", default=5, help_text="Reward points for visiting each day (7-day streak gets double). 0 = off.")
    gift_cards_enabled = models.BooleanField(default=True, help_text="Sell digital gift cards (needs card or PayPal payments). Redeemed as store credit.")
    require_seller_2fa = models.BooleanField(default=False, help_text="Sellers must turn on two-step sign-in before using Seller Center.")
    cookie_banner = models.BooleanField(default=True, help_text="Ask visitors before using analytics/marketing cookies (required in the EU/UK).")
    ga4_id = models.CharField("Google Analytics 4 ID", max_length=20, blank=True, help_text="e.g. G-ABC123XYZ. Only loads after the visitor accepts cookies.")
    meta_pixel_id = models.CharField("Meta (Facebook) Pixel ID", max_length=20, blank=True, help_text="Only loads after the visitor accepts cookies.")
    google_site_verification = models.CharField(max_length=100, blank=True, help_text="Google Search Console verification code (content of the meta tag).")
    prices_include_tax = models.BooleanField(default=False, help_text="On: product prices already include tax (usual in Europe, UK, Australia) - checkout shows it, doesn't add it. Off: tax is added at checkout (usual in the US).")
    store_tax_id = models.CharField("Store VAT / tax number", max_length=40, blank=True, help_text="Printed on invoices.")
    DUTIES = [("customer", "Customer pays import duties on delivery (DAP)"), ("included", "Prices include duties - we pay them (DDP)")]
    duties_policy = models.CharField(max_length=10, choices=DUTIES, default="customer",
                                     help_text="What international shoppers see at checkout about customs duties.")
    shipping_flat_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0, help_text="Flat shipping fee per order. 0 = free shipping.")
    free_shipping_threshold = models.DecimalField(max_digits=10, decimal_places=2, default=0, help_text="Orders at or above this subtotal ship free. 0 = disabled.")
    return_days = models.PositiveSmallIntegerField(
        default=14, help_text="How many days after delivery customers can request a return.",
    )
    SHIPPING_PROVIDERS = [("", "Off - add tracking numbers by hand"), ("easypost", "EasyPost (100+ carriers worldwide)"),
                          ("shippo", "Shippo (DHL, UPS, FedEx, USPS and more)")]
    shipping_provider = models.CharField(max_length=20, choices=SHIPPING_PROVIDERS, blank=True,
                                         help_text="Courier service for booking pickups, printing labels and live tracking. Add its API key under Store settings > Keys & passwords.")
    ship_from_name = models.CharField("Store's shipping name", max_length=120, blank=True)
    ship_from_street = models.CharField("Store's street address", max_length=200, blank=True, help_text="Pickup address for the store's own products.")
    ship_from_city = models.CharField(max_length=100, blank=True)
    ship_from_state = models.CharField(max_length=100, blank=True)
    ship_from_postal_code = models.CharField(max_length=20, blank=True)
    ship_from_country = models.CharField(max_length=2, blank=True, help_text="2-letter code, e.g. PK, AE, GB, US")
    ship_from_phone = models.CharField(max_length=30, blank=True)
    default_weight_grams = models.PositiveIntegerField(default=500, help_text="Used when a product has no weight.")
    auto_deliver_days = models.PositiveSmallIntegerField(
        default=14, help_text="Items still 'on the way' this many days after shipping are marked delivered automatically (the customer is reminded 2 days before). 0 = off.")
    auto_cancel_days = models.PositiveSmallIntegerField(
        default=7, help_text="Paid orders nobody has shipped after this many days are cancelled and refunded automatically. Sellers are reminded after 2 days, the store team after 4. 0 = off.")
    claim_reply_days = models.PositiveSmallIntegerField(
        default=3, help_text="Days a seller has to answer a customer's problem report before the store team decides.")
    delivery_days = models.PositiveSmallIntegerField(
        default=5, help_text="Usual delivery time in business days. Used for the 'Arrives by' date customers see and get emailed.",
    )
    stripe_last_webhook = models.DateTimeField(null=True, blank=True, editable=False)
    require_staff_2fa = models.BooleanField(
        "Require two-step sign-in for staff", default=True,
        help_text="Staff must use an authenticator app code to open the store admin.",
    )
    commission_individual = models.DecimalField(
        "Commission for individual sellers (%)", max_digits=5, decimal_places=2, default=10,
        help_text="Taken from each sale by new individual sellers. Existing sellers keep their own rate.")
    commission_organization = models.DecimalField(
        "Commission for organizations (%)", max_digits=5, decimal_places=2, default=20,
        help_text="Taken from each sale by new business / organization sellers.")
    setup_completed = models.BooleanField(default=False, editable=False)
    referral_enabled = models.BooleanField("Referral program on", default=True,
                                           help_text="Customers share a link; friends get a welcome discount and the customer is rewarded after the friend's first delivered order.")
    referral_friend_percent = models.PositiveSmallIntegerField("Friend's welcome discount (%)", default=10)
    referral_reward_percent = models.PositiveSmallIntegerField("Reward for the customer who shared (%)", default=10)
    allow_guest_checkout = models.BooleanField(
        "Allow checkout without an account", default=False,
        help_text="Off (recommended): shoppers sign in or create an account before ordering, so every order has a verified customer, order history and support chat.")
    allow_cash_on_delivery = models.BooleanField(default=True, help_text="Show 'Cash on delivery' at checkout. Card payments appear automatically once Stripe keys are set.")
    show_language_menu = models.BooleanField(default=True, help_text="Let shoppers choose the shop language (English, Arabic, Spanish, French, Urdu, Roman Urdu) in the header and footer.")
    banner_text = models.CharField(
        max_length=200, blank=True,
        help_text="Shown as a site-wide announcement bar at the top of every page, e.g. 'Summer sale: 20% off everything'. Leave blank to hide it.",
    )
    banner_active = models.BooleanField(default=False)
    banner_link = models.CharField(max_length=300, blank=True, help_text="Optional URL the banner links to (e.g. a sale category page).")

    class Meta:
        verbose_name = "Site settings"
        verbose_name_plural = "Site settings"

    def __str__(self):
        return "Site settings"

    CACHE_KEY = "site_settings:v1"

    @classmethod
    def load(cls):
        """Site settings are read many times on every page, so they're kept
        in this worker's memory for a few seconds, in the shared cache for a
        minute, and cleared whenever they're saved."""
        import time
        from django.core.cache import cache
        memo = _SETTINGS_MEMO.get("obj")
        if memo is not None and _SETTINGS_MEMO["until"] > time.monotonic():
            return memo
        obj = cache.get(cls.CACHE_KEY)
        if obj is None:
            obj, _ = cls.objects.get_or_create(pk=1)
            cache.set(cls.CACHE_KEY, obj, 60)
        _SETTINGS_MEMO.update(obj=obj, until=time.monotonic() + 5)
        return obj

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        from django.core.cache import cache
        cache.delete(self.CACHE_KEY)
        _SETTINGS_MEMO.update(obj=None, until=0.0)

    @property
    def logo(self):
        if self.logo_file:
            try:
                return self.logo_file.url
            except Exception:
                return ""
        return self.logo_url

    def shipping_for(self, subtotal):
        from decimal import Decimal
        if self.free_shipping_threshold and subtotal >= self.free_shipping_threshold:
            return Decimal("0")
        return Decimal(self.shipping_flat_fee or 0)


class Currency(models.Model):
    """Extra currencies shoppers can see prices in. Prices are converted
    for display only; checkout charges the store currency (STORE_CURRENCY)."""
    code = models.CharField(max_length=3, unique=True, help_text="Three letters, e.g. EUR")
    symbol = models.CharField(max_length=6, help_text="Shown before the amount, e.g. €")
    rate = models.DecimalField(max_digits=14, decimal_places=6,
                               help_text="How many of this currency one unit of the store currency buys.")
    decimals = models.PositiveSmallIntegerField(default=2)
    active = models.BooleanField(default=False)
    position = models.PositiveSmallIntegerField(default=0)
    updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["position", "code"]
        verbose_name_plural = "Currencies"

    def __str__(self):
        return self.code

    def save(self, *args, **kwargs):
        self.code = self.code.upper()
        super().save(*args, **kwargs)
        from django.core.cache import cache
        cache.delete("currencies:v1")


class UIText(models.Model):
    """One piece of shop text and its translations (Admin > Translations).
    New text is added automatically the first time a page shows it in
    another language."""
    source = models.CharField(max_length=400, unique=True)
    ur = models.TextField("Urdu", blank=True)
    roman = models.TextField("Roman Urdu", blank=True)
    ar = models.TextField("Arabic", blank=True)
    es = models.TextField("Spanish", blank=True)
    fr = models.TextField("French", blank=True)
    by_ai = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["source"]
        verbose_name = "Shop text"

    def __str__(self):
        return self.source[:60]

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        from .i18n import clear_cache
        clear_cache()


class Conversation(models.Model):
    """Private messages between a shopper and a seller (optionally about
    one product)."""
    buyer = models.ForeignKey("auth.User", related_name="seller_conversations", on_delete=models.CASCADE)
    seller = models.ForeignKey(SellerAccount, related_name="conversations", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    last_message_at = models.DateTimeField(null=True, blank=True, db_index=True)
    buyer_unread = models.PositiveIntegerField(default=0)
    seller_unread = models.PositiveIntegerField(default=0)
    buyer_emailed_at = models.DateTimeField(null=True, blank=True)
    seller_emailed_at = models.DateTimeField(null=True, blank=True)
    reported = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-last_message_at", "-id"]
        constraints = [models.UniqueConstraint(fields=["buyer", "seller"], name="one_conversation_per_buyer_seller")]

    def __str__(self):
        return f"{self.buyer} <-> {self.seller.display_name}"


class Message(models.Model):
    conversation = models.ForeignKey(Conversation, related_name="messages", on_delete=models.CASCADE)
    sender = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL, related_name="+")
    from_seller = models.BooleanField(default=False)
    body = models.TextField(max_length=2000)
    product = models.ForeignKey(Product, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # Photos in chat are private: only the two sides (and staff) can open them.
    image = models.ImageField(upload_to=message_image_path, storage=_private_storage, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return self.body[:40] or "Photo"


class AuditLog(models.Model):
    user = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} ({self.user})"


class ChatThread(models.Model):
    """One support-chat thread per logged-in user (guests get a
    session-based thread key). Staff reply to these from /admin/."""
    user = models.OneToOneField("auth.User", null=True, blank=True, on_delete=models.CASCADE, related_name="chat_thread")
    session_key = models.CharField(max_length=40, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_resolved = models.BooleanField(default=False)

    def __str__(self):
        return f"Chat with {self.user or self.session_key}"


class ChatMessage(models.Model):
    SENDER_CHOICES = [("user", "User"), ("support", "Support")]

    thread = models.ForeignKey(ChatThread, related_name="messages", on_delete=models.CASCADE)
    sender = models.CharField(max_length=10, choices=SENDER_CHOICES, default="user")
    message = models.TextField()
    # Links / quick-reply buttons for automatic answers.
    extra = models.JSONField(default=dict, blank=True)
    is_auto = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    is_read = models.BooleanField(default=False)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.sender}: {self.message[:40]}"


# Header role flags are cached; clear them when a seller account or team
# membership changes.
from django.db.models.signals import post_delete, post_save  # noqa: E402
from django.dispatch import receiver  # noqa: E402


@receiver(post_save, sender=Product)
@receiver(post_save, sender=ProductVariant)
def _back_in_stock(sender, instance, **kwargs):
    if instance.stock > 0:
        from .stock_alerts import notify_restocked
        if sender is Product:
            notify_restocked(instance.pk, None)
        else:
            notify_restocked(instance.product_id, instance.pk)


from django.contrib.auth.signals import user_logged_in  # noqa: E402


@receiver(user_logged_in)
def _merge_saved_cart(sender, request, user, **kwargs):
    """On sign-in, items saved on the account (e.g. from another device)
    are added to this browser's cart, and the merged cart is saved back."""
    if request is None or not hasattr(request, "session"):
        return
    from .cart import persist
    profile = Profile.objects.filter(user=user).only("saved_cart").first()
    cart = dict(request.session.get("cart", {}))
    for key, qty in ((profile.saved_cart if profile else {}) or {}).items():
        cart.setdefault(key, qty)
    request.session["cart"] = cart
    persist(request, user=user)


@receiver(post_save, sender=Order)
def _deliver_downloads(sender, instance, **kwargs):
    """Digital items are delivered the moment the order is paid: the buyer
    can download them from My orders."""
    fields = kwargs.get("update_fields")
    if fields is not None and not ({"payment_status", "status"} & set(fields)):
        return
    if instance.payment_status == "paid" and instance.status != "cancelled":
        from django.utils import timezone
        now = timezone.now()
        changed = OrderItem.objects.filter(order=instance, product__is_digital=True).exclude(
            fulfillment_status="delivered").update(fulfillment_status="delivered", delivered_at=now, shipped_at=now)
        if changed and not OrderItem.objects.filter(order=instance).exclude(fulfillment_status="delivered").exists():
            Order.objects.filter(pk=instance.pk).update(status="delivered")


@receiver([post_save, post_delete], sender=Page)
@receiver([post_save, post_delete], sender=HomeBanner)
def _content_changed(sender, **kwargs):
    from django.core.cache import cache
    cache.delete_many(["content:footer_pages", "content:banners"])


@receiver([post_save, post_delete], sender=Category)
def _categories_changed(sender, **kwargs):
    from . import categories
    categories.clear_cache()


@receiver([post_save, post_delete], sender=ShippingZone)
def _clear_shipping_cache(sender, **kwargs):
    from .shipping import clear_cache
    clear_cache()


@receiver([post_save, post_delete], sender=SellerAccount)
@receiver([post_save, post_delete], sender=OrganizationMember)
def _clear_seller_flag(sender, instance, **kwargs):
    from django.core.cache import cache
    cache.delete(f"is_seller:{instance.user_id}")


class Claim(models.Model):
    """A customer reports a problem with an item ("didn't arrive", "not as
    described"...). The seller answers; the store team makes the final call
    and can refund straight from here."""
    REASONS = [
        ("not_received", "It didn't arrive"),
        ("not_as_described", "Not as described"),
        ("damaged", "Arrived damaged or broken"),
        ("wrong_item", "Wrong item sent"),
        ("other", "Something else"),
    ]
    STATUS = [
        ("open", "Waiting for the seller"),
        ("seller_replied", "Seller replied"),
        ("escalated", "With the store team"),
        ("refunded", "Refunded"),
        ("rejected", "Closed - no refund"),
        ("withdrawn", "Withdrawn by customer"),
    ]
    OPEN_STATUSES = ("open", "seller_replied", "escalated")
    order_item = models.ForeignKey("OrderItem", related_name="claims", on_delete=models.CASCADE)
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="claims")
    reason = models.CharField(max_length=20, choices=REASONS)
    details = models.TextField(max_length=2000)
    photo = models.ImageField(upload_to=claim_photo_path, storage=_private_storage, blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS, default="open", db_index=True)
    seller_reply = models.TextField(blank=True, max_length=2000)
    seller_offers_refund = models.BooleanField(default=False)
    seller_replied_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=500, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    return_request = models.ForeignKey("ReturnRequest", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Claim #{self.pk} on {self.order_item.product_name}"

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    @property
    def seller(self):
        return self.order_item.seller_account


class Shipment(models.Model):
    """A parcel booked with a courier service (EasyPost, Shippo...) for one
    seller's items in an order (or the store's own items)."""
    STATUS = [("rated", "Rates fetched"), ("booked", "Label ready"), ("in_transit", "In transit"),
              ("out_for_delivery", "Out for delivery"), ("delivered", "Delivered"), ("returned", "Returned to sender"),
              ("failed", "Delivery problem"), ("cancelled", "Cancelled")]
    order = models.ForeignKey("Order", related_name="shipments", on_delete=models.CASCADE)
    seller = models.ForeignKey(SellerAccount, null=True, blank=True, on_delete=models.SET_NULL, related_name="shipments")
    item_ids = models.JSONField(default=list)
    is_return = models.BooleanField(default=False)
    return_request = models.ForeignKey("ReturnRequest", null=True, blank=True, on_delete=models.SET_NULL, related_name="labels")
    provider = models.CharField(max_length=20)
    external_id = models.CharField(max_length=100, blank=True)
    transaction_id = models.CharField(max_length=100, blank=True)
    tracker_id = models.CharField(max_length=100, blank=True)
    rates = models.JSONField(default=list, blank=True)
    carrier = models.CharField(max_length=60, blank=True)
    service = models.CharField(max_length=80, blank=True)
    tracking_number = models.CharField(max_length=80, blank=True, db_index=True)
    tracking_url = models.URLField(max_length=500, blank=True)
    label_url = models.URLField(max_length=500, blank=True)
    cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    cost_currency = models.CharField(max_length=3, blank=True)
    status = models.CharField(max_length=20, choices=STATUS, default="rated", db_index=True)
    carrier_status = models.CharField(max_length=40, blank=True)
    error = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.provider} {self.tracking_number or self.external_id} (order #{self.order_id})"

    @property
    def active(self):
        return self.status in ("booked", "in_transit", "out_for_delivery")


class TaxRate(models.Model):
    """Sales tax / VAT / GST for one destination country."""
    country = models.CharField(max_length=2, unique=True, help_text="2-letter code, e.g. GB, DE, AE")
    name = models.CharField(max_length=20, default="VAT", help_text="Shown at checkout: VAT, GST, Sales tax...")
    rate = models.DecimalField(max_digits=5, decimal_places=2)
    includes_shipping = models.BooleanField(default=True, help_text="Charge it on shipping too (true for VAT in most countries).")
    duty_rate = models.DecimalField("Import duty %", max_digits=5, decimal_places=2, default=0, help_text="Typical customs duty for international orders, shown as an estimate at checkout. 0 = no estimate.")
    duty_free_up_to = models.DecimalField("Duty-free up to", max_digits=10, decimal_places=2, default=0, help_text="Orders up to this value (store currency) pay no duty. 0 = no limit.")
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["country"]

    def __str__(self):
        return f"{self.country} {self.name} {self.rate}%"


class SocialAccount(models.Model):
    """A Google / Apple sign-in linked to a user."""
    user = models.ForeignKey("auth.User", related_name="social_accounts", on_delete=models.CASCADE)
    provider = models.CharField(max_length=20)
    uid = models.CharField(max_length=255)
    email = models.EmailField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_login = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("provider", "uid")

    def __str__(self):
        return f"{self.provider}: {self.email or self.uid}"


class CodBlock(models.Model):
    """Phone numbers / emails that may only pay online (no cash on delivery)."""
    KINDS = [("phone", "Phone"), ("email", "Email")]
    kind = models.CharField(max_length=10, choices=KINDS)
    value = models.CharField(max_length=254, db_index=True)
    reason = models.CharField(max_length=200, blank=True)
    automatic = models.BooleanField(default=False)
    created_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("kind", "value")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.kind}: {self.value}"


class GiftCard(models.Model):
    """A digital gift card. Bought online (or issued by staff), emailed as a
    code, and redeemed into the recipient's store credit."""
    STATUS = [("pending", "Awaiting payment"), ("active", "Active"), ("redeemed", "Redeemed"), ("void", "Cancelled")]
    code = models.CharField(max_length=24, unique=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="usd")
    status = models.CharField(max_length=10, choices=STATUS, default="pending", db_index=True)
    purchaser = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="gift_cards_bought")
    purchaser_email = models.EmailField(blank=True)
    from_name = models.CharField(max_length=80, blank=True)
    recipient_name = models.CharField(max_length=80, blank=True)
    recipient_email = models.EmailField(blank=True)
    message = models.CharField(max_length=300, blank=True)
    payment_method = models.CharField(max_length=10, blank=True)  # card / paypal / staff
    stripe_session_id = models.CharField(max_length=255, blank=True, db_index=True)
    paypal_order_id = models.CharField(max_length=64, blank=True, db_index=True)
    issued_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.CharField(max_length=200, blank=True, help_text="Only staff see this.")
    paid_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    redeemed_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="gift_cards_redeemed")
    redeemed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Gift card {self.masked} ({self.amount})"

    @property
    def masked(self):
        return f"…{self.code[-4:]}"


class DailyStat(models.Model):
    """Anonymous daily counters for store reports (no personal data):
    visits, product views, add-to-cart, checkout starts, searches."""
    date = models.DateField(db_index=True)
    kind = models.CharField(max_length=16)
    key = models.CharField(max_length=150, blank=True, default="")
    count = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("date", "kind", "key")
        indexes = [models.Index(fields=["kind", "date"])]

    def __str__(self):
        return f"{self.date} {self.kind} {self.key}: {self.count}"


class Promotion(models.Model):
    """A timed sale: a seller (or the store, for its own products) puts
    products X% off between two times. Prices are lowered when it starts
    (old price shown crossed out) and put back when it ends."""
    STATUS = [("scheduled", "Scheduled"), ("active", "Running"), ("ended", "Ended"), ("cancelled", "Cancelled")]
    seller_account = models.ForeignKey("SellerAccount", null=True, blank=True, on_delete=models.CASCADE, related_name="promotions")
    name = models.CharField(max_length=80)
    percent_off = models.PositiveSmallIntegerField()
    category = models.CharField(max_length=30, blank=True, help_text="Empty = all products.")
    product_ids = models.JSONField(default=list, blank=True, help_text="Empty = every product (in the category).")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    status = models.CharField(max_length=10, choices=STATUS, default="scheduled", db_index=True)
    saved_prices = models.JSONField(default=dict, blank=True, editable=False)
    created_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-starts_at"]

    def __str__(self):
        return f"{self.name} (-{self.percent_off}%)"


class AdCampaign(models.Model):
    """A seller pays per click to show one product as "Sponsored"."""
    STATUS = [("active", "Running"), ("paused", "Paused"), ("ended", "Ended")]
    seller_account = models.ForeignKey("SellerAccount", on_delete=models.CASCADE, related_name="ad_campaigns")
    product = models.ForeignKey("Product", on_delete=models.CASCADE, related_name="ad_campaigns")
    bid = models.DecimalField("Cost per click", max_digits=6, decimal_places=2)
    daily_budget = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=8, choices=STATUS, default="active", db_index=True)
    paused_by_staff = models.BooleanField(default=False)
    spent = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Ad #{self.pk} {self.product_id} @ {self.bid}"


class AdStat(models.Model):
    campaign = models.ForeignKey(AdCampaign, on_delete=models.CASCADE, related_name="stats")
    date = models.DateField()
    impressions = models.PositiveIntegerField(default=0)
    clicks = models.PositiveIntegerField(default=0)
    spend = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    orders = models.PositiveIntegerField(default=0)
    sales = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        unique_together = ("campaign", "date")


class EmailCampaign(models.Model):
    """A marketing email to newsletter subscribers (people who opted in)."""
    AUDIENCES = [("all", "All subscribers"), ("buyers", "Subscribers who have ordered"),
                 ("new", "Subscribers who haven't ordered yet"), ("lapsed", "Customers who haven't ordered for 90 days")]
    STATUS = [("draft", "Draft"), ("sending", "Sending"), ("sent", "Sent")]
    subject = models.CharField(max_length=150)
    heading = models.CharField(max_length=150)
    body = models.TextField(help_text="Plain text. Blank lines start a new paragraph.")
    button_text = models.CharField(max_length=40, blank=True)
    button_url = models.CharField(max_length=300, blank=True)
    product_ids = models.JSONField(default=list, blank=True)
    coupon_code = models.CharField(max_length=30, blank=True)
    audience = models.CharField(max_length=10, choices=AUDIENCES, default="all")
    status = models.CharField(max_length=10, choices=STATUS, default="draft", db_index=True)
    recipients = models.JSONField(default=list, blank=True, editable=False)
    sent_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    opens = models.PositiveIntegerField(default=0)
    clicks = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.subject


class PushSubscription(models.Model):
    """A browser/phone that agreed to get notifications (web push)."""
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="push_subscriptions")
    endpoint = models.URLField(max_length=500, unique=True)
    p256dh = models.CharField(max_length=120)
    auth = models.CharField(max_length=60)
    user_agent = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Push for {self.user_id}"


class ListingReport(models.Model):
    """'Report this listing' from shoppers (illegal, counterfeit, unsafe,
    misleading...). Reviewed by the store team (notice-and-action)."""
    REASONS = [("counterfeit", "Fake or counterfeit"), ("illegal", "Illegal or banned item"), ("unsafe", "Unsafe product"),
               ("misleading", "Wrong or misleading information"), ("ip", "Uses my brand or copyright"),
               ("offensive", "Offensive content"), ("other", "Something else")]
    STATUS = [("open", "Open"), ("removed", "Listing removed"), ("dismissed", "No action needed")]
    product = models.ForeignKey("Product", on_delete=models.CASCADE, related_name="reports")
    reason = models.CharField(max_length=20, choices=REASONS)
    details = models.TextField(max_length=2000, blank=True)
    reporter = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    email = models.EmailField(blank=True)
    status = models.CharField(max_length=10, choices=STATUS, default="open", db_index=True)
    decision_note = models.CharField(max_length=300, blank=True)
    handled_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    handled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class ApiToken(models.Model):
    """Personal access token for the REST API (mobile apps, integrations).

    Only a SHA-256 hash of the token is stored; the token itself is shown
    once when it's created. ``prefix`` (the first characters) identifies it
    in lists without revealing it."""
    PREFIX = "lm_"
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="api_tokens")
    name = models.CharField(max_length=60, default="API token")
    key_hash = models.CharField(max_length=64, unique=True, editable=False)
    prefix = models.CharField(max_length=12, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True, editable=False)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.prefix}… ({self.name}) for {self.user_id}"

    @staticmethod
    def hash(key):
        import hashlib
        return hashlib.sha256(key.encode()).hexdigest()

    @classmethod
    def issue(cls, user, name="API token", days=None):
        """Creates a token and returns (token_object, plain_key)."""
        import secrets
        from datetime import timedelta
        from django.utils import timezone
        key = cls.PREFIX + secrets.token_urlsafe(32)
        token = cls.objects.create(user=user, name=(name or "API token")[:60], key_hash=cls.hash(key), prefix=key[:10],
                                   expires_at=timezone.now() + timedelta(days=days) if days else None)
        return token, key

    @property
    def is_expired(self):
        from django.utils import timezone
        return self.expires_at is not None and self.expires_at <= timezone.now()



# ---------------------------------------------------------------------------
# Marketplace parity (Amazon / eBay / Etsy / Daraz features)
# ---------------------------------------------------------------------------

class ReviewPhoto(models.Model):
    review = models.ForeignKey(Review, related_name="photos", on_delete=models.CASCADE)
    image_url = models.CharField(max_length=500)


class ReviewVote(models.Model):
    review = models.ForeignKey(Review, related_name="votes", on_delete=models.CASCADE)
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["review", "user"], name="one_helpful_vote")]


class CouponClaim(models.Model):
    """A voucher a shopper collected from the voucher centre."""
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="collected_coupons")
    coupon = models.ForeignKey(Coupon, on_delete=models.CASCADE, related_name="claims")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "coupon"], name="one_claim_per_coupon")]


class PriceHistory(models.Model):
    """Every price a product had, for the EU 'lowest price in the last 30
    days' rule (Omnibus Directive) shown next to discounts."""
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="price_history")
    price = models.DecimalField(max_digits=10, decimal_places=2)
    recorded_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["product", "recorded_at"])]


class Bid(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="bids")
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="bids")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-amount", "created_at"]


class Subscription(models.Model):
    """Subscribe & Save: every N weeks the shopper gets a one-click reorder
    at the subscription discount."""
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="subscriptions")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="subscriptions")
    variant = models.ForeignKey("ProductVariant", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    quantity = models.PositiveSmallIntegerField(default=1)
    every_weeks = models.PositiveSmallIntegerField(default=4)
    next_date = models.DateField(db_index=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["next_date"]


class PickupPoint(models.Model):
    """A collection point (shop, locker) where customers can pick up orders."""
    name = models.CharField(max_length=100)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=2)
    hours = models.CharField("Opening hours", max_length=120, blank=True)
    fee = models.DecimalField(max_digits=8, decimal_places=2, default=0, help_text="Shipping fee when collecting here.")
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["country", "city", "name"]

    def __str__(self):
        return f"{self.name}, {self.city}"


class MembershipPayment(models.Model):
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="membership_payments")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    days = models.PositiveSmallIntegerField()
    stripe_session_id = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=10, default="pending")  # pending / paid
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)


@receiver(post_save, sender=Product)
def _remember_price(sender, instance, created, **kwargs):
    fields = kwargs.get("update_fields")
    if fields is not None and "price" not in fields:
        return
    last = PriceHistory.objects.filter(product=instance).order_by("-recorded_at").values_list("price", flat=True).first()
    if last is None or last != instance.price:
        PriceHistory.objects.create(product=instance, price=instance.price)


class PriceAlert(models.Model):
    """'Tell me when it drops below X' on a product page."""
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="price_alerts")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="price_alerts")
    target_price = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)
    notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "product"], name="one_price_alert_per_product")]


@receiver(post_save, sender=Product)
def _price_alerts(sender, instance, created, **kwargs):
    fields = kwargs.get("update_fields")
    if created or (fields is not None and "price" not in fields):
        return
    due = PriceAlert.objects.filter(product=instance, notified_at__isnull=True, target_price__gte=instance.price)
    if not due.exists():
        return
    from django.utils import timezone
    for alert in due.select_related("user"):
        Notification.objects.create(user_id=alert.user_id, link=f"/product/{instance.id}/",
                                    message=f"Price alert: {instance.name} is now {instance.price} (your target {alert.target_price})."[:255])
        from .views import _send_html_email
        from .order_emails import absolute
        _send_html_email(f"{instance.name} dropped to your price", "bees/emails/price_drop.html", {
            "user": alert.user, "items": [{"product": instance, "old": alert.target_price, "new": instance.price, "pct": 0,
                                           "url": absolute(f"/product/{instance.id}/")}],
            "wishlist_url": absolute(f"/product/{instance.id}/"), "prefs_url": absolute("/account/privacy/")}, alert.user.email)
    due.update(notified_at=timezone.now())


class Passkey(models.Model):
    """A passkey (fingerprint, face or device PIN) the customer can sign in
    with instead of a password. Only the public key is stored."""
    user = models.ForeignKey("auth.User", related_name="passkeys", on_delete=models.CASCADE)
    credential_id = models.CharField(max_length=512, unique=True)
    public_key = models.TextField()
    sign_count = models.PositiveBigIntegerField(default=0)
    transports = models.JSONField(default=list, blank=True)
    name = models.CharField(max_length=60, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name or 'Passkey'} ({self.user})"


class AgentCheckout(models.Model):
    """A checkout an AI shopping agent started through the Universal
    Commerce Protocol. The agent builds the cart; the shopper finishes on
    the store's own checkout (continue_url)."""
    STATUS = [("requires_escalation", "Waiting for the shopper"), ("canceled", "Canceled"), ("completed", "Completed")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lines = models.JSONField(default=list)
    buyer = models.JSONField(default=dict, blank=True)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=24, choices=STATUS, default="requires_escalation")
    agent = models.CharField(max_length=300, blank=True)
    idempotency_key = models.CharField(max_length=100, blank=True, db_index=True)
    opened_at = models.DateTimeField(null=True, blank=True, help_text="The shopper opened the checkout link.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
