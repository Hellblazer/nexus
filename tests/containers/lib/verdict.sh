#!/usr/bin/env bash
# Aggregate a fan-out's shard artifacts into one verdict.
#
# Sourced by tests/containers/fanout.sh and exercised directly by
# tests/test_fanout_verdict.py. It lives here so the tests drive THE REAL
# AGGREGATION rather than a reimplementation of it.
#
# NON-VACUITY (nexus-moht0: "a sweep that found nothing to check is a failure,
# not a pass"). Per-shard pytest exit 5 (nothing collected) is tolerated — a
# marker deselect can legitimately empty ONE shard. Zero tests across ALL
# shards is a different thing: a broken roster, a marker change that
# deselected everything, or an image whose test tree never got copied.
#
# Measured 2026-08-24, this guard's first real use: a 6-shard run had two
# shards SIGKILLed at their memory cap. They wrote no junit, so 9,622 of
# ~14,500 tests were collected and the rest silently never ran. Without the
# floor that reads as "9,074 passed" plus some failures, with no signal that a
# third of the suite was missing.

# fanout_verdict <out_dir> <shards> [min_tests]
# Prints the per-shard table and totals. Returns 0 only when every shard
# exited 0 or 5, every shard reported, and the totals clear the floor.
fanout_verdict() {
    local out="$1" shards="$2" min_tests="${3:-}"
    local fail=0 reported=0
    local total_t=0 total_p=0 total_f=0 total_e=0 total_s=0
    local i rc s e wall summary xml suite_tag t err f sk

    echo
    echo "shard  exit  wall     summary"
    for i in $(seq 0 $((shards - 1))); do
        rc="$(cat "$out/shard-$i.rc" 2>/dev/null || echo 99)"
        read -r s e < "$out/shard-$i.time" 2>/dev/null || { s=0; e=0; }
        wall=$((e - s))
        summary="$(grep -E '^[0-9]+ (passed|failed|error)|=+ .*(passed|failed|error|no tests ran).*=+' \
            "$out/shard-$i.log" 2>/dev/null | tail -1 || true)"
        [ -f "$out/shard-$i.rc" ] && reported=$((reported + 1))
        xml="$out/shard-$i.xml"
        if [ -f "$xml" ]; then
            suite_tag="$(grep -o '<testsuite [^>]*>' "$xml" | head -1 || true)"
            t="$(printf '%s' "$suite_tag" | sed -n 's/.*tests="\([0-9]*\)".*/\1/p')"
            err="$(printf '%s' "$suite_tag" | sed -n 's/.*errors="\([0-9]*\)".*/\1/p')"
            f="$(printf '%s' "$suite_tag" | sed -n 's/.*failures="\([0-9]*\)".*/\1/p')"
            sk="$(printf '%s' "$suite_tag" | sed -n 's/.*skipped="\([0-9]*\)".*/\1/p')"
            if [ -n "${t:-}" ]; then
                total_t=$((total_t + t)); total_e=$((total_e + ${err:-0}))
                total_f=$((total_f + ${f:-0})); total_s=$((total_s + ${sk:-0}))
                total_p=$((total_p + t - ${err:-0} - ${f:-0} - ${sk:-0}))
            fi
        fi
        # pytest exit 5 = no tests collected in this shard (marker deselect).
        if [ "$rc" != 0 ] && [ "$rc" != 5 ]; then fail=1; fi
        printf "%-6s %-5s %-8s %s\n" "$i" "$rc" "${wall}s" "${summary:-<no summary>}"
    done

    echo
    echo "total: ${total_t} tests / ${total_p} passed / ${total_f} failed / ${total_e} errors / ${total_s} skipped"

    if [ "$reported" -ne "$shards" ]; then
        echo "fanout: FAILED — $reported of $shards shard(s) reported. A run that" >&2
        echo "        silently produced fewer verdicts than shards must not pass." >&2
        fail=1
    fi
    if [ "$total_t" -eq 0 ]; then
        echo "fanout: FAILED — 0 tests collected across all $shards shard(s). A run" >&2
        echo "        that executed nothing is not a pass. Check the roster, the" >&2
        echo "        marker selection, and that shard-*.xml were produced." >&2
        fail=1
    fi
    if [ -n "$min_tests" ] && [ "$total_t" -lt "$min_tests" ]; then
        echo "fanout: FAILED — collected $total_t tests, below the floor of $min_tests." >&2
        fail=1
    fi
    return "$fail"
}
