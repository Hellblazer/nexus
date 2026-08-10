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
# already solved "corpus that binds the cap" for a Linux CONTAINER using
# /proc introspection and a single oversize-file trick (a file whose own
# chunk count exceeds the cap forces ChunkBatcher.add() to reject it as
# oversize-for-one-batch, routing it through the legacy per-file paged
# upload whose FIRST page is exactly min(total, cap) chunks — deterministic,
# no multi-file boundary-alignment luck required). This script reuses that
# mechanism, generalized: (1) it runs on the bare host (macOS or Linux) via
# `ps`, not /proc, since this box has no /proc; (2) it CALIBRATES file size
# against the live chunker instead of a hard-coded function count, because
# the container script's own hard-coded "40 funcs -> 21 chunks" figure does
# NOT reproduce on this tree (measured here: 40 funcs -> 10 chunks — the
# chunker's actual behavior drifted from that comment, silently); trusting a
# stale magic number is exactly the class of bug this gate exists to avoid a
# repeat of. (3) it adds a HARD RSS CEILING with immediate SIGKILL — the
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

# Count of flush/page events (either family) whose chunk count reached or
# exceeded *cap* — the "bound M times" figure in the verdict line. A cap
# that binds exactly once could still be luck; a gate reporting the COUNT
# lets a reviewer judge how thoroughly the ceiling was exercised.
_cap_bound_count_in_log() {
    local log="$1" cap="$2" n=0 v
    for v in $(grep -F 'chunk_flush_complete' "$log" 2>/dev/null | grep -oE 'chunks=[0-9]+' | cut -d= -f2); do
        [ "$v" -ge "$cap" ] 2>/dev/null && n=$((n + 1))
    done
    for v in $(grep -F 'http_vector_upsert_chunks_request' "$log" 2>/dev/null | grep -oE ' count=[0-9]+' | tr -d ' ' | cut -d= -f2); do
        [ "$v" -ge "$cap" ] 2>/dev/null && n=$((n + 1))
    done
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
    # $6 killed(0/1) $7 wall_s $8 manifest_retries $9 refreshable_retries
    printf '{"peak_gb":%s,"cap":%s,"timeout_s":%s,"flushes":%s,"flushes_at_cap":%s,"killed":%s,"wall_s":%s,"manifest_write_retries":%s,"refreshable_retries":%s}\n' \
        "$1" "$2" "$3" "$4" "$5" "$6" "$7" "$8" "$9"
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
    _assert_eq "two flushes >= cap=16 in fake1.log" \
        "$(_cap_bound_count_in_log "$t/fake1.log" 16)" "2"
    _assert_eq "zero flushes >= cap=16 in fake2.log (the 97dp4 vacuous case)" \
        "$(_cap_bound_count_in_log "$t/fake2.log" 16)" "0"

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
    json="$(_emit_json "3.03" 16 600.0 8 2 0 187 0 0)"
    printf '%s\n' "$json"
    if command -v python3 >/dev/null 2>&1; then
        if printf '%s' "$json" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["peak_gb"]==3.03; assert d["cap"]==16; assert d["killed"]==0' 2>/dev/null; then
            ok "JSON line parses and round-trips the fields it claims"
        else
            bad "JSON line failed to parse or a field did not round-trip"
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
  --corpus-scale N    number of independently-oversize filler files to
                      generate, each calibrated LIVE against this tree's
                      real chunker to exceed the effective cap (default: 1)
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
_nx init >"$LOGS/init.log" 2>&1 || { tail -30 "$LOGS/init.log" >&2; _die "nx init failed"; }
healthy=0
for _ in $(seq 1 30); do
    _nx daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|running" && { healthy=1; break; }
    sleep 2
done
[ "$healthy" = 1 ] || _die "service never became healthy"

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
say "4/6 generate a corpus calibrated to actually bind cap=$EFFECTIVE_CAP (corpus-scale=$CORPUS_SCALE)"
i=1
while [ "$i" -le "$CORPUS_SCALE" ]; do
    FUNCS=40
    ITER=0
    while :; do
        ITER=$((ITER + 1))
        [ "$ITER" -le 12 ] || _die "corpus calibration for file $i did not exceed cap=$EFFECTIVE_CAP after 12 growth iterations (last: $FUNCS funcs -> $CHUNKS chunks) — the chunker or cap changed shape enough that this generator needs a new starting point"
        "$VENV/bin/python" - "$CORPUS_DIR/oversize_$i.py" "$FUNCS" <<'PYEOF'
import sys
from pathlib import Path

path, nfuncs = sys.argv[1], int(sys.argv[2])


def make_func(i: int) -> str:
    body = "\n".join(f"    v{j} = {j} * {i} + {j % 7}  # filler" for j in range(12))
    return (
        f"def widget_function_{i:04d}(x: int, y: int = {i}) -> int:\n"
        f'    """Deterministic local-index-memory-gate filler function {i}."""\n'
        f"{body}\n"
        f"    return v0 + x + y\n"
    )


content = "# Generated local-index-memory-gate fixture — deterministic filler.\n\n"
content += "\n\n".join(make_func(i) for i in range(nfuncs)) + "\n"
Path(path).write_text(content)
PYEOF
        CHUNKS="$("$VENV/bin/python" -c "
from pathlib import Path
from nexus.chunker import chunk_file
p = Path('$CORPUS_DIR/oversize_$i.py')
print(len(chunk_file(p, p.read_text())))
")"
        if [ "$CHUNKS" -gt "$EFFECTIVE_CAP" ] 2>/dev/null; then
            note "oversize_$i.py: $FUNCS funcs -> $CHUNKS chunks (> cap $EFFECTIVE_CAP, margin ${CHUNKS}-${EFFECTIVE_CAP}=$((CHUNKS - EFFECTIVE_CAP))) — calibration OK, measured not guessed"
            break
        fi
        FUNCS=$((FUNCS * 2))
    done
    i=$((i + 1))
done
# One small in-cap file for contrast with the normal ChunkBatcher combined-
# write path (mirrors rehearse_shakeout_e2e.sh's design).
printf '# small\n\ndef sentinel_%s() -> str:\n    """local-index-memory-gate sentinel."""\n    return "ok"\n' "$$" > "$CORPUS_DIR/small_sentinel.py"

( cd "$CORPUS_DIR" && git init -q && git add -A \
    && git -c user.email=memgate@e2e.local -c user.name=local-index-memory-gate commit -qm corpus ) \
    >"$LOGS/corpus-git.log" 2>&1 || { cat "$LOGS/corpus-git.log" >&2; _die "corpus fixture repo init/commit failed"; }

FILE_COUNT="$(find "$CORPUS_DIR" -type f -name '*.py' | wc -l | tr -d ' ')"
note "corpus: $FILE_COUNT python files (--corpus-scale=$CORPUS_SCALE oversize + 1 small)"

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
FLUSHES_TOTAL=0
MANIFEST_RETRIES=0
REFRESHABLE_RETRIES=0
if [ -n "$RUN_LOG" ] && [ -r "$RUN_LOG" ]; then
    LARGEST_FLUSH="$(_largest_flush_in_log "$RUN_LOG")"
    FLUSHES_AT_CAP="$(_cap_bound_count_in_log "$RUN_LOG" "$EFFECTIVE_CAP")"
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

PEAK_GB_STR="$(_kb_to_gb_str "$PEAK_KB")"
_emit_json "$PEAK_GB_STR" "$EFFECTIVE_CAP" "${EFFECTIVE_TIMEOUT:-null}" \
    "$FLUSHES_TOTAL" "$FLUSHES_AT_CAP" "$KILLED" "$WALL_S" \
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
