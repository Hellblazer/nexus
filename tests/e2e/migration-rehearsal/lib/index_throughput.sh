#!/usr/bin/env bash
# Indexing wall-clock floor for the release gates (nexus-98zsp). SOURCED.
#
# engine-service-v0.1.99 capped the shared ONNX session at two intra-op
# threads and every gate passed: the Java suite, the Python suite, the
# shakeout, the candidate migration, the published-client write gate. The
# first thing that indexed a real corpus was the client shakedown, where
# `nx index rdr` sat for 30 minutes. Nothing measured how long indexing
# took, so an 8x slowdown was invisible until a human waited it out.
#
# Contract:
#   throughput_gate LABEL INDEX_LOG ELAPSED_SECONDS BASELINE_FILE
#     Sums the "N chunks" per-file lines in INDEX_LOG, computes seconds per
#     chunk, and compares against the persisted baseline for LABEL in
#     BASELINE_FILE (a committed TSV beside this file: label, seconds_per_chunk, chunks,
#     recorded_at, engine). Returns:
#       0  measured <= THROUGHPUT_CEILING_FACTOR x baseline (PASS)
#       1  measured  > ceiling (FAIL: a throughput regression)
#       2  no baseline for LABEL: the measurement is RECORDED into
#          BASELINE_FILE and reported. Not a pass — the caller prints it as
#          a warning and the next run has a ceiling. A run that embedded
#          fewer than THROUGHPUT_MIN_CHUNKS chunks records nothing and
#          returns 3 (too small to be a baseline; reported, never silent).
#   throughput_engine_shape [ENGINE_LOG]
#     Prints the engine's ONNX thread and admission boot lines, so a
#     thread-shape change is visible in the gate transcript next to the
#     number it explains.
#
# Baseline discipline: a baseline is the previous ENGINE's number for the
# same corpus, on the same box class. Re-record deliberately (delete the
# row) after a change that is MEANT to move it; never let a red re-record
# itself.

THROUGHPUT_CEILING_FACTOR="${THROUGHPUT_CEILING_FACTOR:-2.0}"
THROUGHPUT_MIN_CHUNKS="${THROUGHPUT_MIN_CHUNKS:-20}"

throughput_box_class() {
    # Hardware model + core count: a row is only comparable within a box
    # class (a 10-core laptop and a 16-core desktop embed at different
    # rates). Best-effort, never fails the gate.
    local model cores
    model="$(sysctl -n machdep.cpu.brand_string 2>/dev/null || sed -n 's/^model name\s*: //p' /proc/cpuinfo 2>/dev/null | head -1)"
    cores="$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 0)"
    printf '%s/%sc\n' "${model:-unknown}" "$cores" | tr '\t' ' '
}

throughput_chunks_in_log() {
    # Sum the per-file "<name> — <n> chunks" lines the client prints (skipped
    # files print "skipped", not a count). Anchored on the em dash so the
    # running "[eta] 9/292 files · 1,053 chunks" line cannot be counted
    # (a bare "[0-9]+ chunks" would read "053 chunks" out of it).
    grep -oE '— [0-9]+ chunks' "$1" 2>/dev/null | awk '{s+=$2} END {print s+0}'
}

throughput_engine_shape() {
    # The engine's own stdout lands in storage_service_native.log (the
    # native binary) while storage_service.log is the supervisor's; the
    # first shakedown run looked only at the latter and printed "not
    # logged" beside a healthy engine. Accept a directory or a file.
    local target="${1:-$HOME/.config/nexus/logs}"
    local -a logs=()
    if [ -d "$target" ]; then
        local f
        for f in "$target"/storage_service_native.log "$target"/storage_service.log; do
            [ -f "$f" ] && logs+=("$f")
        done
    elif [ -f "$target" ]; then
        logs=("$target")
    fi
    if [ "${#logs[@]}" -eq 0 ]; then
        echo "  engine shape: no engine log at $target"
        return 0
    fi
    local threads admission
    # `|| true` on each: the callers run under set -euo pipefail, and a log
    # without the line makes grep exit 1 through tail — a bare assignment
    # would then kill the whole gate run with no diagnostic.
    threads="$(grep -ho 'event=onnx_intra_op_threads_configured.*' "${logs[@]}" | tail -1 || true)"
    admission="$(grep -ho 'event=local_onnx_admission_configured.*' "${logs[@]}" | tail -1 || true)"
    echo "  engine shape: ${threads:-onnx_intra_op_threads_configured not logged}"
    echo "  engine shape: ${admission:-local_onnx_admission_configured not logged}"
}

throughput_gate() {
    local label="$1" log="$2" elapsed="$3" baseline_file="$4"
    local chunks spc
    chunks="$(throughput_chunks_in_log "$log")"
    if [ "$chunks" -lt "$THROUGHPUT_MIN_CHUNKS" ]; then
        echo "  throughput[$label]: ${chunks} chunks in ${elapsed}s — below the ${THROUGHPUT_MIN_CHUNKS}-chunk floor, nothing to measure (NOT a pass)"
        return 3
    fi
    spc="$(awk -v e="$elapsed" -v c="$chunks" 'BEGIN {printf "%.4f", e / c}')"
    local box base
    box="$(throughput_box_class)"
    # The row for THIS box class; a baseline from a different box is not a
    # ceiling for this one, so its absence records, never compares.
    base="$(awk -F'\t' -v l="$label" -v b="$box" '$1 == l && $6 == b {print $2}' "$baseline_file" 2>/dev/null | tail -1)"
    if [ -z "$base" ]; then
        local engine
        engine="$(nx --version 2>/dev/null | head -1 || echo unknown)"
        local row
        row="$(printf '%s\t%s\t%s\t%s\t%s\t%s' "$label" "$spc" "$chunks" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$engine" "$box")"
        if { [ -f "$baseline_file" ] || printf 'label\tseconds_per_chunk\tchunks\trecorded_at\tclient\tbox\n' > "$baseline_file"; } 2>/dev/null \
            && printf '%s\n' "$row" >> "$baseline_file" 2>/dev/null; then
            echo "  throughput[$label]: ${chunks} chunks in ${elapsed}s = ${spc} s/chunk — NO BASELINE for box '$box', recorded into $baseline_file (no ceiling applied this run; commit the row)"
        else
            echo "  throughput[$label]: ${chunks} chunks in ${elapsed}s = ${spc} s/chunk — NO BASELINE for box '$box', and $baseline_file is not writable; add this row by hand:"
            echo "  $row"
        fi
        return 2
    fi
    local ceiling
    ceiling="$(awk -v b="$base" -v f="$THROUGHPUT_CEILING_FACTOR" 'BEGIN {printf "%.4f", b * f}')"
    if awk -v s="$spc" -v c="$ceiling" 'BEGIN {exit !(s > c)}'; then
        echo "  throughput[$label]: ${chunks} chunks in ${elapsed}s = ${spc} s/chunk — FAIL: above ${THROUGHPUT_CEILING_FACTOR}x baseline ${base} (ceiling ${ceiling})"
        return 1
    fi
    echo "  throughput[$label]: ${chunks} chunks in ${elapsed}s = ${spc} s/chunk — ok (baseline ${base}, ceiling ${ceiling})"
    return 0
}
