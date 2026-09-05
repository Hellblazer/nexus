#!/usr/bin/env bash
# release-sandbox.sh — high-fidelity local pre-merge verification.
#
# Combines:
#   - scripts/reinstall-tool.sh   (wheel-equivalent install via uv tool)
#   - tests/e2e/sandbox.sh        (isolated $HOME for Claude Code state)
#   - tests/e2e/lib.sh            (tmux primitives, used by tmux mode)
#
# Why this exists: merging to main to "test things out" is dangerous because
# the wheel-install path (uv tool install) resolves package data and version-
# gated migrations differently from the editable install that pytest uses.
# Run this BEFORE pushing/merging anything that touches: install/packaging,
# T2 migrations, MCP servers, hooks, plugin manifests, commands that read
# T2/T3 state, or anything tagged "ships to users".
#
# Modes:
#   smoke     — install + activate + self-provision (engine + PG + bge-768
#               embedder) + post-install canary checks. ~4–6 min (cold cache
#               pays the native binary + PG bundle + ~416 MB ONNX download;
#               warm cache is faster).
#   shakedown — full ensemble: smoke + index a bounded fixture repo/pdf/rdr +
#               search/query/T1/T2 + link graph readback + T1 turd sniff.
#               ~15–30 min (bounded fixture corpus, nexus-m7kcv — the
#               unbounded full-repo index this used to run was ~2.5h at
#               sandbox local-embed speed and was never actually a routine
#               gate step in practice).
#   shell     — install + activate + drop into a subshell with sandbox env.
#               Exit the subshell to tear down (HOME restored automatically).
#   tmux      — install + activate + launch Claude Code in tmux against
#               the sandbox. Useful for exercising MCP / hooks / skills.
#   reset     — tear down ~/nexus-sandbox without reinstalling.
#
# Source-of-truth doc: tests/e2e/release-sandbox.md
# Companion gist: https://gist.github.com/Hellblazer/511a05e1bf79dd6ea20be962d0ca04af

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SANDBOX="$HOME/nexus-sandbox"
TMUX_SESSION="${TMUX_SESSION:-nexus-sandbox}"

MODE="${1:-help}"
shift || true

_die() { echo "ERROR: $*" >&2; exit 1; }

# critic Significant 5 (T2 [21599]): this machine's /bin/bash is confirmed
# 3.2.57, and a stripped-PATH LaunchAgent/cron invocation context has
# already been proven (elsewhere in this repo's e2e suite) to reach that
# bash instead of a Homebrew bash4+ on an interactive PATH. Under 3.2,
# bash4-only constructs (${VAR,,} lowercasing, associative arrays) fail at
# PARSE time with "bad substitution" — a cryptic failure mode, not a
# graceful one. Guard early so this script fails loud with a clear message
# instead, before any such construct (present or future) is reached. This
# guard line itself is deliberately 3.2-parseable (arithmetic + indexed
# arrays predate bash4) so the check can run under the very shell it is
# rejecting.
(( BASH_VERSINFO[0] >= 4 )) || _die "bash >= 4 required (found $BASH_VERSION) — invoke with an explicit bash4+ (e.g. /opt/homebrew/bin/bash $0 ...), not a stripped-PATH default"

# ── Local-service helpers (RDR-157 P4.2 / nexus-596jm) ──────────────────────
# Shared by the `service` mode below AND by `_provision_local_service` (used
# by smoke/shakedown). Factored out rather than duplicated per-mode.
_svc_field() {  # $1 = json key
    nx daemon service status --json 2>/dev/null \
        | python3 -c "import sys,json;print(json.load(sys.stdin).get('$1',''))" \
        2>/dev/null || true
}

# gap-15 (T2 [22511]): non-vacuity floor for the shakedown's indexing steps.
# Prints "<doc_count> <chunk_count>" from `nx catalog stats --json` totals.
# The four indexing steps previously asserted exit code only -- a zero-chunk
# index (early return, wrong cwd, silently-empty walk) passed. Deltas across
# this helper give each step a real "did indexing actually put rows in the
# store" assertion, not just "did the process exit 0". Falls back to "0 0"
# on any parse failure so a caller's arithmetic never sees a bare newline.
_catalog_counts() {
    nx catalog stats --json 2>/dev/null \
        | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('documents',0), d.get('chunks',0))" \
        2>/dev/null || echo "0 0"
}

# gap-15: assert the doc/chunk deltas across an indexing step meet a
# per-fixture floor. Only called when the step's own exit code was clean
# (an exit-code failure is already recorded by the caller) — this is the
# SECOND, independent assertion: a step can exit 0 while indexing nothing
# (early return, wrong cwd, silently-empty walk), and that is exactly the
# vacuous-pass class this closes.
_index_floor_check() {
    local label="$1" doc_floor="$2" chunk_floor="$3" doc_before="$4" chunk_before="$5"
    local doc_after chunk_after doc_delta chunk_delta
    read -r doc_after chunk_after < <(_catalog_counts)
    doc_delta=$((doc_after - doc_before))
    chunk_delta=$((chunk_after - chunk_before))
    echo "  non-vacuity: docs +$doc_delta (floor $doc_floor), chunks +$chunk_delta (floor $chunk_floor)"
    if (( doc_delta < doc_floor || chunk_delta < chunk_floor )); then
        echo "  [FAIL] non-vacuity floor not met for $label" >&2
        SHAKEDOWN_FAILED+=("$label (non-vacuity floor: docs +$doc_delta/$doc_floor chunks +$chunk_delta/$chunk_floor)")
    fi
}

# nexus-s71lr: an `nx index repo`/`nx index rdr` step's whole stdout+stderr is
# redirected into a log file and only tailed AFTER the command returns (see
# steps 2/11 and 4/11 below) -- a stall was invisible in THIS transcript even
# after the client-side heartbeat fix (index.py's _PhaseHeartbeat / tightened
# ETA ticker), because those lines land in the log file, never on this
# script's own stdout, until the command finishes. Backgrounds `tail -f` on
# the log file for the DURATION of the foreground command so the same
# heartbeat/eta lines the CLI now prints by default become visible live in
# THIS transcript too, not only in the post-hoc `tail -5`. The log file is
# touched first so `tail -f` has something to open immediately even if the
# indexed command is slow to produce its first byte.
#
# UNTESTED end-to-end at authoring time (no Docker / live shakedown run
# available in the authoring sandbox) -- rehearse this step live before
# relying on it for a real release shakedown.
# The live tail's PID is also tracked globally so an interrupted run (Ctrl-C
# on a stall, a CI timeout) kills it from the EXIT/INT/TERM traps instead
# of orphaning a `tail -f` that holds the log open forever (s71lr review).
_LIVE_TAIL_PID=""

# Sets _LIVE_TAIL_PID; never call this through `$(...)` (the backgrounded
# tail inherits the substitution's stdout pipe, so the substitution never
# sees EOF and the caller hangs; pass-2 critique, reproduced standalone).
_start_live_log_tail() {
    local log_file="$1"
    : > "$log_file"
    tail -n +1 -f "$log_file" &
    _LIVE_TAIL_PID=$!
}

_stop_live_log_tail() {
    local tail_pid="$1"
    kill "$tail_pid" 2>/dev/null || true
    wait "$tail_pid" 2>/dev/null || true
    [[ "$_LIVE_TAIL_PID" == "$tail_pid" ]] && _LIVE_TAIL_PID=""
}

_kill_live_tail() {
    [[ -n "$_LIVE_TAIL_PID" ]] || return 0
    kill "$_LIVE_TAIL_PID" 2>/dev/null || true
    _LIVE_TAIL_PID=""
}
trap '_kill_live_tail; exit 130' INT
trap '_kill_live_tail; exit 143' TERM

# nexus-98zsp: indexing wall-clock floor. engine-service-v0.1.99 shipped an
# 8x embed slowdown through every gate because none of them timed an index
# run; this is the only place a real corpus is indexed pre-release.
# shellcheck source=tests/e2e/migration-rehearsal/lib/index_throughput.sh disable=SC1091
source "$REPO_ROOT/tests/e2e/migration-rehearsal/lib/index_throughput.sh"
THROUGHPUT_BASELINES="$REPO_ROOT/tests/e2e/migration-rehearsal/lib/index-throughput-baselines.tsv"
_throughput_step() {
    local step="$1" label="$2" log="$3" elapsed="$4"
    throughput_engine_shape "$HOME/.config/nexus/logs"
    local rc=0
    throughput_gate "$label" "$log" "$elapsed" "$THROUGHPUT_BASELINES" || rc=$?
    case "$rc" in
        0) ;;
        1) SHAKEDOWN_FAILED+=("$step (throughput above ${THROUGHPUT_CEILING_FACTOR}x baseline)") ;;
        2) SHAKEDOWN_SOFT+=("$step: throughput baseline RECORDED, no ceiling applied — commit $THROUGHPUT_BASELINES") ;;
        *) SHAKEDOWN_SOFT+=("$step: too few chunks to measure throughput") ;;
    esac
}

# nexus-whqun: the T1 leak sniff used to count two directories that are
# BOTH confirmed dead by the current T1 design: `$HOME/.config/nexus/
# sessions/` (the legacy SESSIONS_DIR resolver -- T1Database._reconnect's
# own docstring in src/nexus/db/t1.py says it and the multi-writer record
# files it read "are gone") and `$TMPDIR/nx_t1_*` (the retired chroma-T1
# tmpdir convention -- `rg "nx_t1_" src/` returns zero hits anywhere in
# this tree). Nothing in this codebase writes to either location any more,
# so both deltas were structurally always 0 -- a WARN that could never
# fire, twice over, not once as originally filed.
#
# What T1 ACTUALLY leaves on disk today (src/nexus/db/t1.py): per-session
# lease files (`t1_session_lease.<session_id>`, published by a live MCP
# session and removed on clean teardown) and one persisted CLI-dedicated-
# session cache file (`t1_cli_dedicated_session`). Both are written via an
# atomic temp-file + os.replace() pattern -- `<name>.<pid>.<uuid>.tmp`,
# renamed into place instantly (`publish_t1_session_lease` /
# `_cli_dedicated_session_id`). A `.tmp` file surviving past the write
# that created it means a process died mid-publish (crash, SIGKILL)
# BEFORE the rename -- genuine litter, the same failure class the
# original sniff was trying (and structurally failing) to catch.
_t1_stray_tmp_count() {
    local config_dir="$1"
    { ls -1 "$config_dir"/t1_session_lease.*.tmp \
          "$config_dir"/t1_cli_dedicated_session.*.tmp 2>/dev/null || true; } \
        | wc -l | tr -d ' '
}

# nexus-9nchs part B: `nx doctor`'s own process exit code encodes ONLY
# `fatal and not ok` (nexus.health.HealthResult docstring: `ok=False,
# warn=True` is "soft warning ... never fatal, never marks the run
# failed" -- RDR-129 B4, a deliberate PRODUCT design choice this script
# does not change). The corpus-integrity instruments this shakedown step
# needs to watch are ALL warn=True by that same design, so bare `nx
# doctor`'s rc is structurally blind to a regression in any of them.
# This is release-sandbox's OWN, additional gate layered over the SAME
# `nx doctor --json` payload (nexus.health.format_health_for_json),
# independent of doctor's own exit-code semantics.
#
# The three named checks are the currently-live corpus-integrity
# instruments in nexus.health (verified against the tree at fix time --
# the bead's originally-cited "dangling manifests" check was retired
# outright at RDR-191 Phase 6/nexus-o8dil.33; "manifest pre-backfill
# rows" is its still-live sibling, explicitly NOT retired per RDR-191
# Decision item 4's carve-out, and the closest surviving analogue).
_DOCTOR_CORPUS_INTEGRITY_LABELS=(
    "Chunk chash conformance"
    "stale index-run fences"
    "manifest pre-backfill rows (collection IS NULL)"
)

# Reads `nx doctor --json` output on stdin. Prints one "LABEL: detail"
# line per named check whose status came back anything other than "ok";
# prints nothing when all named checks are clean. Unparseable input
# (doctor crashed/changed shape before emitting valid JSON) is flagged
# explicitly rather than silently read as "all clean" -- the same
# fail-loud-never-fail-open discipline health.py itself uses.
_doctor_corpus_integrity_regressions() {
    python3 -c '
import json, sys
named = set(sys.argv[1:])
try:
    data = json.load(sys.stdin)
except ValueError as exc:
    print(f"<unparseable nx doctor --json output: {exc}>")
    raise SystemExit(0)
for c in data.get("checks", []):
    if c.get("name") in named and c.get("status") != "ok":
        name = c.get("name")
        detail = c.get("detail", "")
        print(f"{name}: {detail}")
' "${_DOCTOR_CORPUS_INTEGRITY_LABELS[@]}"
}

# nexus-jy4hd: extractor-identity verdicts for the MinerU shakedown step.
# (1) `nx doctor --check-mineru` prints check lines but its rc is not a
# reliable failure signal for this one check — parse the output for the
# MinerU line and require it to be a pass; no MinerU line at all is a FAIL
# (a probe that produced nothing is never a pass). (2) after step 3b
# indexes with an EXPLICIT --extractor mineru, the indexed chunk's
# Extractor field must literally be mineru — belt-and-braces against any
# future in-extractor fallback quietly substituting a different engine.
_mineru_doctor_verdict() {
    # stdin: `nx doctor --check-mineru` output → "OK" or "FAIL|<cause>"
    python3 -c '
import sys
out = sys.stdin.read()
lines = [l for l in out.splitlines()
         if "MinerU import" in l or "do_parse" in l]
if not lines:
    print("FAIL|doctor output carries no MinerU line at all — the probe ran nothing")
    raise SystemExit(0)
bad = [l.strip() for l in lines if "✗" in l]
if bad:
    print("FAIL|" + "; ".join(bad)[:300])
else:
    print("OK")
'
}

# Isolate the chunk ids ADDED by a step: set-difference of two id listings
# (one per line, any order). Ordering-proof by construction — review
# Critical on the first cut: `nx store list | head -1` rode the engine's
# ORDER BY chash ASC, so with steps 3a and 3b sharing a collection the
# "first" chunk could be the OTHER extractor's document (false-FAIL a
# healthy MinerU run, or false-PASS without inspecting 3b's chunk at all).
_new_chunk_ids() {
    # $1 = file of before-ids, $2 = file of after-ids -> new ids, one/line
    comm -13 <(sort -u "$1") <(sort -u "$2")
}

_extractor_identity_verdict() {
    # stdin: `nx store get <chunk-id>` output → "OK" or "FAIL|<cause>"
    python3 -c '
import sys
out = sys.stdin.read()
for line in out.splitlines():
    if line.startswith("Extractor:"):
        method = line.split(":", 1)[1].strip()
        if method == "mineru":
            print("OK")
        else:
            print(f"FAIL|indexed chunk carries extraction_method={method!r}, not mineru — the step exercised a DIFFERENT extractor than the one it claims to gate")
        raise SystemExit(0)
print("FAIL|no Extractor field on the indexed chunk — extraction_method missing, identity unproven")
'
}

# ── --self-test: pure-function checks against synthetic fixtures ───────────
# Exercises the two detectors above directly -- no wheel build, no sandbox
# HOME, no engine, no PG, no network. Mirrors tests/e2e/local-index-
# memory-gate.sh's --self-test precedent: a full sandbox run is
# impractical to drive per-edit, so the changed assertions get a pure-
# function seam a plain shell can exercise anywhere, any time.
_SELF_TEST_FAILED=0
_st_ok()  { printf '  [PASS] %s\n' "$*"; }
_st_bad() { printf '  [FAIL] %s\n' "$*" >&2; _SELF_TEST_FAILED=1; }

_self_test() {
    echo "== self-test: _t1_stray_tmp_count (nexus-whqun) =="
    local tdir n
    tdir="$(mktemp -d)"

    n=$(_t1_stray_tmp_count "$tdir")
    [[ "$n" == "0" ]] && _st_ok "clean dir -> 0" \
        || _st_bad "clean dir -> expected 0, got $n"

    # RED: plant the exact litter class the detector claims to catch.
    : > "$tdir/t1_session_lease.abc123.999.deadbeef.tmp"
    : > "$tdir/t1_cli_dedicated_session.999.deadbeef.tmp"
    : > "$tdir/t1_session_lease.abc123"   # real, non-.tmp lease -- must NOT count
    n=$(_t1_stray_tmp_count "$tdir")
    [[ "$n" == "2" ]] && _st_ok "2 stray .tmp files -> detected 2 (RED)" \
        || _st_bad "2 stray .tmp files -> expected 2, got $n (detector did not fire)"

    # GREEN: repair (remove the litter) -- must clear back to 0.
    rm -f "$tdir"/*.tmp
    n=$(_t1_stray_tmp_count "$tdir")
    [[ "$n" == "0" ]] && _st_ok "litter removed -> 0 (GREEN)" \
        || _st_bad "litter removed -> expected 0, got $n"

    rm -rf "$tdir"

    echo
    echo "== self-test: _doctor_corpus_integrity_regressions (nexus-9nchs) =="
    local clean_json warn_json out
    clean_json='{"checks":[
        {"name":"Chunk chash conformance","ok":true,"status":"ok","detail":""},
        {"name":"stale index-run fences","ok":true,"status":"ok","detail":""},
        {"name":"manifest pre-backfill rows (collection IS NULL)","ok":true,"status":"ok","detail":""}
    ]}'
    out=$(printf '%s' "$clean_json" | _doctor_corpus_integrity_regressions)
    [[ -z "$out" ]] && _st_ok "all three ok -> no regressions reported" \
        || _st_bad "all three ok -> expected empty, got: $out"

    # RED: plant the exact regression the step claims to catch -- one
    # named instrument flips to warn=true/status=warn (the real shape
    # health.py emits, e.g. CHASH_CONFORMANCE_LABEL's ok=False, warn=True
    # branch on poisoned chash rows) while doctor's own rc would stay 0.
    warn_json='{"checks":[
        {"name":"Chunk chash conformance","ok":false,"status":"warn","detail":"3 chunk row(s) have a non-conformant chash length"},
        {"name":"stale index-run fences","ok":true,"status":"ok","detail":""},
        {"name":"manifest pre-backfill rows (collection IS NULL)","ok":true,"status":"ok","detail":""}
    ]}'
    out=$(printf '%s' "$warn_json" | _doctor_corpus_integrity_regressions)
    if [[ "$out" == *"Chunk chash conformance"*"non-conformant chash length"* ]]; then
        _st_ok "chash-conformance regression (warn=true, doctor rc still 0) -> flagged (RED)"
    else
        _st_bad "chash-conformance regression -> expected it named in output, got: $out"
    fi

    # GREEN: repair -- back to the clean payload.
    out=$(printf '%s' "$clean_json" | _doctor_corpus_integrity_regressions)
    [[ -z "$out" ]] && _st_ok "repaired -> no regressions reported (GREEN)" \
        || _st_bad "repaired -> expected empty, got: $out"

    # Unparseable --json output (doctor crashed before emitting JSON) must
    # be flagged too, not silently read as "all clean".
    out=$(printf 'not json' | _doctor_corpus_integrity_regressions)
    [[ -n "$out" ]] && _st_ok "unparseable --json output -> flagged, not silently clean" \
        || _st_bad "unparseable --json output -> expected a flagged line, got empty"

    echo
    echo "== self-test: _mineru_doctor_verdict (nexus-jy4hd) =="
    out=$(printf '✓ MinerU import\n✓ MinerU do_parse reachable\n' | _mineru_doctor_verdict)
    [[ "$out" == "OK" ]] && _st_ok "passing doctor output -> OK" \
        || _st_bad "passing doctor output -> expected OK, got: $out"
    out=$(printf '✗ MinerU import ModuleNotFoundError: mineru\n' | _mineru_doctor_verdict)
    [[ "$out" == FAIL\|* ]] && _st_ok "RED: broken MinerU import -> FAIL naming the cause ($out)" \
        || _st_bad "broken import -> expected FAIL, got: $out"
    out=$(printf 'unrelated doctor chatter\n' | _mineru_doctor_verdict)
    [[ "$out" == FAIL\|*"no MinerU line"* ]] && _st_ok "RED: probe produced no MinerU line -> FAIL, never a silent pass" \
        || _st_bad "empty probe -> expected FAIL, got: $out"

    echo "== self-test: _new_chunk_ids (nexus-jy4hd review Critical) =="
    tdir_ids="$(mktemp -d)"
    # BEFORE holds 3a's docling chunks; AFTER adds 3b's mineru chunks. The
    # docling ids sort LEXICOGRAPHICALLY FIRST (the exact trap: a
    # chash-ordered head -1 would pick 0a..., a step-3a chunk).
    printf '0a%.0s' 1 > /dev/null  # noop guard for shellcheck
    printf '%s\n' "$(printf '0a%.0s' $(seq 32))" "$(printf '0b%.0s' $(seq 32))" > "$tdir_ids/before"
    printf '%s\n' "$(printf '0a%.0s' $(seq 32))" "$(printf '0b%.0s' $(seq 32))" "$(printf 'ff%.0s' $(seq 32))" > "$tdir_ids/after"
    new_id="$(_new_chunk_ids "$tdir_ids/before" "$tdir_ids/after")"
    [[ "$new_id" == "$(printf 'ff%.0s' $(seq 32))" ]] \
        && _st_ok "set diff isolates ONLY the step-added chunk (ordering-proof: the lexicographically-first id belongs to the other step and is excluded)" \
        || _st_bad "set diff -> expected the ff... id only, got: $new_id"
    new_id="$(_new_chunk_ids "$tdir_ids/after" "$tdir_ids/after")"
    [[ -z "$new_id" ]] && _st_ok "RED: no new chunks -> empty diff -> the step FAILs with 'added no new chunk ids' (never picks an old chunk)" \
        || _st_bad "identical sets -> expected empty, got: $new_id"
    rm -rf "$tdir_ids"

    echo "== self-test: _extractor_identity_verdict (nexus-jy4hd) =="
    out=$(printf 'ID: abc\nExtractor:  mineru\n\ncontent' | _extractor_identity_verdict)
    [[ "$out" == "OK" ]] && _st_ok "mineru-extracted chunk -> OK" \
        || _st_bad "mineru chunk -> expected OK, got: $out"
    out=$(printf 'ID: abc\nExtractor:  docling\n' | _extractor_identity_verdict)
    [[ "$out" == FAIL\|*"docling"* ]] && _st_ok "RED: silent substitution (docling) -> FAIL naming the extractor" \
        || _st_bad "docling chunk -> expected FAIL naming docling, got: $out"
    out=$(printf 'ID: abc\nTitle: x\n' | _extractor_identity_verdict)
    [[ "$out" == FAIL\|*"no Extractor field"* ]] && _st_ok "RED: missing extraction_method -> FAIL, identity unproven" \
        || _st_bad "missing field -> expected FAIL, got: $out"

    echo "== self-test: bash -n on this script itself =="
    if bash -n "${BASH_SOURCE[0]}"; then
        _st_ok "bash -n clean"
    else
        _st_bad "bash -n reported a syntax error"
    fi

    echo
    if (( _SELF_TEST_FAILED )); then
        echo "SELF-TEST FAILED" >&2
        return 1
    fi
    echo "SELF-TEST PASSED"
    return 0
}

_svc_teardown() {
    echo "  ── teardown (nx daemon service stop --with-pg) ──"
    nx daemon service stop --with-pg 2>&1 | tail -3 | sed 's/^/    /' || true

    # CRITICAL (substantive-critic, T2 [21599], script-side mitigation —
    # product-side fix tracked separately as nexus-f7t9e): `--with-pg`
    # silently no-ops when pg_credentials does not exist yet
    # (daemon.py service_stop_cmd: `if not creds_path.exists(): return`),
    # but pg_provision.provision() starts postgres (_start_cluster) BEFORE
    # writing credentials (_write_credentials) — three subprocess calls
    # (_create_db, _create_vector_extension, _create_roles) sit in that
    # gap. A crash mid-provision (elevated likelihood on this machine per
    # the documented pipe-degradation history) leaves a live postmaster
    # the command above cannot discover (no lease, no creds) — a leak the
    # NEXT run's --keep-existing warm-cache reuse then collides with
    # (orphaned process holding the old port/PGDATA). Fall back to direct
    # discovery, scoped strictly to the SANDBOX HOME's own PGDATA so this
    # never touches a real install's Postgres.
    local pgdata="$HOME/.config/nexus/postgres"
    if [[ -f "$pgdata/postmaster.pid" ]]; then
        echo "  [fallback] postmaster.pid still present under sandbox PGDATA after 'stop --with-pg' (credentials-write race) — stopping it directly"
        local pg_ctl_bin="$HOME/.config/nexus/pg-bundle/bundle/bin/pg_ctl"
        if [[ -x "$pg_ctl_bin" ]]; then
            echo "    using bundled pg_ctl: $pg_ctl_bin"
            "$pg_ctl_bin" -D "$pgdata" stop -m fast 2>&1 | sed 's/^/    /' || true
        elif command -v pg_ctl >/dev/null 2>&1; then
            echo "    using PATH pg_ctl: $(command -v pg_ctl)"
            pg_ctl -D "$pgdata" stop -m fast 2>&1 | sed 's/^/    /' || true
        else
            local pm_pid
            pm_pid=$(head -1 "$pgdata/postmaster.pid" 2>/dev/null || true)
            if [[ -n "$pm_pid" ]]; then
                echo "    no pg_ctl available anywhere — killing postmaster pid $pm_pid directly"
                # SIGINT = PG fast shutdown, matching the pg_ctl -m fast
                # branches above (SIGTERM is smart shutdown: waits on clients).
                kill -INT "$pm_pid" 2>/dev/null || true
            fi
        fi
    fi

    # nexus-m7kcv (second half): the formula-routing PDF indexer (shakedown
    # step 3b) lazily spawns a `mineru-api --host 127.0.0.1` daemon under
    # the sandbox HOME that outlives a plain teardown — observed leaked
    # after a TERM-killed test run. Match on the live process's own command
    # path (not just the process name) so this only ever targets a process
    # actually rooted under THIS sandbox HOME, never a real install's
    # mineru-api.
    local mineru_pid mineru_cmd
    for mineru_pid in $(pgrep -f "mineru-api" 2>/dev/null || true); do
        mineru_cmd=$(ps -p "$mineru_pid" -o command= 2>/dev/null || true)
        if [[ "$mineru_cmd" == *"$HOME"* ]]; then
            echo "  [fallback] terminating leftover sandbox mineru-api (pid $mineru_pid)"
            kill "$mineru_pid" 2>/dev/null || true
        fi
    done

    # nexus-bv8yl: `nx daemon service stop --with-pg` stops the storage
    # service + PG but NOT the aspect-worker daemon the indexing steps
    # spawn. The survivor holds files under the sandbox HOME, so the NEXT
    # run's recreate dies with `rm: Directory not empty` (bit twice at the
    # 7.4.0 cut, back-to-back). Same sandbox-HOME-scoped matching rule as
    # the mineru reaper above: command path must be rooted under THIS
    # sandbox HOME, so a real install's worker is never touched.
    local worker_pid worker_cmd
    for worker_pid in $(pgrep -f "aspect-worker" 2>/dev/null || true); do
        worker_cmd=$(ps -p "$worker_pid" -o command= 2>/dev/null || true)
        if [[ "$worker_cmd" == *"$HOME"* ]]; then
            echo "  [fallback] terminating leftover sandbox aspect-worker (pid $worker_pid)"
            kill "$worker_pid" 2>/dev/null || true
        fi
    done
}

# nexus-596jm: smoke and shakedown exercise substrate steps (plan reseed,
# index repo/pdf/rdr, cross-corpus search, T2 roundtrip) and doctor checks
# (--check-schema, --check-plan-library, bare doctor's vector-service row)
# that need a live storage service — but neither mode ever provisioned one.
# Those steps became fail-capable this cycle (6xkdu removed `|| true` from
# the indexing steps; fe6452a4 de-theatred doctor), so running them against
# an unprovisioned sandbox HOME is now a structural red, not a genuine
# defect signal: ServiceEndpointUnresolvableError with nothing behind it.
#
# Mirrors tests/e2e/fresh-install-mvv.sh's provisioning mechanics: plain
# `nx init` in LOCAL mode self-provisions the native binary + PG bundle +
# bge-768 embedder with zero manual positioning, so this stays self-
# contained — no dependence on the live install's config/lease (the live
# box is cloud-mode and must not be touched). NEXUS_SERVICE_BIN /
# NEXUS_PG_BUNDLE / NEXUS_SERVICE_TAG, if already exported by the caller,
# flow straight through (this script does not `env -i` scrub the way the
# MVV's virgin-box layer does), so a warm cache avoids re-downloading the
# signed binary + PG bundle + ~416 MB bge-768 ONNX on every run; combine
# with --keep-existing to preserve that cache across invocations of this
# script. Shares `_svc_field` / `_svc_teardown` above with the `service`
# mode below rather than each mode duplicating its own copy.
#
# Provisioning failure is always loud (_die), never a skip-pass. Teardown is
# installed as the EXIT trap BEFORE `nx init` runs (mirrors the MVV's
# `trap cleanup EXIT` set before its own install step) so a failure mid-
# provisioning still tears down whatever got partially started; `nx daemon
# service stop --with-pg` is a no-op-safe call even when nothing came up.
_provision_local_service() {
    echo "  ── self-provisioning local service (nexus-596jm) ──"
    trap '_kill_live_tail; _svc_teardown; lock_release "$LOCKDIR" 2>/dev/null || true' EXIT
    if ! nx init -y --no-autostart 2>&1 | sed 's/^/    /'; then
        _die "nx init did not reach serving inside the sandbox (self-provisioning failed — see remedy above)"
    fi
    local health
    health=$(_svc_field health)
    [[ "$health" == "ok" ]] || _die "service not serving after provisioning: /health=$health (expected ok)"
    echo "    [ok] local service serving, /health=ok"
}

_print_help() {
    # printf rather than here-doc: bash here-docs hang in some non-
    # interactive shell contexts (Claude Code harness, certain CI
    # runners) where parent stdin is wired to a pipe the here-doc
    # machinery never closes. printf has no such dependency.
    printf '%s\n' \
        "Usage: $0 <mode> [options]" \
        "" \
        "Modes:" \
        "  smoke      Reinstall + activate + self-provision local service (engine + PG +" \
        "             bge-768 embedder) + run nx upgrade --dry-run + nx doctor checks." \
        "             Verifies the wheel install + migrations + health surface." \
        "             ~4–6 min (cold cache pays the native binary + PG bundle + ~416 MB" \
        "             ONNX download; warm cache with --keep-existing is faster)." \
        "  shakedown  Full ensemble: smoke + indexing a bounded fixture repo/pdf/rdr +" \
        "             cross-corpus search + T2 memory roundtrip + T1 scratch use +" \
        "             catalog link readback + T1 turd sniff. Exercises every pipeline" \
        "             against a fresh install." \
        "             ~15–30 min on warm cache, +10–15 min if MinerU models are not yet" \
        "             downloaded. Repo indexing (step 2/11) uses a hard-coded ~36-file" \
        "             fixture subset, not the full nexus tree (nexus-m7kcv — the full" \
        "             tree at sandbox local-embed speed is ~2.5h)." \
        "             Probes tc-sql.pdf (Docling path) AND bft-to-smr.pdf (MinerU path)." \
        "  shell      Reinstall + activate + drop into a subshell with HOME=\$SANDBOX." \
        "             Use this for manual nx index, nx search, etc. Exit normally to" \
        "             tear down." \
        "  tmux       Reinstall + activate + launch Claude Code interactively in tmux." \
        "             Useful for end-to-end exercises against MCP / plugin / hooks." \
        "             Requires tests/e2e/.claude-auth/.credentials.json (run" \
        "             tests/e2e/auth-login.sh first)." \
        "  service    RDR-157 P4.2 fresh-machine LOCAL-mode E2E: position the service" \
        "             artifact (native binary via NEXUS_SERVICE_BIN, else the repo JAR)," \
        "             then prove ONE command (nx init --service) goes fresh-install ->" \
        "             serving with zero manual steps, idempotent on re-run, then stop." \
        "             Requires a local PG with pgvector (host or NEXUS_PG_BUNDLE) and the" \
        "             bge-768 ONNX (auto-fetched by init; ~416 MB on a cold cache)." \
        "  reset      Remove ~/nexus-sandbox. Does NOT reinstall." \
        "  self-test  Pure-function checks on the T1-litter / doctor-corpus-" \
        "             integrity detectors (nexus-whqun / nexus-9nchs) against" \
        "             synthetic fixtures. No wheel build, no sandbox HOME, no" \
        "             engine, no PG, no network. Safe to run anywhere, any time." \
        "  help       Print this message." \
        "" \
        "Common options (post-mode):" \
        "  --skip-install   Skip the reinstall step. Useful when the tool venv is" \
        "                   already at the version you want to exercise." \
        "  --keep-existing  Reuse \$HOME/nexus-sandbox if it exists (default: blow away" \
        "                   and recreate so state is reproducible)." \
        "" \
        "Examples:" \
        "  # Pre-merge smoke after a refactor" \
        "  $0 smoke" \
        "" \
        "  # Hand-test indexing into the sandbox" \
        "  $0 shell" \
        "  (sandbox) nx index repo /path/to/test-repo" \
        "  (sandbox) nx taxonomy status" \
        "  (sandbox) exit" \
        "" \
        "  # Spin up Claude Code against the sandbox" \
        "  $0 tmux" \
        "" \
        "  # Skip reinstall (e.g. iterating on shell flow)" \
        "  $0 shell --skip-install" \
        ""
}

# ── Option parsing ───────────────────────────────────────────────────────────

SKIP_INSTALL=0
KEEP_EXISTING=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-install) SKIP_INSTALL=1; shift ;;
        --keep-existing) KEEP_EXISTING=1; shift ;;
        --help|-h) _print_help; exit 0 ;;
        *) _die "unknown option: $1 (use $0 help)" ;;
    esac
done

# ── Mode dispatch ────────────────────────────────────────────────────────────

if [[ "$MODE" == "help" || "$MODE" == "--help" || "$MODE" == "-h" ]]; then
    _print_help
    exit 0
fi

# nexus-whqun / nexus-9nchs: pure-function self-test, dispatched before the
# machine-global lock below — it touches neither $SANDBOX nor the lock dir,
# requires no `nx` install, no engine, no PG. Safe to run anywhere, any time.
if [[ "$MODE" == "self-test" ]]; then
    _self_test
    exit $?
fi

# RDR-184 P0.2 (nexus-ccs9v.2): serialize on the machine-global fixed
# resources this harness mutates — $HOME/nexus-sandbox and the fixed tmux
# session name. Lock dir lives under a stable machine-global temp root, NOT
# under this checkout — the sandbox HOME and tmux session are per-machine
# singleton resources, so two different checkouts on the same host must
# still serialize. Acquired here, after option parsing/help dispatch (usage
# errors don't need the lock) but strictly before the first mutation (the
# "reset"/Step-1 sandbox rm -rf just below). Lock dir is a HARD-CODED /tmp
# path, deliberately NOT ${TMPDIR:-/tmp} (code-review SIGNIFICANT fix): on
# darwin, an interactive shell's TMPDIR is a per-user /var/folders/... path
# while a LaunchAgent/CI/stripped-env invocation sees plain /tmp — two
# different invocation contexts would silently compute DIFFERENT lockdirs
# and never contend, defeating the whole point of a machine-global guard
# (this repo runs LaunchAgents that could race an interactive run). /tmp is
# always the same path across every context on the same host.
# shellcheck source=./lib/lock.sh disable=SC1091
source "$SCRIPT_DIR/lib/lock.sh"
LOCKDIR="/tmp/nexus-e2e-locks/release-sandbox.lock"
mkdir -p "$(dirname "$LOCKDIR")"
lock_acquire "$LOCKDIR" || exit 1
trap '_kill_live_tail; lock_release "$LOCKDIR" 2>/dev/null || true' EXIT
echo "[rdr-184] lock acquired: $LOCKDIR (pid $$)" >&2
# Test seam (RDR-184 P0.2, nexus-ccs9v.2): tests/e2e/lib/harness_lock_test.sh
# sets this to prove a concurrent invocation gets PAST the lock without ever
# running this harness's real body (reinstall / index / sandbox rm -rf).
# No-op — unset in every normal invocation.
[[ -n "${NX_E2E_LOCK_SELFTEST:-}" ]] && exit 0

if [[ "$MODE" == "reset" ]]; then
    if [[ -d "$SANDBOX" ]]; then
        echo "Removing $SANDBOX ..."
        rm -rf "$SANDBOX"
        echo "Sandbox removed."
    else
        echo "No sandbox at $SANDBOX — nothing to reset."
    fi
    exit 0
fi

if [[ "$MODE" != "smoke" && "$MODE" != "shakedown" && "$MODE" != "shell" \
      && "$MODE" != "tmux" && "$MODE" != "service" ]]; then
    _die "unknown mode: $MODE (use $0 help)"
fi

# ── Step 1 — create sandbox HOME ─────────────────────────────────────────────

if [[ -d "$SANDBOX" && $KEEP_EXISTING -eq 0 ]]; then
    echo "[1/3] Recreating sandbox at $SANDBOX (use --keep-existing to reuse)"
    rm -rf "$SANDBOX"
elif [[ -d "$SANDBOX" ]]; then
    echo "[1/3] Reusing existing sandbox at $SANDBOX"
fi

if [[ ! -d "$SANDBOX" ]]; then
    echo "[1/3] Creating fresh sandbox at $SANDBOX ..."
    NX_E2E_LOCK_HELD_BY_PARENT=1 "$REPO_ROOT/tests/e2e/sandbox.sh" >/dev/null
fi

# Activate the sandbox HOME/PATH BEFORE the reinstall below (not after, as
# this script did until nexus-h9f1w-follow-up): the generation root resolves
# off $HOME ($HOME/.local/share/nexus/tools via nx_tools_dir, $HOME/.local/bin
# via nx_bin_dir), so activating first makes the reinstall land entirely inside
# $SANDBOX/.local/{share/nexus/tools,bin} — genuinely isolated from the live
# global install. layout.sh recomputes both on EVERY call rather than caching,
# specifically so this harness's $HOME redirection cannot be outrun.
#
# The ordering used to matter for a second, louder reason: running against the
# REAL $HOME swapped the live venv out from under every concurrent Claude Code
# session's nx-mcp servers and daemons, tripping a live-holder guard whose only
# remedies were --force (unsafe) or --cycle-daemons (which could not touch
# MCP-server subprocesses anyway). That failure mode is GONE — installs are
# side-by-side generations now and nothing is swapped underneath a holder
# (nexus-utpuw.8), and those flags no longer exist. Isolation is still the
# reason to activate first, and it is reason enough: get it wrong and the
# sandbox writes its generations into the developer's live install.
# shellcheck source=/dev/null
. "$SANDBOX/activate"

# ── Step 2 — reinstall into the now-isolated sandbox tool dir (unless skipped) ──

if (( SKIP_INSTALL == 0 )); then
    echo "[2/3] Reinstalling nx CLI from $REPO_ROOT (isolated: HOME=$SANDBOX) ..."
    (cd "$REPO_ROOT" && uv sync >/dev/null 2>&1)
    # THE SOURCE IS NAMED, NEVER DEFAULTED (nexus-hibpr). reinstall-tool.sh's
    # default source is "." -- the CALLER's cwd -- and this script never cds
    # into $REPO_ROOT before invoking it. Measured 2026-08-27: pointed at a
    # v7.20.0 worktree from another directory, this line installed the primary
    # checkout at 7.18.0 and the run still ended "SMOKE PASSED: all steps
    # green". A gate that can green a tree it never installed is not a gate.
    "$REPO_ROOT/scripts/reinstall-tool.sh" "$REPO_ROOT" >/dev/null
    # gap-8 (T2 [22511]): this used to be `nx --version 2>/dev/null ||
    # echo 'nx --version failed'` -- a broken reinstall printed a friendly
    # string and the script CONTINUED into smoke/shakedown against a dead
    # nx. reinstall-tool.sh exiting 0 only proves the wheel build/install
    # step ran; it does not prove the resulting `nx` is actually callable.
    NX_VER_OUT="$(nx --version 2>&1)" \
        || _die "reinstall-tool.sh exited 0 but 'nx --version' failed afterward (broken install): $NX_VER_OUT"
    echo "      $NX_VER_OUT"
    # ...and the version it reports must be $REPO_ROOT's, or every verdict
    # below is about a tree nobody asked this run to test. Pipe-free
    # first-line extraction (the pipefail/SIGPIPE class).
    _expected_all="$(sed -n "s/^version *= *[\"']\([^\"']*\)[\"']/\1/p" "$REPO_ROOT/pyproject.toml")"
    _expected_ver="${_expected_all%%$'\n'*}"
    [[ -n "$_expected_ver" ]] || _die "could not read version from $REPO_ROOT/pyproject.toml"
    [[ "$NX_VER_OUT" == *"$_expected_ver"* ]] \
        || _die "installed '$NX_VER_OUT' is not $REPO_ROOT's $_expected_ver — the sandbox installed a different tree (nexus-hibpr)"
else
    echo "[2/3] Skipping reinstall (--skip-install). nx version: $(nx --version 2>/dev/null || echo 'unknown')"
fi

# ── Step 3 — execute mode ────────────────────────────────────────────────────

case "$MODE" in
    smoke)
        # Force local mode so the smoke does not contact ChromaDB Cloud
        # even if the parent shell has CHROMA_* set. The sandbox HOME is
        # empty by design — there is no cloud data to populate from. Same
        # pattern shakedown uses below; tests/e2e/run.sh has the long
        # explanation.
        export NX_LOCAL=1
        unset CHROMA_API_KEY CHROMA_TENANT CHROMA_DATABASE VOYAGE_API_KEY
        echo "[3/3] Smoke checks (running from /tmp, NX_LOCAL=1):"
        cd /tmp
        _provision_local_service
        echo "  nx --version: $(nx --version)"
        echo
        # gap-8 (T2 [22511]): `nx upgrade` / `nx plan reseed` used to run
        # under `|| true` -- the release sandbox ran the user-facing
        # convergence command and ignored whether it worked. Collect-and-
        # continue (nexus-6xkdu precedent, same pattern as the doctor loop
        # below) so a failure here turns the final verdict red without
        # forfeiting the diagnostic value of the remaining checks.
        SMOKE_FAILED=()
        echo "  nx upgrade --dry-run:"
        if ! nx upgrade --dry-run 2>&1 | sed 's/^/    /'; then
            echo "    [FAIL] -- exit non-zero" >&2
            SMOKE_FAILED+=("nx upgrade --dry-run")
        fi
        echo
        echo "  nx upgrade (apply):"
        if ! nx upgrade 2>&1 | sed 's/^/    /'; then
            echo "    [FAIL] -- exit non-zero" >&2
            SMOKE_FAILED+=("nx upgrade (apply)")
        fi
        echo
        # nx plan reseed seeds the builtin plan templates that
        # --check-plan-library verifies. Without this step the doctor
        # check fails on every fresh sandbox — that is "you forgot the
        # second setup step", not "something is genuinely broken." Make
        # smoke green-green-green when the install is healthy.
        #
        # nexus-vl8lk: this used to be `nx catalog setup`, which is
        # RETIRED (raises ClickException — the catalog is engine-owned,
        # nothing left here to set up) and had been silently swallowed by
        # `|| true` ever since, so the seeding half of this step had
        # already stopped running before --check-plan-library was ported
        # off its N/A stub. `nx plan reseed` is the live equivalent —
        # idempotent, routes through HttpPlanLibrary in every mode.
        echo "  nx plan reseed (seeds plan library):"
        if ! nx plan reseed 2>&1 | tail -5 | sed 's/^/    /'; then
            echo "    [FAIL] -- exit non-zero" >&2
            SMOKE_FAILED+=("nx plan reseed")
        fi
        echo
        # A gate that PRINTS a failure and exits 0 is not a gate. This loop
        # detected failures correctly (set -euo pipefail at the top makes the
        # pipeline carry nx doctor's status into the `if`) and then dropped
        # them on the floor: the caller saw exit 0 with "[FAIL]" in the log.
        # Found by the 7.0.0 release dry run, where --check-taxonomy failed and
        # the gate reported success.
        for check in --check-schema --check-plan-library --check-taxonomy; do
            echo "  nx doctor $check:"
            # nexus-b1v9z: --check-schema's honest N/A (endpoint withholds
            # the schema fingerprint by design) is an intentional exit-0
            # outcome for interactive use (nexus-vl8lk) but is indistinguishable
            # from a real pass to THIS caller -- a release gate whose entire
            # point is proving the substrate is present and correct.
            # --fail-on-violation makes that N/A a hard failure here without
            # touching interactive `nx doctor --check-schema`'s behavior.
            check_args=("$check")
            if [[ "$check" == "--check-schema" ]]; then
                check_args+=(--fail-on-violation)
            fi
            if nx doctor "${check_args[@]}" 2>&1 | sed 's/^/    /'; then
                echo "    [pass]"
            else
                echo "    [FAIL] -- exit non-zero" >&2
                SMOKE_FAILED+=("nx doctor $check")
            fi
            echo
        done
        echo "[done] Sandbox state at $SANDBOX. Run '$0 reset' to tear down."
        if (( ${#SMOKE_FAILED[@]} )); then
            echo >&2
            echo "SMOKE FAILED: ${#SMOKE_FAILED[@]} step(s) exited non-zero:" >&2
            printf '  %s\n' "${SMOKE_FAILED[@]}" >&2
            exit 1
        fi
        echo "SMOKE PASSED: all steps green."
        ;;

    shakedown)
        # Ensemble pipeline check: every nx surface exercised in sequence
        # against the wheel install. Uses the smaller PDF fixture (tc-sql)
        # for speed. T1 sniff at the start + end catches lifecycle bugs
        # (orphan tmpdirs, leaked session files).
        #
        # Force local mode so the shakedown does not contact ChromaDB Cloud
        # even if the parent shell has CHROMA_* set. Mirrors tests/e2e/run.sh.
        export NX_LOCAL=1
        unset CHROMA_API_KEY CHROMA_TENANT CHROMA_DATABASE VOYAGE_API_KEY
        echo "[3/3] Shakedown: full pipeline ensemble (running from /tmp, NX_LOCAL=1)"
        cd /tmp
        _provision_local_service

        # nexus-6xkdu: the four indexing steps (2, 3a, 3b, 4) previously ran
        # under `|| true`, so a broken indexer — including a broken MinerU
        # path — could not turn this run red. Collect-and-continue rather
        # than abort-on-first: a failure here does not forfeit the
        # diagnostic value of the remaining steps (search, T2, T1, catalog
        # readback), but it DOES turn the final verdict FAILED. Mirrors the
        # SMOKE_FAILED pattern in the smoke arm above.
        SHAKEDOWN_FAILED=()
        # gap-8 (T2 [22511]): steps that are deliberately NOT part of the
        # pass/fail verdict (a pre-step whose real gate runs right after it —
        # e.g. the 11/11 backfill-collections pre-step below) still need to
        # be VISIBLE, never silently dropped. Collected here and printed
        # unconditionally in the final summary, regardless of overall
        # pass/fail. (The T1 sniff formerly lived here too — nexus-whqun
        # promoted it to a real SHAKEDOWN_FAILED gate; see its own comment.)
        SHAKEDOWN_SOFT=()

        echo
        echo "── T1 sniff: BEFORE ──"
        # nexus-whqun: see _t1_stray_tmp_count's comment near the top of
        # this file — the prior BEFORE_SESSIONS/BEFORE_TMPDIRS measurement
        # globbed two directories nothing in this tree writes to any more.
        BEFORE_TMP_LITTER=$(_t1_stray_tmp_count "$HOME/.config/nexus")
        echo "  stray T1 .tmp litter: $BEFORE_TMP_LITTER"

        echo
        echo "── nx --version + upgrade ──"
        nx --version | sed 's/^/  /'
        # gap-8 (T2 [22511]): was `|| true` -- ran the user-facing
        # convergence command and discarded whether it worked.
        if ! nx upgrade 2>&1 | sed 's/^/  /'; then
            echo "  [FAIL] nx upgrade exited non-zero" >&2
            SHAKEDOWN_FAILED+=("0/11 nx upgrade")
        fi

        echo
        echo "── 1/11 nx plan reseed (seeds plan library) ──"
        # nexus-vl8lk: was `nx catalog setup` (retired, raised, swallowed by
        # `|| true`) — see the smoke arm's comment above for the full story.
        # gap-8 (T2 [22511]): the `|| true` on the live `nx plan reseed`
        # call was purged the same way.
        if ! nx plan reseed 2>&1 | tail -5 | sed 's/^/  /'; then
            echo "  [FAIL] nx plan reseed exited non-zero" >&2
            SHAKEDOWN_FAILED+=("1/11 nx plan reseed")
        fi

        echo
        echo "── 2/11 nx index repo (bounded fixture corpus) ──"
        # nexus-m7kcv (Hal-approved gate-scope decision, 2026-08-07):
        # indexing the FULL repo at sandbox local-embed speed (bge-768
        # ONNX, no Voyage key in the sandbox — ~2.8 chunks/s) is ~2.5h, so
        # this step was never actually completing inside anything
        # resembling a routine gate run ("shakedown scope" only in name —
        # vacuous-era affordability). Build a deterministic, explicitly
        # hard-coded fixture subset instead: a representative slice of
        # src/nexus/catalog (all 17 files — the module this shakedown's
        # own step 11 exercises via catalog doctor), a sample of
        # src/nexus/commands, and a handful of docs/*.md including one
        # AGENTS.md. Hard-coded rather than globbed so the corpus stays
        # reproducible run-to-run and reviewable in a diff — a glob would
        # silently pull in whatever the tree grows to.
        FIXTURE_FILES=(
            src/nexus/catalog/__init__.py
            src/nexus/catalog/auto_linker.py
            src/nexus/catalog/catalog_protocol.py
            src/nexus/catalog/catalog_spans.py
            src/nexus/catalog/chunk_quarantine.py
            src/nexus/catalog/collection_name.py
            src/nexus/catalog/dt_link_generator.py
            src/nexus/catalog/factory.py
            src/nexus/catalog/http_catalog_client.py
            src/nexus/catalog/link_generator.py
            src/nexus/catalog/manifest_backfill.py
            src/nexus/catalog/manifest_heal.py
            src/nexus/catalog/orphan_backfill.py
            src/nexus/catalog/store_hook.py
            src/nexus/catalog/tumbler.py
            src/nexus/catalog/types.py
            src/nexus/catalog/write_priority.py
            src/nexus/commands/catalog.py
            src/nexus/commands/daemon.py
            src/nexus/commands/doctor.py
            src/nexus/commands/index.py
            src/nexus/commands/init.py
            src/nexus/commands/memory.py
            src/nexus/commands/scratch.py
            src/nexus/commands/search_cmd.py
            src/nexus/commands/store.py
            src/nexus/commands/upgrade.py
            docs/architecture.md
            docs/catalog.md
            docs/cli-reference.md
            docs/configuration.md
            docs/contributing.md
            docs/getting-started.md
            docs/repo-indexing.md
            docs/storage-tiers.md
            AGENTS.md
        )
        FIXTURE_DIR="$SANDBOX/shakedown-fixture"
        rm -rf "$FIXTURE_DIR"
        for f in "${FIXTURE_FILES[@]}"; do
            src="$REPO_ROOT/$f"
            [[ -f "$src" ]] || _die "shakedown fixture file missing from repo: $f (FIXTURE_FILES in $0 is stale — update it)"
            dest="$FIXTURE_DIR/$f"
            mkdir -p "$(dirname "$dest")"
            cp "$src" "$dest"
        done
        # nx index repo refuses a path with no .git (nexus-git-guard), so
        # the fixture needs a real (throwaway) repo, not just loose files.
        # Stage by the same explicit FIXTURE_FILES list used to populate
        # the tree — never a blanket `git add -A` — so a stray file
        # dropped into $FIXTURE_DIR by a future edit can't silently widen
        # the indexed corpus.
        (cd "$FIXTURE_DIR" \
            && git init -q \
            && git add "${FIXTURE_FILES[@]}" \
            && git -c user.email=shakedown@nexus.local -c user.name=shakedown \
                   commit -q -m "shakedown fixture snapshot")
        echo "  fixture: ${#FIXTURE_FILES[@]} files under $FIXTURE_DIR"
        # gap-15 (T2 [22511]): non-vacuity floor -- 60% of the fixture file
        # count, both for docs and chunks (each indexed file should produce
        # at least one catalog document with at least one chunk; 60% leaves
        # slack for any file type this fixture set later grows to include
        # that legitimately produces zero chunks, without being so loose it
        # misses a real zero-chunk regression).
        REPO_FLOOR=$(( ${#FIXTURE_FILES[@]} * 60 / 100 ))
        read -r RDOCS_BEFORE RCHUNKS_BEFORE < <(_catalog_counts)
        # nexus-98zsp: the full client log is kept (the throughput gate
        # counts its "N chunks" lines) and the step is timed.
        mkdir -p "$SANDBOX/logs"
        INDEX_REPO_LOG="$SANDBOX/logs/shakedown-index-repo.log"
        INDEX_REPO_T0=$SECONDS
        # nexus-s71lr: live-tail this step's own log so a stall (the eta
        # ticker / heartbeat lines nx index now prints by default) is visible
        # in THIS transcript, not only in the post-hoc tail -5 below.
        _start_live_log_tail "$INDEX_REPO_LOG"; _REPO_TAIL_PID="$_LIVE_TAIL_PID"
        if ! nx index repo "$FIXTURE_DIR" >"$INDEX_REPO_LOG" 2>&1; then
            _stop_live_log_tail "$_REPO_TAIL_PID"
            tail -5 "$INDEX_REPO_LOG" | sed 's/^/  /'
            echo "  [FAIL] nx index repo exited non-zero" >&2
            SHAKEDOWN_FAILED+=("2/11 nx index repo")
        else
            _stop_live_log_tail "$_REPO_TAIL_PID"
            tail -5 "$INDEX_REPO_LOG" | sed 's/^/  /'
            _throughput_step "2/11 index-repo" "sandbox-repo-fixture" "$INDEX_REPO_LOG" $((SECONDS - INDEX_REPO_T0))
            # Chunk floor is 3x the doc floor (same 1:3 ratio as the PDF
            # steps): live evidence 2026-08-14 measured ~47 chunks/doc on
            # this fixture set, so 3x has wide margin while still catching
            # a chunking-degraded-to-1-per-doc regression that the old
            # equal doc==chunk floor waved through.
            _index_floor_check "2/11 nx index repo" "$REPO_FLOOR" "$((REPO_FLOOR * 3))" "$RDOCS_BEFORE" "$RCHUNKS_BEFORE"
        fi

        echo
        echo "── 3a/11 nx index pdf (tc-sql.pdf — Docling path, no formulas) ──"
        read -r PDOCS_BEFORE PCHUNKS_BEFORE < <(_catalog_counts)
        if ! nx index pdf "$REPO_ROOT/tests/fixtures/tc-sql.pdf" \
                --collection knowledge__shakedown 2>&1 | tail -5 | sed 's/^/  /'; then
            echo "  [FAIL] nx index pdf (tc-sql.pdf, Docling path) exited non-zero" >&2
            SHAKEDOWN_FAILED+=("3a/11 nx index pdf (Docling path)")
        else
            # gap-15: one PDF -> floor 1 doc; a real multi-page PDF should
            # chunk into several pieces, floor 3 catches a zero/near-zero
            # chunk regression without being brittle to chunking-boundary
            # drift across tokenizer/model changes.
            _index_floor_check "3a/11 nx index pdf (Docling path)" 1 3 "$PDOCS_BEFORE" "$PCHUNKS_BEFORE"
        fi

        echo
        echo "── 3b/11 nx index pdf (bft-to-smr.pdf — MinerU path, formulas) ──"
        # nexus-2fyb: shakedown previously tested ONLY tc-sql.pdf which has
        # zero formulas and never invokes MinerU. After mineru was promoted
        # to a default dep, the shakedown must actually exercise that code
        # path — otherwise a regression in the formula-routing/MinerU code
        # would slip through (which is exactly how the original silent-
        # corruption bug shipped). bft-to-smr.pdf has 9 raw math symbols,
        # crosses the auto-route threshold, and is the smallest formula
        # fixture available (~440 KB). First MinerU run downloads ~2-3 GB
        # of models, so this step pays the model-download cost on cold
        # sandbox runs.
        #
        # nexus-6xkdu: this step used to run under `|| true`, which is the
        # reason the "shakedown covers MinerU end-to-end" docstrings
        # scattered across the pytest suite were false — the step could not
        # fail even when MinerU itself was broken. Now propagated: a
        # non-zero exit here is collected into SHAKEDOWN_FAILED and turns
        # the final verdict red.
        # nexus-jy4hd: MinerU used to be asserted by exit code ALONE on an
        # extractor=auto route — a MinerU outage degraded the step to
        # Docling/PyMuPDF and the gate stayed green while the release
        # shipped on the belief MinerU was exercised. Three fixes:
        # (1) doctor --check-mineru first, verdict parsed (not rc-trusted);
        # (2) --extractor mineru EXPLICIT, so a route-away is impossible
        #     and a MinerU failure reddens the step;
        # (3) post-hoc identity assert on the indexed chunk's Extractor
        #     field, against any future in-extractor fallback.
        MINERU_DOCTOR_VERDICT="$(nx doctor --check-mineru 2>&1 | _mineru_doctor_verdict)"
        if [ "$MINERU_DOCTOR_VERDICT" != "OK" ]; then
            echo "  [FAIL] nx doctor --check-mineru: ${MINERU_DOCTOR_VERDICT#FAIL|}" >&2
            SHAKEDOWN_FAILED+=("3b/11 doctor --check-mineru")
        fi
        read -r MDOCS_BEFORE MCHUNKS_BEFORE < <(_catalog_counts)
        # Identity-scoping (review Critical): snapshot the collection's id
        # set BEFORE 3b so the assert below inspects only chunks THIS step
        # added — step 3a shares the collection and the list is
        # chash-ordered, so any positional pick is the wrong document
        # roughly half the time. Step-local mktemp dir: $SCRATCH belongs to
        # the SIBLING gate script and was unbound here (wave-3.5 review,
        # BOTH reviewers: under set -u the || true swallowed the unbound-var
        # error, the snapshot files never existed, and the identity assert
        # failed unconditionally on every run).
        MINERU_IDS_DIR="$(mktemp -d /tmp/release-sandbox-mineru-ids-XXXXXX)"
        nx store list --collection knowledge__shakedown 2>/dev/null \
            | grep -oE '\b[0-9a-f]{64}\b' > "$MINERU_IDS_DIR/before" || true
        if ! nx index pdf "$REPO_ROOT/tests/fixtures/bft-to-smr.pdf" \
                --extractor mineru \
                --collection knowledge__shakedown 2>&1 | tail -5 | sed 's/^/  /'; then
            echo "  [FAIL] nx index pdf (bft-to-smr.pdf, MinerU path) exited non-zero" >&2
            SHAKEDOWN_FAILED+=("3b/11 nx index pdf (MinerU path)")
        else
            # gap-15: same reasoning as 3a — one PDF -> floor 1 doc, floor 3
            # chunks (bft-to-smr.pdf is a real multi-page formula fixture).
            _index_floor_check "3b/11 nx index pdf (MinerU path)" 1 3 "$MDOCS_BEFORE" "$MCHUNKS_BEFORE"
            # (3) identity: a chunk ADDED BY THIS STEP must say mineru.
            # Chunk ids are full 64-hex (RDR-180); the set diff against the
            # pre-step snapshot is what scopes the pick to bft-to-smr.pdf.
            nx store list --collection knowledge__shakedown 2>/dev/null \
                | grep -oE '\b[0-9a-f]{64}\b' > "$MINERU_IDS_DIR/after" || true
            # Pipe-free first-line pick (pipefail-lint clean): the command
            # substitution drains comm to EOF; parameter expansion takes
            # the first id.
            NEW_MINERU_IDS="$(_new_chunk_ids "$MINERU_IDS_DIR/before" "$MINERU_IDS_DIR/after" || true)"
            MCHUNK_ID="${NEW_MINERU_IDS%%$'\n'*}"
            if [ -n "$MCHUNK_ID" ]; then
                IDENT_VERDICT="$(nx store get "$MCHUNK_ID" --collection knowledge__shakedown 2>/dev/null | _extractor_identity_verdict)"
            else
                IDENT_VERDICT="FAIL|step 3b added no new chunk ids to knowledge__shakedown — nothing to verify extractor identity against"
            fi
            if [ "$IDENT_VERDICT" != "OK" ]; then
                echo "  [FAIL] extractor identity: ${IDENT_VERDICT#FAIL|}" >&2
                SHAKEDOWN_FAILED+=("3b/11 extractor identity (mineru)")
            else
                echo "  [ok] indexed chunk extraction_method=mineru (identity proven, nexus-jy4hd)"
            fi
            rm -rf "$MINERU_IDS_DIR"
        fi

        echo
        echo "── 4/11 nx index rdr ──"
        # gap-15: docs/rdr carries ~190 RDR files at the time this floor was
        # set; 10 is a deliberately conservative floor (survives future
        # trims/consolidation of the RDR corpus) that still catches a
        # complete-failure regression (zero indexed).
        read -r ODOCS_BEFORE OCHUNKS_BEFORE < <(_catalog_counts)
        INDEX_RDR_LOG="$SANDBOX/logs/shakedown-index-rdr.log"
        INDEX_RDR_T0=$SECONDS
        # nexus-s71lr: this is the EXACT command the bead's own reproduction
        # used (212 RDR files, 13 minutes, no output) -- live-tail so its
        # heartbeat lines are visible in THIS transcript while it runs.
        _start_live_log_tail "$INDEX_RDR_LOG"; _RDR_TAIL_PID="$_LIVE_TAIL_PID"
        if ! nx index rdr "$REPO_ROOT" >"$INDEX_RDR_LOG" 2>&1; then
            _stop_live_log_tail "$_RDR_TAIL_PID"
            tail -5 "$INDEX_RDR_LOG" | sed 's/^/  /'
            echo "  [FAIL] nx index rdr exited non-zero" >&2
            SHAKEDOWN_FAILED+=("4/11 nx index rdr")
        else
            _stop_live_log_tail "$_RDR_TAIL_PID"
            tail -5 "$INDEX_RDR_LOG" | sed 's/^/  /'
            _throughput_step "4/11 index-rdr" "sandbox-rdr-corpus" "$INDEX_RDR_LOG" $((SECONDS - INDEX_RDR_T0))
            # 1:3 doc:chunk ratio floor, matching the other steps; live
            # evidence 2026-08-14 measured ~36 chunks/doc here.
            _index_floor_check "4/11 nx index rdr" 10 30 "$ODOCS_BEFORE" "$OCHUNKS_BEFORE"
        fi

        echo
        echo "── 5/11 nexus-e5uw greenfield acceptance: no deprecated chunk keys ──"
        # Bead nexus-e5uw acceptance: a fresh greenfield index must produce
        # 0 chunks carrying any of {source_path, git_branch, git_commit_hash,
        # git_project_name, git_remote_url, corpus, store_type, git_meta}.
        # RDR-102 Phase B drops source_path; normalize() drops the four
        # flat git_* keys; RDR-101 Phase 5c additionally dropped corpus,
        # store_type, git_meta from ALLOWED_TOP_LEVEL.
        #
        # nexus-iftc retired ``nx catalog prune-deprecated-keys``. Delegate
        # to the canonical pytest regression guard at
        # tests/test_indexer_e2e.py::test_greenfield_index_writes_no_deprecated_keys,
        # which walks T3 via ``col.get(include=["metadatas"])`` and asserts
        # zero leaks across all collections this test produced. Running
        # under ``uv run`` from REPO_ROOT picks up the editable install
        # so the assertion runs against the same in-tree code the
        # sandbox's nx wheel was built from.
        # ``-m integration`` is required: the test module is marked
        # ``pytestmark = pytest.mark.integration`` and the project
        # default in pyproject.toml deselects that marker. Without
        # the flag pytest exits 5 ("no tests collected") and the
        # shakedown reads it as FAIL even though the regression
        # guard never ran.
        # nexus-nyry9.13 (2026-08-21): capture the FULL pytest output to a
        # file and print only its tail on success -- the prior `| tail -5`
        # swallowed the session-level failure reason. The 7.14.0 shakedown
        # printed "1 passed" in those five lines while pytest still exited
        # non-zero (a pytest_sessionfinish guard sets session.exitstatus=1
        # AFTER the summary line), so the [FAIL] branch fired with no
        # visible cause. On failure print the whole log.
        # The sandbox's own daemons (aspect-worker, mineru-api, the local
        # service) write logs/locks under $SANDBOX/.config/nexus for the
        # whole shakedown, and with HOME=$SANDBOX that IS the directory the
        # nexus-pfuns real-config-dir guard (tests/conftest.py) scans -- so
        # the guard fails this otherwise-green session on the sandbox's own
        # ambient writes. Point it at an empty scratch dir for this one
        # pytest invocation (the documented busy-box seam); the real guard
        # still runs in the unit-suite stage of the battery.
        _gf_guard_dir="$(mktemp -d "${TMPDIR:-/tmp}/nx-greenfield-guard-XXXXXX")"
        # No `.log` suffix: BSD mktemp (macOS) only substitutes X's when they
        # are TRAILING. With a suffix it takes the template literally, so the
        # first run ever creates a real file named `nx-greenfield-XXXXXX.log`
        # and EVERY later run dies `mktemp: ... File exists` at this step --
        # step 5/11, i.e. after the repo index, both PDF indexes (including
        # the cold MinerU download) and the RDR index. The sibling `mktemp -d`
        # above was always fine because its X's already trail.
        _gf_log="$(mktemp "${TMPDIR:-/tmp}/nx-greenfield-XXXXXX")"
        if (cd "$REPO_ROOT" && NX_REAL_CONFIG_DIR_FOR_GUARD_TEST="$_gf_guard_dir" \
                uv run pytest -x -q --no-header -m integration \
                tests/test_indexer_e2e.py::test_greenfield_index_writes_no_deprecated_keys \
                >"$_gf_log" 2>&1); then
            tail -5 "$_gf_log" | sed 's/^/  /'
            echo "  [pass] greenfield index produced 0 chunks with deprecated keys"
        else
            echo "  --- full pytest output ($_gf_log) ---"
            sed 's/^/  /' "$_gf_log"
            # gap-8 D3 (T2 [22511]): report the OBSERVATION (the pytest
            # regression test exited non-zero / assertion failed), and name
            # the nexus-e5uw class as a HYPOTHESIS the test's own name and
            # assertions point at — not an asserted-but-unobserved cause.
            # The actual failure detail is in the pytest output printed
            # above this block.
            echo "  [FAIL] test_greenfield_index_writes_no_deprecated_keys failed" >&2
            echo "         Hypothesis (unconfirmed beyond the test's own name/assertion):"
            echo "         nexus-e5uw class — indexer may be writing pruned/deprecated keys."
            echo "         See the pytest output above for the actual failure."
            echo "         Investigate before merge."
            exit 1
        fi

        echo
        echo "── 6/11 cross-corpus search ──"
        # critic Significant 2 (T2 [21599]): de-theatre — the sandbox now
        # has a real, provisioned service and (since the fixture corpus
        # above) real indexed content to find, so an empty result here is
        # a genuine search/embed-pipeline signal, not "the sandbox happens
        # to be empty". "catalog link graph" is chosen to land in the
        # step-2 fixture (docs/catalog.md, docs/storage-tiers.md,
        # docs/architecture.md all use that exact phrase; it also appears
        # in a handful of RDR docs indexed by step 4, so hits may come
        # from either corpus — the assert is "the pipeline finds indexed
        # content", not fixture-exclusivity). `nx search` exits 0 even on
        # zero hits (not an error condition for interactive use), so the
        # check is on the literal "No results." text — but a NON-ZERO
        # exit (real search/embed failure) must land in the verdict too,
        # not trip errexit past the collect-and-continue convention.
        if ! SEARCH_OUT=$(nx search "catalog link graph" -m 3 2>&1); then
            echo "$SEARCH_OUT" | tail -10 | sed 's/^/  /'
            echo "  [FAIL] nx search exited non-zero" >&2
            SHAKEDOWN_FAILED+=("6/11 nx search (non-zero exit)")
        else
            echo "$SEARCH_OUT" | tail -10 | sed 's/^/  /'
            if [[ "$SEARCH_OUT" == *"No results."* ]]; then
                echo "  [FAIL] cross-corpus search returned no hits (expected indexed-content hits)" >&2
                SHAKEDOWN_FAILED+=("6/11 nx search (no hits)")
            fi
        fi

        echo
        echo "── 7/11 T2 memory roundtrip ──"
        SHAKE_TS=$(date +%s)
        if ! nx memory put "shakedown marker $SHAKE_TS" \
                --project nexus_shakedown --title shakedown.md 2>&1 | tail -2 | sed 's/^/  /'; then
            echo "  [FAIL] nx memory put exited non-zero" >&2
            SHAKEDOWN_FAILED+=("7/11 nx memory put")
        fi
        if ! MEMORY_GET_OUT=$(nx memory get --project nexus_shakedown --title shakedown.md 2>&1); then
            # || true: head is an early-exit consumer — under pipefail a
            # SIGPIPE'd echo would abort the script BEFORE the [FAIL]
            # bookkeeping below (nexus-6zxfb, same class as nexus-i66g4).
            echo "$MEMORY_GET_OUT" | head -3 | sed 's/^/  /' || true
            echo "  [FAIL] nx memory get exited non-zero" >&2
            SHAKEDOWN_FAILED+=("7/11 nx memory get (non-zero exit)")
        else
            echo "$MEMORY_GET_OUT" | head -3 | sed 's/^/  /' || true
            if [[ "$MEMORY_GET_OUT" != *"$SHAKE_TS"* ]]; then
                echo "  [FAIL] T2 memory get did not return the marker written above" >&2
                SHAKEDOWN_FAILED+=("7/11 nx memory get roundtrip")
            fi
        fi

        echo
        echo "── 8/11 T1 scratch use (write + readback) ──"
        # Outside a Claude Code session, no SessionStart hook fires to
        # publish a live MCP session lease. T1 is PG-only (nexus-4lkmz —
        # the in-process ``NX_T1_ISOLATED=1`` escape hatch retired
        # outright, it now hard-fails). The bare-CLI path
        # (``get_t1_database()``'s CLI-dedicated mint, nexus-rn3wo.1)
        # mints its own PERSISTED session against the live storage service
        # and REUSES it across every subsequent bare-CLI invocation in this
        # sandbox HOME (the id is cached to
        # ``$HOME/.config/nexus/t1_cli_dedicated_session`` on first mint) —
        # so cross-invocation readback genuinely works here. gap-8 (T2
        # [22511]): this step's title always claimed "write + readback" but
        # only ever performed the write; the "readback only works inside a
        # Claude Code session" note below was stale, predating the
        # persisted CLI-dedicated session design (nexus-rn3wo.1). Now
        # performs the readback for real and joins SHAKEDOWN_FAILED on
        # failure, same as every other fail-capable step in this arm.
        SCRATCH_OUT=$(nx scratch put "shakedown probe $SHAKE_TS" --tags=shakedown 2>&1 | tail -1)
        if [[ "$SCRATCH_OUT" == *"Stored:"* ]]; then
            echo "  put: ok ($SCRATCH_OUT)"
        else
            echo "  put: [FAIL] unexpected output — $SCRATCH_OUT" >&2
            SHAKEDOWN_FAILED+=("8/11 nx scratch put")
        fi
        SCRATCH_LIST_OUT=$(nx scratch list 2>&1)
        if [[ "$SCRATCH_LIST_OUT" == *"$SHAKE_TS"* ]]; then
            echo "  readback: ok (marker found via nx scratch list)"
        else
            echo "  readback: [FAIL] marker not found in nx scratch list output" >&2
            echo "$SCRATCH_LIST_OUT" | tail -5 | sed 's/^/    /'
            SHAKEDOWN_FAILED+=("8/11 nx scratch list readback")
        fi

        echo
        echo "── 9/11 catalog stats (registry + link graph readback) ──"
        # Deliberately informational-only, not fail-capable (unlike 6/7
        # above): `nx catalog stats` has no pass/fail contract of its own
        # to assert on — it's a readback for human eyeballing, and the
        # collections-drift release gate that DOES have a fail contract
        # over the same catalog state runs explicitly at 11/11 below.
        nx catalog stats 2>&1 | head -15 | sed 's/^/  /' || true

        echo
        echo "── 10/11 nx doctor (all checks, post-activity) ──"
        # (--check-tmpdirs retired at RDR-155 P4b with the chroma T1 tmpdirs.)
        # BARE `nx doctor` runs run_health_checks() — the corpus-integrity
        # instruments live ONLY there, not behind any --check-* flag. The
        # strandedness audit (T2 [21590], 2026-08-07) found this step had
        # never invoked it, so the release gate was blind and `|| true`
        # meant even the flag-scoped checks could not redden the run.
        # Fail-capable now, per the 6xkdu de-theatre precedent.
        #
        # nexus-u88vu: this used to call `_fail`, which is undefined anywhere
        # in this script — the first honest doctor red died with rc=127
        # "_fail: command not found" instead of reporting the failure.
        # Route through the same SHAKEDOWN_FAILED collect-and-continue
        # convention (6xkdu) every other fail-capable step in this arm
        # already uses, so a doctor red produces an explicit verdict line
        # rather than crashing the script.
        #
        # nexus-jds59: --git-hooks-scope "$SANDBOX" restricts the
        # stanza-drift walk (part of the bare sweep above) to repos
        # registered under this sandbox. The registered-repo catalog is
        # shared machine-wide, not scoped to $HOME, so an unscoped walk
        # also sees every other repo ever indexed on this machine —
        # including the live dev checkout this sandbox reinstalls from,
        # whose post-commit hook may be deliberately held on an older
        # stanza. That ambient state has nothing to do with this gate.
        if ! nx doctor --git-hooks-scope "$SANDBOX" 2>&1 | sed 's/^/  /'; then
            echo "  [FAIL] nx doctor exited non-zero" >&2
            SHAKEDOWN_FAILED+=("10/11 nx doctor")
        fi
        for check in --check-schema --check-plan-library --check-taxonomy; do
            echo "  $check:"
            # nexus-b1v9z: see the smoke-mode loop's comment above -- an
            # honest --check-schema N/A must not read as a pass to a
            # release gate.
            check_args=("$check")
            if [[ "$check" == "--check-schema" ]]; then
                check_args+=(--fail-on-violation)
            fi
            if ! nx doctor "${check_args[@]}" 2>&1 | tail -5 | sed 's/^/    /'; then
                echo "  [FAIL] nx doctor $check exited non-zero" >&2
                SHAKEDOWN_FAILED+=("10/11 nx doctor $check")
            fi
        done

        # nexus-9nchs part B: bare `nx doctor`'s own rc (checked above) is
        # structurally blind to chash conformance / stale index-run fences /
        # manifest pre-backfill rows — all three are warn=True by RDR-129 B4
        # design (see _DOCTOR_CORPUS_INTEGRITY_LABELS's comment near the top
        # of this file), so a regression in any of them could not previously
        # redden this step no matter what happened. Read `nx doctor --json`
        # directly and assert on the named checks' own status field instead
        # of doctor's rc.
        echo "  corpus-integrity instruments (chash conformance, stale index-run"
        echo "  fences, manifest pre-backfill rows) — warn=true by design, so"
        echo "  they never flip bare doctor's exit code; asserting on --json directly:"
        DOCTOR_JSON_OUT=$(nx doctor --json --git-hooks-scope "$SANDBOX" 2>/dev/null || true)
        if [[ -z "$DOCTOR_JSON_OUT" ]]; then
            echo "  [FAIL] nx doctor --json produced no output" >&2
            SHAKEDOWN_FAILED+=("10/11 nx doctor --json (no output)")
        else
            CORPUS_REGRESSIONS=$(printf '%s' "$DOCTOR_JSON_OUT" | _doctor_corpus_integrity_regressions)
            if [[ -n "$CORPUS_REGRESSIONS" ]]; then
                echo "$CORPUS_REGRESSIONS" | sed 's/^/    [FAIL] /' >&2
                while IFS= read -r _regression_line; do
                    SHAKEDOWN_FAILED+=("10/11 nx doctor corpus-integrity: $_regression_line")
                done <<< "$CORPUS_REGRESSIONS"
            else
                echo "    [ok] chash conformance / stale index-run fences / manifest pre-backfill rows all clean"
            fi
        fi

        echo
        echo "── 11/11 nx catalog doctor (collections-drift release gate, nexus-o6aa.14) ──"
        # RDR-101 Phase 6: collections-drift is a release blocker.
        #
        # The indexer creates T3 collections on first write; the
        # collections projection is populated by ``nx catalog
        # backfill-collections`` (the documented remediation in the
        # doctor's own output). Run backfill THEN drift so the gate
        # validates the full create-register-check sequence rather
        # than failing on the transient unregistered window. A genuine
        # drift (orphan projection rows, missing T3 collections, or a
        # backfill that cannot reach a clean state) still surfaces
        # because the second check runs WITHOUT ``|| true`` and exits
        # non-zero on FAIL.
        echo "  [pre] nx catalog backfill-collections --no-dry-run:"
        # The verb defaults to --dry-run; the gate needs the actual
        # registration so the doctor's drift check sees the populated
        # projection on the next call.
        #
        # gap-8 (T2 [22511]): this stays a SOFT step (backfill is not
        # required to succeed for THIS gate to be meaningful — the
        # collections-drift check right below is the actual assertion, and
        # it runs WITHOUT `|| true`) but the soft-ness is now
        # SUMMARY-SURFACED, not invisible: a silent backfill failure used
        # to vanish into the `|| true`. SHAKEDOWN_SOFT entries print in the
        # final summary but never gate the exit code (only SHAKEDOWN_FAILED
        # does). It is recorded as an explicit
        # soft/advisory finding so a reviewer can see it happened, without
        # failing the run on a step whose job is prep, not verification.
        if ! nx catalog backfill-collections --no-dry-run 2>&1 | tail -5 | sed 's/^/    /'; then
            echo "  [soft-fail, non-blocking] nx catalog backfill-collections exited non-zero" >&2
            SHAKEDOWN_SOFT+=("11/11 nx catalog backfill-collections pre-step (non-blocking; see collections-drift check below for the real gate)")
        fi
        echo "  [check] nx catalog doctor --collections-drift:"
        if ! nx catalog doctor --collections-drift 2>&1 | sed 's/^/    /'; then
            # gap-8 D3 (T2 [22511]): report the observation (the doctor
            # check exited non-zero: release blocked) and let the doctor's
            # OWN printed output above (piped through unfiltered) carry the
            # specific diagnosis, rather than asserting a named cause this
            # script never independently confirmed.
            echo "  [FAIL] nx catalog doctor --collections-drift exited non-zero: release blocked" >&2
            echo "         See the doctor output above for the specific drift found."
            echo "         Investigate before tagging."
            exit 1
        fi

        echo
        echo "── T1 sniff: AFTER ──"
        AFTER_TMP_LITTER=$(_t1_stray_tmp_count "$HOME/.config/nexus")
        echo "  stray T1 .tmp litter: $AFTER_TMP_LITTER (was $BEFORE_TMP_LITTER)"
        DELTA_LITTER=$((AFTER_TMP_LITTER - BEFORE_TMP_LITTER))
        echo "  delta: +$DELTA_LITTER"
        # nexus-whqun: a REAL gate now (SHAKEDOWN_FAILED, not the SOFT/
        # ADVISORY list) — any net-new litter is a genuine crash-mid-
        # publish signal (see _t1_stray_tmp_count's comment near the top
        # of this file), not a benign steady-state count the way the two
        # dead directories the old measurement globbed were.
        if (( DELTA_LITTER > 0 )); then
            echo "  [FAIL] T1 stray .tmp litter increased by $DELTA_LITTER during this run" >&2
            echo "         Investigate $HOME/.config/nexus/t1_session_lease.*.tmp and"
            echo "         $HOME/.config/nexus/t1_cli_dedicated_session.*.tmp"
            SHAKEDOWN_FAILED+=("T1 sniff: stray .tmp litter (+$DELTA_LITTER)")
        else
            echo "  [ok] no new T1 .tmp litter"
        fi

        echo
        echo "[done] Sandbox state at $SANDBOX. Run '$0 reset' to tear down."
        if (( ${#SHAKEDOWN_SOFT[@]} )); then
            echo
            echo "SHAKEDOWN SOFT/ADVISORY (non-blocking, not counted in the pass/fail verdict):"
            printf '  %s\n' "${SHAKEDOWN_SOFT[@]}"
        fi
        if (( ${#SHAKEDOWN_FAILED[@]} )); then
            echo >&2
            echo "SHAKEDOWN FAILED: ${#SHAKEDOWN_FAILED[@]} release-gate step(s) exited non-zero:" >&2
            printf '  %s\n' "${SHAKEDOWN_FAILED[@]}" >&2
            exit 1
        fi
        # nexus-9nchs part C: name what this banner actually proved. Every
        # numbered step but 9/11 (`nx catalog stats` — deliberately
        # informational readback, no pass/fail contract of its own by
        # design) is fail-capable and feeds SHAKEDOWN_FAILED above; the
        # backfill-collections pre-step at 11/11 is deliberately soft
        # (its real gate, collections-drift, runs immediately after it and
        # IS fail-capable). Anything soft is already printed in the
        # SOFT/ADVISORY block above, not silently folded into "green".
        echo "SHAKEDOWN PASSED: every fail-capable release-gate step is green" \
             "(9/11 catalog stats is informational-only by design; see" \
             "SOFT/ADVISORY above for any other non-blocking findings)."
        ;;

    service)
        # RDR-157 P4.2 (bead nexus-vwvv5.18): fresh-machine -> serving with ZERO
        # manual steps, LOCAL mode. The sandbox HOME is the "fresh machine"; we
        # position the distribution artifact (what the release archive/launcher
        # ships), then prove a single `nx init --service` collapses
        # provision-PG -> fetch-bge-768 -> start-service -> /health green, is
        # idempotent on re-run, and tears down cleanly.
        export NX_LOCAL=1
        export NX_STORAGE_BACKEND=service
        unset CHROMA_API_KEY CHROMA_TENANT CHROMA_DATABASE VOYAGE_API_KEY
        echo "[3/3] Service E2E (LOCAL mode, fresh sandbox HOME=$SANDBOX):"
        cd /tmp

        # ── Artifact positioning. RDR-161: the native binary is the SOLE launch
        #    artifact — the java -jar fallback is expunged, so there is no
        #    repo-JAR dev fallback any more. Provide a native binary via
        #    NEXUS_SERVICE_BIN, the well-known location (e.g. from
        #    `nx daemon service install-binary <engine-service-v* tag>`), or
        #    `nx init --service` (which acquires + verifies it). ──
        SVC_WELL_KNOWN="$HOME/.config/nexus/service/nexus-service"
        if [[ -n "${NEXUS_SERVICE_BIN:-}" && -x "${NEXUS_SERVICE_BIN}" ]]; then
            echo "  artifact: native binary (NEXUS_SERVICE_BIN=$NEXUS_SERVICE_BIN)"
        elif [[ -x "$SVC_WELL_KNOWN" ]]; then
            echo "  artifact: native binary (well-known $SVC_WELL_KNOWN)"
        else
            _die "no native service binary: set NEXUS_SERVICE_BIN to a native nexus-service binary, or install one with 'nx daemon service install-binary <engine-service-v* tag>' (RDR-161: the java -jar path is expunged; there is no repo-JAR fallback)"
        fi

        # ── PG source (host PG on PATH, or a ship-alongside bundle). The bundle
        #    extract/initdb/provision is what nx init --service drives. ──
        if [[ -n "${NEXUS_PG_BUNDLE:-}" ]]; then
            echo "  pg source: ship-alongside bundle (NEXUS_PG_BUNDLE=$NEXUS_PG_BUNDLE)"
        elif command -v initdb >/dev/null 2>&1; then
            echo "  pg source: host PostgreSQL ($(command -v initdb))"
        else
            _die "no PostgreSQL: put initdb/pg_ctl on PATH (with pgvector) or set NEXUS_PG_BUNDLE to a ship-alongside bundle"
        fi

        # `_svc_field` / `_svc_teardown` are defined once near the top of this
        # script (shared with `_provision_local_service`, used by smoke/
        # shakedown) — keeps the actual health/port/pid parsing honest in one
        # place rather than duplicated per-mode (a stale lease can outlive a
        # dead JVM for up to the TTL, so "the command exited 0" is NOT proof
        # of serving).

        # ── The one command: fresh-install -> serving, zero manual steps. ──
        echo
        # --no-autostart (RDR-174 P2.4): the sandbox proves the SESSION-start
        # path (and its idempotent live-lease short-circuit below); it does not
        # want a persistent OS unit. Explicit since pre-P2.4 plain `nx init
        # --service` always session-started.
        echo "  ── nx init --service (the one-command collapse) ──"
        if ! nx init --service --no-autostart 2>&1 | sed 's/^/    /'; then
            _die "nx init --service did not reach serving (see remedy above)"
        fi
        # RDR-184 P0.2 (nexus-ccs9v.2): chain the top-level lock release into
        # this mode's own teardown trap — this assignment REPLACES the
        # top-level trap set earlier, so the lock release must be re-added
        # here or a crash in this window would leak the lock.
        trap '_kill_live_tail; _svc_teardown; lock_release "$LOCKDIR" 2>/dev/null || true' EXIT

        # ── serving proof: /health == ok (NOT merely "a lease exists"). ──
        echo
        echo "  ── service health (must be ok) ──"
        nx daemon service status 2>&1 | sed 's/^/    /' || true
        HEALTH=$(_svc_field health)
        PORT1=$(_svc_field port); PID1=$(_svc_field pid)
        [[ "$HEALTH" == "ok" ]] || _die "service not serving: /health=$HEALTH (expected ok)"
        [[ -n "$PORT1" && -n "$PID1" ]] || _die "no endpoint published (port=$PORT1 pid=$PID1)"
        echo "    [ok] serving on port $PORT1 (pid $PID1), /health=ok"
        # Visibility only: embedding_mode is voyage-vs-onnx-local (driven by
        # whether a Voyage key is present), NOT a clean bge-768 signal, so we
        # report it but do not assert on it. The bge-768 LOCAL ONNX is fetched +
        # validated by `nx init --service` itself (fail-loud); the JAR-fallback
        # path here does not re-prove the Java service's model load.
        echo "    embedding_mode=$(_svc_field embedding_mode)"

        # ── idempotency: re-run must hit the live-lease short-circuit and
        #    return the SAME endpoint, not spawn a second service. ──
        echo
        echo "  ── nx init --service AGAIN (idempotent re-run) ──"
        if ! nx init --service --no-autostart 2>&1 | sed 's/^/    /'; then
            _die "re-run of nx init --service failed (not idempotent)"
        fi
        PORT2=$(_svc_field port); PID2=$(_svc_field pid)
        [[ "$PORT2" == "$PORT1" && "$PID2" == "$PID1" ]] \
            || _die "re-run was NOT idempotent: endpoint changed ($PORT1/$PID1 -> $PORT2/$PID2)"
        echo "    [ok] idempotent: same endpoint $PORT2 (pid $PID2)"

        # ── teardown via the trap; clear it so we report honestly below. ──
        echo
        trap - EXIT
        _svc_teardown
        # trap - EXIT above cleared the combined teardown+lock-release trap,
        # so release explicitly now that teardown has run manually (RDR-184
        # P0.2, nexus-ccs9v.2).
        lock_release "$LOCKDIR" 2>/dev/null || true
        # Re-confirm the lease is gone (teardown actually stopped the service).
        if [[ "$(_svc_field health)" == "ok" ]]; then
            _die "service still serving after stop --with-pg — teardown failed"
        fi

        echo
        echo "[done] Service E2E green: fresh sandbox -> serving in one command -> stopped."
        echo "       Sandbox state at $SANDBOX. Run '$0 reset' to tear down."
        ;;

    shell)
        echo "[3/3] Dropping into subshell with HOME=$SANDBOX ..."
        echo "      Exit the subshell to restore your real \$HOME."
        echo
        # Subshell: env stays sandboxed, exit returns control + restores HOME.
        # NOT `exec` (RDR-184 P0.2, nexus-ccs9v.2): exec replaces this
        # script's own process image, which would skip the EXIT trap
        # entirely and leak the lock for the whole interactive session. Run
        # as a plain child instead — the lock stays held for the duration
        # of the interactive sandbox use (the correct semantics: another
        # invocation must not touch this sandbox while it is in use), and
        # releases via the normal EXIT trap once the user exits the shell.
        cd "$SANDBOX"
        env \
            HOME="$SANDBOX" \
            PATH="$SANDBOX/.local/bin:$PATH" \
            VOYAGE_API_KEY="${VOYAGE_API_KEY:-}" \
            CHROMA_API_KEY="${CHROMA_API_KEY:-}" \
            CHROMA_TENANT="${CHROMA_TENANT:-}" \
            CHROMA_DATABASE="${CHROMA_DATABASE:-default_database}" \
            ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}" \
            NEXUS_SANDBOX=1 \
            PS1="(sandbox) $ " \
            bash --noprofile --norc -i
        ;;

    tmux)
        if ! command -v tmux >/dev/null 2>&1; then
            _die "tmux not installed (brew install tmux)"
        fi
        AUTH_DIR="$REPO_ROOT/tests/e2e/.claude-auth"
        if [[ ! -f "$AUTH_DIR/.credentials.json" ]]; then
            _die "missing $AUTH_DIR/.credentials.json — run tests/e2e/auth-login.sh first"
        fi
        # Reuse cc-validation lib for tmux primitives + claude_start.
        export TEST_HOME="$SANDBOX"
        export TMUX_SESSION
        echo "[3/3] Launching Claude Code in tmux session '$TMUX_SESSION' ..."
        echo "      Detach: Ctrl-b d   |   Kill: tmux kill-session -t $TMUX_SESSION"
        echo
        # shellcheck source=/dev/null
        . "$REPO_ROOT/tests/e2e/lib.sh"
        # Ensure auth credentials are reachable inside the sandbox HOME.
        mkdir -p "$SANDBOX/.claude"
        cp "$AUTH_DIR/.credentials.json" "$SANDBOX/.claude/.credentials.json"
        if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
            tmux kill-session -t "$TMUX_SESSION"
        fi
        tmux new-session -d -s "$TMUX_SESSION" -x 220 -y 50 \
            "env HOME='$SANDBOX' PATH='$SANDBOX/.local/bin:$PATH' bash -i"
        sleep 1
        tmux send-keys -t "$TMUX_SESSION" "claude" Enter
        echo "Attaching ... (Ctrl-b d to detach without killing)"
        tmux attach -t "$TMUX_SESSION"
        ;;
esac
