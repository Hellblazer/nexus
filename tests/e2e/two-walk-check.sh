#!/usr/bin/env bash
# nexus-k9fs1: local rehearsal of the PITR-fork walk assertions.
#
# conexus runs scripts/check_pitr_fork_walk.py against a restored fork of
# production before the final engine cut deploys. This script runs the SAME
# assertions here, on a throwaway local engine, so the checker itself is proven
# against a real two-boot sequence before it is trusted on the fork. The
# property it exists for only shows on a database that has already been
# walked once: the q81g7 defect made boot 2 read an empty history and replan
# everything.
#
# What it does (host-level, no Docker; same self-provisioning idiom as
# published-client-write-gate.sh steps 1-2):
#   1. provisions a throwaway PG + scratch NEXUS_CONFIG_DIR with `nx init
#      --service --no-autostart` (the pinned engine walks the database once);
#   2. starts the WORKING-TREE dev jar against it: walk 1, the upgrade walk;
#   3. stops and restarts the same jar: walk 2;
#   4. runs `check_pitr_fork_walk.py schema` before and after each walk, and
#      `walk` on each walk's engine log (walk 2 with --noop).
# The migration role is read from the provisioned credentials file the engine
# is started with (NX_DB_ADMIN_USER), never typed here. Credentials are used
# only as env on the checker's own process and are never printed.
#
# Usage:
#   tests/e2e/two-walk-check.sh
#   TWO_WALK_EXPECTED_REEXECUTED=13 tests/e2e/two-walk-check.sh   # override only to probe the checker
#
# The runAlways count the second walk must re-execute is DEFAULT_REEXECUTED in
# scripts/check_pitr_fork_walk.py, defined once and pinned to the changelog by
# tests/scripts/test_check_pitr_fork_walk.py; this script passes no number unless
# the variable above is set.
#
# Last line: "TWO-WALK CHECK PASSED" (exit 0) or "TWO-WALK CHECK FAILED" (exit 1).
# Cost: a gate-jar build when service/ is not cached, then three engine boots.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

# Step 1's `nx init --service` runs as this checkout's own nx against a scratch
# NEXUS_CONFIG_DIR created below, never production; same reasoned opt-in as
# published-client-write-gate.sh (nexus-jspsn).
export NX_ALLOW_PROD_WRITE="two-walk-check: provisions a throwaway engine via this checkout's own nx, against its own scratch NEXUS_CONFIG_DIR, never production (nexus-k9fs1)"

REEXECUTED_ARGS=()
if [ -n "${TWO_WALK_EXPECTED_REEXECUTED:-}" ]; then
  REEXECUTED_ARGS=(--expect-reexecuted "$TWO_WALK_EXPECTED_REEXECUTED")
fi
CHECK="scripts/check_pitr_fork_walk.py"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/nx-twowalk.XXXXXX")"
ENGINE_HOME="$WORK/engine-config"
LOGS="$WORK/logs"
mkdir -p "$ENGINE_HOME" "$LOGS"
echo "[twowalk] scratch: $WORK"

GATE_OK=0
_fail() { echo "TWO-WALK CHECK FAILED: $*" >&2; exit 1; }

PG_BIN=""
# shellcheck disable=SC2329  # invoked via the EXIT trap below
cleanup() {
  set +e
  NX_LOCAL=1 NEXUS_CONFIG_DIR="$ENGINE_HOME" uv run nx daemon service stop >/dev/null 2>&1
  if [ -n "$PG_BIN" ] && [ -f "$ENGINE_HOME/pg_credentials" ]; then
    "$PG_BIN/pg_ctl" -D "$ENGINE_HOME/postgres" stop -m fast >/dev/null 2>&1
  fi
  if [ "$GATE_OK" = 1 ]; then
    rm -rf "$WORK"
  else
    echo "FAILURE EVIDENCE PRESERVED: $WORK" >&2
  fi
}
trap cleanup EXIT

echo "── 1/4 Provision (throwaway PG, scratch config; the pinned engine walks once) ──"
NEXUS_CONFIG_DIR="$ENGINE_HOME" uv run nx init --service --no-autostart </dev/null 2>&1 | tee "$LOGS/init.log" \
  || _fail "nx init --service failed (see $LOGS/init.log)"
NX_LOCAL=1 NEXUS_CONFIG_DIR="$ENGINE_HOME" uv run nx daemon service stop \
  || _fail "could not stop the auto-started service"

# Debug logging can reach stdout ahead of the value, so take the last line only.
PG_BIN="$(NEXUS_CONFIG_DIR="$ENGINE_HOME" uv run python -c "
from nexus.db.pg_provision import discover_pg_binaries
print(discover_pg_binaries().bin_dir)
" | tail -n 1)"
[ -x "$PG_BIN/psql" ] || _fail "no psql under $PG_BIN"

# The credentials file is the engine's own env (the supervisor sources it).
# Read the three values the checker needs without echoing anything.
CRED="$ENGINE_HOME/pg_credentials"
_cred() { sed -n "s/^$1=//p" "$CRED"; }
MIGRATION_ROLE="$(_cred NX_DB_ADMIN_USER)"
PG_PORT="$(_cred PG_PORT)"
[ -n "$MIGRATION_ROLE" ] && [ -n "$PG_PORT" ] || _fail "pg_credentials lacks NX_DB_ADMIN_USER or PG_PORT"
echo "[twowalk] migration role (from the engine's credentials file): $MIGRATION_ROLE"

# pg_db_role_setting rows: saved by the first schema check, compared by the rest.
SETTINGS_FILE="$WORK/pg_db_role_setting.json"

_check_schema() {
  # $1 = label, rest = extra args. PGPASSWORD is set on this process only.
  local label="$1"; shift
  echo "[twowalk] schema check: $label"
  PGHOST=127.0.0.1 PGPORT="$PG_PORT" PGUSER="$MIGRATION_ROLE" PGDATABASE=nexus \
    PGPASSWORD="$(_cred NX_DB_ADMIN_PASS)" \
    uv run python "$CHECK" schema --psql "$PG_BIN/psql" --migration-role "$MIGRATION_ROLE" "$@" \
    || _fail "schema check failed: $label"
}

_walk_rows() {
  PGHOST=127.0.0.1 PGPORT="$PG_PORT" PGUSER="$MIGRATION_ROLE" PGDATABASE=nexus \
    PGPASSWORD="$(_cred NX_DB_ADMIN_PASS)" \
    "$PG_BIN/psql" --no-psqlrc -X -At -c "select count(*) from public.databasechangelog"
}

_check_schema "after the pinned engine's walk" --save-settings "$SETTINGS_FILE"

# Rows in public.databasechangelog before walk 1: the delta after it is how many
# changesets walk 1 recorded, which is what `walk --expect-new` must be pinned to
# (a walk checked against no expectation would pass a no-op boot).
ROWS_BEFORE_1="$(_walk_rows)"
echo "[twowalk] public.databasechangelog rows before walk 1: $ROWS_BEFORE_1"

echo "── 2/4 Walk 1: the working-tree dev jar upgrades the database ──"
./scripts/build-gate-jar.sh 2>&1 | tee "$LOGS/build-gate-jar.log" \
  || _fail "scripts/build-gate-jar.sh failed (see $LOGS/build-gate-jar.log)"
JAR="$REPO_ROOT/service/target/nexus-service-1.0-SNAPSHOT.jar"
[ -f "$JAR" ] || _fail "build-gate-jar.sh reported success but $JAR does not exist"

SVC_LOG="$ENGINE_HOME/logs/storage_service_jar.log"

# The engine appends every boot to this one file (measured 2026-10-01: after the
# second start it held both, 280 lines), and the checker refuses a log holding more
# than one boot, since reading only the last would hide the first. So hand it ONE
# boot: the lines from the last schema_migration_start on.
_boot_slice() {
  awk '/event=schema_migration_start/ { n = NR } { l[NR] = $0 } END { if (n) for (i = n; i <= NR; i++) print l[i] }' "$1" > "$2"
  [ -s "$2" ] || _fail "no event=schema_migration_start in $1: nothing to hand the checker"
}
env NX_LOCAL=1 "NEXUS_CONFIG_DIR=$ENGINE_HOME" "NEXUS_SERVICE_JAR=$JAR" uv run nx daemon service start \
  2>&1 | tee "$LOGS/start-1.log" || _fail "walk 1: nx daemon service start failed (see $LOGS/start-1.log)"
[ -f "$SVC_LOG" ] || _fail "no engine log at $SVC_LOG after the start"
_boot_slice "$SVC_LOG" "$LOGS/walk1.engine.log"

ROWS_AFTER_1="$(_walk_rows)"
echo "[twowalk] public.databasechangelog rows after walk 1: $ROWS_AFTER_1"
NEW_IN_WALK_1=$((ROWS_AFTER_1 - ROWS_BEFORE_1))
if [ "$NEW_IN_WALK_1" -gt 0 ]; then
  WALK1_MODE=(--expect-new "$NEW_IN_WALK_1")
  echo "[twowalk] walk 1 recorded $NEW_IN_WALK_1 changeset(s): pinning --expect-new $NEW_IN_WALK_1"
else
  # The tree carries nothing beyond the engine that provisioned the database, so
  # walk 1 is itself a no-op. Say so and check it as one; never run it unpinned.
  WALK1_MODE=(--noop)
  echo "[twowalk] NOTE: this tree adds no changeset over the provisioning engine; walk 1 is checked as a no-op (nothing here exercises a changeset-applying walk)"
fi

echo "[twowalk] walk 1 assertions"
uv run python "$CHECK" walk --engine-log "$LOGS/walk1.engine.log" --migration-role "$MIGRATION_ROLE" \
  "${WALK1_MODE[@]}" ${REEXECUTED_ARGS[@]+"${REEXECUTED_ARGS[@]}"} || _fail "walk 1 assertions failed"
_check_schema "after walk 1" --compare-settings "$SETTINGS_FILE"

echo "── 3/4 Walk 2: stop and restart the same jar, nothing may apply ──"
NX_LOCAL=1 NEXUS_CONFIG_DIR="$ENGINE_HOME" uv run nx daemon service stop \
  || _fail "could not stop the service between walks"
env NX_LOCAL=1 "NEXUS_CONFIG_DIR=$ENGINE_HOME" "NEXUS_SERVICE_JAR=$JAR" uv run nx daemon service start \
  2>&1 | tee "$LOGS/start-2.log" || _fail "walk 2: nx daemon service start failed (see $LOGS/start-2.log)"
_boot_slice "$SVC_LOG" "$LOGS/walk2.engine.log"

echo "[twowalk] walk 2 assertions (--noop)"
uv run python "$CHECK" walk --engine-log "$LOGS/walk2.engine.log" --migration-role "$MIGRATION_ROLE" \
  --noop ${REEXECUTED_ARGS[@]+"${REEXECUTED_ARGS[@]}"} || _fail "walk 2 is not a no-op"
_check_schema "after walk 2 (row count unchanged)" --expect-rows "$ROWS_AFTER_1" --compare-settings "$SETTINGS_FILE"

echo "── 4/4 Verdict ──"
GATE_OK=1
echo "TWO-WALK CHECK PASSED"
