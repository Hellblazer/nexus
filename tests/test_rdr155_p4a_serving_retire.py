# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-155 P4a.1 (bead nexus-655hc): serving-path Chroma retire — structural suite.

TDD-RED: the confinement tests fail until bead nexus-1k8s1 (P4a.2) reroutes T3
serving onto the pgvector service and carves the surviving ETL read leg out into
``src/nexus/migration/chroma_read.py``. The survival pins (chroma_quotas, skp06
absence) are GREEN by design — they guard against premature Phase-4b deletions
and against resurrecting the superseded app-layer guard.

Phase 4a contract (RDR-155 §Retire; plan nx_plan_audit 4a/4b split):

* **Serving constructions retired.** No live retrieval/storage/serving code in
  ``src/nexus`` constructs a Chroma ``PersistentClient`` or ``CloudClient``.
  The ONLY module allowed to construct them is the Phase-5 ETL read leg
  ``src/nexus/migration/chroma_read.py`` (local PersistentClient read +
  ChromaCloud read). ``storage_boundary_lint.py`` is additionally allowed to
  NAME the constructors — it is the enforcement machinery and must spell them.
* **Java serving wiring Chroma-free.** The service's serving surface
  (``VectorHandler``, ``NexusService``, ``Main``) routes vectors exclusively
  through ``PgVectorRepository``; the Java Chroma classes survive ONLY as the
  read-client machinery + test fixtures (P4b deletes them after P5.G).
* **chroma_quotas.py SURVIVES 4a.** It still governs the surviving read leg.
  Its deletion (and this pin's inversion) is Phase 4b (nexus-19svb/g37fr),
  gated on P5.G migration completion.
* **skp06 stays unbuilt.** The app-layer Chroma tenant guard was superseded by
  native FORCE RLS by tenant_id; nothing may reintroduce it.

T1 is OUT OF SCOPE (RDR-105: T1 stays on local chroma) — this suite scans only
the T3-scope client constructors (PersistentClient / CloudClient), never T1's
Ephemeral/Http clients.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "nexus"
SERVICE_MAIN = REPO_ROOT / "service" / "src" / "main" / "java" / "dev" / "nexus" / "service"

# The surviving Phase-5 ETL read leg — the ONE module allowed to construct
# T3-scope Chroma clients after the Phase-4a serving retire. P4a.2 creates it.
ETL_READ_LEG = SRC / "migration" / "chroma_read.py"

# storage_boundary_lint must NAME the banned constructors to enforce the ban.
_CONSTRUCTION_ALLOWED = {
    ETL_READ_LEG,
    SRC / "storage_boundary_lint.py",
}

# T3-scope Chroma client constructions (T1's Ephemeral/Http clients are out of
# RDR-155 scope). Matches both `chromadb.PersistentClient(` and bare
# `PersistentClient(` forms.
_T3_CLIENT_CONSTRUCTION = re.compile(
    r"\b(?:chromadb\s*\.\s*)?(PersistentClient|CloudClient)\s*\("
)

# Java serving-wiring files that must be Chroma-free after P4a.2. The Chroma
# classes themselves (VectorRepository.java, ChromaRestClient.java,
# LocalChromaServer.java, ChromaQuotaValidator.java) survive as the read-client
# machinery; the SERVING surface may not reference them.
_JAVA_SERVING_FILES = [
    SERVICE_MAIN / "http" / "VectorHandler.java",
    SERVICE_MAIN / "NexusService.java",
    SERVICE_MAIN / "Main.java",
]

# `(?<!Pg)VectorRepository` — PgVectorRepository is the pgvector path and is
# exactly what serving SHOULD reference.
_JAVA_CHROMA_TOKENS = re.compile(
    r"ChromaRestClient|LocalChromaServer|ChromaQuotaValidator|(?<!Pg)VectorRepository\b"
)


def _python_construction_sites() -> dict[Path, list[int]]:
    """All T3-scope Chroma client construction sites under src/nexus."""
    sites: dict[Path, list[int]] = {}
    for path in sorted(SRC.rglob("*.py")):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if _T3_CLIENT_CONSTRUCTION.search(line):
                sites.setdefault(path, []).append(lineno)
    return sites


class TestServingConstructionsRetired:
    """RED until P4a.2 — the serving paths still construct Chroma clients."""

    def test_t3_chroma_constructions_confined_to_etl_read_leg(self) -> None:
        sites = _python_construction_sites()
        offenders = {
            str(p.relative_to(REPO_ROOT)): lines
            for p, lines in sites.items()
            if p not in _CONSTRUCTION_ALLOWED
        }
        assert offenders == {}, (
            "T3-scope Chroma client constructions (PersistentClient/CloudClient) "
            "outside the Phase-5 ETL read leg — the Phase-4a serving retire "
            f"requires ALL serving to route through pgvector: {offenders}"
        )

    def test_etl_read_leg_module_exists(self) -> None:
        assert ETL_READ_LEG.is_file(), (
            f"{ETL_READ_LEG.relative_to(REPO_ROOT)} must exist: the minimal Chroma "
            "READ client (local PersistentClient read + ChromaCloud read) survives "
            "Phase 4a, reserved for the Phase-5 migration ETL"
        )

    def test_etl_read_leg_contains_both_read_legs(self) -> None:
        # The ETL needs BOTH legs (RDR §Migrate): local PersistentClient copy and
        # ChromaCloud REST/auth read. An ETL module with only one leg is a silent
        # half-migration.
        text = ETL_READ_LEG.read_text(encoding="utf-8")
        assert re.search(r"\bPersistentClient\s*\(", text), (
            "ETL read leg must retain the local PersistentClient read path"
        )
        assert re.search(r"\bCloudClient\s*\(", text), (
            "ETL read leg must retain the ChromaCloud read path"
        )


class TestJavaServingWiringChromaFree:
    """RED until P4a.2 — VectorHandler/NexusService/Main still wire Chroma."""

    def test_java_serving_files_reference_no_chroma_classes(self) -> None:
        offenders: dict[str, list[int]] = {}
        for path in _JAVA_SERVING_FILES:
            assert path.is_file(), f"serving file moved? {path}"
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if _JAVA_CHROMA_TOKENS.search(line):
                    offenders.setdefault(
                        str(path.relative_to(REPO_ROOT)), []
                    ).append(lineno)
        assert offenders == {}, (
            "Java SERVING wiring still references Chroma classes — after the "
            "Phase-4a cutover the serving surface routes exclusively through "
            f"PgVectorRepository: {offenders}"
        )


class TestPhase4aSurvivalPins:
    """GREEN by design — these guard against PREMATURE Phase-4b deletions."""

    def test_chroma_quotas_survives_phase_4a(self) -> None:
        quotas = SRC / "db" / "chroma_quotas.py"
        assert quotas.is_file(), (
            "chroma_quotas.py must SURVIVE Phase 4a — it still governs the "
            "surviving ETL read leg. Its deletion is Phase 4b (nexus-g37fr), "
            "gated on P5.G migration completion. If this test fails, a 4b "
            "deletion ran early."
        )

    def test_java_chroma_read_client_classes_survive_phase_4a(self) -> None:
        # The Java Chroma machinery survives 4a as the read client + the parity /
        # dual-run test comparand; P4b owns the deletion.
        for name in ("VectorRepository.java", "ChromaRestClient.java",
                     "LocalChromaServer.java", "ChromaQuotaValidator.java"):
            path = SERVICE_MAIN / "vectors" / name
            assert path.is_file(), (
                f"{name} must survive Phase 4a (read-client machinery + test "
                "comparand); full deletion is Phase 4b, gated on P5.G"
            )

    def test_skp06_app_layer_guard_stays_unbuilt(self) -> None:
        # The skp06 app-layer Chroma tenant guard was never built and is
        # superseded by native FORCE RLS by tenant_id (RDR-155 §Context).
        # Nothing may reintroduce it.
        offenders = [
            str(p.relative_to(REPO_ROOT))
            for p in sorted(SRC.rglob("*.py"))
            if re.search(r"skp06|chroma_tenant_guard", p.read_text(encoding="utf-8"))
        ]
        assert offenders == [], (
            f"the superseded skp06 app-layer Chroma tenant guard surfaced in: {offenders}"
        )
