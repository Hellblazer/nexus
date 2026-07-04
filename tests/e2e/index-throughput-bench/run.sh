#!/usr/bin/env bash
# Index-throughput scaling benchmark (nexus-duoak.2).
#
# Sweeps NX_INDEX_CONCURRENCY over WORKER_COUNTS against the real cloud
# service, one FRESH clone per run (distinct temp dir => distinct scratch
# owner via ensure_owner_for_repo, so no run warms another's staleness
# state). Captures wall clock + the --debug-timing stderr block per run,
# then prints the scaling table via aggregate.py.
#
# SAFETY: every clone lands under a bench-XXXX temp root, so all catalog
# owners/collections created are scratch-scoped. teardown.sh (duoak.3)
# removes them and asserts no real collection was touched. The trap runs
# teardown even on abort.
#
# Usage: ./run.sh [output-dir]
#   REPO_URL   (default: fastapi) pinned, code-heavy public corpus
#   REPO_REF   (default: 0.115.0) fixed tag/SHA for determinism
#   WORKER_COUNTS (default: "1 2 4 8")

set -euo pipefail

BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${1:-$BENCH_DIR/results-$(date +%Y%m%d-%H%M%S)}"
REPO_URL="${REPO_URL:-https://github.com/fastapi/fastapi.git}"
REPO_REF="${REPO_REF:-0.115.0}"
WORKER_COUNTS="${WORKER_COUNTS:-1 2 4 8}"
WORK_ROOT="$(mktemp -d -t bench-index-XXXXXX)"
# Clone dirs are named benchidx-<stamp>-w<N>: the catalog owner takes its
# name from the dir basename, and teardown_scope.py keys deletion on that
# marker (collection names embed owner TUMBLERS, so the name is the only
# place the marker can live).
STAMP="$(date +%m%d%H%M)"

mkdir -p "$OUT_DIR"
echo "corpus: $REPO_URL @ $REPO_REF" | tee "$OUT_DIR/corpus.txt"
echo "workers: $WORKER_COUNTS" | tee -a "$OUT_DIR/corpus.txt"

cleanup() {
    "$BENCH_DIR/teardown.sh" "$WORK_ROOT" "$OUT_DIR" || echo "WARN: teardown failed — run teardown.sh manually with: $WORK_ROOT" >&2
    rm -rf "$WORK_ROOT"
}
trap cleanup EXIT

# Snapshot the collection list BEFORE any run — teardown asserts against it.
uv run nx collection list > "$OUT_DIR/collections-before.txt"

base_clone="$WORK_ROOT/base"
git clone --quiet --depth 1 --branch "$REPO_REF" "$REPO_URL" "$base_clone"
git -C "$base_clone" rev-parse HEAD > "$OUT_DIR/corpus-sha.txt"

for w in $WORKER_COUNTS; do
    run_dir="$WORK_ROOT/benchidx-$STAMP-w$w"
    cp -R "$base_clone" "$run_dir"      # distinct path => distinct scratch owner
    rm -rf "$run_dir/.git" && git -C "$run_dir" init -q && git -C "$run_dir" add -A -f >/dev/null 2>&1 || true

    echo "=== workers=$w corpus=$run_dir ==="
    start=$(python3 -c 'import time; print(time.time())')
    NX_INDEX_CONCURRENCY="$w" uv run nx index repo "$run_dir" --force --debug-timing \
        > "$OUT_DIR/w$w.log" 2>&1
    end=$(python3 -c 'import time; print(time.time())')
    python3 -c "print($end - $start)" > "$OUT_DIR/w$w.wall"
    echo "workers=$w wall=$(cat "$OUT_DIR/w$w.wall")s"
done

echo
uv run python "$BENCH_DIR/aggregate.py" "$OUT_DIR" | tee "$OUT_DIR/scaling-table.txt"
echo
echo "results in: $OUT_DIR"
