# PostgreSQL role hardening

The application should not connect to PostgreSQL as the bootstrap/admin role.

Use two database roles:

- `DB_ADMIN_USER`: bootstrap/admin role used by the Postgres container and
  healthcheck. This role is not passed to `web` or `worker`.
- `DB_USER`: app role used by Django. This role can connect to the app database
  and create objects in the `public` schema, but is not superuser and cannot
  create roles, create databases, or replicate.

## New deployments

Set both secrets in `.env`:

```env
DB_NAME=pve_helper
DB_ADMIN_USER=pve_helper_admin
DB_ADMIN_PASSWORD=<admin-password>
DB_USER=pve_helper
DB_PASSWORD=<app-password>
```

On an empty Postgres volume, `docker/postgres/initdb/10-create-app-role.sh`
creates or updates `DB_USER` with:

```text
LOGIN
NOSUPERUSER
NOCREATEDB
NOCREATEROLE
NOREPLICATION
```

It grants only:

```text
CONNECT, TEMPORARY on the app database
USAGE, CREATE on schema public
```

That is enough for Django migrations and normal app writes in this single-app
database, while keeping the app away from cluster-admin privileges.

## Existing deployments

Older deployments may have used `DB_USER` as `POSTGRES_USER`, which makes the
app role a Postgres superuser. Check the current role:

```bash
docker compose exec -T db sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication from pg_roles where rolname = current_user;"'
```

If it prints `t` for `rolsuper`, do not try to demote that same role. PostgreSQL
does not allow the original bootstrap superuser to be demoted. Keep that role as
the DB admin role and create a new runtime app role instead.

Example for an existing deployment that was initialized with
`POSTGRES_USER=pve_helper`:

```env
DB_ADMIN_USER=pve_helper
DB_ADMIN_PASSWORD=<existing-postgres-password>
DB_USER=pve_helper_app
DB_PASSWORD=<new-app-password>
```

Run this before recreating `web` and `worker` with the new `DB_USER`:

```bash
export APP_DB_USER=pve_helper_app
export APP_DB_PASSWORD='<new-app-password>'

docker compose exec -T \
  -e APP_DB_USER="$APP_DB_USER" \
  -e APP_DB_PASSWORD="$APP_DB_PASSWORD" \
  db sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    --set ON_ERROR_STOP=1 \
    --set app_db="$POSTGRES_DB" \
    --set app_user="$APP_DB_USER" \
    --set app_password="$APP_DB_PASSWORD"' <<'SQL'
SELECT set_config('pve_helper.app_user', :'app_user', false);
SELECT set_config('pve_helper.app_password', :'app_password', false);

DO $$
DECLARE
    app_user text := current_setting('pve_helper.app_user');
    app_password text := current_setting('pve_helper.app_password');
    obj record;
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

    FOR obj IN
        SELECT n.nspname, c.relname, c.relkind
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg_toast%'
          AND c.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
          AND (
              c.relkind <> 'S'
              OR NOT EXISTS (
                  SELECT 1
                  FROM pg_depend d
                  WHERE d.objid = c.oid
                    AND d.deptype = 'a'
              )
          )
    LOOP
        EXECUTE format(
            'ALTER %s %I.%I OWNER TO %I',
            CASE obj.relkind
                WHEN 'S' THEN 'SEQUENCE'
                WHEN 'v' THEN 'VIEW'
                WHEN 'm' THEN 'MATERIALIZED VIEW'
                WHEN 'f' THEN 'FOREIGN TABLE'
                ELSE 'TABLE'
            END,
            obj.nspname,
            obj.relname,
            app_user
        );
    END LOOP;
END
$$;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE :"app_db" TO :"app_user";
GRANT USAGE, CREATE ON SCHEMA public TO :"app_user";
SQL
```

Then update `.env`, recreate the containers, and verify:

```bash
docker compose up -d --force-recreate web worker

docker compose exec -T db sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication from pg_roles where rolname in (current_user, '\''pve_helper_app'\'') order by rolname;"'
docker compose exec -T web python manage.py check
```

Expected result:

```text
pve_helper|t|t|t|t
pve_helper_app|f|f|f|f
```

## Test runs

Django's default test runner creates and drops a test database. A hardened app
role without `CREATEDB` cannot do that. For local test runs, either use the
admin DB role for the test command or grant `CREATEDB` temporarily in a
throwaway development database.
