"""The outermost email backend.

* Never sends to addresses that can't exist: reserved test domains
  (.invalid, .test, .example, .localhost, example.com...) used by the demo
  accounts and sample data. Mail servers drop the connection when asked to
  deliver there, which used to show up as "email failures" in System check.
* Tries once more when the mail server closes the connection mid-way
  (common with Gmail after a pause).
"""
import logging
import smtplib

from django.conf import settings
from django.core.mail import get_connection
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)

RESERVED_SUFFIXES = (".invalid", ".test", ".example", ".localhost", ".local")
RESERVED_DOMAINS = {"example.com", "example.net", "example.org", "localhost"}


def deliverable(address):
    """False for addresses on reserved test domains (RFC 2606 / 6761)."""
    if not address or "@" not in address:
        return False
    domain = address.rsplit("@", 1)[1].strip().strip(">").lower()
    return domain not in RESERVED_DOMAINS and not domain.endswith(RESERVED_SUFFIXES)


WRAPPERS = ("bees.mailguard.GuardedBackend", "bees.multistore.StoreEmailBackend", "bees.staging.RedirectEmailBackend")


def real_backend():
    """The backend that actually delivers mail (SMTP, console...), looking
    through this guard and the multi-store / staging wrappers."""
    name = settings.EMAIL_BACKEND
    if name in WRAPPERS:
        name = getattr(settings, "EMAIL_REAL_BACKEND", name)
    return name


def is_console():
    return real_backend().endswith("console.EmailBackend")


class GuardedBackend(BaseEmailBackend):
    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently)
        self.inner = get_connection(getattr(settings, "MAILGUARD_INNER_BACKEND", "django.core.mail.backends.smtp.EmailBackend"),
                                    fail_silently=fail_silently, **kwargs)

    def open(self):
        return self.inner.open()

    def close(self):
        return self.inner.close()

    def send_messages(self, email_messages):
        ready = []
        for m in email_messages:
            m.to = [a for a in m.to if deliverable(a)]
            m.cc = [a for a in m.cc if deliverable(a)]
            m.bcc = [a for a in m.bcc if deliverable(a)]
            if m.recipients():
                ready.append(m)
        if not ready:
            return 0
        try:
            return self.inner.send_messages(ready)
        except (smtplib.SMTPServerDisconnected, ConnectionResetError, TimeoutError) as exc:
            logger.warning("Mail server dropped the connection (%s) - trying once more", exc)
            try:
                self.inner.close()
            except Exception:
                pass
            return self.inner.send_messages(ready)
