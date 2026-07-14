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
# SELF-PROVISIONING (nexus-edwlp, 2026-07-07): infra is hermetic — the gate
# provisions its own PG + service, auto-rebuilds a stale dev jar, and the T3
# vector resolver honors the same HOST/PORT env leg as the T2 stores. NOT
# credential-independent: the voyage/CCE-embedding subset needs a real
# VOYAGE_API_KEY, sourced explicitly from .env (repo root) — on a checkout
# without one those tests fail rather than degrade. A `lived_in` marker
# excludes the handful of tests that dispatch real `claude -p` or need
# seeded lived-in corpora, and a vacuity guard asserts passed/skipped stay
# within the pinned FLOOR/BUDGET below (plus an exact lived_in carve-out
# count). A guard trip means real regression, not ambient drift.
#
# What it does, fully isolated from ~/.config/nexus and from any cloud config:
#   1. Scratch NEXUS_CONFIG_DIR; `nx init --service` provisions a throwaway
#      PG cluster (port 0 -> dynamic).
#   2. Rebuilds the dev jar if stale or missing (rebuild-on-key-miss,
#      compares jar mtime against service/src/{main/java,main/resources/
#      db/changelog}); skipped when a native binary is supplied.
#   3. Starts the storage service against it (NX_LOCAL=1), preferring an
#      installed native binary, falling back to the dev jar
#      (service/target/nexus-service-1.0-SNAPSHOT.jar).
#   4. Sources .env (repo root) for cloud API keys, then runs
#      `pytest -m "integration and not lived_in"` with
#      NX_SERVICE_HOST/PORT/TOKEN + NX_SERVICE_URL pointed at the
#      throwaway service (both the T2 and T3 resolver env legs), so the
#      whole local-service family runs deterministically.
#   5. Parses the pytest summary and asserts passed >= FLOOR and
#      skipped <= BUDGET (the vacuity guard) — see the numbers pinned
#      below.
#   6. Tears everything down (service, PG, scratch dir), even on failure.
#
# Usage:
#   tests/e2e/local-service-gate.sh            # full integration gate
#   tests/e2e/local-service-gate.sh -k catalog # pass-through pytest args
#
# NEVER run this concurrently with another pytest invocation (repo rule:
# one pytest at a time).
set -euo pipefail

# ── Vacuity-guard summary-line parser (nexus-edwlp Task 6) ──────────────────
# Extracts a count (e.g. "77" from "77 passed") out of a pytest -q summary
# line such as "2 failed, 77 passed, 24 skipped in 812.34s". A category
# absent from the line (pytest omits zero-count categories) yields 0.
parse_summary_count() {
  local label="$1" text="$2" n
  n="$(grep -oE "[0-9]+ ${label}" <<<"$text" | grep -oE '^[0-9]+' | head -1)"
  echo "${n:-0}"
}

# Select pytest's counts line from captured output. ANCHORED as a counts line
# ("N <category>[, ...] in 12.34s"): a failing test's error repr can contain
# " in 0.53s" and print AFTER the real summary; an unanchored last-match then
# parses passed=0 (observed live, 2026-07-07). Without -q (e.g. a -v
# pass-through run) pytest decorates the line ("==== N passed in 7.75s ===="),
# which false-tripped the no-summary guard (observed live, 2026-07-07) — the
# optional =-decoration prefix covers that form. Empty on no match (`|| true`
# keeps set -e/pipefail from aborting before the failure is reported).
select_summary_line() {
  grep -E '^(=+ )?[0-9]+ (failed|passed|skipped|deselected|error|xfailed|xpassed|warning)[a-z]*(,.*)? in [0-9.]+s' "$1" | tail -1 || true
}

# ── Self-test (NX_GATE_SELFTEST=1): exercise the parser against synthetic
# fixtures with no real infrastructure. Exits before any provisioning. ──────
if [ "${NX_GATE_SELFTEST:-0}" = "1" ]; then
  selftest_failed=0
  check_parse() {
    local desc="$1" line="$2" expected_passed="$3" expected_skipped="$4"
    local got_passed got_skipped
    got_passed="$(parse_summary_count passed "$line")"
    got_skipped="$(parse_summary_count skipped "$line")"
    if [ "$got_passed" != "$expected_passed" ] || [ "$got_skipped" != "$expected_skipped" ]; then
      echo "[gate-selftest] FAIL ($desc): line='$line' passed=$got_passed(expected $expected_passed) skipped=$got_skipped(expected $expected_skipped)" >&2
      selftest_failed=1
    else
      echo "[gate-selftest] ok ($desc): passed=$got_passed skipped=$got_skipped"
    fi
  }
  check_parse "passed+skipped" "77 passed, 430 skipped in 812.34s" 77 430
  check_parse "passed only, zero skipped omitted" "512 passed in 45.01s" 512 0
  check_parse "failed+passed+skipped" "2 failed, 505 passed, 24 skipped in 900.12s" 505 24
  check_parse "non-quiet =-decorated summary" "=========== 7 passed, 75 deselected in 7.75s ===========" 7 0

  # Non-quiet (-v pass-through) runs decorate the summary line with = signs;
  # selection must still find it (false no-summary trip observed 2026-07-07).
  selftest_fixture_v="$(mktemp)"
  printf '%s\n' \
    "tests/test_mcp_server.py::test_mcp_server_round_trip PASSED" \
    "======================= 7 passed, 75 deselected in 7.75s =======================" \
    > "$selftest_fixture_v"
  selected_v="$(select_summary_line "$selftest_fixture_v")"
  rm -f "$selftest_fixture_v"
  if [ "$selected_v" = "======================= 7 passed, 75 deselected in 7.75s =======================" ]; then
    echo "[gate-selftest] ok (=-decorated summary line selected)"
  else
    echo "[gate-selftest] FAIL (=-decorated selection): got '$selected_v'" >&2
    selftest_failed=1
  fi

  # Line SELECTION: a post-summary decoy containing " in 0.53s" must not win.
  selftest_fixture="$(mktemp)"
  printf '%s\n' \
    "1 failed, 438 passed, 31 skipped in 250.88s" \
    "E   AssertionError: GuidedUpgradeResult(... completed in 0.53s ...)" \
    > "$selftest_fixture"
  selected="$(select_summary_line "$selftest_fixture")"
  rm -f "$selftest_fixture"
  if [ "$selected" = "1 failed, 438 passed, 31 skipped in 250.88s" ]; then
    echo "[gate-selftest] ok (decoy after summary line ignored)"
  else
    echo "[gate-selftest] FAIL (decoy selection): got '$selected'" >&2
    selftest_failed=1
  fi

  if [ "$selftest_failed" -ne 0 ]; then
    echo "[gate-selftest] SELFTEST FAILED" >&2
    exit 1
  fi
  echo "[gate-selftest] all selftest cases passed"
  exit 0
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/nx-local-service-gate.XXXXXX")"
echo "[gate] scratch config: $SCRATCH"

# .env does NOT auto-load anywhere in the suite; source it explicitly, and
# BEFORE the service starts — the supervisor plumbs VOYAGE_API_KEY ->
# NX_VOYAGE_API_KEY into the service env at spawn (storage_service_daemon.py),
# so the key must be exported by then or the service falls back to ONNX-only
# and 422s every voyage-* collection. pytest inherits the same exports.
if [ -f "$REPO_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO_ROOT/.env"
  set +a
fi
# NEXUS_GATE_NO_VOYAGE=1: mask the Voyage key AFTER sourcing .env. A DEAD key
# hard-fails the voyage/CCE subset (the embed 401s surface as typed 502s),
# whereas an ABSENT key makes the same subset skip with a clear reason — the
# honest posture when the key is known-rotated/revoked (2026-07-13: the .env
# key is dead; rotation is operator work). The vacuity guard still enforces
# the skip BUDGET, so masking cannot silently hollow out the gate.
if [ -n "${NEXUS_GATE_NO_VOYAGE:-}" ]; then
  unset VOYAGE_API_KEY NX_VOYAGE_API_KEY
  echo "[gate] NEXUS_GATE_NO_VOYAGE=1 — voyage/CCE subset will SKIP (key masked)"
fi

# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() {
  set +e
  NX_LOCAL=1 NEXUS_CONFIG_DIR="$SCRATCH" uv run nx daemon service stop >/dev/null 2>&1
  # Stop the throwaway PG cluster if still up (pg_ctl from the provisioned
  # credentials; best-effort — the datadir removal below is the backstop).
  if [ -f "$SCRATCH/pg_credentials" ]; then
    # shellcheck disable=SC1090,SC1091
    source "$SCRATCH/pg_credentials" >/dev/null 2>&1
    pg_ctl -D "$SCRATCH/postgres" stop -m fast >/dev/null 2>&1
  fi
  rm -rf "$SCRATCH"
  # Backstop for a signal mid-jar-build: never leave a stamped
  # release.properties in the working tree (it bakes into later builds).
  git checkout -- "service/src/main/resources/META-INF/nexus/release.properties" 2>/dev/null || true
  echo "[gate] cleaned up"
}
trap cleanup EXIT

# 1. Provision the throwaway PG cluster.
NEXUS_CONFIG_DIR="$SCRATCH" uv run nx init --service

# 2. Rebuild the dev jar if stale, missing, or WRONG-STAMPED (rebuild-on-
#    key-miss only; a fresh, correctly-stamped jar re-run is a no-op).
#    Skipped when a native binary is supplied — the native path never
#    launches the jar, so freshness is moot.
#
#    Stamp discipline (2026-07-13, found by the 0.1.39->0.1.41 floor bump):
#    the cloud-probe-path tests in this gate require the service to report
#    release_version >= REQUIRED_ENGINE_VERSION, but a clean dev jar bakes a
#    BLANK stamp (-> null -> fail-closed), so the gate previously depended on
#    whatever stamped jar an earlier rehearsal happened to leave in
#    service/target — ambient machine state, the exact gate defect the
#    self-provisioning rule forbids. Now: derive the stamp from the floor
#    constant (same parse as migration-rehearsal/run.sh), treat a jar whose
#    baked stamp differs as stale, and stamp/restore release.properties
#    around the build so the jar always carries exactly the floor.
JAR="$REPO_ROOT/service/target/nexus-service-1.0-SNAPSHOT.jar"
RELEASE_PROPS="service/src/main/resources/META-INF/nexus/release.properties"
GATE_STAMP="$(python3 -c '
import re, pathlib
src = pathlib.Path("src/nexus/engine_version.py").read_text()
m = re.search(r"REQUIRED_ENGINE_VERSION[^=]*=\s*\((\d+),\s*(\d+),\s*(\d+)\)", src)
print(".".join(m.groups()) if m else "")
')"
[ -n "$GATE_STAMP" ] || { echo "[gate] FATAL: could not parse REQUIRED_ENGINE_VERSION" >&2; exit 2; }
if [ -z "${NEXUS_SERVICE_BIN:-}" ]; then
  JAR_SKIP_REASON="$(uv run python3 -c '
from tests.db._service_fixture import jar_freshness_skip_reason
print(jar_freshness_skip_reason() or "")
')"
  BAKED_STAMP=""
  [ -f "$JAR" ] && BAKED_STAMP="$(unzip -p "$JAR" META-INF/nexus/release.properties 2>/dev/null | sed -n 's/^release_version=//p')"
  if [ -n "$JAR_SKIP_REASON" ] || [ "$BAKED_STAMP" != "$GATE_STAMP" ]; then
    [ -n "$JAR_SKIP_REASON" ] && echo "[gate] $JAR_SKIP_REASON"
    [ "$BAKED_STAMP" != "$GATE_STAMP" ] && echo "[gate] jar stamp '${BAKED_STAMP:-<none>}' != floor '$GATE_STAMP' — rebuilding stamped"
    echo "[gate] rebuilding service jar (release_version=$GATE_STAMP)..."
    _restore_props() { git checkout -- "$RELEASE_PROPS" 2>/dev/null || true; }
    sed "s/^release_version=.*/release_version=${GATE_STAMP}/" "$RELEASE_PROPS" > "$RELEASE_PROPS.tmp" \
      && mv "$RELEASE_PROPS.tmp" "$RELEASE_PROPS"
    if ! (cd service && ./mvnw -q package -DskipTests); then
      _restore_props
      echo "[gate] ERROR: service jar rebuild failed — fix the Maven build and re-run:" >&2
      echo "         cd service && ./mvnw package -DskipTests" >&2
      exit 2
    fi
    _restore_props
  fi
fi

# 3. Resolve a launch artifact: installed native binary wins; dev jar fallback.
START_ENV=(NX_LOCAL=1 "NEXUS_CONFIG_DIR=$SCRATCH")
if [ -n "${NEXUS_SERVICE_BIN:-}" ]; then
  # No freshness check exists for a native binary (jar-mtime logic does not
  # apply) — log what is being pinned so a stale artifact is at least visible.
  echo "[gate] native binary: $NEXUS_SERVICE_BIN (mtime: $(stat -f '%Sm' "$NEXUS_SERVICE_BIN" 2>/dev/null || stat -c '%y' "$NEXUS_SERVICE_BIN"))"
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

# 4. Read the lease and run the gate through the env leg.
LEASE_JSON="$(cat "$SCRATCH"/storage_service_addr.*)"
SERVICE_PORT="$(printf '%s' "$LEASE_JSON" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('endpoint',d)['port'])")"
SERVICE_TOKEN="$(printf '%s' "$LEASE_JSON" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('endpoint',d)['token'])")"
echo "[gate] throwaway service on 127.0.0.1:$SERVICE_PORT"

# The NX_SERVICE_* env leg below pins the SERVICE at the throwaway
# instance (.env was sourced up top, before the service spawned). HOST/PORT
# halves only — deliberately NOT NX_SERVICE_URL: the URL leg outranks the
# per-module self-provisioning fixtures (tests/db/*) which pin their own
# HOST/PORT/TOKEN, so a gate-wide URL hijacks their requests to the wrong
# service (empirically: 17 fixture-family 401s, 2026-07-07). The T3 vector
# client honors the host/port halves since nexus-edwlp
# (service_endpoint.env_host_port_url).
# Bound the lived_in carve-out BEFORE the run (nexus-no210): the marker
# filter moves excluded tests into pytest's `deselected` bucket, which the
# passed/skipped guard below never sees — so tagging tests lived_in (to
# dodge a red test, or via careless merge) would silently shrink coverage.
# Exact count, not <=: growing the carve-out must be a conscious edit here.
LIVED_IN_EXPECTED=39
LIVED_IN_COUNT="$(uv run pytest -m "integration and lived_in" --collect-only -q 2>/dev/null | grep -cE '::' || true)"
if [ "$LIVED_IN_COUNT" -ne "$LIVED_IN_EXPECTED" ]; then
  echo "[gate] VACUITY GUARD TRIPPED: lived_in carve-out is $LIVED_IN_COUNT tests, expected exactly $LIVED_IN_EXPECTED" >&2
  echo "[gate] (a new lived_in mark must bump LIVED_IN_EXPECTED here, consciously)" >&2
  exit 1
fi

set +e
NX_SERVICE_HOST=127.0.0.1 NX_SERVICE_PORT="$SERVICE_PORT" NX_SERVICE_TOKEN="$SERVICE_TOKEN" \
  uv run pytest -m "integration and not lived_in" -q "$@" 2>&1 | tee "$SCRATCH/pytest.out"
STATUS=${PIPESTATUS[0]}
set -e

# 5. Vacuity guard (nexus-edwlp Task 6): pinned from the post-fix empirical
# full-gate run (2026-07-07, macOS: 446 passed / 31 skipped / 0 failed; the
# 31 = 24 CA-3-bundle-conditional + ~7 platform one-offs). A trip means real
# regression -- either fewer tests are actually executing (silent coverage
# loss creeping back in) or more are silently skipping again -- not ambient
# drift, so it fails the gate even if pytest itself reported exit 0. FLOOR
# carries a small allowance below the observed 446 for platform variation;
# BUDGET a small allowance above the observed 31. New legitimately-
# conditional tests must bump these consciously, in the same change.
# NX_GATE_FLOOR / NX_GATE_BUDGET: env overrides so CI (different platform,
# CA-3 bundle present => different counts) can pin its own numbers without
# editing this script.
FLOOR="${NX_GATE_FLOOR:-440}"
BUDGET="${NX_GATE_BUDGET:-40}"

SUMMARY_LINE="$(select_summary_line "$SCRATCH/pytest.out")"
PASSED_COUNT="$(parse_summary_count passed "$SUMMARY_LINE")"
SKIPPED_COUNT="$(parse_summary_count skipped "$SUMMARY_LINE")"

# pytest exit 0 with no parseable summary is itself anomalous — trip loudly.
if [ "$STATUS" -eq 0 ] && [ -z "$SUMMARY_LINE" ]; then
  echo "[gate] VACUITY GUARD TRIPPED: pytest exited 0 but no summary line was found" >&2
  STATUS=1
fi

# The guard only applies to the FULL run: pass-through pytest args (-k,
# file paths) legitimately shrink the selection, so a subset run would
# always trip the floor. NX_GATE_FORCE_GUARD=1 re-arms it for testing the
# trip path against a small subset.
if [ "$STATUS" -eq 0 ] && { [ "$#" -eq 0 ] || [ "${NX_GATE_FORCE_GUARD:-0}" = "1" ]; }; then
  if [ "$PASSED_COUNT" -lt "$FLOOR" ] || [ "$SKIPPED_COUNT" -gt "$BUDGET" ]; then
    echo "[gate] VACUITY GUARD TRIPPED: passed=$PASSED_COUNT (floor=$FLOOR) skipped=$SKIPPED_COUNT (budget=$BUDGET)" >&2
    STATUS=1
  fi
fi

if [ "$STATUS" -eq 0 ]; then
  echo "[gate] LOCAL-SERVICE GATE PASSED (passed=$PASSED_COUNT skipped=$SKIPPED_COUNT)"
else
  echo "[gate] LOCAL-SERVICE GATE FAILED (pytest exit ${STATUS}; passed=$PASSED_COUNT skipped=$SKIPPED_COUNT)"
fi
exit "$STATUS"
