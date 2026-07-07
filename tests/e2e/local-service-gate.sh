#!/usr/bin/env bash
# Local-service functional gate (2026-07-06, born from the v6.3.6 release).
#
# WHY THIS EXISTS: the integration suite's local-service round-trip family
# (tests/test_integration.py T3 round-trips, scratch/MCP round-trips — ~370
# tests) is the FUNCTIONAL TEST of local mode, one of the two shipped modes.
# Its skip-gate resolves a service via env vars or a local lease — never the
# managed cloud (deliberate: integration tests must not write junk into
# production). Before this script, the gate ran only when a local service
# HAPPENED to be running, so it silently degraded to 74/516 tests the day the
# ambient dev service died (v6.3.6 release, 2026-07-06). A functional gate
# must be self-provisioning, not a side quest.
#
# HONEST COVERAGE (nexus-edwlp tracks closing the gap): this harness
# currently unlocks only PART of the family — many tests re-isolate their own
# NEXUS_CONFIG_DIR/HOME and lose the env-leg service inside that boundary
# (they need fixture-level NX_SERVICE_URL injection), and the analytics
# fixtures skip without seeded corpora. Until nexus-edwlp lands, expect
# partial coverage; treat any new HARD failure as real signal.
#
# What it does, fully isolated from ~/.config/nexus and from any cloud config:
#   1. Scratch NEXUS_CONFIG_DIR; `nx init --service` provisions a throwaway
#      PG cluster (port 0 -> dynamic).
#   2. Starts the storage service against it (NX_LOCAL=1), preferring an
#      installed native binary, falling back to the dev jar
#      (service/target/nexus-service-1.0-SNAPSHOT.jar; build with
#      `cd service && ./mvnw -q package -DskipTests`).
#   3. Runs `pytest -m integration` with NX_SERVICE_HOST/PORT/TOKEN pointed
#      at the throwaway service (the env leg of resolve_service_config), so
#      the whole local-service family runs deterministically.
#   4. Tears everything down (service, PG, scratch dir), even on failure.
#
# Usage:
#   tests/e2e/local-service-gate.sh            # full integration gate
#   tests/e2e/local-service-gate.sh -k catalog # pass-through pytest args
#
# NEVER run this concurrently with another pytest invocation (repo rule:
# one pytest at a time).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/nx-local-service-gate.XXXXXX")"
echo "[gate] scratch config: $SCRATCH"

cleanup() {
  set +e
  NX_LOCAL=1 NEXUS_CONFIG_DIR="$SCRATCH" uv run nx daemon service stop >/dev/null 2>&1
  # Stop the throwaway PG cluster if still up (pg_ctl from the provisioned
  # credentials; best-effort — the datadir removal below is the backstop).
  if [ -f "$SCRATCH/pg_credentials" ]; then
    # shellcheck disable=SC1090
    source "$SCRATCH/pg_credentials" >/dev/null 2>&1
    pg_ctl -D "$SCRATCH/postgres" stop -m fast >/dev/null 2>&1
  fi
  rm -rf "$SCRATCH"
  echo "[gate] cleaned up"
}
trap cleanup EXIT

# 1. Provision the throwaway PG cluster.
NEXUS_CONFIG_DIR="$SCRATCH" uv run nx init --service

# 2. Resolve a launch artifact: installed native binary wins; dev jar fallback.
JAR="$REPO_ROOT/service/target/nexus-service-1.0-SNAPSHOT.jar"
START_ENV=(NX_LOCAL=1 "NEXUS_CONFIG_DIR=$SCRATCH")
if [ -n "${NEXUS_SERVICE_BIN:-}" ]; then
  START_ENV+=("NEXUS_SERVICE_BIN=$NEXUS_SERVICE_BIN")
elif [ -f "$JAR" ]; then
  START_ENV+=("NEXUS_SERVICE_JAR=$JAR")
else
  echo "[gate] ERROR: no launch artifact — build the dev jar first:" >&2
  echo "         cd service && ./mvnw -q package -DskipTests" >&2
  echo "       or export NEXUS_SERVICE_BIN=<native binary>" >&2
  exit 2
fi
env "${START_ENV[@]}" uv run nx daemon service start

# 3. Read the lease and run the gate through the env leg.
LEASE_JSON="$(cat "$SCRATCH"/storage_service_addr.*)"
SERVICE_PORT="$(printf '%s' "$LEASE_JSON" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('endpoint',d)['port'])")"
SERVICE_TOKEN="$(printf '%s' "$LEASE_JSON" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('endpoint',d)['token'])")"
echo "[gate] throwaway service on 127.0.0.1:$SERVICE_PORT"

# .env (repo root) auto-loads inside the suite for cloud API keys; the
# NX_SERVICE_* env leg pins the SERVICE at the throwaway instance.
set +e
NX_SERVICE_HOST=127.0.0.1 NX_SERVICE_PORT="$SERVICE_PORT" NX_SERVICE_TOKEN="$SERVICE_TOKEN" \
  uv run pytest -m integration -q "$@"
STATUS=$?
set -e

if [ "$STATUS" -eq 0 ]; then
  echo "[gate] LOCAL-SERVICE GATE PASSED"
else
  echo "[gate] LOCAL-SERVICE GATE FAILED (pytest exit $STATUS)"
fi
exit "$STATUS"
