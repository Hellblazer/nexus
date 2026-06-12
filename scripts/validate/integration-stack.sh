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
# Each Python suite spins up its own throwaway PG16 + a fresh service JAR
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
# and pg16 binaries at /opt/homebrew/opt/postgresql@16/bin. Suites self-skip
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
elif [[ ! -f "$JAR" ]]; then
    echo "✗ $JAR missing and --no-build/--java-only set — build it first." >&2
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

# The Java T3 serving contract tests — pgvector serving + the RDR-156 stats view.
JAVA_TESTS="PgVectorServingContractTest,PgVectorRepositoryContractTest,PgVectorHybridSearchContractTest,CollectionVectorStatsTest,SoftDeleteTest,ManifestFunctionsTest,VectorHybridHttpTest"

rc=0

if [[ "$RUN_PYTHON" == "1" ]]; then
    echo "▸ T1/T2/catalog Python integration suites (ephemeral PG + service)…"
    uv run pytest -m integration "${PY_SUITES[@]}" -q || rc=$?
fi

if [[ "$RUN_JAVA" == "1" ]]; then
    echo "▸ T3 Java serving contract tests…"
    ( cd service && mvn -q test -Dtest="$JAVA_TESTS" ) || rc=$?
fi

echo ""
if [[ "$rc" == "0" ]]; then
    echo "✓ storage stack green — T1+T2+T3 serve end-to-end in the sandbox."
else
    echo "✗ storage stack RED (rc=$rc) — see output above."
fi
exit "$rc"
