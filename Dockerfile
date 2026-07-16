FROM python:3.14-slim AS base

LABEL org.opencontainers.image.source="https://github.com/ssavant2/pve-helper" \
      org.opencontainers.image.licenses="AGPL-3.0-only"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

RUN apt-get update \
    && apt-get install -y --no-install-recommends qemu-utils acl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY requirements.txt .
RUN uv pip install --system -r requirements.txt

EXPOSE 8000

CMD ["sh", "-c", "gunicorn pve_helper.wsgi:application --bind 0.0.0.0:8000 --workers ${GUNICORN_WORKERS:-2} --timeout ${GUNICORN_TIMEOUT:-86400} --no-sendfile --access-logfile - --error-logfile - --no-control-socket"]

FROM base AS test

COPY --chown=app:app . .
RUN mkdir -p /app/staticfiles \
    && chown -R app:app /app

USER app

RUN APP_SECRET_KEY=build-time-placeholder DEBUG=true python manage.py collectstatic --noinput

FROM busybox:1.37 AS runtime-source

WORKDIR /src
COPY console_app console_app
COPY core core
COPY pve_helper pve_helper
COPY scripts/worker_healthcheck.py scripts/worker_healthcheck.py
COPY static static
COPY templates templates
COPY manage.py LICENSE NOTICE.md requirements.txt ./
RUN rm -f core/tests*.py pve_helper/test_settings.py

FROM base AS runtime

COPY --from=runtime-source --chown=app:app /src/ .
RUN mkdir -p /app/staticfiles \
    && chown -R app:app /app

USER app

RUN APP_SECRET_KEY=build-time-placeholder DEBUG=true python manage.py collectstatic --noinput
