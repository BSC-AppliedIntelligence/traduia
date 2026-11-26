#!/usr/bin/env bash
# Integration test of the database initialisation image (db/).
# Builds the image, starts a throwaway Supabase backend (supabase/postgres + GoTrue + PostgREST) on a private Docker
# network, runs the init job and checks: role passwords, migrations applied once, bootstrap administrators, sign-up
# trigger, row-level security and idempotency. Needs Docker and Internet access (pulls the images below).
# Usage: bash scripts/test_db_init.sh        (override the images with PG_IMAGE / GOTRUE_IMAGE / POSTGREST_IMAGE)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PG_IMAGE="${PG_IMAGE:-supabase/postgres:15.8.1.085}"
GOTRUE_IMAGE="${GOTRUE_IMAGE:-supabase/gotrue:v2.186.0}"
POSTGREST_IMAGE="${POSTGREST_IMAGE:-postgrest/postgrest:v13.0.8}"
CURL_IMAGE="${CURL_IMAGE:-curlimages/curl:8.10.1}"
INIT_IMAGE="${INIT_IMAGE:-bsc-backstage-db-init:test-$$}"
NET="dbinit-test-$$"
PW="pw$(openssl rand -hex 12)"
JWT_SECRET="$(openssl rand -hex 32)"
ADMIN_EMAIL="First.Admin@example.org"
fail=0
pass() { echo "  ok   $*"; }
bad()  { echo "  FAIL $*"; fail=1; }
check() { local what="$1"; shift; if "$@" >/dev/null 2>&1; then pass "$what"; else bad "$what"; fi; }

cleanup() {
  docker rm -f "$NET-db" "$NET-gotrue" "$NET-rest" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  [ -n "${BUILT:-}" ] && docker rmi "$INIT_IMAGE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
jwt() { # role
  local now exp h p s; now=$(date +%s); exp=$((now + 3600))
  h=$(printf '{"alg":"HS256","typ":"JWT"}' | b64url)
  p=$(printf '{"role":"%s","iss":"supabase","iat":%s,"exp":%s}' "$1" "$now" "$exp" | b64url)
  s=$(printf '%s.%s' "$h" "$p" | openssl dgst -binary -sha256 -hmac "$JWT_SECRET" | b64url)
  echo "$h.$p.$s"
}
ANON="$(jwt anon)"; SERVICE="$(jwt service_role)"
sql() { docker exec -e PGPASSWORD="$PW" "$NET-db" psql -h localhost -U postgres -Atq -c "$1"; }
cu()  { docker run --rm --network "$NET" "$CURL_IMAGE" -s "$@"; }
run_init() { # ADMIN_EMAILS value
  docker run --rm --network "$NET" -e POSTGRES_PASSWORD="$PW" -e JWT_SECRET="$JWT_SECRET" -e ADMIN_EMAILS="$1" -e WAIT_SECONDS=120 "$INIT_IMAGE"
}
signup() { # email -> user id
  cu -X POST http://gotrue:9999/signup -H "apikey: $ANON" -H 'Content-Type: application/json' \
     -d "{\"email\":\"$1\",\"password\":\"Test-pass-12345\",\"data\":{\"full_name\":\"test\"}}"
}

echo "== build init image"
docker build -q -f db/Dockerfile -t "$INIT_IMAGE" . >/dev/null && BUILT=1 && pass "image builds"
check "image contains exactly the migrations of db/migrations" bash -c \
  "diff <(docker run --rm --entrypoint ls $INIT_IMAGE /db/migrations) <(ls db/migrations)"

echo "== start database"
docker network create "$NET" >/dev/null
docker run -d --name "$NET-db" --network "$NET" --network-alias db -e POSTGRES_PASSWORD="$PW" "$PG_IMAGE" >/dev/null
for _ in $(seq 1 60); do docker exec "$NET-db" pg_isready -U postgres -h localhost -q && break; sleep 2; done

echo "== init job (first run)"
run_init "$ADMIN_EMAIL, second.admin@example.org" > /tmp/dbinit-$$.log 2>&1 && pass "init job exits 0" || { bad "init job failed"; cat /tmp/dbinit-$$.log; }
check "all migrations tracked" test "$(sql 'select count(*) from public.app_schema_migrations')" = "$(ls db/migrations | wc -l)"
check "admin list loaded (lower-cased, trimmed)" test "$(sql "select string_agg(email, ',' order by email) from public.bootstrap_admin_emails")" = "first.admin@example.org,second.admin@example.org"

echo "== start GoTrue and PostgREST with the role passwords set by the job"
docker run -d --name "$NET-gotrue" --network "$NET" --network-alias gotrue \
  -e GOTRUE_API_HOST=0.0.0.0 -e GOTRUE_API_PORT=9999 -e API_EXTERNAL_URL=http://gotrue:9999 \
  -e GOTRUE_DB_DRIVER=postgres -e "GOTRUE_DB_DATABASE_URL=postgres://supabase_auth_admin:$PW@db:5432/postgres" \
  -e GOTRUE_SITE_URL=http://localhost:3000 -e GOTRUE_JWT_SECRET="$JWT_SECRET" -e GOTRUE_JWT_EXP=3600 \
  -e GOTRUE_JWT_AUD=authenticated -e GOTRUE_JWT_DEFAULT_GROUP_NAME=authenticated -e GOTRUE_JWT_ADMIN_ROLES=service_role \
  -e GOTRUE_EXTERNAL_EMAIL_ENABLED=true -e GOTRUE_MAILER_AUTOCONFIRM=true "$GOTRUE_IMAGE" >/dev/null
docker run -d --name "$NET-rest" --network "$NET" --network-alias rest \
  -e "PGRST_DB_URI=postgres://authenticator:$PW@db:5432/postgres" -e PGRST_DB_SCHEMAS=public -e PGRST_DB_ANON_ROLE=anon \
  -e PGRST_JWT_SECRET="$JWT_SECRET" "$POSTGREST_IMAGE" >/dev/null
for _ in $(seq 1 60); do [ "$(cu -o /dev/null -w '%{http_code}' http://gotrue:9999/health)" = 200 ] && break; sleep 2; done
check "GoTrue healthy (migrated its schema with the role password)" test "$(cu -o /dev/null -w '%{http_code}' http://gotrue:9999/health)" = 200
for _ in $(seq 1 30); do [ "$(cu -o /dev/null -w '%{http_code}' -H "apikey: $ANON" http://rest:3000/)" = 200 ] && break; sleep 2; done
check "PostgREST serves the schema" test "$(cu -o /dev/null -w '%{http_code}' -H "apikey: $ANON" -H "Authorization: Bearer $ANON" http://rest:3000/events?select=id)" = 200

echo "== sign-ups"
signup "$ADMIN_EMAIL" >/dev/null; signup "someone.else@example.org" >/dev/null; signup "" >/dev/null || true
check "listed address (mixed case) is approved admin" test "$(sql "select concat(is_approved::int, is_admin::int) from public.profiles where lower(email)='first.admin@example.org'")" = "11"
check "other address stays pending" test "$(sql "select concat(is_approved::int, is_admin::int) from public.profiles where email='someone.else@example.org'")" = "00"

echo "== row-level security through PostgREST"
check "anon reads events" test "$(cu -o /dev/null -w '%{http_code}' -H "apikey: $ANON" -H "Authorization: Bearer $ANON" 'http://rest:3000/events?select=id')" = 200
check "anon cannot read the admin list" test "$(cu -o /dev/null -w '%{http_code}' -H "apikey: $ANON" -H "Authorization: Bearer $ANON" 'http://rest:3000/bootstrap_admin_emails?select=email')" != 200
check "service role reads both profiles" test "$(cu -H "apikey: $SERVICE" -H "Authorization: Bearer $SERVICE" 'http://rest:3000/profiles?select=email' | grep -o email | wc -l)" = 2

echo "== second run: idempotent, promotes a registered user, never demotes"
run_init "someone.else@example.org" > /tmp/dbinit-$$.log 2>&1 && pass "second run exits 0" || { bad "second run failed"; cat /tmp/dbinit-$$.log; }
check "migrations are not applied twice" grep -q "already applied: 010_admin_bootstrap.sql" /tmp/dbinit-$$.log
check "registered user on the new list is promoted" test "$(sql "select is_admin::int from public.profiles where email='someone.else@example.org'")" = 1
check "removed address is not demoted" test "$(sql "select is_admin::int from public.profiles where lower(email)='first.admin@example.org'")" = 1
rm -f /tmp/dbinit-$$.log

[ "$fail" = 0 ] && echo "ALL CHECKS PASSED" || { echo "SOME CHECKS FAILED"; exit 1; }
