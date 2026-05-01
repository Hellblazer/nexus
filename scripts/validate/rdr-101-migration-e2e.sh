#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# RDR-101 Phase 3 sandbox e2e migration validator (nexus-o6aa.9.10).
#
# Walks the actual operator upgrade workflow in an isolated NEXUS_CONFIG_DIR:
#
#   1. Bootstrap a "pre-RDR-101" catalog by running nx commands under
#      NEXUS_EVENT_SOURCED=0 — populates the legacy JSONL files (no events.jsonl).
#   2. Drop the gate (default ON under PR ζ); confirm bootstrap-fallback fires.
#   3. Run synthesize-log --force + t3-backfill-doc-id; confirm doctor PASS.
#   4. Mutate post-migration; confirm doctor stays PASS.
#   5. Rollback test (NEXUS_EVENT_SOURCED=0); confirm legacy reads still work.
#   6. Concurrency smoke; confirm events.jsonl stays well-formed.
#   7. Performance baseline for doctor.
#
# Self-contained: spawns its own embedded Chroma via NX_LOCAL=1, isolates
# config to a tmp dir, cleans up on exit. Non-destructive against the real
# user catalog.
#
# Usage:
#   ./scripts/validate/rdr-101-migration-e2e.sh
#
# Exit code 0 = all steps passed; non-zero = at least one assertion failed.
# Transcript and per-step logs land in $SANDBOX/.cache/.

set -uo pipefail

# ── Sandbox bootstrap ────────────────────────────────────────────────────

SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/nexus-rdr101-e2e-XXXXXX")"
ORIG_HOME="$HOME"
export HOME="$SANDBOX"
export NX_LOCAL=1
export NX_LOCAL_CHROMA_PATH="$SANDBOX/.local/share/nexus/chroma"
export NEXUS_CONFIG_DIR="$SANDBOX/.config/nexus"
export NEXUS_CATALOG_PATH="$SANDBOX/.config/nexus/catalog"
mkdir -p "$NEXUS_CONFIG_DIR" "$NX_LOCAL_CHROMA_PATH" "$SANDBOX/.cache"
TRANSCRIPT="$SANDBOX/.cache/transcript.log"

cleanup() {
    local rc=$?
    if [[ $rc -eq 0 ]]; then
        rm -rf "$SANDBOX"
    else
        printf "\nSandbox preserved at %s for inspection (transcript: %s)\n" \
            "$SANDBOX" "$TRANSCRIPT" >&2
    fi
    exit $rc
}
trap cleanup EXIT INT TERM

# ── Output helpers ────────────────────────────────────────────────────────

PASS=0
FAIL=0
FAILED_CASES=()

ts()   { date +"%H:%M:%S"; }
step() { printf "\n[%s] ═══ %s ═══\n" "$(ts)" "$*" | tee -a "$TRANSCRIPT" >&2; }
info() { printf "[%s]    %s\n"        "$(ts)" "$*" | tee -a "$TRANSCRIPT" >&2; }
pass() { printf "[%s]  ✓ %s\n"        "$(ts)" "$*" | tee -a "$TRANSCRIPT" >&2; PASS=$((PASS+1)); }
fail() { printf "[%s]  ✗ %s\n"        "$(ts)" "$*" | tee -a "$TRANSCRIPT" >&2; FAIL=$((FAIL+1)); FAILED_CASES+=("$*"); }

assert_contains() {
    local label="$1" haystack="$2" needle="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        pass "$label"
    else
        fail "$label  (expected substring: $needle)"
        printf "    haystack head:\n%s\n" "$(echo "$haystack" | head -20 | sed 's/^/      /')" >&2
    fi
}

assert_eq() {
    local label="$1" got="$2" want="$3"
    if [[ "$got" == "$want" ]]; then
        pass "$label"
    else
        fail "$label  (got=$got want=$want)"
    fi
}

run_capture() {
    # run_capture <varname> <cmd...> — capture combined stdout+stderr into
    # a variable, and also append to transcript.
    local _name="$1"; shift
    local _out
    _out="$("$@" 2>&1)"
    local _rc=$?
    printf "[%s] $ %s\n%s\n" "$(ts)" "$*" "$_out" >> "$TRANSCRIPT"
    printf -v "$_name" "%s" "$_out"
    return $_rc
}

# ── Step 1: Sandbox initialized ──────────────────────────────────────────

step "1. Sandbox initialized"
info "Sandbox:           $SANDBOX"
info "NEXUS_CONFIG_DIR:  $NEXUS_CONFIG_DIR"
info "NX_LOCAL_CHROMA:   $NX_LOCAL_CHROMA_PATH"
info "Transcript:        $TRANSCRIPT"

# Initialize the catalog. nx catalog init expects to write to NEXUS_CATALOG_PATH.
mkdir -p "$NEXUS_CATALOG_PATH"
run_capture INIT_OUT uv run nx catalog init || true
assert_contains "nx catalog init produces a catalog" "$INIT_OUT" "catalog"

# ── Step 2: Bootstrap pre-RDR-101 state under legacy mode ────────────────

step "2. Bootstrap legacy (pre-RDR-101) state"
export NEXUS_EVENT_SOURCED=0

# Hand-craft three small markdown files; index them under legacy mode so
# the catalog's documents.jsonl / owners.jsonl get populated without
# events.jsonl. This simulates a catalog from before PR α / β / γ.
mkdir -p "$SANDBOX/repo"
cat >"$SANDBOX/repo/alpha.md" <<'EOF'
# Alpha
The first sandbox document.
EOF
cat >"$SANDBOX/repo/beta.md" <<'EOF'
# Beta
The second sandbox document.
EOF
cat >"$SANDBOX/repo/gamma.md" <<'EOF'
# Gamma
The third sandbox document, for testing post-migration mutation.
EOF

# Index two of the three under legacy mode.
run_capture IDX_A uv run nx index md "$SANDBOX/repo/alpha.md" --corpus sandbox || true
info "alpha indexed: $(echo "$IDX_A" | tail -3 | head -1)"
run_capture IDX_B uv run nx index md "$SANDBOX/repo/beta.md" --corpus sandbox || true
info "beta indexed:  $(echo "$IDX_B" | tail -3 | head -1)"

# Confirm the legacy JSONL files are populated.
DOCS_JSONL="$NEXUS_CATALOG_PATH/documents.jsonl"
EVENTS_JSONL="$NEXUS_CATALOG_PATH/events.jsonl"
LEGACY_DOC_COUNT=$(grep -c '"_deleted"' "$DOCS_JSONL" 2>/dev/null || echo 0)
LEGACY_LINE_COUNT=$(wc -l <"$DOCS_JSONL" 2>/dev/null || echo 0)
info "documents.jsonl line count: $LEGACY_LINE_COUNT (deleted: $LEGACY_DOC_COUNT)"

if [[ "$LEGACY_LINE_COUNT" -ge 2 ]]; then
    pass "legacy documents.jsonl populated"
else
    fail "legacy documents.jsonl populated  (got $LEGACY_LINE_COUNT lines)"
fi

if [[ ! -s "$EVENTS_JSONL" ]]; then
    pass "events.jsonl absent or empty (legacy bootstrap shape)"
else
    fail "events.jsonl unexpectedly populated under NEXUS_EVENT_SOURCED=0"
    info "events.jsonl head:"
    head -3 "$EVENTS_JSONL" >&2
fi

# ── Step 3a: Empty events.jsonl falls through cleanly ──────────────────

step "3a. ES default ON with empty events.jsonl: synthesizer path PASSes"
unset NEXUS_EVENT_SOURCED  # default is ON under PR ζ.

run_capture DR0_OUT uv run nx catalog doctor --replay-equality
DR0_RC=$?
info "doctor exit: $DR0_RC"

# When events.jsonl is empty, ``use_event_log`` is False at the size
# gate before ``_event_log_covers_legacy`` runs — the guardrail does
# not fire. Doctor's synthesizer then walks the legacy JSONL, emits
# synthesized events on the fly, and PASSes. This is the correct
# behaviour for a freshly-upgraded catalog that has not yet seen any
# ES-mode mutations.
if [[ $DR0_RC -eq 0 ]]; then
    pass "doctor PASSes on empty-events catalog (synthesizer path)"
else
    fail "doctor unexpectedly fails on empty-events catalog (rc=$DR0_RC)"
fi
if [[ "$DR0_OUT" != *"bootstrap-fallback"* ]]; then
    pass "no bootstrap-fallback warning when events.jsonl is empty"
else
    fail "bootstrap-fallback fires unexpectedly when events.jsonl is empty"
fi

# ── Step 3b: ES mutation makes events.jsonl sparse → bootstrap fires ───

step "3b. ES mutation sparses events.jsonl; bootstrap-fallback fires"
# A single index call under ES default-on emits one DocumentRegistered
# (and one OwnerRegistered if the owner is new). Combined with the
# legacy documents.jsonl (3 entries from step 2 plus the new one),
# event_doc_count < threshold = max(1, int(N * 0.95)).
mkdir -p "$SANDBOX/repo/sparse"
cat >"$SANDBOX/repo/sparse/sparse-trigger.md" <<'EOF'
# Sparse Trigger
Indexed under ES to push events.jsonl past empty but well below
documents.jsonl, exercising the guardrail's sparse-vs-legacy branch.
EOF
run_capture IDX_SPARSE uv run nx index md "$SANDBOX/repo/sparse/sparse-trigger.md" \
    --corpus sandbox || true
info "sparse-trigger indexed: $(echo "$IDX_SPARSE" | tail -3 | head -1)"

EVT_LINES=$(wc -l <"$EVENTS_JSONL" 2>/dev/null || echo 0)
DOC_LINES=$(wc -l <"$DOCS_JSONL" 2>/dev/null || echo 0)
info "events.jsonl=$EVT_LINES lines  documents.jsonl=$DOC_LINES lines"

run_capture DR_OUT uv run nx catalog doctor --replay-equality
DR_RC=$?
info "doctor exit: $DR_RC"

assert_contains "bootstrap-fallback warning fires" "$DR_OUT" "bootstrap-fallback"
assert_contains "remediation hint includes --force"  "$DR_OUT" "synthesize-log --force"

if [[ $DR_RC -ne 0 ]]; then
    pass "doctor exits non-zero under bootstrap fallback"
else
    fail "doctor exits non-zero under bootstrap fallback  (got $DR_RC)"
fi

# ── Step 4: Run the migration ─────────────────────────────────────────────

step "4. Run nx catalog synthesize-log --force"
run_capture SYN_OUT uv run nx catalog synthesize-log --force
SYN_RC=$?
info "synthesize-log exit: $SYN_RC"

if [[ $SYN_RC -eq 0 ]]; then
    pass "synthesize-log --force succeeds"
else
    fail "synthesize-log --force failed (rc=$SYN_RC)"
    info "$SYN_OUT" | tail -5
fi

# events.jsonl must now be populated.
if [[ -s "$EVENTS_JSONL" ]]; then
    EVENT_LINE_COUNT=$(wc -l <"$EVENTS_JSONL")
    pass "events.jsonl populated ($EVENT_LINE_COUNT lines)"
else
    fail "events.jsonl still empty after synthesize-log --force"
fi

step "5. Doctor PASSes after migration"
run_capture DR2_OUT uv run nx catalog doctor --replay-equality
DR2_RC=$?
info "doctor exit: $DR2_RC"

if [[ $DR2_RC -eq 0 ]]; then
    pass "doctor --replay-equality PASS post-migration"
else
    fail "doctor --replay-equality fails post-migration  (rc=$DR2_RC)"
    info "$DR2_OUT" | tail -10
fi

# bootstrap-fallback warning should NOT be present now.
if [[ "$DR2_OUT" != *"bootstrap-fallback"* ]]; then
    pass "bootstrap-fallback warning gone after migration"
else
    fail "bootstrap-fallback warning still present after migration"
fi

# ── Step 6: Mutate post-migration ─────────────────────────────────────────

step "6. Mutate post-migration; doctor stays green"
run_capture IDX_C uv run nx index md "$SANDBOX/repo/gamma.md" --corpus sandbox || true
info "gamma indexed: $(echo "$IDX_C" | tail -3 | head -1)"

run_capture DR3_OUT uv run nx catalog doctor --replay-equality
DR3_RC=$?
if [[ $DR3_RC -eq 0 ]]; then
    pass "doctor still PASSes after a post-migration mutation"
else
    fail "doctor fails after a post-migration mutation (rc=$DR3_RC)"
    info "$DR3_OUT" | tail -10
fi

# ── Step 7: Rollback to legacy mode ───────────────────────────────────────

step "7. Rollback: NEXUS_EVENT_SOURCED=0 still reads catalog"
run_capture LIST_OUT env NEXUS_EVENT_SOURCED=0 uv run nx catalog list
if [[ -n "$LIST_OUT" ]]; then
    pass "nx catalog list works under explicit legacy opt-out"
else
    fail "nx catalog list returned empty under explicit legacy opt-out"
fi

# Should see at least 2 of our 3 sandbox docs in the listing.
SANDBOX_HITS=$(echo "$LIST_OUT" | grep -cE "alpha|beta|gamma" || echo 0)
if [[ "$SANDBOX_HITS" -ge 2 ]]; then
    pass "rollback list surfaces sandbox docs ($SANDBOX_HITS hits)"
else
    fail "rollback list missing sandbox docs (only $SANDBOX_HITS hits)"
fi

# ── Step 8: Concurrency smoke test ────────────────────────────────────────

step "8. Concurrency: parallel doctor + register"
# Two operations in parallel — doctor (read-only) + a fresh index.
mkdir -p "$SANDBOX/repo/parallel"
cat >"$SANDBOX/repo/parallel/concurrent.md" <<'EOF'
# Concurrent
Written during a parallel doctor run.
EOF

(uv run nx catalog doctor --replay-equality >"$SANDBOX/.cache/par-doctor.out" 2>&1) &
DOCTOR_PID=$!
(uv run nx index md "$SANDBOX/repo/parallel/concurrent.md" --corpus sandbox \
    >"$SANDBOX/.cache/par-index.out" 2>&1) &
INDEX_PID=$!

wait $DOCTOR_PID
DOC_RC=$?
wait $INDEX_PID
IDX_RC=$?

if [[ $DOC_RC -eq 0 && $IDX_RC -eq 0 ]]; then
    pass "parallel doctor + index both succeeded"
else
    fail "parallel doctor + index disagree (doctor=$DOC_RC index=$IDX_RC)"
fi

# events.jsonl must still be valid JSONL — no torn writes.
INVALID_EVENT_LINES=0
if [[ -s "$EVENTS_JSONL" ]]; then
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        if ! printf '%s\n' "$line" | python3 -c "import sys,json; json.loads(sys.stdin.read())" >/dev/null 2>&1; then
            INVALID_EVENT_LINES=$((INVALID_EVENT_LINES+1))
        fi
    done <"$EVENTS_JSONL"
fi
if [[ "$INVALID_EVENT_LINES" -eq 0 ]]; then
    pass "events.jsonl JSON-clean after concurrent writes"
else
    fail "events.jsonl has $INVALID_EVENT_LINES malformed line(s) after concurrent writes"
fi

# ── Step 9: Performance baseline ──────────────────────────────────────────

step "9. Performance baseline for doctor"
declare -a TIMES
for i in 1 2 3; do
    BEFORE=$(python3 -c "import time; print(time.monotonic())")
    uv run nx catalog doctor --replay-equality >/dev/null 2>&1 || true
    AFTER=$(python3 -c "import time; print(time.monotonic())")
    DELTA=$(python3 -c "print(f'{$AFTER - $BEFORE:.3f}')")
    info "doctor run $i: ${DELTA}s"
    TIMES+=("$DELTA")
done

# Report median (sort, pick middle).
MEDIAN=$(printf '%s\n' "${TIMES[@]}" | sort -n | sed -n '2p')
info "median doctor wall time: ${MEDIAN}s"

# Soft check — flag if median > 5s on the sandbox-sized catalog. This
# isn't a hard fail; it's a heads-up if _check_bootstrap_status added
# unexpected overhead.
if python3 -c "import sys; sys.exit(0 if $MEDIAN < 5.0 else 1)"; then
    pass "doctor median wall time under 5s threshold"
else
    fail "doctor median wall time exceeded 5s soft threshold (${MEDIAN}s)"
fi

# ── Summary ───────────────────────────────────────────────────────────────

step "Summary"
info "PASS: $PASS"
info "FAIL: $FAIL"
if (( FAIL > 0 )); then
    info "Failed cases:"
    for c in "${FAILED_CASES[@]}"; do
        info "  - $c"
    done
    exit 1
fi
info "All e2e migration validation steps passed."
exit 0
