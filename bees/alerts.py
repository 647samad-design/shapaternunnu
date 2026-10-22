"""Things the store owner must know about: failed emails, failed refunds,
refunds/disputes made in Stripe. Shown as admin notifications and kept in
the audit log (Admin > System check lists the recent ones)."""
import logging

logger = logging.getLogger(__name__)

EMAIL_FAILED = "Email failed"
REFUND_FAILED = "Refund failed"
STRIPE_EVENT = "Stripe"
PAYMENT_FAILED = "Payment page failed"
PAYMENT_OK = "Payments working"
WEBHOOK_REJECTED = "Webhook rejected"
SERVER_ERROR = "Server error"
DAILY_TASKS = "Daily tasks ran"
ALERTS_CLEARED = "Alerts marked resolved"


def cleared_since():
    """When staff last marked email failures / page crashes as resolved
    (older entries stay in the audit log but no longer count as problems)."""
    from .models import AuditLog
    try:
        return AuditLog.objects.filter(action__startswith=ALERTS_CLEARED).order_by("-created_at").values_list("created_at", flat=True).first()
    except Exception:
        return None


def log_once(prefix, message, minutes=10):
    """Keeps one, refreshed entry for a repeating event instead of many."""
    from django.utils import timezone
    from .models import AuditLog
    try:
        latest = AuditLog.objects.filter(action__startswith=prefix).order_by("-created_at").first()
        if latest and (timezone.now() - latest.created_at).total_seconds() < minutes * 60:
            return
        AuditLog.objects.filter(action__startswith=prefix).delete()
        AuditLog.objects.create(user=None, action=f"{prefix}: {message}"[:255])
    except Exception:
        logger.exception("Could not write audit log entry")


def payments_working(note):
    """Remembers the last time a Stripe call worked, so System check only
    reports payment errors that happened after it (older ones were fixed).
    Keeps a single entry instead of one per payment."""
    from django.utils import timezone
    from .models import AuditLog
    try:
        latest = AuditLog.objects.filter(action__startswith=PAYMENT_OK).order_by("-created_at").first()
        if latest and (timezone.now() - latest.created_at).total_seconds() < 600:
            return  # recorded in the last 10 minutes - no need to write again
        AuditLog.objects.filter(action__startswith=PAYMENT_OK).delete()
        AuditLog.objects.create(user=None, action=f"{PAYMENT_OK}: {note}"[:255])
    except Exception:
        logger.exception("Could not record working payments")


def log(action):
    from .models import AuditLog
    try:
        AuditLog.objects.create(user=None, action=action[:255])
    except Exception:
        logger.exception("Could not write audit log entry")


def notify_staff(message, link="/manage/", category=None):
    from django.contrib.auth import get_user_model
    from .models import Notification
    if category:
        log(f"{category}: {message}")
    for user in get_user_model().objects.filter(is_staff=True, is_active=True):
        Notification.objects.create(user=user, message=message[:255], link=link)


def email_failed(subject, recipient, exc):
    logger.error("Email '%s' to %s failed: %s", subject, recipient, exc)
    log(f"{EMAIL_FAILED}: '{subject}' to {recipient} - {type(exc).__name__}: {exc}")
