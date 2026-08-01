#!/usr/bin/env bash
# Containerized unit-suite shard fan-out (bead nexus-uq3xs, leg 1).
#
# Builds the shard image once (layer-cached; see Dockerfile.shard for the
# cache keys), splits the test-file roster into N round-robin shards, runs
# N capped containers concurrently, and aggregates pass/fail into one exit
# code. Each container is an isolated sandbox (own PG, own engine JAR boot,
# own tmpfs-backed /tmp) — the isolated-sandbox concurrency refinement
# (feedback_parallelize_isolated_gates), not a parallel-test-rule violation.
#
# Usage:
#   tests/containers/fanout.sh [options] [testfile ...]
#     -n N          shard count (default 6)
#     -o DIR        output dir for logs/junit (default tests/containers/out/<ts>)
#     --cpus X      per-container CPU cap (default 2)
#     --memory X    per-container memory cap (default 3g)
#     --no-build    skip docker build (image must exist)
#     --build-only  build the image and exit
#     --image REF   override image ref (default nexus-test-shard:<engine tag>)
#     testfile ...  explicit roster (relative to repo root); when omitted the
#                   roster is pytest --collect-only run INSIDE the image
#                   (hermetic — honors the suite's addopts markers)
#
# Env passthrough:
#   NX_TEST_T2_SUBSTRATE   forwarded into every container when set (=engine
#                          flips the autouse pin to the engine substrate)
#   PYTEST_ADDOPTS         forwarded when set
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SHARDS=6
OUT=""
CPUS=2
MEMORY=3g
DO_BUILD=1
BUILD_ONLY=0
IMAGE=""
FILES=()

while [ $# -gt 0 ]; do
  case "$1" in
    -n)          SHARDS="$2"; shift 2 ;;
    -o)          OUT="$2"; shift 2 ;;
    --cpus)      CPUS="$2"; shift 2 ;;
    --memory)    MEMORY="$2"; shift 2 ;;
    --no-build)  DO_BUILD=0; shift ;;
    --build-only) BUILD_ONLY=1; shift ;;
    --image)     IMAGE="$2"; shift 2 ;;
    -h|--help)   sed -n '2,26p' "$0"; exit 0 ;;
    *)           FILES+=("$1"); shift ;;
  esac
done

# ── Engine tag: the ONE constant (engine_version.py), never hand-typed ───────
ENGINE_VER="$(sed -n 's/^REQUIRED_ENGINE_VERSION.*= (\([0-9][0-9]*\), \([0-9][0-9]*\), \([0-9][0-9]*\)).*/\1.\2.\3/p' \
  "$REPO_ROOT/src/nexus/engine_version.py")"
if [ -z "$ENGINE_VER" ]; then
  echo "fanout: could not parse REQUIRED_ENGINE_VERSION from src/nexus/engine_version.py" >&2
  exit 2
fi
PINNED_SERVICE_TAG="engine-service-v${ENGINE_VER}"
[ -n "$IMAGE" ] || IMAGE="nexus-test-shard:${PINNED_SERVICE_TAG}"

# ── Build (layer cache does the rebuild-only-on-key-miss work) ───────────────
if [ "$DO_BUILD" = 1 ]; then
  echo "fanout: building $IMAGE (PINNED_SERVICE_TAG=$PINNED_SERVICE_TAG)"
  docker build --platform linux/arm64 \
    -f "$REPO_ROOT/tests/containers/Dockerfile.shard" \
    --build-arg "PINNED_SERVICE_TAG=$PINNED_SERVICE_TAG" \
    -t "$IMAGE" "$REPO_ROOT"
fi
[ "$BUILD_ONLY" = 1 ] && exit 0

# ── Output dir ───────────────────────────────────────────────────────────────
[ -n "$OUT" ] || OUT="$REPO_ROOT/tests/containers/out/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"

# ── Env passthrough ──────────────────────────────────────────────────────────
ENV_OPTS=()
[ -n "${NX_TEST_T2_SUBSTRATE:-}" ] && ENV_OPTS+=(-e "NX_TEST_T2_SUBSTRATE=${NX_TEST_T2_SUBSTRATE}")
[ -n "${PYTEST_ADDOPTS:-}" ]       && ENV_OPTS+=(-e "PYTEST_ADDOPTS=${PYTEST_ADDOPTS}")

# ── Roster ───────────────────────────────────────────────────────────────────
if [ ${#FILES[@]} -eq 0 ]; then
  echo "fanout: collecting roster in-container (pytest --collect-only)"
  ROSTER_RAW="$OUT/roster.txt"
  docker run --rm --platform linux/arm64 "${ENV_OPTS[@]}" "$IMAGE" \
    python -m pytest -p no:cacheprovider --collect-only -q 2>/dev/null \
    | sed -n 's/::.*//p' | sort -u > "$ROSTER_RAW"
  mapfile -t FILES < "$ROSTER_RAW"
fi
if [ ${#FILES[@]} -eq 0 ]; then
  echo "fanout: empty roster" >&2
  exit 2
fi
echo "fanout: ${#FILES[@]} test files -> $SHARDS shards ($CPUS cpus / $MEMORY each)"

# Round-robin by file.
declare -a SHARD_FILES
for i in "${!FILES[@]}"; do
  s=$((i % SHARDS))
  SHARD_FILES[s]="${SHARD_FILES[s]:-} ${FILES[i]}"
done

# ── Launch ───────────────────────────────────────────────────────────────────
RUN_ID="nxfan-$$"
cleanup() {
  for i in $(seq 0 $((SHARDS - 1))); do
    docker rm -f "${RUN_ID}-${i}" >/dev/null 2>&1 || true
  done
}
trap cleanup INT TERM

T0=$(date +%s)
PIDS=()
for i in $(seq 0 $((SHARDS - 1))); do
  files="${SHARD_FILES[i]:-}"
  if [ -z "$files" ]; then
    echo "0" > "$OUT/shard-$i.rc"; echo "0 0" > "$OUT/shard-$i.time"
    : > "$OUT/shard-$i.log"
    continue
  fi
  (
    s=$(date +%s)
    rc=0
    # shellcheck disable=SC2086  # word-splitting of $files is intended
    docker run --rm --platform linux/arm64 --name "${RUN_ID}-${i}" \
      --cpus="$CPUS" --memory="$MEMORY" --shm-size=256m \
      "${ENV_OPTS[@]}" \
      -v "$OUT:/out" \
      "$IMAGE" \
      python -m pytest -p no:cacheprovider -q --junitxml="/out/shard-$i.xml" $files \
      > "$OUT/shard-$i.log" 2>&1 || rc=$?
    e=$(date +%s)
    echo "$rc" > "$OUT/shard-$i.rc"
    echo "$s $e" > "$OUT/shard-$i.time"
  ) &
  PIDS+=($!)
done
wait "${PIDS[@]}" 2>/dev/null || true
T1=$(date +%s)

# ── Aggregate ────────────────────────────────────────────────────────────────
FAIL=0
TOTAL_T=0; TOTAL_P=0; TOTAL_F=0; TOTAL_E=0; TOTAL_S=0
echo
echo "shard  exit  wall     summary"
for i in $(seq 0 $((SHARDS - 1))); do
  rc="$(cat "$OUT/shard-$i.rc" 2>/dev/null || echo 99)"
  read -r s e < "$OUT/shard-$i.time" 2>/dev/null || { s=0; e=0; }
  wall=$((e - s))
  summary="$(grep -E '^[0-9]+ (passed|failed|error)|=+ .*(passed|failed|error|no tests ran).*=+' \
    "$OUT/shard-$i.log" 2>/dev/null | tail -1 || true)"
  xml="$OUT/shard-$i.xml"
  if [ -f "$xml" ]; then
    # junit: <testsuite ...> carries tests/errors/failures/skipped counts;
    # attribute ORDER is not guaranteed, so pull each one independently.
    suite_tag="$(grep -o '<testsuite [^>]*>' "$xml" | head -1 || true)"
    attr() { printf '%s' "$suite_tag" | sed -n "s/.*$1=\"\([0-9]*\)\".*/\1/p"; }
    t="$(attr tests)"; err="$(attr errors)"; f="$(attr failures)"; sk="$(attr skipped)"
    if [ -n "${t:-}" ]; then
      TOTAL_T=$((TOTAL_T + t)); TOTAL_E=$((TOTAL_E + err))
      TOTAL_F=$((TOTAL_F + f)); TOTAL_S=$((TOTAL_S + sk))
      TOTAL_P=$((TOTAL_P + t - err - f - sk))
    fi
  fi
  # pytest exit 5 = no tests collected in this shard (marker deselect) — pass.
  if [ "$rc" != 0 ] && [ "$rc" != 5 ]; then FAIL=1; fi
  printf "%-6s %-5s %-8s %s\n" "$i" "$rc" "${wall}s" "${summary:-<no summary>}"
done
echo
echo "total: ${TOTAL_T} tests / ${TOTAL_P} passed / ${TOTAL_F} failed / ${TOTAL_E} errors / ${TOTAL_S} skipped"
echo "wall-clock: $((T1 - T0))s across $SHARDS shards ($CPUS cpus, $MEMORY each) — logs: $OUT"
exit "$FAIL"
