#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRET_DIR="$ROOT_DIR/secrets/selfhost-postgres"
CERT_DIR="$SECRET_DIR/certs"
POSTGRES_ENV="$SECRET_DIR/postgres.env"
URL_ENV="$SECRET_DIR/local-database-url.env"

mkdir -p "$CERT_DIR"

if [ ! -f "$POSTGRES_ENV" ]; then
  password="$(openssl rand -hex 32)"
  umask 077
  {
    echo "POSTGRES_DB=openoutreach"
    echo "POSTGRES_USER=openoutreach"
    echo "POSTGRES_PASSWORD=$password"
  } > "$POSTGRES_ENV"
else
  password="$(awk -F= '$1 == "POSTGRES_PASSWORD" {print $2}' "$POSTGRES_ENV")"
  if [ -z "$password" ]; then
    echo "POSTGRES_PASSWORD is missing from $POSTGRES_ENV" >&2
    exit 1
  fi
fi

if [ ! -f "$CERT_DIR/server.crt" ] || [ ! -f "$CERT_DIR/server.key" ]; then
  openssl req \
    -new \
    -x509 \
    -days 825 \
    -nodes \
    -text \
    -subj "/CN=localhost" \
    -keyout "$CERT_DIR/server.key" \
    -out "$CERT_DIR/server.crt" >/dev/null 2>&1
  chmod 600 "$CERT_DIR/server.key"
  chmod 644 "$CERT_DIR/server.crt"
fi

umask 077
cat > "$URL_ENV" <<EOF
SELFHOST_DATABASE_URL=postgresql://openoutreach:$password@127.0.0.1:55432/openoutreach?sslmode=require
EOF

cat <<EOF
Prepared local self-hosted Postgres secrets:
  $POSTGRES_ENV
  $URL_ENV
  $CERT_DIR/server.crt
  $CERT_DIR/server.key

Start the test DB:
  make selfhost-db-up
EOF
