"""Scrubs personal data in a TEST copy of the store (after copying the live
database there). Refuses to run unless STAGING=True.

    python manage.py anonymize_data --yes
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Replace customers' personal data with fake data (test site only)."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Really do it.")

    def handle(self, *args, **options):
        if not getattr(settings, "STAGING", False):
            raise CommandError("This only runs on a test copy: set STAGING=True in that copy's .env first. Never run it on the live store.")
        if not options["yes"]:
            raise CommandError("Add --yes to confirm. Personal data in THIS database will be replaced.")
        from django.contrib.auth.models import User
        from django.contrib.sessions.models import Session
        from bees import models as m
        n = 0
        for u in User.objects.all().iterator():
            u.email = f"user{u.pk}@example.test"
            if not u.is_superuser:
                u.first_name, u.last_name = "Test", f"User {u.pk}"
                u.set_unusable_password()
            u.save()
            n += 1
        for o in m.Order.objects.all().iterator():
            m.Order.objects.filter(pk=o.pk).update(
                full_name=f"Test Customer {o.pk}", email=f"order{o.pk}@example.test" if o.email else "",
                guest_email=f"order{o.pk}@example.test" if o.guest_email else "", phone="+15550100",
                address=f"{o.pk} Test Street", customer_tax_id="", cod_otp_hash="")
        m.Address.objects.update(full_name="Test Customer", phone="+15550100", address="1 Test Street")
        m.SellerAccount.objects.update(phone="+15550100", bank_details="TEST BANK 0000", paypal_email="", stripe_account_id="",
                                       stripe_payouts_ready=False, payout_method="manual")
        m.NewsletterSubscriber.objects.all().delete()
        m.SocialAccount.objects.all().delete()
        m.PushSubscription.objects.all().delete()
        m.CodBlock.objects.all().delete()
        m.GiftCard.objects.update(purchaser_email="gift@example.test", recipient_email="gift@example.test")
        m.Profile.objects.update(stripe_customer_id="", totp_enabled=False, totp_secret="")
        m.Message.objects.update(body="(message hidden on the test site)")
        m.ChatMessage.objects.update(message="(message hidden on the test site)")
        Session.objects.all().delete()
        self.stdout.write(self.style.SUCCESS(f"Done: {n} accounts scrubbed. Superusers keep their passwords; everyone else must use 'Forgot password'."))
