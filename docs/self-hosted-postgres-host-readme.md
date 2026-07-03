# OpenOutreach Shared Postgres Host

Use this on the machine that will host the shared OpenOutreach database.

## 1. Get The Repo

```bash
git clone <repo-url> OpenOutreach
cd OpenOutreach
```

If the repo is already there:

```bash
git pull
```

The host needs the self-hosted Postgres files:

- `compose/selfhost-postgres.yml`
- `scripts/selfhost_postgres_prepare.sh`
- `scripts/restore_neon_to_selfhost_test.sh`
- `docs/self-hosted-postgres.md`

## 2. Install Docker

Install Docker Desktop or Docker Engine on the host machine. Docker is only
needed on this central DB host, not on every daemon machine.

## 3. Generate DB Password And SSL Cert

```bash
make selfhost-db-prepare
```

This creates ignored local files under:

```text
secrets/selfhost-postgres/
```

Do not commit these files.

## 4. Start Postgres Publicly

For the real shared host, bind Postgres publicly on port `5432`:

```bash
SELFHOST_POSTGRES_BIND=0.0.0.0 SELFHOST_POSTGRES_PORT=5432 make selfhost-db-up
```

For a host that should remain public after ordinary `make selfhost-db-up`
restarts, create ignored `compose/.env`:

```text
SELFHOST_POSTGRES_BIND=0.0.0.0
SELFHOST_POSTGRES_PORT=5432
```

Confirm it is healthy:

```bash
make selfhost-db-ps
```

## 5. Get The Connection URL

Read the generated local URL:

```bash
cat secrets/selfhost-postgres/local-database-url.env
```

For other machines, replace this part:

```text
127.0.0.1:55432
```

with the host's public IP/domain and port:

```text
<host-ip-or-domain>:5432
```

Final shape:

```bash
DATABASE_URL=postgresql://openoutreach:<password>@<host-ip-or-domain>:5432/openoutreach?sslmode=require
```

## 6. Restore Data

For staging, restore a copy first. From a machine with `.env` pointing at Neon:

```bash
make selfhost-db-restore-copy
```

If the host runs on public port `5432`, target that port explicitly:

```bash
TARGET_DATABASE_URL='postgresql://openoutreach:<password>@127.0.0.1:5432/openoutreach?sslmode=require' \
ALLOW_NONLOCAL_TARGET=true \
make selfhost-db-restore-copy
```

The restore script uses local `pg_dump`/`pg_restore` when available. If those
client tools are not installed on the host, it falls back to the running
`openoutreach-postgres-test` container's Postgres clients.

For final production cutover:

1. Stop all OpenOutreach daemons.
2. Take the final Neon dump.
3. Restore it into this host DB.
4. Update `DATABASE_URL` everywhere: daemon machines, admin machine, Vercel.
5. Start one daemon first and verify.
6. Start the rest.

Keep Neon unchanged for rollback until the self-hosted DB has run cleanly for a
few days.

## 7. Firewall Notes

This setup intentionally uses username/password plus SSL rather than IP
allowlisting. The host must allow inbound TCP `5432`.

If the host is behind a NAT router, add a port-forwarding rule:

```text
external TCP 5432 -> <db-host-lan-ip>:5432
```

Minimum precautions:

- Use the generated long password.
- Do not expose the `postgres` superuser.
- Keep `sslmode=require` in every `DATABASE_URL`.
- Back up nightly with `pg_dump` and copy the backup off the host.
