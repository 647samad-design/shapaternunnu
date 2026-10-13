# Lumen Market - Developer Guide

For the engineers who take over the platform: how it is built, where
everything lives, how money and orders move, and how to change it safely.

Related documents:

| Document | For |
|---|---|
| `README.md` | Feature overview, quick start, rebranding |
| `DOCKER.md` | Running with Docker (web, scheduler, Postgres, Redis) |
| `DEPLOY.md` | Going live: hosting, payments, email, sign-in, SMS, payouts, legal |
| `API.md` | REST API for apps and integrations |
| `QUALITY.md` | Accessibility audit, load test, senior code audit |
| `LICENSE`, `THIRD_PARTY_NOTICES.md` | Ownership and the licences of the open-source parts |

---

## 1. Architecture at a glance

```
            Browser / mobile app / partner system
                           │  HTTPS
              ┌────────────▼─────────────┐
              │  Django 6 (gunicorn)     │  web container / process
              │  ─ Shop        /         │
              │  ─ Seller Ctr  /seller/  │
              │  ─ Store admin /manage/  │
              │  ─ REST API    /api/v1/  │
              │  ─ Django admin /admin/  │
              └──┬─────────┬─────────┬───┘
                 │         │         │
        PostgreSQL     Redis      S3 / Supabase Storage
        (or SQLite)   (cache)     (or local disk)
                 ▲
     scheduler: timed sales, unpaid orders, daily_tasks (cron-style)

 Outside services (all optional except payments + email):
 Stripe · PayPal · SMTP email · Google/Apple sign-in · Twilio SMS ·
 EasyPost/Shippo couriers · Web push (VAPID) · Anthropic AI · Sentry
```

* **One Django project** (`backend/`) with **one app** (`bees/`). Server-
  rendered HTML (Django templates) with small, dependency-free JavaScript; no
  front-end build step.
* **Stateless web processes**: anything shared between workers lives in the
  database or the cache (Redis, or a shared file cache when Redis isn't set),
  so you can run any number of web workers and servers.
* **Slow work after the response**: emails, push notifications and similar run
  after the page is sent (`bees/background.py`, `after_response`), and
  periodic work runs from management commands (section 7).
* **White-label**: everything brand-specific (name, logo, colours, texts,
  rules) is in the `SiteSettings` row, edited in the store admin. No code
  changes per client.

## 2. Tech stack

| Layer | Choice |
|---|---|
| Language / framework | Python 3.12+ (3.13 in Docker and CI), Django 6 |
| Database | PostgreSQL 16 in production (Supabase, RDS, any) - SQLite for local/small |
| Cache | Redis (recommended), otherwise a shared file cache |
| Files | Local disk or S3-compatible storage (Supabase Storage), private bucket for seller documents |
| Web server | gunicorn behind any HTTPS proxy; static files by WhiteNoise |
| API | Django REST Framework + drf-spectacular (OpenAPI 3, Swagger UI) |
| Payments | Stripe Checkout + webhooks, PayPal Orders v2 + webhooks |
| PDFs / spreadsheets | reportlab (invoices, statements), openpyxl (bulk import/export) |
| Monitoring | Sentry (optional), `/health/` for uptime monitors |
| Tests / CI | Django test runner, 400+ tests, GitHub Actions on SQLite, PostgreSQL and Docker |

## 3. Repository layout

```
backend/                 settings.py, urls.py (every URL), wsgi.py
bees/                    the marketplace application
  models.py              all database models (section 4)
  views.py               storefront: home, search, product, cart, checkout, orders, sign-in
  account_views.py       customer account: security, cards, addresses, privacy
  seller_views.py        Seller Center (/seller/)
  seller_center.py       Seller Center numbers, queues, alerts
  manage_views.py        store admin (/manage/)
  api/                   REST API v1 (views, serializers, token auth, docs)
  templates/bees/        all HTML (design tokens and base layout in base.html)
  management/commands/   scheduled jobs and maintenance commands
  migrations/            database migrations (apply with `manage.py migrate`)
  tests.py               the automated tests
deploy/docker/           container entrypoint and scheduler
deploy/supabase/         optional Stripe relay for hosts that block Stripe
netlify/                 optional edge proxy (Netlify as the front door)
Dockerfile, docker-compose.yml, .github/workflows/tests.yml
```

### Code map of `bees/`

| Area | Modules |
|---|---|
| Catalog & search | `views.py` (listing, `_smart_search`), `autocomplete.py` (suggest + typo fix), `recommend.py`, `seo.py`, `sitemaps.py`, `product_art.py` (demo images) |
| Cart & checkout | `cart.py` (session cart), `views.checkout_view` (atomic order creation, stock locking), `shipping.py`, `taxes.py`, `currency.py` |
| Payments | `payments.py` (Stripe), `paypal.py`, `cod.py` (cash on delivery protection), `giftcards.py`, `refunds.py` |
| Orders after purchase | `fulfilment.py` (pack/ship/deliver rules), `order_emails.py`, `tracking.py`, `couriers.py` + `courier_views.py` (EasyPost/Shippo labels and tracking), `claims.py`, returns in `views.py`, `automation.py` |
| Sellers | `seller_views.py`, `seller_center.py`, `performance.py`, `badges.py`, `statements.py` (PDF), `payouts.py` (PayPal Payouts, Stripe Connect), `bulk.py` + `bulk_views.py` |
| Marketing | `promotions.py` + `promo_views.py` (coupons, timed sales), `ads.py` (sponsored products), `campaigns.py` (email), `webpush.py`, `price_alerts.py`, `stock_alerts.py` |
| Messaging | `messaging.py` (buyer-seller), `assistant.py` (help chat), `live.py` (polling for unread counts) |
| Store admin | `manage_views.py`, `staff_roles.py`, `alerts.py`, `reports_listing.py`, `analytics.py` (first-party stats) |
| Accounts & security | `social.py` (Google/Apple), `twofactor.py` (TOTP), `security.py`, `ratelimit.py`, `middleware.py` (CSP, headers, staff 2FA, demo guard) |
| International | `i18n.py`, `i18n_pack*.py`, `translations*.py`, `email_i18n.py`, `countries.py` |
| Platform | `envfile.py` (Keys & passwords page), `background.py`, `staging.py`, `images.py`, `pwa.py`, `sms.py`, `ai.py` |

## 4. Data model (main tables)

| Model | Purpose / key points |
|---|---|
| `SiteSettings` | One row: brand, store rules, commission defaults, feature switches. Read with `SiteSettings.load()` (cached, 5 s per worker) |
| `Product`, `ProductVariant`, `ProductImage` | Catalog. `Product.objects.live()` = what customers may see (approved product, approved seller not on holiday). Variants carry their own stock/price; `sync_variants()` keeps the product total |
| `SellerAccount`, `OrganizationMember` | A seller (individual or business) and its team. `get_seller_account_for_user()` returns the account and role (`owner`, `admin`, `staff`) |
| `SellerPlan`, `PlanPayment` | Paid seller plans (lower commission, more products) |
| `Order`, `OrderItem`, `OrderEvent` | One order can contain items from several sellers. Commission is copied onto each `OrderItem` at purchase so later rate changes never rewrite history. `OrderEvent` is the customer-visible timeline |
| `Shipment` | Courier labels and tracking from EasyPost/Shippo |
| `ReturnRequest`, `Claim` | Returns and buyer-protection problem reports |
| `Payout` | Seller payouts (manual, PayPal Payouts, Stripe Connect) |
| `Coupon`, `Promotion`, `GiftCard` | Discounts, timed sales, gift cards |
| `AdCampaign`, `AdStat` | Sponsored products, daily stats |
| `EmailCampaign`, `NewsletterSubscriber`, `PushSubscription` | Marketing |
| `Profile` | Per-user extras: store credit, points, 2FA secret (TOTP), referral, language |
| `Conversation`, `Message`, `ChatThread`, `ChatMessage` | Buyer-seller messages and the help chat |
| `Review`, `SellerReview`, `Question` | Feedback |
| `TaxRate`, `ShippingZone`, `Currency`, `UIText` | Market configuration and editable translations |
| `AuditLog` | Who did what (staff actions, security events, API sign-ins) |
| `ApiToken` | REST API tokens (SHA-256 hash only) |
| `DailyStat`, `ListingReport`, `CodBlock`, `SocialAccount` | Statistics, listing reports, refused cash-order block list, Google/Apple links |

## 5. How money and orders flow

### Checkout (`views.checkout_view`)
1. Inside one database transaction the products (and variants) are locked
   with `SELECT ... FOR UPDATE`, stock is checked and reserved, coupon / gift
   card / store credit are applied, shipping and tax are calculated, and the
   `Order` + `OrderItem`s are created. Each item gets the seller's commission
   rate at that moment (`OrderItem.apply_commission`).
2. **Card**: the customer goes to Stripe's hosted page. The signed webhook
   (`/payment/stripe/webhook/`) is the source of truth: it checks the amount
   and currency and marks the order paid, exactly once (idempotent).
   **PayPal**: same idea with PayPal Orders v2 + webhook. **Cash on
   delivery**: confirmed by email/SMS code for new customers (`cod.py`).
3. Unpaid card/PayPal orders release their stock after the payment window
   (`release_unpaid_orders`, every 15 minutes).

### Fulfilment (`fulfilment.set_items_status`)
`pending -> packed -> handed_to_courier -> delivered`, per item (each seller
ships their own items). Blocked for cancelled, unpaid, refunded and
unconfirmed cash orders. The order moves to *shipped* / *delivered* when all
items have (`sync_order_status_from_items`). Customers are emailed and
notified for every package. The website, bulk actions, courier webhooks and the
API all call this one function.

### Refunds and returns
Cancelling before shipping refunds automatically (Stripe/PayPal, or store
credit). Returns and claims refund one item's fair share
(`Order.refund_value`) via `refunds.py`. Seller earnings and commission are
reversed accordingly (`OrderItem.objects.counted()` excludes refunded sales).

### Seller money
`seller earning = item subtotal - commission`. It becomes **available**
after delivery plus the return window; sponsored-product clicks are deducted
(`ad_spend`). Payouts run automatically on the chosen schedule
(`auto_payouts`) via PayPal Payouts or Stripe Connect, or are recorded by
staff for bank transfers.

## 6. Security model

* **Roles**: customer, seller (owner/admin/staff), store staff with roles
  (`staff_roles.py`: manager, orders, support, catalog, finance, marketing),
  owner (superuser). Staff roles are enforced by middleware per admin area.
* **Two-step sign-in** (TOTP + backup codes); can be required for staff and
  sellers. Google/Apple sign-in via OpenID Connect.
* **Payments**: card data never touches the server; webhooks are signature-
  verified and amount-checked.
* **Web protections**: CSRF, Content-Security-Policy, HSTS/secure cookies in
  production, rate limits on sign-in, sign-up, checkout, coupons, reviews,
  messages and the API, safe redirects, upload validation (real images only,
  EXIF stripped), private storage for ID documents, SSRF allowlist for push.
* **Secrets** only in environment variables / `.env` (never in git; the git
  history was scanned and is clean). The Keys & passwords page shows only the
  last 4 characters and is limited to the owner.
* **Audit log** for staff actions and security events.

## 7. Background jobs

| Command | When | What |
|---|---|---|
| `run_promotions` | every 5 min | Start/end timed sales, write ad statistics, send email-campaign batches |
| `release_unpaid_orders` | every 15 min | Free stock of abandoned card/PayPal orders, cancel unconfirmed cash orders |
| `daily_tasks` | daily | Order automation (late reminders, auto-cancel, auto-deliver), cart and price-drop emails, payouts, plan reminders, badges, currency rates, backup, session clean-up (and demo reset in demo mode) |

Docker runs these in the `scheduler` container. Elsewhere use cron or the
host's scheduled tasks, e.g. `*/15 * * * * cd /app && python manage.py release_unpaid_orders`.
Each job is safe to run twice and safe to run late.

Other commands: `seed_data`, `demo_setup`, `backup_data`, `restore_backup`,
`anonymize_data` (scrub personal data in a staging copy), `reset_two_factor`.

## 8. Configuration

All configuration is environment variables (`.env.example` lists every one
with instructions). The most important:

| Variable | Notes |
|---|---|
| `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` | Standard Django. Production refuses to start without `SECRET_KEY` |
| `DATABASE_URL`, `DATABASE_SSL` | Postgres URL; SSL is required automatically for remote hosts |
| `REDIS_URL` | Shared cache (recommended with several workers/servers) |
| `NUM_PROXIES` | Number of proxies in front, for correct client IPs and rate limits |
| `STORE_CURRENCY` | Base currency (prices, payouts) |
| `STRIPE_*`, `PAYPAL_*`, `EMAIL_*`, `GOOGLE_*`, `APPLE_*`, `TWILIO_*`, `EASYPOST_API_KEY`/`SHIPPO_API_TOKEN`, `VAPID_*`, `ANTHROPIC_API_KEY`, `SENTRY_DSN` | Outside services; also editable on the Keys & passwords page |
| `DEMO_MODE`, `STAGING` | Demo copy with one-click logins; test copy that never emails real people |
| `API_TOKEN_DAYS`, `API_RATE_ANON`, `API_RATE_USER` | REST API token lifetime and rate limits |

Business settings (commission, return window, COD limits, gift cards, ads,
languages, currencies, taxes, shipping zones, team) are in the store admin,
not in code.

## 9. Working on the code

### Local setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate && python manage.py seed_data && python manage.py createsuperuser
python manage.py runserver
```
or `docker compose up -d --build` (see `DOCKER.md`).

### Tests
```bash
python manage.py test bees --parallel auto
```
CI runs the suite on SQLite and on PostgreSQL 16, and builds and boots the
Docker stack, on every pull request. Keep it green: add a test for every bug
fixed and every rule added.

### Conventions
* **Business rules live in one place** and are reused by the website, admin,
  bulk import and API (`fulfilment.py`, `bulk.py`, `refunds.py`,
  `payments.py`...). Don't re-implement a rule in a view.
* Money is `Decimal`, rounded with `ROUND_HALF_UP` to 2 places (0 for
  zero-decimal currencies). Never use floats for amounts.
* Lock rows that money or stock depend on (`select_for_update`). On
  PostgreSQL, add `of=("self",)` when the query also uses `select_related`
  on a nullable relation.
* Send email/SMS/push after the response or on commit
  (`after_response`, `transaction.on_commit`), never inside a long request.
* Every customer-facing string is translatable: add new strings to
  `i18n_pack_data.py` (Urdu, Roman Urdu, Arabic, Spanish, French).
* Templates reuse the design tokens in `base.html`; check new pages at
  320px width, in dark mode and right-to-left (Arabic).
* Use plain, friendly wording in the UI (no jargon).

### Adding a feature - checklist
1. Model change -> `python manage.py makemigrations bees` and commit the migration.
2. Settings a store owner should control -> a `SiteSettings` field + the Store settings form.
3. Staff page -> `manage_views.py` + map its URL prefix to an area in `staff_roles.py`.
4. API -> `bees/api/` (serializer + view + URL); it appears in `/api/v1/docs/`.
5. Translations, tests, and a line in `README.md`.

### Upgrading dependencies
`requirements.txt` uses compatible ranges. Upgrade in a branch, run the
tests on SQLite and PostgreSQL (CI does both), read the Django release notes
for deprecations, then deploy. `python manage.py check --deploy` must stay clean.

## 10. Operations

* **Health**: `/health/` (200 when the site and database answer).
* **System check** in the store admin: email, payments, webhooks, storage,
  backups, scheduled jobs, security settings, recent errors - with live tests.
* **Logs**: gunicorn and the scheduler log to stdout (`docker compose logs`).
  Set `SENTRY_DSN` for error alerts.
* **Backups**: nightly (14 kept) + `pg_dump` for off-site copies; restore with
  `restore_backup` or `psql` (see `DOCKER.md`).
* **Performance**: about 75 pages/second on a 2-CPU server with SQLite
  (`QUALITY.md`); PostgreSQL + Redis + more workers + a CDN scale it further.
