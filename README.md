# White-label multi-vendor marketplace (Django)

A complete, production-ready marketplace you can rebrand in minutes: customers shop and pay by card,
independent sellers and businesses run their own stores, and the owner earns commission on every sale.

Out of the box it ships as **"Lumen Market"**. Change the name, logo, colours, homepage copy, shipping,
tax and contact details from **Admin → Site settings** — no code changes.

## Documentation

| Document | What's inside |
|---|---|
| [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) | Architecture, code map, data model, money and order flows, conventions |
| [DOCKER.md](DOCKER.md) | One-command setup: web, scheduler, PostgreSQL, Redis |
| [DEPLOY.md](DEPLOY.md) | Going live: hosting, payments, email, sign-in, SMS, payouts, legal |
| [API.md](API.md) | REST API for mobile apps and integrations (live docs at `/api/v1/docs/`) |
| [QUALITY.md](QUALITY.md) | Security audit, accessibility audit, load test |
| [HANDOVER.md](HANDOVER.md) | Ownership, licences and handover checklist |
| [LICENSE](LICENSE), [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) | Licence and open-source components |

## Highlights

**For customers**
- Fast, accessible storefront (WCAG-minded contrast, 16px+ text, keyboard focus, dark mode, mobile-first)
- Search with live suggestions, categories with subcategories, filters (price, rating, seller, brand, size, colour, stock) and sorting
- Product pages with gallery and video, specifications, size guides, EU product-safety (GPSR) details, verified-purchase reviews, questions for the seller, related products
- Digital downloads (e-books, music, templates…) delivered in My orders after online payment
- **Request a quote** for bulk orders: the seller offers a price, the buyer accepts, counters or declines
- Follow stores and get told about their new products
- Item condition (new / used / refurbished), "Other sellers" for the same product, **Make an offer** and **auctions** with anti-sniping
- Personalization (e.g. engraving text), free gift wrap and gift message, collection points instead of home delivery
- Voucher centre (collect store and seller vouchers, free-shipping vouchers), daily check-in coins, paid **membership** (free shipping, member price)
- Subscribe & Save, "Frequently bought together", Save for later, several wishlists and shareable wishlists
- Review photos, "Helpful" votes and review filters; search by photo (with AI on); "pay in instalments" message for Klarna/Afterpay via Stripe
- EU rules: lowest price of the last 30 days next to discounts (Omnibus) and seller trader information (DSA)
- AI shopping assistant ("Ask"), deals page, "Recommended for you", price alerts, delivery dates, "50+ bought in the past month" and "#1 Best Seller" badges, voice search, image zoom
- Loyalty levels (Bronze to Platinum, up to 2x points), affiliate commission on shared links, Buyer Protection page
- Cart with live updates, saved addresses, coupons, shipping and tax calculation
- **Stripe Checkout** — cards, Apple Pay, Google Pay, Link (whatever you enable in Stripe)
- Order tracking, PDF invoices, one-click cancel with **automatic refund**, return requests, buy again
- Wishlist, product comparison, notifications, referral rewards, support chat

**For sellers**
- Individual and business accounts, with team members for businesses
- Dashboard: sales chart, earnings after commission, low-stock alerts, order fulfilment
- Product listing with image upload; edits to listing details go back to moderation automatically
- Brand, specifications, video, minimum/maximum order, wholesale quotes and digital files per product

**For the store owner**
- Store dashboard: revenue, commission, money owed to sellers, approvals and stock alerts
- Admin for products, orders, sellers, coupons, returns, chat, newsletter (with bulk email) and audit log
- Tiered commission, payout tracking, CSV exports
- White-label branding, announcement bar, cash-on-delivery toggle
- Categories and subcategories managed in the admin (no code changes), editable About/Terms/Privacy pages, extra pages and scheduled home-page banners

**Engineering**
- Django 6, Postgres (**Supabase**) or SQLite, S3-compatible file storage (**Supabase Storage**)
- Atomic checkout with row locking (no overselling), idempotent signed Stripe webhooks with amount checks
- Rate limiting, upload validation, private storage for seller ID documents, safe production defaults
- REST API with token sign-in and OpenAPI docs (`/api/v1/docs/`)
- Docker setup with PostgreSQL, Redis and a background scheduler
- 465+ automated tests, run on SQLite, PostgreSQL and Docker by GitHub Actions: `python manage.py test bees`

## Seller Center (/seller/)

Every individual seller and organization gets their own back office:

- **Overview** – sales, units sold, commission, earnings and average order for 7 / 30 / 90 days or 12 months (with change vs. the previous period), a sales chart, best sellers, sales by country, store health and a getting-started checklist.
- **Orders** – to pack / packed / in transit / delivered tabs, search, bulk "mark packed / handed to courier", CSV export, order page with ship-to address, courier + tracking number and a printable packing slip. Sellers get a notification and email the moment an order is ready to ship, and an alert if it is cancelled.
- **Products** – live / in review / rejected / low / out of stock, quick price and stock edits, copy a listing, units sold and revenue per product.
- **Earnings & payouts** – a ledger of every sale with the commission taken, a downloadable statement (CSV), balance (available / on the way / paid), commission tier progress and payout requests. Staff record payouts (method + reference) under Admin › Sellers; the seller is emailed.
- **Returns**, **Reviews & questions**, **Store settings** (incl. holiday mode, which hides the store's products) and **Team** for organizations (staff members can't see money pages).

`MIN_PAYOUT` in settings (default 10) is the smallest payout a seller can request. Payouts can only be requested from earnings on delivered orders.

## Seller plans (/manage/plans/)

Sellers can buy a monthly plan (Starter free, Pro $19, Business $49 by default) for a lower
commission, more products and a "Pro seller" badge. They pay by card on Stripe's page from
Seller Center > Plan; nothing renews automatically and they get a reminder 3 days before it ends.
Staff can also give a plan by hand on a seller's page (bank transfer, free trial). Edit prices,
limits and perks at /manage/plans/. Default commission for new sellers is in Store settings > Selling.

## Setup wizard (/manage/setup/)

A brand-new store opens a five-step wizard the first time the owner visits the admin: store details,
logo and colours (with live preview), commission, payments & delivery, and a launch checklist.
Run it again any time from Store settings.

## AI listing writer and smart search

The product forms have a "Write it for me" button that drafts a description from the product name
and a few key details. Add `ANTHROPIC_API_KEY=...` to `.env` to use Claude (optional `AI_MODEL`);
without a key a built-in writer is used. Search understands phrases like `hoodie under 40`,
`lamp between 20 and 50` or `cheap earbuds`, matches words in any order and plurals, and shows
close matches when nothing matches every word.

## Languages and currencies

Turn on Store settings > Language & currency. Shoppers then pick English, Arabic (right-to-left),
Spanish, French, Urdu or Roman Urdu (first visit follows the browser language), and a currency.
Add or switch on currencies at /manage/currencies/; rates update daily (or type them in).
Product prices are converted for display; cart, checkout and payments stay in STORE_CURRENCY.

## Buyer and seller messages

"Message seller" on product and store pages opens a private conversation. Sellers (and their team)
answer from Seller Center > Messages; new messages appear live, as a notification and by email.
Either side can report a conversation; staff review them at /manage/messages/.

## Deals, quantity offers, referrals, shipping and badges

- Deals: tick "Show in today's deals" and set an end time for a live countdown; ended deals drop off.
- Quantity offers: "Buy 3 or more, save 10%" on any product, applied automatically in the cart.
- Referrals: customers get a dashboard at /account/referrals/ with share buttons and rewards; any
  link with `?ref=CODE` counts. Reward sizes are in Store settings; report at /manage/referrals/.
- Shipping: zones can add a fee per extra item; product pages show delivery cost and time by country.
- Tracking: pick a courier (DHL, FedEx, UPS, USPS, Royal Mail, Aramex, TCS, Leopards…) and the customer
  gets a "Track package" button and email link; any other courier uses 17TRACK.
- Badges: staff mark a seller "Verified" after checking documents; "Top seller" is given daily
  (10+ orders in 90 days and 4.5★ from 3+ reviews, see TOP_SELLER_* settings).

## Time savers for sellers, shoppers and the owner

- **List from a photo:** on Add product, sellers snap a picture and the AI fills in the title, category,
  description and details (needs `ANTHROPIC_API_KEY`). The photo also becomes the product image.
- **Price tips:** pick a category and the form shows what similar products sell for (low, typical, high).
- **Move from another shop:** Bulk import reads Shopify, WooCommerce, Amazon, Etsy and eBay export
  files as they are: column names, sale prices, kg weights, several images and HTML descriptions
  are converted automatically.
- **Trusted sellers skip the queue:** turn on "Auto-approve trusted sellers" in Store settings > Save time.
  Verified, approved sellers with good or excellent performance then go live right away.
- **Morning summary email:** the owner gets yesterday's sales, orders to ship, open returns and items
  waiting for approval; each seller gets their own (they can switch it off in Store settings).
  Sent by `daily_tasks` (or `python manage.py send_digests`).
- **Admin search:** the box at the top of the admin finds orders (#123), emails, phones, products,
  customers and sellers in one place. An exact match opens straight away.
- **WhatsApp:** add a WhatsApp number in Store settings > Contact for "Chat on WhatsApp" buttons
  (footer, Help, My orders). Sellers can add their own for their store page. Set
  "Customer texts by" to WhatsApp to send order texts through Twilio's WhatsApp sender
  (`TWILIO_WHATSAPP_FROM`, or `TWILIO_FROM` if that number is WhatsApp-enabled).

## Sales channels, AI shopping agents and more

- **Google, Facebook, Instagram, TikTok:** Admin > Sales channels shows two feed addresses that stay in
  sync by themselves: `/feeds/google.xml` (Google Merchant Center) and `/feeds/products.csv`
  (Meta Commerce Manager and TikTok catalogs). Products have a **barcode (GTIN)** and part number field
  (also in bulk import: `gtin`, `mpn`, and Shopify's "Variant Barcode" etc.).
- **AI shopping assistants:** the store publishes a Universal Commerce Protocol profile at
  `/.well-known/ucp` and the checkout API under `/ucp/v1/checkout-sessions`. An agent (e.g. Google
  Gemini / AI Mode) builds the cart; the shopper gets a link that fills their cart and opens the normal
  checkout, so payment never goes through the agent. `/llms.txt` describes the store for AI crawlers.
  Turn agents off in Store settings > Sales channels.
- **Review requests:** 3 days after delivery customers get a "How was your order?" email (daily task
  `send_review_requests`); a verified review with a photo earns reward points (Store settings > Reviews).
- **Passkeys:** customers add a passkey under Password & security and then sign in with fingerprint,
  face or device PIN. Needs HTTPS on the real domain (or `localhost` while developing).
- **Import duty estimate:** set "Import duty %" and "Duty-free up to" per country under Tax rates;
  international shoppers see the estimate at checkout.
- **Chat translation:** buyers and sellers tap "Translate" on a message to read it in their own
  language (needs `ANTHROPIC_API_KEY`).

## Live shopping, group buys, registries and more

- **Live shopping** (`/live-shopping/`): sellers plan shows in Seller Center > Live shows, stream on
  YouTube Live, Vimeo or Twitch, and pin products under the video. Shoppers get reminders, chat live and
  buy without leaving. Followers are told when the show starts. **Shop by video** (`/videos/`) lists every
  product that has a video.
- **Group buy:** on a product set "Group buy: people" and "% back". When that many people buy through
  the same group link within 24 hours, each gets the % back as store credit after delivery
  (paid by the scheduled job `run_promotions`).
- **Pre-order:** set "Pre-order: ships on" - the product shows a Pre-order badge and delivery dates
  count from that day.
- **Spin & win** (`/spin/`, Store settings > Spin & win): one spin a day for a single-use coupon,
  free shipping or points; prizes and chances are editable.
- **Blog** (`/blog/`, Admin > Blog): posts with linked products, RSS feed and sitemap entries.
- **Gift registries** (Account > Gift registries): a shareable list for weddings, baby showers and
  birthdays; guests see what's still wanted and the owner is told when something is bought.
- **'Still looking?' emails:** signed-in shoppers who viewed products without buying get one email
  (at most weekly; daily task `send_browse_reminders`).
- **Address suggestions at checkout:** add a Google Maps Platform key (Places API (New)) under
  Keys & passwords. Requests go through the store's server, so the key stays private.
- **Filters:** material (from product specifications) and condition, next to brand, size and colour.

## Demo copy for clients

On a separate copy of the store (never your live store):

    python manage.py demo_setup        # demo shopper, seller and admin + sample orders
    # .env: DEMO_MODE=True, then reload

The sign-in page then shows one-click "Shop as a customer", "Open the Seller Center" and
"Run the marketplace" buttons. Demo accounts can't change store settings, passwords or security,
and `daily_tasks` resets them every day.

## Quick start (local)

With Docker (see [DOCKER.md](DOCKER.md)):

```bash
cp .env.example .env            # then set SECRET_KEY
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
```

Without Docker:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # DEBUG=True is already set for local use
python manage.py migrate
python manage.py seed_data      # demo products and coupons (optional)
python manage.py createsuperuser
python manage.py runserver
```

Open http://127.0.0.1:8000 — the store admin is at `/manage/`, Seller Center at `/seller/`, the API at `/api/v1/` and the Django admin at `/admin/`.

## Rebrand the store

Admin → **Site settings**:

| Section | What you set |
|---|---|
| Brand | Store name, tagline, logo (URL or upload), favicon, primary + accent colour |
| Homepage hero | Headline, subheading, optional hero image |
| Contact & social | Support email/phone, company address, social links |
| Checkout | Tax %, flat shipping fee, free-shipping threshold, cash on delivery on/off |
| Announcement bar | Site-wide message and link |

Use a **dark** primary colour (white text sits on it) and a **bright** accent colour (dark text sits on it).
Prices use the `STORE_CURRENCY` environment variable (USD by default).

## Connect Supabase

1. Create a project at https://supabase.com.
2. **Database**: Project Settings → Database → *Connection string* → URI. Put it in `DATABASE_URL`.
   Use the session pooler (port 5432) on a normal server, or the transaction pooler (port 6543) on
   serverless hosts — the settings adapt automatically.
3. **Storage**: Storage → New bucket:
   - `media` — **public** (product images, logos, store banners)
   - `private` — **private** (seller ID documents and certificates)
4. Storage → Settings → **S3 Connection**: enable it, create an access key, and fill in
   `SUPABASE_PROJECT_REF`, `SUPABASE_S3_REGION`, `SUPABASE_S3_ACCESS_KEY_ID`, `SUPABASE_S3_SECRET_ACCESS_KEY`.
5. Run `python manage.py migrate` against the new database and create a superuser.

## Connect Stripe

1. Get API keys at https://dashboard.stripe.com/test/apikeys and set `STRIPE_SECRET_KEY` /
   `STRIPE_PUBLISHABLE_KEY`. "Card, Apple Pay or Google Pay" appears at checkout automatically.
2. Developers → Webhooks → **Add endpoint** `https://YOUR-DOMAIN/payment/stripe/webhook/` with events
   `checkout.session.completed`, `checkout.session.async_payment_succeeded`,
   `checkout.session.async_payment_failed`, `checkout.session.expired`. Copy the signing secret into
   `STRIPE_WEBHOOK_SECRET`.
3. Local testing with the Stripe CLI:
   ```bash
   stripe listen --forward-to localhost:8000/payment/stripe/webhook/
   ```
   Pay with the test card `4242 4242 4242 4242`, any future date, any CVC.
4. Switch to live keys when you're ready to take real payments.

How payments work: placing a card order reserves stock and opens Stripe's hosted page. The webhook
confirms the exact amount and marks the order paid. Abandoned payments expire after 30 minutes and the
stock is returned. As a safety net, schedule `python manage.py release_unpaid_orders` every 15–30 minutes.

## Deploy

Any host that runs Python works (Render, Railway, Fly.io, Heroku, a VPS). Minimum production settings:

```
DEBUG=False
SECRET_KEY=<long random string>
ALLOWED_HOSTS=shop.example.com
CSRF_TRUSTED_ORIGINS=https://shop.example.com
NUM_PROXIES=1
DATABASE_URL=...            # Supabase
SUPABASE_...                # storage keys
STRIPE_...                  # payment keys
EMAIL_HOST=...              # transactional email
```

Build / start commands:

```bash
pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate
gunicorn backend.wsgi --workers 3
```

With more than one worker, also set `REDIS_URL` so rate limits are shared. `python manage.py check --deploy`
should report no issues.

## Using Netlify as the front door

Netlify can't run Python, but it can sit in front of the Django host. `netlify.toml` and
`netlify/edge-functions/proxy.ts` forward every request to the backend so visitors only see the
Netlify domain.

1. Host Django on a Python platform (PythonAnywhere free tier works).
2. In Netlify, set the environment variable `BACKEND_URL=https://YOUR-USERNAME.pythonanywhere.com` and deploy.
3. On the backend, add the Netlify domain:
   ```
   ALLOWED_HOSTS=YOUR-SITE.netlify.app,YOUR-USERNAME.pythonanywhere.com
   CSRF_TRUSTED_ORIGINS=https://YOUR-SITE.netlify.app
   USE_X_FORWARDED_HOST=True
   NUM_PROXIES=2
   ```

## Project layout

```
backend/              settings, URLs, WSGI
bees/                 the marketplace app
  views.py            storefront, cart, checkout, orders
  seller_views.py     Seller Center        manage_views.py  store admin
  api/                REST API v1          payments.py      Stripe (paypal.py: PayPal)
  fulfilment.py       shipping rules       bulk.py          spreadsheet import/export
  models.py           all database models  templates/bees/  every page (design tokens in base.html)
  management/         scheduled jobs and maintenance commands
deploy/docker/        container entrypoint and scheduler
```

The full code map is in [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md).

## Tests

```bash
python manage.py test bees
```

Covers order totals, shipping/tax, coupons, stock locking, Stripe and PayPal flows (mocked), refunds,
payouts, fulfilment, the REST API, permissions and staff roles, rate limiting, open-redirect protection,
upload validation, translations and white-label rendering. CI runs it on SQLite and PostgreSQL and boots
the Docker stack on every pull request.

## Daily maintenance (PythonAnywhere scheduled task)

One command does everything the store needs once a day — cancel unpaid
card orders, send cart reminders, back up the database and images, and
clear old sessions:

```
cd ~/Lumen-Market && venv/bin/python manage.py daily_tasks
```

On PythonAnywhere: **Tasks** tab → *Scheduled tasks* → paste the line above,
pick a time (e.g. 03:00) → **Create**.

Backups go to the Supabase `private` bucket (folder `backups/`) when
Supabase Storage is configured, otherwise to `backups/` next to
`manage.py`. The newest 14 are kept.

```
python manage.py restore_backup --list     # see backups
python manage.py restore_backup            # restore the newest (then Reload the web app)
```

## Two-step sign-in

Staff must set up an authenticator app before opening the store admin
(Store settings → Security). If someone loses their phone *and* backup
codes:

```
python manage.py reset_two_factor <username or email>
```
