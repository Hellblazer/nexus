#!/usr/bin/env bash
# integration-stack.sh — reproducible T1+T2+T3 storage-stack sandbox gate.
#
# Exercises every storage tier end-to-end against a REAL, EPHEMERAL service +
# Postgres — no production data, no live daemon, no API keys:
#
#   T2  memory / plans / taxonomy / telemetry / chash / aspects   (Python ↔ service)
#   T1  scratch                                                   (Python ↔ service)
#   catalog  documents / links / manifest / FTS / RLS             (Python ↔ service)
#   T3  pgvector serving + collection_vector_stats                (Java contract tests)
#
# Each Python suite spins up its own throwaway PG17 + a fresh service JAR
# subprocess with an isolated bearer, applies the schema clean, runs, and tears
# down. The whole thing is hermetic and idempotent.
#
# Why this exists: these suites are @pytest.mark.integration and therefore
# EXCLUDED from the default CI/unit run, so storage-stack regressions (the T2/T1
# HTTP path, RLS isolation, the Phase-E token model) can rot unseen. This script
# is the single button-press that proves the whole tier stack still serves.
#
# Usage:
#   scripts/validate/integration-stack.sh [--no-build] [--python-only] [--java-only]
#
#   (default)      build the JAR if stale/missing, then run Python + Java tiers
#   --no-build     skip the JAR build (use the existing service/target jar)
#   --python-only  T1/T2/catalog Python suites only (skip the Java T3 contract tests)
#   --java-only    T3 Java serving contract tests only
#
# Prerequisites (darwin/aarch64 dev box): JDK/GraalVM on PATH or JAVA_HOME set,
# and pg17 binaries at /opt/homebrew/opt/postgresql@17/bin (16/15 also work as
# discover_pg_binaries fallbacks). Suites self-skip
# (not fail) when prerequisites are absent.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

BUILD=1
RUN_PYTHON=1
RUN_JAVA=1
for arg in "$@"; do
    case "$arg" in
        --no-build)    BUILD=0 ;;
        --python-only) RUN_JAVA=0 ;;
        --java-only)   RUN_PYTHON=0 ; BUILD=0 ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

# JAVA_HOME: the Python integration fixtures honor JAVA_HOME to locate `java`;
# default to the system JDK if unset so the suites don't self-skip on a box that
# has java only via /usr/libexec/java_home.
if [[ -z "${JAVA_HOME:-}" ]] && command -v /usr/libexec/java_home >/dev/null 2>&1; then
    JAVA_HOME="$(/usr/libexec/java_home 2>/dev/null || true)"
    export JAVA_HOME
fi

JAR="service/target/nexus-service-1.0-SNAPSHOT.jar"

# ── Build the service JAR (fresh — nexus-todyv: stale shaded jar reuse) ────────
if [[ "$BUILD" == "1" ]]; then
    echo "▸ Building service JAR (mvn package -DskipTests)…"
    ( cd service && mvn -q -DskipTests package )
    echo "  built: $JAR"
elif [[ "$RUN_PYTHON" == "1" && ! -f "$JAR" ]]; then
    # Only the Python suites need a pre-built JAR (their fixtures spawn it as a
    # subprocess). `mvn test` compiles its own, so --java-only does not need it.
    echo "✗ $JAR missing and --no-build set — build it first." >&2
    exit 1
fi

# The Python integration suites — every storage-tier HTTP path.
PY_SUITES=(
    tests/db/test_http_memory_store_integration.py
    tests/db/test_http_plan_library_integration.py
    tests/db/test_http_taxonomy_store_integration.py
    tests/db/test_http_telemetry_store_integration.py
    tests/db/test_http_chash_integration.py
    tests/db/test_http_aspects_stores_integration.py
    tests/db/test_http_scratch_store_integration.py
    tests/db/test_http_catalog_integration.py
)

# The Java tier tests: T3 pgvector serving + the RDR-156 stats view, AND the
# repo-layer RLS contracts (incl. the unset-GUC fail-closed property that the
# chash HTTP test_l delegates to — see test_http_chash_integration.py).
JAVA_TESTS="PgVectorServingContractTest,PgVectorRepositoryContractTest,PgVectorHybridSearchContractTest,CollectionVectorStatsTest,SoftDeleteTest,ManifestFunctionsTest,VectorHybridHttpTest,ChashRepositoryTest,ChunksRlsBehavioralTest,PlanHandlerTest"

rc=0

if [[ "$RUN_PYTHON" == "1" ]]; then
    echo "▸ T1/T2/catalog Python integration suites (ephemeral PG + service)…"
    # Capture so an all-SKIP run (missing pg/jar → module skipif) is reported
    # as INCONCLUSIVE, not green. Skipped tests exit pytest 0; a gate that prints
    # "green" having run zero assertions is the exact false-confidence a gate
    # must not have.
    py_out="$(uv run pytest -m integration "${PY_SUITES[@]}" -q 2>&1)" || rc=$?
    echo "$py_out"
    if ! grep -qE '[1-9][0-9]* passed' <<<"$py_out"; then
        echo "✗ Python tier INCONCLUSIVE — no tests ran (prerequisites absent?)." >&2
        rc=2
    fi
fi

if [[ "$RUN_JAVA" == "1" ]]; then
    echo "▸ T3 + repo-layer Java contract tests…"
    # NOTE: deliberately NOT `-q` — quiet mode suppresses the surefire
    # "Tests run: N" summary the inconclusive-detection below greps for, which
    # would make every Java run read as INCONCLUSIVE even when green.
    java_out="$( cd service && mvn test -Dtest="$JAVA_TESTS" 2>&1 )" || rc=$?
    # Show the surefire per-class lines + the final reactor summary, not the
    # full noisy build log.
    grep -E 'Tests run:|BUILD' <<<"$java_out" | tail -20
    # N==0 means the -Dtest filter matched nothing (renamed/removed class) — a
    # silent-green path the gate must catch.
    if ! grep -qE 'Tests run: [1-9]' <<<"$java_out"; then
        echo "✗ Java tier INCONCLUSIVE — no tests ran (filter matched nothing?)." >&2
        rc=2
    fi
fi

echo ""
if [[ "$rc" == "0" ]]; then
    echo "✓ storage stack green — T1+T2+T3 serve end-to-end in the sandbox."
else
    echo "✗ storage stack RED (rc=$rc) — see output above."
fi
exit "$rc"
