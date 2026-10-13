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
