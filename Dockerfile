FROM python:3.14-slim

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

COPY --chown=app:app . .
RUN mkdir -p /app/staticfiles \
    && chown -R app:app /app

USER app

RUN APP_SECRET_KEY=build-time-placeholder DEBUG=true python manage.py collectstatic --noinput

EXPOSE 8000

CMD ["sh", "-c", "gunicorn pve_helper.wsgi:application --bind 0.0.0.0:8000 --workers ${GUNICORN_WORKERS:-2} --timeout ${GUNICORN_TIMEOUT:-86400} --no-sendfile --access-logfile - --error-logfile - --no-control-socket"]
