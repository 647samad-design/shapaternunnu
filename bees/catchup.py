"""Safety net for the daily scheduled task.

If the PythonAnywhere (or cron) task stops running - expired free account,
task deleted, server moved - unpaid orders stop being released, currency
rates go stale and no backups are made. When a page is requested and the
last successful run is more than DAILY_CATCHUP_HOURS old, the jobs are
started once in a background thread (never blocking the visitor). A
cache lock makes sure only one worker starts them.

The demo reset is skipped here: it would sign out whoever is looking at
the demo right now. It still runs from the real scheduled task.
"""
import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

log = logging.getLogger(__name__)
CHECK_KEY = "daily_catchup_checked"
LOCK_KEY = "daily_catchup_running"


def overdue(now=None):
    from .alerts import DAILY_TASKS
    from .models import AuditLog
    last = AuditLog.objects.filter(action__startswith=DAILY_TASKS).order_by("-created_at").values_list("created_at", flat=True).first()
    if last is None:
        return True
    hours = getattr(settings, "DAILY_CATCHUP_HOURS", 26)
    return (now or timezone.now()) - last > timedelta(hours=hours)


def _run(store_slug):
    from django.core.management import call_command
    from django.db import connections
    from . import multistore
    try:
        if store_slug:
            with multistore.use(multistore.get(store_slug)):
                call_command("daily_tasks", "--catchup")
        else:
            call_command("daily_tasks", "--catchup")
    except SystemExit:
        pass  # some job failed - already recorded in the audit log
    except Exception:
        log.exception("Daily tasks catch-up failed")
    finally:
        cache.delete(_key(LOCK_KEY, store_slug))
        connections.close_all()


def _key(name, slug):
    return f"{name}:{slug or '-'}"


def maybe_start():
    """Cheap: one cache read per request, a DB query at most every 10 min."""
    if not getattr(settings, "DAILY_CATCHUP", True):
        return False
    from . import multistore
    store = multistore.current() if multistore.enabled() else None
    slug = getattr(store, "slug", None)
    if cache.get(_key(CHECK_KEY, slug)):
        return False
    cache.set(_key(CHECK_KEY, slug), 1, 600)
    try:
        if not overdue():
            return False
    except Exception:  # tables missing mid-deploy
        return False
    if not cache.add(_key(LOCK_KEY, slug), 1, 3 * 3600):
        return False
    log.warning("Daily tasks are overdue - starting a catch-up run")
    threading.Thread(target=_run, args=(slug,), daemon=True, name="daily-catchup").start()
    return True


class DailyCatchupMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            maybe_start()
        except Exception:
            log.exception("Catch-up check failed")
        return response
