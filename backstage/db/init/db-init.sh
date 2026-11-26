#!/usr/bin/env bash
# One-shot initialisation of a Supabase backend (Postgres + GoTrue + PostgREST) for Backstage.
# Idempotent: safe to run on every deployment. Steps:
#   1. wait for the database;
#   2. as supabase_admin: set the passwords of the roles GoTrue/PostgREST use and the JWT settings;
#   3. as postgres: apply each /db/migrations/*.sql once (tracked in public.app_schema_migrations);
#   4. as postgres: load ADMIN_EMAILS (approved + admin at sign-up; existing users are promoted, never demoted);
#   5. tell PostgREST to reload its schema cache.
# Environment: POSTGRES_PASSWORD, JWT_SECRET (required); ADMIN_EMAILS, JWT_EXP (default 3600), PGHOST (default db),
#              PGPORT, PGDATABASE, WAIT_SECONDS (default 600), MIGRATIONS_DIR, INIT_DIR.
set -euo pipefail
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD required}" "${JWT_SECRET:?JWT_SECRET required}"
export PGHOST="${PGHOST:-db}" PGPORT="${PGPORT:-5432}" PGDATABASE="${PGDATABASE:-postgres}"
MIGRATIONS_DIR="${MIGRATIONS_DIR:-/db/migrations}" INIT_DIR="${INIT_DIR:-/db/init}"
export POSTGRES_PASSWORD JWT_SECRET JWT_EXP="${JWT_EXP:-3600}"

# The password is passed through the environment (PGPASSWORD) so it never shows up in a process list.
as_admin() { PGUSER=supabase_admin PGPASSWORD="$POSTGRES_PASSWORD" psql -v ON_ERROR_STOP=1 -X -q "$@"; }
as_owner() { PGUSER=postgres PGPASSWORD="$POSTGRES_PASSWORD" psql -v ON_ERROR_STOP=1 -X -q "$@"; }
log() { echo "[db-init] $*"; }

log "waiting for the database at ${PGHOST}:${PGPORT}"
deadline=$((SECONDS + ${WAIT_SECONDS:-600}))
until as_admin -Atc "select to_regclass('auth.users') is not null" 2>/dev/null | grep -q t; do
  [ "$SECONDS" -lt "$deadline" ] || { log "database/auth schema not ready after ${WAIT_SECONDS:-600}s"; exit 1; }
  sleep 3
done

log "setting role passwords and JWT settings"
as_admin -f "$INIT_DIR/roles-and-jwt.sql"

as_owner -c "CREATE TABLE IF NOT EXISTS public.app_schema_migrations (filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
for f in "$MIGRATIONS_DIR"/*.sql; do
  n="$(basename "$f")"
  if [ "$(as_owner -Atc "select count(*) from public.app_schema_migrations where filename='$n'")" = "0" ]; then
    log "applying $n"
    as_owner -1 -f "$f"
    as_owner -c "INSERT INTO public.app_schema_migrations (filename) VALUES ('$n')"
  else
    log "already applied: $n"
  fi
done

log "applying ADMIN_EMAILS (${ADMIN_EMAILS:-<empty>})"
as_owner -v admin_emails="${ADMIN_EMAILS:-}" -f "$INIT_DIR/apply-admins.sql"
as_owner -c "NOTIFY pgrst, 'reload schema'"
log "done"
