# Lumen Market - production image
#
#   docker compose up -d          (web + scheduler + Postgres + Redis)
#
# The same image runs the web server ("web", the default) and the background
# scheduler ("scheduler"). See DOCKER.md for every option.

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# curl is only used by the container health check.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Run as an unprivileged user; uploads, backups and caches live in volumes.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p config media private_media backups staticfiles .cache .live-cache \
    && chmod +x deploy/docker/entrypoint.sh \
    && chown -R app:app /app
USER app

# Static files are baked into the image and served by WhiteNoise.
RUN SECRET_KEY=build-only DEBUG=False python manage.py collectstatic --noinput

EXPOSE 8000

# (docker-compose.yml turns this off for the scheduler container.)
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health/ || exit 1

ENTRYPOINT ["deploy/docker/entrypoint.sh"]
CMD ["web"]
