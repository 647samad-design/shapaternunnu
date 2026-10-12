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

## 6. Tax & legal

- Store settings > Taxes & duties: tax-inclusive prices (EU/UK/AU) or tax added at checkout (US), your VAT number, duties policy.
- Store settings > Tax rates: "Add common rates", then check them with an accountant.
- Store settings > Privacy, analytics & search: cookie banner (keep it on for EU/UK visitors), Google Analytics / Meta Pixel IDs, Google Search Console code.
- Update Terms and Privacy text with your company name and address.

## 7. Search engines

- Verify the site in Google Search Console and submit `https://yourdomain.com/sitemap.xml`
  (includes every product with images and all language versions).
- Each product can have its own SEO title/description (product form > Google search listing).
