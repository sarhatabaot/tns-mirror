#!/usr/bin/env bash
# End-to-end smoke test of the *published artifact*, not the source tree.
#
# Everything else in CI tests the code. This tests the thing a user actually
# receives: the built image, driven by the compose file we publish, against a
# real Postgres, following the documented setup in the documented order.
#
# It exists because two faults reached the 1.0.1 release that every source-level
# test passed straight through:
#
#   * the compose file built a DATABASE_URL by pasting the password into a URL,
#     so any password containing '/' or '@' silently became part of the host
#   * migrate/status/print-grants demanded a TNS credential they never use,
#     which blocked the documented setup order
#
# Hence two deliberate choices below, both load-bearing:
#
#   1. The password is adversarial. With the shipped default of "CHANGE-ME" the
#      URL bug does not reproduce — a smoke test using a well-behaved password
#      would have passed while the release was broken.
#   2. No TNS credential is configured. The local commands must work without
#      one, and the downloading commands must refuse. Both directions matter.
#
# Usage:  scripts/smoke_test.sh [image-tag]
#
# The tag defaults to "smoke"; the image name is whatever the compose file
# names, so the compose file itself is under test.

set -euo pipefail

TAG=${1:-smoke}
PROJECT=tns-smoke-$$
COMPOSE_FILE="$(cd "$(dirname "$0")/.." && pwd)/quickstart/docker-compose.yml"

# Contains the characters that break a URL: '/' ends the authority section,
# '@' splits userinfo from host. Also '+' and '=' from base64.
DB_PASSWORD='aB/cd+Ef/gh=@z'
READER_PASSWORD='rd/pw+9=@q'

compose() {
    docker compose -p "$PROJECT" -f "$COMPOSE_FILE" "$@"
}

cleanup() {
    compose down -v --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() {
    echo "SMOKE FAIL: $*" >&2
    echo "--- server logs ---" >&2
    compose logs server 2>&1 | tail -30 >&2 || true
    exit 1
}

pass() { echo "  ok   $*"; }

export TNS_MIRROR_VERSION="$TAG"
export POSTGRES_PASSWORD="$DB_PASSWORD"
# Deliberately absent: TNS_USER_AGENT, TNS_API_KEY, TNS_BOT_ID, TNS_BOT_NAME.

echo "smoke test: image tag '$TAG', password with / @ + ="
cleanup

# --- the database ------------------------------------------------------------
compose up -d db >/dev/null 2>&1 || fail "could not start the database"

for _ in $(seq 1 60); do
    state=$(docker inspect --format='{{.State.Health.Status}}' \
        "$(compose ps -q db)" 2>/dev/null || echo starting)
    [ "$state" = "healthy" ] && break
    sleep 1
done
[ "$state" = "healthy" ] || fail "database never became healthy"
pass "database healthy"

# --- commands that must work with no TNS credential --------------------------
compose run --rm server migrate >/dev/null 2>&1 \
    || fail "migrate failed — the server cannot reach its own database, or it
          demands a TNS credential it does not need"
pass "migrate (no TNS credential)"

compose run --rm server migrate 2>&1 | grep -q "already up to date" \
    || fail "migrate is not idempotent"
pass "migrate is idempotent"

compose run --rm server status 2>&1 | grep -q "schema_version:       1" \
    || fail "status did not report schema v1"
pass "status"

# A fresh mirror has never synced, so the healthcheck must call it stale. This
# is exit code 3 specifically — the container HEALTHCHECK depends on it.
set +e
compose run --rm server status --max-age-hours 26 >/dev/null 2>&1
code=$?
set -e
[ "$code" -eq 3 ] || fail "status --max-age-hours should exit 3 when never synced, got $code"
pass "status --max-age-hours exits 3 (healthcheck contract)"

grants=$(compose run --rm server print-grants --database tnsdb 2>/dev/null) \
    || fail "print-grants failed — it must not require a TNS credential, since
          it is how an operator creates the reader before having an account"
for want in "GRANT CONNECT ON DATABASE" "GRANT USAGE ON SCHEMA" \
            "GRANT SELECT ON public.tns_objects" \
            "GRANT SELECT ON public.tns_mirror_meta"; do
    grep -q "$want" <<<"$grants" || fail "print-grants is missing: $want"
done

# Comments explain intent and say the words "ALTER DEFAULT PRIVILEGES" in order
# to disclaim them; only the executable statements may be asserted on.
statements=$(grep -v '^[[:space:]]*--' <<<"$grants")
grep -q "ALTER DEFAULT PRIVILEGES" <<<"$statements" \
    && fail "print-grants would widen the reader to every future table"
grep -qE "GRANT (INSERT|UPDATE|DELETE|ALL)" <<<"$statements" \
    && fail "print-grants grants more than SELECT"
pass "print-grants (no TNS credential)"

# --- the reader actually works, and cannot write -----------------------------
psql_writer() {
    compose exec -T -e PGPASSWORD="$DB_PASSWORD" db \
        psql -q -v ON_ERROR_STOP=1 -U tns_writer -d tnsdb "$@"
}
psql_reader() {
    compose exec -T -e PGPASSWORD="$READER_PASSWORD" db \
        psql -q -U tns_ro -d tnsdb "$@"
}

printf '%s\n' "$grants" | psql_writer -v pw="$READER_PASSWORD" -f - >/dev/null \
    || fail "the generated grants did not apply"
pass "grants applied"

psql_writer -c "INSERT INTO tns_objects (objid,name,ra,\"dec\",source_snapshot,refreshed_at)
                VALUES (1,'AT2026smoke',203.1,10.2,current_date,now())" >/dev/null \
    || fail "could not seed a row as the writer"

seen=$(psql_reader -tAc "SELECT count(*) FROM tns_objects" 2>/dev/null || echo "")
[ "$seen" = "1" ] || fail "the reader cannot SELECT from the catalogue (got '$seen')"
pass "reader can SELECT the catalogue"

version=$(psql_reader -tAc "SELECT schema_version FROM tns_mirror_meta" 2>/dev/null || echo "")
[ "$version" = "1" ] || fail "the reader cannot read tns_mirror_meta — the client
          needs it to check the schema version it was built against"
pass "reader can read tns_mirror_meta"

for statement in "DELETE FROM tns_objects" \
                 "UPDATE tns_objects SET name='x'" \
                 "INSERT INTO tns_objects (objid,name,ra,\"dec\",source_snapshot,refreshed_at)
                  VALUES (2,'x',1,1,current_date,now())" \
                 "DROP TABLE tns_objects"; do
    if psql_reader -c "$statement" >/dev/null 2>&1; then
        fail "the read-only role was allowed to run: $statement"
    fi
done
pass "reader is denied INSERT, UPDATE, DELETE and DROP"

# --- invariant 4: no credential, no download ---------------------------------
for argv in "sync" "sync --hour 3" "catch-up 2" "serve"; do
    # shellcheck disable=SC2086  # word splitting is intended here
    out=$(compose run --rm server $argv 2>&1 || true)
    grep -q "never downloads anonymously" <<<"$out" \
        || fail "'$argv' should refuse without a TNS credential, but did not.
          Output: $(tail -3 <<<"$out")"
done
pass "sync, catch-up and serve refuse without a TNS credential"

echo
echo "smoke test passed: the published image and compose file work end to end"
