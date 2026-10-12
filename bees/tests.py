"""
Automated tests for the marketplace.

These aren't exhaustive - they focus on the business logic that's most
likely to break silently: money calculations (order totals, commission),
permission checks (who can access what), and the bugs found and fixed
during development (display_name, N+1 query annotations).

Run with: python manage.py test bees
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client
from django.test import TestCase as DjangoTestCase


class TestCase(DjangoTestCase):
    """Clears the cache before every test: site settings and rate-limit
    counters are cached, and the test database is rolled back between
    tests while the in-memory cache is not."""

    def _pre_setup(self):
        super()._pre_setup()
        cache.clear()
        # Never call the real Stripe API from tests.
        from unittest import mock as _mock
        self._stripe_customer = _mock.patch("stripe.Customer.create", return_value=_mock.MagicMock(id="cus_test"))
        self._stripe_customer.start()
        # Older admin tests use staff without an authenticator app; the
        # two-step tests switch this back on.
        from .models import SiteSettings as _SS
        _SS.objects.update_or_create(pk=1, defaults={"require_staff_2fa": False})
        # Older checkout tests treat every buyer as having a confirmed email;
        # the cash-on-delivery confirmation tests switch this back on.
        self._cod_patch = None
        if not getattr(self, "real_cod_confirmation", False):
            self._cod_patch = _mock.patch("bees.views._email_verified", return_value=True)
            self._cod_patch.start()

    def _post_teardown(self):
        self._stripe_customer.stop()
        if self._cod_patch:
            self._cod_patch.stop()
        super()._post_teardown()
from django.urls import reverse

from .models import (
    Product, Order, OrderItem, SellerAccount, OrganizationMember,
    Review, Notification, AuditLog,
)
from .views import with_ratings, get_seller_account_for_user


def make_product(**kwargs):
    defaults = dict(
        name="Test Product", category="skincare", price=Decimal("500.00"),
        stock=10, image_url="https://example.com/img.jpg",
    )
    defaults.update(kwargs)
    return Product.objects.create(**defaults)


class ProductRatingTests(TestCase):
    """The average_rating/rating_count properties used to run a fresh query
    per product (N+1). They now use a DB annotation when present, and fall
    back to the old per-query behaviour otherwise. Both paths must agree."""

    def setUp(self):
        self.product = make_product()

    def test_no_reviews_gives_zero(self):
        self.assertEqual(self.product.average_rating, 0)
        self.assertEqual(self.product.rating_count, 0)

    def test_average_rating_matches_manual_calculation(self):
        Review.objects.create(product=self.product, username="a", rating=4, comment="Good")
        Review.objects.create(product=self.product, username="b", rating=5, comment="Great")
        self.product.refresh_from_db()
        self.assertEqual(self.product.average_rating, 4.5)
        self.assertEqual(self.product.rating_count, 2)

    def test_annotated_queryset_matches_property_fallback(self):
        Review.objects.create(product=self.product, username="a", rating=4, comment="Good")
        Review.objects.create(product=self.product, username="b", rating=5, comment="Great")
        annotated = with_ratings(Product.objects.filter(pk=self.product.pk)).first()
        self.assertEqual(annotated.average_rating, 4.5)
        self.assertEqual(annotated.rating_count, 2)


class ProductStockNotificationTests(TestCase):
    """Product.save() notifies the seller once when stock crosses into the
    low-stock/out-of-stock zone. Must fire exactly once per crossing, not
    on every save while it stays low (that would spam the seller)."""

    def setUp(self):
        self.user = User.objects.create_user("seller1", "s1@example.com", "pass12345")
        self.seller = SellerAccount.objects.create(
            user=self.user, account_type="individual", status="approved",
            business_name="Test Shop",
        )
        self.product = make_product(stock=20, seller_account=self.seller)

    def test_low_stock_notification_fires_once(self):
        self.product.stock = 4
        self.product.save()
        self.assertEqual(
            Notification.objects.filter(user=self.user, message__icontains="running low").count(), 1
        )
        self.product.name = self.product.name
        self.product.save()
        self.assertEqual(
            Notification.objects.filter(user=self.user, message__icontains="running low").count(), 1
        )

    def test_out_of_stock_notification(self):
        self.product.stock = 0
        self.product.save()
        self.assertTrue(
            Notification.objects.filter(user=self.user, message__icontains="out of stock").exists()
        )


class OrderTotalTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("buyer1", "b1@example.com", "pass12345")
        self.product = make_product(price=Decimal("250.00"))

    def test_total_sums_items_and_subtracts_discount(self):
        order = Order.objects.create(
            user=self.user, full_name="Buyer", address="St", city="Karachi",
            phone="0300", payment_method="cod", discount_amount=Decimal("50.00"),
        )
        OrderItem.objects.create(order=order, product=self.product, product_name=self.product.name,
                                  price=Decimal("250.00"), quantity=2)
        self.assertEqual(order.total, Decimal("450.00"))

    def test_total_never_goes_negative(self):
        order = Order.objects.create(
            user=self.user, full_name="Buyer", address="St", city="Karachi",
            phone="0300", payment_method="cod", discount_amount=Decimal("999.00"),
        )
        OrderItem.objects.create(order=order, product=self.product, product_name=self.product.name,
                                  price=Decimal("250.00"), quantity=1)
        self.assertEqual(order.total, 0)

    def test_is_cancellable_only_for_pending_or_confirmed(self):
        order = Order.objects.create(
            user=self.user, full_name="Buyer", address="St", city="Karachi",
            phone="0300", payment_method="cod", status="pending",
        )
        self.assertTrue(order.is_cancellable)
        order.status = "shipped"
        self.assertFalse(order.is_cancellable)
        order.status = "delivered"
        self.assertFalse(order.is_cancellable)


class SellerAccountTests(TestCase):
    """display_name used to silently ignore organization_name - every org
    account's name fell back to the raw username everywhere it was shown."""

    def test_display_name_prefers_business_name(self):
        user = User.objects.create_user("u1", "u1@example.com", "pass12345")
        seller = SellerAccount.objects.create(
            user=user, account_type="individual", business_name="Ali's Shop",
        )
        self.assertEqual(seller.display_name, "Ali's Shop")

    def test_display_name_falls_back_to_organization_name(self):
        user = User.objects.create_user("u2", "u2@example.com", "pass12345")
        org = SellerAccount.objects.create(
            user=user, account_type="organization", organization_name="Acme Traders",
        )
        self.assertEqual(org.display_name, "Acme Traders")

    def test_display_name_falls_back_to_username_last(self):
        user = User.objects.create_user("plainuser", "u3@example.com", "pass12345")
        seller = SellerAccount.objects.create(user=user, account_type="individual")
        self.assertEqual(seller.display_name, "plainuser")

    def test_lifetime_sales_aggregate(self):
        user = User.objects.create_user("u4", "u4@example.com", "pass12345")
        seller = SellerAccount.objects.create(user=user, account_type="individual", status="approved")
        product = make_product(price=Decimal("100.00"), seller_account=seller)
        buyer = User.objects.create_user("buyer2", "b2@example.com", "pass12345")
        order = Order.objects.create(user=buyer, full_name="B", address="St", city="Karachi",
                                      phone="0300", payment_method="cod")
        item = OrderItem(order=order, product=product, product_name=product.name,
                         price=Decimal("100.00"), quantity=3)
        item.apply_commission(seller)
        item.save()
        self.assertEqual(seller.lifetime_sales, Decimal("300.00"))
        self.assertEqual(item.commission_amount, Decimal("30.00"))

    def test_commission_rate_reduces_at_volume_thresholds(self):
        user = User.objects.create_user("u5", "u5@example.com", "pass12345")
        seller = SellerAccount.objects.create(
            user=user, account_type="individual", commission_rate=Decimal("10.00"),
        )
        self.assertEqual(seller.effective_commission_rate, 10.0)


class OrganizationTeamAccessTests(TestCase):
    """The core organization feature: team members can act on behalf of the
    org account without the owner's credentials."""

    def setUp(self):
        self.owner = User.objects.create_user("owner1", "o1@example.com", "pass12345")
        self.staff = User.objects.create_user("staff1", "s1@example.com", "pass12345")
        self.outsider = User.objects.create_user("outsider1", "out1@example.com", "pass12345")
        self.org = SellerAccount.objects.create(
            user=self.owner, account_type="organization", status="approved",
            organization_name="Test Org",
        )
        OrganizationMember.objects.create(organization=self.org, user=self.staff, role="staff")

    def test_owner_resolves_to_their_own_account(self):
        account, role = get_seller_account_for_user(self.owner)
        self.assertEqual(account, self.org)
        self.assertEqual(role, "owner")

    def test_team_member_resolves_to_organization_account(self):
        account, role = get_seller_account_for_user(self.staff)
        self.assertEqual(account, self.org)
        self.assertEqual(role, "staff")

    def test_unrelated_user_has_no_seller_account(self):
        account, role = get_seller_account_for_user(self.outsider)
        self.assertIsNone(account)
        self.assertIsNone(role)

    def test_staff_can_load_seller_dashboard(self):
        client = Client()
        client.force_login(self.staff)
        response = client.get(reverse("seller_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Org")

    def test_outsider_is_sent_to_seller_signup(self):
        client = Client()
        client.force_login(self.outsider)
        response = client.get(reverse("seller_dashboard"))
        self.assertRedirects(response, reverse("become_seller"), fetch_redirect_response=False)
        self.assertEqual(client.get(reverse("seller_order", args=[999])).status_code, 302)

    def test_only_owner_can_add_team_members(self):
        client = Client()
        client.force_login(self.staff)
        new_user = User.objects.create_user("newperson", "np@example.com", "pass12345")
        client.post(reverse("add_team_member"), {"username_or_email": "newperson", "role": "staff"})
        self.assertFalse(OrganizationMember.objects.filter(user=new_user).exists())


class CorePageLoadTests(TestCase):
    """Smoke tests: the most-visited pages should always return 200."""

    def setUp(self):
        make_product(name="Homepage Product", stock=5)

    def test_homepage_loads(self):
        self.assertEqual(self.client.get(reverse("home")).status_code, 200)

    def test_all_products_loads(self):
        self.assertEqual(self.client.get(reverse("all_products")).status_code, 200)

    def test_category_page_loads(self):
        self.assertEqual(self.client.get(reverse("category_products", args=["skincare"])).status_code, 200)

    def test_product_detail_loads(self):
        product = Product.objects.first()
        self.assertEqual(self.client.get(reverse("product_detail", args=[product.id])).status_code, 200)


class CartAndCheckoutTests(TestCase):
    def setUp(self):
        self.product = make_product(stock=5)
        self.user = User.objects.create_user("shopper", "sh@example.com", "pass12345")

    def test_add_to_cart_updates_session(self):
        response = self.client.post(
            reverse("add_to_cart", args=[self.product.id]),
            {"quantity": 2},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session["cart"], {str(self.product.id): 2})

    def test_add_to_cart_cannot_exceed_stock(self):
        self.client.post(
            reverse("add_to_cart", args=[self.product.id]),
            {"quantity": 999},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(self.client.session["cart"][str(self.product.id)], self.product.stock)

    def test_empty_checkout_goes_back_to_cart(self):
        response = self.client.get(reverse("checkout"))
        self.assertRedirects(response, reverse("cart"))

    def test_cancel_order_only_works_for_own_pending_order(self):
        other_user = User.objects.create_user("someone_else", "oe@example.com", "pass12345")
        order = Order.objects.create(
            user=other_user, full_name="Someone Else", address="St", city="Karachi",
            phone="0300", payment_method="cod", status="pending",
        )
        self.client.force_login(self.user)
        response = self.client.post(reverse("cancel_order", args=[order.id]))
        self.assertEqual(response.status_code, 404)
        order.refresh_from_db()
        self.assertEqual(order.status, "pending")


class CouponValidationTests(TestCase):
    """Coupons used to have no expiry, no usage limit, no per-user limit,
    and no minimum order check - any active code worked forever, for
    anyone, any number of times."""

    def setUp(self):
        self.user = User.objects.create_user("shopper1", "sh1@example.com", "pass12345")
        self.other_user = User.objects.create_user("shopper2", "sh2@example.com", "pass12345")
        self.product = make_product(price=Decimal("500.00"))

    def _make_order(self, user, coupon_code, status="confirmed"):
        order = Order.objects.create(
            user=user, full_name="Buyer", address="St", city="Karachi",
            phone="0300", payment_method="cod", coupon_code=coupon_code, status=status,
        )
        OrderItem.objects.create(order=order, product=self.product, product_name=self.product.name,
                                  price=Decimal("500.00"), quantity=1)
        return order

    def test_inactive_coupon_is_invalid(self):
        from .models import Coupon
        coupon = Coupon.objects.create(code="OFF10", percent_off=10, active=False)
        is_valid, error = coupon.is_valid_for(self.user, Decimal("1000"))
        self.assertFalse(is_valid)
        self.assertIn("no longer active", error)

    def test_expired_coupon_is_invalid(self):
        from datetime import timedelta
        from django.utils import timezone
        from .models import Coupon
        coupon = Coupon.objects.create(
            code="OLD10", percent_off=10, expiry_date=timezone.localdate() - timedelta(days=1),
        )
        is_valid, error = coupon.is_valid_for(self.user, Decimal("1000"))
        self.assertFalse(is_valid)
        self.assertIn("expired", error)

    def test_future_expiry_is_still_valid(self):
        from datetime import timedelta
        from django.utils import timezone
        from .models import Coupon
        coupon = Coupon.objects.create(
            code="NEW10", percent_off=10, expiry_date=timezone.localdate() + timedelta(days=1),
        )
        is_valid, error = coupon.is_valid_for(self.user, Decimal("1000"))
        self.assertTrue(is_valid)

    def test_minimum_order_value_enforced(self):
        from .models import Coupon
        coupon = Coupon.objects.create(code="BIG50", percent_off=50, min_order_value=Decimal("2000"))
        is_valid, error = coupon.is_valid_for(self.user, Decimal("500"))
        self.assertFalse(is_valid)
        self.assertIn("minimum order", error)
        is_valid, error = coupon.is_valid_for(self.user, Decimal("2500"))
        self.assertTrue(is_valid)

    def test_global_usage_limit_enforced(self):
        from .models import Coupon
        coupon = Coupon.objects.create(code="LIMITED", percent_off=10, usage_limit=1)
        self._make_order(self.user, "LIMITED")
        # Already used once globally, limit is 1 - a different user should now be blocked too.
        is_valid, error = coupon.is_valid_for(self.other_user, Decimal("1000"))
        self.assertFalse(is_valid)
        self.assertIn("usage limit", error)

    def test_cancelled_orders_dont_count_against_usage_limit(self):
        from .models import Coupon
        coupon = Coupon.objects.create(code="LIMITED2", percent_off=10, usage_limit=1)
        self._make_order(self.user, "LIMITED2", status="cancelled")
        is_valid, error = coupon.is_valid_for(self.other_user, Decimal("1000"))
        self.assertTrue(is_valid)

    def test_per_user_limit_enforced(self):
        from .models import Coupon
        coupon = Coupon.objects.create(code="ONEUSE", percent_off=10, per_user_limit=1)
        self._make_order(self.user, "ONEUSE")
        # This user already used it once - should now be blocked for them...
        is_valid, error = coupon.is_valid_for(self.user, Decimal("1000"))
        self.assertFalse(is_valid)
        self.assertIn("maximum number of times", error)
        # ...but a different user should still be able to use it.
        is_valid, error = coupon.is_valid_for(self.other_user, Decimal("1000"))
        self.assertTrue(is_valid)

    def test_apply_coupon_view_rejects_invalid_code(self):
        self.client.force_login(self.user)
        self.client.post(reverse("add_to_cart", args=[self.product.id]), {"quantity": 1})
        response = self.client.post(reverse("apply_coupon"), {"coupon_code": "DOESNOTEXIST"}, follow=True)
        self.assertContains(response, "coupon code isn")

    def test_apply_coupon_view_accepts_valid_code(self):
        from .models import Coupon
        Coupon.objects.create(code="WORKS10", percent_off=10)
        self.client.force_login(self.user)
        self.client.post(reverse("add_to_cart", args=[self.product.id]), {"quantity": 1})
        response = self.client.post(reverse("apply_coupon"), {"coupon_code": "WORKS10"}, follow=True)
        self.assertContains(response, "Coupon applied")


class ProductFilterTests(TestCase):
    def setUp(self):
        make_product(name="Cheap Item", price=Decimal("100.00"), stock=5, seller_name="SellerA")
        make_product(name="Mid Item", price=Decimal("500.00"), stock=0, seller_name="SellerB")
        make_product(name="Expensive Item", price=Decimal("2000.00"), stock=10, seller_name="SellerA")

    def test_price_range_filter(self):
        response = self.client.get(reverse("all_products"), {"min_price": "200", "max_price": "1000"})
        self.assertContains(response, "Mid Item")
        self.assertNotContains(response, "Cheap Item")
        self.assertNotContains(response, "Expensive Item")

    def test_in_stock_filter_excludes_zero_stock(self):
        response = self.client.get(reverse("all_products"), {"in_stock": "1"})
        self.assertNotContains(response, "Mid Item")
        self.assertContains(response, "Cheap Item")

    def test_seller_filter(self):
        response = self.client.get(reverse("all_products"), {"seller": "SellerB"})
        self.assertContains(response, "Mid Item")
        self.assertNotContains(response, "Cheap Item")

    def test_filters_combine_with_category_page(self):
        response = self.client.get(reverse("category_products", args=["skincare"]), {"min_price": "1000"})
        self.assertContains(response, "Expensive Item")
        self.assertNotContains(response, "Cheap Item")


class ChatTests(TestCase):
    def test_guest_can_send_and_read_chat_messages(self):
        response = self.client.post(reverse("chat_send"), {"message": "Hello, is anyone there?"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("reply", data)
        self.assertTrue(data["reply"]["message"])

        history = self.client.get(reverse("chat_messages"))
        messages = history.json()["messages"]
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["sender"], "user")
        self.assertEqual(messages[0]["message"], "Hello, is anyone there?")
        self.assertEqual(messages[1]["sender"], "support")

    def test_empty_chat_message_rejected(self):
        response = self.client.post(reverse("chat_send"), {"message": "   "})
        self.assertEqual(response.status_code, 400)

    def test_order_keyword_triggers_relevant_auto_reply(self):
        response = self.client.post(reverse("chat_send"), {"message": "where is my order tracking"})
        self.assertIn("My orders", response.json()["reply"]["message"])

    def test_logged_in_user_thread_persists_across_requests(self):
        user = User.objects.create_user(username="chatuser", password="pass12345")
        self.client.force_login(user)
        self.client.post(reverse("chat_send"), {"message": "First message"})
        self.client.post(reverse("chat_send"), {"message": "Second message"})

        from .models import ChatThread
        self.assertEqual(ChatThread.objects.filter(user=user).count(), 1)
        history = self.client.get(reverse("chat_messages")).json()["messages"]
        user_messages = [m for m in history if m["sender"] == "user"]
        self.assertEqual(len(user_messages), 2)


# ---------------------------------------------------------------------------
# Checkout, Stripe and security regression tests
# ---------------------------------------------------------------------------
import json
import os
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from .models import Address, Coupon, NewsletterSubscriber, ProductImage, SiteSettings


CHECKOUT_FORM = {
    "full_name": "Jane Doe", "email": "jane@example.com", "phone": "+1 555 0100",
    "address": "1 Main St", "city": "Austin", "state": "TX", "postal_code": "73301", "country": "us",
}


class CheckoutTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("jane", "jane@example.com", "pass12345")
        self.product = make_product(price=Decimal("20.00"), stock=3)
        self.client.force_login(self.user)

    def _add(self, qty=1, product=None):
        self.client.post(reverse("add_to_cart", args=[(product or self.product).id]), {"quantity": qty})

    def test_cod_checkout_creates_order_and_decrements_stock(self):
        self._add(2)
        response = self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "cod"})
        order = Order.objects.get(user=self.user)
        self.assertRedirects(response, reverse("order_success", args=[order.id]))
        self.assertEqual(order.country, "US")
        self.assertEqual(order.total, Decimal("40.00"))
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 1)
        self.assertEqual(self.client.session["cart"], {})

    def test_checkout_blocks_when_stock_ran_out(self):
        self._add(3)
        Product.objects.filter(pk=self.product.pk).update(stock=1)  # someone else bought it
        response = self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "cod"})
        self.assertRedirects(response, reverse("cart"))
        self.assertFalse(Order.objects.exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 1)

    def test_missing_address_fields_rejected(self):
        self._add()
        form = {**CHECKOUT_FORM, "postal_code": "", "payment_method": "cod"}
        response = self.client.post(reverse("checkout"), form)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Order.objects.exists())

    def test_shipping_and_tax_applied(self):
        settings_obj = SiteSettings.load()
        settings_obj.shipping_flat_fee = Decimal("5.00")
        settings_obj.tax_percent = Decimal("10")
        settings_obj.save()
        self._add(1)
        self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "cod"})
        order = Order.objects.get()
        self.assertEqual(order.shipping_amount, Decimal("5.00"))
        self.assertEqual(order.tax_amount, Decimal("2.00"))
        self.assertEqual(order.total, Decimal("27.00"))

    def test_free_shipping_threshold(self):
        settings_obj = SiteSettings.load()
        settings_obj.shipping_flat_fee = Decimal("5.00")
        settings_obj.free_shipping_threshold = Decimal("30.00")
        settings_obj.save()
        self._add(2)
        self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "cod"})
        self.assertEqual(Order.objects.get().shipping_amount, Decimal("0"))

    def test_cod_disabled_hides_method(self):
        settings_obj = SiteSettings.load()
        settings_obj.allow_cash_on_delivery = False
        settings_obj.save()
        self._add()
        response = self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "cod"})
        self.assertRedirects(response, reverse("checkout"), fetch_redirect_response=False)
        self.assertFalse(Order.objects.exists())

    def test_coupon_applied_to_order(self):
        Coupon.objects.create(code="TEN", percent_off=10)
        self._add(1)
        self.client.post(reverse("apply_coupon"), {"coupon_code": "ten"})
        self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "cod"})
        order = Order.objects.get()
        self.assertEqual(order.coupon_code, "TEN")
        self.assertEqual(order.discount_amount, Decimal("2.00"))

    def test_negative_quantity_cannot_lower_cart_total(self):
        self.client.post(reverse("add_to_cart", args=[self.product.id]), {"quantity": -5})
        self.assertEqual(self.client.session["cart"][str(self.product.id)], 1)

    def test_cannot_add_unapproved_product(self):
        hidden = make_product(name="Hidden", approval_status="pending")
        response = self.client.post(reverse("add_to_cart", args=[hidden.id]))
        self.assertEqual(response.status_code, 404)

    def test_cancel_cod_order_restocks(self):
        self._add(2)
        self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "cod"})
        order = Order.objects.get()
        self.client.post(reverse("cancel_order", args=[order.id]))
        order.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(self.product.stock, 3)

    def test_cancel_requires_post(self):
        self._add(1)
        self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "cod"})
        order = Order.objects.get()
        response = self.client.get(reverse("cancel_order", args=[order.id]))
        self.assertEqual(response.status_code, 405)


def _fake_session(order, **overrides):
    data = {
        "id": order.stripe_session_id or "cs_test_123",
        "payment_status": "paid",
        "status": "complete",
        "amount_total": order.total_cents,
        "currency": order.currency,
        "payment_intent": "pi_test_123",
        "metadata": {"order_id": str(order.id)},
    }
    data.update(overrides)
    return data


@override_settings(STRIPE_SECRET_KEY="sk_test_dummy", STRIPE_WEBHOOK_SECRET="whsec_dummy")
class StripeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("payer", "payer@example.com", "pass12345")
        self.product = make_product(price=Decimal("25.00"), stock=5)
        self.client.force_login(self.user)

    def _checkout_card(self, qty=2):
        self.client.post(reverse("add_to_cart", args=[self.product.id]), {"quantity": qty})
        fake = mock.MagicMock()
        fake.id = "cs_test_123"
        fake.url = "https://checkout.stripe.com/c/pay/cs_test_123"
        with mock.patch("stripe.checkout.Session.create", return_value=fake) as create:
            response = self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "card"})
        return response, create

    def test_card_checkout_redirects_to_stripe_with_correct_amounts(self):
        response, create = self._checkout_card()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("https://checkout.stripe.com/"))
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["line_items"][0]["price_data"]["unit_amount"], 2500)
        self.assertEqual(kwargs["line_items"][0]["quantity"], 2)
        self.assertEqual(kwargs["metadata"]["order_id"], str(Order.objects.get().id))
        order = Order.objects.get()
        self.assertEqual(order.payment_status, "pending")
        self.assertEqual(order.stripe_session_id, "cs_test_123")
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)  # reserved while paying

    def test_stripe_failure_releases_stock_and_restores_cart(self):
        self.client.post(reverse("add_to_cart", args=[self.product.id]), {"quantity": 2})
        with mock.patch("stripe.checkout.Session.create", side_effect=Exception("boom")):
            response = self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "card"})
        self.assertRedirects(response, reverse("cart"))
        order = Order.objects.get()
        self.assertEqual(order.status, "cancelled")
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)
        self.assertEqual(self.client.session["cart"], {str(self.product.id): 2})

    def _webhook(self, event_type, session):
        payload = json.dumps({"type": event_type, "data": {"object": session}})
        with mock.patch("stripe.Webhook.construct_event", return_value={}):
            return self.client.post(
                reverse("stripe_webhook"), data=payload, content_type="application/json",
                HTTP_STRIPE_SIGNATURE="t=1,v1=fake",
            )

    def test_webhook_marks_order_paid(self):
        self._checkout_card()
        order = Order.objects.get()
        response = self._webhook("checkout.session.completed", _fake_session(order))
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "paid")
        self.assertEqual(order.status, "confirmed")
        self.assertEqual(order.stripe_payment_intent, "pi_test_123")

    def test_webhook_rejects_amount_mismatch(self):
        self._checkout_card()
        order = Order.objects.get()
        self._webhook("checkout.session.completed", _fake_session(order, amount_total=100))
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")

    def test_webhook_bad_signature_rejected(self):
        with mock.patch("stripe.Webhook.construct_event", side_effect=ValueError("bad")):
            response = self.client.post(reverse("stripe_webhook"), data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_expired_session_cancels_and_restocks(self):
        self._checkout_card()
        order = Order.objects.get()
        self._webhook("checkout.session.expired", _fake_session(order, payment_status="unpaid", status="expired"))
        order.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(self.product.stock, 5)

    def test_webhook_is_idempotent(self):
        self._checkout_card()
        order = Order.objects.get()
        self._webhook("checkout.session.completed", _fake_session(order))
        self._webhook("checkout.session.completed", _fake_session(order))
        self._webhook("checkout.session.expired", _fake_session(order, status="expired"))
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "paid")
        self.assertEqual(order.status, "confirmed")

    def test_success_page_confirms_payment(self):
        self._checkout_card()
        order = Order.objects.get()
        fake = mock.MagicMock()
        fake.to_dict.return_value = _fake_session(order)
        with mock.patch("stripe.checkout.Session.retrieve", return_value=fake):
            response = self.client.get(reverse("payment_success"), {"session_id": "cs_test_123"})
        self.assertRedirects(response, reverse("order_success", args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "paid")

    def test_cancel_page_restores_cart(self):
        self._checkout_card()
        order = Order.objects.get()
        fake = mock.MagicMock()
        fake.to_dict.return_value = _fake_session(order, payment_status="unpaid", status="open")
        with mock.patch("stripe.checkout.Session.retrieve", return_value=fake), \
                mock.patch("stripe.checkout.Session.expire"):
            response = self.client.get(reverse("payment_cancel", args=[order.id]))
        self.assertRedirects(response, reverse("cart"))
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(self.client.session["cart"], {str(self.product.id): 2})

    def test_cancelling_paid_order_refunds(self):
        self._checkout_card()
        order = Order.objects.get()
        self._webhook("checkout.session.completed", _fake_session(order))
        with mock.patch("stripe.Refund.create") as refund:
            self.client.post(reverse("cancel_order", args=[order.id]))
        refund.assert_called_once()
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "refunded")
        self.assertEqual(order.status, "cancelled")

    def test_refund_failure_keeps_order_active(self):
        self._checkout_card()
        order = Order.objects.get()
        self._webhook("checkout.session.completed", _fake_session(order))
        with mock.patch("stripe.Refund.create", side_effect=Exception("declined")):
            self.client.post(reverse("cancel_order", args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "paid")
        self.assertEqual(order.status, "confirmed")


class SecurityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "Sup3r-secret-pass")

    def test_login_ignores_external_next_url(self):
        response = self.client.post(
            reverse("login") + "?next=https://evil.example/phish",
            {"username": "alice", "password": "Sup3r-secret-pass"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("home"))

    def test_login_allows_local_next_url(self):
        response = self.client.post(
            reverse("login"), {"username": "alice", "password": "Sup3r-secret-pass", "next": "/my-orders/"},
        )
        self.assertEqual(response.url, "/my-orders/")

    def test_login_with_email(self):
        response = self.client.post(reverse("login"), {"username": "ALICE@example.com", "password": "Sup3r-secret-pass"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.id)

    def test_spoofed_forwarded_for_does_not_bypass_rate_limit(self):
        from django.core.cache import cache
        cache.clear()
        for i in range(8):
            self.client.post(reverse("login"), {"username": "alice", "password": "wrong"},
                             HTTP_X_FORWARDED_FOR=f"10.0.0.{i}")
        response = self.client.post(reverse("login"), {"username": "alice", "password": "Sup3r-secret-pass"},
                                    HTTP_X_FORWARDED_FOR="10.0.0.99")
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)
        cache.clear()

    def test_google_login_route_removed(self):
        self.assertEqual(self.client.post("/auth/google/").status_code, 404)

    def test_signup_rejects_weak_password_and_duplicate_email(self):
        self.client.post(reverse("signup"), {
            "username": "bob", "email": "bob@example.com", "password": "123", "confirm_password": "123",
        })
        self.assertFalse(User.objects.filter(username="bob").exists())
        self.client.post(reverse("signup"), {
            "username": "bob2", "email": "ALICE@example.com",
            "password": "Another-strong-pass1", "confirm_password": "Another-strong-pass1",
        })
        self.assertFalse(User.objects.filter(username="bob2").exists())

    def test_signup_rejects_email_as_username(self):
        self.client.post(reverse("signup"), {
            "username": "victim@example.com", "email": "attacker@example.com",
            "password": "Another-strong-pass1", "confirm_password": "Another-strong-pass1",
        })
        self.assertFalse(User.objects.filter(username="victim@example.com").exists())

    def test_logout_requires_post(self):
        self.client.force_login(self.user)
        self.client.get(reverse("logout"))
        self.assertIn("_auth_user_id", self.client.session)
        self.client.post(reverse("logout"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_invoice_of_guest_order_not_public(self):
        order = Order.objects.create(user=None, full_name="Guest", address="x", city="y", phone="1")
        self.assertEqual(self.client.get(reverse("invoice_pdf", args=[order.id])).status_code, 404)
        self.assertEqual(self.client.get(reverse("invoice_pdf", args=[order.id]) + "?t=wrong").status_code, 404)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("invoice_pdf", args=[order.id])).status_code, 404)
        self.assertEqual(Client().get(reverse("invoice_pdf", args=[order.id]) + f"?t={order.access_token}").status_code, 200)

    def test_email_code_locks_after_too_many_attempts(self):
        from django.core.cache import cache
        self.client.force_login(self.user)
        Profile_ = __import__("bees.models", fromlist=["Profile"]).Profile
        Profile_.objects.create(user=self.user, referral_code="ABC123")
        cache.set(f"email_verify_code:{self.user.id}", "123456", 900)
        for _ in range(5):
            self.client.post(reverse("verify_email"), {"code": "000000"})
        self.client.post(reverse("verify_email"), {"code": "123456"})
        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.email_verified)

    def test_review_requires_login_and_clamps_rating(self):
        product = make_product()
        self.client.post(reverse("product_detail", args=[product.id]), {"rating": 5, "comment": "Spam"})
        self.assertFalse(Review.objects.exists())
        self.client.force_login(self.user)
        self.client.post(reverse("product_detail", args=[product.id]), {"rating": 4, "comment": "Never bought it"})
        self.assertFalse(Review.objects.exists())  # only customers who received it can review
        order = Order.objects.create(user=self.user, full_name="R", address="x", city="y", phone="1", status="delivered")
        OrderItem.objects.create(order=order, product=product, product_name=product.name, price=product.price, quantity=1)
        self.client.post(reverse("product_detail", args=[product.id]), {"rating": 999, "comment": "Great"})
        self.assertFalse(Review.objects.exists())
        self.client.post(reverse("product_detail", args=[product.id]), {"rating": 4, "comment": "Great"})
        self.client.post(reverse("product_detail", args=[product.id]), {"rating": 5, "comment": "Even better"})
        self.assertEqual(Review.objects.get().rating, 5)


class SellerProductSecurityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("maker", "maker@example.com", "pass12345")
        self.seller = SellerAccount.objects.create(user=self.user, status="approved", business_name="Maker Co")
        self.product = make_product(seller_account=self.seller, seller_name="Maker Co", approval_status="approved")
        self.client.force_login(self.user)

    def _form(self, **overrides):
        data = {
            "name": self.product.name, "category": "skincare", "price": "10.00", "stock": "4",
            "image_url": self.product.image_url, "description": self.product.description,
        }
        data.update(overrides)
        return data

    def test_editing_listing_details_requires_re_approval(self):
        self.client.post(reverse("seller_edit_product", args=[self.product.id]), self._form(name="Totally different"))
        self.product.refresh_from_db()
        self.assertEqual(self.product.approval_status, "pending")

    def test_price_and_stock_changes_stay_live(self):
        self.client.post(reverse("seller_edit_product", args=[self.product.id]), self._form(price="12.50", stock="9"))
        self.product.refresh_from_db()
        self.assertEqual(self.product.approval_status, "approved")
        self.assertEqual(self.product.price, Decimal("12.50"))

    def test_html_upload_rejected(self):
        evil = SimpleUploadedFile("x.html", b"<script>alert(1)</script>", content_type="text/html")
        self.client.post(reverse("seller_add_product"), {**self._form(name="New", image_url=""), "image_file": evil})
        self.assertFalse(Product.objects.filter(name="New").exists())

    def test_fake_image_rejected(self):
        evil = SimpleUploadedFile("x.png", b"<svg onload=alert(1)>", content_type="image/png")
        self.client.post(reverse("seller_add_product"), {**self._form(name="New2", image_url=""), "image_file": evil})
        self.assertFalse(Product.objects.filter(name="New2").exists())

    def test_invalid_price_does_not_crash(self):
        response = self.client.post(reverse("seller_add_product"), self._form(name="Bad", price="abc"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Product.objects.filter(name="Bad").exists())

    def test_delete_requires_post(self):
        response = self.client.get(reverse("seller_delete_product", args=[self.product.id]))
        self.assertEqual(response.status_code, 405)
        self.assertTrue(Product.objects.filter(pk=self.product.pk).exists())

    def test_seller_documents_are_staff_only(self):
        response = self.client.get(reverse("seller_document", args=[self.seller.id, "id_document"]))
        self.assertEqual(response.status_code, 403)


class WhiteLabelTests(TestCase):
    def test_store_name_comes_from_settings(self):
        make_product(name="Branded Product")
        settings_obj = SiteSettings.load()
        settings_obj.site_name = "Acme Goods"
        settings_obj.save()
        response = self.client.get(reverse("home"))
        self.assertContains(response, "Acme Goods")
        self.assertNotContains(response, "19Bees")

    @override_settings(STORE_CURRENCY="eur")
    def test_prices_use_store_currency(self):
        make_product(name="Euro Product", price=Decimal("1234.50"))
        response = self.client.get(reverse("all_products"))
        self.assertContains(response, "€1,234.50")


# ---------------------------------------------------------------------------
# Store admin (/manage/)
# ---------------------------------------------------------------------------
from django.core import mail

from .models import ChatMessage, ChatThread, Profile, ProductVariant, Question, ReturnRequest


class ManageAccessTests(TestCase):
    PAGES = ["manage_dashboard", "manage_orders", "manage_products", "manage_product_new", "manage_sellers",
             "manage_customers", "manage_coupons", "manage_reviews", "manage_returns", "manage_support", "manage_settings"]

    def setUp(self):
        self.staff = User.objects.create_user("boss", "boss@example.com", "pass12345", is_staff=True)
        self.shopper = User.objects.create_user("shop", "shop@example.com", "pass12345")
        make_product(name="Admin Visible Product")

    def test_anonymous_redirected_to_login(self):
        for name in self.PAGES:
            r = self.client.get(reverse(name))
            self.assertEqual(r.status_code, 302, name)
            self.assertIn("/login/", r.url)

    def test_customers_are_forbidden(self):
        self.client.force_login(self.shopper)
        for name in self.PAGES:
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)

    def test_staff_can_open_every_page(self):
        self.client.force_login(self.staff)
        for name in self.PAGES:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        self.assertEqual(self.client.get(reverse("manage_reviews") + "?tab=questions").status_code, 200)
        self.assertEqual(self.client.get(reverse("manage_dashboard") + "?range=30").status_code, 200)

    def test_admin_icon_only_for_staff(self):
        self.client.force_login(self.shopper)
        self.assertNotContains(self.client.get(reverse("home")), f'href="{reverse("manage_dashboard")}"')
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(reverse("home")), f'href="{reverse("manage_dashboard")}"')

    def test_old_dashboard_url_redirects(self):
        self.client.force_login(self.staff)
        self.assertRedirects(self.client.get(reverse("owner_dashboard")), reverse("manage_dashboard"))


class ManageActionTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("boss", "boss@example.com", "pass12345", is_staff=True)
        self.buyer = User.objects.create_user("buyer", "buyer@example.com", "pass12345")
        self.product = make_product(price=Decimal("30.00"), stock=5)
        self.client.force_login(self.staff)

    def _order(self, **kw):
        order = Order.objects.create(user=self.buyer, email="buyer@example.com", full_name="Buy Er", address="1 St",
                                     city="Austin", postal_code="1", country="US", phone="1", **kw)
        OrderItem.objects.create(order=order, product=self.product, product_name=self.product.name, price=Decimal("30.00"), quantity=2)
        return order

    def test_marking_shipped_emails_customer_and_notifies(self):
        order = self._order(status="confirmed")
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("manage_order", args=[order.id]), {
                "action": "update", "status": "shipped", "tracking_number": "1Z999", "courier_name": "UPS", "estimated_delivery": "2026-12-01",
            })
        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")
        self.assertEqual(order.tracking_number, "1Z999")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("1Z999", mail.outbox[0].alternatives[0][0])
        self.assertTrue(Notification.objects.filter(user=self.buyer, message__icontains="Shipped").exists())

    def test_cancel_restocks(self):
        order = self._order(status="pending")
        Product.objects.filter(pk=self.product.pk).update(stock=3)
        self.client.post(reverse("manage_order", args=[order.id]), {"action": "cancel"})
        order.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(self.product.stock, 5)

    def test_orders_csv_export(self):
        self._order()
        r = self.client.get(reverse("manage_orders") + "?export=csv")
        self.assertEqual(r["Content-Type"], "text/csv")
        self.assertIn(b"Buy Er", r.content)

    def test_create_product(self):
        r = self.client.post(reverse("manage_product_new"), {
            "name": "Staff Made", "category": "skincare", "price": "12.50", "stock": "7",
            "image_url": "https://example.com/a.jpg", "approval_status": "approved", "is_flash_sale": "1",
            "extra_images": "https://example.com/b.jpg\nnot-a-url",
        })
        p = Product.objects.get(name="Staff Made")
        self.assertRedirects(r, reverse("manage_product_edit", args=[p.id]))
        self.assertTrue(p.is_flash_sale)
        self.assertEqual(p.extra_images.count(), 1)

    def test_bulk_approve_notifies_seller(self):
        seller_user = User.objects.create_user("sel", "sel@example.com", "pass12345")
        seller = SellerAccount.objects.create(user=seller_user, status="approved", business_name="Sel Co")
        pending = make_product(name="Pending One", seller_account=seller, approval_status="pending")
        self.client.post(reverse("manage_products_bulk"), {"ids": [pending.id], "action": "approve"})
        pending.refresh_from_db()
        self.assertEqual(pending.approval_status, "approved")
        self.assertTrue(Notification.objects.filter(user=seller_user, message__icontains="approved").exists())

    def test_seller_approve_and_payout(self):
        u = User.objects.create_user("app", "app@example.com", "pass12345")
        s = SellerAccount.objects.create(user=u, business_name="App Shop")
        self.client.post(reverse("manage_seller", args=[s.id]), {"action": "approved"})
        s.refresh_from_db()
        self.assertEqual(s.status, "approved")
        self.client.post(reverse("manage_seller", args=[s.id]), {"action": "payout", "amount": "25.50"})
        s.refresh_from_db()
        self.assertEqual(s.total_paid_out, Decimal("25.50"))
        self.client.post(reverse("manage_seller", args=[s.id]), {"action": "payout", "amount": "-4"})
        s.refresh_from_db()
        self.assertEqual(s.total_paid_out, Decimal("25.50"))

    def test_coupon_create_toggle_delete(self):
        self.client.post(reverse("manage_coupons"), {"action": "create", "code": "spring", "percent_off": "15",
                                                      "min_order_value": "0", "per_user_limit": "1", "active": "on"})
        c = Coupon.objects.get(code="SPRING")
        self.client.post(reverse("manage_coupons"), {"action": "toggle", "id": c.id})
        c.refresh_from_db()
        self.assertFalse(c.active)
        self.client.post(reverse("manage_coupons"), {"action": "create", "code": "bad", "percent_off": "500",
                                                      "min_order_value": "0", "per_user_limit": "1"})
        self.assertFalse(Coupon.objects.filter(code="BAD").exists())
        self.client.post(reverse("manage_coupons"), {"action": "delete", "id": c.id})
        self.assertFalse(Coupon.objects.filter(pk=c.id).exists())

    def test_answer_question(self):
        qn = Question.objects.create(product=self.product, username="q", question="Waterproof?")
        self.client.post(reverse("manage_reviews") + "?tab=questions", {"action": "answer", "id": qn.id, "answer": "Yes."})
        qn.refresh_from_db()
        self.assertEqual(qn.answer, "Yes.")

    @override_settings(STRIPE_SECRET_KEY="sk_test_dummy")
    def test_return_refund_to_card_is_partial(self):
        order = self._order(status="delivered", payment_method="card", payment_status="paid", stripe_payment_intent="pi_1")
        item = order.items.get()
        rr = ReturnRequest.objects.create(order_item=item, user=self.buyer, reason="Broken")
        with mock.patch("stripe.Refund.create") as refund:
            self.client.post(reverse("manage_returns"), {"id": rr.id, "action": "refund"})
        self.assertEqual(refund.call_args.kwargs["amount"], 6000)
        rr.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(rr.status, "refunded")
        self.assertEqual(self.product.stock, 7)

    def test_support_reply(self):
        thread = ChatThread.objects.create(user=self.buyer)
        ChatMessage.objects.create(thread=thread, sender="user", message="Where is my order?")
        self.client.get(reverse("manage_support_thread", args=[thread.id]))
        self.assertFalse(thread.messages.filter(is_read=False).exists())
        self.client.post(reverse("manage_support_thread", args=[thread.id]), {"action": "reply", "message": "Shipping today!"})
        self.assertTrue(thread.messages.filter(sender="support", message="Shipping today!").exists())
        self.client.logout()
        self.client.force_login(self.buyer)
        msgs = self.client.get(reverse("chat_messages")).json()["messages"]
        self.assertEqual(msgs[-1]["message"], "Shipping today!")

    def test_settings_update_rebrands_store(self):
        data = {f: v for f, v in {
            "site_name": "Nova Goods", "tagline": "Good things", "primary_color": "#112233", "accent_color": "#FFCC00",
            "hero_title": "Hello", "hero_subtitle": "World", "tax_percent": "0", "shipping_flat_fee": "0",
            "free_shipping_threshold": "0", "delivery_days": "5", "return_days": "14", "allow_cash_on_delivery": "on",
        }.items()}
        self.client.post(reverse("manage_settings"), data)
        self.assertContains(self.client.get(reverse("home")), "Nova Goods")
        bad = dict(data, primary_color="red")
        self.client.post(reverse("manage_settings"), bad)
        self.assertEqual(SiteSettings.load().primary_color, "#112233")


class SpeculativeRequestTests(TestCase):
    def test_prefetch_does_not_count_as_search(self):
        from .models import SearchLog
        make_product(name="Oil Lamp")
        self.client.get(reverse("search_products"), {"q": "oil"}, HTTP_SEC_PURPOSE="prefetch;prerender")
        self.assertFalse(SearchLog.objects.exists())
        self.client.get(reverse("search_products"), {"q": "oil"})
        self.assertTrue(SearchLog.objects.exists())


class MarketplaceFlowTests(TestCase):
    """End to end: a business signs up to sell, gets approved, lists a
    product, a buyer orders it, and the commission is recorded."""

    def test_full_seller_to_buyer_flow(self):
        staff = User.objects.create_user("boss", "boss@example.com", "pass12345", is_staff=True)
        # 1. A business applies to sell
        self.client.post(reverse("signup"), {
            "username": "acme", "email": "acme@example.com", "password": "Strong-pass-123", "confirm_password": "Strong-pass-123",
            "user_type": "organization", "organization_name": "Acme Ltd", "phone": "+1 555 0101", "country": "US",
        })
        seller = SellerAccount.objects.get(user__username="acme")
        self.assertEqual(seller.status, "pending")
        self.assertEqual(seller.commission_rate, Decimal("20"))
        # 2. Staff approve the seller
        self.client.force_login(staff)
        self.client.post(reverse("manage_seller", args=[seller.id]), {"action": "approved"})
        # 3. Seller lists a product; it waits for review
        self.client.force_login(seller.user)
        self.client.post(reverse("seller_add_product"), {
            "name": "Acme Lamp", "category": "table-lamp", "price": "50.00", "stock": "4",
            "image_url": "https://example.com/lamp.jpg", "description": "A lamp",
        })
        product = Product.objects.get(name="Acme Lamp")
        self.assertEqual(product.approval_status, "pending")
        self.assertEqual(self.client.get(reverse("product_detail", args=[product.id])).status_code, 200)  # owner preview
        self.client.logout()
        self.assertEqual(self.client.get(reverse("product_detail", args=[product.id])).status_code, 404)  # hidden from public
        # 4. Staff approve the product
        self.client.force_login(staff)
        self.client.post(reverse("manage_products_bulk"), {"ids": [product.id], "action": "approve"})
        # 5. A buyer orders 2 with cash on delivery
        buyer = User.objects.create_user("buyer", "buyer@example.com", "pass12345")
        self.client.force_login(buyer)
        self.client.post(reverse("add_to_cart", args=[product.id]), {"quantity": 2})
        self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "cod"})
        item = OrderItem.objects.get(product=product)
        self.assertEqual(item.seller_account, seller)
        self.assertEqual(item.commission_rate, Decimal("20"))
        self.assertEqual(item.commission_amount, Decimal("20.00"))
        seller.refresh_from_db()
        self.assertEqual(seller.lifetime_sales, Decimal("100.00"))
        self.assertEqual(seller.net_earnings, 80.0)
        # 6. The seller is alerted and sees the sale, the address and their earning
        self.assertTrue(Notification.objects.filter(user=seller.user, message__icontains=f"New order #{item.order_id}").exists())
        self.client.force_login(seller.user)
        page = self.client.get(reverse("seller_dashboard"))
        self.assertContains(page, "Acme Lamp")
        self.assertContains(page, "$80.00")
        page = self.client.get(reverse("seller_orders"))
        self.assertContains(page, "Austin")
        page = self.client.get(reverse("seller_order", args=[item.order_id]))
        self.assertContains(page, "Austin")
        self.assertContains(page, "$80.00")
        # 7. Cancelled orders don't count toward earnings
        self.client.force_login(buyer)
        self.client.post(reverse("cancel_order", args=[item.order.id]))
        seller = SellerAccount.objects.get(pk=seller.pk)
        self.assertEqual(seller.lifetime_sales, 0)
        product.refresh_from_db()
        self.assertEqual(product.stock, 4)

    def test_store_products_have_no_commission(self):
        buyer = User.objects.create_user("b2", "b2@example.com", "pass12345")
        p = make_product(price=Decimal("10.00"))
        self.client.force_login(buyer)
        self.client.post(reverse("add_to_cart", args=[p.id]))
        self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "cod"})
        item = OrderItem.objects.get()
        self.assertIsNone(item.seller_account)
        self.assertEqual(item.commission_amount, 0)


@override_settings(STRIPE_SECRET_KEY="sk_test_dummy", STRIPE_WEBHOOK_SECRET="whsec_dummy")
class PayCodOrderOnlineTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("payer", "payer@example.com", "pass12345")
        self.product = make_product(price=Decimal("25.00"), stock=5)
        self.client.force_login(self.user)
        self.client.post(reverse("add_to_cart", args=[self.product.id]))
        self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "cod"})
        self.order = Order.objects.get()

    def _start(self):
        fake = mock.MagicMock()
        fake.id = "cs_cod_1"
        fake.url = "https://checkout.stripe.com/c/pay/cs_cod_1"
        with mock.patch("stripe.checkout.Session.create", return_value=fake):
            return self.client.post(reverse("pay_online", args=[self.order.id]))

    def test_cod_order_can_be_paid_in_advance(self):
        r = self._start()
        self.assertTrue(r.url.startswith("https://checkout.stripe.com/"))
        self.order.refresh_from_db()
        payload = json.dumps({"type": "checkout.session.completed", "data": {"object": _fake_session(self.order)}})
        with mock.patch("stripe.Webhook.construct_event", return_value={}):
            self.client.post(reverse("stripe_webhook"), data=payload, content_type="application/json", HTTP_STRIPE_SIGNATURE="x")
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "paid")
        self.assertEqual(self.order.payment_method, "card")
        self.assertFalse(self.order.cod_fallback)

    def test_abandoned_advance_payment_keeps_cod_order(self):
        self._start()
        self.order.refresh_from_db()
        payload = json.dumps({"type": "checkout.session.expired", "data": {"object": _fake_session(self.order, payment_status="unpaid", status="expired")}})
        with mock.patch("stripe.Webhook.construct_event", return_value={}):
            self.client.post(reverse("stripe_webhook"), data=payload, content_type="application/json", HTTP_STRIPE_SIGNATURE="x")
        self.order.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.order.status, "pending")
        self.assertEqual(self.order.payment_method, "cod")
        self.assertEqual(self.order.payment_status, "not_applicable")
        self.assertEqual(self.product.stock, 4)  # still reserved for the COD order

    def test_cancel_page_returns_to_cod(self):
        self._start()
        fake = mock.MagicMock()
        self.order.refresh_from_db()
        fake.to_dict.return_value = _fake_session(self.order, payment_status="unpaid", status="open")
        with mock.patch("stripe.checkout.Session.retrieve", return_value=fake), mock.patch("stripe.checkout.Session.expire"):
            r = self.client.get(reverse("payment_cancel", args=[self.order.id]))
        self.assertRedirects(r, reverse("my_orders"))
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_method, "cod")
        self.assertNotEqual(self.order.status, "cancelled")

    def test_paid_or_shipped_orders_cannot_switch(self):
        Order.objects.filter(pk=self.order.pk).update(status="shipped")
        self._start()
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_method, "cod")


class DemoCatalogueTests(TestCase):
    def test_seed_gives_every_product_a_matching_image(self):
        import tempfile
        from django.core.management import call_command
        with tempfile.TemporaryDirectory() as tmp, override_settings(MEDIA_ROOT=tmp):
            Product.objects.create(name="Charcoal Face Wash", category="skincare", price=5,
                                   image_url="https://picsum.photos/id/10/400/400")
            call_command("seed_data", stdout=open(os.devnull, "w"))
            fw = Product.objects.get(name="Charcoal Face Wash")
            self.assertIn("charcoal-face-wash", fw.image_url)
            self.assertFalse(Product.objects.filter(image_url__contains="picsum").exists())
            self.assertFalse(ProductImage.objects.filter(image_url__contains="picsum").exists())


class StripeRelayTests(TestCase):
    def tearDown(self):
        import stripe
        stripe.api_base = "https://api.stripe.com"

    def test_default_api_base(self):
        from . import payments
        with self.settings(STRIPE_SECRET_KEY="sk_test_x", STRIPE_API_BASE=""):
            self.assertEqual(payments._stripe().api_base, "https://api.stripe.com")

    def test_relay_api_base(self):
        from . import payments
        relay = "https://ref.supabase.co/functions/v1/stripe-relay/secret"
        with self.settings(STRIPE_SECRET_KEY="sk_test_x", STRIPE_API_BASE=relay):
            self.assertEqual(payments._stripe().api_base, relay)
@override_settings(STRIPE_SECRET_KEY="sk_test_dummy", STRIPE_WEBHOOK_SECRET="whsec_dummy",
                   ALLOWED_HOSTS=["shop.example.com", "testserver"], SITE_URL="")
class OrderJourneyTests(TestCase):
    """The customer is kept informed at every step, and cancelling a paid
    order refunds it."""

    def setUp(self):
        self.staff = User.objects.create_user("boss", "boss@example.com", "pass12345", is_staff=True)
        self.buyer = User.objects.create_user("buyer", "buyer@example.com", "pass12345")
        self.product = make_product(name="Charcoal Face Wash", price=Decimal("20.00"), stock=10)

    def _cod_order(self):
        self.client.force_login(self.buyer)
        self.client.post(reverse("add_to_cart", args=[self.product.id]), {"quantity": 2})
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "cod"})
        return Order.objects.get()

    def _admin_set(self, order, status, **extra):
        self.client.force_login(self.staff)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("manage_order", args=[order.id]), {
                "action": "update", "status": status, "tracking_number": extra.get("tracking", ""),
                "courier_name": extra.get("courier", ""),
                "estimated_delivery": order.estimated_delivery.isoformat() if order.estimated_delivery else "",
            })
        order.refresh_from_db()
        return order

    def test_full_cod_journey_emails_every_step(self):
        order = self._cod_order()
        # Placed: estimated delivery set, "received" email with the date
        self.assertIsNotNone(order.estimated_delivery)
        self.assertGreater(order.estimated_delivery, order.created_at.date())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("received", mail.outbox[0].subject)
        self.assertIn("Estimated delivery", mail.outbox[0].alternatives[0][0])

        order = self._admin_set(order, "confirmed")
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("is confirmed", mail.outbox[1].subject)
        self.assertIn(order.estimated_delivery.strftime("%B"), mail.outbox[1].alternatives[0][0])

        order = self._admin_set(order, "shipped", tracking="TRK123", courier="DHL")
        self.assertEqual(len(mail.outbox), 3)
        self.assertIn("on its way", mail.outbox[2].subject)
        self.assertIn("TRK123", mail.outbox[2].alternatives[0][0])
        self.assertIn("https://shop.example.com/my-orders/", mail.outbox[2].alternatives[0][0])
        self.assertEqual(set(order.items.values_list("fulfillment_status", flat=True)), {"handed_to_courier"})

        order = self._admin_set(order, "delivered")
        self.assertEqual(len(mail.outbox), 4)
        self.assertIn("delivered", mail.outbox[3].subject)
        self.assertIn("Write a review", mail.outbox[3].alternatives[0][0])
        self.assertTrue(all(m.to == ["jane@example.com"] for m in mail.outbox))  # the email given at checkout

    def test_customer_cancels_cod_order(self):
        order = self._cod_order()
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("cancel_order", args=[order.id]))
        order.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(self.product.stock, 10)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("cancelled", mail.outbox[0].subject)
        self.assertIn("not been charged", mail.outbox[0].alternatives[0][0])

    def test_customer_cancels_paid_order_gets_refund_and_email(self):
        self.client.force_login(self.buyer)
        self.client.post(reverse("add_to_cart", args=[self.product.id]), {"quantity": 1})
        fake = mock.MagicMock(id="cs_test_9", url="https://checkout.stripe.com/x")
        with mock.patch("stripe.checkout.Session.create", return_value=fake):
            self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "card"})
        order = Order.objects.get()
        payload = json.dumps({"type": "checkout.session.completed", "data": {"object": _fake_session(order)}})
        with mock.patch("stripe.Webhook.construct_event", return_value={}), self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("stripe_webhook"), data=payload, content_type="application/json", HTTP_STRIPE_SIGNATURE="t=1,v1=x")
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "paid")
        self.assertIsNotNone(order.estimated_delivery)
        self.assertEqual(len(mail.outbox), 1)  # one "confirmed + paid" email, not two
        mail.outbox.clear()

        with mock.patch("stripe.Refund.create") as refund, self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("cancel_order", args=[order.id]))
        refund.assert_called_once()
        self.assertEqual(refund.call_args.kwargs["payment_intent"], "pi_test_123")
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(order.payment_status, "refunded")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("full refund", mail.outbox[0].alternatives[0][0])
        self.assertIn("$20.00", mail.outbox[0].alternatives[0][0])
        self.assertContains(self.client.get(reverse("my_orders")), "refunded to your card")

    def test_failed_refund_keeps_order_and_warns(self):
        order = self._cod_order()
        Order.objects.filter(pk=order.pk).update(payment_status="paid", payment_method="card", stripe_payment_intent="pi_x")
        with mock.patch("stripe.Refund.create", side_effect=Exception("down")):
            r = self.client.post(reverse("cancel_order", args=[order.id]), follow=True)
        order.refresh_from_db()
        self.assertEqual(order.status, "pending")
        self.assertContains(r, "couldn&#x27;t process the refund")

    def test_shipped_order_cannot_be_cancelled_by_customer(self):
        order = self._cod_order()
        self._admin_set(order, "shipped")
        self.client.force_login(self.buyer)
        self.client.post(reverse("cancel_order", args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")

    def test_seller_fulfilment_moves_order_forward(self):
        seller_user = User.objects.create_user("sel", "sel@example.com", "pass12345")
        seller = SellerAccount.objects.create(user=seller_user, status="approved", business_name="Sel Co")
        Product.objects.filter(pk=self.product.pk).update(seller_account=seller)
        order = self._cod_order()
        item = order.items.get()
        mail.outbox.clear()
        self.client.force_login(seller_user)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("update_fulfillment_status", args=[item.id]), {"fulfillment_status": "handed_to_courier"})
        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")
        self.assertIn("on its way", mail.outbox[-1].subject)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("update_fulfillment_status", args=[item.id]), {"fulfillment_status": "delivered"})
        order.refresh_from_db()
        self.assertEqual(order.status, "delivered")
        self.assertIn("delivered", mail.outbox[-1].subject)

    def test_seller_cannot_ship_cancelled_order(self):
        seller_user = User.objects.create_user("sel", "sel@example.com", "pass12345")
        seller = SellerAccount.objects.create(user=seller_user, status="approved", business_name="Sel Co")
        Product.objects.filter(pk=self.product.pk).update(seller_account=seller)
        order = self._cod_order()
        self.client.post(reverse("cancel_order", args=[order.id]))
        item = order.items.get()
        self.client.force_login(seller_user)
        self.client.post(reverse("update_fulfillment_status", args=[item.id]), {"fulfillment_status": "handed_to_courier"})
        item.refresh_from_db()
        self.assertEqual(item.fulfillment_status, "pending")

    def test_delivery_days_setting_controls_date(self):
        from .order_emails import add_business_days
        from datetime import date
        self.assertEqual(add_business_days(date(2026, 10, 9), 1), date(2026, 10, 12))  # Fri -> Mon
        SiteSettings.objects.update_or_create(pk=1, defaults={"delivery_days": 10})
        cache.clear()
        order = self._cod_order()
        from django.utils import timezone
        self.assertEqual(order.estimated_delivery, add_business_days(timezone.localdate(), 10))


@override_settings(STRIPE_SECRET_KEY="sk_test_dummy", STRIPE_WEBHOOK_SECRET="whsec_dummy")
class AuditFixTests(TestCase):
    """Regression tests for the problems found in the full-site review."""

    def setUp(self):
        self.buyer = User.objects.create_user("buyer", "buyer@example.com", "pass12345")
        self.staff = User.objects.create_user("boss", "boss@example.com", "pass12345", is_staff=True)
        self.product = make_product(name="Face Wash", price=Decimal("20.00"), stock=10)

    # --- helpers -----------------------------------------------------------
    def _seller(self, name="Sel Co", username="sel", **kw):
        u = User.objects.create_user(username, f"{username}@example.com", "pass12345")
        return SellerAccount.objects.create(user=u, status="approved", business_name=name, **kw)

    def _delivered_order(self, user=None, product=None, qty=1, **kw):
        product = product or self.product
        order = Order.objects.create(user=user or self.buyer, email="buyer@example.com", full_name="B", address="1 St",
                                     city="Austin", country="US", phone="1", status="delivered", **kw)
        OrderItem.objects.create(order=order, product=product, product_name=product.name, price=product.price, quantity=qty)
        return order

    def _webhook(self, session):
        payload = json.dumps({"type": "checkout.session.completed", "data": {"object": session}})
        with mock.patch("stripe.Webhook.construct_event", return_value={}):
            return self.client.post(reverse("stripe_webhook"), data=payload, content_type="application/json", HTTP_STRIPE_SIGNATURE="t=1,v1=x")

    def _card_checkout(self, qty=1, **post):
        self.client.force_login(self.buyer)
        self.client.post(reverse("add_to_cart", args=[self.product.id]), {"quantity": qty})
        fake = mock.MagicMock(id="cs_test_1", url="https://checkout.stripe.com/x")
        with mock.patch("stripe.checkout.Session.create", return_value=fake) as create, \
             mock.patch("stripe.Coupon.create", return_value=mock.MagicMock(id="co_1")) as coupon:
            self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "card", **post})
        return Order.objects.latest("id"), create, coupon

    # --- sellers -----------------------------------------------------------
    def test_seller_product_pages_open_and_keep_zero_stock(self):
        seller = self._seller()
        p = make_product(name="Sold Out", seller_account=seller, seller_name="Sel Co", stock=0)
        self.client.force_login(seller.user)
        self.assertEqual(self.client.get(reverse("seller_add_product")).status_code, 200)
        page = self.client.get(reverse("seller_edit_product", args=[p.id]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'name="stock" min="0" step="1" value="0"')

    def test_seller_can_keep_uploaded_image_path(self):
        seller = self._seller()
        p = make_product(name="Local", seller_account=seller, seller_name="Sel Co", image_url="/media/products/a.png")
        self.client.force_login(seller.user)
        self.client.post(reverse("seller_edit_product", args=[p.id]), {
            "name": "Local", "category": "skincare", "price": "11.00", "stock": "3", "image_url": "/media/products/a.png",
        })
        p.refresh_from_db()
        self.assertEqual(p.price, Decimal("11.00"))

    def test_store_name_with_slash_works(self):
        seller = self._seller(name="A/B Goods")
        p = make_product(name="Slashy", seller_account=seller, seller_name="A/B Goods")
        self.assertEqual(self.client.get(reverse("product_detail", args=[p.id])).status_code, 200)
        self.assertContains(self.client.get(reverse("store_page", args=["A/B Goods"])), "Slashy")

    def test_suspended_seller_products_hidden_and_unbuyable(self):
        seller = self._seller()
        p = make_product(name="Gone", seller_account=seller, seller_name="Sel Co")
        SellerAccount.objects.filter(pk=seller.pk).update(status="suspended")
        self.assertEqual(self.client.get(reverse("product_detail", args=[p.id])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("all_products")), "Gone")
        self.client.post(reverse("add_to_cart", args=[p.id]))
        self.assertEqual(self.client.session.get("cart", {}), {})

    def test_team_admin_can_manage_team(self):
        org = self._seller(name="Org", username="orgo", account_type="organization")
        admin = User.objects.create_user("adm", "adm@example.com", "pass12345")
        OrganizationMember.objects.create(organization=org, user=admin, role="admin")
        User.objects.create_user("newbie", "newbie@example.com", "pass12345")
        self.client.force_login(admin)
        self.client.post(reverse("add_team_member"), {"username_or_email": "newbie", "role": "staff"})
        self.assertTrue(OrganizationMember.objects.filter(organization=org, user__username="newbie").exists())

    def test_seller_answers_question_and_asker_is_notified(self):
        seller = self._seller()
        p = make_product(name="Q Product", seller_account=seller, seller_name="Sel Co")
        self.client.force_login(self.buyer)
        self.client.post(reverse("ask_question", args=[p.id]), {"question": "Is it vegan?"})
        self.assertTrue(Notification.objects.filter(user=seller.user, message__icontains="question").exists())
        q = Question.objects.get()
        self.client.force_login(seller.user)
        self.assertContains(self.client.get(reverse("seller_reviews")), "Is it vegan?")
        self.client.post(reverse("seller_answer_question", args=[q.id]), {"answer": "Yes, 100%."})
        q.refresh_from_db()
        self.assertEqual(q.answer, "Yes, 100%.")
        self.assertTrue(Notification.objects.filter(user=self.buyer, message__icontains="answered").exists())

    # --- payments ----------------------------------------------------------
    def test_cancelled_card_order_cannot_be_charged(self):
        order, _, _ = self._card_checkout()
        with mock.patch("stripe.checkout.Session.expire") as expire:
            self.client.post(reverse("cancel_order", args=[order.id]))
        expire.assert_called_once_with("cs_test_1")
        order.refresh_from_db()
        self.assertEqual((order.status, order.payment_status), ("cancelled", "failed"))
        # Even if the old Stripe tab is paid anyway, the money goes straight back.
        order.payment_status = "pending"
        order.save(update_fields=["payment_status"])
        with mock.patch("stripe.Refund.create") as refund:
            self._webhook(_fake_session(order))
        refund.assert_called_once()
        order.refresh_from_db()
        self.assertEqual((order.status, order.payment_status), ("cancelled", "refunded"))

    def test_zero_decimal_currency_amounts(self):
        from .payments import to_cents
        self.assertEqual(to_cents(Decimal("1000"), "jpy"), 1000)
        self.assertEqual(to_cents(Decimal("10.50"), "usd"), 1050)

    def test_coupon_kept_when_payment_cancelled(self):
        Coupon.objects.create(code="SAVE10", percent_off=10)
        self.client.force_login(self.buyer)
        self.client.post(reverse("add_to_cart", args=[self.product.id]))
        self.client.post(reverse("apply_coupon"), {"coupon_code": "SAVE10"})
        fake = mock.MagicMock(id="cs_test_2", url="https://checkout.stripe.com/x")
        with mock.patch("stripe.checkout.Session.create", return_value=fake), \
             mock.patch("stripe.Coupon.create", return_value=mock.MagicMock(id="co")):
            self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "card"})
        order = Order.objects.get()
        self.assertEqual(self.client.session["coupon_code"], "")
        with mock.patch("stripe.checkout.Session.retrieve", side_effect=Exception("offline")), \
             mock.patch("stripe.checkout.Session.expire"):
            self.client.get(reverse("payment_cancel", args=[order.id]))
        self.assertEqual(self.client.session["coupon_code"], "SAVE10")

    def test_free_shipping_uses_price_after_coupon(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={"shipping_flat_fee": Decimal("5"), "free_shipping_threshold": Decimal("50")})
        cache.clear()
        from .views import _price_cart
        coupon = Coupon(code="HALF", percent_off=50)
        self.assertEqual(_price_cart(Decimal("60"), coupon)["shipping"], Decimal("5"))
        self.assertEqual(_price_cart(Decimal("120"), coupon)["shipping"], Decimal("0"))

    # --- store credit & points ---------------------------------------------
    def test_store_credit_partly_pays_card_order_and_returns_on_cancel(self):
        Profile.objects.create(user=self.buyer, referral_code="B1", store_credit=Decimal("5.00"))
        order, create, coupon = self._card_checkout(use_credit="1")
        self.assertEqual(order.credit_used, Decimal("5.00"))
        self.assertEqual(order.total, Decimal("15.00"))
        self.assertEqual(coupon.call_args.kwargs["amount_off"], 500)
        self.assertEqual(Profile.objects.get(user=self.buyer).store_credit, 0)
        with mock.patch("stripe.checkout.Session.expire"):
            self.client.post(reverse("cancel_order", args=[order.id]))
        self.assertEqual(Profile.objects.get(user=self.buyer).store_credit, Decimal("5.00"))

    def test_store_credit_can_cover_whole_order(self):
        Profile.objects.create(user=self.buyer, referral_code="B1", store_credit=Decimal("50.00"))
        order, create, _ = self._card_checkout(use_credit="1")
        create.assert_not_called()
        self.assertEqual((order.payment_status, order.status), ("paid", "confirmed"))
        self.assertEqual(Profile.objects.get(user=self.buyer).store_credit, Decimal("30.00"))
        self.client.post(reverse("cancel_order", args=[order.id]))  # nothing on a card; credit comes back
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(Profile.objects.get(user=self.buyer).store_credit, Decimal("50.00"))

    def test_credit_not_used_unless_ticked(self):
        Profile.objects.create(user=self.buyer, referral_code="B1", store_credit=Decimal("5.00"))
        order, _, _ = self._card_checkout()
        self.assertEqual(order.credit_used, 0)

    def test_redeem_points(self):
        Profile.objects.create(user=self.buyer, referral_code="B1", loyalty_points=250)
        self.client.force_login(self.buyer)
        self.assertContains(self.client.get(reverse("profile")), "Turn 200 points into $2.00")
        self.client.post(reverse("redeem_points"))
        prof = Profile.objects.get(user=self.buyer)
        self.assertEqual((prof.loyalty_points, prof.store_credit), (50, Decimal("2.00")))

    # --- returns -----------------------------------------------------------
    def _return(self, order, method="original_payment"):
        return ReturnRequest.objects.create(order_item=order.items.get(), user=self.buyer, reason="Broken", refund_method=method)

    def test_return_refund_respects_coupon_and_only_once(self):
        order = self._delivered_order(qty=1, discount_amount=Decimal("10.00"), payment_status="paid",
                                      payment_method="card", stripe_payment_intent="pi_1")
        rr = self._return(order)
        self.client.force_login(self.staff)
        with mock.patch("stripe.Refund.create") as refund:
            self.client.post(reverse("manage_returns"), {"id": rr.id, "action": "refund"})
            self.client.post(reverse("manage_returns"), {"id": rr.id, "action": "refund"})
        refund.assert_called_once()
        self.assertEqual(refund.call_args.kwargs["amount"], 1000)  # paid $10 after coupon, not $20
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 11)  # restocked once

    def test_store_credit_refund_and_cod_refund(self):
        order = self._delivered_order(payment_method="cod")
        rr = self._return(order, method="store_credit")
        self.client.force_login(self.staff)
        self.client.post(reverse("manage_returns"), {"id": rr.id, "action": "refund"})
        self.assertEqual(Profile.objects.get(user=self.buyer).store_credit, Decimal("20.00"))
        order2 = self._delivered_order(payment_method="cod")
        rr2 = self._return(order2)
        r = self.client.post(reverse("manage_returns"), {"id": rr2.id, "action": "refund"}, follow=True)
        self.assertContains(r, "pay the customer $20.00 yourself")
        rr2.refresh_from_db()
        self.assertEqual(rr2.status, "refunded")

    def test_return_window_enforced(self):
        from datetime import timedelta
        from django.utils import timezone
        order = self._delivered_order()
        Order.objects.filter(pk=order.pk).update(delivered_at=timezone.now() - timedelta(days=20))
        self.client.force_login(self.buyer)
        item = order.items.get()
        self.client.post(reverse("request_return", args=[item.id]), {"reason": "late"})
        self.assertFalse(ReturnRequest.objects.exists())
        self.assertContains(self.client.get(reverse("my_orders")), "Return window closed")

    def test_delivered_at_recorded(self):
        order = self._delivered_order()
        order.status = "confirmed"; order.save()
        order.status = "delivered"; order.save(update_fields=["status"])
        order.refresh_from_db()
        self.assertIsNotNone(order.delivered_at)

    # --- accounts, reviews, misc -------------------------------------------
    def test_seller_cannot_review_own_product(self):
        seller = self._seller()
        p = make_product(name="Mine", seller_account=seller, seller_name="Sel Co")
        self._delivered_order(user=seller.user, product=p)
        self.client.force_login(seller.user)
        self.client.post(reverse("product_detail", args=[p.id]), {"rating": 5, "comment": "Best ever"})
        self.assertFalse(Review.objects.exists())

    def test_referral_rewarded_once_after_first_delivery(self):
        referrer = Profile.objects.create(user=self.staff, referral_code="REFCODE1")
        self.client.post(reverse("signup") + "?ref=REFCODE1", {
            "username": "friend", "email": "friend@example.com", "password": "Strong-pass-123",
            "confirm_password": "Strong-pass-123", "ref": "REFCODE1",
        })
        friend = User.objects.get(username="friend")
        self.assertFalse(Coupon.objects.filter(code__startswith="REF-").exists())  # nothing for just signing up
        welcome = Coupon.objects.get(code__startswith="WELCOME-")
        self.assertEqual(welcome.usage_limit, 1)
        order = self._delivered_order(user=friend)
        order.status = "confirmed"; order.save()
        order.status = "delivered"; order.save()
        order2 = self._delivered_order(user=friend)
        order2.status = "confirmed"; order2.save(); order2.status = "delivered"; order2.save()
        self.assertEqual(Coupon.objects.filter(code__startswith="REF-").count(), 1)
        self.assertTrue(Notification.objects.filter(user=referrer.user, message__icontains="first purchase").exists())

    def test_signup_returns_to_next_page(self):
        r = self.client.post(reverse("signup"), {
            "username": "newone", "email": "newone@example.com", "password": "Strong-pass-123",
            "confirm_password": "Strong-pass-123", "next": "/checkout/",
        })
        self.assertEqual(r.url, "/checkout/")
        r = self.client.post(reverse("logout"))
        r = self.client.post(reverse("signup"), {
            "username": "evil", "email": "evil@example.com", "password": "Strong-pass-123",
            "confirm_password": "Strong-pass-123", "next": "https://evil.example/",
        })
        self.assertEqual(r.url, "/")

    def test_notifications_not_marked_read_by_prefetch(self):
        Notification.objects.create(user=self.buyer, message="hi", link="/")
        self.client.force_login(self.buyer)
        self.client.get(reverse("notifications_list"), HTTP_SEC_PURPOSE="prefetch;prerender")
        self.assertTrue(Notification.objects.filter(user=self.buyer, is_read=False).exists())
        self.client.get(reverse("notifications_list"))
        self.assertFalse(Notification.objects.filter(user=self.buyer, is_read=False).exists())

    def test_add_to_cart_requires_post(self):
        self.assertEqual(self.client.get(reverse("add_to_cart", args=[self.product.id])).status_code, 405)

    def test_cart_badge_ignores_removed_products(self):
        gone = make_product(name="Temp")
        self.client.post(reverse("add_to_cart", args=[self.product.id]))
        self.client.post(reverse("add_to_cart", args=[gone.id]))
        gone.delete()
        self.assertEqual(self.client.get(reverse("home")).context["cart_count"], 1)

    def test_cancelled_order_success_page(self):
        self.client.force_login(self.buyer)
        order = self._delivered_order()
        Order.objects.filter(pk=order.pk).update(status="cancelled")
        page = self.client.get(reverse("order_success", args=[order.id]))
        self.assertContains(page, "was cancelled")
        self.assertNotContains(page, "your order is confirmed")

    def test_guest_chat_kept_after_login(self):
        self.client.get(reverse("chat_messages"))
        self.client.post(reverse("chat_send"), {"message": "Where is my parcel?"})
        self.client.post(reverse("login"), {"username": "buyer", "password": "pass12345"})
        msgs = self.client.get(reverse("chat_messages")).json()["messages"]
        self.assertTrue(any("parcel" in m["message"] for m in msgs))
        self.assertEqual(ChatThread.objects.get(user=self.buyer).messages.filter(sender="user").count(), 1)


@override_settings(STRIPE_SECRET_KEY="sk_test_dummy", STRIPE_WEBHOOK_SECRET="whsec_dummy")
class AccountSettingsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("acct", "acct@example.com", "Old-pass-123")
        self.client.force_login(self.user)

    # --- password & devices -------------------------------------------------
    def test_change_password(self):
        other = Client()
        other.force_login(self.user)
        r = self.client.post(reverse("account_security"), {"old_password": "wrong", "new_password1": "New-pass-456!", "new_password2": "New-pass-456!"})
        self.assertContains(r, "incorrectly")
        self.client.post(reverse("account_security"), {"old_password": "Old-pass-123", "new_password1": "New-pass-456!", "new_password2": "New-pass-456!"})
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("New-pass-456!"))
        self.assertEqual(self.client.get(reverse("profile")).status_code, 200)  # still signed in here
        self.assertEqual(other.get(reverse("profile")).status_code, 302)  # signed out elsewhere
        self.assertTrue(any("password was changed" in m.subject.lower() for m in mail.outbox))

    def test_sign_out_other_devices(self):
        self.client.logout()
        self.client.post(reverse("login"), {"username": "acct", "password": "Old-pass-123", "remember": "1"})
        other = Client()
        other.post(reverse("login"), {"username": "acct", "password": "Old-pass-123", "remember": "1"})
        self.assertContains(self.client.get(reverse("account_security")), "1 other device")
        self.client.post(reverse("account_security"), {"action": "signout_others"})
        self.assertEqual(other.get(reverse("profile")).status_code, 302)
        self.assertEqual(self.client.get(reverse("profile")).status_code, 200)

    def test_remember_me(self):
        self.client.logout()
        self.client.post(reverse("login"), {"username": "acct", "password": "Old-pass-123"})
        self.assertTrue(self.client.session.get_expire_at_browser_close())
        self.client.logout()
        self.client.post(reverse("login"), {"username": "acct", "password": "Old-pass-123", "remember": "1"})
        self.assertFalse(self.client.session.get_expire_at_browser_close())

    def test_email_change_alerts_old_address(self):
        self.client.post(reverse("profile"), {"first_name": "A", "email": "new@example.com", "phone": ""})
        self.assertTrue(any(m.to == ["acct@example.com"] and "email address was changed" in m.subject.lower() for m in mail.outbox))

    # --- saved cards ----------------------------------------------------------
    def test_cards_listed_added_and_removed(self):
        Profile.objects.create(user=self.user, referral_code="AC1", stripe_customer_id="cus_1")
        listing = {"data": [{"id": "pm_1", "card": {"brand": "visa", "last4": "4242", "exp_month": 4, "exp_year": 2030}}]}
        with mock.patch("stripe.Customer.list_payment_methods", return_value=listing):
            page = self.client.get(reverse("payment_methods"))
        self.assertContains(page, "Visa ending in 4242")
        self.assertContains(page, "Expires 04/2030")
        with mock.patch("stripe.checkout.Session.create", return_value=mock.MagicMock(url="https://checkout.stripe.com/setup")) as create:
            r = self.client.post(reverse("add_card"))
        self.assertEqual(r.url, "https://checkout.stripe.com/setup")
        self.assertEqual(create.call_args.kwargs["mode"], "setup")
        self.assertEqual(create.call_args.kwargs["customer"], "cus_1")
        # Someone else's card can't be removed
        with mock.patch("stripe.PaymentMethod.retrieve", return_value={"customer": "cus_OTHER"}), \
             mock.patch("stripe.PaymentMethod.detach") as detach:
            self.client.post(reverse("remove_card"), {"card": "pm_x"})
        detach.assert_not_called()
        with mock.patch("stripe.PaymentMethod.retrieve", return_value={"customer": "cus_1"}), \
             mock.patch("stripe.PaymentMethod.detach") as detach:
            self.client.post(reverse("remove_card"), {"card": "pm_1"})
        detach.assert_called_once_with("pm_1")

    def test_card_added_on_stripe_is_offered_again(self):
        Profile.objects.create(user=self.user, referral_code="AC1", stripe_customer_id="cus_1")
        session = {"customer": "cus_1", "status": "complete", "setup_intent": {"payment_method": "pm_9"}}
        with mock.patch("stripe.checkout.Session.retrieve", return_value=session), \
             mock.patch("stripe.Customer.list_payment_methods", return_value={"data": []}), \
             mock.patch("stripe.PaymentMethod.modify") as modify:
            r = self.client.get(reverse("payment_methods") + "?added=cs_setup_1", follow=True)
        modify.assert_called_once_with("pm_9", allow_redisplay="always")
        self.assertContains(r, "Card saved")

    def test_checkout_uses_saved_customer(self):
        product = make_product(price=Decimal("10.00"))
        self.client.post(reverse("add_to_cart", args=[product.id]))
        fake = mock.MagicMock(id="cs_1", url="https://checkout.stripe.com/pay")
        with mock.patch("stripe.checkout.Session.create", return_value=fake) as create:
            self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "card"})
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["customer"], "cus_test")
        self.assertEqual(kwargs["saved_payment_method_options"], {"payment_method_save": "enabled"})
        self.assertNotIn("customer_email", kwargs)
        self.assertEqual(Profile.objects.get(user=self.user).stripe_customer_id, "cus_test")

    @override_settings(STRIPE_SECRET_KEY="")
    def test_cards_page_without_stripe(self):
        self.assertContains(self.client.get(reverse("payment_methods")), "aren't switched on")

    # --- addresses ------------------------------------------------------------
    def test_edit_and_default_address_prefills_checkout(self):
        self.client.post(reverse("add_address"), {"label": "Home", "full_name": "A One", "address": "1 Road", "city": "Austin", "country": "US", "phone": "1"})
        self.client.post(reverse("add_address"), {"label": "Work", "full_name": "A Two", "address": "2 Office", "city": "Boston", "country": "US", "phone": "2"})
        home, work = Address.objects.order_by("id")
        self.assertTrue(home.is_default)
        self.client.post(reverse("edit_address", args=[work.id]), {"label": "Office", "full_name": "A Two", "address": "22 Office", "city": "Boston", "country": "US", "phone": "2", "is_default": "1"})
        work.refresh_from_db(); home.refresh_from_db()
        self.assertEqual((work.label, work.address, work.is_default, home.is_default), ("Office", "22 Office", True, False))
        product = make_product()
        self.client.post(reverse("add_to_cart", args=[product.id]))
        self.assertContains(self.client.get(reverse("checkout")), 'value="22 Office"')
        other = User.objects.create_user("other", "o@example.com", "pass12345")
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("edit_address", args=[work.id])).status_code, 404)

    # --- privacy --------------------------------------------------------------
    def test_email_preferences(self):
        self.client.post(reverse("account_privacy"), {"action": "emails", "marketing": "1"})
        self.assertTrue(NewsletterSubscriber.objects.filter(email="acct@example.com").exists())
        self.client.post(reverse("account_privacy"), {"action": "emails"})
        self.assertFalse(NewsletterSubscriber.objects.exists())

    def test_download_data(self):
        order = Order.objects.create(user=self.user, full_name="A", address="x", city="y", phone="1", status="delivered")
        OrderItem.objects.create(order=order, product_name="Face Wash", price=Decimal("5"), quantity=1)
        r = self.client.get(reverse("download_data"))
        data = json.loads(r.content)
        self.assertEqual(data["account"]["email"], "acct@example.com")
        self.assertEqual(data["orders"][0]["items"][0]["product"], "Face Wash")

    def test_delete_account(self):
        r = self.client.post(reverse("delete_account"), {"password": "nope"}, follow=True)
        self.assertContains(r, "isn&#x27;t right")
        open_order = Order.objects.create(user=self.user, full_name="A", address="x", city="y", phone="1", status="shipped")
        self.client.post(reverse("delete_account"), {"password": "Old-pass-123"})
        self.assertTrue(User.objects.get(pk=self.user.pk).is_active)
        Order.objects.filter(pk=open_order.pk).update(status="delivered")
        Address.objects.create(user=self.user, full_name="A", phone="1", address="x", city="y", country="US")
        Profile.objects.update_or_create(user=self.user, defaults={"referral_code": "AC1", "stripe_customer_id": "cus_1"})
        with mock.patch("stripe.Customer.delete") as delete:
            self.client.post(reverse("delete_account"), {"password": "Old-pass-123"})
        delete.assert_called_once_with("cus_1")
        u = User.objects.get(pk=self.user.pk)
        self.assertFalse(u.is_active)
        self.assertEqual(u.email, "")
        self.assertFalse(Address.objects.filter(user=u).exists())
        self.assertTrue(Order.objects.filter(pk=open_order.pk).exists())  # sales records kept
        self.assertFalse(Client().login(username="acct", password="Old-pass-123"))
        self.assertEqual(self.client.get(reverse("profile")).status_code, 302)  # signed out

    def test_sellers_cannot_self_delete(self):
        SellerAccount.objects.create(user=self.user, status="approved", business_name="S")
        self.client.post(reverse("delete_account"), {"password": "Old-pass-123"})
        self.assertTrue(User.objects.get(pk=self.user.pk).is_active)

    # --- seller store settings ------------------------------------------------
    def test_store_settings_rename_updates_products(self):
        seller = SellerAccount.objects.create(user=self.user, status="approved", business_name="Old Name")
        p = make_product(seller_account=seller, seller_name="Old Name")
        self.client.post(reverse("seller_store_settings"), {"store_name": "New Name", "store_description": "Hello", "bank_details": "IBAN 123"})
        seller.refresh_from_db(); p.refresh_from_db()
        self.assertEqual((seller.business_name, seller.bank_details, p.seller_name), ("New Name", "IBAN 123", "New Name"))
        SellerAccount.objects.create(user=User.objects.create_user("z", "z@example.com", "x"), business_name="Taken")
        self.client.post(reverse("seller_store_settings"), {"store_name": "taken"})
        seller.refresh_from_db()
        self.assertEqual(seller.business_name, "New Name")

    def test_team_staff_cannot_change_store_settings(self):
        owner = User.objects.create_user("own", "own@example.com", "x")
        org = SellerAccount.objects.create(user=owner, status="approved", business_name="Org", account_type="organization")
        OrganizationMember.objects.create(organization=org, user=self.user, role="staff")
        self.client.post(reverse("seller_store_settings"), {"store_name": "Hijack"})
        org.refresh_from_db()
        self.assertEqual(org.business_name, "Org")


class ShippingZoneTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("ship", "ship@example.com", "pass12345")
        self.product = make_product(price=Decimal("20.00"), stock=10)
        self.client.force_login(self.user)

    def _checkout(self, country):
        self.client.post(reverse("add_to_cart", args=[self.product.id]))
        return self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "country": country, "payment_method": "cod"}, follow=True)

    def test_flat_fee_without_zones(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={"shipping_flat_fee": Decimal("7")})
        cache.clear()
        self._checkout("DE")
        self.assertEqual(Order.objects.get().shipping_amount, Decimal("7"))

    def test_fee_by_country_and_unsupported_country(self):
        from .models import ShippingZone
        ShippingZone.objects.create(name="US", countries="US", fee=Decimal("5"), free_over=Decimal("50"), delivery_days=3)
        rest = ShippingZone.objects.create(name="World", countries="*", fee=Decimal("20"), delivery_days=12)
        self._checkout("US")
        us = Order.objects.get()
        self.assertEqual(us.shipping_amount, Decimal("5"))
        from .order_emails import add_business_days
        from django.utils import timezone
        self.assertEqual(us.estimated_delivery, add_business_days(timezone.localdate(), 3))
        self._checkout("DE")
        self.assertEqual(Order.objects.latest("id").shipping_amount, Decimal("20"))
        rest.delete()
        r = self._checkout("DE")
        self.assertContains(r, "don&#x27;t deliver to Germany")
        self.assertEqual(Order.objects.count(), 2)

    def test_free_over_threshold(self):
        from .models import ShippingZone
        ShippingZone.objects.create(name="US", countries="US", fee=Decimal("5"), free_over=Decimal("50"))
        self.client.post(reverse("add_to_cart", args=[self.product.id]), {"quantity": 3})
        self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "country": "US", "payment_method": "cod"})
        self.assertEqual(Order.objects.get().shipping_amount, 0)

    def test_checkout_page_has_shipping_table(self):
        from .models import ShippingZone
        ShippingZone.objects.create(name="US", countries="US, CA", fee=Decimal("5"))
        self.client.post(reverse("add_to_cart", args=[self.product.id]))
        page = self.client.get(reverse("checkout"))
        self.assertContains(page, '"CA": {"fee": "5.00"')
        self.assertContains(page, "Choose a country")

    def test_admin_manages_zones(self):
        from .models import ShippingZone
        staff = User.objects.create_user("boss", "boss@example.com", "pass12345", is_staff=True)
        self.client.force_login(staff)
        r = self.client.post(reverse("manage_shipping"), {"action": "save", "name": "Bad", "countries": "US, XX", "fee": "5"})
        self.assertContains(r, "Unknown country code(s): XX")
        self.client.post(reverse("manage_shipping"), {"action": "save", "name": "North America", "countries": "us,ca, us", "fee": "6", "active": "on"})
        self.assertEqual(ShippingZone.objects.get().countries, "US, CA")
        ShippingZone.objects.all().delete()
        self.client.post(reverse("manage_shipping"), {"action": "starter"})
        self.assertEqual(ShippingZone.objects.count(), 3)
        self.assertEqual(self.client.get(reverse("manage_shipping")).status_code, 200)


class VariantTests(TestCase):
    def setUp(self):
        self.seller_user = User.objects.create_user("vs", "vs@example.com", "pass12345")
        self.seller = SellerAccount.objects.create(user=self.seller_user, status="approved", business_name="Hood Co")
        self.buyer = User.objects.create_user("vb", "vb@example.com", "pass12345")

    def _form(self, **extra):
        data = {"name": "Hoodie", "category": "hoodies", "price": "40.00", "stock": "0", "image_url": "https://example.com/h.jpg",
                "variant_id": ["", ""], "variant_size": ["M", "L"], "variant_color": ["Black", "Black"],
                "variant_price": ["", "45.00"], "variant_stock": ["3", "0"], "variant_sku": ["H-M", "H-L"]}
        data.update(extra)
        return data

    def _product(self):
        self.client.force_login(self.seller_user)
        self.client.post(reverse("seller_add_product"), self._form())
        p = Product.objects.get(name="Hoodie")
        Product.objects.filter(pk=p.pk).update(approval_status="approved")
        p.refresh_from_db()
        return p

    def test_seller_creates_variants(self):
        p = self._product()
        self.assertTrue(p.has_variants)
        self.assertEqual(p.stock, 3)
        self.assertEqual([v.label for v in p.variants.all()], ["M / Black", "L / Black"])
        self.assertEqual(self.client.get(reverse("seller_edit_product", args=[p.id])).status_code, 200)

    def test_duplicate_options_rejected(self):
        self.client.force_login(self.seller_user)
        r = self.client.post(reverse("seller_add_product"), self._form(variant_size=["M", "m"]))
        self.assertContains(r, "listed twice")
        self.assertFalse(Product.objects.exists())

    def test_buy_variant_and_cancel_restocks(self):
        p = self._product()
        m, large = p.variants.all()
        self.client.force_login(self.buyer)
        page = self.client.get(reverse("product_detail", args=[p.id]))
        self.assertContains(page, 'data-value="M"')
        self.client.post(reverse("add_to_cart", args=[p.id]))  # no size chosen
        self.assertEqual(self.client.session.get("cart", {}), {})
        self.client.post(reverse("add_to_cart", args=[p.id]), {"variant": large.id})  # sold out
        self.assertEqual(self.client.session.get("cart", {}), {})
        self.client.post(reverse("add_to_cart", args=[p.id]), {"variant": m.id, "quantity": 5})
        self.assertEqual(self.client.session["cart"], {f"{p.id}-{m.id}": 3})  # capped at stock
        self.assertContains(self.client.get(reverse("cart")), "M / Black")
        self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "cod"})
        item = OrderItem.objects.get()
        self.assertEqual((item.product_name, item.variant, item.quantity, item.price), ("Hoodie (M / Black)", m, 3, Decimal("40.00")))
        m.refresh_from_db(); p.refresh_from_db()
        self.assertEqual((m.stock, p.stock), (0, 0))
        self.client.post(reverse("cancel_order", args=[item.order_id]))
        m.refresh_from_db(); p.refresh_from_db()
        self.assertEqual((m.stock, p.stock), (3, 3))

    def test_variant_price_used(self):
        p = self._product()
        large = p.variants.get(size="L")
        ProductVariant.objects.filter(pk=large.pk).update(stock=2)
        p.sync_variants()
        self.client.force_login(self.buyer)
        self.client.post(reverse("add_to_cart", args=[p.id]), {"variant": large.id})
        self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "cod"})
        self.assertEqual(OrderItem.objects.get().price, Decimal("45.00"))

    def test_admin_removes_variant(self):
        p = self._product()
        staff = User.objects.create_user("boss", "boss@example.com", "pass12345", is_staff=True)
        self.client.force_login(staff)
        m = p.variants.get(size="M")
        self.client.post(reverse("manage_product_edit", args=[p.id]), {
            "name": "Hoodie", "category": "hoodies", "price": "40", "stock": "0", "image_url": p.image_url,
            "approval_status": "approved", "variant_id": [m.id], "variant_size": ["M"], "variant_color": ["Black"],
            "variant_price": [""], "variant_stock": ["7"], "variant_sku": [""],
        })
        p.refresh_from_db()
        self.assertEqual(p.variants.count(), 1)
        self.assertEqual(p.stock, 7)

    def test_buy_again_keeps_size(self):
        p = self._product()
        m = p.variants.get(size="M")
        self.client.force_login(self.buyer)
        self.client.post(reverse("add_to_cart", args=[p.id]), {"variant": m.id})
        self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "cod"})
        self.client.post(reverse("buy_again", args=[Order.objects.get().id]))
        self.assertEqual(list(self.client.session["cart"]), [f"{p.id}-{m.id}"])


class StockAlertTests(TestCase):
    def test_notified_once_when_back_in_stock(self):
        from .models import StockAlert
        product = make_product(name="Rare Serum", stock=0)
        self.client.post(reverse("stock_alert", args=[product.id]), {"email": "Fan@Example.com"})
        self.client.post(reverse("stock_alert", args=[product.id]), {"email": "fan@example.com"})
        self.assertEqual(StockAlert.objects.count(), 1)
        self.assertContains(self.client.get(reverse("product_detail", args=[product.id])), "Notify me")
        with self.captureOnCommitCallbacks(execute=True):
            product.stock = 4
            product.save()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("back in stock", mail.outbox[0].subject)
        self.assertEqual(mail.outbox[0].to, ["fan@example.com"])
        with self.captureOnCommitCallbacks(execute=True):
            product.stock = 5
            product.save()
        self.assertEqual(len(mail.outbox), 1)

    def test_variant_alert_waits_for_that_variant(self):
        product = make_product(name="Tee", stock=0)
        small = ProductVariant.objects.create(product=product, size="S", stock=0)
        large = ProductVariant.objects.create(product=product, size="L", stock=0)
        product.sync_variants()
        self.client.post(reverse("stock_alert", args=[product.id]), {"email": "a@example.com", "variant": small.id})
        with self.captureOnCommitCallbacks(execute=True):
            large.stock = 3
            large.save()
        self.assertEqual(len(mail.outbox), 0)
        with self.captureOnCommitCallbacks(execute=True):
            small.stock = 1
            small.save()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Tee (S)", mail.outbox[0].subject)


@override_settings(STRIPE_SECRET_KEY="sk_test_dummy", STRIPE_WEBHOOK_SECRET="whsec_dummy")
class GuestCheckoutTests(TestCase):
    """Guest checkout is off by default; these cover stores that switch it on."""
    def setUp(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={"allow_guest_checkout": True})
        cache.clear()
        self.product = make_product(name="Guest Serum", price=Decimal("15.00"), stock=5)

    def test_guest_must_sign_in_when_switched_off(self):
        SiteSettings.objects.filter(pk=1).update(allow_guest_checkout=False)
        cache.clear()
        self.client.post(reverse("add_to_cart", args=[self.product.id]))
        r = self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "cod"})
        self.assertTrue(r.url.startswith(reverse("login")))
        self.assertFalse(Order.objects.exists())
        self.assertContains(self.client.get(reverse("cart")), "Sign in to check out")
        user = User.objects.create_user("later", "later@example.com", "pass12345")
        self.client.post(reverse("login"), {"username": "later", "password": "pass12345"})
        self.assertContains(self.client.get(reverse("cart")), "Guest Serum")  # cart kept through sign-in

    def _guest_cod(self, client=None):
        c = client or self.client
        c.post(reverse("add_to_cart", args=[self.product.id]))
        with self.captureOnCommitCallbacks(execute=True):
            r = c.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "cod"})
        return Order.objects.latest("id"), r

    def test_guest_places_and_tracks_order(self):
        self.client.post(reverse("add_to_cart", args=[self.product.id]))
        self.assertContains(self.client.get(reverse("checkout")), "Checking out as a guest")
        order, r = self._guest_cod()
        self.assertIsNone(order.user)
        self.assertEqual(order.guest_email, "jane@example.com")
        self.assertRedirects(r, reverse("order_success", args=[order.id]))
        self.assertContains(self.client.get(r.url), "Create an account")
        email = mail.outbox[0].alternatives[0][0]
        self.assertIn(order.tracking_path(), email)
        # Anyone else needs the private link
        stranger = Client()
        self.assertEqual(stranger.get(reverse("order_success", args=[order.id])).status_code, 404)
        self.assertEqual(stranger.get(reverse("order_track", args=[order.id, "nope"])).status_code, 404)
        page = stranger.get(order.tracking_path())
        self.assertContains(page, "Guest Serum")
        # ... and can cancel from it
        stranger.post(reverse("cancel_order", args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)

    def test_guest_cannot_touch_member_orders(self):
        member = User.objects.create_user("mem", "mem@example.com", "pass12345")
        order = Order.objects.create(user=member, full_name="M", address="x", city="y", phone="1")
        self.client.post(reverse("cancel_order", args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.status, "pending")
        r = self.client.get(order.tracking_path())
        self.assertEqual(r.status_code, 302)

    def test_guest_card_payment_uses_email_not_customer(self):
        self.client.post(reverse("add_to_cart", args=[self.product.id]))
        fake = mock.MagicMock(id="cs_g", url="https://checkout.stripe.com/g")
        with mock.patch("stripe.checkout.Session.create", return_value=fake) as create:
            self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "card"})
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["customer_email"], "jane@example.com")
        self.assertNotIn("customer", kwargs)
        order = Order.objects.get()
        session = {"id": "cs_g", "payment_status": "paid", "status": "complete", "amount_total": order.total_cents,
                   "currency": order.currency, "payment_intent": "pi_g", "metadata": {"order_id": str(order.id)}}
        with mock.patch("stripe.checkout.Session.retrieve", return_value=mock.MagicMock(to_dict=lambda: session)):
            r = self.client.get(reverse("payment_success") + "?session_id=cs_g")
        self.assertRedirects(r, reverse("order_success", args=[order.id]), fetch_redirect_response=False)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "paid")

    def test_guest_coupon_limit_by_email(self):
        Coupon.objects.create(code="ONCE", percent_off=10, per_user_limit=1)
        self.client.post(reverse("apply_coupon"), {"coupon_code": "ONCE"})
        self._guest_cod()
        other = Client()
        other.post(reverse("add_to_cart", args=[self.product.id]))
        other.post(reverse("apply_coupon"), {"coupon_code": "ONCE"})
        other.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "cod"})
        self.assertEqual(Order.objects.count(), 1)  # same email can't reuse it

    def test_guest_orders_join_account_after_email_verified(self):
        order, _ = self._guest_cod()
        user = User.objects.create_user("jane", "jane@example.com", "pass12345")
        self.client.force_login(user)
        from django.core.cache import cache as _cache
        Profile.objects.create(user=user, referral_code="JN1")
        _cache.set(f"email_verify_code:{user.id}", "123456", 900)
        self.client.post(reverse("verify_email"), {"code": "123456"})
        order.refresh_from_db()
        self.assertEqual(order.user, user)
        self.assertContains(self.client.get(reverse("my_orders")), "Guest Serum")


class SavedCartTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("cart", "cart@example.com", "pass12345")
        self.product = make_product(name="Lip Balm", price=Decimal("4.00"), stock=9)

    def test_cart_follows_account_to_another_device(self):
        phone = Client()
        phone.post(reverse("login"), {"username": "cart", "password": "pass12345"})
        phone.post(reverse("add_to_cart", args=[self.product.id]), {"quantity": 2})
        laptop = Client()
        laptop.post(reverse("add_to_cart", args=[make_product(name="Other").id]))  # browsing as a visitor
        laptop.post(reverse("login"), {"username": "cart", "password": "pass12345"})
        self.assertEqual(laptop.session["cart"][str(self.product.id)], 2)
        self.assertEqual(len(laptop.session["cart"]), 2)
        self.assertEqual(len(Profile.objects.get(user=self.user).saved_cart), 2)

    def test_reminder_sent_once_after_a_day(self):
        from datetime import timedelta
        from django.core.management import call_command
        from django.utils import timezone
        self.client.force_login(self.user)
        self.client.post(reverse("add_to_cart", args=[self.product.id]))
        call_command("send_cart_reminders", stdout=open(os.devnull, "w"))
        self.assertEqual(len(mail.outbox), 0)  # too soon
        Profile.objects.filter(user=self.user).update(cart_updated_at=timezone.now() - timedelta(hours=25))
        call_command("send_cart_reminders", stdout=open(os.devnull, "w"))
        call_command("send_cart_reminders", stdout=open(os.devnull, "w"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Lip Balm", mail.outbox[0].alternatives[0][0])

    def test_no_reminder_when_opted_out_or_ordered(self):
        from datetime import timedelta
        from django.core.management import call_command
        from django.utils import timezone
        self.client.force_login(self.user)
        self.client.post(reverse("add_to_cart", args=[self.product.id]))
        self.client.post(reverse("account_privacy"), {"action": "emails"})  # both boxes unticked
        Profile.objects.filter(user=self.user).update(cart_updated_at=timezone.now() - timedelta(hours=30))
        call_command("send_cart_reminders", stdout=open(os.devnull, "w"))
        self.assertEqual(len(mail.outbox), 0)
        Profile.objects.filter(user=self.user).update(cart_reminders=True)
        self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "cod"})
        self.assertEqual(Profile.objects.get(user=self.user).saved_cart, {})


from django.test import TransactionTestCase  # noqa: E402


class BackupTests(TransactionTestCase):
    """Not wrapped in a transaction: SQLite can't copy a database while the
    test's own open transaction holds its write lock."""

    def test_backup_and_list(self):
        import tempfile
        from django.core.management import call_command
        with tempfile.TemporaryDirectory() as tmp, override_settings(BASE_DIR=tmp, MEDIA_ROOT=os.path.join(tmp, "m")):
            for _ in range(3):
                call_command("backup_data", "--keep", "2", stdout=open(os.devnull, "w"))
            files = os.listdir(os.path.join(tmp, "backups"))
            self.assertTrue(1 <= len(files) <= 2)
            self.assertTrue(all(f.startswith("backup-") and f.endswith(".tar.gz") for f in files))


class TwoStepSignInTests(TestCase):
    def setUp(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={"require_staff_2fa": True})
        cache.clear()
        self.staff = User.objects.create_user("owner", "owner@example.com", "Owner-pass-1", is_staff=True)

    def _now_code(self, secret, offset=0):
        from . import twofactor
        return twofactor.code_at(secret, twofactor.current_step() + offset)

    def _enable(self, client):
        client.get(reverse("two_factor_setup"))
        secret = client.session["totp_setup_secret"]
        r = client.post(reverse("two_factor_setup"), {"code": self._now_code(secret)})
        return secret, r

    def test_staff_must_set_up_before_admin(self):
        self.client.force_login(self.staff)
        self.assertRedirects(self.client.get(reverse("manage_dashboard")), reverse("two_factor_setup"), fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/admin/"), reverse("two_factor_setup"), fetch_redirect_response=False)
        page = self.client.get(reverse("two_factor_setup"))
        self.assertContains(page, "<svg")
        r = self.client.post(reverse("two_factor_setup"), {"code": "000000"})
        self.assertContains(r, "didn&#x27;t match")
        secret, r = self._enable(self.client)
        self.assertContains(r, "Save your backup codes")
        self.assertEqual(self.client.get(reverse("manage_dashboard")).status_code, 200)
        self.assertTrue(Profile.objects.get(user=self.staff).totp_enabled)

    def test_sign_in_needs_code(self):
        self.client.force_login(self.staff)
        secret, r = self._enable(self.client)
        codes = r.context["codes"]
        self.client.logout()
        r = self.client.post(reverse("login"), {"username": "owner", "password": "Owner-pass-1", "next": "/manage/"})
        self.assertRedirects(r, reverse("login_code"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)  # not signed in yet
        self.client.post(reverse("login_code"), {"code": "123456"})
        self.assertNotIn("_auth_user_id", self.client.session)
        r = self.client.post(reverse("login_code"), {"code": self._now_code(secret, 1)})
        self.assertRedirects(r, "/manage/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/manage/").status_code, 200)
        # The same code can't be used again
        other = Client()
        other.post(reverse("login"), {"username": "owner", "password": "Owner-pass-1"})
        other.post(reverse("login_code"), {"code": self._now_code(secret, 1)})
        self.assertNotIn("_auth_user_id", other.session)
        # A backup code works once
        other.post(reverse("login"), {"username": "owner", "password": "Owner-pass-1"})
        other.post(reverse("login_code"), {"code": codes[0]})
        self.assertIn("_auth_user_id", other.session)
        third = Client()
        third.post(reverse("login"), {"username": "owner", "password": "Owner-pass-1"})
        third.post(reverse("login_code"), {"code": codes[0]})
        self.assertNotIn("_auth_user_id", third.session)

    def test_django_admin_login_goes_through_store_login(self):
        r = self.client.get("/admin/login/?next=/admin/")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.url.startswith("/login/"))

    def test_reset_command(self):
        from django.core.management import call_command
        self.client.force_login(self.staff)
        self._enable(self.client)
        call_command("reset_two_factor", "owner@example.com", stdout=open(os.devnull, "w"))
        self.assertFalse(Profile.objects.get(user=self.staff).totp_enabled)

    def test_customers_can_opt_in_but_are_not_forced(self):
        buyer = User.objects.create_user("cust", "cust@example.com", "Cust-pass-1")
        self.client.force_login(buyer)
        self.assertContains(self.client.get(reverse("account_security")), "Set up")
        secret, _ = self._enable(self.client)
        self.client.post(reverse("two_factor_disable"), {"password": "Cust-pass-1", "code": self._now_code(secret, 1)})
        self.assertFalse(Profile.objects.get(user=buyer).totp_enabled)


@override_settings(STRIPE_SECRET_KEY="sk_test_dummy", STRIPE_WEBHOOK_SECRET="whsec_dummy")
class SystemCheckTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("boss", "boss@example.com", "pass12345", is_staff=True)
        self.client.force_login(self.staff)

    def test_page_and_live_tests(self):
        page = self.client.get(reverse("manage_system"))
        self.assertContains(page, "System check")
        self.assertContains(page, "Database")
        r = self.client.post(reverse("manage_system"), {"action": "email", "to": "me@example.com"}, follow=True)
        self.assertContains(r, "Test email sent to me@example.com")
        self.assertEqual(mail.outbox[-1].to, ["me@example.com"])
        with mock.patch("stripe.Balance.retrieve", return_value={}):
            r = self.client.post(reverse("manage_system"), {"action": "stripe"}, follow=True)
        self.assertContains(r, "Stripe connection works")
        import stripe as _stripe
        err = _stripe.error.AuthenticationError("Invalid API Key provided", http_status=401)
        with mock.patch("stripe.Balance.retrieve", side_effect=err):
            r = self.client.post(reverse("manage_system"), {"action": "stripe"}, follow=True)
        self.assertContains(r, "rejected the secret key")
        r = self.client.post(reverse("manage_system"), {"action": "storage"}, follow=True)
        self.assertContains(r, "Image storage works")

    def test_email_failures_are_recorded(self):
        from .views import _send_html_email
        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=OSError("SMTP down")):
            _send_html_email("Hello", "bees/emails/security_notice.html", {"user": self.staff, "event": "x", "detail": "y", "reset_url": "/"}, "a@example.com")
        self.assertTrue(AuditLog.objects.filter(action__contains="SMTP down").exists())
        self.assertContains(self.client.get(reverse("manage_system")), "SMTP down")

    def _hook(self, typ, obj):
        payload = json.dumps({"type": typ, "data": {"object": obj}})
        with mock.patch("stripe.Webhook.construct_event", return_value={}):
            return self.client.post(reverse("stripe_webhook"), data=payload, content_type="application/json", HTTP_STRIPE_SIGNATURE="t=1,v1=x")

    def test_refund_made_in_stripe_dashboard_updates_order(self):
        order = Order.objects.create(user=self.staff, full_name="A", address="x", city="y", phone="1", status="confirmed",
                                     payment_method="card", payment_status="paid", stripe_payment_intent="pi_dash")
        self.assertEqual(self._hook("charge.refunded", {"payment_intent": "pi_dash", "refunded": True, "amount": 1000}).status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "refunded")
        self.assertTrue(Notification.objects.filter(user=self.staff, message__icontains="refunded in Stripe").exists())
        self.assertIsNotNone(SiteSettings.objects.get(pk=1).stripe_last_webhook)

    def test_dispute_alerts_staff(self):
        self._hook("charge.dispute.created", {"payment_intent": "pi_x", "amount": 2500})
        self.assertTrue(Notification.objects.filter(user=self.staff, message__icontains="disputed").exists())

    def test_failed_refund_alerts_staff(self):
        buyer = User.objects.create_user("b", "b@example.com", "pass12345")
        order = Order.objects.create(user=buyer, full_name="A", address="x", city="y", phone="1", status="confirmed",
                                     payment_method="card", payment_status="paid", stripe_payment_intent="pi_f")
        OrderItem.objects.create(order=order, product_name="X", price=Decimal("5"), quantity=1)
        c = Client(); c.force_login(buyer)
        with mock.patch("stripe.Refund.create", side_effect=Exception("card_declined")):
            r = c.post(reverse("cancel_order", args=[order.id]), follow=True)
        self.assertContains(r, "team has been alerted")
        self.assertTrue(Notification.objects.filter(user=self.staff, message__icontains="refund failed").exists())
        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")


class HelpAssistantTests(TestCase):
    def test_topics_answer_from_store_settings(self):
        from .models import ShippingZone
        ShippingZone.objects.create(name="Europe", countries="DE, FR", fee=Decimal("12"), delivery_days=8)
        SiteSettings.objects.update_or_create(pk=1, defaults={"return_days": 30})
        cache.clear()
        d = self.client.get(reverse("chat_topic") + "?key=start").json()
        self.assertIn("help assistant", d["message"])
        self.assertTrue(any(o["key"] == "human" for o in d["options"]))
        self.assertIn("Europe: $12.00", self.client.get(reverse("chat_topic") + "?key=shipping").json()["message"])
        self.assertIn("within 30 days", self.client.get(reverse("chat_topic") + "?key=returns").json()["message"])

    def test_track_shows_own_orders(self):
        user = User.objects.create_user("t", "t@example.com", "pass12345")
        Order.objects.create(user=user, full_name="T", address="x", city="y", phone="1", status="shipped", tracking_number="1Z9")
        self.client.force_login(user)
        msg = self.client.get(reverse("chat_topic") + "?key=track").json()["message"]
        self.assertIn("Shipped", msg)
        self.assertIn("1Z9", msg)

    def test_typed_question_gets_instant_answer_and_reaches_team(self):
        r = self.client.post(reverse("chat_send"), {"message": "How do I return a broken item?"}).json()
        self.assertIn("return items within", r["reply"]["message"])
        self.assertTrue(r["reply"]["auto"])
        r = self.client.post(reverse("chat_send"), {"message": "I want to talk to someone about bulk orders"}).json()
        self.assertIn("member of our team will reply", r["reply"]["message"])
        thread = ChatThread.objects.get()
        self.assertEqual(thread.messages.filter(sender="user").count(), 2)

    def test_team_reply_badge_notification_and_email(self):
        user = User.objects.create_user("c", "c@example.com", "pass12345")
        staff = User.objects.create_user("boss", "boss@example.com", "pass12345", is_staff=True)
        self.client.force_login(user)
        self.client.post(reverse("chat_send"), {"message": "Hello?"})
        thread = ChatThread.objects.get(user=user)
        s = Client(); s.force_login(staff)
        s.post(reverse("manage_support_thread", args=[thread.id]), {"action": "reply", "message": "Hi! How can we help?"})
        self.assertEqual(self.client.get(reverse("chat_unread")).json()["unread"], 1)
        self.assertTrue(any(m.to == ["c@example.com"] and "reply" in m.subject.lower() for m in mail.outbox))
        self.client.get(reverse("chat_messages"))
        self.assertEqual(self.client.get(reverse("chat_unread")).json()["unread"], 0)


@override_settings(STRIPE_SECRET_KEY="sk_test_dummy")
class PaymentPageExpiryTests(TestCase):
    def test_expiry_safely_above_stripe_minimum(self):
        import time as _time
        user = User.objects.create_user("exp", "exp@example.com", "pass12345")
        self.client.force_login(user)
        product = make_product(price=Decimal("10.00"))
        self.client.post(reverse("add_to_cart", args=[product.id]))
        fake = mock.MagicMock(id="cs_e", url="https://checkout.stripe.com/e")
        with mock.patch("stripe.checkout.Session.create", return_value=fake) as create:
            self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "card"})
        # Stripe rejects sessions that expire in under 30 minutes when they arrive.
        self.assertGreaterEqual(create.call_args.kwargs["expires_at"] - int(_time.time()), 45 * 60)


class SellerCenterTests(TestCase):
    """The Seller Center: numbers, orders, products, payouts and team access."""

    def setUp(self):
        from django.core import mail as _mail
        self.mail = _mail
        owner = User.objects.create_user("shop", "shop@example.com", "pass12345")
        self.seller = SellerAccount.objects.create(user=owner, account_type="organization", business_name="Shop Co",
                                                   bank_details="IBAN GB00 TEST")
        self.seller.status = "approved"
        self.seller.save()
        self.product = make_product(name="Lamp", price=Decimal("50.00"), stock=20,
                                    seller_account=self.seller, seller_name="Shop Co")
        self.buyer = User.objects.create_user("buyer", "buyer@example.com", "pass12345")

    def _order(self, qty=2, method="cod"):
        self.client.force_login(self.buyer)
        self.client.post(reverse("add_to_cart", args=[self.product.id]), {"quantity": qty})
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": method})
        order = Order.objects.filter(user=self.buyer).latest("id")
        self.client.force_login(self.seller.user)
        return order

    def test_new_cod_order_alerts_seller_once(self):
        order = self._order()
        alerts = Notification.objects.filter(user=self.seller.user, message__icontains=f"New order #{order.id}")
        self.assertEqual(alerts.count(), 1)
        self.assertTrue(any("new order" in m.subject.lower() and "shop@example.com" in m.to for m in self.mail.outbox))
        from . import seller_center
        seller_center.notify_new_order(order)  # already done: no second alert
        self.assertEqual(alerts.count(), 1)
        order.refresh_from_db()
        self.assertTrue(order.sellers_notified)

    def test_unpaid_card_order_does_not_alert_seller_until_paid(self):
        from . import seller_center
        order = self._order()
        Notification.objects.filter(user=self.seller.user).delete()
        Order.objects.filter(pk=order.pk).update(payment_method="card", payment_status="pending", sellers_notified=False)
        order.refresh_from_db()
        seller_center.notify_new_order(order)
        self.assertFalse(Notification.objects.filter(user=self.seller.user, message__icontains="New order").exists())
        Order.objects.filter(pk=order.pk).update(payment_status="paid", status="confirmed")
        order.refresh_from_db()
        seller_center.notify_new_order(order)
        self.assertTrue(Notification.objects.filter(user=self.seller.user, message__icontains=f"New order #{order.id}").exists())

    def test_cancelling_tells_seller_not_to_ship(self):
        order = self._order()
        self.client.force_login(self.buyer)
        self.client.post(reverse("cancel_order", args=[order.id]))
        self.assertTrue(Notification.objects.filter(user=self.seller.user, message__icontains="cancelled").exists())

    def test_overview_shows_sales_commission_and_earnings(self):
        self._order(qty=2)  # $100 sale, 20% commission -> $80
        page = self.client.get(reverse("seller_dashboard") + "?range=7")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "$100.00")
        self.assertContains(page, "$20.00")
        self.assertContains(page, "$80.00")
        self.assertContains(page, "United States")
        for key in ("30", "90", "365", "bogus"):
            self.assertEqual(self.client.get(reverse("seller_dashboard") + f"?range={key}").status_code, 200)

    def test_every_seller_center_page_loads(self):
        order = self._order()
        for name, args in [
            ("seller_dashboard", []), ("seller_orders", []), ("seller_order", [order.id]),
            ("seller_packing_slip", [order.id]), ("seller_products", []), ("seller_earnings", []),
            ("seller_returns", []), ("seller_reviews", []), ("seller_team", []), ("seller_store_settings", []),
            ("seller_add_product", []), ("seller_edit_product", [self.product.id]), ("store_page", ["Shop Co"]),
        ]:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200)
        for tab in ("to_pack", "packed", "in_transit", "delivered", "unpaid", "cancelled", "all"):
            self.assertEqual(self.client.get(reverse("seller_orders") + f"?tab={tab}&q=Jane").status_code, 200)
        for tab in ("all", "live", "review", "rejected", "low", "out"):
            self.assertEqual(self.client.get(reverse("seller_products") + f"?tab={tab}&sort=stock").status_code, 200)
        for tab in ("questions", "answered", "reviews", "store"):
            self.assertEqual(self.client.get(reverse("seller_reviews") + f"?tab={tab}").status_code, 200)

    def test_ship_order_with_tracking_moves_order_and_emails_customer(self):
        order = self._order()
        self.mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("seller_ship_order", args=[order.id]), {
                "status": "handed_to_courier", "courier_name": "DHL", "tracking_number": "JD0001",
            })
        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")
        self.assertEqual(order.tracking_number, "JD0001")
        self.assertTrue(order.items.filter(fulfillment_status="handed_to_courier").exists())
        self.assertTrue(any("on its way" in m.subject for m in self.mail.outbox))

    def test_bulk_mark_packed(self):
        o1, o2 = self._order(qty=1), self._order(qty=1)
        self.client.post(reverse("seller_bulk_fulfilment"), {"status": "packed", "order": [o1.id, o2.id]})
        self.assertEqual(OrderItem.objects.filter(order__in=[o1, o2], fulfillment_status="packed").count(), 2)
        OrderItem.objects.filter(order=o1).update(fulfillment_status="delivered")
        self.client.post(reverse("seller_bulk_fulfilment"), {"status": "packed", "order": [o1.id]})
        self.assertTrue(OrderItem.objects.filter(order=o1, fulfillment_status="delivered").exists())  # never moved back

    def test_cannot_touch_another_sellers_order(self):
        order = self._order()
        other = User.objects.create_user("other", "o@example.com", "pass12345")
        acct = SellerAccount.objects.create(user=other, business_name="Other")
        acct.status = "approved"
        acct.save()
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("seller_order", args=[order.id])).status_code, 404)
        self.assertEqual(self.client.get(reverse("seller_packing_slip", args=[order.id])).status_code, 404)
        self.client.post(reverse("seller_ship_order", args=[order.id]), {"status": "delivered"})
        self.client.post(reverse("seller_bulk_fulfilment"), {"status": "delivered", "order": [order.id]})
        self.assertFalse(OrderItem.objects.filter(order=order).exclude(fulfillment_status="pending").exists())
        self.client.post(reverse("seller_quick_update", args=[self.product.id]), {"price": "1", "stock": "0"})
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal("50.00"))

    def test_csv_exports(self):
        self._order()
        r = self.client.get(reverse("seller_orders") + "?tab=all&export=csv")
        self.assertEqual(r["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("Lamp", r.content.decode())
        r = self.client.get(reverse("seller_earnings") + "?export=csv")
        body = r.content.decode()
        self.assertIn("Statement for Shop Co", body)
        self.assertIn("80.00", body)

    def test_csv_neutralises_formulas(self):
        self.product.name = "=HYPERLINK(\"http://x\")"
        self.product.save()
        self._order()
        body = self.client.get(reverse("seller_orders") + "?tab=all&export=csv").content.decode()
        self.assertIn("'=HYPERLINK", body)

    def test_quick_update_price_and_stock(self):
        self.client.force_login(self.seller.user)
        self.client.post(reverse("seller_quick_update", args=[self.product.id]), {"price": "42.50", "stock": "3"})
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal("42.50"))
        self.assertEqual(self.product.stock, 3)
        self.client.post(reverse("seller_quick_update", args=[self.product.id]), {"price": "-1", "stock": "3"})
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal("42.50"))

    def test_duplicate_product_goes_to_review(self):
        self.client.force_login(self.seller.user)
        r = self.client.post(reverse("seller_duplicate_product", args=[self.product.id]))
        copy = Product.objects.exclude(pk=self.product.pk).get()
        self.assertRedirects(r, reverse("seller_edit_product", args=[copy.pk]), fetch_redirect_response=False)
        self.assertEqual(copy.approval_status, "pending")
        self.assertEqual(copy.seller_account, self.seller)

    def test_payout_request_and_staff_records_it(self):
        from .models import Payout
        order = self._order(qty=2)
        # Not delivered yet: nothing available
        self.client.post(reverse("seller_request_payout"), {"amount": "80"})
        self.assertFalse(Payout.objects.exists())
        OrderItem.objects.filter(order=order).update(fulfillment_status="delivered")
        o = Order.objects.get(pk=order.pk)
        o.status = "delivered"
        o.save()
        self.client.post(reverse("seller_request_payout"), {"amount": "500"})  # too much
        self.assertFalse(Payout.objects.exists())
        self.client.post(reverse("seller_request_payout"), {"amount": "80"})
        payout = Payout.objects.get()
        self.assertEqual(payout.status, "requested")
        self.client.post(reverse("seller_request_payout"), {"amount": "10"})  # one at a time
        self.assertEqual(Payout.objects.count(), 1)
        staff = User.objects.create_user("boss", "boss@example.com", "pass12345", is_staff=True)
        self.client.force_login(staff)
        self.assertContains(self.client.get(reverse("manage_sellers") + "?status=payout"), "Shop Co")
        self.assertContains(self.client.get(reverse("manage_seller", args=[self.seller.id])), "asked for $80.00")
        self.client.post(reverse("manage_seller", args=[self.seller.id]), {"action": "payout", "amount": "80", "method": "wise", "reference": "TX-1"})
        payout.refresh_from_db()
        self.seller.refresh_from_db()
        self.assertEqual(payout.status, "paid")
        self.assertEqual(payout.method, "wise")
        self.assertEqual(self.seller.total_paid_out, Decimal("80.00"))
        self.assertEqual(self.seller.amount_owed, 0)
        self.client.force_login(self.seller.user)
        self.assertContains(self.client.get(reverse("seller_earnings")), "TX-1")

    def test_staff_team_member_cannot_see_money_pages(self):
        helper = User.objects.create_user("helper", "h@example.com", "pass12345")
        OrganizationMember.objects.create(organization=self.seller, user=helper, role="staff")
        self.client.force_login(helper)
        self.assertEqual(self.client.get(reverse("seller_dashboard")).status_code, 200)
        self.assertEqual(self.client.get(reverse("seller_orders")).status_code, 200)
        for name in ("seller_earnings", "seller_team", "seller_store_settings"):
            self.assertRedirects(self.client.get(reverse(name)), reverse("seller_dashboard"), fetch_redirect_response=False)
        self.client.post(reverse("seller_request_payout"), {"amount": "10"})
        from .models import Payout
        self.assertFalse(Payout.objects.exists())

    def test_holiday_mode_hides_products(self):
        self.client.force_login(self.seller.user)
        self.client.post(reverse("seller_vacation"), {"vacation_mode": "on", "vacation_message": "Back Monday"})
        self.seller.refresh_from_db()
        self.assertTrue(self.seller.vacation_mode)
        self.assertFalse(Product.objects.live().filter(pk=self.product.pk).exists())
        self.client.logout()
        self.assertEqual(self.client.get(reverse("product_detail", args=[self.product.id])).status_code, 404)
        self.assertContains(self.client.get(reverse("store_page", args=["Shop Co"])), "Back Monday")
        self.client.force_login(self.seller.user)
        self.client.post(reverse("seller_vacation"), {"vacation_mode": "off"})
        self.assertTrue(Product.objects.live().filter(pk=self.product.pk).exists())

    def test_pending_seller_sees_center_but_cannot_sell(self):
        u = User.objects.create_user("newbie", "n@example.com", "pass12345")
        SellerAccount.objects.create(user=u, business_name="Newbie")
        self.client.force_login(u)
        page = self.client.get(reverse("seller_dashboard"))
        self.assertContains(page, "under review")
        self.assertEqual(self.client.get(reverse("seller_add_product")).status_code, 404)

    def test_store_page_search_and_stats(self):
        self._order()
        self.client.logout()
        page = self.client.get(reverse("store_page", args=["Shop Co"]) + "?q=lamp&sort=price_asc")
        self.assertContains(page, "Lamp")
        self.assertContains(page, "Registered business")
        self.assertContains(page, "2+ sold")


class ProfileReferralCodeTests(TestCase):
    def test_profiles_created_without_a_code_get_a_unique_one(self):
        from .models import Profile
        a = Profile.objects.create(user=User.objects.create_user("pa", "pa@example.com", "x"))
        b, _ = Profile.objects.get_or_create(user=User.objects.create_user("pb", "pb@example.com", "x"))
        self.assertTrue(a.referral_code and b.referral_code)
        self.assertNotEqual(a.referral_code, b.referral_code)


class SystemCheckPaymentHistoryTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("boss2", "boss2@example.com", "pass12345", is_staff=True)
        self.client.force_login(self.staff)

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_old_payment_errors_clear_once_stripe_works(self):
        from .alerts import PAYMENT_FAILED, log
        log(f"{PAYMENT_FAILED}: order #1 - The Supabase relay refused the request: RELAY_SECRET mismatch")
        with mock.patch("stripe.Balance.retrieve", side_effect=Exception("still down")):
            page = self.client.get(reverse("manage_system"))
        self.assertContains(page, "RELAY_SECRET mismatch")
        with mock.patch("stripe.Balance.retrieve", return_value={}):
            self.client.post(reverse("manage_system"), {"action": "stripe"})
        page = self.client.get(reverse("manage_system"))
        self.assertNotContains(page, "RELAY_SECRET mismatch")
        self.assertContains(page, "1 earlier error was already fixed")
        # A new failure after that is reported again
        log(f"{PAYMENT_FAILED}: order #2 - Stripe rejected the secret key")
        self.assertContains(self.client.get(reverse("manage_system")), "Stripe rejected the secret key")

    def test_success_marker_is_a_single_entry(self):
        from .alerts import PAYMENT_OK, payments_working
        for _ in range(3):
            payments_working("test")
        self.assertEqual(AuditLog.objects.filter(action__startswith=PAYMENT_OK).count(), 1)


class BackupButtonTests(TransactionTestCase):
    def test_back_up_now_button(self):
        import tempfile
        staff = User.objects.create_user("boss3", "boss3@example.com", "pass12345", is_staff=True)
        from .models import SiteSettings
        SiteSettings.objects.update_or_create(pk=1, defaults={"require_staff_2fa": False})
        self.client.force_login(staff)
        with tempfile.TemporaryDirectory() as tmp, override_settings(BASE_DIR=tmp, MEDIA_ROOT=os.path.join(tmp, "m")):
            r = self.client.post(reverse("manage_system"), {"action": "backup"}, follow=True)
            self.assertContains(r, "Backup saved.")
            self.assertEqual(len(os.listdir(os.path.join(tmp, "backups"))), 1)
            self.assertNotContains(r, "No backups yet")
class StripeObjectCompatibilityTests(TestCase):
    """stripe-python v15+ returns objects that are not dicts; saved cards
    must still work (this used to crash the Saved cards page)."""

    def _obj(self, data):
        from stripe import StripeObject
        return StripeObject.construct_from(data, "sk_test")

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_saved_cards_page_with_real_stripe_objects(self):
        from .models import Profile
        user = User.objects.create_user("cardy", "c@example.com", "pass12345")
        Profile.objects.create(user=user, referral_code="CARDY1", stripe_customer_id="cus_1")
        listing = self._obj({"object": "list", "data": [
            {"id": "pm_1", "object": "payment_method", "card": {"brand": "visa", "last4": "4242", "exp_month": 4, "exp_year": 2030}},
        ]})
        self.client.force_login(user)
        with mock.patch("stripe.Customer.list_payment_methods", return_value=listing):
            r = self.client.get(reverse("payment_methods"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Visa ending in 4242")
        self.assertContains(r, "Expires 04/2030")

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_stripe_outage_shows_message_not_error_page(self):
        from .models import Profile
        user = User.objects.create_user("cardz", "z@example.com", "pass12345")
        Profile.objects.create(user=user, referral_code="CARDZ1", stripe_customer_id="cus_2")
        self.client.force_login(user)
        with mock.patch("stripe.Customer.list_payment_methods", side_effect=Exception("down")):
            r = self.client.get(reverse("payment_methods"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "couldn&#x27;t load your saved cards")

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_remove_and_finish_setup_with_real_stripe_objects(self):
        from . import payments
        with mock.patch("stripe.PaymentMethod.retrieve", return_value=self._obj({"id": "pm_1", "customer": "cus_1"})), \
                mock.patch("stripe.PaymentMethod.detach") as detach:
            self.assertTrue(payments.remove_card("cus_1", "pm_1"))
            detach.assert_called_once()
        with mock.patch("stripe.PaymentMethod.retrieve", return_value=self._obj({"id": "pm_9", "customer": "cus_other"})):
            self.assertFalse(payments.remove_card("cus_1", "pm_9"))
        session = self._obj({"id": "cs_1", "customer": "cus_1", "status": "complete", "setup_intent": {"id": "seti_1", "payment_method": "pm_1"}})
        with mock.patch("stripe.checkout.Session.retrieve", return_value=session), \
                mock.patch("stripe.PaymentMethod.modify") as modify:
            self.assertTrue(payments.finish_card_setup("cs_1", "cus_1"))
            modify.assert_called_once_with("pm_1", allow_redisplay="always")


class PaymentDiagnosticsTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("boss4", "boss4@example.com", "pass12345", is_staff=True)
        self.client.force_login(self.staff)

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_old_errors_clear_automatically_when_stripe_works(self):
        from .alerts import PAYMENT_FAILED, log
        log(f"{PAYMENT_FAILED}: order #1 - RELAY_SECRET mismatch")
        with mock.patch("stripe.Balance.retrieve", return_value={}) as bal:
            page = self.client.get(reverse("manage_system"))
            bal.assert_called_once()
        self.assertNotContains(page, "RELAY_SECRET mismatch")
        self.assertContains(page, "already fixed")

    @override_settings(STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET="whsec_x")
    def test_webhook_with_wrong_secret_is_explained(self):
        self.client.logout()
        r = self.client.post(reverse("stripe_webhook"), data="{}", content_type="application/json", HTTP_STRIPE_SIGNATURE="t=1,v1=bad")
        self.assertEqual(r.status_code, 400)
        self.client.force_login(self.staff)
        with mock.patch("stripe.Balance.retrieve", return_value={}):
            page = self.client.get(reverse("manage_system"))
        self.assertContains(page, "webhook secret doesn&#x27;t match")

    @override_settings(STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET="whsec_x")
    def test_no_webhook_yet_shows_exact_url(self):
        with mock.patch("stripe.Balance.retrieve", return_value={}):
            page = self.client.get(reverse("manage_system"))
        self.assertContains(page, "/payment/stripe/webhook/")

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_add_card_recovers_from_missing_customer(self):
        import stripe as _stripe
        from .models import Profile
        Profile.objects.create(user=self.staff, referral_code="BOSS4X", stripe_customer_id="cus_old")
        missing = _stripe.error.InvalidRequestError("No such customer: 'cus_old'", "customer", code="resource_missing", http_status=400)
        ok = mock.MagicMock(url="https://checkout.stripe.com/c/pay/cs_1")
        with mock.patch("stripe.checkout.Session.create", side_effect=[missing, ok]) as create, \
                mock.patch("stripe.Customer.create", return_value=mock.MagicMock(id="cus_new")):
            r = self.client.post(reverse("add_card"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], "https://checkout.stripe.com/c/pay/cs_1")
        self.assertEqual(create.call_args.kwargs["customer"], "cus_new")
        self.assertEqual(Profile.objects.get(user=self.staff).stripe_customer_id, "cus_new")

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_add_card_failure_shows_reason_to_staff_and_is_logged(self):
        import stripe as _stripe
        from .models import Profile
        Profile.objects.create(user=self.staff, referral_code="BOSS4Y", stripe_customer_id="cus_1")
        err = _stripe.error.InvalidRequestError("Something specific is wrong", None, http_status=400)
        with mock.patch("stripe.checkout.Session.create", side_effect=err), \
                mock.patch("stripe.Customer.list_payment_methods", return_value={"data": []}):
            r = self.client.post(reverse("add_card"), follow=True)
        self.assertContains(r, "Admin info: Stripe error 400")
        self.assertTrue(AuditLog.objects.filter(action__contains="saved-card page").exists())


class ImageUploadTests(TestCase):
    """Uploads are turned upright, resized, stored and shown; links work."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self._media = override_settings(MEDIA_ROOT=self._tmp.name)
        self._media.enable()
        owner = User.objects.create_user("imgshop", "imgshop@example.com", "pass12345")
        self.seller = SellerAccount.objects.create(user=owner, business_name="Img Co")
        self.seller.status = "approved"
        self.seller.save()
        self.client.force_login(owner)

    def tearDown(self):
        self._media.disable()
        self._tmp.cleanup()

    def _photo(self, name="phone.jpg", size=(4000, 3000), orientation=6, fmt="JPEG", mode="RGB"):
        from io import BytesIO
        from PIL import Image
        img = Image.new(mode, size, (200, 30, 30) if mode == "RGB" else (200, 30, 30, 120))
        buf = BytesIO()
        if fmt == "JPEG":
            exif = Image.Exif()
            exif[0x0112] = orientation  # camera says "rotate 90°"
            exif[0x8825] = {1: "N"}     # GPS info that must be stripped
            img.save(buf, fmt, exif=exif.tobytes())
        else:
            img.save(buf, fmt)
        return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg" if fmt == "JPEG" else "image/png")

    def _open_saved(self, url):
        from PIL import Image
        from django.conf import settings
        return Image.open(os.path.join(settings.MEDIA_ROOT, url.replace(settings.MEDIA_URL, "", 1)))

    def _form(self, **extra):
        data = {"name": "Lamp", "category": "table-lamp", "price": "20", "stock": "5", "description": "x"}
        data.update(extra)
        return data

    def test_phone_photo_is_rotated_resized_and_stripped(self):
        r = self.client.post(reverse("seller_add_product"), self._form(image_file=self._photo()))
        self.assertEqual(r.status_code, 302)
        p = Product.objects.get(name="Lamp")
        img = self._open_saved(p.image_url)
        self.assertEqual(max(img.size), 1600)
        self.assertGreater(img.height, img.width)  # turned upright (was 4000x3000 landscape + rotate)
        self.assertFalse(img.getexif())
        self.assertTrue(p.image_url.endswith(".jpg"))

    def test_transparent_png_stays_png(self):
        self.client.post(reverse("seller_add_product"), self._form(image_file=self._photo("logo.png", (300, 300), fmt="PNG", mode="RGBA")))
        self.assertTrue(Product.objects.get(name="Lamp").image_url.endswith(".png"))

    def test_gallery_upload_links_and_remove(self):
        files = [self._photo("a.jpg", (800, 600)), self._photo("b.jpg", (800, 600))]
        self.client.post(reverse("seller_add_product"), self._form(
            image_url="https://example.com/main.jpg", extra_files=files, extra_image_links="https://example.com/c.jpg"))
        p = Product.objects.get(name="Lamp")
        self.assertEqual(p.extra_images.count(), 3)
        page = self.client.get(reverse("product_detail", args=[p.id]))
        self.assertContains(page, "https://example.com/c.jpg")
        # remove one, keep the others
        first = p.extra_images.first()
        self.client.post(reverse("seller_edit_product", args=[p.id]), self._form(image_url=p.image_url, remove_extra=[first.id]))
        self.assertEqual(p.extra_images.count(), 2)
        self.assertFalse(p.extra_images.filter(pk=first.pk).exists())

    def test_too_many_gallery_pictures_rejected(self):
        links = "\n".join(f"https://example.com/{i}.jpg" for i in range(9))
        r = self.client.post(reverse("seller_add_product"), self._form(image_url="https://example.com/m.jpg", extra_image_links=links))
        self.assertContains(r, "up to 8")
        self.assertFalse(Product.objects.exists())

    def test_bad_links_and_heic_rejected(self):
        r = self.client.post(reverse("seller_add_product"), self._form(image_url="http://example.com/x.jpg"))
        self.assertContains(r, "must start with https://")
        heic = SimpleUploadedFile("IMG_0001.HEIC", b"\x00" * 100, content_type="image/heic")
        r = self.client.post(reverse("seller_add_product"), self._form(image_file=heic))
        self.assertContains(r, "Most Compatible")

    def test_admin_can_edit_product_with_uploaded_image_path(self):
        staff = User.objects.create_user("imgboss", "ib@example.com", "pass12345", is_staff=True)
        p = make_product(image_url="/media/products/abc.jpg")
        self.client.force_login(staff)
        page = self.client.get(reverse("manage_product_edit", args=[p.id]))
        self.assertNotContains(page, 'type="url" name="image_url"')  # a /media/ path must not be blocked by the browser
        r = self.client.post(reverse("manage_product_edit", args=[p.id]), {
            "name": p.name, "category": p.category, "price": "9.99", "stock": "3", "image_url": p.image_url,
            "approval_status": "approved",
        })
        self.assertEqual(r.status_code, 302)
        p.refresh_from_db()
        self.assertEqual(p.price, Decimal("9.99"))

    def test_store_logo_is_resized(self):
        r = self.client.post(reverse("seller_store_settings"), {"store_name": "Img Co", "store_logo": self._photo("logo.jpg", (3000, 3000))})
        self.assertEqual(r.status_code, 302)
        self.seller.refresh_from_db()
        from PIL import Image
        with Image.open(self.seller.store_logo.path) as img:
            self.assertLessEqual(max(img.size), 600)


@override_settings(STRIPE_SECRET_KEY="sk_test_dummy", STRIPE_WEBHOOK_SECRET="whsec_dummy")
class PaymentResilienceTests(TestCase):
    """Saved-card problems must never stop a customer from paying."""

    def setUp(self):
        self.user = User.objects.create_user("payer2", "payer2@example.com", "pass12345")
        self.product = make_product(price=Decimal("10.00"), stock=5)
        self.client.force_login(self.user)
        from .models import Profile
        Profile.objects.create(user=self.user, referral_code="PAYER2", stripe_customer_id="cus_stale")

    def _ok(self):
        fake = mock.MagicMock()
        fake.id, fake.url = "cs_ok", "https://checkout.stripe.com/c/pay/cs_ok"
        return fake

    def _pay(self, side_effect):
        self.client.post(reverse("add_to_cart", args=[self.product.id]))
        with mock.patch("stripe.checkout.Session.create", side_effect=side_effect) as create:
            r = self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "card"})
        return r, create

    def test_stale_customer_falls_back_to_guest_payment(self):
        import stripe as _stripe
        from .models import Profile
        missing = _stripe.error.InvalidRequestError("No such customer: 'cus_stale'", "customer", code="resource_missing", http_status=400)
        r, create = self._pay([missing, self._ok()])
        self.assertEqual(r["Location"], "https://checkout.stripe.com/c/pay/cs_ok")
        second = create.call_args_list[1].kwargs
        self.assertNotIn("customer", second)
        self.assertEqual(second["customer_email"], "jane@example.com")
        self.assertEqual(Profile.objects.get(user=self.user).stripe_customer_id, "")
        self.assertTrue(AuditLog.objects.filter(action__contains="retried without saved cards").exists())

    def test_saved_card_option_rejected_falls_back(self):
        import stripe as _stripe
        bad = _stripe.error.InvalidRequestError("Invalid saved_payment_method_options", "saved_payment_method_options", http_status=400)
        r, create = self._pay([bad, self._ok()])
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("saved_payment_method_options", create.call_args_list[1].kwargs)

    def test_network_errors_are_not_retried_as_guest(self):
        r, create = self._pay(Exception("network down"))
        self.assertEqual(create.call_count, 1)
        self.assertRedirects(r, reverse("cart"))

    def test_saved_cards_page_forgets_stale_customer(self):
        import stripe as _stripe
        from .models import Profile
        missing = _stripe.error.InvalidRequestError("No such customer: 'cus_stale'", "customer", code="resource_missing", http_status=400)
        with mock.patch("stripe.Customer.list_payment_methods", side_effect=missing):
            r = self.client.get(reverse("payment_methods"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "No saved cards yet")
        self.assertEqual(Profile.objects.get(user=self.user).stripe_customer_id, "")

    def test_new_customer_uses_fresh_idempotency_key(self):
        from . import payments
        from .models import Profile
        Profile.objects.filter(user=self.user).update(stripe_customer_id="")
        with mock.patch("stripe.Customer.create", return_value=mock.MagicMock(id="cus_a")) as create:
            payments.ensure_customer(self.user)
            Profile.objects.filter(user=self.user).update(stripe_customer_id="")
            payments.ensure_customer(self.user)
        keys = [c.kwargs["idempotency_key"] for c in create.call_args_list]
        self.assertNotEqual(keys[0], keys[1])


@override_settings(STRIPE_SECRET_KEY="sk_test_dummy")
class PaymentSelfTestTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("boss9", "boss9@example.com", "pass12345", is_staff=True)
        self.client.force_login(self.staff)

    def test_all_steps_pass(self):
        sess = mock.MagicMock(id="cs_1", url="https://checkout.stripe.com/x")
        with mock.patch("stripe.Balance.retrieve", return_value={}), \
                mock.patch("stripe.Customer.create", return_value=mock.MagicMock(id="cus_1")), \
                mock.patch("stripe.checkout.Session.create", return_value=sess) as create, \
                mock.patch("stripe.checkout.Session.expire") as expire, \
                mock.patch("stripe.Customer.delete") as delete:
            r = self.client.post(reverse("manage_system"), {"action": "stripe_full"}, follow=True)
        self.assertContains(r, "Full payment test passed")
        self.assertContains(r, "Add-a-card page")
        self.assertEqual(create.call_count, 3)
        self.assertEqual(expire.call_count, 3)
        delete.assert_called_once()

    def test_failing_step_is_explained(self):
        import stripe as _stripe
        err = _stripe.error.InvalidRequestError("The currency provided is not supported", "currency", http_status=400)
        sess = mock.MagicMock(id="cs_1")
        with mock.patch("stripe.Balance.retrieve", return_value={}), \
                mock.patch("stripe.Customer.create", return_value=mock.MagicMock(id="cus_1")), \
                mock.patch("stripe.checkout.Session.create", side_effect=[sess, sess, err]), \
                mock.patch("stripe.checkout.Session.expire"), mock.patch("stripe.Customer.delete"):
            r = self.client.post(reverse("manage_system"), {"action": "stripe_full"}, follow=True)
        self.assertContains(r, "found a problem")
        self.assertContains(r, "The currency provided is not supported")


@override_settings(STRIPE_SECRET_KEY="sk_test_dummy")
class AddCardRequestTests(TestCase):
    def test_setup_page_uses_dashboard_payment_methods(self):
        """Stripe's current API rejects payment_method_types on Checkout."""
        from .models import Profile
        user = User.objects.create_user("cardq", "cq@example.com", "pass12345")
        Profile.objects.create(user=user, referral_code="CARDQ1", stripe_customer_id="cus_q")
        self.client.force_login(user)
        ok = mock.MagicMock(url="https://checkout.stripe.com/c/pay/cs_q")
        with mock.patch("stripe.checkout.Session.create", return_value=ok) as create:
            r = self.client.post(reverse("add_card"))
        self.assertEqual(r["Location"], "https://checkout.stripe.com/c/pay/cs_q")
        kwargs = create.call_args.kwargs
        self.assertNotIn("payment_method_types", kwargs)
        self.assertEqual(kwargs["mode"], "setup")
        self.assertEqual(kwargs["currency"], "usd")


# ---------------------------------------------------------------------------
# Growth pack: seller plans, setup wizard, AI writer, smart search, demo mode
# ---------------------------------------------------------------------------

import io  # noqa: E402
from datetime import timedelta  # noqa: E402

from django.utils import timezone  # noqa: E402

from .models import Notification, Order, PlanPayment, SellerAccount, SellerPlan  # noqa: E402


class SellerPlanTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("planner", "planner@example.com", "pass12345")
        self.seller = SellerAccount.objects.create(user=self.owner, account_type="individual", business_name="Plan Co")
        self.seller.status = "approved"
        self.seller.save()
        self.free = SellerPlan.objects.get(name="Starter")
        self.pro = SellerPlan.objects.get(name="Pro")
        self.client.force_login(self.owner)

    def test_default_plans_and_new_seller_starts_on_free(self):
        self.assertEqual(self.seller.active_plan, self.free)
        self.assertEqual(self.seller.product_limit, 50)
        self.assertEqual(self.seller.effective_commission_rate, 10)
        r = self.client.get(reverse("seller_plan"))
        self.assertContains(r, "Pro")
        self.assertContains(r, "Starter")

    def test_commission_defaults_come_from_store_settings(self):
        site = SiteSettings.load()
        site.commission_individual, site.commission_organization = Decimal("12"), Decimal("18")
        site.save()
        u = User.objects.create_user("org", "org@example.com", "pass12345")
        org = SellerAccount.objects.create(user=u, account_type="organization")
        self.assertEqual(org.commission_rate, Decimal("18"))

    def test_paid_plan_lowers_commission_and_expires(self):
        payment = PlanPayment.objects.create(seller=self.seller, plan=self.pro, plan_name="Pro", months=2, amount=Decimal("38"))
        self.assertTrue(payment.activate())
        self.assertFalse(payment.activate())  # second call does nothing
        seller = SellerAccount.objects.get(pk=self.seller.pk)
        self.assertEqual(seller.active_plan, self.pro)
        self.assertEqual(seller.effective_commission_rate, 7)
        self.assertTrue(seller.has_badge)
        self.assertGreater(seller.plan_expires_at, timezone.now() + timedelta(days=59))
        SellerAccount.objects.filter(pk=seller.pk).update(plan_expires_at=timezone.now() - timedelta(minutes=1))
        seller = SellerAccount.objects.get(pk=self.seller.pk)
        self.assertEqual(seller.active_plan, self.free)
        self.assertEqual(seller.effective_commission_rate, 10)

    def test_renewing_adds_to_time_left(self):
        PlanPayment.objects.create(seller=self.seller, plan=self.pro, plan_name="Pro", months=1, amount=19).activate()
        first = SellerAccount.objects.get(pk=self.seller.pk).plan_expires_at
        PlanPayment.objects.create(seller=self.seller, plan=self.pro, plan_name="Pro", months=1, amount=19).activate()
        second = SellerAccount.objects.get(pk=self.seller.pk).plan_expires_at
        self.assertAlmostEqual((second - first).days, 30, delta=1)

    def test_product_limit_blocks_adding(self):
        self.free.product_limit = 1
        self.free.save()
        make_product(name="One", seller_account=self.seller, seller_name="Plan Co")
        r = self.client.get(reverse("seller_add_product"))
        self.assertRedirects(r, reverse("seller_plan"), fetch_redirect_response=False)

    def test_buy_without_stripe_shows_message(self):
        with self.settings(STRIPE_SECRET_KEY=""):
            r = self.client.post(reverse("seller_buy_plan", args=[self.pro.id]), {"months": "3"}, follow=True)
        self.assertContains(r, "Card payments aren")
        self.assertFalse(PlanPayment.objects.exists())

    @override_settings(STRIPE_SECRET_KEY="sk_test_dummy", STRIPE_WEBHOOK_SECRET="whsec_dummy")
    def test_card_payment_activates_plan_via_webhook(self):
        fake = mock.Mock()
        fake.id, fake.url = "cs_plan_1", "https://checkout.stripe.com/c/pay/cs_plan_1"
        with mock.patch("stripe.checkout.Session.create", return_value=fake) as create:
            r = self.client.post(reverse("seller_buy_plan", args=[self.pro.id]), {"months": "3"})
        self.assertEqual(r.url, fake.url)
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["line_items"][0]["quantity"], 3)
        self.assertEqual(kwargs["line_items"][0]["price_data"]["unit_amount"], 1900)
        payment = PlanPayment.objects.get()
        self.assertEqual(payment.amount, Decimal("57"))
        session = {"id": "cs_plan_1", "payment_status": "paid", "amount_total": 5700,
                   "metadata": {"plan_payment_id": str(payment.id)}}
        payload = json.dumps({"type": "checkout.session.completed", "data": {"object": session}})
        with mock.patch("stripe.Webhook.construct_event", return_value={}):
            self.client.post(reverse("stripe_webhook"), data=payload, content_type="application/json", HTTP_STRIPE_SIGNATURE="x")
        payment.refresh_from_db()
        self.assertEqual(payment.status, "paid")
        self.assertEqual(SellerAccount.objects.get(pk=self.seller.pk).active_plan, self.pro)

    @override_settings(STRIPE_SECRET_KEY="sk_test_dummy")
    def test_underpaid_session_does_not_activate(self):
        from . import payments
        payment = PlanPayment.objects.create(seller=self.seller, plan=self.pro, plan_name="Pro", months=1,
                                             amount=Decimal("19"), stripe_session_id="cs_x")
        payments.confirm_plan_payment(payment.id, {"id": "cs_x", "payment_status": "paid", "amount_total": 100})
        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")

    def test_staff_can_give_plan_and_manage_plans(self):
        admin = User.objects.create_superuser("boss", "boss@example.com", "pass12345")
        self.client.force_login(admin)
        r = self.client.post(reverse("manage_seller", args=[self.seller.id]),
                             {"action": "grant_plan", "plan": self.pro.id, "months": "1", "free": "on"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(SellerAccount.objects.get(pk=self.seller.pk).active_plan, self.pro)
        self.assertEqual(PlanPayment.objects.get().amount, 0)
        r = self.client.get(reverse("manage_plans"))
        self.assertContains(r, "Seller plans")
        self.client.post(reverse("manage_plans"), {"name": "Gold", "price": "99", "commission_discount": "6",
                                                   "product_limit": "", "position": "4", "active": "on"})
        self.assertTrue(SellerPlan.objects.filter(name="Gold", product_limit=None).exists())
        r = self.client.post(reverse("manage_plans"), {"name": "Bad", "price": "5", "commission_discount": "80", "position": "5"})
        self.assertFalse(SellerPlan.objects.filter(name="Bad").exists())

    def test_staff_team_member_cannot_open_plan(self):
        from .models import OrganizationMember
        staff = User.objects.create_user("helper", "h@example.com", "pass12345")
        self.seller.account_type = "organization"
        self.seller.save()
        OrganizationMember.objects.create(organization=self.seller, user=staff, role="staff")
        self.client.force_login(staff)
        r = self.client.get(reverse("seller_plan"))
        self.assertEqual(r.status_code, 302)

    def test_reminder_sent_once(self):
        from django.core.management import call_command
        SellerAccount.objects.filter(pk=self.seller.pk).update(plan=self.pro, plan_expires_at=timezone.now() + timedelta(days=2))
        call_command("plan_reminders", stdout=io.StringIO())
        call_command("plan_reminders", stdout=io.StringIO())
        self.assertEqual(Notification.objects.filter(user=self.owner, message__icontains="plan ends").count(), 1)


class SetupWizardTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("owner", "owner@example.com", "pass12345")
        self.client.force_login(self.admin)

    def test_steps_save_and_finish(self):
        r = self.client.get(reverse("manage_setup"))
        self.assertContains(r, "Set up your store")
        r = self.client.post(reverse("manage_setup"), {"step": "basics", "site_name": "Nova Market", "tagline": "Hi",
                                                       "support_email": "help@nova.test"})
        self.assertRedirects(r, reverse("manage_setup") + "?step=look", fetch_redirect_response=False)
        self.assertEqual(SiteSettings.load().site_name, "Nova Market")
        r = self.client.post(reverse("manage_setup"), {"step": "selling", "commission_individual": "8", "commission_organization": "15"})
        self.assertEqual(SiteSettings.objects.get(pk=1).commission_individual, Decimal("8"))
        r = self.client.post(reverse("manage_setup"), {"step": "selling", "commission_individual": "120", "commission_organization": "15"})
        self.assertContains(r, "between 0 and 90")
        for step in ("look", "checkout", "launch"):
            self.assertEqual(self.client.get(reverse("manage_setup") + f"?step={step}").status_code, 200)
        self.client.post(reverse("manage_setup"), {"finish": "1"})
        self.assertTrue(SiteSettings.objects.get(pk=1).setup_completed)

    def test_dashboard_banner_and_redirect(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={"setup_completed": False})
        from django.core.cache import cache as c
        c.clear()
        self.assertContains(self.client.get(reverse("manage_dashboard")), "Finish setting up your store")
        with self.settings(SETUP_WIZARD_REDIRECT=True):
            r = self.client.get(reverse("manage_dashboard"))
            self.assertRedirects(r, reverse("manage_setup"), fetch_redirect_response=False)
            self.assertEqual(self.client.get(reverse("manage_dashboard")).status_code, 200)  # only once

    def test_plain_staff_cannot_run_setup(self):
        staff = User.objects.create_user("clerk", "c@example.com", "pass12345", is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(reverse("manage_setup")).status_code, 302)


class AIWriterAndSmartSearchTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("writer", "w@example.com", "pass12345")
        self.seller = SellerAccount.objects.create(user=self.owner, business_name="Write Co")
        self.seller.status = "approved"
        self.seller.save()

    def test_builtin_writer_uses_details_and_needs_a_name(self):
        self.client.force_login(self.owner)
        r = self.client.post(reverse("ai_write_listing"), {"name": "Cotton Hoodie", "category": "hoodies",
                                                           "notes": "100% cotton, machine washable"})
        data = r.json()
        self.assertEqual(data["source"], "builtin")
        self.assertIn("Cotton Hoodie", data["description"])
        self.assertIn("• 100% cotton", data["description"])
        self.assertEqual(self.client.post(reverse("ai_write_listing"), {"name": ""}).status_code, 400)

    def test_writer_falls_back_when_ai_unreachable(self):
        from . import ai
        with self.settings(AI_API_KEY="key"), mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            text, source = ai.write_listing("Desk Lamp", "table-lamp", "")
        self.assertEqual(source, "builtin")
        self.assertIn("Desk Lamp", text)

    def test_writer_uses_ai_reply(self):
        from . import ai
        reply = mock.MagicMock()
        reply.__enter__.return_value.read.return_value = json.dumps(
            {"content": [{"type": "text", "text": "A **lovely** lamp for reading late into the night.\n\nWhy you'll love it:\n• Warm light"}]}).encode()
        with self.settings(AI_API_KEY="key"), mock.patch("urllib.request.urlopen", return_value=reply):
            text, source = ai.write_listing("Desk Lamp", "table-lamp", "")
        self.assertEqual(source, "ai")
        self.assertNotIn("**", text)

    def test_writer_only_for_sellers_and_staff(self):
        shopper = User.objects.create_user("shopper", "s@example.com", "pass12345")
        self.client.force_login(shopper)
        self.assertEqual(self.client.post(reverse("ai_write_listing"), {"name": "Lamp"}).status_code, 403)

    def test_parse_search(self):
        from . import ai
        self.assertEqual(ai.parse_search("red hoodie under 40"), {"words": ["red", "hoodie"], "min": None, "max": 40.0, "cheap": False})
        p = ai.parse_search("lamp between 20 and 50")
        self.assertEqual((p["min"], p["max"], p["words"]), (20.0, 50.0, ["lamp"]))
        p = ai.parse_search("sneakers $60-$20")
        self.assertEqual((p["min"], p["max"]), (20.0, 60.0))
        self.assertTrue(ai.parse_search("cheap earbuds")["cheap"])

    def test_smart_search_filters_price_and_matches_words_in_any_order(self):
        make_product(name="Red Cotton Hoodie", category="hoodies", price=Decimal("35"))
        make_product(name="Red Wool Hoodie", category="hoodies", price=Decimal("80"))
        make_product(name="Blue Lamp", category="table-lamp", price=Decimal("20"))
        r = self.client.get(reverse("search_products"), {"q": "hoodies red under 40"})
        self.assertContains(r, "Red Cotton Hoodie")
        self.assertNotContains(r, "Red Wool Hoodie")
        self.assertContains(r, "Under")
        r = self.client.get(reverse("search_products"), {"q": "red lamp"})  # nothing matches both words
        self.assertContains(r, "Close matches")
        self.assertContains(r, "Blue Lamp")


@override_settings(DEMO_MODE=True)
class DemoModeTests(TestCase):
    def setUp(self):
        from django.core.management import call_command
        make_product(name="Existing")
        call_command("demo_setup", stdout=io.StringIO())

    def test_demo_accounts_and_data(self):
        seller = SellerAccount.objects.get(user__username="demo_seller")
        self.assertEqual(seller.status, "approved")
        self.assertTrue(seller.has_badge)
        self.assertTrue(Order.objects.filter(user__username="demo_shopper").exists())
        self.assertFalse(User.objects.get(username="demo_admin").has_usable_password())

    def test_one_click_login_for_each_role(self):
        r = self.client.get(reverse("login"))
        self.assertContains(r, "Try the live demo")
        r = self.client.post(reverse("demo_login", args=["seller"]))
        self.assertRedirects(r, reverse("seller_dashboard"), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("seller_dashboard")).status_code, 200)
        self.assertEqual(self.client.get(reverse("login")).status_code, 200)  # can switch role
        self.client.post(reverse("demo_login", args=["admin"]))
        self.assertEqual(self.client.get(reverse("manage_dashboard")).status_code, 200)
        self.assertEqual(self.client.post(reverse("demo_login", args=["nobody"])).status_code, 404)

    def test_demo_admin_cannot_change_settings(self):
        self.client.post(reverse("demo_login", args=["admin"]))
        before = SiteSettings.load().site_name
        r = self.client.post(reverse("manage_settings"), {"site_name": "Hacked"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(SiteSettings.objects.get(pk=1).site_name, before)

    def test_reset_puts_accounts_back(self):
        from django.core.management import call_command
        SellerAccount.objects.filter(user__username="demo_seller").update(status="suspended", vacation_mode=True)
        call_command("demo_setup", "--reset", stdout=io.StringIO())
        seller = SellerAccount.objects.get(user__username="demo_seller")
        self.assertEqual((seller.status, seller.vacation_mode), ("approved", False))

    @override_settings(DEMO_MODE=False)
    def test_demo_login_off_by_default(self):
        self.assertEqual(self.client.post(reverse("demo_login", args=["admin"])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("login")), "Try the live demo")


# ---------------------------------------------------------------------------
# Marketplace pro: currencies, languages, messages, deals, referrals,
# shipping/tracking, seller badges
# ---------------------------------------------------------------------------

from .models import Conversation, Coupon, Currency, Message, Profile, ShippingZone  # noqa: E402


class CurrencyAndLanguageTests(TestCase):
    def setUp(self):
        cache.clear()
        Currency.objects.all().delete()
        self.eur = Currency.objects.create(code="EUR", symbol="€", rate=Decimal("0.5"), active=True)
        self.product = make_product(name="Mug", price=Decimal("20.00"))

    def test_prices_shown_in_chosen_currency_but_cart_stays_in_store_currency(self):
        self.client.get(reverse("set_currency", args=["EUR"]))
        r = self.client.get(reverse("product_detail", args=[self.product.id]))
        self.assertContains(r, "€10.00")
        self.client.post(reverse("add_to_cart", args=[self.product.id]), {"quantity": 1})
        r = self.client.get(reverse("cart"))
        self.assertContains(r, "$20.00")
        self.assertContains(r, "charged in")
        self.client.get(reverse("set_currency", args=["USD"]))
        self.assertContains(self.client.get(reverse("product_detail", args=[self.product.id])), "$20.00")

    def test_unknown_or_hidden_currency_ignored(self):
        self.eur.active = False
        self.eur.save()
        self.client.get(reverse("set_currency", args=["EUR"]))
        self.assertContains(self.client.get(reverse("product_detail", args=[self.product.id])), "$20.00")

    def test_rates_update(self):
        from . import currency
        reply = mock.MagicMock()
        reply.__enter__.return_value.read.return_value = json.dumps({"result": "success", "rates": {"EUR": 0.9}}).encode()
        with mock.patch("urllib.request.urlopen", return_value=reply):
            updated, error = currency.update_rates()
        self.assertEqual((updated, error), (1, None))
        self.eur.refresh_from_db()
        self.assertEqual(self.eur.rate, Decimal("0.9"))
        with mock.patch("urllib.request.urlopen", side_effect=OSError("blocked")):
            updated, error = currency.update_rates()
        self.assertEqual(updated, 0)
        self.assertIn("by hand", error)

    def test_admin_manages_currencies(self):
        admin = User.objects.create_superuser("cadmin", "c@example.com", "pass12345")
        self.client.force_login(admin)
        self.assertContains(self.client.get(reverse("manage_currencies")), "EUR")
        self.client.post(reverse("manage_currencies"), {"code": "jpy", "symbol": "¥", "rate": "150", "decimals": "0", "position": "2", "active": "on"})
        self.assertTrue(Currency.objects.filter(code="JPY", active=True).exists())
        r = self.client.post(reverse("manage_currencies"), {"code": "USD", "symbol": "$", "rate": "1", "decimals": "2", "position": "3"})
        self.assertContains(r, "store currency")

    def test_new_languages_and_rtl(self):
        site = SiteSettings.load()
        site.show_language_menu = True
        site.save()
        self.client.get(reverse("set_language", args=["ar"]))
        r = self.client.get(reverse("home"))
        self.assertContains(r, 'dir="rtl"')
        self.assertContains(r, "أضف إلى السلة")
        c = Client(HTTP_ACCEPT_LANGUAGE="es-ES,es;q=0.9")
        self.assertContains(c.get(reverse("home")), 'lang="es"')


class BuyerSellerMessageTests(TestCase):
    def setUp(self):
        from django.core import mail as _mail
        self.mail = _mail
        owner = User.objects.create_user("maker", "maker@example.com", "pass12345")
        self.seller = SellerAccount.objects.create(user=owner, business_name="Maker Co")
        self.seller.status = "approved"
        self.seller.save()
        self.product = make_product(name="Vase", seller_account=self.seller, seller_name="Maker Co")
        self.buyer = User.objects.create_user("asker", "asker@example.com", "pass12345")

    def _send(self, user, conv, body, **extra):
        self.client.force_login(user)
        return self.client.post(reverse("message_send", args=[conv.id]), {"body": body, **extra}, HTTP_X_REQUESTED_WITH="fetch")

    def test_full_conversation(self):
        self.client.force_login(self.buyer)
        r = self.client.get(reverse("message_seller", args=[self.product.id]))
        conv = Conversation.objects.get()
        self.assertIn(f"/messages/{conv.id}/?about={self.product.id}", r.url)
        self.assertContains(self.client.get(r.url), "About <strong>Vase</strong>")
        r = self._send(self.buyer, conv, "Is it hand made?", about=str(self.product.id))
        self.assertTrue(r.json()["message"]["mine"])
        conv.refresh_from_db()
        self.assertEqual(conv.seller_unread, 1)
        self.assertTrue(Notification.objects.filter(user=self.seller.user, message__icontains="New message").exists())
        self.assertTrue(any("maker@example.com" in m.to for m in self.mail.outbox))
        # seller sees and answers
        self.client.force_login(self.seller.user)
        r = self.client.get(reverse("seller_conversation", args=[conv.id]))
        self.assertContains(r, "Is it hand made?")
        conv.refresh_from_db()
        self.assertEqual(conv.seller_unread, 0)
        self._send(self.seller.user, conv, "Yes, by hand!")
        conv.refresh_from_db()
        self.assertEqual(conv.buyer_unread, 1)
        self.client.force_login(self.buyer)
        first = Message.objects.order_by("id").first()
        data = self.client.get(reverse("message_poll", args=[conv.id]) + f"?after={first.id}").json()
        self.assertEqual([m["body"] for m in data["messages"]], ["Yes, by hand!"])
        self.assertFalse(data["messages"][0]["mine"])

    def test_outsiders_cannot_read_or_post(self):
        conv = Conversation.objects.create(buyer=self.buyer, seller=self.seller)
        other = User.objects.create_user("snoop", "s@example.com", "pass12345")
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("conversation", args=[conv.id])).status_code, 404)
        self.assertEqual(self.client.get(reverse("message_poll", args=[conv.id])).status_code, 404)
        self.assertEqual(self._send(other, conv, "hi").status_code, 404)

    def test_empty_and_rate_limited_messages(self):
        conv = Conversation.objects.create(buyer=self.buyer, seller=self.seller)
        self.assertEqual(self._send(self.buyer, conv, "   ").status_code, 400)
        cache.set(f"msg-rate:{self.buyer.pk}", 999, 60)
        self.assertEqual(self._send(self.buyer, conv, "hello").status_code, 400)
        cache.delete(f"msg-rate:{self.buyer.pk}")

    def test_report_reaches_admin(self):
        conv = Conversation.objects.create(buyer=self.buyer, seller=self.seller)
        self._send(self.buyer, conv, "Hello")
        self.client.post(reverse("message_report", args=[conv.id]))
        conv.refresh_from_db()
        self.assertTrue(conv.reported)
        admin = User.objects.create_superuser("mod", "mod@example.com", "pass12345")
        self.client.force_login(admin)
        self.assertContains(self.client.get(reverse("manage_conversations") + "?tab=reported"), "Reported")
        self.assertContains(self.client.get(reverse("manage_conversation", args=[conv.id])), "Hello")
        self.client.post(reverse("manage_conversation", args=[conv.id]), {"action": "resolve"})
        conv.refresh_from_db()
        self.assertFalse(conv.reported)

    def test_seller_cannot_message_own_store(self):
        self.client.force_login(self.seller.user)
        r = self.client.get(reverse("message_seller", args=[self.product.id]))
        self.assertRedirects(r, reverse("seller_messages"), fetch_redirect_response=False)
        self.assertFalse(Conversation.objects.exists())


class DealsAndOffersTests(TestCase):
    def test_flash_sale_ends(self):
        live = make_product(name="Live deal", is_flash_sale=True, flash_sale_ends=timezone.now() + timedelta(hours=3))
        make_product(name="Old deal", is_flash_sale=True, flash_sale_ends=timezone.now() - timedelta(minutes=1))
        names = list(Product.objects.flash().values_list("name", flat=True))
        self.assertEqual(names, ["Live deal"])
        r = self.client.get(reverse("product_detail", args=[live.id]))
        self.assertContains(r, "data-ends=")
        self.assertContains(r, "Deal ends in")

    def test_admin_sets_deal_end(self):
        admin = User.objects.create_superuser("deals", "d@example.com", "pass12345")
        p = make_product(name="Lamp")
        self.client.force_login(admin)
        self.client.post(reverse("manage_product_edit", args=[p.id]), {
            "name": "Lamp", "category": "skincare", "price": "10", "stock": "5", "discount_percent": "0",
            "image_url": "https://example.com/a.jpg", "is_flash_sale": "1", "flash_sale_ends": "2030-01-02T10:30",
            "approval_status": "approved", "bulk_min_qty": "3", "bulk_percent": "10"})
        p.refresh_from_db()
        self.assertEqual((p.flash_sale_ends.year, p.bulk_min_qty, p.bulk_percent), (2030, 3, 10))

    def test_quantity_offer_in_cart_and_order(self):
        self.client.force_login(User.objects.create_user("bulkbuyer", "bb@example.com", "pass12345"))
        p = make_product(name="Socks", price=Decimal("10.00"), stock=20, bulk_min_qty=3, bulk_percent=10)
        self.client.post(reverse("add_to_cart", args=[p.id]), {"quantity": 2})
        r = self.client.get(reverse("cart"))
        self.assertContains(r, "Buy 3 or more and save 10%")
        self.client.post(reverse("add_to_cart", args=[p.id]), {"quantity": 1})
        r = self.client.get(reverse("cart"))
        self.assertContains(r, "$27.00")
        self.assertContains(r, "Bundle discount applied")
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "cod"})
        item = OrderItem.objects.get(product=p)
        self.assertEqual((item.price, item.quantity), (Decimal("9.00"), 3))

    def test_offer_validation(self):
        owner = User.objects.create_user("offer", "o@example.com", "pass12345")
        seller = SellerAccount.objects.create(user=owner, business_name="Offer Co")
        seller.status = "approved"
        seller.save()
        self.client.force_login(owner)
        r = self.client.post(reverse("seller_add_product"), {"name": "X", "category": "skincare", "price": "5", "stock": "1",
                                                             "discount_percent": "0", "image_url": "https://example.com/x.jpg",
                                                             "bulk_min_qty": "3"}, follow=True)
        self.assertContains(r, "fill in both")


class ReferralProgramTests(TestCase):
    def setUp(self):
        self.friend_of = User.objects.create_user("sharer", "sharer@example.com", "pass12345")
        self.code = Profile.objects.get_or_create(user=self.friend_of)[0].referral_code
        site = SiteSettings.load()
        site.referral_friend_percent, site.referral_reward_percent = 15, 20
        site.save()

    def test_link_on_any_page_credits_signup(self):
        product = make_product(name="Shared thing")
        self.client.get(reverse("product_detail", args=[product.id]) + f"?ref={self.code}")
        self.client.post(reverse("signup"), {"username": "newbie", "email": "newbie@example.com",
                                             "password": "Str0ng-pass-123", "confirm_password": "Str0ng-pass-123",
                                             "password1": "Str0ng-pass-123", "password2": "Str0ng-pass-123"})
        newbie = User.objects.get(username="newbie")
        self.assertEqual(newbie.profile.referred_by, self.code)
        welcome = Coupon.objects.get(owner=newbie)
        self.assertEqual((welcome.purpose, welcome.percent_off), ("referral_welcome", 15))
        from .models import _reward_referrer_for
        _reward_referrer_for(newbie)
        reward = Coupon.objects.get(owner=self.friend_of)
        self.assertEqual((reward.purpose, reward.percent_off), ("referral_reward", 20))
        self.client.force_login(self.friend_of)
        r = self.client.get(reverse("referrals"))
        self.assertContains(r, reward.code)
        self.assertContains(r, "ne•••")
        self.assertContains(r, f"ref={self.code}")

    def test_admin_report_and_switch_off(self):
        admin = User.objects.create_superuser("radmin", "r@example.com", "pass12345")
        self.client.force_login(admin)
        self.assertContains(self.client.get(reverse("manage_referrals")), "Top referrers")
        SiteSettings.objects.filter(pk=1).update(referral_enabled=False)
        cache.clear()
        self.client.force_login(self.friend_of)
        self.assertRedirects(self.client.get(reverse("referrals")), reverse("profile"), fetch_redirect_response=False)


class ShippingAndTrackingTests(TestCase):
    def test_per_item_fee(self):
        from . import shipping
        ShippingZone.objects.create(name="US", countries="US", fee=Decimal("5"), per_item_fee=Decimal("2"))
        shipping.clear_cache()
        self.assertEqual(shipping.quote("US", Decimal("10"), 3)["fee"], Decimal("9"))
        self.assertEqual(shipping.quote("US", Decimal("10"))["fee"], Decimal("5"))

    def test_tracking_links(self):
        from .tracking import tracking_link
        self.assertIn("ups.com", tracking_link("ups", "1Z 999"))
        self.assertIn("1Z%20999", tracking_link("UPS", "1Z 999"))
        self.assertIn("17track", tracking_link("Some Local Courier", "AB12"))
        self.assertEqual(tracking_link("DHL", ""), "")
        self.assertEqual(tracking_link("DHL", "1", "https://my.courier/track/1"), "https://my.courier/track/1")

    def test_customer_sees_track_button(self):
        user = User.objects.create_user("trk", "trk@example.com", "pass12345")
        order = Order.objects.create(user=user, full_name="T", address="1", city="X", phone="1", country="US",
                                     status="shipped", courier_name="FedEx", tracking_number="123")
        self.client.force_login(user)
        self.assertContains(self.client.get(reverse("my_orders")), "fedex.com/fedextrack/?trknbr=123")
        admin = User.objects.create_superuser("tadmin", "t@example.com", "pass12345")
        self.client.force_login(admin)
        self.client.post(reverse("manage_order", args=[order.id]), {"action": "update", "status": "shipped", "courier_name": "Local",
                                                                   "tracking_number": "9", "tracking_url": "https://local.example/9"})
        order.refresh_from_db()
        self.assertEqual(order.tracking_link, "https://local.example/9")


class SellerBadgeTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user("badge", "badge@example.com", "pass12345")
        self.seller = SellerAccount.objects.create(user=owner, business_name="Badge Co")
        self.seller.status = "approved"
        self.seller.save()
        self.product = make_product(name="Pot", seller_account=self.seller, seller_name="Badge Co")

    def test_staff_verifies_seller(self):
        admin = User.objects.create_superuser("vadmin", "v@example.com", "pass12345")
        self.client.force_login(admin)
        self.client.post(reverse("manage_seller", args=[self.seller.id]), {"action": "verify"})
        self.seller.refresh_from_db()
        self.assertTrue(self.seller.is_verified)
        self.assertContains(self.client.get(reverse("product_detail", args=[self.product.id])), "Verified seller")

    @override_settings(TOP_SELLER_MIN_ORDERS=2, TOP_SELLER_MIN_REVIEWS=1, TOP_SELLER_MIN_RATING=4.5)
    def test_top_seller_badge_is_earned_and_lost(self):
        from . import badges
        buyer = User.objects.create_user("b2", "b2@example.com", "pass12345")
        for _ in range(2):
            o = Order.objects.create(user=buyer, full_name="B", address="1", city="X", phone="1", country="US", status="delivered")
            item = OrderItem(order=o, product=self.product, product_name="Pot", price=Decimal("10"), quantity=1)
            item.apply_commission(self.seller)
            item.save()
        Review.objects.create(product=self.product, user=buyer, username="b2", rating=5, comment="Great")
        self.assertEqual(badges.update_all(), (1, 0))
        self.seller.refresh_from_db()
        self.assertTrue(self.seller.top_seller)
        self.client.force_login(self.seller.user)
        self.assertContains(self.client.get(reverse("seller_dashboard")), "Top seller")
        Review.objects.create(product=self.product, user=buyer, username="b2", rating=1, comment="Broke")
        Review.objects.create(product=self.product, user=buyer, username="b2", rating=1, comment="Broke")
        self.assertEqual(badges.update_all(), (0, 1))


# ---------------------------------------------------------------------------
# Ultra pack: COD confirmation, shipments, refunds, guides, translation,
# system health
# ---------------------------------------------------------------------------

from .models import OrderEvent, UIText  # noqa: E402


class CodConfirmationTests(TestCase):
    real_cod_confirmation = True

    def setUp(self):
        from django.core import mail as _mail
        self.mail = _mail
        owner = User.objects.create_user("codseller", "cs@example.com", "pass12345")
        self.seller = SellerAccount.objects.create(user=owner, business_name="Cod Co")
        self.seller.status = "approved"
        self.seller.save()
        self.product = make_product(name="Tea", price=Decimal("10.00"), stock=5, seller_account=self.seller, seller_name="Cod Co")
        self.buyer = User.objects.create_user("codbuyer", "cb@example.com", "pass12345")
        self.client.force_login(self.buyer)

    def _order(self):
        self.client.post(reverse("add_to_cart", args=[self.product.id]))
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "cod"})
        return Order.objects.get()

    def test_unverified_buyer_confirms_by_email_then_seller_is_told(self):
        order = self._order()
        self.assertFalse(order.cod_confirmed)
        self.assertFalse(order.sellers_notified)
        self.assertTrue(any("confirm" in m.subject.lower() for m in self.mail.outbox))
        self.assertContains(self.client.get(reverse("my_orders")), "Please confirm this cash order")
        self.client.force_login(self.seller.user)
        self.client.post(reverse("seller_ship_order", args=[order.id]), {"status": "packed"})
        self.assertFalse(OrderItem.objects.filter(order=order, fulfillment_status="packed").exists())
        self.client.logout()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.get(reverse("confirm_cod", args=[order.id, order.access_token]))
        order.refresh_from_db()
        self.assertTrue(order.cod_confirmed and order.sellers_notified)
        self.assertTrue(Notification.objects.filter(user=self.seller.user, message__icontains="New order").exists())
        self.assertEqual(self.client.get(reverse("confirm_cod", args=[order.id, "wrong"])).status_code, 404)

    def test_verified_buyer_skips_confirmation(self):
        from .models import Profile
        Profile.objects.update_or_create(user=self.buyer, defaults={"email_verified": True})
        order = self._order()
        self.assertTrue(order.cod_confirmed)

    def test_unconfirmed_orders_cancel_after_48_hours(self):
        from django.core.management import call_command
        order = self._order()
        Order.objects.filter(pk=order.pk).update(created_at=timezone.now() - timedelta(hours=49))
        call_command("release_unpaid_orders", stdout=io.StringIO())
        order.refresh_from_db()
        self.assertEqual((order.status, order.cancel_reason), ("cancelled", "not confirmed in time"))
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)

    def test_delivery_marks_cash_collected(self):
        from .models import Profile
        Profile.objects.update_or_create(user=self.buyer, defaults={"email_verified": True})
        order = self._order()
        order.status = "delivered"
        order.save()
        self.assertIsNotNone(order.cod_collected_at)
        self.assertTrue(OrderEvent.objects.filter(order=order, kind="cod").exists())


class ShipmentAndTrackingPageTests(TestCase):
    def test_every_order_gets_unique_shipment_number_and_courier(self):
        a = Order.objects.create(full_name="A", address="1", city="Lahore", phone="1", country="PK")
        b = Order.objects.create(full_name="B", address="1", city="Paris", phone="1", country="FR")
        self.assertRegex(a.shipment_number, r"^[A-Z]{2,4}-\d{4}-[2-9A-Z]{6}$")
        self.assertNotEqual(a.shipment_number, b.shipment_number)
        from .tracking import suggest_courier
        self.assertEqual((suggest_courier("PK"), suggest_courier("FR"), suggest_courier("BR")), ("TCS", "DPD", "DHL"))
        ShippingZone.objects.create(name="Pakistan", countries="PK", fee=Decimal("2"), courier="Leopards")
        self.assertEqual(suggest_courier("PK"), "Leopards")

    def test_checkout_picks_courier_and_public_tracking_page(self):
        user = User.objects.create_user("ship", "ship@example.com", "pass12345")
        self.client.force_login(user)
        p = make_product(name="Mat", price=Decimal("10"), stock=3)
        self.client.post(reverse("add_to_cart", args=[p.id]))
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("checkout"), {**CHECKOUT_FORM, "payment_method": "cod"})
        order = Order.objects.get()
        self.assertEqual(order.courier_name, "USPS")
        order.status = "shipped"
        order.save()
        self.assertTrue(order.uses_store_tracking)
        self.client.logout()
        r = self.client.get(reverse("track_shipment", args=[order.shipment_number]))
        self.assertContains(r, order.shipment_number)
        self.assertContains(r, "Handed to USPS")
        self.assertNotContains(r, "1 Main St")  # no street address on the public page
        r = self.client.get(reverse("track_lookup") + f"?n={order.shipment_number.lower()}")
        self.assertRedirects(r, reverse("track_shipment", args=[order.shipment_number]), fetch_redirect_response=False)


@override_settings(STRIPE_SECRET_KEY="sk_test_dummy")
class RefundRecordTests(TestCase):
    def test_cancel_paid_order_refunds_to_card_with_reason(self):
        user = User.objects.create_user("refundme", "r@example.com", "pass12345")
        p = make_product(name="Cup", price=Decimal("12"), stock=4)
        order = Order.objects.create(user=user, full_name="R", address="1", city="X", phone="1", country="US",
                                     payment_method="card", payment_status="paid", status="confirmed",
                                     stripe_payment_intent="pi_123", card_brand="visa", card_last4="4242")
        OrderItem.objects.create(order=order, product=p, product_name="Cup", price=Decimal("12"), quantity=1)
        self.client.force_login(user)
        refund = mock.Mock(id="re_999")
        with mock.patch("stripe.Refund.create", return_value=refund) as create:
            r = self.client.post(reverse("cancel_order", args=[order.id]), {"reason": "Changed my mind"}, follow=True)
        create.assert_called_once()
        order.refresh_from_db()
        self.assertEqual((order.payment_status, order.refund_id, order.cancel_reason), ("refunded", "re_999", "Changed my mind"))
        self.assertContains(r, "Visa •••• 4242")
        self.assertTrue(OrderEvent.objects.filter(order=order, kind="refunded").exists())

    def test_card_details_saved_after_payment(self):
        from . import payments
        order = Order.objects.create(full_name="R", address="1", city="X", phone="1", stripe_payment_intent="pi_1")
        intent = {"latest_charge": {"payment_method_details": {"card": {"brand": "mastercard", "last4": "4444"}}}}
        with self.settings(STRIPE_CARD_DETAILS=True), mock.patch("stripe.PaymentIntent.retrieve", return_value=intent):
            payments.remember_card(order)
        order.refresh_from_db()
        self.assertEqual(order.card_label, "Mastercard •••• 4444")


class BuyingGuideTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user("guide", "g@example.com", "pass12345")
        self.seller = SellerAccount.objects.create(user=owner, business_name="Guide Co")
        self.seller.status = "approved"
        self.seller.save()
        self.client.force_login(owner)

    def test_seller_draft_and_save_guide(self):
        r = self.client.post(reverse("ai_buying_guide_draft"), {"name": "Yoga Mat", "category": "fashion"})
        self.assertEqual(r.json()["source"], "builtin")
        self.assertTrue(r.json()["guide"]["pros"])
        self.client.post(reverse("seller_add_product"), {"name": "Yoga Mat", "category": "fashion", "price": "20", "stock": "3",
                                                         "discount_percent": "0", "image_url": "https://example.com/m.jpg",
                                                         "guide_uses": "Yoga\\nPilates", "guide_pros": "Non-slip", "guide_cons": "Heavy"})
        p = Product.objects.get(name="Yoga Mat")
        p.approval_status = "approved"
        p.save()
        self.assertEqual(p.guide["pros"], ["Non-slip"])
        r = self.client.get(reverse("product_detail", args=[p.id]))
        self.assertContains(r, "Buying guide")
        self.assertContains(r, "Non-slip")
        self.assertContains(r, "message-seller" if False else "Message seller")

    def test_shopper_ai_guide_and_question(self):
        p = make_product(name="Kettle", seller_account=self.seller, seller_name="Guide Co")
        reply = mock.MagicMock()
        reply.__enter__.return_value.read.return_value = json.dumps({"content": [{"type": "text", "text":
            '{"uses": ["Boiling water fast"], "pros": ["Quick", "Easy"], "cons": ["Check the voltage"]}'}]}).encode()
        with self.settings(AI_API_KEY="k"), mock.patch("urllib.request.urlopen", return_value=reply):
            r = self.client.post(reverse("ai_product_guide", args=[p.id]))
        self.assertEqual(r.json()["guide"]["pros"], ["Quick", "Easy"])
        p.refresh_from_db()
        self.assertEqual(p.guide_source, "ai")
        answer = mock.MagicMock()
        answer.__enter__.return_value.read.return_value = json.dumps({"content": [{"type": "text", "text": "Yes, it suits travel."}]}).encode()
        with self.settings(AI_API_KEY="k"), mock.patch("urllib.request.urlopen", return_value=answer):
            r = self.client.post(reverse("ai_product_question", args=[p.id]), {"question": "Good for travel?"})
        self.assertEqual(r.json()["answer"], "Yes, it suits travel.")
        self.assertEqual(self.client.post(reverse("ai_product_question", args=[p.id]), {"question": "Hi"}).status_code, 400)


class WholePageTranslationTests(TestCase):
    def test_pages_translate_and_user_content_does_not(self):
        p = make_product(name="Plain Mug", description="Add to cart this mug")
        s = self.client.session
        s["site_lang"] = "fr"
        s.save()
        r = self.client.get(reverse("product_detail", args=[p.id]))
        html = r.content.decode()
        self.assertIn("Tous les produits", html)       # header (built-in pack)
        self.assertIn("Add to cart this mug", html)   # seller's description untouched
        r = self.client.get(reverse("help_support"))
        self.assertIn("Comment passer commande ?", r.content.decode())
        s = self.client.session
        s["site_lang"] = "ar"
        s.save()
        self.assertIn("قارن", self.client.get(reverse("all_products")).content.decode())

    def test_owner_translation_overrides_and_admin_page(self):
        from . import i18n
        UIText.objects.create(source="Plain Mug", fr="Tasse simple")
        i18n.clear_cache()
        p = make_product(name="Plain Mug")
        s = self.client.session
        s["site_lang"] = "fr"
        s.save()
        self.assertIn("Tasse simple", self.client.get(reverse("product_detail", args=[p.id])).content.decode())
        admin = User.objects.create_superuser("tr", "tr@example.com", "pass12345")
        self.client.force_login(admin)
        r = self.client.get(reverse("manage_translations") + "?lang=fr&show=all")
        self.assertContains(r, "Plain Mug")
        row = UIText.objects.get(source="Plain Mug")
        self.client.post(reverse("manage_translations"), {"action": "save", "id": row.id, "lang": "fr", "text": "Mug uni"})
        row.refresh_from_db()
        self.assertEqual(row.fr, "Mug uni")
        reply = mock.MagicMock()
        UIText.objects.create(source="Brand new text")
        reply.__enter__.return_value.read.return_value = json.dumps({"content": [{"type": "text", "text": '["Texte tout neuf"]'}]}).encode()
        with self.settings(AI_API_KEY="k"), mock.patch("urllib.request.urlopen", return_value=reply):
            self.client.post(reverse("manage_translations"), {"action": "ai", "lang": "fr"})
        self.assertEqual(UIText.objects.get(source="Brand new text").fr, "Texte tout neuf")

    def test_keep_english_clears_the_system_check_warning(self):
        from .manage_views import operations_checks
        from .i18n_pack import PACK
        builtin = next(iter(PACK))
        UIText.objects.create(source=builtin)  # built-in texts never count as missing
        UIText.objects.create(source="Zuma Lamp")
        UIText.objects.create(source="Kora Vase", fr="Vase Kora")

        def check():
            return next(c for c in operations_checks() if c["name"] == "Untranslated shop text")
        self.assertEqual(check()["level"], "warn")
        self.assertIn("French 1", check()["detail"])
        self.assertNotIn(builtin, str(check()))
        admin = User.objects.create_superuser("kp", "kp@example.com", "pass12345")
        self.client.force_login(admin)
        row = UIText.objects.get(source="Zuma Lamp")
        self.client.post(reverse("manage_translations"), {"action": "keep", "id": row.id, "lang": "fr"})
        row.refresh_from_db()
        self.assertEqual(row.fr, "Zuma Lamp")
        for code in ("ur", "roman", "ar", "es"):
            self.client.post(reverse("manage_translations"), {"action": "keep_all", "lang": code})
        self.assertEqual(UIText.objects.get(source=builtin).ar, "")
        self.assertEqual(check()["level"], "ok")

    def test_placeholders_dates_and_store_name(self):
        from .i18n import translate_html
        out = translate_html("<p>Placed Oct 8, 2026</p><p>6 items</p><h2>Sell on Acme</h2>", "es", "Acme")
        self.assertIn("Realizado el oct 8, 2026", out)
        self.assertIn("6 artículos", out)
        self.assertIn("Vende en Acme", out)


class SystemHealthTests(TestCase):
    def test_system_check_lists_operations_and_dashboard_warns(self):
        admin = User.objects.create_superuser("health", "h@example.com", "pass12345")
        self.client.force_login(admin)
        from .alerts import SERVER_ERROR, log
        log(f"{SERVER_ERROR}: GET /boom/ - ValueError: bad")
        r = self.client.get(reverse("manage_system"))
        self.assertContains(r, "Page crashes (24 hours)")
        self.assertContains(r, "Daily tasks")
        self.assertContains(r, "/boom/")
        cache.clear()
        self.assertContains(self.client.get(reverse("manage_dashboard")), "Open System check")
