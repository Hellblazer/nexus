#!/usr/bin/env bash
# Local-service functional gate (2026-07-06, born from the v6.3.6 release).
#
# WHY THIS EXISTS: the integration suite's local-service round-trip family
# (tests/test_integration.py T3 round-trips, scratch/MCP round-trips — several
# hundred tests) is the FUNCTIONAL TEST of local mode, one of the two shipped
# modes. Its skip-gate resolves a service via env vars or a local lease —
# never the managed cloud (deliberate: integration tests must not write junk
# into production). Before this script, the gate ran only when a local
# service HAPPENED to be running, so it silently degraded to 74/516 tests the
# day the ambient dev service died (v6.3.6 release, 2026-07-06). A functional
# gate must be self-provisioning, not a side quest.
#
# SELF-PROVISIONING (nexus-edwlp, 2026-07-07): infra is hermetic — the gate
# provisions its own PG + service and auto-rebuilds a stale dev jar. Two
# markers carve tests out, each with an exact-count guard below: `lived_in`
# (dispatches real `claude -p` or needs seeded lived-in corpora) and
# `cloud_mode`. A vacuity guard asserts passed/skipped stay within the pinned
# FLOOR/BUDGET. A guard trip means real regression, not ambient drift.
#
# TWO SEPARATE PROOFS, RE-SCOPED HONESTLY (nexus-x81ks, 2026-08-02, decision
# (b)-with-teeth). nexus-tmsnz found that the pytest family below is NOT
# actually exercising the throwaway service this script provisions: autouse
# conftest fixtures (_isolate_service_endpoint_env, _isolate_config_dir,
# _pin_t2_substrate — conftest.py) strip NX_SERVICE_* from every test body
# and route it at a SEPARATE substrate the suite boots ITSELF, session-scoped.
# A full re-wire (making the fixtures honor this gate's env instead) is
# structurally infeasible: the deep integration tests need superuser access
# to their OWN substrate PG for Liquibase/seeding/tenant-minting, which a
# foreign service cannot grant, and a partial re-wire would punch conditional
# holes in the isolation fixtures that exist to prevent tests from leaking
# into a live install. So the two proofs are kept SEPARATE and both honest:
#   - The pytest family (FLOOR/BUDGET below) is the functional surface of
#     local mode, proven against SELF-PROVISIONED substrates the suite
#     manages itself. It says nothing about whether the throwaway service
#     THIS SCRIPT boots actually works — see the next line.
#   - The DIRECT SMOKE LEG (step 5 below, before the pytest family runs) is
#     a counted, script-driven probe against THIS script's own throwaway
#     service — outside pytest, so immune to the autouse isolation. It is
#     what proves the shipped-shape service (native/stamped binary, real
#     daemon machinery) actually boots and serves: health, version identity,
#     catalog read/write, the RUNFENCE index-run fence, one vector round
#     trip. Exact-count non-vacuity (SMOKE_EXPECTED), fail-loud on any
#     mismatch or unreachable service — never silently skipped. NOTE
#     (nexus-308ph, landed 2026-08-02): release_version ALONE cannot
#     discriminate WHICH artifact is running — a pinned release binary and a
#     stamped dev jar built against the same floor bake the identical
#     release_version. build_ref, a per-run nonce stamped fresh into
#     release.properties by step 2 below on every invocation, IS the
#     discriminator of record — see the step (b)/(d) comments below. The
#     RUNFENCE index-run fence (step d) was the INTERIM discriminator (404
#     on a pre-fence pinned release, 200 on a fresh jar) but that signal dies
#     once a pinned release ships the fence routes (v0.1.62+); it stays as
#     supplementary coverage of the fence contract itself, not identity.
#
# THIS GATE IS BGE-768 ONLY (nexus-w6h2m, 2026-07-28). A local service embeds
# with bge-768 and nothing else — RDR-160 makes that the only valid value in
# the service-stack topology, and `nx init --service` stamps it. The service
# also serves ONE embedding mode at a time. So tests that need the SERVICE to
# embed voyage-* collections are cloud-mode tests and are marked `cloud_mode`
# and deselected here; the gate no longer needs a Voyage key for its own
# corpus.
#
# It used to carry those tests and pass, because it was never actually running
# local: the CHROMA_* secrets made is_local_mode() answer False, `nx init
# --service` returned before stamping local.embed_model, and the client built
# voyage collections matching a voyage-mode service. Removing those dead
# credentials (534251da, RDR-155 P4b P3) made the client honestly bge-768 and
# exposed a corpus split across two modes the service cannot serve at once.
# Do not "fix" that by routing the Voyage key — it only swaps which half 422s
# (tried in 6b04cd1b, reverted in 699369b0) and reintroduces the exact failure
# nexus-r5f3c exists to prevent.
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
#   4. Reads back its lease.
#   5. Runs the direct smoke leg (nexus-x81ks) against THIS throwaway
#      service — health, version identity, catalog round-trip, RUNFENCE
#      fence round-trip, one vector round trip. Counted, fail-loud; see the
#      "TWO SEPARATE PROOFS" note above.
#   6. Sources .env (repo root) for cloud API keys, then runs
#      `pytest -m "integration and not lived_in"` with NX_SERVICE_HOST/
#      PORT/TOKEN set (harmless legacy env — the autouse isolation fixtures
#      strip it for every test body; see the re-scope note above), so the
#      self-provisioned-substrate local-service family runs deterministically.
#   7. Parses the pytest summary and asserts passed >= FLOOR and
#      skipped <= BUDGET (the vacuity guard) — see the numbers pinned
#      below.
#   8. Tears everything down (service, PG, scratch dir), even on failure.
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
  # Pipe-free tail (nexus-i66g4/wbeyi class): take the first line via
  # parameter expansion instead of `| head -1` -- under this script's
  # `set -euo pipefail`, a still-writing grep closed early by head risks
  # its SIGPIPE getting promoted over head's own (successful) exit status.
  n="$(grep -oE "[0-9]+ ${label}" <<<"$text" | grep -oE '^[0-9]+')"
  n="${n%%$'\n'*}"
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
  # Strip ANSI before matching. --color=no above is the primary fix; this is
  # the belt, because the guard this feeds must not be defeatable by anything
  # that colourises the stream (a future wrapper, FORCE_COLOR in someone's
  # env, a CI runner that allocates a PTY). A guard that silently inverts on
  # a formatting change is worse than no guard.
  sed $'s/\033\[[0-9;]*m//g' "$1" \
    | grep -E '^(=+ )?[0-9]+ (failed|passed|skipped|deselected|error|xfailed|xpassed|warning)[a-z]*(,.*)? in [0-9.]+s' \
    | tail -1 || true
}

# ── Smoke-leg non-vacuity guard (nexus-x81ks) ───────────────────────────────
# smoke_verify_count PASSED EXPECTED — the direct-smoke leg's tail check
# (called for real after the live sequence, step 5 below). Defined up here,
# alongside the summary-line parser above, so NX_GATE_SELFTEST can exercise
# the guard logic itself with no live service: every smoke_fail call in the
# live sequence already exits 1 immediately on a failed assertion, so by the
# time this runs PASSED should always equal EXPECTED — this is the belt-and-
# suspenders check for a bug where an assertion path silently never
# incremented SMOKE_PASSED without erroring (same discipline as the pytest
# FLOOR/BUDGET guard further down). Returns 1 (does not exit) so callers choose.
smoke_verify_count() {
  local passed="$1" expected="$2"
  if [ "$passed" -ne "$expected" ]; then
    echo "[gate] SMOKE LEG VACUITY GUARD TRIPPED: passed=$passed expected=$expected" >&2
    return 1
  fi
  echo "[gate] SMOKE LEG: passed=$passed expected=$expected"
  return 0
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

  # smoke_verify_count (nexus-x81ks): the smoke leg's own non-vacuity guard,
  # exercised directly since it needs no live service — a match must pass
  # (rc 0), a mismatch must fail (rc 1). Output is suppressed (>/dev/null):
  # only the mismatch case prints to stderr, which the selftest deliberately
  # provokes and does not want mistaken for a real trip in CI logs.
  if smoke_verify_count 11 11 >/dev/null 2>&1; then
    echo "[gate-selftest] ok (smoke_verify_count: match passes)"
  else
    echo "[gate-selftest] FAIL (smoke_verify_count: 11/11 should pass)" >&2
    selftest_failed=1
  fi
  if smoke_verify_count 9 11 >/dev/null 2>&1; then
    echo "[gate-selftest] FAIL (smoke_verify_count: 9/11 mismatch should fail)" >&2
    selftest_failed=1
  else
    echo "[gate-selftest] ok (smoke_verify_count: mismatch fails)"
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

# ── Fence the REAL config dir (nexus-pfuns follow-up) ────────────────────────
#
# Pinning NEXUS_CONFIG_DIR per-invocation is necessary and NOT sufficient.
# ``config.nexus_config_dir()`` falls back to ``Path.home()/".config"/"nexus"``
# whenever that variable is absent, so ANY process in this tree that does not
# inherit it -- a subprocess with a scrubbed env, a helper invoked through a
# shell that resets it, another gate running concurrently on this box -- writes
# the OPERATOR'S REAL config dir instead. The env pin is load-bearing with
# nothing underneath it.
#
# Observed twice, both recorded in this script:
#   2026-07-13  get_credential()'s config.yml fallback read the operator's real
#               ~/.config/nexus/config.yml from inside this "fully isolated"
#               gate. Fixed by pinning the variable; the fallback was left open.
#   2026-08-24  the pfuns guard caught last_seen_version stamped 7.16.3 -- the
#               INSTALLED tool's version, not the tree under test -- reddening a
#               release leg in which all 560 tests passed.
#
# THE MIRROR IS A DENYLIST, NOT AN ALLOWLIST, and that distinction is the whole
# design. The first attempt symlinked a hand-picked set (.cache/.local/.claude)
# back into a fresh HOME and broke on the SECOND thing it touched: the Maven
# jar rebuild died because ~/.testcontainers.properties (which carries
# ``testcontainers.ryuk.disabled=true``) and ~/.docker (which holds the socket
# at ~/.docker/run/docker.sock) were both absent, so testcontainers fell back
# to its ryuk-enabled default and could not reach the daemon. An allowlist of
# "things HOME is for" cannot be completed by enumeration -- every tool that
# reads $HOME would have to be known in advance.
#
# So: mirror EVERYTHING, shadow ONE path. Every top-level entry of the real
# HOME is symlinked through; ~/.config is recreated as a real directory whose
# entries are likewise symlinked through EXCEPT ``nexus``, which becomes a
# fresh empty scratch dir. Net effect: exactly one path in the whole home is
# fenced, and any tool needing anything else is unaffected by construction.
#
# NOT A LAYER: sandbox-exec. An earlier revision created a Seatbelt profile
# denying file-write* under the real config dir, printed "real-config writes
# denied", and then only exported the profile path -- NOTHING consumed it. It
# was an inert guard advertising protection it did not provide. Wiring it up
# for real was then measured and REJECTED: `ps` is blocked under sandbox-exec
# even with explicit (allow process-exec*) (allow process-info*), and this
# repo's conftest substrate sweep shells out to `ps`, so a sandboxed pytest leg
# errors on collection. Do not re-add it without re-measuring that.

REAL_HOME="$HOME"

# LAYER 1 -- HOME mirror, shadowing only ~/.config/nexus. The implementation
# lives in lib/fence_home.sh so the test suite drives THIS code rather than a
# reimplementation of it.
# shellcheck source=tests/e2e/lib/fence_home.sh
source "$REPO_ROOT/tests/e2e/lib/fence_home.sh"
GATE_HOME="$SCRATCH/home"
fence_home "$REAL_HOME" "$GATE_HOME" ".config/nexus"
export HOME="$GATE_HOME"
# uv resolves its cache off HOME at process start; pin it explicitly so the
# mirror is not the only thing between this gate and a cold 250-package
# resolve.
export UV_CACHE_DIR="${UV_CACHE_DIR:-$REAL_HOME/.cache/uv}"

# LAYER 2 -- PATH. A bare ``nx`` resolves to the INSTALLED tool, a different
# build from the tree under test: the 2026-08-24 stamp read 7.16.3 while the
# tree was 7.17.0. Every legitimate call here goes through ``uv run nx``, so a
# bare one is always a mistake and must fail LOUDLY naming its caller rather
# than quietly succeeding against production state.
GATE_SHIM="$SCRATCH/shim"
mkdir -p "$GATE_SHIM"
cat > "$GATE_SHIM/nx" <<'SHIM'
#!/bin/sh
echo "FATAL: bare 'nx' called inside local-service-gate (PPID=$PPID)." >&2
echo "  It resolves to the INSTALLED tool, not the tree under test, and it" >&2
echo "  writes the real ~/.config/nexus. Use 'uv run nx'." >&2
exit 1
SHIM
chmod +x "$GATE_SHIM/nx"
export PATH="$GATE_SHIM:$PATH"

# NOTE: no "nx <word>" sequence in this string. tests/test_release_artifact_verb_rot.py
# extracts `nx <verb>` from release artifacts and checks the verb exists; prose like
# a bare tool name followed by a word parses as a verb and reddens develop (it did).
echo "[gate] fenced: HOME mirrored to $HOME, only ~/.config/nexus shadowed; PATH shim active"

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

# nexus-iws18: snapshot release.properties' ACTUAL BYTES now, and restore those
# on every exit path. The old `git checkout -- <path>` reverted to HEAD, which
# silently destroyed any UNCOMMITTED edit to that file on every gate run (it
# bit the nexus-308ph implementer twice mid-verification). Same byte-snapshot
# shape scripts/build-gate-jar.sh uses; the two stay separate because this
# gate's restore choreography differs (mid-run _restore_props + EXIT backstop).
RELEASE_PROPS="$REPO_ROOT/service/src/main/resources/META-INF/nexus/release.properties"
RELEASE_PROPS_SNAPSHOT="$(mktemp "${TMPDIR:-/tmp}/release.properties.snapshot.XXXXXX")"
cp "$RELEASE_PROPS" "$RELEASE_PROPS_SNAPSHOT"

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
  # Restore the pre-invocation BYTES, never HEAD (nexus-iws18).
  cp "$RELEASE_PROPS_SNAPSHOT" "$RELEASE_PROPS" 2>/dev/null || true
  rm -f "$RELEASE_PROPS_SNAPSHOT"
  echo "[gate] cleaned up"
}
trap cleanup EXIT

# 1. Provision the throwaway PG cluster.
NEXUS_CONFIG_DIR="$SCRATCH" uv run nx init --service

# `nx init --service` does not stop at provisioning: it also installs the
# CURRENT PINNED RELEASE native binary (REQUIRED_ENGINE_VERSION) and starts
# it (commands/init.py -> ensure_storage_supervisor), publishing a live
# lease. Left running, that lease makes step 3 below a NO-OP: daemon start
# is idempotent on an existing lease (commands/daemon.py
# ensure_storage_supervisor — the LOAD-BEARING short-circuit, at
# commands/daemon.py:597-633 — returns the live lease without ever
# inspecting NEXUS_SERVICE_JAR/BIN or even spawning a subprocess, so
# storage_service_daemon.py's own _start_locked copy of the check is never
# reached either) — found live 2026-08-02 while adding the nexus-x81ks smoke leg: the
# RUNFENCE routes (unreleased, post-v0.1.61) 404'd even though step 2 had
# just rebuilt a jar containing them, because the ACTUAL running process the
# whole time was the pinned release binary init installed, never the fresh
# build (nexus-4e96a). Stop it here so step 3's launch-artifact selection
# (native override / dev jar / re-installed pinned binary) is what actually
# runs — PG is left up (`stop` never touches it, by design).
echo "[gate] stopping the pinned-release service nx init auto-started (nexus-4e96a)"
NX_LOCAL=1 NEXUS_CONFIG_DIR="$SCRATCH" uv run nx daemon service stop

# 2. Rebuild the dev jar, UNCONDITIONALLY stamped, on every invocation.
#    Skipped when a native binary is supplied — the native path never
#    launches the jar, so freshness is moot (and, per nexus-308ph below, a
#    native artifact was built from a tag, never from THIS gate run, so it
#    structurally cannot carry a matching build_ref — that is the intended
#    failure mode, not a gap to work around).
#
#    Stamp discipline (2026-07-13, found by the 0.1.39->0.1.41 floor bump):
#    the cloud-probe-path tests in this gate require the service to report
#    release_version >= REQUIRED_ENGINE_VERSION, but a clean dev jar bakes a
#    BLANK stamp (-> null -> fail-closed), so the gate previously depended on
#    whatever stamped jar an earlier rehearsal happened to leave in
#    service/target — ambient machine state, the exact gate defect the
#    self-provisioning rule forbids. release_version is derived from the
#    floor constant (same parse as migration-rehearsal/run.sh).
#
#    nexus-308ph (2026-08-02): ALSO stamps build_ref, a per-run nonce
#    (<git short sha>+<epoch seconds>-<pid>) — mirrors
#    scripts/build-gate-jar.sh's own stamp. This is why the rebuild is now
#    UNCONDITIONAL rather than rebuild-on-key-miss: build_ref must be fresh
#    on every single invocation, so a "reuse the jar if release_version
#    already matches" shortcut would leave a STALE nonce baked into a reused
#    jar — one that can never equal the value THIS run computes below,
#    permanently hard-failing the smoke leg's discriminator assertion
#    (step 5b) for no real reason. Stamp/restore release.properties around
#    the build so the jar always carries exactly these two values.
JAR="$REPO_ROOT/service/target/nexus-service-1.0-SNAPSHOT.jar"
# RELEASE_PROPS + its byte snapshot are set once, above cleanup() (nexus-iws18).
GATE_STAMP="$(python3 -c '
import re, pathlib
src = pathlib.Path("src/nexus/engine_version.py").read_text()
m = re.search(r"REQUIRED_ENGINE_VERSION[^=]*=\s*\((\d+),\s*(\d+),\s*(\d+)\)", src)
print(".".join(m.groups()) if m else "")
')"
[ -n "$GATE_STAMP" ] || { echo "[gate] FATAL: could not parse REQUIRED_ENGINE_VERSION" >&2; exit 2; }
GATE_BUILD_REF=""
if [ -z "${NEXUS_SERVICE_BIN:-}" ]; then
  JAR_SKIP_REASON="$(uv run python3 -c '
from tests.db._service_fixture import jar_freshness_skip_reason
print(jar_freshness_skip_reason() or "")
')"
  [ -n "$JAR_SKIP_REASON" ] && echo "[gate] $JAR_SKIP_REASON"
  # Nonce shape mirrors scripts/build-gate-jar.sh (intentional duplication:
  # the gate must hold the expected value in its own process for the
  # smoke-leg compare, and the restore choreography differs) — keep in step.
  GATE_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"
  GATE_BUILD_REF="${GATE_SHA}+$(date +%s)-$$"
  echo "[gate] rebuilding service jar (release_version=$GATE_STAMP build_ref=$GATE_BUILD_REF)..."
  # Pre-invocation bytes, never `git checkout` (nexus-iws18: HEAD is not
  # what was in the tree when the gate started).
  _restore_props() { cp "$RELEASE_PROPS_SNAPSHOT" "$RELEASE_PROPS"; }
  sed -e "s/^release_version=.*/release_version=${GATE_STAMP}/" \
      -e "s/^build_ref=.*/build_ref=${GATE_BUILD_REF}/" \
      "$RELEASE_PROPS" > "$RELEASE_PROPS.tmp" \
    && mv "$RELEASE_PROPS.tmp" "$RELEASE_PROPS"
  # nexus-c00dw: mvnw-leased.sh takes the single-builder lease around this
  # ./mvnw call itself (cds into service/ internally) — never call the bare
  # ./mvnw here, or this gate can collide with a concurrent build.
  if ! "$REPO_ROOT/scripts/mvnw-leased.sh" -q package -DskipTests; then
    _restore_props
    echo "[gate] ERROR: service jar rebuild failed — fix the Maven build and re-run:" >&2
    echo "         scripts/mvnw-leased.sh package -DskipTests" >&2
    exit 2
  fi
  _restore_props
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
  echo "         scripts/mvnw-leased.sh -q package -DskipTests" >&2
  echo "       or export NEXUS_SERVICE_BIN=<native binary>" >&2
  exit 2
fi
env "${START_ENV[@]}" uv run nx daemon service start

# 4. Read the lease.
LEASE_JSON="$(cat "$SCRATCH"/storage_service_addr.*)"
SERVICE_PORT="$(printf '%s' "$LEASE_JSON" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('endpoint',d)['port'])")"
SERVICE_TOKEN="$(printf '%s' "$LEASE_JSON" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('endpoint',d)['token'])")"
echo "[gate] throwaway service on 127.0.0.1:$SERVICE_PORT"

# ── 5. Direct smoke leg (nexus-x81ks) ───────────────────────────────────────
# A counted, script-driven probe of THIS throwaway service — outside pytest,
# so immune to the autouse isolation fixtures that route every pytest test
# body at its own self-provisioned substrate (see the "TWO SEPARATE PROOFS"
# header note). This is what proves the shipped-shape service (native/
# stamped binary, real daemon machinery) actually boots and serves: health,
# version identity, catalog read/write, the RUNFENCE index-run fence, and one
# vector round trip. Exact-count non-vacuity, same discipline as
# LIVED_IN_EXPECTED / CLOUD_MODE_EXPECTED below: every assertion increments
# SMOKE_PASSED, and a mismatch against SMOKE_EXPECTED FAILS the gate — an
# unreachable service or a malformed response fails loud, never skips.
SMOKE_EXPECTED=11  # 12->11 at 3b2901141: the manifest/verify leg was retired
                   # with the catalog-030 subtraction but the count was not
                   # lowered, making the gate structurally unpassable (caught
                   # by its own vacuity guard in the 7.8.0 battery).
SMOKE_PASSED=0
SMOKE_BASE="http://127.0.0.1:$SERVICE_PORT"
SMOKE_DIR="$SCRATCH/smoke"
mkdir -p "$SMOKE_DIR"
SMOKE_AUTH=(-H "Authorization: Bearer $SERVICE_TOKEN")

# smoke_request METHOD PATH [JSON_BODY] — writes the response body to
# $SMOKE_DIR/resp.json and sets SMOKE_CODE to the HTTP status ("000" on a
# connection-level failure, e.g. connection refused — curl exits nonzero
# before any status line is available; never silently treated as any other
# code).
smoke_request() {
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS -o "$SMOKE_DIR/resp.json" -w '%{http_code}' -X "$method" "${SMOKE_AUTH[@]}")
  if [ -n "$body" ]; then
    args+=(-H 'Content-Type: application/json' -d "$body")
  fi
  SMOKE_CODE="$(curl "${args[@]}" "$SMOKE_BASE$path" 2>"$SMOKE_DIR/curl.err")" || true
  [ -n "$SMOKE_CODE" ] || SMOKE_CODE="000"
}

smoke_fail() {
  echo "[gate] SMOKE LEG FAILED: $1 (code=${SMOKE_CODE:-n/a})" >&2
  [ -f "$SMOKE_DIR/resp.json" ] && echo "[gate]   body: $(cat "$SMOKE_DIR/resp.json")" >&2
  # Connection-refused / connection-level failures leave resp.json empty (or
  # absent) — curl's own error text (curl.err) is the only diagnostic in
  # that case, so surface it too.
  [ -s "$SMOKE_DIR/curl.err" ] && echo "[gate]   curl: $(cat "$SMOKE_DIR/curl.err" 2>/dev/null)" >&2
  exit 1
}

# smoke_check DESC PYTHON_EXPR — PYTHON_EXPR is a python expression over `d`,
# the JSON-decoded $SMOKE_DIR/resp.json (dict or list, whatever the endpoint
# returns). A falsy expression or a JSON-decode failure fails the leg loud.
smoke_check() {
  local desc="$1" expr="$2"
  python3 -c "
import json, sys
d = json.load(open('$SMOKE_DIR/resp.json'))
sys.exit(0 if bool($expr) else 1)
" || smoke_fail "$desc"
  SMOKE_PASSED=$((SMOKE_PASSED + 1))
  echo "[gate] smoke ok ($SMOKE_PASSED/$SMOKE_EXPECTED): $desc"
}

SMOKE_UID="$$"

# a. GET /health -> 200 + {"status":"ok","db":"up"}.
smoke_request GET /health
[ "$SMOKE_CODE" = "200" ] || smoke_fail "GET /health"
smoke_check "GET /health -> status=ok db=up" "d.get('status')=='ok' and d.get('db')=='up'"

# b. Version identity — this throwaway is a STAMPED rebuild (step 2 above),
# so this is an exact match against GATE_STAMP, not a floor comparison.
# release_version ALONE cannot discriminate WHICH artifact is actually
# running — a pinned release binary and a freshly-stamped dev jar both bake
# the identical release_version by construction. build_ref (nexus-308ph) is
# what closes that gap: a per-run nonce (step 2 above), stamped fresh on
# every invocation, that no pinned release — past or future — can ever bake,
# because no release is ever built BY this gate run. A missing build_ref
# field (an artifact built before this change, or the pinned-release
# short-circuit nexus-4e96a exists to catch) or a value mismatch (a stale
# reused jar, or the wrong artifact serving) is a HARD FAIL naming
# nexus-4e96a — this IS the artifact-identity discriminator of record.
#
# NOTE (all smoke_check calls below that splice a shell var into their
# PYTHON_EXPR arg, starting with $GATE_STAMP here): unlike the REQUEST side
# (smoke_request bodies are always built through python3 json.dumps, which
# escapes for JSON), the ASSERTION side is spliced into raw python SOURCE
# with no escaping. Every value used here (GATE_STAMP, GATE_BUILD_REF,
# SMOKE_UID and its derivatives) is script-controlled and shell-safe today —
# keep it that way; never splice untrusted or free-form text into a
# PYTHON_EXPR.
smoke_request GET /version
[ "$SMOKE_CODE" = "200" ] || smoke_fail "GET /version"
smoke_check "GET /version -> release_version==$GATE_STAMP" "d.get('release_version')=='$GATE_STAMP'"
if [ -n "${NEXUS_SERVICE_BIN:-}" ]; then
  # Native-binary mode skips the stamped rebuild (step 2), so GATE_BUILD_REF
  # is empty and an equality compare could NEVER pass (d.get() yields None,
  # never ''). The discriminating invariant for this mode is the inverse:
  # native release builds NEVER stamp build_ref, so a PRESENT field means a
  # stray jar is serving instead of $NEXUS_SERVICE_BIN.
  smoke_check "GET /version -> build_ref ABSENT (native binary never stamps it; a present field means a stray jar is serving, not \$NEXUS_SERVICE_BIN — nexus-308ph)" \
    "d.get('build_ref') is None"
else
  smoke_check "GET /version -> build_ref==$GATE_BUILD_REF (nexus-308ph artifact-identity discriminator; missing/mismatched means the served process is not the jar THIS run built — the nexus-4e96a short-circuit)" \
    "d.get('build_ref')=='$GATE_BUILD_REF'"
fi

# c. Catalog round-trip: owner upsert -> doc/register -> show, title round-trips.
SMOKE_OWNER_PREFIX="9.$SMOKE_UID"
smoke_request POST /v1/catalog/owners/upsert \
  "$(python3 -c "import json;print(json.dumps({'tumbler_prefix':'$SMOKE_OWNER_PREFIX','name':'gate-smoke-owner','owner_type':'gate_smoke'}))")"
[ "$SMOKE_CODE" = "200" ] || smoke_fail "POST /v1/catalog/owners/upsert"
smoke_check "POST /v1/catalog/owners/upsert -> ok" "d.get('ok') is True"

SMOKE_TITLE="gate-smoke-doc-$SMOKE_UID"
smoke_request POST /v1/catalog/doc/register \
  "$(python3 -c "import json;print(json.dumps({'owner_prefix':'$SMOKE_OWNER_PREFIX','title':'$SMOKE_TITLE','content_type':'knowledge','file_path':'/gate-smoke/$SMOKE_UID.md'}))")"
[ "$SMOKE_CODE" = "200" ] || smoke_fail "POST /v1/catalog/doc/register"
smoke_check "POST /v1/catalog/doc/register -> tumbler under $SMOKE_OWNER_PREFIX" \
  "isinstance(d.get('tumbler'), str) and d['tumbler'].startswith('$SMOKE_OWNER_PREFIX.')"
SMOKE_DOC_TUMBLER="$(python3 -c "import json;print(json.load(open('$SMOKE_DIR/resp.json'))['tumbler'])")"

smoke_request GET "/v1/catalog/show?tumbler=$SMOKE_DOC_TUMBLER"
[ "$SMOKE_CODE" = "200" ] || smoke_fail "GET /v1/catalog/show"
smoke_check "GET /v1/catalog/show -> title round-trips" "d.get('title')=='$SMOKE_TITLE'"

# d. Fence round-trip (RUNFENCE routes, free coverage on a shipped-shape
# service): begin -> fail -> a subsequent show reflects index_state='failed'.
# Deliberately NOT begin -> complete(chunk_count=0): the client must never
# claim completion with zero chunks, so exercising that call shape here would
# smoke-test a request no honest client makes.
#
# HISTORICAL NOTE (nexus-308ph, resolved 2026-08-02): before build_ref
# landed (step (b) above), THIS fence round-trip was the artifact-identity
# discriminator by accident — RUNFENCE was unreleased (pre-v0.1.62), so it
# 404'd against the pinned release binary and 200'd only against a fresh dev
# build. That signal necessarily dies once a pinned release ships the fence
# routes (v0.1.62+), which is exactly why it was never durable. It stays
# here now purely as functional coverage of the fence CONTRACT itself
# (begin/fail/show state transitions) — build_ref is the discriminator of
# record; this step no longer carries that responsibility.
smoke_request POST /v1/catalog/index-run/begin \
  "$(python3 -c "import json;print(json.dumps({'doc_id':'$SMOKE_DOC_TUMBLER','run_id':'gate-smoke-$SMOKE_UID','collection':'knowledge__gate-smoke__bge-base-en-v15-768__v1'}))")"
[ "$SMOKE_CODE" = "200" ] || smoke_fail "POST /v1/catalog/index-run/begin"
smoke_check "POST /v1/catalog/index-run/begin -> ok" "d.get('ok') is True"

# GET /v1/catalog/manifest/verify smoke check REMOVED (RDR-191 Phase 6,
# nexus-o8dil.33) — the route is retired; the manifest-chunk FK makes the
# dangling state it diagnosed unreachable. completeIndexRun's internal use
# of the same underlying SQL function is exercised by the /complete calls
# elsewhere in this script, not by this now-gone read-only route.

smoke_request POST /v1/catalog/index-run/fail \
  "$(python3 -c "import json;print(json.dumps({'doc_id':'$SMOKE_DOC_TUMBLER','error':'gate-smoke synthetic failure'}))")"
[ "$SMOKE_CODE" = "200" ] || smoke_fail "POST /v1/catalog/index-run/fail"
smoke_check "POST /v1/catalog/index-run/fail -> ok" "d.get('ok') is True"

smoke_request GET "/v1/catalog/show?tumbler=$SMOKE_DOC_TUMBLER"
[ "$SMOKE_CODE" = "200" ] || smoke_fail "GET /v1/catalog/show (post-fail)"
smoke_check "GET /v1/catalog/show -> index_state==failed (fence round-trip)" "d.get('index_state')=='failed'"

# e. Vector leg: one tiny doc, upsert then search-returns-it. Safe to include
# unconditionally — the bge-768 ONNX model lives in the machine-wide
# ~/.cache/nexus/onnx_models/ cache, not scratch-isolated, so a box with any
# prior local-mode use already has it (verified present on this box; no
# download is triggered by this leg). NEXUS_GATE_NO_VECTOR_SMOKE=1 drops the
# leg (and SMOKE_EXPECTED with it) if that assumption stops holding somewhere.
if [ -z "${NEXUS_GATE_NO_VECTOR_SMOKE:-}" ]; then
  SMOKE_CHASH="$(python3 -c "import hashlib;print(hashlib.sha256(b'gate-smoke-chunk-$SMOKE_UID').hexdigest())")"
  SMOKE_VEC_COLLECTION="knowledge__gate-smoke__bge-base-en-v15-768__v1"
  SMOKE_CHUNK_TEXT="gate smoke vector round-trip probe $SMOKE_UID"
  smoke_request POST /v1/vectors/upsert-chunks \
    "$(python3 -c "import json;print(json.dumps({'collection':'$SMOKE_VEC_COLLECTION','ids':['$SMOKE_CHASH'],'documents':['$SMOKE_CHUNK_TEXT'],'metadatas':[{'source':'gate-smoke'}]}))")"
  [ "$SMOKE_CODE" = "200" ] || smoke_fail "POST /v1/vectors/upsert-chunks"
  smoke_check "POST /v1/vectors/upsert-chunks -> upserted=1" "d.get('upserted')==1"

  smoke_request POST /v1/vectors/search \
    "$(python3 -c "import json;print(json.dumps({'query':'$SMOKE_CHUNK_TEXT','collections':['$SMOKE_VEC_COLLECTION'],'n_results':5}))")"
  [ "$SMOKE_CODE" = "200" ] || smoke_fail "POST /v1/vectors/search"
  smoke_check "POST /v1/vectors/search -> returns the upserted chunk" \
    "any(r.get('id')=='$SMOKE_CHASH' for r in d)"
else
  echo "[gate] NEXUS_GATE_NO_VECTOR_SMOKE=1 — vector leg skipped; SMOKE_EXPECTED lowered"
  SMOKE_EXPECTED=9
fi

smoke_verify_count "$SMOKE_PASSED" "$SMOKE_EXPECTED" || exit 1

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
# 2026-08-21: 39 -> 41 for nexus-nyry9.11's two RDR-196 Phase 1 MVV tests
# (tests/integration/test_nx_answer_step_telemetry_mvv.py, module-marked
# lived_in: isolated and bundled step-telemetry round trips).
# 2026-08-21: 41 -> 42 for nexus-nyry9.16's RDR-196 .p2c A/B harness
# (tests/integration/test_rdr_196_p2c_ab_measurement.py, module-marked
# lived_in: dispatches real claude -p, spends real money).
# 2026-08-23: 42 -> 72 for nexus-pc15o, the fix for eight red nightlies
# (2026-08-16..08-23). Three RDR-196 live-dispatch families landed marked
# `integration` only while their six siblings were marked lived_in:
#   +18 tests/test_operator_proxy_controls.py            (nexus-nyry9.14)
#   +11 tests/test_operator_dispatch.py                  (nexus-nyry9.16)
#       ::TestClaudeDispatchLiveUsage (1)
#       ::TestClaudeDispatchPerOperatorSchemaCheapTier (10)
#   + 1 tests/test_aspect_extraction_cost_measurement.py (nexus-nyry9.6)
#       ::TestLiveMeasurement
# All 30 gate on an authenticated `claude` CLI, which a CI runner does not
# have and should not have (live model dispatch in a nightly = real money).
# So they SKIPPED there, and 21 legitimate skips + 30 of these = 51 against
# BUDGET=25 -- the gate went red for eight nights with ZERO failing tests
# (run 32630987956: "573 passed, 51 skipped"). The BUDGET is deliberately
# NOT raised: it is the guard that caught this, and it was right. Marking
# these carves them out of the population it measures, which is what the
# lived_in marker is for. Post-fix skips return to exactly 21 -- the count
# of the last green run (32403015822, 2026-08-20T18:24).
LIVED_IN_EXPECTED=72
LIVED_IN_COUNT="$(uv run pytest -m "integration and lived_in" --collect-only -q 2>/dev/null | grep -cE '::' || true)"
if [ "$LIVED_IN_COUNT" -ne "$LIVED_IN_EXPECTED" ]; then
  echo "[gate] VACUITY GUARD TRIPPED: lived_in carve-out is $LIVED_IN_COUNT tests, expected exactly $LIVED_IN_EXPECTED" >&2
  echo "[gate] (a new lived_in mark must bump LIVED_IN_EXPECTED here, consciously)" >&2
  exit 1
fi

# The cloud_mode carve-out (nexus-w6h2m, 2026-07-28). Same exact-count
# discipline as lived_in above, and for the same reason.
#
# WHY IT EXISTS: this gate's corpus spanned TWO embedding modes while the
# service serves exactly one. Three tests require the SERVICE to embed
# voyage-* collections; everything else here is a bge-768 local install,
# which is what RDR-160 says a local service IS. Whichever way the Voyage key
# was routed, one half 422'd:
#     key withheld -> embedding mode onnx-local -> the voyage tests 422
#     key plumbed  -> embedding mode voyage     -> the bge tests 422
# There was no value of that knob that made the gate green, which is why the
# first attempt at this (6b04cd1b, reverted in 699369b0) traded one 422
# cluster for its mirror image and reintroduced the exact failure nexus-r5f3c
# exists to prevent.
#
# It passed historically only because it was never running local: the CHROMA_*
# secrets made is_local_mode() answer False, so `nx init --service` returned
# before stamping local.embed_model and the client built voyage collections
# that matched a voyage-mode service. Removing those dead credentials
# (534251da, RDR-155 P4b P3) made the client honestly bge-768 and exposed the
# split corpus.
#
# So the three go, and they go by MARKER rather than by -k pattern so the
# reason travels with the test. They still need a cloud-mode home — see
# tests/e2e/cloud-client-path-gate.sh and nexus-w6h2m; until they have one the
# voyage/CCE embedding path has NO gate, and that is a known, recorded gap
# rather than an accident.
CLOUD_MODE_EXPECTED=3
CLOUD_MODE_COUNT="$(uv run pytest -m "integration and cloud_mode" --collect-only -q 2>/dev/null | grep -cE '::' || true)"
if [ "$CLOUD_MODE_COUNT" -ne "$CLOUD_MODE_EXPECTED" ]; then
  echo "[gate] VACUITY GUARD TRIPPED: cloud_mode carve-out is $CLOUD_MODE_COUNT tests, expected exactly $CLOUD_MODE_EXPECTED" >&2
  echo "[gate] (a new cloud_mode mark must bump CLOUD_MODE_EXPECTED here, consciously —" >&2
  echo "[gate]  the marker must never become a place to park a red test)" >&2
  exit 1
fi

set +e
# NEXUS_CONFIG_DIR pinned to the scratch dir (2026-07-13): without it,
# get_credential()'s config.yml fallback read the OPERATOR's real
# ~/.config/nexus/config.yml from inside the "fully isolated" gate — a
# dead voyage credential there hard-failed the voyage subset even with
# the env key masked, and any credential there can leak into gate runs.
# NEXUS_PG_BIN pinned to the scratch bundle (2026-07-19): tests that
# provision their OWN throwaway cluster (test_pg_provision, the rdr182
# forensics MVV, nexus_diag) override NEXUS_CONFIG_DIR to a tmp dir, which
# makes bundle discovery (config-dir-relative, pg_provision step 1.5) lose
# sight of the gate's extracted bundle. On a bundle-only box (no system PG
# — the shipped configuration) that family silently depended on ambient
# Homebrew PG. Export the gate's own bundle explicitly: self-provisioning,
# never ambient.
GATE_PG_BIN="$SCRATCH/pg-bundle/bundle/bin"
if [ -x "$GATE_PG_BIN/initdb" ]; then
  export NEXUS_PG_BIN="$GATE_PG_BIN"
fi   # else: host-PG / dev mode — fall through to auto-discovery
# --color=no: this output is PARSED, so it must not depend on whether the
# caller has a TTY. `tee` writes to stdout as well as the file, so pytest
# sees a terminal when a human runs the gate by hand — exactly how the
# release checklist says to run it — and colorises. The summary line then
# begins with an escape sequence, the ^-anchored selector below misses it,
# and the vacuity guard trips on a run where every test passed. Observed
# live 2026-08-01: 467 passed, gate reported FAILED (passed=0).
#
# -rs: name every skip. The BUDGET trip below is undiagnosable from CI logs
# without the reasons (2026-08-02: two nightly reds could report only a
# count, never which tests). Costs a few summary lines on a green run.
#
# NOTE this comment block must stay ABOVE the command, never inside the
# backslash continuation: a continued line that lands on a comment ends the
# logical line, turning the env prefix into plain unexported assignments —
# pytest then runs env-less. That is NOT merely a skip: with NX_SERVICE_*
# gone, service resolution falls through to the uid-scoped ServiceRegistry
# lease (service_endpoint.py), so on a box with a live supervisor the
# "hermetic" gate runs against the OPERATOR'S REAL INSTALL; only on a box
# with no live service do the tests skip. The scratch config-dir pin
# (3f61b851) is lost either way. That exact regression shipped in 376115c1
# and cost nightlies 2026-08-01/02 (skipped 21 -> 28, budget 25).
# Mechanically enforced by tests/test_shell_continuation_lint.py.
NX_SERVICE_HOST=127.0.0.1 NX_SERVICE_PORT="$SERVICE_PORT" NX_SERVICE_TOKEN="$SERVICE_TOKEN" \
  NX_GATE_SERVICE_EXPECTED=1 \
  NX_GATE_SERVICE_HOST=127.0.0.1 NX_GATE_SERVICE_PORT="$SERVICE_PORT" NX_GATE_SERVICE_TOKEN="$SERVICE_TOKEN" \
  NEXUS_CONFIG_DIR="$SCRATCH" \
  uv run pytest -m "integration and not lived_in and not cloud_mode" -q -rs --color=no "$@" 2>&1 | tee "$SCRATCH/pytest.out"
STATUS=${PIPESTATUS[0]}
set -e

# 7. Vacuity guard (nexus-edwlp Task 6): pinned from the post-fix empirical
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
