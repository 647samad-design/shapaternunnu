#!/bin/sh
# Starts one role of Lumen Market inside the container.
#   web        apply database updates, then serve the site with gunicorn (default)
#   scheduler  run the background jobs (unpaid orders, promotions, daily tasks)
#   anything else is run as a command, e.g. "python manage.py createsuperuser"
set -e

wait_for_database() {
    python - <<'PY'
import os, sys, time
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
django.setup()
from django.db import connection
for attempt in range(60):
    try:
        connection.ensure_connection()
        sys.exit(0)
    except Exception as exc:
        print(f"Waiting for the database... ({exc.__class__.__name__})", flush=True)
        time.sleep(2)
print("The database did not answer after 2 minutes.", file=sys.stderr)
sys.exit(1)
PY
}

wait_for_migrations() {
    # The web container applies database updates; jobs wait until it's done.
    tries=0
    until python manage.py migrate --check >/dev/null 2>&1; do
        tries=$((tries + 1))
        if [ "$tries" -ge 100 ]; then
            echo "Database updates still missing after 5 minutes - is the web container running?" >&2
            return 1
        fi
        echo "Waiting for the web container to apply database updates..."
        sleep 3
    done
}

case "${1:-web}" in
    web)
        wait_for_database
        if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
            python manage.py migrate --noinput
        fi
        exec gunicorn backend.wsgi \
            --bind "0.0.0.0:${PORT:-8000}" \
            --workers "${WEB_WORKERS:-3}" \
            --timeout "${WEB_TIMEOUT:-60}" \
            --access-logfile - --error-logfile -
        ;;
    scheduler)
        wait_for_database
        wait_for_migrations
        exec python deploy/docker/scheduler.py
        ;;
    *)
        exec "$@"
        ;;
esac
