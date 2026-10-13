import csv
from django.contrib import admin
from django.http import HttpResponse
from django.db.models import Sum
from .models import (
    Product, Review, Order, OrderItem, Wishlist, Coupon,
    ProductImage, Profile, Address, Question, NewsletterSubscriber,
    Notification, SearchLog, SellerAccount, SellerReview, ReturnRequest, SiteSettings, AuditLog,
    OrganizationMember, ChatThread, ChatMessage,
)


def export_as_csv(modeladmin, request, queryset, field_names):
    response = HttpResponse(content_type="text/csv")
    filename = f"{modeladmin.model._meta.verbose_name_plural}_export.csv".replace(" ", "_")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow(field_names)
    for obj in queryset:
        row = []
        for field in field_names:
            value = getattr(obj, field, "")
            row.append(value() if callable(value) else value)
        writer.writerow(row)
    return response

from django.urls import reverse
from django.utils.html import format_html

from . import payments
from .templatetags.bees_extras import money

admin.site.index_title = "Database admin"
_original_each_context = admin.site.each_context


def _branded_each_context(request):
    """Admin header/title follow the white-label store name."""
    context = _original_each_context(request)
    try:
        name = SiteSettings.load().site_name
    except Exception:
        name = "Store"
    context["site_header"] = name
    context["site_title"] = f"{name} Admin"
    return context


admin.site.each_context = _branded_each_context


def cancel_order_with_side_effects(order, request=None):
    """Cancels an order, refunding card payments and returning stock.
    Returns an error string, or None on success."""
    if order.status == "cancelled":
        return None
    if order.payment_status == "paid":
        if not payments.refund_order(order):
            return f"Order #{order.id}: Stripe refund failed - refund it from the Stripe dashboard, then cancel again."
        order.payment_status = "refunded"
    payments.void_pending_payment(order)
    order.status = "cancelled"
    order.save(update_fields=["status", "payment_status", "payment_method", "cod_fallback"])
    payments.restock(order)
    return None


class ReviewInline(admin.TabularInline):
    model = Review
    extra = 0


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 1


class QuestionInline(admin.TabularInline):
    model = Question
    extra = 0


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "price", "old_price", "discount_percent", "stock", "stock_status", "seller_name", "approval_status", "is_flash_sale")
    list_filter = ("category", "is_flash_sale", "approval_status")
    search_fields = ("name", "seller_name", "description")
    list_editable = ("price", "stock")
    list_per_page = 50
    inlines = [ProductImageInline, ReviewInline, QuestionInline]
    actions = ["approve_products", "reject_products", "mark_flash_sale", "unmark_flash_sale", "export_products_csv"]

    def export_products_csv(self, request, queryset):
        return export_as_csv(self, request, queryset, [
            "id", "name", "category", "price", "old_price", "stock",
            "seller_name", "approval_status", "is_flash_sale",
        ])
    export_products_csv.short_description = "Export selected products to CSV"

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        action = "Edited" if change else "Created"
        AuditLog.objects.create(user=request.user, action=f"{action} product #{obj.id} ({obj.name})")

    def delete_model(self, request, obj):
        AuditLog.objects.create(user=request.user, action=f"Deleted product #{obj.id} ({obj.name})")
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            AuditLog.objects.create(user=request.user, action=f"Deleted product #{obj.id} ({obj.name})")
        super().delete_queryset(request, queryset)

    def stock_status(self, obj):
        if obj.stock <= 0:
            return "Out of stock"
        if obj.stock <= 5:
            return f"Low ({obj.stock})"
        return "In stock"
    stock_status.short_description = "Stock status"

    def approve_products(self, request, queryset):
        queryset.update(approval_status="approved")
        self.message_user(request, f"{queryset.count()} product(s) approved and now live.")
    approve_products.short_description = "Approve selected products (make live)"

    def reject_products(self, request, queryset):
        queryset.update(approval_status="rejected")
        self.message_user(request, f"{queryset.count()} product(s) rejected.")
    reject_products.short_description = "Reject selected products"

    def mark_flash_sale(self, request, queryset):
        queryset.update(is_flash_sale=True)
        self.message_user(request, f"{queryset.count()} product(s) added to flash sale.")
    mark_flash_sale.short_description = "Add to flash sale"

    def unmark_flash_sale(self, request, queryset):
        queryset.update(is_flash_sale=False)
        self.message_user(request, f"{queryset.count()} product(s) removed from flash sale.")
    unmark_flash_sale.short_description = "Remove from flash sale"


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ("username", "product", "rating", "is_verified_purchase", "created_at")
    list_filter = ("rating", "is_verified_purchase")
    search_fields = ("username", "product__name", "comment")
    date_hierarchy = "created_at"


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = ("product", "product_name", "price", "quantity")
    can_delete = False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "full_name", "country", "payment_method", "payment_status", "status", "tracking_number", "total_display", "created_at")
    list_filter = ("status", "payment_method", "payment_status", "country", "created_at")
    search_fields = ("id", "full_name", "email", "user__username", "phone", "city", "tracking_number", "stripe_session_id", "stripe_payment_intent")
    fieldsets = (
        ("Customer", {"fields": ("user", "email", "full_name", "phone")}),
        ("Shipping address", {"fields": ("address", "city", "state", "postal_code", "country")}),
        ("Fulfilment", {"fields": ("status", "tracking_number", "courier_name", "estimated_delivery")}),
        ("Payment", {"fields": (
            "payment_method", "payment_status", "currency", "stripe_link",
            "coupon_code", "discount_amount", "shipping_amount", "tax_amount", "total_display",
        )}),
    )
    readonly_fields = ("payment_method", "payment_status", "currency", "stripe_link", "total_display",
                       "coupon_code", "discount_amount", "shipping_amount", "tax_amount")

    def total_display(self, obj):
        return money(obj.total)
    total_display.short_description = "Total"

    def stripe_link(self, obj):
        if not obj.stripe_payment_intent:
            return obj.stripe_session_id or "-"
        mode = "test/" if str(getattr(payments.settings, "STRIPE_SECRET_KEY", "")).startswith("sk_test") else ""
        return format_html(
            '<a href="https://dashboard.stripe.com/{}payments/{}" target="_blank" rel="noopener">View in Stripe</a>',
            mode, obj.stripe_payment_intent,
        )
    stripe_link.short_description = "Stripe payment"
    date_hierarchy = "created_at"

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("items")

    inlines = [OrderItemInline]
    actions = ["mark_confirmed", "mark_shipped", "mark_delivered", "mark_cancelled", "export_orders_csv"]

    def export_orders_csv(self, request, queryset):
        return export_as_csv(self, request, queryset, [
            "id", "user", "email", "full_name", "city", "country", "phone", "payment_method", "payment_status", "status", "total", "created_at",
        ])
    export_orders_csv.short_description = "Export selected orders to CSV"

    def save_model(self, request, obj, form, change):
        old_status = None
        if change:
            old_status = Order.objects.filter(pk=obj.pk).values_list("status", flat=True).first()
        if change and obj.status == "cancelled" and old_status and old_status != "cancelled":
            obj.status = old_status
            error = cancel_order_with_side_effects(obj, request)
            if error:
                self.message_user(request, error, level="error")
                return
            obj.refresh_from_db()
        super().save_model(request, obj, form, change)
        if change and old_status and old_status != obj.status:
            AuditLog.objects.create(
                user=request.user,
                action=f"Changed order #{obj.id} status from {old_status} to {obj.status}",
            )

    def mark_confirmed(self, request, queryset):
        self._bulk_status(request, queryset, "confirmed")
    mark_confirmed.short_description = "Mark selected orders as Confirmed"

    def mark_shipped(self, request, queryset):
        self._bulk_status(request, queryset, "shipped")
    mark_shipped.short_description = "Mark selected orders as Shipped"

    def mark_delivered(self, request, queryset):
        self._bulk_status(request, queryset, "delivered")
    mark_delivered.short_description = "Mark selected orders as Delivered"

    def mark_cancelled(self, request, queryset):
        cancelled = 0
        for order in queryset.exclude(status="cancelled"):
            old_status = order.status
            error = cancel_order_with_side_effects(order, request)
            if error:
                self.message_user(request, error, level="error")
                continue
            AuditLog.objects.create(user=request.user, action=f"Cancelled order #{order.id} (was {old_status})")
            cancelled += 1
        self.message_user(request, f"{cancelled} order(s) cancelled. Stock was returned and card payments refunded.")
    mark_cancelled.short_description = "Cancel selected orders (refund + restock)"

    def _bulk_status(self, request, queryset, new_status):
        for order in queryset:
            old_status = order.status
            if old_status != new_status:
                AuditLog.objects.create(
                    user=request.user,
                    action=f"Changed order #{order.id} status from {old_status} to {new_status}",
                )
        for order in queryset.exclude(status="cancelled"):
            order.status = new_status
            order.save(update_fields=["status"])  # save() notifies the customer


@admin.register(Wishlist)
class WishlistAdmin(admin.ModelAdmin):
    list_display = ("user", "product", "created_at")
    search_fields = ("user__username", "product__name")


@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    list_display = (
        "code", "percent_off", "active", "expiry_date", "min_order_value",
        "usage_display", "per_user_limit",
    )
    list_editable = ("active",)
    search_fields = ("code",)
    fields = ("code", "percent_off", "active", "expiry_date", "usage_limit", "per_user_limit", "min_order_value")

    def usage_display(self, obj):
        limit = obj.usage_limit if obj.usage_limit is not None else "\u221e"
        return f"{obj.times_used()} / {limit}"
    usage_display.short_description = "Used"


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "phone")
    search_fields = ("user__username", "phone")


@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    list_display = ("user", "label", "city", "country", "phone")
    search_fields = ("user__username", "city", "phone")


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ("product", "username", "question", "answer")
    search_fields = ("product__name", "username", "question")


@admin.register(NewsletterSubscriber)
class NewsletterSubscriberAdmin(admin.ModelAdmin):
    list_display = ("email", "created_at")
    search_fields = ("email",)
    date_hierarchy = "created_at"
    actions = ["send_bulk_email"]

    def send_bulk_email(self, request, queryset):
        from django.core.mail import send_mail
        from django.conf import settings as dj_settings
        from django.template.response import TemplateResponse
        from django.contrib.admin import helpers

        if "apply" in request.POST:
            subject = request.POST.get("subject", "").strip()
            body = request.POST.get("body", "").strip()
            if not subject or not body:
                self.message_user(request, "Subject and message are both required.", level="error")
                return None
            recipients = list(queryset.values_list("email", flat=True))
            sent = 0
            for email in recipients:
                try:
                    send_mail(subject, body, dj_settings.DEFAULT_FROM_EMAIL, [email], fail_silently=False)
                    sent += 1
                except Exception:
                    pass
            self.message_user(request, f"Email sent to {sent} of {len(recipients)} subscriber(s).")
            return None

        return TemplateResponse(request, "admin/bulk_email_form.html", {
            "subscribers": queryset,
            "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
        })
    send_bulk_email.short_description = "Send an email to selected subscribers"


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("user", "message", "is_read", "created_at")
    list_filter = ("is_read",)
    search_fields = ("user__username", "message")


@admin.register(SearchLog)
class SearchLogAdmin(admin.ModelAdmin):
    list_display = ("query", "count")
    search_fields = ("query",)
    ordering = ("-count",)


@admin.register(OrganizationMember)
class OrganizationMemberAdmin(admin.ModelAdmin):
    list_display = ("user", "organization", "role", "added_at")
    list_filter = ("role",)
    search_fields = ("user__username", "organization__business_name", "organization__organization_name")


@admin.register(SellerAccount)
class SellerAccountAdmin(admin.ModelAdmin):
    list_display = (
        "display_name", "user", "account_type", "status",
        "products_sold_count", "total_sales_display", "commission_owed_display",
        "net_earnings_display", "total_paid_out", "amount_owed_display",
        "commission_rate", "effective_commission_rate", "city", "country", "created_at",
    )
    list_filter = ("account_type", "status")
    search_fields = ("business_name", "organization_name", "full_name", "user__username", "cnic")
    readonly_fields = (
        "created_at", "products_sold_count", "total_sales_display",
        "commission_owed_display", "net_earnings_display", "amount_owed_display",
        "verification_documents",
    )
    date_hierarchy = "created_at"
    actions = ["approve_sellers", "reject_sellers", "suspend_sellers", "mark_fully_paid_out", "export_sellers_csv"]

    def export_sellers_csv(self, request, queryset):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="sellers_export.csv"'
        writer = csv.writer(response)
        writer.writerow([
            "id", "display_name", "username", "account_type", "status", "units_sold",
            "total_sales", "commission_rate_pct", "commission_owed", "net_earnings",
            "total_paid_out", "still_owed", "city", "country", "created_at",
        ])
        for s in queryset:
            units = OrderItem.objects.filter(seller_account=s).counted().aggregate(
                total=Sum("quantity")
            )["total"] or 0
            sales = float(s.lifetime_sales)
            rate = s.effective_commission_rate
            commission = float(s.commission_total)
            writer.writerow([
                s.id, s.display_name, s.user.username, s.account_type, s.status, units,
                f"{sales:.2f}", rate, f"{commission:.2f}", f"{s.net_earnings:.2f}",
                f"{s.total_paid_out:.2f}", f"{s.amount_owed:.2f}", s.city, s.country, s.created_at,
            ])
        return response
    export_sellers_csv.short_description = "Export selected sellers to CSV"

    fieldsets = (
        ("Account", {"fields": ("user", "account_type", "status", "commission_rate", "admin_note", "created_at")}),
        ("Earnings summary", {"fields": (
            "products_sold_count", "total_sales_display", "commission_owed_display",
            "net_earnings_display", "total_paid_out", "amount_owed_display",
        )}),
        ("Registration details", {"fields": (
            "full_name", "business_name", "organization_name", "phone", "cnic",
            "business_address", "city", "country", "store_description",
            "product_categories", "brand_info", "tax_info", "bank_details",
        )}),
        ("Documents", {"fields": ("verification_documents", "store_logo", "store_banner")}),
    )

    def verification_documents(self, obj):
        links = []
        for field, label in (("business_certificate", "Business certificate"), ("id_document", "ID document")):
            if getattr(obj, field):
                url = reverse("seller_document", args=[obj.pk, field])
                links.append(format_html('<a href="{}" target="_blank" rel="noopener">{}</a>', url, label))
        if not links:
            return "No documents uploaded"
        return format_html(" &nbsp;|&nbsp; ".join(["{}"] * len(links)), *links)
    verification_documents.short_description = "Private documents (staff only)"

    def products_sold_count(self, obj):
        from django.db.models import Sum
        total = OrderItem.objects.filter(seller_account=obj).counted().aggregate(total=Sum("quantity"))["total"]
        return total or 0
    products_sold_count.short_description = "Units sold"

    def total_sales_display(self, obj):
        return money(obj.lifetime_sales)
    total_sales_display.short_description = "Total sales"

    def commission_owed_display(self, obj):
        return f"{money(obj.commission_total)} (current rate {obj.effective_commission_rate:g}%)"
    commission_owed_display.short_description = "Commission earned by the store"

    def net_earnings_display(self, obj):
        return money(obj.net_earnings)
    net_earnings_display.short_description = "Seller's net earnings (after commission)"

    def amount_owed_display(self, obj):
        return money(obj.amount_owed)
    amount_owed_display.short_description = "Still owed to seller"

    def mark_fully_paid_out(self, request, queryset):
        count = 0
        for seller in queryset:
            seller.total_paid_out = seller.net_earnings
            seller.save(update_fields=["total_paid_out"])
            AuditLog.objects.create(
                user=request.user,
                action=f"Recorded full payout of {money(seller.net_earnings)} to seller #{seller.id} ({seller.display_name})",
            )
            count += 1
        self.message_user(request, f"Marked {count} seller(s) as fully paid out.")
    mark_fully_paid_out.short_description = "Mark selected sellers as fully paid out"

    def approve_sellers(self, request, queryset):
        from .models import Notification, AuditLog
        for seller in queryset:
            seller.status = "approved"
            seller.save(update_fields=["status"])
            Notification.objects.create(
                user=seller.user,
                message="Your seller account has been approved! You can now list products.",
                link="/seller/dashboard/",
            )
            AuditLog.objects.create(user=request.user, action=f"Approved seller #{seller.id} ({seller.display_name})")
        self.message_user(request, f"{queryset.count()} seller(s) approved.")
    approve_sellers.short_description = "Approve selected sellers"

    def reject_sellers(self, request, queryset):
        from .models import AuditLog
        queryset.update(status="rejected")
        for seller in queryset:
            AuditLog.objects.create(user=request.user, action=f"Rejected seller #{seller.id} ({seller.display_name})")
        self.message_user(request, f"{queryset.count()} seller(s) rejected.")
    reject_sellers.short_description = "Reject selected sellers"

    def suspend_sellers(self, request, queryset):
        from .models import AuditLog
        queryset.update(status="suspended")
        for seller in queryset:
            AuditLog.objects.create(user=request.user, action=f"Suspended seller #{seller.id} ({seller.display_name})")
        self.message_user(request, f"{queryset.count()} seller(s) suspended.")
    suspend_sellers.short_description = "Suspend selected sellers"


@admin.register(ReturnRequest)
class ReturnRequestAdmin(admin.ModelAdmin):
    list_display = ("order_item", "user", "status", "refund_method", "created_at")
    list_filter = ("status", "refund_method")
    search_fields = ("user__username",)
    date_hierarchy = "created_at"
    actions = ["mark_approved", "mark_rejected", "mark_refunded"]

    def mark_approved(self, request, queryset):
        self._bulk_status(queryset, "approved")
    mark_approved.short_description = "Mark as approved"

    def mark_rejected(self, request, queryset):
        self._bulk_status(queryset, "rejected")
    mark_rejected.short_description = "Mark as rejected"

    def mark_refunded(self, request, queryset):
        self._bulk_status(queryset, "refunded")
    mark_refunded.short_description = "Mark as refunded"

    def _bulk_status(self, queryset, new_status):
        # Loop + individual .save() so the model's save() override fires
        # (notifies the customer) - queryset.update() would skip that.
        for obj in queryset:
            obj.status = new_status
            obj.save(update_fields=["status"])


@admin.register(SellerReview)
class SellerReviewAdmin(admin.ModelAdmin):
    list_display = ("seller", "user", "rating", "created_at")
    search_fields = ("seller__display_name", "user__username")


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    list_display = ("site_name", "tax_percent", "shipping_flat_fee", "banner_active")
    fieldsets = (
        ("Brand (white-label)", {
            "description": "Change the store name, logo and colours here - no code changes needed.",
            "fields": ("site_name", "tagline", "logo_url", "logo_file", "favicon_url", "primary_color", "accent_color"),
        }),
        ("Homepage hero", {"fields": ("hero_title", "hero_subtitle", "hero_image_url")}),
        ("Contact & social", {"fields": (
            "support_email", "support_phone", "company_address",
            "facebook_url", "instagram_url", "twitter_url", "youtube_url",
        )}),
        ("Checkout", {"fields": ("tax_percent", "shipping_flat_fee", "free_shipping_threshold", "allow_cash_on_delivery")}),
        ("Announcement bar", {"fields": ("banner_active", "banner_text", "banner_link")}),
        ("Advanced", {"fields": ("show_language_menu",)}),
    )

    def has_add_permission(self, request):
        return not SiteSettings.objects.exists()


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("action", "user", "created_at")
    list_filter = ("created_at",)
    search_fields = ("action", "user__username")
    date_hierarchy = "created_at"


class ChatMessageInline(admin.TabularInline):
    model = ChatMessage
    extra = 1
    fields = ("sender", "message", "created_at", "is_read")
    readonly_fields = ("created_at",)


@admin.register(ChatThread)
class ChatThreadAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "session_key", "is_resolved", "created_at")
    list_filter = ("is_resolved",)
    inlines = [ChatMessageInline]


# ---- Custom admin dashboard: live summary cards on the /admin/ home page ----
import types
from django.contrib.admin.sites import AdminSite
from django.utils import timezone


def _bees_index(self, request, extra_context=None):
    extra_context = extra_context or {}
    extra_context["bees_stats"] = {
        "orders_today": Order.objects.filter(created_at__date=timezone.localdate()).count(),
        "total_orders": Order.objects.count(),
        "total_products": Product.objects.count(),
        "pending_sellers": SellerAccount.objects.filter(status="pending").count(),
        "pending_products": Product.objects.filter(approval_status="pending").count(),
        "low_stock": Product.objects.filter(stock__gt=0, stock__lte=5).count(),
    }
    return AdminSite.index(self, request, extra_context)


admin.site.index = types.MethodType(_bees_index, admin.site)


def _admin_login(request, extra_context=None):
    """Django admin sign-in goes through the store's sign-in page, so
    two-step sign-in can't be skipped."""
    from urllib.parse import urlencode
    from django.shortcuts import redirect
    from .security import safe_next_url
    return redirect(f"/login/?{urlencode({'next': safe_next_url(request, request.GET.get('next'), '/admin/')})}")


admin.site.login = _admin_login


from .models import ApiToken  # noqa: E402


@admin.register(ApiToken)
class ApiTokenAdmin(admin.ModelAdmin):
    """API sign-ins. Only a hash is stored, so tokens can be revoked here
    (delete) but never read."""
    list_display = ("prefix", "name", "user", "created_at", "last_used_at", "expires_at")
    search_fields = ("user__username", "user__email", "name", "prefix")
    readonly_fields = ("user", "name", "prefix", "created_at", "last_used_at", "expires_at")
    exclude = ("key_hash",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
