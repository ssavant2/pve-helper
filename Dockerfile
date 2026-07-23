FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6 AS base

ARG RELEASE_VERSION=DEV

LABEL org.opencontainers.image.source="https://github.com/ssavant2/pve-helper" \
      org.opencontainers.image.licenses="AGPL-3.0-only"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_VERSION=${RELEASE_VERSION}

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

RUN apt-get update \
    && apt-get install -y --no-install-recommends qemu-utils acl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc /uv /usr/local/bin/uv

COPY requirements.txt .
RUN uv pip install --system --require-hashes -r requirements.txt

EXPOSE 8000

CMD ["sh", "-c", "gunicorn pve_helper.wsgi:application --bind 0.0.0.0:8000 --workers ${GUNICORN_WORKERS:-2} --timeout ${GUNICORN_TIMEOUT:-86400} --no-sendfile --access-logfile - --error-logfile - --no-control-socket"]

FROM base AS test

COPY --chown=app:app . .
RUN mkdir -p /app/staticfiles \
    && chown -R app:app /app

USER app

RUN APP_SECRET_KEY=build-time-placeholder DEBUG=true python manage.py collectstatic --noinput

FROM busybox:1.38@sha256:fd8d9aa63ba2f0982b5304e1ee8d3b90a210bc1ffb5314d980eb6962f1a9715d AS runtime-source

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
