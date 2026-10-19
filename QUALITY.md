# Quality checks

## Accessibility (WCAG 2.1 AA)
Checked with axe-core 4.10 in Chromium on 37 pages (shop, account, checkout, Seller Center and admin), in light mode at 1280px and dark mode on a 390px phone screen.

| Found | Fixed |
|---|---|
| Green "ok" and amber "warning" labels were slightly too light (contrast 4.2-4.4:1) | Darker colours, now 5.1:1 or better |
| On phones the sign-in icon had no name for screen readers | Added a label |
| Wide tables that scroll sideways couldn't be scrolled with the keyboard | They can now be focused and scrolled with the arrow keys |

Result after the fixes: **0 violations** on all 37 pages, in both modes. The site also has a "Skip to content" link, visible focus outlines, labelled form fields and right-to-left layout for Urdu and Arabic.

## Load test
Production setup (gunicorn, 3 workers, DEBUG off, SQLite) on a small 2-CPU machine. The load-test program ran on the **same** machine and used part of the CPU. Simulated shoppers browsed the homepage, products, categories, search, cart, robots.txt and the sitemap without pausing.

| Shoppers at once | Requests / second | Typical page (median) | Slow pages (95th %) | Errors |
|---|---|---|---|---|
| 10 | 75 | 129 ms | 181 ms | 0 |
| 30 | 75 | 397 ms | 470 ms | 0 |
| 60 | 72 | 816 ms | 939 ms | 0 |

- About 75 pages per second is roughly 6 million page views a day. Real visitors pause between pages, so this handles a few hundred people browsing at once.
- Server time per page with no load: home 19 ms, product 32 ms, search 16 ms, cart 5 ms. Product pages went from 28 database queries to 14 (ratings are now loaded in one query).
- For more traffic: add workers or CPU, move to Postgres (see DEPLOY.md) and put the CDN in front for images and static files.

## Senior code audit (October 2026)
Reviewed with Django's deploy checks, the Bandit security scanner, a manual read of the risky areas (payments, refunds, payouts, sign-in, uploads, outgoing requests, permissions) and profiling.

| Area | Problem found | Fix |
|---|---|---|
| Reliability | Without Redis the cache lived inside each web worker, so Google/Apple sign-in, 2-step codes, bulk-import previews, rate limits and settings changes could randomly "expire" or lag when the next request hit another worker | Shared file cache for all workers (Redis still recommended for several servers); settings kept 5 s in memory per worker |
| Security | Push-notification addresses could point at internal servers (SSRF) | Only real browser push services are accepted |
| Security | Opening the cash-order email link confirmed the order - email virus scanners open links by themselves, which skipped the fake-order check | The link shows a button; only the customer's click confirms |
| Security | Changing the account email didn't need the password (stolen session -> account takeover) | Current password required; old address is still alerted |
| Security | No Content-Security-Policy | CSP (no plugins, no framing, no foreign scripts except the analytics you turn on), Permissions-Policy |
| Security | Coupon codes, reviews and messages had no rate limits | Limits added (coupon guessing, review spam, message spam) |
| Security | Rate limits could treat every visitor as one when the host's proxy hides real addresses | System check warns and tells you to set NUM_PROXIES=1 |
| Money | A script without cookies could click a competitor's ad again and again | One paid click per campaign per address per day, max 20 paid clicks per address per day |
| Money | Unpaid card/PayPal orders held stock until the daily task | Released every 15 minutes |
| Legal | Seller sign-up had no terms acceptance, duplicate store names were allowed, commission text was hard-coded | Store name unique, country from list, terms accepted and recorded with time; real commission shown |
| Legal | Terms said "14 days" and "Stripe" regardless of settings, no seller/gift card/sponsored terms; privacy page missed SMS, sign-in and statistics | Pages now follow the store settings and cover every feature (have a lawyer review them for your country) |
| Legal | No way to report illegal/fake listings (marketplace laws such as the EU DSA expect one) | "Report this listing" + admin queue; reporters and sellers are told the decision |
| Data | Checkout accepted any two letters as the country and empty phone numbers | Country must be from the list; phone must be callable |
| Data | Sale start/end times were always in UTC | Store time zone setting (TIME_ZONE) shown next to the fields |
| Speed | Every page view wrote statistics and ad impressions to the database | Counted in the cache and written every few minutes |
| Speed | Sponsored products checked budgets and earnings on every page | Eligible ads cached for a minute (clicks still re-check exactly) |
| Speed | Reports loaded every order object; My orders loaded all orders at once | Reports use light rows; My orders has pages |
| Database | Missing indexes for email lookups and cash-order checks | Indexes added |
| Upkeep | Old notifications and abandoned gift card checkouts kept growing | Cleaned up daily |
| Monitoring | No health URL for uptime monitors | /health/ returns 200 when the site and database answer |

## Sale-readiness review (October 2026)
The whole test suite now also runs on **PostgreSQL 16** and the full **Docker** stack is built and started on every change (GitHub Actions). Doing that found three problems that only showed up outside SQLite:

| Area | Problem found | Fix |
|---|---|---|
| Orders (PostgreSQL) | Shipping items and refunding returns failed on PostgreSQL ("FOR UPDATE cannot be applied to the nullable side of an outer join"), so sellers couldn't mark orders shipped | Row locks now lock only the item row (`select_for_update(of=("self",))`); the 18 affected tests pass on PostgreSQL |
| Cache | Setting `REDIS_URL` crashed every page: the `redis` package was missing from requirements | Added; tested with a real Redis-compatible server |
| Database | A Postgres server in the same Docker network was forced to use SSL and refused the connection | SSL is decided by host (on for remote databases, off for local/Docker), or set with `DATABASE_SSL` |

New in this round: REST API v1 with hashed tokens, two-step sign-in support, rate limits and OpenAPI docs; Docker setup (web, scheduler, PostgreSQL, Valkey/Redis); developer guide, handover and licence documents. Tests: 422 passing on SQLite and PostgreSQL.

Competitor-parity round (compared with Dokan, CS-Cart Multi-Vendor, Yo!Kart and Sharetribe): admin-managed categories with subcategories, editable pages and home banners, brand/size/colour filters, product specifications, video, size guides, EU GPSR safety fields, minimum/maximum order quantities, digital downloads, request-a-quote (RFQ) and store following. Tests: 443 passing.

Big-marketplace round (compared with Amazon, eBay, Etsy, Daraz, AliExpress/Temu, Noon, Walmart): item condition and ships-from, other sellers of the same product, make an offer, auctions, personalization, gift wrap and message, collection points, voucher centre with free-shipping vouchers, membership, daily check-in coins, Subscribe & Save, frequently bought together, save for later, multiple and shared wishlists, review photos and helpful votes, photo search, BNPL message, EU Omnibus lowest price and DSA trader information. Tests: 462 passing; layout audit 0 issues.
