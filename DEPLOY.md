# Going live internationally

A checklist for moving from the free test setup to a production store.

## 1. Hosting (needed for PayPal, couriers, AI and Postgres)

PythonAnywhere's free plan blocks most outside connections. Pick one:

| Option | Cost (approx.) | Notes |
|---|---|---|
| PythonAnywhere "Hacker" or higher | ~$5-12 / month | Same setup you have now, outside connections allowed. Easiest. |
| Render / Railway / Fly.io | ~$7-25 / month | Git push to deploy. Add a Postgres add-on. |
| VPS (Hetzner, DigitalOcean, Contabo) | ~$5-15 / month | Full control: Nginx + Gunicorn + Postgres + Redis. |

## 2. Database: Postgres

1. Create a Postgres database (Supabase, Neon, Render, or on the VPS).
2. Set `DATABASE_URL=postgresql://user:password@host:5432/dbname` in `.env`
   (Supabase: use the *session pooler* string).
3. Move your data:
   ```bash
   # on the old server
   python manage.py dumpdata --natural-foreign --natural-primary -e contenttypes -e auth.permission -o data.json
   # with DATABASE_URL pointing at the new database
   python manage.py migrate
   python manage.py loaddata data.json
   ```
4. Several web workers? Set `REDIS_URL` so rate limits and live chat are shared.

## 3. Backups

`daily_tasks` already backs up every night. With Supabase Storage keys set, the
archives go to the private bucket (off the server) - recommended. Restore with
`python manage.py restore_backup`.

## 4. Speed worldwide (CDN)

1. Put Cloudflare (free) or BunnyCDN in front of your domain.
2. Optional: serve static files from a CDN host - set `CDN_URL=https://cdn.yourdomain.com`
   (Store settings > Keys & passwords > Website).
3. Product images already load lazily with fixed sizes; the main product image is preloaded.

## 5. Payments

- **Stripe** (cards, Apple Pay, Google Pay): needs a company in a Stripe-supported country.
- **PayPal**: add Client ID / Secret / Webhook ID in Keys & passwords. Store currency must be one PayPal supports (USD, EUR, GBP, AED is *not* supported - use USD).
- **Cash on delivery** works everywhere.
- **Charge in the shopper's currency** (Store settings > Language & currency): card/PayPal payments are taken in the currency the shopper picked, at the rate in Currencies. Stripe and PayPal convert it back to your currency; 3-decimal currencies (KWD, BHD, OMR, JOD) are charged in the store currency.

## 6. Tax & legal

- Store settings > Taxes & duties: tax-inclusive prices (EU/UK/AU) or tax added at checkout (US), your VAT number, duties policy.
- Store settings > Tax rates: "Add common rates", then check them with an accountant.
- Store settings > Privacy, analytics & search: cookie banner (keep it on for EU/UK visitors), Google Analytics / Meta Pixel IDs, Google Search Console code.
- Update Terms and Privacy text with your company name and address.

## 7. Search engines

- Verify the site in Google Search Console and submit `https://yourdomain.com/sitemap.xml`
  (includes every product with images and all language versions).
- Each product can have its own SEO title/description (product form > Google search listing).

## 8. Sign in with Google / Apple
All keys go in **Admin > Keys & passwords** (the page shows the exact redirect URLs to copy).
- **Google**: console.cloud.google.com > APIs & Services > Credentials > *Create OAuth client ID* (Web application). Authorised redirect URI: `https://YOUR-DOMAIN/login/callback/google/`. Paste the Client ID and Client secret. Also fill the *OAuth consent screen* (app name, logo, privacy link) and publish it.
- **Apple** (needs an Apple Developer account): create a *Services ID* with "Sign in with Apple", add your domain and the return URL `https://YOUR-DOMAIN/login/callback/apple/`; create a *Key* with Sign in with Apple and download the `.p8` file. Paste Services ID, Team ID, Key ID and the whole `.p8` text.
- Accounts are linked by verified email, and people who use two-step sign-in still have to type their code.

## 9. Text messages (Twilio SMS)
twilio.com > Console: copy **Account SID** and **Auth Token**; buy a phone number (or create a *Messaging Service*, `MG...`, or an approved alphanumeric sender like `LUMEN`). Add the three values in Keys & passwords. For Pakistan and some other countries Twilio needs a registered sender ID - check *Messaging > Regulatory compliance*.
Then in Store settings: *Text messages (SMS)* (order updates, seller alerts) and *Cash on delivery* (phone code check, largest cash order, block after N refused parcels).

## 10. Seller payouts
- **PayPal Payouts**: in your PayPal business account ask PayPal to enable *Payouts* (developer.paypal.com > Apps > your app > Features > Payouts). Uses the same PayPal keys. Add the webhook events `PAYMENT.PAYOUTS-ITEM.*` to the PayPal webhook you already made.
- **Stripe Connect**: dashboard.stripe.com > Connect > *Get started* (choose "Express"). Uses the same Stripe keys. Stripe Connect isn't available to sellers in every country (e.g. not Pakistan) - those sellers use PayPal or bank transfer.
- Store settings > *Seller payouts*: Off / every Monday / every day, plus the minimum amount. Payouts run with the daily task; only money past the return window is ever sent.

## 11. After updating
```
git pull
pip install -r requirements.txt     # PyJWT[crypto] and openpyxl (added in the pro pack)
python manage.py migrate
python manage.py collectstatic --noinput
```
Then press **Reload** on the Web tab. The daily task (`manage.py daily_tasks`) now also sends automatic payouts.

## 12. Store team
Admin > **Team**: add staff by email and give each a role (Manager, Orders & shipping, Customer support, Products & sellers, Finance, Marketing). They only see their part of the admin; only the owner (superuser) can open Keys & passwords and Team. Turn on "Staff must use two-step sign-in" in Store settings > Security.

## 13. Phone notifications (web push)
Keys & passwords > *Phone notifications*: tick **Create push keys**, enter a contact email and save. Customers and sellers then get a "Notify me on this device" button (Notifications page, Privacy page, messages). Every site notification (orders, messages, payouts) is also sent to their phone/computer. Free; works in Chrome, Edge, Firefox, Samsung Internet and Safari (iPhone: after "Add to Home Screen").

## 14. Automatic tests on GitHub
`.github/workflows/tests.yml` runs all tests on every pull request and on main. In GitHub: Settings > Branches > add a rule for `main` with "Require status checks to pass" (pick *Tests*) so nothing broken can be merged.

## 15. Test site (staging)
A second copy of the store to try changes before they go live:
1. PythonAnywhere > Web > *Add a new web app* (e.g. `yourname-test.pythonanywhere.com` or `test.yourdomain.com`), pointing at a second folder: `git clone ... ~/Lumen-Market-test`.
2. Its own `.env` with: `STAGING=True`, `STAGING_PASSWORD=<a password>`, `STAGING_EMAIL_TO=<your inbox>`, Stripe **test** keys (`sk_test_...`), `PAYPAL_MODE=sandbox`, and a separate database (its own `db.sqlite3` or a second Postgres database). Never reuse the live database URL.
3. To test with real-looking data: copy the live database into the test one, then run `python manage.py anonymize_data --yes` there (it refuses to run unless `STAGING=True`).
4. Deploy a branch there first (`git checkout <branch> && git pull && python manage.py migrate`), check it, then merge the PR and update the live site.
On the test site every email goes to `STAGING_EMAIL_TO` only, SMS and seller payouts are off, search engines are blocked and a yellow "TEST SITE" bar is shown.

## 16. Marketing tools
- **Email campaigns** (Admin > Email campaigns): to newsletter subscribers only, with unsubscribe links, test send, preview, opens and clicks.
- **Sponsored products**: sellers pay per click from their earnings (Seller Center > Sponsored). Store settings > *Sponsored products* sets the lowest bid and how many places show.
- **Promotions**: sellers create their own discount codes and timed sales (Seller Center > Promotions); the store runs sales for its own products in Admin > Coupons > Timed sales.

## 17. After the audit - settings to check
- **NUM_PROXIES**: if System check says every visitor looks like the same address, add `NUM_PROXIES=1` to .env.
- **TIME_ZONE**: your store's time zone (Keys & passwords > Store time zone), e.g. `Asia/Karachi`.
- **Uptime monitoring**: add `https://YOUR-DOMAIN/health/` to UptimeRobot or Better Stack (free) to get an alert if the site goes down.
- **More traffic**: with several servers or heavy traffic, add Redis (`REDIS_URL`) so the cache is shared and faster.
- **Legal pages**: Terms and Privacy now describe every feature, but have a lawyer check them for the countries you sell in.
