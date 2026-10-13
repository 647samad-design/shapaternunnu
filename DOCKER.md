# Running Lumen Market with Docker

One command starts the whole platform: the website, the background scheduler,
PostgreSQL and Redis. It works the same on a laptop, a VPS (DigitalOcean,
Hetzner, AWS EC2, Azure, Google Cloud) or any container host.

```
docker compose up -d --build
```

| Container   | What it does |
|-------------|--------------|
| `web`       | The shop, Seller Center, store admin and API (gunicorn). Applies database updates on start. Port 8000. |
| `scheduler` | Background jobs: timed sales and email campaigns every 5 min, releasing unpaid orders every 15 min, `daily_tasks` once a day (order automation, reminders, seller payouts, badges, currency rates, backups). |
| `db`        | PostgreSQL 16. Data in the `pgdata` volume. |
| `redis`     | Shared cache for sign-in codes, rate limits and settings across all web workers (Valkey, the BSD-licensed Redis-compatible server). |

Requirements: Docker Engine 24+ with Docker Compose v2.24+ (included in Docker Desktop).

---

## 1. First start (about 5 minutes)

```bash
git clone <your repository> lumen-market && cd lumen-market
cp .env.example .env
```

Open `.env` and set at least:

```
SECRET_KEY=<paste a long random string>
```

Create one with `python3 -c "import secrets; print(secrets.token_urlsafe(50))"`.
Leave `DATABASE_URL` empty: Docker sets it for you.

Then:

```bash
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
```

Open http://localhost:8000. Sign in with the account you just created; the
store admin is at http://localhost:8000/manage/ and opens the setup wizard
the first time.

Optional demo content (products with pictures, demo shopper/seller/admin):

```bash
docker compose exec web python manage.py seed_data
docker compose exec web python manage.py demo_setup
```

## 2. Going live on a server

1. Point your domain (e.g. `shop.example.com`) at the server.
2. Put an HTTPS proxy in front of port 8000. The simplest is Caddy, which gets
   certificates automatically. `/etc/caddy/Caddyfile`:
   ```
   shop.example.com {
       reverse_proxy 127.0.0.1:8000
   }
   ```
3. In `.env`:
   ```
   DEBUG=False
   ALLOWED_HOSTS=shop.example.com
   CSRF_TRUSTED_ORIGINS=https://shop.example.com
   SECURE_SSL_REDIRECT=True
   NUM_PROXIES=1
   POSTGRES_PASSWORD=<a long random password>
   ```
   Set `POSTGRES_PASSWORD` **before the first start**: Postgres only reads it
   when the database volume is created.
4. `docker compose up -d --build`
5. Add your keys (Stripe, PayPal, email, Google sign-in, Twilio...) in
   **Store admin > Store settings > Keys & passwords**, or in `.env`.
   Then run **System check** in the admin: every item should be green.

Keys saved on the Keys & passwords page are stored in the `config` volume
(`/app/config/.env`), survive updates and rebuilds, and take effect at once
(the web workers restart gracefully). Values in the server's `.env` file
always win over the page, so keep each key in one place: either the page
(recommended) or `.env`.

## 3. Everyday commands

| Task | Command |
|------|---------|
| See what's running | `docker compose ps` |
| Follow the logs | `docker compose logs -f web scheduler` |
| Update to a new version | `git pull && docker compose up -d --build` (database updates apply automatically) |
| Django shell | `docker compose exec web python manage.py shell` |
| Run the tests | `docker compose exec web python manage.py test bees` |
| Stop everything | `docker compose down` (data is kept in volumes; `down -v` deletes all data) |
| Restart one part | `docker compose restart web` |

## 4. Backups

`daily_tasks` makes a backup every night (database export + uploaded images)
and keeps the newest 14. With Supabase / S3 storage configured, backups go to
the private bucket; otherwise they're in the `backups` volume.

Extra full database dump (recommended weekly, kept off the server):

```bash
docker compose exec -T db pg_dump -U lumen lumen | gzip > lumen-$(date +%F).sql.gz
```

Restore a dump into an empty database:

```bash
gunzip -c lumen-2026-10-01.sql.gz | docker compose exec -T db psql -U lumen lumen
```

## 5. Settings you can change

All in `.env` (or the environment):

| Variable | Default | Meaning |
|----------|---------|---------|
| `WEB_PORT` | 8000 | Port on the host |
| `WEB_WORKERS` | 3 | gunicorn workers (about 2 x CPU cores + 1) |
| `WEB_TIMEOUT` | 60 | Seconds before a slow request is stopped |
| `RUN_MIGRATIONS` | 1 | Apply database updates when `web` starts |
| `DAILY_TASKS_HOUR` | 3 | Hour (UTC) for the daily jobs |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | lumen / lumen / lumen-local-password | Database name and login |
| `DATABASE_SSL` | False in Docker | Use True with a hosted database (Supabase, RDS) |
| `ENV_FILE` | /app/config/.env | Where the admin's Keys & passwords page saves keys |

### Using a hosted database instead (Supabase, Amazon RDS, Azure...)

Set `DATABASE_URL` in `docker-compose.yml` (under `x-app > environment`) to the
provider's connection string, set `DATABASE_SSL: "True"`, and remove the `db`
service and its `depends_on` entry.

### Images on S3 / Supabase Storage

Fill in the `SUPABASE_*` keys (see `.env.example`). Uploaded images then live
in the bucket instead of the `media` volume, which is best when you run more
than one server.

## 6. Scaling up

* More traffic on one server: raise `WEB_WORKERS`.
* Several servers: use a hosted Postgres, Redis and S3 storage, run `web` on
  each server behind a load balancer, and run **one** `scheduler` in total.
* A CDN (Cloudflare) in front of the site speeds up images and pages worldwide.

## 7. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Set SECRET_KEY in .env` when starting | Add `SECRET_KEY=...` to `.env` |
| "Waiting for the database..." for a long time | `docker compose logs db`; check `POSTGRES_PASSWORD` didn't change after the first start |
| Site redirects to https on your laptop | `SECURE_SSL_REDIRECT=False` (the default in docker-compose.yml) |
| "Bad Request (400)" | Add the domain to `ALLOWED_HOSTS` |
| Forms say "CSRF verification failed" | Add `https://your-domain` to `CSRF_TRUSTED_ORIGINS` |
| Health check | `curl http://localhost:8000/health/` returns `{"ok": true}` |

The Docker setup is tested on every change by GitHub Actions
(`.github/workflows/tests.yml`, job "Docker"): it builds the image, starts all
four containers, loads the main pages and the API, checks the scheduler and
creates an admin account.
