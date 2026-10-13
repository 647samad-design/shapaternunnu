# Ownership, IP and handover

This document is for the buyer of the Lumen Market platform and their
technical and legal advisers. It lists what is included, who owns what, and
the steps to hand the platform over safely.

## 1. What is included

| Item | Where |
|---|---|
| Full source code of the platform (shop, Seller Center, store admin, REST API, background jobs) | This repository |
| Database design and all migrations | `bees/models.py`, `bees/migrations/` |
| Templates, styles and scripts (no front-end build step) | `bees/templates/` |
| Automated tests (400+) and CI pipeline (SQLite, PostgreSQL, Docker) | `bees/tests.py`, `.github/workflows/tests.yml` |
| Docker setup (web, scheduler, PostgreSQL, Redis-compatible cache) | `Dockerfile`, `docker-compose.yml`, `deploy/docker/` |
| Documentation | `README.md`, `DEVELOPER_GUIDE.md`, `DOCKER.md`, `DEPLOY.md`, `API.md`, `QUALITY.md`, this file |
| REST API with OpenAPI 3 schema and interactive docs | `bees/api/`, `/api/v1/docs/` |
| Translations (English, Urdu, Roman Urdu, Arabic, Spanish, French) | `bees/i18n_pack_data.py`, `bees/translations*.py` |
| Platform overview for clients (PDF) | Provided separately |

## 2. Ownership

* The platform is proprietary software of the copyright holder named in
  `LICENSE`. Rights pass to the buyer only through the signed sale agreement.
* The open-source parts it uses keep their own licences
  (`THIRD_PARTY_NOTICES.md`). All are permissive (BSD, MIT, Apache-2.0,
  PSF, MPL-2.0 unmodified, LGPL library unmodified) and allow commercial,
  closed-source use. No component requires publishing the platform's code.
* Demo product pictures are drawn by the platform's own code; there are no
  stock photos or third-party images to license.
* "Lumen Market" is a demo brand name. Before trading under any name, the
  buyer should run their own trademark search; the platform is fully
  white-label (name, logo, colours, texts are set in the admin).

## 3. Not included (each owner uses their own accounts)

Payment, email, sign-in, SMS, courier, storage, AI and monitoring accounts
(Stripe, PayPal, SMTP provider, Google, Apple, Twilio, EasyPost/Shippo,
Supabase/S3, Anthropic, Sentry) belong to whoever operates the store. The
buyer creates their own and enters the keys in **Store admin > Keys &
passwords**. No customer data, orders or payment data are part of the code.

## 4. Security state at handover

* No passwords, API keys or tokens are stored in the repository or its git
  history (scanned for Stripe, GitHub, AWS and Anthropic key patterns; `.env`,
  databases, uploads and backups are git-ignored).
* Production defaults are safe: debug off, HTTPS-only cookies and HSTS,
  Content-Security-Policy, rate limits, two-step sign-in for staff.
* Audit results: `QUALITY.md` (security, reliability, money, legal, speed;
  accessibility WCAG 2.1 AA; load test).

## 5. Handover steps

1. **Repository**: transfer the GitHub repository to the buyer's account
   (GitHub > Settings > Transfer ownership), or deliver a clean archive.
   The buyer then removes the seller's access.
2. **Secrets**: the buyer generates a new `SECRET_KEY` and enters their own
   service keys. Any key ever used by the seller is revoked on the seller's side.
3. **Hosting**: deploy with Docker (`DOCKER.md`) or any Python host
   (`DEPLOY.md`). Run `python manage.py check --deploy` and the store admin's
   **System check** - everything should be green.
4. **Domain and email**: point the buyer's domain, set up a sending domain with
   SPF/DKIM, and test with System check > "Send test email".
5. **Payments**: connect Stripe/PayPal in live mode and place one real test
   order end to end (pay, ship, deliver, refund).
6. **Legal pages**: Terms, Privacy and Seller terms are generated from the
   store settings; the buyer's lawyer reviews them for the countries served.
7. **Demo copy**: `DEMO_MODE=False` on the live store; keep a separate demo
   or staging copy if wanted (`STAGING=True`).
8. **Knowledge transfer**: walkthrough of the three portals and
   `DEVELOPER_GUIDE.md`, plus the agreed support period.

## 6. Verifying the code yourself

```bash
pip install -r requirements.txt
python manage.py test bees --parallel auto         # 400+ tests
docker compose up -d --build                       # full stack in containers
curl http://localhost:8000/health/                 # {"ok": true}
open http://localhost:8000/api/v1/docs/            # REST API documentation
```
