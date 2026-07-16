#!/usr/bin/env sh
set -eu

: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${APP_DB_USER:?APP_DB_USER is required}"
: "${APP_DB_PASSWORD:?APP_DB_PASSWORD is required}"

if [ "$APP_DB_USER" = "$POSTGRES_USER" ]; then
  echo "APP_DB_USER must not be the same role as POSTGRES_USER" >&2
  exit 1
fi

psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set ON_ERROR_STOP=1 \
  --set app_db="$POSTGRES_DB" \
  --set app_user="$APP_DB_USER" \
  --set app_password="$APP_DB_PASSWORD" <<'SQL'
SELECT set_config('pve_helper.app_user', :'app_user', false);
SELECT set_config('pve_helper.app_password', :'app_password', false);

DO $$
DECLARE
    app_user text := current_setting('pve_helper.app_user');
    app_password text := current_setting('pve_helper.app_password');
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = app_user) THEN
        EXECUTE format(
            'ALTER ROLE %I WITH LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION',
            app_user,
            app_password
        );
    ELSE
        EXECUTE format(
            'CREATE ROLE %I WITH LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION',
            app_user,
            app_password
        );
    END IF;
END
$$;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE :"app_db" TO :"app_user";
GRANT USAGE, CREATE ON SCHEMA public TO :"app_user";
SQL
