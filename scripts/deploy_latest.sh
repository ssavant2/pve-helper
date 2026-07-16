#!/usr/bin/env sh
set -eu

docker compose pull web console worker worker-bulk
docker compose up -d --wait db
docker compose stop nginx web console worker worker-bulk >/dev/null 2>&1 || true
docker compose run --rm --no-deps web python manage.py migrate --noinput
docker compose up -d --remove-orphans --wait
docker compose ps
