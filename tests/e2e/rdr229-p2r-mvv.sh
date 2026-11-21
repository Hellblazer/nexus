#!/usr/bin/env bash
# This gate self-provisions a THROWAWAY PG + service (the pytest engine substrate: a hermetic PG and the stamped
# service jar of THIS checkout, a minted tenant per run) and writes only there, with a scratch NEXUS_CONFIG_DIR.
# The nexus-a2qhz production-write guard needs a reason-bearing opt-in for a dev checkout's subprocess writes; it is
# given to the substrate pytest run alone (NX_ALLOW_PROD_WRITE below, on that one command). Never production.
# RDR-229 P2.R (nexus-p2m1x.16): the Minimum Viable Validation, Phase 2 half, as a self-provisioning gate.
#
# The RDR's text: "On a substrate-backed engine (the local-service gate), a fixture tenant with one collection whose
# row was corrected so its owner_id and embedding_model differ from its name: `nx index md` into it stamps the row's
# model into chunk metadata, `nx search` calibrates with the row's model, and a quarantine of one of its chunks
# registers a sibling whose lifecycle_state is quarantine without the engine reading the prefix. In the same run, a
# recovery bundle recorded under a legacy name restores. Both census gates are at their Phase 2 pins."
#
# Six checks, each reported PASS or FAIL with its evidence:
#   1. nx index md stamps the row's embedding_model          2. nx search calibrates with the row's model
#   3. quarantine registers a lifecycle_state=quarantine sibling; the engine's guards read the column, probed on a
#      plain name whose row says quarantine and a quarantine- name whose row says live (a prefix reader gets both wrong)
#   4. a recovery bundle recorded under legacy names restores
#   5. both census gates (client CLASS_PIN/PARSE_SITE_PIN, engine CollectionParseGateTest) sit at their Phase 2 pins
#   6. P2.10 (nexus-p2m1x.15), beyond the MVV text: URI moves leave chroma:// residue alone, residue already moved
#      is repaired by the registered data moves (the worker is waited on through the registry and /v1/status
#      boot_work, never a sleep; it needs pg_background in the substrate PG, and its absence is a gate error), and
#      the backfill verb re-derives moved and repaired residue to file://
# Checks 1-4 and 6 are tests/e2e/rdr229_p2r_mvv_checks.py on the engine substrate; check 5 and the report are
# tests/e2e/lib/rdr229_p2r_report.py plus the two gate tests. Checks 2 and 4 are PRE-SATISFIED GUARDS: they held
# before Phase 2 (the row-preferred model resolver, nexus-ml9vu; P2.1's non-stranding tests) and guard against a
# regression, so a PASS there proves no Phase 2 work. The final summary lists each FAIL with the Phase 2
# step it waits on. A FAIL is the finding: no check is weakened to be green, and the exit is 1 for any FAIL. A check that records ERROR
# (its own fixture or a dependency, such as pg_background, is wrong) exits 2: that says nothing about the code.
#
# FAILS LOUDLY, NEVER SKIP-PASSES: an absent dependency (uv, java, git, the service jar build, the PG bundle) exits 2
# naming it; the substrate run must report its two tests, none skipped and none errored; every check must have
# recorded a result and examined at least one item; each census gate must have run at least one test.
#
# Usage:   tests/e2e/rdr229-p2r-mvv.sh
# Heavy (an engine build, a PG boot, an embedding model): from the laptop, run it as a background
#   lab-remote -c lab-m -- tests/e2e/rdr229-p2r-mvv.sh
# NX_GATE_BUILD_LAB_CLASS=<class> passes --lab-run <class> to the jar build and the census gate's Maven run, the
# form for a pool host when this script is NOT already inside a lab-run grant (scripts/build-gate-jar.sh).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

die() { echo "[p2r-mvv] GATE ERROR: $*" >&2; exit 2; }

# -- Preflight: every dependency, before any scratch dir or build ---------------------------------------------------
for tool in uv java git; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not on PATH; this gate needs it and does not skip without it"
done
[ -f "$REPO_ROOT/scripts/build-gate-jar.sh" ] || die "scripts/build-gate-jar.sh is missing"
[ -f "$REPO_ROOT/tests/e2e/rdr229_p2r_mvv_checks.py" ] || die "tests/e2e/rdr229_p2r_mvv_checks.py is missing"
[ -f "$REPO_ROOT/tests/e2e/lib/rdr229_p2r_report.py" ] || die "tests/e2e/lib/rdr229_p2r_report.py is missing"

cd "$REPO_ROOT"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/nx-rdr229-p2r-mvv.XXXXXX")"
trap 'rm -rf "$SCRATCH"' EXIT
echo "[p2r-mvv] scratch: $SCRATCH (tree $(git rev-parse --short HEAD 2>/dev/null || echo nogit))"

# A scratch config dir, no ambient service endpoint, no Voyage key: the substrate is the local bge-768 engine.
export NEXUS_CONFIG_DIR="$SCRATCH/config"
mkdir -p "$NEXUS_CONFIG_DIR"
export NX_NO_TELEMETRY=1
unset NX_SERVICE_URL NX_SERVICE_TOKEN NX_SERVICE_HOST NX_SERVICE_PORT NEXUS_SERVICE_BIN NEXUS_SERVICE_JAR
unset VOYAGE_API_KEY NX_VOYAGE_API_KEY
unset NX_TEST_EXTERNAL_ENGINE_URL NX_TEST_EXTERNAL_TENANT_TOKEN
# Check 6 needs the engine's data-move worker: the substrate PG gets the product's own dml_utils bootstrap
# (pg_background, the library roles) before the engine migrates. Opt-in, read by tests/_engine_substrate.py only.
export NX_TEST_SUBSTRATE_DATA_MOVES=1

LAB_ARGS=()
[ -z "${NX_GATE_BUILD_LAB_CLASS:-}" ] || LAB_ARGS=(--lab-run "$NX_GATE_BUILD_LAB_CLASS")

# -- The service jar of this checkout, stamped (scripts/build-gate-jar.sh; cached on the build inputs) ---------------
echo "[p2r-mvv] building the stamped service jar"
"$REPO_ROOT/scripts/build-gate-jar.sh" ${LAB_ARGS[@]+"${LAB_ARGS[@]}"} >"$SCRATCH/build-jar.out" 2>&1 \
  || { tail -n 30 "$SCRATCH/build-jar.out" >&2; die "scripts/build-gate-jar.sh failed (output above)"; }
tail -n 3 "$SCRATCH/build-jar.out"

# -- Checks 1-4: one fixture tenant on the engine substrate -----------------------------------------------------------
RESULTS="$SCRATCH/results.jsonl"
JUNIT="$SCRATCH/mvv-junit.xml"
: > "$RESULTS"
echo "[p2r-mvv] checks 1-4 and 6 on the engine substrate"
set +e
NX_ALLOW_PROD_WRITE="rdr229-p2r-mvv: self-provisioned throwaway engine substrate, minted tenant, scratch config (nexus-a2qhz)" \
  P2R_RESULTS="$RESULTS" uv run pytest tests/e2e/rdr229_p2r_mvv_checks.py -p no:cacheprovider -q -rs --color=no \
  --junitxml="$JUNIT" 2>&1 | tee "$SCRATCH/mvv-pytest.out"
PYTEST_RC=${PIPESTATUS[0]}
set -e

# The substrate test must have run: one test, none skipped, none errored (an error is a setup failure, an
# infrastructure fault, not a FAIL of a check), and the fixture and the four checks must have recorded.
JUNIT_FACTS="$(uv run python -I - "$JUNIT" "$RESULTS" <<'PY'
import json, sys, xml.etree.ElementTree as ET
junit, results = sys.argv[1:3]
try:
    root = ET.parse(junit).getroot()
except Exception as exc:  # noqa: BLE001
    print(f"ERROR no readable junit file ({exc})"); sys.exit(0)
suite = root if root.tag == "testsuite" else root.find("testsuite")
tests, skipped, errors = (int(suite.get(k, 0)) for k in ("tests", "skipped", "errors"))
seen = sorted({json.loads(l)["n"] for l in open(results) if l.strip()})
if tests != 2 or skipped or errors or seen != [0, 1, 2, 3, 4, 6]:
    print(f"ERROR tests={tests} skipped={skipped} errors={errors} recorded={seen} (want 2 tests, 0 skipped, 0 errors, "
          "records for the fixture and checks 1-4 and 6)")
else:
    print("OK")
PY
)"
[ "$JUNIT_FACTS" = "OK" ] || die "the engine-substrate test did not run to the end: ${JUNIT_FACTS#ERROR } (pytest rc=$PYTEST_RC; output above)"

# -- Check 5: the two census gates ---------------------------------------------------------------------------------
echo "[p2r-mvv] check 5: the client census gate"
set +e
NX_TEST_T2_SUBSTRATE=none uv run pytest tests/test_collection_name_parse_census.py -m lint -p no:cacheprovider -q \
  --color=no --junitxml="$SCRATCH/client-census-junit.xml" >"$SCRATCH/client-census.out" 2>&1
CLIENT_RC=$?
set -e
tail -n 3 "$SCRATCH/client-census.out"

echo "[p2r-mvv] check 5: the engine census gate (CollectionParseGateTest)"
ENGINE_XML="$REPO_ROOT/service/target/surefire-reports/TEST-dev.nexus.service.vectors.CollectionParseGateTest.xml"
rm -f "$ENGINE_XML"
set +e
"$REPO_ROOT/scripts/mvnw-leased.sh" ${LAB_ARGS[@]+"${LAB_ARGS[@]}"} -B -q test -Dtest=CollectionParseGateTest \
  -Dsurefire.failIfNoSpecifiedTests=false >"$SCRATCH/engine-census.out" 2>&1
ENGINE_RC=$?
set -e
tail -n 8 "$SCRATCH/engine-census.out"

# tests run (not failed, not errored, not skipped); 0 when a run produced no report
gate_count() {  # <junit xml> <rc>
  uv run python -I - "$1" <<'PY'
import sys, xml.etree.ElementTree as ET
try:
    root = ET.parse(sys.argv[1]).getroot()
except Exception:  # noqa: BLE001
    print(0); sys.exit(0)
suite = root if root.tag == "testsuite" else root.find("testsuite")
# failures keep the count (rc says the gate is red); a skip or an error means the gate did not really run: 0
print(0 if int(suite.get("skipped", 0)) or int(suite.get("errors", 0)) else int(suite.get("tests", 0)))
PY
}
CLIENT_N="$(gate_count "$SCRATCH/client-census-junit.xml")"
ENGINE_N="$(gate_count "$ENGINE_XML")"

uv run python -I "$REPO_ROOT/tests/e2e/lib/rdr229_p2r_report.py" census >"$SCRATCH/census.json" \
  || die "the census record could not be built (the census module or the engine gate moved)"

# -- The report; its exit status is the gate's ---------------------------------------------------------------------
set +e
uv run python -I "$REPO_ROOT/tests/e2e/lib/rdr229_p2r_report.py" report "$RESULTS" "$SCRATCH/census.json" \
  "$CLIENT_RC" "$CLIENT_N" "$ENGINE_RC" "$ENGINE_N"
REPORT_RC=$?
set -e
exit "$REPORT_RC"
