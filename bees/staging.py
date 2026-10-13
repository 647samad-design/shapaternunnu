"""Test ("staging") copy of the store: STAGING=True in that copy's .env.

* A yellow "Test site" bar on every page, and search engines are told to
  stay away (robots.txt + X-Robots-Tag).
* Optional password for the whole site (STAGING_PASSWORD) so only your team
  can open it.
* No real customer is ever contacted: every email goes to STAGING_EMAIL_TO
  instead (subject starts with [TEST]), SMS and seller payouts are switched
  off. Use Stripe/PayPal test keys there.
* ``manage.py anonymize_data`` scrubs personal data after copying the live
  database into the test copy.
"""
import base64
import hmac

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend
from django.http import HttpResponse
from django.utils.module_loading import import_string


def on():
    return getattr(settings, "STAGING", False)


class RedirectEmailBackend(BaseEmailBackend):
    """Sends every email to STAGING_EMAIL_TO (or prints it) instead of the real people."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.real = import_string(getattr(settings, "STAGING_REAL_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"))(*args, **kwargs)

    def send_messages(self, email_messages):
        target = getattr(settings, "STAGING_EMAIL_TO", "")
        for msg in email_messages:
            original = ", ".join(msg.to + msg.cc + msg.bcc)
            msg.subject = f"[TEST] {msg.subject}" + (f" (to {original})" if original else "")
            msg.to, msg.cc, msg.bcc = ([target] if target else []), [], []
            msg.extra_headers.pop("List-Unsubscribe", None)
        return self.real.send_messages([m for m in email_messages if m.to])


class StagingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not on():
            return self.get_response(request)
        password = getattr(settings, "STAGING_PASSWORD", "")
        open_paths = ("/stripe/webhook", "/paypal/webhook", "/shipping/webhook", "/static/", "/sw.js", "/manifest")
        if password and not request.path.startswith(open_paths):
            ok = False
            header = request.META.get("HTTP_AUTHORIZATION", "")
            if header.startswith("Basic "):
                try:
                    _user, _sep, given = base64.b64decode(header[6:]).decode().partition(":")
                    ok = hmac.compare_digest(given, password)
                except (ValueError, UnicodeDecodeError):
                    ok = False
            if not ok:
                resp = HttpResponse("This is a private test site.", status=401, content_type="text/plain")
                resp["WWW-Authenticate"] = 'Basic realm="Test site", charset="UTF-8"'
                return resp
        response = self.get_response(request)
        response["X-Robots-Tag"] = "noindex, nofollow"
        return response
