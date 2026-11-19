#!/usr/bin/env bash
# Runs ON the server. Installs PostgreSQL locally (no external service), creates role+db `kirbet`
# with a random password, migrates the schema and copies all data from the SQLite file, then
# switches the bot to PostgreSQL. Idempotent; the SQLite file is kept as a fallback/backup.
#   bash deploy/setup_postgres.sh
set -euo pipefail
APP_DIR="${APP_DIR:-$HOME/bot}"
cd "$APP_DIR"

if grep -q '^DATABASE_URL=postgresql' .env; then
  echo "== already on PostgreSQL"; exit 0
fi

echo "== installing PostgreSQL"
sudo apt-get update -y -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq postgresql >/dev/null
sudo systemctl enable --now postgresql

PASS_FILE="$HOME/.kirbet_pg_pass"
if [ ! -f "$PASS_FILE" ]; then
  umask 077; openssl rand -hex 24 > "$PASS_FILE"
fi
PASS="$(cat "$PASS_FILE")"
sudo -u postgres psql -v ON_ERROR_STOP=1 -q <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kirbet') THEN
    CREATE ROLE kirbet LOGIN PASSWORD '$PASS';
  ELSE
    ALTER ROLE kirbet PASSWORD '$PASS';
  END IF;
END \$\$;
SQL
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='kirbet'" | grep -q 1 \
  || sudo -u postgres createdb -O kirbet kirbet
# listen on localhost only (Ubuntu default) — nothing exposed to the internet
PG_URL="postgresql+asyncpg://kirbet:${PASS}@127.0.0.1:5432/kirbet"
SQLITE_URL="sqlite+aiosqlite:///$APP_DIR/data/arb.db"

echo "== schema"
DATABASE_URL="$PG_URL" .venv/bin/alembic upgrade head

echo "== stopping the bot for a consistent copy"
sudo systemctl stop arb-bot
cp -a data/arb.db "data/arb.db.before-postgres.$(date -u +%Y%m%dT%H%M%SZ)"
.venv/bin/alembic upgrade head   # SQLite at the same revision
.venv/bin/python scripts/migrate_sqlite_to_postgres.py "$SQLITE_URL" "$PG_URL"

echo "== switching .env to PostgreSQL"
cp -a .env ".env.before-postgres"
if grep -q '^DATABASE_URL=' .env; then
  sed -i "s#^DATABASE_URL=.*#DATABASE_URL=${PG_URL}#" .env
else
  printf '\nDATABASE_URL=%s\n' "$PG_URL" >> .env
fi
chmod 600 .env
sudo systemctl start arb-bot
sleep 8
systemctl is-active arb-bot
echo "== done (rollback: cp .env.before-postgres .env && sudo systemctl restart arb-bot)"
