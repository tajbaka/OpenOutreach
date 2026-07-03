#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_URL_ENV="$ROOT_DIR/secrets/selfhost-postgres/local-database-url.env"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"

if [ "${1:-}" != "--confirm-reset-local" ]; then
  cat >&2 <<'EOF'
This restores the current Neon DATABASE_URL into the local self-host test DB,
resetting the local target first.

Usage:
  scripts/restore_neon_to_selfhost_test.sh --confirm-reset-local
EOF
  exit 2
fi

if [ ! -x "$PYTHON_BIN" ]; then
  echo "Missing $PYTHON_BIN; run local setup first." >&2
  exit 1
fi

dotenv_value() {
  "$PYTHON_BIN" -c 'from dotenv import dotenv_values; import sys; print(dotenv_values(sys.argv[1]).get(sys.argv[2], "") or "")' "$1" "$2"
}

SOURCE_DATABASE_URL="${SOURCE_DATABASE_URL:-${DATABASE_URL:-$(dotenv_value "$ROOT_DIR/.env" DATABASE_URL)}}"
TARGET_DATABASE_URL="${TARGET_DATABASE_URL:-${SELFHOST_DATABASE_URL:-$(dotenv_value "$LOCAL_URL_ENV" SELFHOST_DATABASE_URL)}}"

if [ -z "$SOURCE_DATABASE_URL" ]; then
  echo "SOURCE_DATABASE_URL/DATABASE_URL is not set." >&2
  exit 1
fi
if [ -z "$TARGET_DATABASE_URL" ]; then
  echo "TARGET_DATABASE_URL/SELFHOST_DATABASE_URL is not set. Run: make selfhost-db-prepare" >&2
  exit 1
fi
if [ "$SOURCE_DATABASE_URL" = "$TARGET_DATABASE_URL" ]; then
  echo "Source and target URLs are identical; refusing to restore." >&2
  exit 1
fi
if [ "${ALLOW_NONLOCAL_TARGET:-false}" != "true" ]; then
  case "$TARGET_DATABASE_URL" in
    *"127.0.0.1:55432"*|*"localhost:55432"*) ;;
    *)
      echo "Refusing to reset non-local target: $TARGET_DATABASE_URL" >&2
      echo "Set ALLOW_NONLOCAL_TARGET=true only for an intentional host restore." >&2
      exit 1
      ;;
  esac
fi

dump_file="$(mktemp "/tmp/openoutreach-neon-copy.XXXXXX.dump")"
cleanup() {
  rm -f "$dump_file"
}
trap cleanup EXIT

echo "Dumping source database..."
if command -v pg_dump >/dev/null 2>&1 && command -v pg_restore >/dev/null 2>&1; then
  pg_dump "$SOURCE_DATABASE_URL" --format=custom --no-owner --no-acl --file "$dump_file"

  echo "Restoring into local self-host test database..."
  pg_restore \
    --clean \
    --if-exists \
    --no-owner \
    --no-acl \
    --dbname "$TARGET_DATABASE_URL" \
    "$dump_file"
else
  echo "Local pg_dump/pg_restore not found; using openoutreach-postgres-test container clients..."
  docker exec \
    -e SOURCE_DATABASE_URL="$SOURCE_DATABASE_URL" \
    -e TARGET_DATABASE_URL="$TARGET_DATABASE_URL" \
    openoutreach-postgres-test \
    bash -lc '
      set -euo pipefail
      dump_file="/tmp/openoutreach-neon-copy.dump"
      rm -f "$dump_file"
      pg_dump "$SOURCE_DATABASE_URL" --format=custom --no-owner --no-acl --file "$dump_file"
      pg_restore --clean --if-exists --no-owner --no-acl --dbname "$TARGET_DATABASE_URL" "$dump_file"
      rm -f "$dump_file"
    '
fi

echo "Restore complete."
