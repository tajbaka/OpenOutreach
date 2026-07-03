# Self-Hosted Postgres

This is the low-cost shared database path for OpenOutreach. It keeps the
existing Postgres contract (`DATABASE_URL`) and avoids the unsafe SQLite split
brain pattern.

## Local Test Target

The local test target runs Postgres 17 in Docker on `127.0.0.1:55432` with SSL
enabled. It is intentionally separate from the app container in `local.yml`.

```bash
make selfhost-db-prepare
make selfhost-db-up
```

The prepare step writes local-only secrets under `secrets/selfhost-postgres/`
which is already ignored by git:

- `postgres.env`
- `local-database-url.env`
- `certs/server.crt`
- `certs/server.key`

Use the local URL for one command without editing `.env`:

```bash
set -a
. secrets/selfhost-postgres/local-database-url.env
set +a
DATABASE_URL="$SELFHOST_DATABASE_URL" .venv/bin/python manage.py check
```

## Restore A Neon Copy Locally

This copies the current `.env` `DATABASE_URL` into the local Docker Postgres
target. It refuses to reset anything except `localhost:55432` unless explicitly
overridden.

```bash
scripts/restore_neon_to_selfhost_test.sh --confirm-reset-local
```

The restore script uses local `pg_dump`/`pg_restore` when they are installed.
On a Docker-only host, it falls back to the running
`openoutreach-postgres-test` container's Postgres clients.

Smoke-test the copied data with outbound automation disabled:

```bash
set -a
. secrets/selfhost-postgres/local-database-url.env
set +a
DATABASE_URL="$SELFHOST_DATABASE_URL" \
ENABLE_CONNECT=false \
ENABLE_FOLLOW_UP=false \
ENABLE_GMAIL_SEQUENCE=false \
ENABLE_REALTIME_LISTENER=false \
ENABLE_NODE_MONITOR=false \
.venv/bin/python manage.py check
```

For a deeper dry run, use commands that do not send LinkedIn or Gmail messages:

```bash
DATABASE_URL="$SELFHOST_DATABASE_URL" \
ENABLE_CONNECT=false \
ENABLE_FOLLOW_UP=false \
ENABLE_GMAIL_SEQUENCE=false \
ENABLE_REALTIME_LISTENER=false \
ENABLE_NODE_MONITOR=false \
.venv/bin/python manage.py sync_sheets --dry-run
```

## Real Host Shape

On the real central DB host, use the same database/user shape but expose the
host's public `5432` instead of local-only `55432`. The handoff checklist for
that machine is in `docs/self-hosted-postgres-host-readme.md`.

```bash
SELFHOST_POSTGRES_BIND=0.0.0.0 SELFHOST_POSTGRES_PORT=5432 make selfhost-db-up
make selfhost-db-ps
```

On a machine that should stay public across restarts, create ignored
`compose/.env` with:

```text
SELFHOST_POSTGRES_BIND=0.0.0.0
SELFHOST_POSTGRES_PORT=5432
```

Minimum public setup:

- dedicated database: `openoutreach`
- dedicated user: `openoutreach`
- long random password
- SSL enabled
- `DATABASE_URL=postgresql://openoutreach:<password>@<host>:5432/openoutreach?sslmode=require`
- host/router forwards inbound TCP `5432` to the Docker host
- nightly `pg_dump` backups copied off the host

This project accepts public Postgres with password + SSL as an intentional
tradeoff. Do not expose the `postgres` superuser, and keep the generated
password out of git.

## Cutover

Do not cut over while any daemon is running.

1. Stop every OpenOutreach daemon and the Vercel Slack function traffic if
   possible.
2. Take a final Neon dump.
3. Restore into the central self-hosted Postgres.
4. Update `DATABASE_URL` on every daemon/admin machine and in Vercel.
5. Start one daemon and verify heartbeat/task behavior.
6. Start the remaining daemons.
7. Keep Neon untouched for rollback for a few days.

Rollback is switching `DATABASE_URL` back to Neon after stopping the daemons.
