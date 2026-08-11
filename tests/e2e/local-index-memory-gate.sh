#!/usr/bin/env bash
# nexus-97dp4 / nexus-b32rx / nexus-y9t08: LOCAL-INDEX MEMORY GATE — a memory
# gate whose corpus is deliberately sized to make the onnx-local upsert-chunk
# cap (src/nexus/db/http_vector_client.py _ONNX_LOCAL_UPSERT_CHUNK_CAP)
# actually BIND, and whose sampler kills the engine before a regression can
# wedge the machine the way the shipped-day incident did.
#
# Why this exists (2026-08-10, T2 nexus/assess-local-index-cluster-2026-08-10):
# the release shakedown fixture (tests/e2e/release-sandbox.sh:469-506, 36
# files) yields only ~92 chunks total against the shipped cap of 16 — the
# largest observed flush is 15. The cap NEVER BINDS on that corpus, so a
# gate built on it cannot detect a future cap raise (16 -> 64 -> 300)
# silently reintroducing the nexus-33hpq blowup (77.4 GB peak RSS, ~15 cores
# pegged, engine wedged, 3/3 reproduced). The gate that caught 33hpq did so
# only because the corpus happened to already be catastrophic at the time —
# luck, not coverage. This script closes that gap: it CALIBRATES its own
# corpus against the real chunker (nexus.chunker.chunk_file — pure client-
# side, no network, no engine) so the largest flush/page is GUARANTEED to
# exceed whatever cap is configured, and it asserts that non-vacuously
# (nexus-97dp4 ask 2) rather than hoping.
#
# It also answers the still-open nexus-b32rx / nexus-y9t08 question: 7.5.0's
# combined write moved the synchronous server-side embed onto a client whose
# HTTP timeout for that call regressed from 600s to 30s, with the retry
# ladder able to fire up to 8 uncancelled duplicate embeds per flush. This
# gate's JSON summary reports the retry counts from BOTH retry axes
# (manifest_write_transient_error_retry, refreshable_http_store.retry) next
# to peak RSS, so a run is direct evidence for or against the timeout
# hypothesis, not just the memory one.
#
# PRECEDENT: tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh Step 2/8
# solved "corpus that binds the cap" for a Linux CONTAINER using a single
# oversize-file trick (a file whose own chunk count exceeds the cap forces
# ChunkBatcher.add() to reject it as oversize-for-one-batch, routing it
# through the LEGACY per-file paged upload). This script's first version
# reused that trick, generalized to the bare host — and that reuse was
# itself D3 (nexus-97dp4 first-run finding): an oversize file ALWAYS routes
# to the legacy path, by construction (chunk_batcher.py:255-260), so it can
# only ever prove the legacy path binds the cap, never the COMBINED-WRITE
# path — a DIFFERENT code path from the one the nexus-33hpq incident
# actually ran, and `--corpus-scale` (more oversize files) cannot fix it.
#
# The combined-write path's cap can only bind by ACCUMULATING multiple
# under-cap files in ChunkBatcher's pending buffer until the running total
# reaches the cap EXACTLY (chunk_batcher.py:262-303 — the overflow guard is
# a strict `>`: exceeding the cap pre-flushes the OLD pending BELOW cap and
# starts fresh, discarding the accumulated progress). A uniform "many small
# files" corpus is NOT automatically sufficient: if the per-file chunk count
# doesn't evenly divide the cap, the running total cycles through the same
# partial sums forever and NEVER lands on the cap — empirically confirmed
# (50 files of 7 chunks each against cap=16 flushes at 14 chunks, 25 times,
# 0 cap-binds; see this repair's report). This script's corpus is instead
# GROUPS of (a) a few "bulk" files calibrated to ~cap/4 chunks each — real
# accumulation, several files needed — followed by (b) exactly the "trim"
# files needed to complete the remainder to the cap. A 1-chunk trim file
# added to a pending buffer of size V can NEVER overflow (V is always < cap
# — the buffer resets to 0 the instant it reaches cap — so V+1 <= cap
# always), so trim files always merge safely and climb the running total
# 1-by-1 until it lands on the cap EXACTLY. Ordering (bulk files before
# trim files within a group) is enforced by `git ls-files`' deterministic
# lexicographic traversal (indexer.py `_git_ls_files`, verified — not
# rglob/os.walk, whose ordering is not guaranteed) via `gNNN_bulk_*` <
# `gNNN_trim_*` filename prefixes. Both unit sizes are CALIBRATED against
# the live chunker (nexus.chunker.chunk_file — pure client-side, no
# network, no engine), never a hard-coded funcs-per-chunk ratio: the
# original oversize-file version's own hard-coded "40 funcs -> 21 chunks"
# figure did NOT reproduce on this tree (measured here: 40 funcs -> 10
# chunks — the chunker's actual behavior drifted from that comment,
# silently); trusting a stale magic number is exactly the class of bug this
# gate exists to avoid a repeat of.
#
# This script also adds a HARD RSS CEILING with immediate SIGKILL — the
# rehearsal gate only samples and reports; this gate protects the machine.
#
# ISOLATION (non-negotiable — see "Refuse to start" below): everything runs
# under a scratch NEXUS_CONFIG_DIR + a scratch HOME. Never touches
# ~/.config/nexus. Never passes --force to any nx command. Engine + PG are
# torn down on exit via a trap, success or failure.
#
# SIGKILL SAFETY: the RSS sampler and the ceiling-breach killer NEVER match
# a process by bare name ("nexus-service") — a live production engine under
# the real ~/.config/nexus could be running on this same box right now, and
# a name-only match could kill it. Every match is `pgrep -f` against the
# run's own unique, mktemp-derived binary PATH, and every kill re-verifies
# that path is present in the target's live command line immediately before
# signaling it (belt-and-suspenders — see _kill_if_isolated).
#
# CAP OVERRIDE MECHANISM: NX_ONNX_LOCAL_UPSERT_CHUNK_CAP (added to
# src/nexus/db/http_vector_client.py alongside this script, nexus-97dp4) —
# a real env-var-read-at-import mechanism, not a runtime sed. Unset leaves
# the constant byte-identical to before (16).
#
# TIMEOUT OVERRIDE: NO env mechanism exists for the combined-write embed
# timeout (src/nexus/catalog/http_catalog_client.py
# _COMBINED_WRITE_EMBED_TIMEOUT_S). That file was being concurrently edited
# by another agent implementing exactly this constant (nexus-y9t08, hardcoded
# 600.0s, no escape hatch) at the time this script was authored, and it is
# out of scope here to touch it. --timeout therefore only ACCEPTS the value
# this tree actually has compiled in (introspected at run time, never
# guessed) — passing a different value is a loud refusal, not a silent
# no-op and not a sed. See this script's --help and the report handed back
# with this deliverable for the full explanation.
#
# Usage:
#   tests/e2e/local-index-memory-gate.sh [--cap N] [--timeout S]
#       [--rss-ceiling GB] [--corpus-scale N]
#   tests/e2e/local-index-memory-gate.sh --self-test
#       Runs the sampler/verdict/log-parsing functions against synthetic
#       fixtures ONLY — no wheel build, no venv, no engine, no PG, no
#       network. Safe to run anywhere, any time.
#
# Exit codes: 0 PASSED, 1 FAILED, 2 KILLED (ceiling breach — a distinct,
# non-pass, non-ordinary-fail outcome; see the verdict line).
set -uo pipefail

(( BASH_VERSINFO[0] >= 4 )) || {
    echo "ERROR: bash >= 4 required (found $BASH_VERSION) — invoke with an explicit" >&2
    echo "       bash4+ (e.g. /opt/homebrew/bin/bash $0 ...), not the macOS system bash." >&2
    exit 1
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; }
note() { printf '       %s\n' "$*"; }
_die() { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# ── pure/portable helpers (exercised directly by --self-test) ──────────────

# Fixed-point KB->GB string, bash-only (no awk/bc dependency assumed).
_kb_to_gb_str() {
    local kb="$1" whole frac
    whole=$(( kb / 1024 / 1024 ))
    frac=$(( (kb * 100 / 1024 / 1024) % 100 ))
    printf '%d.%02d' "$whole" "$frac"
}

# Max chunk count seen across BOTH cap-governed event families in a client
# run log: the normal ChunkBatcher combined-write flush
# (chunk_flush_complete, chunks=N — src/nexus/chunk_batcher.py) and the
# legacy oversize-file paged upload (http_vector_upsert_chunks_request,
# count=N — src/nexus/db/http_vector_client.py upsert_chunks paging). The
# leading space before " count=" in the grep excludes the OTHER event's
# `distinct_chash_count=` field from matching.
_largest_flush_in_log() {
    local log="$1" a b
    a="$(grep -F 'chunk_flush_complete' "$log" 2>/dev/null | grep -oE 'chunks=[0-9]+' | cut -d= -f2 | sort -n | tail -1)"
    b="$(grep -F 'http_vector_upsert_chunks_request' "$log" 2>/dev/null | grep -oE ' count=[0-9]+' | tr -d ' ' | cut -d= -f2 | sort -n | tail -1)"
    a="${a:-0}"; b="${b:-0}"
    if [ "$a" -ge "$b" ] 2>/dev/null; then echo "$a"; else echo "$b"; fi
}

# Count of flush/page events whose chunk count reached or exceeded *cap* —
# the "bound M times" figure in the verdict line. A cap that binds exactly
# once could still be luck; a gate reporting the COUNT lets a reviewer judge
# how thoroughly the ceiling was exercised.
#
# D4 (nexus-97dp4 first-run finding): *family* selects which event family is
# counted — "combined" (chunk_flush_complete, the ChunkBatcher combined-
# write path this gate exists to exercise), "legacy" (the per-file paged
# upload's http_vector_upsert_chunks_request), or "both" (default — the sum,
# kept ONLY for this function's own log-scan use and the pre-existing
# self-test; the JSON emitter below NEVER uses "both" — see _emit_json's own
# comment for why flushes_at_cap and legacy_pages_at_cap must stay separate
# counters, not a summed one, to keep flushes_at_cap <= flushes true by
# construction).
_cap_bound_count_in_log() {
    local log="$1" cap="$2" family="${3:-both}" n=0 v
    if [ "$family" = "combined" ] || [ "$family" = "both" ]; then
        for v in $(grep -F 'chunk_flush_complete' "$log" 2>/dev/null | grep -oE 'chunks=[0-9]+' | cut -d= -f2); do
            [ "$v" -ge "$cap" ] 2>/dev/null && n=$((n + 1))
        done
    fi
    if [ "$family" = "legacy" ] || [ "$family" = "both" ]; then
        for v in $(grep -F 'http_vector_upsert_chunks_request' "$log" 2>/dev/null | grep -oE ' count=[0-9]+' | tr -d ' ' | cut -d= -f2); do
            [ "$v" -ge "$cap" ] 2>/dev/null && n=$((n + 1))
        done
    fi
    echo "$n"
}

# nexus-y9t08 evidence: counts from BOTH retry axes. Grep, not a structured
# parse — these are structlog key=value renderings (logging_setup.py
# KeyValueRenderer), and grep -c is the same technique warm-reindex-skip-
# gate.sh already uses for engine-log evidence.
_count_matches_in_log() {
    # NOT `grep -cF ... || echo 0`: `grep -c` on a genuine zero-match file
    # still prints "0" to stdout while exiting 1 (no match) — under `||`
    # that non-zero exit ALSO fires the fallback, double-printing "0\n0".
    # Capture first, default only when grep produced nothing at all
    # (missing/unreadable file).
    local n
    n="$(grep -cF "$2" "$1" 2>/dev/null)" || true
    echo "${n:-0}"
}

# RSS (KB) of one pid via `ps`, never /proc (this box has none) and never an
# HTTP endpoint (must survive an unresponsive engine). Empty string, not an
# error, when the pid is gone — callers treat that as "stopped contributing"
# not "probe broken".
_ps_rss_kb() {
    ps -o rss= -p "$1" 2>/dev/null | tr -d ' '
}

# All live pids whose FULL command line contains *marker* — see the header
# comment's SIGKILL SAFETY note for why this is a path substring, never a
# bare process name. macOS `pgrep` has NO `-c` flag (confirmed on this
# host: `pgrep -c -f ...` exits 2 with a usage error) — every call site
# below counts via `wc -l` / a for-loop, never `-c`.
_service_pids() {
    pgrep -f "$1" 2>/dev/null || true
}

_service_rss_kb_total() {
    local marker="$1" pid kb total=0 samples=0
    for pid in $(_service_pids "$marker"); do
        kb="$(_ps_rss_kb "$pid")"
        if [ -n "$kb" ]; then
            total=$((total + kb))
            samples=$((samples + 1))
        fi
    done
    if [ "$samples" -gt 0 ]; then echo "$total"; else echo ""; fi
}

# Re-verifies *marker* is actually present in the target's own command line
# (via `ps -o command=`) immediately before signaling — defense in depth on
# top of the pgrep match, given the blast radius of getting this wrong (a
# live production engine on the same box).
_kill_if_isolated() {
    local pid="$1" marker="$2" cmdline
    cmdline="$(ps -o command= -p "$pid" 2>/dev/null || true)"
    case "$cmdline" in
        *"$marker"*)
            kill -9 "$pid" 2>/dev/null || true
            return 0
            ;;
        *)
            note "REFUSED to kill pid $pid — its command line does not contain the isolated marker ($marker); this is the safety check working, not a bug"
            return 1
            ;;
    esac
}

# Pure verdict decision — no I/O, so --self-test can drive it directly with
# synthetic numbers. Echoes "<STATUS>|<reason>" on one line; STATUS is
# exactly one of PASSED / FAILED / KILLED.
_compute_verdict() {
    local killed="$1" index_rc="$2" peak_kb="$3" ceiling_kb="$4" \
        configured_cap="$5" largest_flush="$6" kill_elapsed_s="$7"
    if [ -z "$ceiling_kb" ]; then
        echo "FAILED|UNMEASURED: no RSS ceiling was resolved — cannot judge peak RSS against a ceiling that does not exist"
        return
    fi
    if [ "$killed" = 1 ]; then
        echo "KILLED|RSS exceeded $(_kb_to_gb_str "$ceiling_kb") GB at T+${kill_elapsed_s}s (peak $(_kb_to_gb_str "$peak_kb") GB observed before the kill)"
        return
    fi
    if [ -z "$configured_cap" ] || [ -z "$largest_flush" ]; then
        echo "FAILED|UNMEASURED: could not read the configured cap and/or parse the run log — this run proves NOTHING about the cap binding (nexus-97dp4 class)"
        return
    fi
    if [ "$largest_flush" -lt "$configured_cap" ] 2>/dev/null; then
        echo "FAILED|the cap NEVER BOUND: largest observed flush/page = ${largest_flush} chunks, configured cap is ${configured_cap} — the corpus is too small to be a memory gate (grow --corpus-scale)"
        return
    fi
    if [ "$index_rc" != 0 ]; then
        echo "FAILED|nx index repo exited ${index_rc}"
        return
    fi
    if [ -z "$peak_kb" ] || [ "$peak_kb" -le 0 ] 2>/dev/null; then
        echo "FAILED|RSS sampler took zero usable samples — the peak-RSS assertion is UNMEASURED, not clean"
        return
    fi
    if [ "$peak_kb" -gt "$ceiling_kb" ] 2>/dev/null; then
        echo "FAILED|peak RSS $(_kb_to_gb_str "$peak_kb") GB exceeded the ceiling $(_kb_to_gb_str "$ceiling_kb") GB but the sampler did not catch it in time to kill — treat this as a near-miss, tighten the sampler interval"
        return
    fi
    echo "PASSED|peak $(_kb_to_gb_str "$peak_kb") GB (ceiling $(_kb_to_gb_str "$ceiling_kb") GB, cap ${configured_cap} bound $8 times)"
}

_emit_json() {
    # $1 peak_gb(str) $2 cap $3 timeout $4 flushes $5 flushes_at_cap
    # $6 legacy_pages_at_cap $7 killed(0/1) $8 wall_s $9 manifest_retries
    # $10 refreshable_retries
    #
    # D4 (nexus-97dp4 first-run finding): "flushes" and "flushes_at_cap" are
    # now the SAME event family (chunk_flush_complete — the ChunkBatcher
    # combined-write path this gate exists to exercise): flushes_at_cap is a
    # same-family SUBSET count of flushes, so flushes_at_cap <= flushes
    # holds BY CONSTRUCTION, not by convention. The legacy per-file paged-
    # upload family (http_vector_upsert_chunks_request) that ALSO reached or
    # exceeded the cap is reported SEPARATELY as legacy_pages_at_cap — never
    # folded into flushes_at_cap, which is exactly the mixing that let the
    # old two-family sum exceed "flushes" in the first place.
    printf '{"peak_gb":%s,"cap":%s,"timeout_s":%s,"flushes":%s,"flushes_at_cap":%s,"legacy_pages_at_cap":%s,"killed":%s,"wall_s":%s,"manifest_write_retries":%s,"refreshable_retries":%s}\n' \
        "$1" "$2" "$3" "$4" "$5" "$6" "$7" "$8" "$9" "${10}"
}

# ── --self-test: pure-function checks against synthetic fixtures ───────────
# No wheel build, no venv, no engine, no PG, no network — safe anywhere.
_self_test() {
    local failures=0
    local t
    t="$(mktemp -d /tmp/local-index-memory-gate-selftest-XXXXXX)"
    trap 'rm -rf "$t"' RETURN

    _assert_eq() {
        if [ "$2" = "$3" ]; then
            ok "$1"
        else
            bad "$1 — expected [$3] got [$2]"
            failures=$((failures + 1))
        fi
    }

    say "self-test: _kb_to_gb_str"
    _assert_eq "0 KB -> 0.00"        "$(_kb_to_gb_str 0)"        "0.00"
    _assert_eq "1 GB exactly"        "$(_kb_to_gb_str 1048576)"  "1.00"
    _assert_eq "3.03 GB (nexus-33hpq measured figure)" \
        "$(_kb_to_gb_str 3176038)" "3.02"

    say "self-test: _largest_flush_in_log"
    cat > "$t/fake1.log" <<'EOF'
event=chunk_flush_complete chunks=12 collection=code__x
event=chunk_flush_complete chunks=16 collection=code__x distinct_chash_count=9
event=some_other_line count=999 chunks=1
event=http_vector_upsert_chunks_request path=/v1/vectors/upsert-chunks count=16 collection=code__x
EOF
    _assert_eq "max across both families, decoy fields ignored" \
        "$(_largest_flush_in_log "$t/fake1.log")" "16"

    cat > "$t/fake2.log" <<'EOF'
event=chunk_flush_complete chunks=3 collection=docs__x
EOF
    _assert_eq "below-cap log reports its true (small) max" \
        "$(_largest_flush_in_log "$t/fake2.log")" "3"

    _assert_eq "missing log file -> 0, not an error" \
        "$(_largest_flush_in_log "$t/does-not-exist.log")" "0"

    say "self-test: _cap_bound_count_in_log"
    _assert_eq "two flushes >= cap=16 in fake1.log (family=both, default)" \
        "$(_cap_bound_count_in_log "$t/fake1.log" 16)" "2"
    _assert_eq "zero flushes >= cap=16 in fake2.log (the 97dp4 vacuous case)" \
        "$(_cap_bound_count_in_log "$t/fake2.log" 16)" "0"
    _assert_eq "D4: family=combined counts ONLY chunk_flush_complete in fake1.log" \
        "$(_cap_bound_count_in_log "$t/fake1.log" 16 combined)" "1"
    _assert_eq "D4: family=legacy counts ONLY http_vector_upsert_chunks_request in fake1.log" \
        "$(_cap_bound_count_in_log "$t/fake1.log" 16 legacy)" "1"
    _assert_eq "D4: family=both still sums combined+legacy (backward compatible)" \
        "$(_cap_bound_count_in_log "$t/fake1.log" 16 both)" "2"

    say "self-test: _count_matches_in_log"
    cat > "$t/fake3.log" <<'EOF'
event=manifest_write_transient_error_retry attempt=1
event=manifest_write_transient_error_retry attempt=2
event=refreshable_http_store.retry endpoint=/v1/catalog
EOF
    _assert_eq "manifest retry count" \
        "$(_count_matches_in_log "$t/fake3.log" manifest_write_transient_error_retry)" "2"
    _assert_eq "refreshable retry count" \
        "$(_count_matches_in_log "$t/fake3.log" refreshable_http_store.retry)" "1"
    _assert_eq "zero retries -> 0, not an error" \
        "$(_count_matches_in_log "$t/fake2.log" manifest_write_transient_error_retry)" "0"

    say "self-test: _service_pids / _service_rss_kb_total against a marker that matches nothing"
    # Real pgrep call (safe: the marker is a random, self-test-only token
    # nothing on the box can match), not a stub — proves the zero-samples
    # path without faking `ps`. Deliberately no dependency on uv/python
    # here: --self-test must run with nothing but bash + pgrep + ps.
    local nomatch rss
    nomatch="nx-memgate-selftest-no-such-marker-$$-$RANDOM-$RANDOM"
    rss="$(_service_rss_kb_total "$nomatch")"
    _assert_eq "no matching process -> empty (zero samples), not an error" "$rss" ""

    say "self-test: _service_pids / _service_rss_kb_total against THIS shell's own pid"
    # A real, safe, side-effect-free positive case: mark this very shell
    # process's command line is findable by its own pid, so RSS must be > 0.
    local self_kb
    self_kb="$(_ps_rss_kb "$$")"
    if [ -n "$self_kb" ] && [ "$self_kb" -gt 0 ] 2>/dev/null; then
        ok "_ps_rss_kb resolves a real RSS for a real live pid (self, ${self_kb} KB)"
    else
        bad "_ps_rss_kb returned nothing for this shell's own pid — ps invocation is broken on this host"
        failures=$((failures + 1))
    fi

    say "self-test: _kill_if_isolated refuses a pid whose command line lacks the marker"
    if _kill_if_isolated "$$" "nx-memgate-marker-that-cannot-possibly-appear-in-this-shells-argv"; then
        bad "_kill_if_isolated killed (or claimed to kill) a process that does NOT carry the marker — SAFETY REGRESSION"
        failures=$((failures + 1))
    else
        ok "_kill_if_isolated correctly refused (this shell's argv does not contain the marker)"
    fi

    say "self-test: _compute_verdict — all six shapes"
    local v
    v="$(_compute_verdict 0 0 3176038 20971520 16 16 0 3)"
    case "$v" in PASSED\|*) ok "happy path -> PASSED ($v)";; *) bad "happy path -> $v"; failures=$((failures+1));; esac

    v="$(_compute_verdict 1 0 25165824 20971520 16 16 47 0)"
    case "$v" in KILLED\|*) ok "ceiling breach -> KILLED ($v)";; *) bad "ceiling breach -> $v"; failures=$((failures+1));; esac

    v="$(_compute_verdict 0 0 3145728 20971520 16 15 0 0)"
    case "$v" in FAILED\|*cap*never*bound*|FAILED\|*NEVER\ BOUND*) ok "cap never bound (97dp4 non-vacuity) -> FAILED ($v)";; *) bad "cap-never-bound case -> $v"; failures=$((failures+1));; esac

    v="$(_compute_verdict 0 1 3145728 20971520 16 20 0 1)"
    case "$v" in FAILED\|*exited\ 1*) ok "nonzero index exit -> FAILED ($v)";; *) bad "nonzero-exit case -> $v"; failures=$((failures+1));; esac

    v="$(_compute_verdict 0 0 0 20971520 16 20 0 1)"
    case "$v" in FAILED\|*zero\ usable\ samples*) ok "zero RSS samples -> FAILED, UNMEASURED framing ($v)";; *) bad "zero-samples case -> $v"; failures=$((failures+1));; esac

    v="$(_compute_verdict 0 0 3145728 20971520 "" 20 0 1)"
    case "$v" in FAILED\|*UNMEASURED*) ok "missing configured cap -> FAILED, UNMEASURED ($v)";; *) bad "missing-cap case -> $v"; failures=$((failures+1));; esac

    v="$(_compute_verdict 0 0 3145728 "" 16 20 0 1)"
    case "$v" in FAILED\|*UNMEASURED*) ok "missing ceiling -> FAILED, UNMEASURED ($v)";; *) bad "missing-ceiling case -> $v"; failures=$((failures+1));; esac

    say "self-test: _emit_json produces one parseable JSON line"
    local json
    # args: peak_gb cap timeout flushes flushes_at_cap legacy_pages_at_cap killed wall_s manifest_retries refreshable_retries
    json="$(_emit_json "3.03" 16 600.0 8 2 0 0 187 0 0)"
    printf '%s\n' "$json"
    if command -v python3 >/dev/null 2>&1; then
        if printf '%s' "$json" | python3 -c 'import json,sys
d=json.load(sys.stdin)
assert d["peak_gb"]==3.03
assert d["cap"]==16
assert d["killed"]==0
assert d["flushes"]==8
assert d["flushes_at_cap"]==2
assert d["legacy_pages_at_cap"]==0
assert d["flushes_at_cap"] <= d["flushes"], "D4: flushes_at_cap must never exceed flushes"' 2>/dev/null; then
            ok "JSON line parses and round-trips the fields it claims, D4 coherence holds (flushes_at_cap <= flushes)"
        else
            bad "JSON line failed to parse, a field did not round-trip, or D4 coherence was violated"
            failures=$((failures + 1))
        fi
    else
        note "no python3 on PATH — skipped structural JSON validation (string shape only)"
    fi

    say "self-test: bash -n on this script itself"
    if bash -n "${BASH_SOURCE[0]}"; then
        ok "bash -n clean"
    else
        bad "bash -n reported a syntax error"
        failures=$((failures + 1))
    fi

    say "RESULT"
    if [ "$failures" -eq 0 ]; then
        printf '\033[32mSELF-TEST PASSED\033[0m — sampler/verdict/log-parsing logic verified against synthetic fixtures (no engine, no PG, no network)\n'
        return 0
    else
        printf '\033[31mSELF-TEST FAILED\033[0m — %d check(s)\n' "$failures"
        return 1
    fi
}

# ── argument parsing ────────────────────────────────────────────────────────
CAP=16
TIMEOUT_S=600
RSS_CEILING_GB=20
CORPUS_SCALE=1
SELF_TEST=0

_usage() {
    cat <<EOF
Usage: $0 [--cap N] [--timeout S] [--rss-ceiling GB] [--corpus-scale N]
       $0 --self-test

  --cap N            override the onnx-local per-flush chunk cap via the
                      real NX_ONNX_LOCAL_UPSERT_CHUNK_CAP env mechanism
                      (default: 16, the shipped nexus-33hpq value)
  --timeout S         the combined-write embed HTTP timeout. NO override
                      mechanism exists for this yet (nexus-y9t08 shipped it
                      as a hardcoded 600.0s constant, no env escape hatch,
                      in a file this script deliberately does not touch —
                      see the header comment). Passing anything other than
                      the tree's actual compiled-in value is a loud refusal,
                      never a silent no-op. (default: 600, matches the
                      shipped value at authoring time)
  --rss-ceiling GB    SIGKILL the isolated engine if its RSS exceeds this
                      (default: 20)
  --corpus-scale N    multiplier on the number of GUARANTEED cap-binding
                      groups generated on the combined-write path (default:
                      1 -> 3 groups, each independently calibrated LIVE
                      against this tree's real chunker to land EXACTLY on
                      the effective cap; see the header comment for why
                      "many small files" alone does not guarantee this)
  --self-test         run the sampler/verdict/log-parsing unit checks
                      against synthetic fixtures ONLY — no provisioning,
                      no engine, no PG, no network. Safe anywhere.

Exit codes: 0 PASSED, 1 FAILED, 2 KILLED.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --cap) CAP="${2:?--cap requires a value}"; shift 2 ;;
        --timeout) TIMEOUT_S="${2:?--timeout requires a value}"; shift 2 ;;
        --rss-ceiling) RSS_CEILING_GB="${2:?--rss-ceiling requires a value}"; shift 2 ;;
        --corpus-scale) CORPUS_SCALE="${2:?--corpus-scale requires a value}"; shift 2 ;;
        --self-test) SELF_TEST=1; shift ;;
        -h|--help) _usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; _usage; exit 1 ;;
    esac
done

if [ "$SELF_TEST" = 1 ]; then
    _self_test
    exit $?
fi

case "$CAP" in ''|*[!0-9]*) _die "--cap must be a positive integer, got: $CAP" ;; esac
case "$RSS_CEILING_GB" in ''|*[!0-9]*) _die "--rss-ceiling must be a positive integer (GB), got: $RSS_CEILING_GB" ;; esac
case "$CORPUS_SCALE" in ''|*[!0-9]*) _die "--corpus-scale must be a positive integer, got: $CORPUS_SCALE" ;; esac
[ "$CORPUS_SCALE" -ge 1 ] 2>/dev/null || _die "--corpus-scale must be >= 1"

# ── isolation — refuse to start before touching anything ───────────────────
REAL_HOME="$HOME"
REAL_CONFIG_DIR="$REAL_HOME/.config/nexus"

_resolve_path() {
    # Portable "realpath -m" (macOS ships neither GNU readlink -f nor
    # realpath by default): resolves via python3, which every dev box here
    # has. Does NOT require the path to exist.
    python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1" 2>/dev/null || echo "$1"
}

if [ -n "${NEXUS_CONFIG_DIR:-}" ]; then
    if [ "$(_resolve_path "$NEXUS_CONFIG_DIR")" = "$(_resolve_path "$REAL_CONFIG_DIR")" ]; then
        _die "NEXUS_CONFIG_DIR is already set to the REAL config dir ($NEXUS_CONFIG_DIR) — refusing to start. This gate must never touch the live install. Unset NEXUS_CONFIG_DIR and re-run; the script provisions its own isolated one."
    fi
fi

WORK="$(mktemp -d /tmp/local-index-memory-gate-XXXXXX)"
HOME_DIR="$WORK/home"
VENV="$WORK/venv"
LOGS="$WORK/logs"
CORPUS_DIR="$WORK/corpus"
ISOLATED_CONFIG_DIR="$HOME_DIR/.config/nexus"
mkdir -p "$HOME_DIR" "$LOGS" "$CORPUS_DIR"

if [ "$(_resolve_path "$ISOLATED_CONFIG_DIR")" = "$(_resolve_path "$REAL_CONFIG_DIR")" ]; then
    _die "computed isolated config dir ($ISOLATED_CONFIG_DIR) resolved to the SAME path as the real config dir ($REAL_CONFIG_DIR) — refusing to start. This should be structurally impossible (mktemp collision); do not proceed."
fi
note "isolated NEXUS_CONFIG_DIR: $ISOLATED_CONFIG_DIR (real config dir untouched: $REAL_CONFIG_DIR)"

GATE_STATUS=""
GATE_RC=1
SERVICE_MARKER="$ISOLATED_CONFIG_DIR/service/nexus-service"

cleanup() {
    # Best-effort — a wedged engine may not answer the CLI, so this ALSO
    # falls back to a direct kill of anything still matching our own
    # isolated marker, re-verified per _kill_if_isolated before signaling.
    _nx daemon service stop --with-pg >/dev/null 2>&1 || true
    local pid
    for pid in $(_service_pids "$SERVICE_MARKER"); do
        _kill_if_isolated "$pid" "$SERVICE_MARKER" || true
    done
    if [ "$GATE_STATUS" = "PASSED" ]; then
        rm -rf "$WORK"
    else
        echo "EVIDENCE PRESERVED: $WORK (logs: $LOGS)" >&2
    fi
}
trap cleanup EXIT

_nx() {
    env \
        HOME="$HOME_DIR" \
        NEXUS_CONFIG_DIR="$ISOLATED_CONFIG_DIR" \
        PATH="$VENV/bin:/usr/bin:/bin" \
        TERM="${TERM:-dumb}" \
        NX_LOCAL=1 \
        NX_ONNX_LOCAL_UPSERT_CHUNK_CAP="$CAP" \
        ${HTTPS_PROXY:+HTTPS_PROXY="$HTTPS_PROXY"} \
        ${HTTP_PROXY:+HTTP_PROXY="$HTTP_PROXY"} \
        "$VENV/bin/nx" "$@"
}

# ── build + install (self-provisioning; feedback_gates_scripted_not_ambient) ─
say "1/6 build the wheel under test"
( cd "$REPO_ROOT" && uv build --wheel -o "$WORK/dist" ) >"$LOGS/build.log" 2>&1 \
    || _die "wheel build failed (see $LOGS/build.log)"
WHEEL="$(ls "$WORK"/dist/conexus-*.whl)"
note "$WHEEL"

say "2/6 virgin venv + install"
uv venv "$VENV" >"$LOGS/venv.log" 2>&1 || _die "venv create failed"
uv pip install --python "$VENV/bin/python" "$WHEEL" >>"$LOGS/venv.log" 2>&1 \
    || _die "wheel install failed (see $LOGS/venv.log)"

# ── verify the --timeout ask against what the tree ACTUALLY has ────────────
EFFECTIVE_TIMEOUT="$("$VENV/bin/python" -c '
try:
    from nexus.catalog.http_catalog_client import _COMBINED_WRITE_EMBED_TIMEOUT_S as t
    print(t)
except Exception:
    print("")
' 2>/dev/null)"
if [ -n "$EFFECTIVE_TIMEOUT" ]; then
    note "combined-write embed timeout compiled into this tree: ${EFFECTIVE_TIMEOUT}s (no override mechanism — nexus-y9t08)"
    if [ "$TIMEOUT_S" != "$EFFECTIVE_TIMEOUT" ] && [ "$TIMEOUT_S" != "600" ]; then
        _die "--timeout $TIMEOUT_S requested, but this tree has NO env override for the combined-write timeout — it is hardcoded to ${EFFECTIVE_TIMEOUT}s in src/nexus/catalog/http_catalog_client.py (nexus-y9t08). Refusing rather than silently ignoring the flag or sedding the source. Re-run with --timeout ${EFFECTIVE_TIMEOUT}, or omit the flag."
    fi
else
    note "could not read _COMBINED_WRITE_EMBED_TIMEOUT_S from the installed wheel (tree predates nexus-y9t08, or the constant was renamed) — --timeout is UNMEASURED against this build"
    if [ "$TIMEOUT_S" != "600" ]; then
        _die "--timeout $TIMEOUT_S requested but this tree exposes no readable combined-write timeout constant at all — cannot honor or verify the request. Omit --timeout."
    fi
fi

say "3/6 nx init (local: engine + PG + bge-768)"
# D5 (nexus-97dp4 first-run finding): --no-autostart takes precedence over
# --yes (init.py:837-843) and declines the autostart registration
# deterministically regardless of interactivity — no TTY prompt to hang on,
# and no autostart unit left pointing at this run's /tmp install for
# cleanup to later delete out from under it. </dev/null is defense in depth
# against any OTHER unexpected prompt.
_nx init --no-autostart >"$LOGS/init.log" 2>&1 </dev/null \
    || { tail -30 "$LOGS/init.log" >&2; _die "nx init failed"; }

# D-POLL + D1 (nexus-97dp4 first-run finding — MUST land together, see the
# report handed back with this repair): the old poll piped nx's own prose
# output into `grep -qiE ... "running"`. Under `set -o pipefail` (line ~87)
# that pipeline can NEVER return 0 — grep -q exits at its FIRST match while
# nx (click.echo, line-buffered) is still mid-write, so nx gets SIGPIPE and
# exits 1, and pipefail promotes nx's 1 over grep's 0. Every run died here,
# even against a live, healthy service. Fixing ONLY the pipefail accident
# would have been WORSE: the dead-service message is literally "...is the
# service running?" — it matches the SAME pattern via the word "running",
# so a naive fix would have made a DEAD service read as healthy (the exact
# false-clean class this gate exists to catch).
#
# The replacement never greps prose and never pipes nx's own stdout into
# anything that can close early: `nx daemon service status --json` is
# captured via command substitution (no pipe -> no pipefail exposure ever),
# its own exit code is checked directly (0 == a lease was found, per the
# command's own docstring: "Exits non-zero when no live lease is found"),
# and the JSON body's `health` field — populated from a real GET /health
# probe, "ok" | "db-down" | "unreachable" (daemon.py:_probe_health) — is the
# ONLY thing ever treated as "healthy". D2: every attempt's raw output is
# preserved in $STATUS_LOG and tailed on failure, never discarded.
STATUS_LOG="$LOGS/health-poll.log"
: > "$STATUS_LOG"
healthy=0
attempt=0
for _ in $(seq 1 30); do
    attempt=$((attempt + 1))
    STATUS_JSON="$(_nx daemon service status --json 2>>"$STATUS_LOG")"
    STATUS_RC=$?
    { printf -- '--- attempt %s (rc=%s) ---\n' "$attempt" "$STATUS_RC"; printf '%s\n' "$STATUS_JSON"; } >> "$STATUS_LOG"
    if [ "$STATUS_RC" = 0 ]; then
        HEALTH="$(printf '%s' "$STATUS_JSON" | "$VENV/bin/python" -c '
import json, sys
try:
    print(json.load(sys.stdin).get("health", ""))
except Exception:
    print("")
')"
        if [ "$HEALTH" = "ok" ]; then
            healthy=1
            break
        fi
    fi
    sleep 2
done
if [ "$healthy" != 1 ]; then
    note "service health poll never observed health==ok after $attempt attempt(s) — see $STATUS_LOG"
    tail -40 "$STATUS_LOG" >&2
    _die "service never became healthy"
fi
note "service health verified via 'nx daemon service status --json' -> health=ok (attempt $attempt/30)"

EFFECTIVE_CAP="$("$VENV/bin/python" -c '
import os
os.environ["NX_ONNX_LOCAL_UPSERT_CHUNK_CAP"] = "'"$CAP"'"
from importlib import reload
import nexus.db.http_vector_client as m
reload(m)
print(m._ONNX_LOCAL_UPSERT_CHUNK_CAP)
' 2>/dev/null)"
[ "$EFFECTIVE_CAP" = "$CAP" ] || _die "requested cap $CAP but the env override did not take effect (read back: ${EFFECTIVE_CAP:-<none>}) — the NX_ONNX_LOCAL_UPSERT_CHUNK_CAP mechanism is broken, not just this run"
note "effective onnx-local upsert-chunk cap, VERIFIED (not assumed): $EFFECTIVE_CAP"

# ── corpus generation — calibrated LIVE against this tree's real chunker ───
say "4/6 generate a corpus calibrated to actually bind cap=$EFFECTIVE_CAP on the ChunkBatcher COMBINED-WRITE path (corpus-scale=$CORPUS_SCALE)"
# D3 (nexus-97dp4 first-run finding — full mechanism in the header comment
# and the repair report). Every generated file's content is tagged unique
# (RDR-108/T3 content addressing: identical chunk text in the same
# collection collapses to one T3 row, which would silently defeat the
# accumulation this corpus depends on).

_measure_chunks() {
    "$VENV/bin/python" -c "
from pathlib import Path
from nexus.chunker import chunk_file
p = Path('$1')
print(len(chunk_file(p, p.read_text())))
"
}

_write_filler() {
    # $1 path  $2 nfuncs  $3 tag (uniqueness marker only — verified NOT to
    # change the chunk count, since it lands in a leading comment line
    # outside any function body)
    "$VENV/bin/python" - "$1" "$2" "$3" <<'PYEOF'
import sys
from pathlib import Path


def make_func(i: int) -> str:
    body = "\n".join(f"    v{j} = {j} * {i} + {j % 7}  # filler" for j in range(12))
    return (
        f"def widget_function_{i:04d}(x: int, y: int = {i}) -> int:\n"
        f'    """Deterministic local-index-memory-gate filler function {i}."""\n'
        f"{body}\n"
        f"    return v0 + x + y\n"
    )


path, nfuncs, tag = sys.argv[1], int(sys.argv[2]), sys.argv[3]
content = f"# Generated local-index-memory-gate fixture — deterministic filler. tag={tag}\n\n"
content += "\n\n".join(make_func(i) for i in range(nfuncs)) + "\n"
Path(path).write_text(content)
PYEOF
}

# Calibrate a "bulk" per-file chunk count around cap/4 — comfortably under
# cap, several files genuinely needed to approach it. Never guessed:
# measured LIVE (see the header comment on why a hard-coded funcs-per-chunk
# ratio already drifted once on this tree).
TARGET_BULK_CHUNKS=$(( EFFECTIVE_CAP / 4 ))
[ "$TARGET_BULK_CHUNKS" -ge 1 ] || TARGET_BULK_CHUNKS=1
BULK_FUNCS=2
BULK_CHUNKS=0
ITER=0
while :; do
    ITER=$((ITER + 1))
    [ "$ITER" -le 14 ] || _die "bulk-file calibration did not reach target=$TARGET_BULK_CHUNKS chunks (cap=$EFFECTIVE_CAP) after 14 doublings (last: $BULK_FUNCS funcs -> $BULK_CHUNKS chunks) — the chunker's funcs-per-chunk ratio changed shape enough that this generator needs a new starting point"
    _write_filler "$CORPUS_DIR/.calib_bulk.py" "$BULK_FUNCS" calib-bulk
    BULK_CHUNKS="$(_measure_chunks "$CORPUS_DIR/.calib_bulk.py")"
    [ "$BULK_CHUNKS" -ge "$TARGET_BULK_CHUNKS" ] 2>/dev/null && break
    BULK_FUNCS=$((BULK_FUNCS * 2))
done
rm -f "$CORPUS_DIR/.calib_bulk.py"
[ "$BULK_CHUNKS" -lt "$EFFECTIVE_CAP" ] 2>/dev/null \
    || _die "bulk calibration overshot the cap: $BULK_FUNCS funcs -> $BULK_CHUNKS chunks >= cap=$EFFECTIVE_CAP while only targeting $TARGET_BULK_CHUNKS — cap is too small relative to the chunker's granularity for a bulk component; try a larger --cap"
note "bulk unit calibrated LIVE: $BULK_FUNCS funcs -> $BULK_CHUNKS chunks/file (target ~$TARGET_BULK_CHUNKS, < cap $EFFECTIVE_CAP)"

# Calibrate the trim unit: smallest funcs count producing EXACTLY one
# chunk — the exact-landing primitive the whole binding guarantee rests on
# (see the header comment's proof sketch).
TRIM_FUNCS=1
TRIM_CHUNKS=""
ITER=0
while :; do
    ITER=$((ITER + 1))
    [ "$ITER" -le 8 ] || _die "trim-file calibration did not find a funcs count producing exactly 1 chunk after 8 iterations (last: $TRIM_FUNCS funcs -> ${TRIM_CHUNKS:-?} chunks) — the chunker's grouping changed shape enough that this generator needs a new starting point"
    _write_filler "$CORPUS_DIR/.calib_trim.py" "$TRIM_FUNCS" calib-trim
    TRIM_CHUNKS="$(_measure_chunks "$CORPUS_DIR/.calib_trim.py")"
    [ "$TRIM_CHUNKS" = 1 ] && break
    TRIM_FUNCS=$((TRIM_FUNCS + 1))
done
rm -f "$CORPUS_DIR/.calib_trim.py"
note "trim unit calibrated LIVE: $TRIM_FUNCS funcs -> exactly 1 chunk/file (the exact-landing primitive)"

# Per group: enough bulk files to approach the cap without exceeding it
# (K = floor(cap / bulk_chunks)), then exactly the trim files needed to
# complete the remainder to the cap (R = cap - K*bulk_chunks). Group/file
# naming (gNNN_bulk_* before gNNN_trim_*) relies on git ls-files'
# deterministic lexicographic traversal (indexer.py _git_ls_files —
# verified, not rglob/os.walk) so bulk files are always staged before the
# trims that complete them. --corpus-scale multiplies GROUPS (independent
# guaranteed landings); default 3 groups so the cap binds more than once,
# not as a single-flush fluke.
BULK_PER_GROUP=$(( EFFECTIVE_CAP / BULK_CHUNKS ))
[ "$BULK_PER_GROUP" -ge 1 ] 2>/dev/null || BULK_PER_GROUP=0
TRIM_PER_GROUP=$(( EFFECTIVE_CAP - BULK_PER_GROUP * BULK_CHUNKS ))
# NOT named GROUPS: that is a bash BUILT-IN special array variable (the
# current user's group-membership list, `declare -p GROUPS`) — assigning to
# it is silently absorbed by bash's own dynamic-variable machinery rather
# than producing the intended integer, so --corpus-scale would have had NO
# real effect and the group count would vary by machine/user instead of by
# request (caught empirically: `GROUPS=$(( 3 * CORPUS_SCALE ))` with
# CORPUS_SCALE=1 read back as 20 on this box, not 3 — this box's first real
# group id). NUM_GROUPS avoids the collision.
NUM_GROUPS=$(( 3 * CORPUS_SCALE ))
note "corpus plan: $NUM_GROUPS group(s) x ($BULK_PER_GROUP bulk file(s) @ $BULK_CHUNKS chunks + $TRIM_PER_GROUP trim file(s) @ 1 chunk) = exactly $EFFECTIVE_CAP chunks/group, guaranteeing $NUM_GROUPS cap landing(s) on the combined-write path"

g=1
while [ "$g" -le "$NUM_GROUPS" ]; do
    gp="$(printf 'g%04d' "$g")"
    i=1
    while [ "$i" -le "$BULK_PER_GROUP" ]; do
        _write_filler "$CORPUS_DIR/${gp}_bulk_$(printf '%03d' "$i").py" "$BULK_FUNCS" "${gp}-bulk-$i"
        i=$((i + 1))
    done
    i=1
    while [ "$i" -le "$TRIM_PER_GROUP" ]; do
        _write_filler "$CORPUS_DIR/${gp}_trim_$(printf '%03d' "$i").py" "$TRIM_FUNCS" "${gp}-trim-$i"
        i=$((i + 1))
    done
    g=$((g + 1))
done

( cd "$CORPUS_DIR" && git init -q && git add -A \
    && git -c user.email=memgate@e2e.local -c user.name=local-index-memory-gate commit -qm corpus ) \
    >"$LOGS/corpus-git.log" 2>&1 || { cat "$LOGS/corpus-git.log" >&2; _die "corpus fixture repo init/commit failed"; }

FILE_COUNT="$(find "$CORPUS_DIR" -type f -name '*.py' | wc -l | tr -d ' ')"
note "corpus: $FILE_COUNT python files ($NUM_GROUPS group(s), each guaranteed to bind cap=$EFFECTIVE_CAP exactly once on the combined-write path)"

# ── indexing + live RSS sampling with hard ceiling ──────────────────────────
say "5/6 nx index repo — live RSS sampling (~1s), hard ceiling $RSS_CEILING_GB GB"

CEILING_KB=$(( RSS_CEILING_GB * 1024 * 1024 ))
MARKER_FILE="$WORK/.log-marker"
touch "$MARKER_FILE"
INDEX_STDOUT="$LOGS/index-stdout.log"

env HOME="$HOME_DIR" NEXUS_CONFIG_DIR="$ISOLATED_CONFIG_DIR" PATH="$VENV/bin:/usr/bin:/bin" \
    TERM="${TERM:-dumb}" NX_LOCAL=1 NX_ONNX_LOCAL_UPSERT_CHUNK_CAP="$CAP" \
    "$VENV/bin/nx" index repo "$CORPUS_DIR" >"$INDEX_STDOUT" 2>&1 &
INDEX_PID=$!

PEAK_KB=0
SAMPLES=0
KILLED=0
KILL_ELAPSED=0
RUN_LOG=""
T0="$(date +%s)"
LAST_PROGRESS="$T0"
# Internal safety net, not one of the required flags — a run that neither
# completes nor breaches the ceiling must still end in bounded time.
# Override for a one-off via NX_MEMGATE_DEADLINE_S.
HARD_DEADLINE_S="${NX_MEMGATE_DEADLINE_S:-1200}"

while true; do
    if [ -z "$RUN_LOG" ]; then
        RUN_LOG="$(find "$ISOLATED_CONFIG_DIR/logs" -maxdepth 1 -name 'index-*.log' -newer "$MARKER_FILE" 2>/dev/null | head -1)"
    fi
    RSS_NOW="$(_service_rss_kb_total "$SERVICE_MARKER")"
    if [ -n "$RSS_NOW" ]; then
        SAMPLES=$((SAMPLES + 1))
        [ "$RSS_NOW" -gt "$PEAK_KB" ] && PEAK_KB="$RSS_NOW"
        if [ "$RSS_NOW" -gt "$CEILING_KB" ]; then
            KILL_ELAPSED=$(( $(date +%s) - T0 ))
            bad "RSS $(_kb_to_gb_str "$RSS_NOW") GB EXCEEDED ceiling $(_kb_to_gb_str "$CEILING_KB") GB at T+${KILL_ELAPSED}s — SIGKILLing the isolated engine now"
            for pid in $(_service_pids "$SERVICE_MARKER"); do
                _kill_if_isolated "$pid" "$SERVICE_MARKER" || true
            done
            kill -9 "$INDEX_PID" 2>/dev/null || true
            wait "$INDEX_PID" 2>/dev/null || true
            KILLED=1
            break
        fi
    fi
    NOW="$(date +%s)"
    if [ $((NOW - LAST_PROGRESS)) -ge 5 ]; then
        LAST_PROGRESS="$NOW"
        FLUSHES_SO_FAR=0
        LARGEST_SO_FAR=0
        if [ -n "$RUN_LOG" ] && [ -r "$RUN_LOG" ]; then
            FLUSHES_SO_FAR="$(_count_matches_in_log "$RUN_LOG" chunk_flush_complete)"
            LARGEST_SO_FAR="$(_largest_flush_in_log "$RUN_LOG")"
        fi
        note "…still indexing ($((NOW - T0))s): flushes=${FLUSHES_SO_FAR} largest=${LARGEST_SO_FAR} peak_rss=$(_kb_to_gb_str "$PEAK_KB")GB samples=${SAMPLES}"
    fi
    if ! kill -0 "$INDEX_PID" 2>/dev/null; then break; fi
    if [ $((NOW - T0)) -ge "$HARD_DEADLINE_S" ]; then
        kill -9 "$INDEX_PID" 2>/dev/null || true
        wait "$INDEX_PID" 2>/dev/null || true
        for pid in $(_service_pids "$SERVICE_MARKER"); do
            _kill_if_isolated "$pid" "$SERVICE_MARKER" || true
        done
        bad "HARD DEADLINE ${HARD_DEADLINE_S}s exceeded without a ceiling breach or completion — treating as FAILED, not KILLED (RSS never crossed the ceiling; this is a hang, not a memory blowup)"
        INDEX_RC=124
        break
    fi
    sleep 1
done

if [ "$KILLED" = 1 ]; then
    INDEX_RC=137
elif [ -z "${INDEX_RC:-}" ]; then
    wait "$INDEX_PID"; INDEX_RC=$?
fi
WALL_S=$(( $(date +%s) - T0 ))

if [ -n "$RUN_LOG" ]; then
    note "run log: $RUN_LOG"
else
    note "never located an index-*.log run log — cap-bind + retry-count evidence below will report UNMEASURED"
fi

LARGEST_FLUSH=0
FLUSHES_AT_CAP=0
LEGACY_PAGES_AT_CAP=0
FLUSHES_TOTAL=0
MANIFEST_RETRIES=0
REFRESHABLE_RETRIES=0
if [ -n "$RUN_LOG" ] && [ -r "$RUN_LOG" ]; then
    LARGEST_FLUSH="$(_largest_flush_in_log "$RUN_LOG")"
    # D4 (nexus-97dp4 first-run finding): FLUSHES_AT_CAP is now the SAME
    # event family as FLUSHES_TOTAL (chunk_flush_complete only) — a
    # same-family subset count, so FLUSHES_AT_CAP <= FLUSHES_TOTAL holds by
    # construction. The legacy per-file paged-upload family that also
    # reached/exceeded the cap is a SEPARATE counter, never summed in here.
    FLUSHES_AT_CAP="$(_cap_bound_count_in_log "$RUN_LOG" "$EFFECTIVE_CAP" combined)"
    LEGACY_PAGES_AT_CAP="$(_cap_bound_count_in_log "$RUN_LOG" "$EFFECTIVE_CAP" legacy)"
    # Same double-print trap as _count_matches_in_log (grep -c prints "0" AND
    # exits 1 on zero matches) — capture-then-default, never `|| echo 0`.
    FLUSHES_TOTAL="$(_count_matches_in_log "$RUN_LOG" chunk_flush_complete)"
    MANIFEST_RETRIES="$(_count_matches_in_log "$RUN_LOG" manifest_write_transient_error_retry)"
    REFRESHABLE_RETRIES="$(_count_matches_in_log "$RUN_LOG" refreshable_http_store.retry)"
fi

# ── verdict ──────────────────────────────────────────────────────────────
say "6/6 verdict"
VERDICT_LINE="$(_compute_verdict "$KILLED" "$INDEX_RC" "$PEAK_KB" "$CEILING_KB" \
    "$EFFECTIVE_CAP" "$LARGEST_FLUSH" "$KILL_ELAPSED" "$FLUSHES_AT_CAP")"
GATE_STATUS="${VERDICT_LINE%%|*}"
REASON="${VERDICT_LINE#*|}"
if [ "$LEGACY_PAGES_AT_CAP" -gt 0 ] 2>/dev/null; then
    note "legacy per-file paged-upload path ALSO bound the cap $LEGACY_PAGES_AT_CAP time(s) (reported separately — D4, not folded into the combined-write count above)"
fi

PEAK_GB_STR="$(_kb_to_gb_str "$PEAK_KB")"
_emit_json "$PEAK_GB_STR" "$EFFECTIVE_CAP" "${EFFECTIVE_TIMEOUT:-null}" \
    "$FLUSHES_TOTAL" "$FLUSHES_AT_CAP" "$LEGACY_PAGES_AT_CAP" "$KILLED" "$WALL_S" \
    "$MANIFEST_RETRIES" "$REFRESHABLE_RETRIES"

case "$GATE_STATUS" in
    PASSED)
        printf '\033[32mLOCAL-INDEX MEMORY GATE PASSED\033[0m — %s\n' "$REASON"
        GATE_RC=0
        ;;
    KILLED)
        printf '\033[31mLOCAL-INDEX MEMORY GATE KILLED\033[0m — %s\n' "$REASON"
        tail -30 "$INDEX_STDOUT" 2>/dev/null | sed 's/^/       /'
        GATE_RC=2
        ;;
    *)
        printf '\033[31mLOCAL-INDEX MEMORY GATE FAILED\033[0m — %s\n' "$REASON"
        tail -30 "$INDEX_STDOUT" 2>/dev/null | sed 's/^/       /'
        GATE_RC=1
        ;;
esac

exit "$GATE_RC"
