"""Store settings > Keys & passwords: edit the server's .env file from the
admin, so the owner never has to open a console.

Safety rules:
* superusers only, and the current password is asked again to save;
* secret values are never shown back - only "set" and the last 4 characters;
* only the keys listed here can be changed (core server settings such as
  SECRET_KEY, DEBUG, ALLOWED_HOSTS and DATABASE_URL stay console-only, because
  a typo there could take the site down);
* the old file is backed up (last 5 kept) and the new one written atomically
  with owner-only permissions; every change is in the activity log (names only).
"""
import os
import re
import shutil
import tempfile
from glob import glob

from django.conf import settings
from django.utils import timezone

GROUPS = [
    ("Card payments - Stripe", "Stripe > Developers > API keys and Webhooks. Use test keys (sk_test_, pk_test_) until you go live.", [
        ("STRIPE_SECRET_KEY", "Secret key", True, r"^(sk|rk)_(test|live)_[A-Za-z0-9]+$"),
        ("STRIPE_PUBLISHABLE_KEY", "Publishable key", False, r"^pk_(test|live)_[A-Za-z0-9]+$"),
        ("STRIPE_WEBHOOK_SECRET", "Webhook signing secret", True, r"^whsec_[A-Za-z0-9]+$"),
        ("STRIPE_API_BASE", "Relay address (only for PythonAnywhere free)", True, r"^https://"),
        ("STORE_CURRENCY", "Store currency (3 letters)", False, r"^[A-Za-z]{3}$"),
    ]),
    ("PayPal", "developer.paypal.com > Apps & Credentials > your app: copy the Client ID and Secret (Sandbox for testing, Live for real money). Then add a webhook with the address shown below and copy its Webhook ID.", [
        ("PAYPAL_CLIENT_ID", "Client ID", False, r"^[A-Za-z0-9_-]{20,}$"),
        ("PAYPAL_CLIENT_SECRET", "Secret", True, r"^[A-Za-z0-9_-]{20,}$"),
        ("PAYPAL_MODE", "Mode: sandbox or live", False, r"^(sandbox|live)$"),
        ("PAYPAL_WEBHOOK_ID", "Webhook ID", False, r"^[A-Z0-9]{10,}$"),
    ]),
    ("Email", "Gmail: your address + an App Password (Google Account > Security > App passwords). Any other provider: also set the server and port.", [
        ("EMAIL_HOST_USER", "Email address / user name", False, r"^\S+$"),
        ("EMAIL_HOST_PASSWORD", "Email password / App Password", True, None),
        ("DEFAULT_FROM_EMAIL", "Send emails as", False, None),
        ("EMAIL_HOST", "Mail server (leave empty for Gmail)", False, r"^[A-Za-z0-9.-]+$"),
        ("EMAIL_PORT", "Mail server port", False, r"^\d{2,5}$"),
    ]),
    ("Couriers", "Pick the service in Store settings > Shipping labels, paste its key here. In the courier's dashboard add the webhook address shown below so tracking updates arrive instantly.", [
        ("EASYPOST_API_KEY", "EasyPost API key", True, r"^\S{10,}$"),
        ("SHIPPO_API_TOKEN", "Shippo API token", True, r"^shippo_(test|live)_\S+$"),
        ("SHIPPING_WEBHOOK_TOKEN", "Courier webhook secret", True, r"^[A-Za-z0-9_-]{16,}$"),
    ]),
    ("Image storage - Supabase", "Supabase > Storage > Settings > S3 connection.", [
        ("SUPABASE_PROJECT_REF", "Project reference", False, r"^[a-z0-9]{10,30}$"),
        ("SUPABASE_S3_REGION", "Region", False, r"^[a-z0-9-]+$"),
        ("SUPABASE_S3_ACCESS_KEY_ID", "Access key ID", True, None),
        ("SUPABASE_S3_SECRET_ACCESS_KEY", "Secret access key", True, None),
        ("SUPABASE_STORAGE_BUCKET", "Public bucket name", False, r"^[a-z0-9._-]+$"),
        ("SUPABASE_PRIVATE_BUCKET", "Private bucket name", False, r"^[a-z0-9._-]+$"),
    ]),
    ("AI assistant", "console.anthropic.com > API keys. Powers listing writer, buying guides, shopper Q&A and one-click translation.", [
        ("ANTHROPIC_API_KEY", "Anthropic API key", True, r"^sk-ant-\S+$"),
        ("AI_MODEL", "Model (optional)", False, r"^[a-z0-9.-]+$"),
    ]),
    ("Error alerts", "sentry.io > Create project > Django > copy the DSN.", [
        ("SENTRY_DSN", "Sentry DSN", True, r"^https://"),
    ]),
    ("Website", "Your public address, used in emails and links. A CDN address (e.g. from Cloudflare or BunnyCDN) makes pages load faster worldwide.", [
        ("SITE_URL", "Site address", False, r"^https://[^\s/]+"),
        ("CDN_URL", "CDN address for static files (optional)", False, r"^https://[^\s]+$"),
    ]),
]
EDITABLE = {key: (label, secret, pattern) for _g, _h, rows in GROUPS for key, label, secret, pattern in rows}
LOCKED = ["SECRET_KEY", "DEBUG", "ALLOWED_HOSTS", "CSRF_TRUSTED_ORIGINS", "DATABASE_URL", "USE_X_FORWARDED_HOST",
          "NUM_PROXIES", "REDIS_URL", "DEMO_MODE", "SECURE_SSL_REDIRECT"]
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


class EnvError(ValueError):
    pass


def path():
    return os.path.join(str(settings.BASE_DIR), ".env")


def _unquote(raw):
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        inner = raw[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\") if raw[0] == '"' else inner
    if " #" in raw:
        raw = raw.split(" #", 1)[0].rstrip()
    return raw


def read():
    """{KEY: value} from the .env file (empty dict if it doesn't exist)."""
    values = {}
    try:
        with open(path(), encoding="utf-8") as fh:
            for line in fh:
                m = _LINE.match(line.rstrip("\n"))
                if m:
                    values[m.group(1)] = _unquote(m.group(2))
    except FileNotFoundError:
        pass
    return values


def mask(value, secret):
    if not value:
        return ""
    if not secret:
        return value
    return "••••" + value[-4:] if len(value) > 8 else "••••"


def _quote(value):
    if re.fullmatch(r"[A-Za-z0-9_./:@+=,-]*", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def validate(key, value):
    if key not in EDITABLE:
        raise EnvError(f"{key} can't be changed here.")
    if any(c in value for c in "\r\n\x00"):
        raise EnvError(f"{EDITABLE[key][0]}: must be on one line.")
    if len(value) > 500:
        raise EnvError(f"{EDITABLE[key][0]}: too long.")
    pattern = EDITABLE[key][2]
    if pattern and not re.search(pattern, value):
        raise EnvError(f"{EDITABLE[key][0]} doesn't look right - please check you copied the whole value.")
    return value


def write(changes):
    """``changes``: {KEY: new value, or None to remove}. Keeps every other
    line (comments, order, other keys) as it was."""
    for key, value in changes.items():
        if value is not None:
            validate(key, value)
        elif key not in EDITABLE:
            raise EnvError(f"{key} can't be changed here.")
    target = path()
    lines = []
    if os.path.exists(target):
        with open(target, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        backup = f"{target}.bak-{timezone.now():%Y%m%d-%H%M%S}"
        shutil.copy2(target, backup)
        os.chmod(backup, 0o600)
        for old in sorted(glob(f"{target}.bak-*"))[:-5]:
            try:
                os.remove(old)
            except OSError:
                pass
    pending = dict(changes)
    out = []
    for line in lines:
        m = _LINE.match(line)
        if m and m.group(1) in pending:
            value = pending.pop(m.group(1))
            if value is not None:
                out.append(f"{m.group(1)}={_quote(value)}")
            continue
        out.append(line)
    for key, value in pending.items():
        if value is not None:
            out.append(f"{key}={_quote(value)}")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".env.tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    for key, value in changes.items():  # this process sees it straight away
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def reload_app():
    """Restarts the web app so every setting is re-read. On PythonAnywhere
    touching the WSGI file does that. Returns True if it could."""
    candidates = [os.environ.get("WSGI_FILE", "")] + glob("/var/www/*_wsgi.py")
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.W_OK):
            os.utime(candidate, None)
            return True
    return False
