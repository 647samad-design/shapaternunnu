"""Multi-store mode: run many separate stores from one installation.

Each store lives in ``stores/<slug>/`` (or MULTISTORE_DIR):

* ``store.json`` - its web addresses, database and on/off switch
* ``.env``       - its own keys (Stripe, PayPal, email, Twilio...) and
                   STORE_CURRENCY; anything missing falls back to the main .env

Every request is matched to a store by its web address. That store's
database is used for the whole request (the "default" connection is pointed
at it for this thread only), cache keys get the store's prefix, uploaded
files go under ``stores/<slug>/`` and its own keys are used. Addresses that
match no store use the main database, so a normal single-store install
works exactly as before. Each store is fully separate: products, sellers,
customers, orders, staff and settings never mix.

Commands: ``store_create`` adds a store, ``stores`` lists them and
``for_each_store <command>`` runs a job (e.g. daily_tasks) in every store.
"""
import contextvars
import json
import os
import re
from pathlib import Path

from django.conf import settings

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
_current = contextvars.ContextVar("lumen_store", default=None)
_registry = {"loaded": False, "stores": {}, "hosts": {}, "mtime": 0.0}


class Store:
    def __init__(self, slug, folder, data):
        self.slug = slug
        self.folder = Path(folder)
        self.name = data.get("name", slug)
        self.hosts = [h.lower() for h in data.get("hosts", []) if h]
        self.active = data.get("active", True)
        self.database = data.get("database") or f"sqlite:///{self.folder / 'db.sqlite3'}"
        self.created = data.get("created", "")
        self._env, self._env_mtime = {}, None

    def __repr__(self):
        return f"<Store {self.slug}>"

    @property
    def env_path(self):
        return self.folder / ".env"

    def env(self):
        """The store's own keys, re-read when the file changes."""
        try:
            mtime = self.env_path.stat().st_mtime
        except OSError:
            return {}
        if mtime != self._env_mtime:
            from dotenv import dotenv_values
            self._env = {k: v for k, v in dotenv_values(self.env_path).items() if v is not None}
            self._env_mtime = mtime
        return self._env

    def db_settings(self, base):
        import dj_database_url
        parsed = dj_database_url.parse(self.database)
        merged = dict(base)
        for key in ("ENGINE", "NAME", "USER", "PASSWORD", "HOST", "PORT", "OPTIONS"):
            if key in parsed:
                merged[key] = parsed[key]
        merged["STORE_SLUG"] = self.slug
        return merged


# ---------------------------------------------------------------- registry

def root():
    return Path(getattr(settings, "MULTISTORE_DIR", "") or Path(settings.BASE_DIR) / "stores")


def enabled():
    return bool(getattr(settings, "MULTISTORE", False))


def load(force=False):
    folder = root()
    try:
        mtime = max([folder.stat().st_mtime] + [p.stat().st_mtime for p in folder.glob("*/store.json")])
    except (OSError, ValueError):
        mtime = 0.0
    if _registry["loaded"] and not force and mtime == _registry["mtime"]:
        return _registry["stores"]
    stores, hosts = {}, {}
    for path in sorted(folder.glob("*/store.json")):
        slug = path.parent.name
        if not SLUG.match(slug):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        store = Store(slug, path.parent, data)
        stores[slug] = store
        for host in store.hosts:
            hosts[host] = slug
    _registry.update(loaded=True, stores=stores, hosts=hosts, mtime=mtime)
    return stores


def all_hosts():
    """Every store's web address (for ALLOWED_HOSTS at start-up)."""
    folder = Path(os.environ.get("MULTISTORE_DIR", "") or Path(__file__).resolve().parent.parent / "stores")
    hosts = []
    for path in folder.glob("*/store.json"):
        try:
            hosts += [h.lower() for h in json.loads(path.read_text(encoding="utf-8")).get("hosts", []) if h]
        except (OSError, ValueError):
            pass
    return hosts


def for_host(host):
    if not enabled():
        return None
    load()
    slug = _registry["hosts"].get((host or "").split(":")[0].lower())
    return _registry["stores"].get(slug) if slug else None


def get(slug):
    return load().get(slug)


def current():
    return _current.get()


# ---------------------------------------------------------------- switching

_BASE = {}


def _base_settings():
    if "db" not in _BASE:
        _BASE["db"] = dict(settings.DATABASES["default"])
    return _BASE["db"]


def activate(store):
    """Points this thread's default database connection at ``store`` (None =
    the main store). Returns a token for deactivate()."""
    from django.db import connections
    token = _current.set(store)
    conn = connections["default"]
    wanted = store.db_settings(_base_settings()) if store else _base_settings()
    if conn.settings_dict.get("NAME") != wanted.get("NAME") or conn.settings_dict.get("HOST") != wanted.get("HOST"):
        if conn.in_atomic_block:
            raise RuntimeError("Can't switch stores inside a database transaction.")
        conn.close()
        conn.settings_dict = dict(wanted)  # a new dict: never touch other threads' settings
    return token


def deactivate(token):
    from django.db import connections
    _current.reset(token)
    store = _current.get()
    conn = connections["default"]
    wanted = store.db_settings(_base_settings()) if store else _base_settings()
    if conn.settings_dict.get("NAME") != wanted.get("NAME"):
        conn.close()
        conn.settings_dict = dict(wanted)


class use:
    """``with multistore.use(store): ...`` runs code inside one store."""

    def __init__(self, store):
        self.store = store

    def __enter__(self):
        self.token = activate(self.store)
        return self.store

    def __exit__(self, *exc):
        deactivate(self.token)


class StoreMiddleware:
    """First in MIDDLEWARE: picks the store for the request's web address."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not enabled():
            return self.get_response(request)
        store = for_host(request.get_host())
        if store is not None and not store.active:
            from django.http import HttpResponse
            return HttpResponse("<!doctype html><title>Store paused</title><body style='font-family:system-ui;text-align:center;padding:15vh 16px'>"
                                "<h1>This store is taking a short break</h1><p>Please come back soon.</p></body>",
                                status=503, headers={"Retry-After": "3600"})
        request.store = store
        token = activate(store)
        try:
            return self.get_response(request)
        finally:
            deactivate(token)


# ---------------------------------------------------------------- per-store values

def env(name, default=""):
    """A key from the current store's .env, else the main environment."""
    store = _current.get()
    if store is not None:
        value = store.env().get(name)
        if value not in (None, ""):
            return value.strip()
    return (os.environ.get(name) or default or "").strip()


class _StoreSettings:
    """``store_settings.X`` = the store's own value for X, else Django's."""
    PER_STORE = {"STORE_CURRENCY", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "DEFAULT_FROM_EMAIL", "SITE_URL", "AI_API_KEY"}
    ENV_NAMES = {"AI_API_KEY": "ANTHROPIC_API_KEY"}

    def __getattr__(self, name):
        store = _current.get()
        if store is not None and name in self.PER_STORE:
            value = store.env().get(self.ENV_NAMES.get(name, name))
            if value:
                return value.upper() if name == "STORE_CURRENCY" else value
        return getattr(settings, name)


store_settings = _StoreSettings()


def cache_key(key, key_prefix, version):
    """CACHES KEY_FUNCTION: every store gets its own cache space."""
    store = _current.get()
    return f"{store.slug if store else '_'}:{key_prefix}:{version}:{key}"


def media_prefix(name):
    store = _current.get()
    if store is None:
        return name
    prefix = f"stores/{store.slug}/"
    return name if name.startswith(prefix) else prefix + name.lstrip("/")


class StoreFilesMixin:
    """Uploaded files of each store go under stores/<slug>/."""

    def get_available_name(self, name, max_length=None):
        return super().get_available_name(media_prefix(name), max_length=max_length)


def storage_class(dotted):
    """A storage backend class with the per-store folder added."""
    from django.utils.module_loading import import_string
    base = import_string(dotted)
    return type(f"Store{base.__name__}", (StoreFilesMixin, base), {})


from django.core.mail.backends.base import BaseEmailBackend  # noqa: E402


class StoreEmailBackend(BaseEmailBackend):
    """Sends with the store's own mail server and sender address when its
    .env has them (EMAIL_HOST_USER, EMAIL_HOST_PASSWORD, EMAIL_HOST,
    EMAIL_PORT, DEFAULT_FROM_EMAIL); otherwise with the main backend."""

    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently)
        from django.core.mail import get_connection
        store = _current.get()
        e = store.env() if store is not None else {}
        self._from = ""
        if e.get("EMAIL_HOST_USER"):
            self.inner = get_connection("django.core.mail.backends.smtp.EmailBackend", fail_silently=fail_silently,
                                        host=e.get("EMAIL_HOST") or "smtp.gmail.com", port=int(e.get("EMAIL_PORT") or 587),
                                        username=e["EMAIL_HOST_USER"], password=e.get("EMAIL_HOST_PASSWORD", ""),
                                        use_tls=e.get("EMAIL_USE_TLS", "True").lower() != "false")
            self._from = e.get("DEFAULT_FROM_EMAIL") or e["EMAIL_HOST_USER"]
        else:
            self.inner = get_connection(getattr(settings, "MULTISTORE_BASE_EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend"),
                                        fail_silently=fail_silently, **kwargs)
            self._from = e.get("DEFAULT_FROM_EMAIL", "")

    def open(self):
        return self.inner.open()

    def close(self):
        return self.inner.close()

    def send_messages(self, email_messages):
        if self._from:
            for m in email_messages:
                if m.from_email == settings.DEFAULT_FROM_EMAIL:
                    m.from_email = self._from
        return self.inner.send_messages(email_messages)
