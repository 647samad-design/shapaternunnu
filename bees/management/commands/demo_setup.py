"""Prepares a demo copy of the store: three demo accounts (shopper, seller,
store admin) with sample products and orders, so visitors can try every
part of the marketplace with one click from the sign-in page.

    python manage.py demo_setup            # create / refresh the demo
    python manage.py demo_setup --reset    # also put the demo accounts back
                                           # the way they started (run daily)

Only use this on a separate demo copy, with DEMO_MODE=True in .env.
"""
import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from bees.models import (
    Order, OrderItem, Product, Profile, SellerAccount, SellerPlan, SiteSettings,
)

DEMO_USERS = {
    "shopper": {"username": "demo_shopper", "first_name": "Sam", "last_name": "Shopper", "email": "shopper@demo.invalid"},
    "seller": {"username": "demo_seller", "first_name": "Sara", "last_name": "Seller", "email": "seller@demo.invalid"},
    "admin": {"username": "demo_admin", "first_name": "Alex", "last_name": "Admin", "email": "admin@demo.invalid"},
}
DEMO_USERNAMES = {u["username"] for u in DEMO_USERS.values()}
STORE_NAME = "Demo Studio"

SELLER_PRODUCTS = [
    ("Handmade Ceramic Mug", "casserole-pot", "18.00", "Wheel-thrown stoneware\n350 ml\nDishwasher safe"),
    ("Linen Tote Bag", "fashion", "24.00", "Natural linen\nInner pocket\nFits a 13-inch laptop"),
    ("Soy Wax Candle - Cedar", "table-lamp", "21.50", "100% soy wax\nAbout 40 hours burn time\nCotton wick"),
    ("Kids Watercolor Starter Kit", "coloring-drawing", "16.00", "12 colours\n2 brushes\nNon-toxic paints"),
    ("Merino Wool Beanie", "fashion", "29.00", "Soft merino wool\nOne size\nHand wash"),
]


class Command(BaseCommand):
    help = "Create (or reset) the demo accounts, products and orders."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Put the demo accounts back to how they started.")

    def handle(self, *args, **options):
        if not Product.objects.exists():
            call_command("seed_data", stdout=self.stdout)
        with transaction.atomic():
            users = {role: self._user(data, staff=(role == "admin")) for role, data in DEMO_USERS.items()}
            seller = self._seller(users["seller"])
            products = self._products(seller)
            if options["reset"] or not Order.objects.filter(user=users["shopper"]).exists():
                self._orders(users["shopper"], products)
            self._showcase(users, seller, products, reset=options["reset"])
            self._marketplace(users, seller, products, reset=options["reset"])
            self._shopper_pack(users, seller, products, reset=options["reset"])
            self._ops_pack(users, seller, products)
            self._power_pack(users, seller, products, reset=options["reset"])
        try:
            from bees.trustcheck import run_checks
            run_checks()
        except Exception:
            pass
        site = SiteSettings.load()
        site.setup_completed = True
        site.show_language_menu = True  # show off languages and currencies in the demo
        site.save()
        from bees.models import Currency
        Currency.objects.filter(code__in=["EUR", "GBP", "AED"]).update(active=True)
        from django.core.cache import cache
        cache.delete("currencies:v1")
        self.stdout.write(self.style.SUCCESS(
            "Demo ready: demo_shopper, demo_seller and demo_admin. Turn on DEMO_MODE=True to show the one-click logins."))

    def _user(self, data, staff=False):
        user, _ = User.objects.get_or_create(username=data["username"])
        user.first_name, user.last_name, user.email = data["first_name"], data["last_name"], data["email"]
        user.is_active = True
        user.is_staff = user.is_superuser = staff
        user.set_unusable_password()  # demo accounts only open through the demo buttons
        user.save()
        profile, _ = Profile.objects.get_or_create(user=user)
        profile.email_verified = True
        profile.totp_enabled = False
        profile.totp_secret = ""
        profile.backup_codes = []
        profile.save()
        return user

    def _seller(self, user):
        seller = SellerAccount.objects.filter(user=user).first() or SellerAccount(user=user, account_type="individual")
        seller.status = "approved"
        seller.full_name = "Sara Seller"
        seller.business_name = STORE_NAME
        seller.city, seller.country = "Lisbon", "Portugal"
        seller.store_description = "Small-batch homeware and gifts, made by hand and shipped worldwide."
        seller.vacation_mode = False
        seller.verified_at = seller.verified_at or timezone.now()
        pro = SellerPlan.objects.filter(active=True, price__gt=0).order_by("price").first()
        if pro:
            seller.plan, seller.plan_expires_at = pro, timezone.now() + timedelta(days=365)
        seller.save()
        return seller

    def _products(self, seller):
        from bees.management.commands.seed_data import _demo_image
        out = []
        for name, category, price, notes in SELLER_PRODUCTS:
            product = Product.objects.filter(seller_account=seller, name=name).first()
            if not product:
                from bees.ai import builtin_listing
                product = Product.objects.create(
                    name=name, category=category, price=Decimal(price), stock=random.Random(name).randint(3, 40),
                    description=builtin_listing(name, category, notes), image_url=_demo_image(name, category),
                    seller_account=seller, seller_name=seller.display_name, approval_status="approved",
                )
            elif product.approval_status != "approved":
                product.approval_status = "approved"
                product.save(update_fields=["approval_status"])
            out.append(product)
        # Show off a running deal with a countdown and a quantity offer.
        Product.objects.filter(pk=out[1].pk).update(
            is_flash_sale=True, flash_sale_ends=timezone.now() + timedelta(days=2, hours=5), old_price=Decimal("32.00"))
        Product.objects.filter(pk=out[0].pk).update(bulk_min_qty=2, bulk_percent=10)
        return out

    def _showcase(self, users, seller, products, reset=False):
        """Brands, specifications, EU safety details, bulk quotes, a home
        banner and a store page, so every feature has something to show."""
        from bees.models import HomeBanner, Page, QuoteRequest
        details = [
            ("Demo Studio", "Material: Stoneware\nCapacity: 350 ml\nDishwasher safe: Yes\nMade in: Portugal", True),
            ("Demo Studio", "Material: 100% linen\nSize: 38 x 42 cm\nPocket: Inside zip pocket", False),
            ("Lumen Home", "Wax: 100% soy\nBurn time: About 40 hours\nScent: Cedar & amber", True),
            ("Little Artists", "Colours: 12\nBrushes: 2\nAge: 4+", False),
            ("Demo Studio", "Material: Merino wool\nSize: One size\nCare: Hand wash cold", False),
        ]
        for product, (brand, specs, quotes) in zip(products, details):
            Product.objects.filter(pk=product.pk).update(
                brand=brand, specs=specs, quote_only=quotes,
                manufacturer=f"{STORE_NAME}, Rua das Flores 12, 1200-195 Lisbon, Portugal - hello@demo.invalid",
                safety_info="Keep away from children under 3." if "Kit" in product.name else "")
        if not HomeBanner.objects.exists():
            HomeBanner.objects.create(title="New from independent makers", subtitle="Handmade mugs, candles and linen - straight from the studio.",
                                      link=f"/store/{seller.display_name}/", button_label="Visit the studio", position=0)
            HomeBanner.objects.create(title="Buying for your business?", subtitle="Request a quote on bulk orders and get a better price.",
                                      link="/search/?q=mug", button_label="See bulk deals", position=1)
        Page.objects.get_or_create(slug="shipping-policy", defaults={
            "title": "Shipping policy", "show_in_footer": True,
            "body": "## Delivery times\nMost orders arrive in 3-7 working days. You'll get a tracking link by email as soon as your parcel ships.\n\n"
                    "## Costs\n- Free shipping on orders over the free-shipping amount\n- Otherwise a flat fee, shown at checkout\n\n"
                    "## International orders\nImport duties may apply depending on your country."})
        if reset or not QuoteRequest.objects.filter(buyer=users["shopper"]).exists():
            QuoteRequest.objects.filter(buyer=users["shopper"]).delete()
            mug, candle = products[0], products[2]
            open_q = QuoteRequest(product=candle, buyer=users["shopper"], seller_account=seller, quantity=120,
                                  target_price=Decimal("15.00"), company="Hotel Aurora", message="Gift candles for our guests, delivery in March.")
            open_q.log("buyer", "Asked for 120 pcs at 15.00 each: Gift candles for our guests, delivery in March.")
            open_q.save()
            offered = QuoteRequest(product=mug, buyer=users["shopper"], seller_account=seller, quantity=60, target_price=Decimal("13.00"),
                                   company="Café Lumen", status="offered", offer_price=Decimal("14.50"),
                                   offer_note="Includes your logo glazed on the side. Ships in 3 weeks.",
                                   offer_expires_at=timezone.now() + timedelta(days=6))
            offered.log("buyer", "Asked for 60 pcs at 13.00 each")
            offered.log("seller", "Offered 14.50 each, valid 6 days - Includes your logo glazed on the side. Ships in 3 weeks.")
            offered.save()

    def _marketplace(self, users, seller, products, reset=False):
        """Auction, offers, other sellers, personalization, Subscribe & Save,
        vouchers, collection points, membership and coins in the demo."""
        from bees.models import Coupon, PickupPoint, Profile
        site = SiteSettings.load()
        site.membership_enabled = True
        site.membership_discount = 5
        site.bnpl_enabled = True
        site.affiliate_percent = 5
        site.save()
        mug, tote, candle, kit, beanie = products
        Product.objects.filter(pk=mug.pk).update(personalization_prompt="Name to glaze on the mug (max 12 letters)", personalization_max=12,
                                                 accepts_offers=True, offer_floor=Decimal("12.00"))
        Product.objects.filter(pk=candle.pk).update(subscribe_percent=10)
        Product.objects.filter(pk=beanie.pk).update(accepts_offers=True, ships_from="PT")
        # An auction that's always running in the demo.
        auction = Product.objects.filter(seller_account=seller, name="Vintage Film Camera").first()
        if auction is None or reset or auction.auction_closed:
            from bees.management.commands.seed_data import _demo_image
            if auction is None:
                auction = Product(name="Vintage Film Camera", category="electronics", seller_account=seller, seller_name=seller.display_name,
                                  image_url=_demo_image("Vintage Film Camera", "electronics"), approval_status="approved")
            auction.price, auction.stock, auction.condition = Decimal("80.00"), 1, "used_good"
            auction.description = "A working 35 mm film camera from the 1980s with a 50 mm lens. Light seals replaced, shutter tested."
            auction.is_auction, auction.auction_closed = True, False
            auction.auction_start_price, auction.auction_reserve = Decimal("40.00"), Decimal("60.00")
            auction.auction_ends_at = timezone.now() + timedelta(days=3, hours=4)
            auction.save()
            auction.bids.all().delete()
        # Another seller offering the same linen tote (used), under "Other sellers".
        other = User.objects.filter(username="demo_market_seller").first()
        if other is None:
            other = User.objects.create(username="demo_market_seller", first_name="Marta", email="marta@demo.invalid")
            other.set_unusable_password()
            other.save()
        second = SellerAccount.objects.filter(user=other).first() or SellerAccount(user=other)
        second.status, second.business_name, second.city, second.country = "approved", "Second Hand Lisboa", "Lisbon", "Portugal"
        second.account_type, second.trader_email, second.trader_declared_at = "individual", "marta@demo.invalid", timezone.now()
        second.save()
        if not Product.objects.filter(seller_account=second, offer_of=tote).exists():
            Product.objects.create(name=tote.name, category=tote.category, image_url=tote.image_url, description=tote.description,
                                   price=Decimal("15.00"), stock=2, condition="used_like_new", seller_account=second,
                                   seller_name=second.display_name, approval_status="approved", offer_of=tote)
        seller.trader_email = seller.trader_email or "hello@demo.invalid"
        seller.company_registration = seller.company_registration or "PT 509 123 456"
        seller.trader_declared_at = seller.trader_declared_at or timezone.now()
        seller.save(update_fields=["trader_email", "company_registration", "trader_declared_at"])
        for code, pct, free, title, owner in [("WELCOME10", 10, False, "New here? 10% off", None),
                                              ("FREESHIP", 0, True, "Free delivery weekend", None),
                                              ("STUDIO15", 15, False, "Demo Studio favourites", seller)]:
            Coupon.objects.update_or_create(code=code, defaults={"percent_off": pct, "free_shipping": free, "title": title,
                                                                 "is_public": True, "active": True, "seller_account": owner,
                                                                 "per_user_limit": 1})
        for name, address, city, country, fee in [("Lumen Locker - Baixa", "Rua Augusta 100", "Lisbon", "PT", "0"),
                                                  ("Corner Shop Pickup", "Calle Mayor 12", "Madrid", "ES", "1.50"),
                                                  ("Station Locker", "Hauptbahnhof 1", "Berlin", "DE", "1.00")]:
            PickupPoint.objects.get_or_create(name=name, defaults={"address": address, "city": city, "country": country,
                                                                   "fee": Decimal(fee), "hours": "Every day 7:00-22:00"})
        Profile.objects.filter(user=users["shopper"]).update(loyalty_points=120)

    def _shopper_pack(self, users, seller, products, reset=False):
        """Group buy, pre-order, prize wheel, live show, blog post and a gift
        registry in the demo."""
        from bees.models import BlogPost, GiftRegistry, LiveShow, RegistryItem
        from bees.shopper import new_registry_token
        site = SiteSettings.load()
        site.spin_enabled = True
        site.price_protection_days = site.price_protection_days or 14
        site.save(update_fields=["spin_enabled", "price_protection_days"])
        mug, tote, candle, kit, beanie = products
        from django.templatetags.static import static
        try:
            model_url = static("bees/demo/mug.glb")
        except ValueError:  # collectstatic hasn't run yet
            model_url = "/static/bees/demo/mug.glb"
        Product.objects.filter(pk=mug.pk).update(model_3d_url=model_url)
        Product.objects.filter(pk=kit.pk).update(group_size=2, group_percent=15)
        Product.objects.filter(pk=beanie.pk).update(preorder_ships_on=timezone.localdate() + timedelta(days=21))
        if reset or not LiveShow.objects.filter(seller_account=seller).exists():
            LiveShow.objects.filter(seller_account=seller).delete()
            show = LiveShow.objects.create(seller_account=seller, title="Studio live: how we glaze our mugs",
                                           description="Watch a mug go from clay to glaze, ask anything in the chat and get the pieces shown at the studio price.",
                                           starts_at=timezone.now() + timedelta(days=2, hours=3))
            show.products.set([mug, candle, kit])
        if not BlogPost.objects.exists():
            post = BlogPost.objects.create(
                title="5 ways to make a small home feel calm", slug="calm-small-home", is_published=True, published_at=timezone.now(),
                excerpt="Simple, affordable changes: light, texture and a few things you really love.",
                body="A calm home isn't about owning less. It's about choosing what stays.\n\n## Start with light\n\n"
                     "Warm, low light in the evening changes a room more than any new furniture. A candle or a small lamp is enough.\n\n"
                     "## Add natural textures\n\n- Linen and cotton for things you touch every day\n- Stoneware instead of plastic\n- One plant per room\n\n"
                     "## Keep surfaces clear\n\nGive every everyday object a home: a tray for keys, a basket for blankets, a shelf for books.")
            post.products.set([candle, mug, tote])
        shopper = users["shopper"]
        if not GiftRegistry.objects.filter(owner=shopper).exists():
            reg = GiftRegistry.objects.create(owner=shopper, title="Sam & Alex's housewarming", event="housewarming",
                                              event_date=timezone.localdate() + timedelta(days=40), token=new_registry_token(),
                                              message="Thank you for celebrating our first home with us!")
            for product, wanted in ((mug, 4), (candle, 2), (tote, 1)):
                RegistryItem.objects.create(registry=reg, product=product, wanted=wanted)

    def _ops_pack(self, users, seller, products):
        """A business account for the demo shopper and two stock locations
        for the demo seller."""
        from decimal import Decimal as D
        from bees.models import Company, CompanyMember, StockLevel, Warehouse
        from bees.ops import sync_total
        shopper = users["shopper"]
        if not CompanyMember.objects.filter(user=shopper).exists():
            company = Company.objects.create(name="Sam's Design Studio Ltd", billing_email=shopper.email, tax_id="GB123456789",
                                             country="GB", status="approved", payment_terms=30, credit_limit=D("2000"))
            CompanyMember.objects.create(company=company, user=shopper, role="admin")
        if not Warehouse.objects.filter(seller_account=seller).exists():
            lisbon = Warehouse.objects.create(seller_account=seller, name="Lisbon studio", country="PT", city="Lisbon", priority=1)
            berlin = Warehouse.objects.create(seller_account=seller, name="Berlin warehouse", country="DE", city="Berlin", priority=2)
            mug, tote, candle, kit, beanie = products
            for product, a, b in ((mug, 25, 15), (candle, 10, 20)):
                if product.has_variants:
                    continue
                StockLevel.objects.create(warehouse=lisbon, product=product, quantity=a)
                StockLevel.objects.create(warehouse=berlin, product=product, quantity=b)
                sync_total(product.pk)

    def _power_pack(self, users, seller, products, reset=False):
        """Pakistan payments, a cash order with a map pin and doorstep code,
        a transfer to check, a running price test, a repricer rule and a
        copied listing for Trust & safety."""
        from decimal import Decimal as D
        from bees.models import DailyStat, PriceTest, PriceTestBlock, RepriceRule, SellerAccount
        mug, tote, candle, kit, beanie = products
        shopper = users["shopper"]
        site = SiteSettings.load()
        site.transfer_enabled = True
        site.transfer_accounts = site.transfer_accounts or ("Easypaisa: 0345 0000000 (demo account - don't send money)\n"
                                                            "Meezan Bank (Raast): PK00 MEZN 0000 0000 0000 0000 (demo)")
        site.delivery_codes = True
        site.save(update_fields=["transfer_enabled", "transfer_accounts", "delivery_codes"])
        SellerAccount.objects.filter(pk=seller.pk, pickup_latitude__isnull=True).update(pickup_latitude=D("38.711200"), pickup_longitude=D("-9.139800"))
        now = timezone.now()
        if reset or not Order.objects.filter(user=shopper, payment_method="cod").exists():
            cod = Order.objects.create(user=shopper, email=shopper.email, full_name="Sam Shopper", address="House 14, Street 7, Gulberg III",
                                       city="Lahore", country="PK", phone="+923000000000", payment_method="cod", payment_status="not_applicable",
                                       status="confirmed", cod_confirmed=True, sellers_notified=True,
                                       latitude=D("31.511996"), longitude=D("74.343584"))
            item = OrderItem(order=cod, product=beanie, product_name=beanie.name, price=beanie.price, quantity=1)
            item.apply_commission(beanie.seller_account)
            item.save()
            from bees.delivery_code import assign
            assign(cod)
            transfer = Order.objects.create(user=shopper, email=shopper.email, full_name="Sam Shopper", address="House 14, Street 7, Gulberg III",
                                            city="Lahore", country="PK", phone="+923000000000", payment_method="transfer", payment_status="pending",
                                            status="pending", cod_confirmed=True, transfer_account="Easypaisa: 0345 0000000",
                                            transfer_reference="DEMO48213377", transfer_submitted_at=now)
            item = OrderItem(order=transfer, product=candle, product_name=candle.name, price=candle.price, quantity=2)
            item.apply_commission(candle.seller_account)
            item.save()
        rule, _ = RepriceRule.objects.update_or_create(product=tote, defaults={"min_price": (tote.price * D("0.8")).quantize(D("0.01")),
                                                                               "max_price": tote.price, "mode": "beat", "step": D("0.50"), "active": True})
        from bees.repricer import apply_rule
        apply_rule(rule)
        if reset:
            PriceTest.objects.filter(product=kit).delete()
        if not PriceTest.objects.filter(product=kit).exists():
            b_price = (kit.price * D("0.85")).quantize(D("0.01"))
            test = PriceTest.objects.create(product=kit, price_a=kit.price, price_b=b_price, days=14, current_arm="b")
            PriceTest.objects.filter(pk=test.pk).update(started_at=now - timedelta(days=6))
            for i in range(6):
                arm = "a" if i % 2 == 0 else "b"
                PriceTestBlock.objects.create(test=test, arm=arm, start=now - timedelta(days=6 - i), end=None if i == 5 else now - timedelta(days=5 - i))
                day = timezone.localdate() - timedelta(days=5 - i)
                stat, _ = DailyStat.objects.get_or_create(date=day, kind="view", key=str(kit.pk), defaults={"count": 0})
                DailyStat.objects.filter(pk=stat.pk).update(count=stat.count + (34 if arm == "a" else 41))
            Product.objects.filter(pk=kit.pk).update(price=b_price)
        # A listing that reuses another seller's photo (waiting for approval, never shown in the shop).
        second = SellerAccount.objects.filter(business_name="Second Hand Lisboa").first()
        if second and not Product.objects.filter(seller_account=second, name="Ceramic Coffee Mug").exists():
            Product.objects.create(name="Ceramic Coffee Mug", category=mug.category, image_url=mug.image_url, price=D("9.99"), stock=50,
                                   seller_account=second, seller_name=second.display_name, approval_status="pending",
                                   description="Coffee mug.")

    def _orders(self, shopper, products):
        Order.objects.filter(user=shopper).delete()
        rng = random.Random(7)
        now = timezone.now()
        plan = [(18, "delivered"), (14, "delivered"), (11, "delivered"), (8, "shipped"), (5, "shipped"),
                (3, "confirmed"), (1, "confirmed"), (0, "confirmed")]
        from bees.models import OrderEvent
        from bees.tracking import suggest_courier
        for days_ago, status in plan:
            country = rng.choice(["GB", "DE", "CA", "AE", "US", "PK"])
            order = Order.objects.create(
                user=shopper, email=shopper.email, full_name="Sam Shopper", address="12 Demo Street",
                city={"GB": "London", "DE": "Berlin", "CA": "Toronto", "AE": "Dubai", "US": "New York", "PK": "Lahore"}[country],
                country=country, courier_name=suggest_courier(country),
                phone="+10000000000", payment_method="card", payment_status="paid", status=status,
                sellers_notified=True,
            )
            items_made = []
            for product in rng.sample(products, rng.randint(1, 2)):
                item = OrderItem(order=order, product=product, product_name=product.name, price=product.price,
                                 quantity=rng.randint(1, 3))
                item.apply_commission(product.seller_account)
                item.fulfillment_status = {"delivered": "delivered", "shipped": "handed_to_courier"}.get(status, "pending")
                item.save()
                items_made.append(item)
            created = now - timedelta(days=days_ago, hours=rng.randint(0, 10))
            for item in items_made:
                if item.fulfillment_status != "pending":
                    OrderItem.objects.filter(pk=item.pk).update(
                        shipped_at=created + timedelta(days=1, hours=2),
                        delivered_at=created + timedelta(days=4) if status == "delivered" else None)
            Order.objects.filter(pk=order.pk).update(
                created_at=created, delivered_at=created + timedelta(days=4) if status == "delivered" else None,
                card_brand="visa", card_last4="4242")
            steps = [("placed", "Order placed", 0), ("paid", "Payment received by card", 0.01)]
            if status in ("confirmed", "shipped", "delivered"):
                steps.append(("confirmed", "Order confirmed by the seller", 0.2))
            if status in ("shipped", "delivered"):
                steps += [("packed", "Packed and ready for the courier", 0.8), ("shipped", f"Handed to {order.courier_name}", 1.1)]
            if status == "delivered":
                steps.append(("delivered", "Delivered", 4))
            OrderEvent.objects.filter(order=order).delete()
            for kind, text, after in steps:
                ev = OrderEvent.objects.create(order=order, kind=kind, text=text)
                OrderEvent.objects.filter(pk=ev.pk).update(created_at=created + timedelta(days=after))
