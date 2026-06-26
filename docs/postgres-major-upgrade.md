# PostgreSQL major upgrade

The app can use PostgreSQL 18, but a PostgreSQL data directory created by one
major version cannot be started directly with a newer major version.

For example, do not switch an existing `postgres:16-alpine` container with an
existing `postgres_data` volume straight to `postgres:18-alpine`. Use a dump and
restore or `pg_upgrade`.

For this small app, a database dump and restore is the simplest and clearest
path.

## Reset instead of migrate

Early in development, it can be cleaner to reset the database and start from an
empty PostgreSQL 18 volume.

This removes scan history, audit history, scheduled scan settings, Django users,
sessions, and cached Proxmox/storage inventory. Runtime storage and Proxmox
endpoint rows can be recreated from environment configuration, but Django admin
users must be created again if needed.

1. Stop the app.

   ```bash
   docker compose down
   ```

2. Remove the active PostgreSQL volume.

   ```bash
   docker volume rm pve-helper_postgres_data
   ```

3. Change the database image in your local `docker-compose.yml` to:

   ```yaml
   image: postgres:18-alpine
   volumes:
     - postgres_data:/var/lib/postgresql
   ```

4. Start PostgreSQL 18 and recreate the schema.

   ```bash
   docker compose up -d db
   docker compose run --rm web python manage.py migrate
   docker compose run --rm web python manage.py shell -c "from core.services.config import sync_runtime_configuration; sync_runtime_configuration()"
   ```

5. Recreate local admin/schedule state if desired.

   ```bash
   docker compose run --rm web python manage.py createsuperuser
   ```

   The automatic scan interval can also be re-enabled from the web UI.

6. Start the app and verify.

   ```bash
   docker compose up -d web worker
   docker compose exec -T db postgres --version
   docker compose exec -T web python manage.py check
   curl -fsS http://127.0.0.1:21080/healthz/live
   ```

## PostgreSQL 16 to 18 dump/restore

1. Confirm the current database works.

   ```bash
   docker compose exec -T db postgres --version
   docker compose exec -T web python manage.py check
   ```

2. Create a compressed database backup outside the database volume.

   ```bash
   mkdir -p .local/backups
   docker compose exec -T db sh -lc 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > .local/backups/pve-helper-pg16.dump
   ```

3. Stop the app.

   ```bash
   docker compose down
   ```

4. Preserve the old volume instead of deleting it.

   ```bash
   docker volume ls | grep pve-helper
   docker volume create pve-helper_postgres_data_pg16_backup
   docker run --rm \
     -v pve-helper_postgres_data:/from:ro \
     -v pve-helper_postgres_data_pg16_backup:/to \
     alpine sh -c "cd /from && cp -a . /to/"
   ```

5. Remove only the active database volume after the backup exists.

   ```bash
   docker volume rm pve-helper_postgres_data
   ```

6. Change the database image in your local `docker-compose.yml` to:

   ```yaml
   image: postgres:18-alpine
   volumes:
     - postgres_data:/var/lib/postgresql
   ```

7. Start PostgreSQL 18 and restore the dump.

   ```bash
   docker compose up -d db
   docker compose exec -T db sh -lc 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' < .local/backups/pve-helper-pg16.dump
   ```

8. Start the app and verify.

   ```bash
   docker compose up -d web worker
   docker compose exec -T db postgres --version
   docker compose exec -T web python manage.py check
   curl -fsS http://127.0.0.1:21080/healthz/live
   ```

Keep the dump and the copied PostgreSQL 16 volume until the app has run
successfully for a while.
