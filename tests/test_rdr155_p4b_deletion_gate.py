# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-155 P4b deletion gate — nexus-19svb (P1 RED suite).

The executable specification of the 7.0.0-wave P2 deletion set (client +
Java engine), written RED before the deletions and landing GREEN with them
in the same branch/PR/CI cycle (critique [21095] Critical 4).

Authority for the lists below (never the stale 2026-06 bead prose):
  - T2 nexus/p4b-scope-reconciliation-2026-07-23  [21094]  (Python/Chroma)
  - T2 nexus/p4b-java-engine-partition-2026-07-23 [21096]  (Java engine)
  - T2 nexus/p4b-sqlite-partition-2026-07-23      [21098]  (combined wave)
  - nexus-g37fr PLAN v4 + locked decisions D-A..D-D

Assertion strategy (19svb 2026-07-23 correction):
  - PATH-absence for whole-file deletes (after the P0e rehomes, that is
    nearly everything).
  - SYMBOL presence for the survivors that absorbed rehomed code, so an
    implementer can neither false-green by token-grep nor "fix" the gate
    by deleting a file with live consumers.
  - Token inverse-greps scoped to what THIS branch retires. The broad
    "chromadb is gone" sweep (pyproject drop, storage_mode collapse,
    census assert-EMPTY) is P3's extension of this suite, not P2's.

Exemption classes (persisted-data / heritage, locked in [21094]/[21096]):
chroma:// URI literals (aspect_readers.py, ChromaSchemeHandler.java —
persisted catalog data, forever), stranded_install.py filename strings,
Liquibase changelog comments (checksum-locked), OnnxEmbedder's
~/.cache/chroma artifact path, db/t3.py + chroma_quotas.py (die at P3
with the chromadb dependency, not at P2).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "nexus"
TESTS = REPO_ROOT / "tests"
SERVICE_MAIN = REPO_ROOT / "service" / "src" / "main" / "java" / "dev" / "nexus" / "service"
SERVICE_TEST = REPO_ROOT / "service" / "src" / "test" / "java" / "dev" / "nexus" / "service"

GATE_FILE = Path(__file__).resolve()

# ---------------------------------------------------------------------------
# 1. Whole-file deletes — src (Python)
# ---------------------------------------------------------------------------

DELETED_SRC = [
    # migration/ Chroma legs ([21094])
    "migration/chroma_read.py",
    "migration/collision_audit.py",
    "migration/driver.py",
    "migration/sequencer.py",
    "migration/staging_land.py",
    "migration/pregate.py",
    # migration machinery double-kill ([21098] §2)
    "migration/orchestrator.py",
    "migration/etl_registry.py",
    "migration/migration_report.py",
    "migration/quiesce.py",
    "migration/validation.py",
    "migration/manifest_check.py",
    "migration/verify_fill.py",
    "migration/verify_fill_watermark.py",
    "migration/wire_reid.py",
    "migration/remap_cascade.py",
    "migration/chash_disposition.py",
    # whole-file after the P0e rehomes (reconcile / provisioning / rekey split)
    "migration/vector_etl.py",
    "migration/detection.py",
    "migration/guided_upgrade.py",
    "migration/remap_client.py",
    "migration/etl_ports.py",
    # commands
    "commands/storage_cmd.py",
    "commands/migrate_cmd.py",
    "commands/guided_upgrade_cmd.py",
    "commands/migration_audit_cmd.py",
    "commands/_provision.py",
    # ladder chroma/sqlite rungs (Hal decision 2, D-A..D-D lock)
    "upgrade_ladder/rungs/t2_schema.py",
    "upgrade_ladder/rungs/substrate_etl.py",
    "upgrade_ladder/census.py",
    # daemon + mcp T1/T3 chroma machinery
    "daemon/t3_daemon.py",
    "mcp/_t1_state.py",
]

# Modules whose import anywhere in src/ or tests/ is a regression.
DELETED_MODULE_IMPORTS = [
    "nexus.migration.chroma_read",
    "nexus.migration.collision_audit",
    "nexus.migration.driver",
    "nexus.migration.sequencer",
    "nexus.migration.staging_land",
    "nexus.migration.pregate",
    "nexus.migration.orchestrator",
    "nexus.migration.etl_registry",
    "nexus.migration.migration_report",
    "nexus.migration.quiesce",
    "nexus.migration.validation",
    "nexus.migration.manifest_check",
    "nexus.migration.verify_fill",
    "nexus.migration.verify_fill_watermark",
    "nexus.migration.wire_reid",
    "nexus.migration.remap_cascade",
    "nexus.migration.chash_disposition",
    "nexus.migration.vector_etl",
    "nexus.migration.detection",
    "nexus.migration.guided_upgrade",
    "nexus.migration.remap_client",
    "nexus.migration.etl_ports",
    "nexus.commands.storage_cmd",
    "nexus.commands.migrate_cmd",
    "nexus.commands.guided_upgrade_cmd",
    "nexus.commands.migration_audit_cmd",
    "nexus.commands._provision",
    "nexus.upgrade_ladder.rungs.t2_schema",
    "nexus.upgrade_ladder.rungs.substrate_etl",
    "nexus.upgrade_ladder.census",
    "nexus.daemon.t3_daemon",
    "nexus.mcp._t1_state",
]

# ---------------------------------------------------------------------------
# 2. Whole-file deletes — tests (the unambiguous dying set; judgment-call
#    files re-ground instead and are deliberately NOT pinned here)
# ---------------------------------------------------------------------------

DELETED_TESTS = [
    "migration/test_chash_disposition.py",
    "migration/test_chroma_read.py",
    "migration/test_collision_audit.py",
    "migration/test_detection.py",
    "migration/test_driver.py",
    "migration/test_e2e_oracle.py",
    "migration/test_guided_upgrade_already_migrated.py",
    "migration/test_guided_upgrade_preflight.py",
    "migration/test_manifest_check.py",
    "migration/test_migration_contract.py",
    "migration/test_migration_report.py",
    "migration/test_orchestrator.py",
    "migration/test_pending_migration_notice.py",
    "migration/test_pregate.py",
    "migration/test_quiesce.py",
    "migration/test_rdr176_p2_verify.py",
    "migration/test_rdr176_p3_batch_conformance.py",
    "migration/test_rdr176_p5_obs.py",
    "migration/test_rdr176_p5_retry.py",
    "migration/test_rdr178_acceptance.py",
    "migration/test_rdr178_gap3_circuit_breaker.py",
    "migration/test_sequencer.py",
    "migration/test_staging_land.py",
    "migration/test_validation.py",
    "migration/test_vector_etl.py",
    "migration/test_verify_fill_cli.py",
    "migration/test_verify_fill_inner.py",
    "migration/test_verify_fill_outer.py",
    "migration/test_verify_fill_regression.py",
    "migration/test_verify_fill_watermark.py",
    "migration/test_verify_fill_wiring.py",
    "upgrade/test_etl_seam.py",
    "upgrade/test_legacy_census.py",
    "upgrade/test_p2_integration.py",
    "upgrade/test_substrate_leg.py",
    "upgrade/test_substrate_rung.py",
    "upgrade/test_t2_schema_rung.py",
    "upgrade/test_wire_reid.py",
    "upgrade/test_remap_cascade.py",
    "test_storage_migrate_vectors_cmd.py",
    "test_provision.py",
    "test_session_sweep_orphan_t1_chromadbs.py",
    "stress/test_t3_daemon_stress.py",
]

# Dev/spike scripts retired with the wave (19svb 2026-07-23 disposition:
# both construct EphemeralClient outside the grep boundary; the spike's
# detection target no longer exists in src/).
DELETED_SCRIPTS = [
    "scripts/rdr092_replay.py",
    "scripts/spikes/spike_rdr094_b_subagent_race.py",
]

# ---------------------------------------------------------------------------
# 3. Java engine ([21096])
# ---------------------------------------------------------------------------

DELETED_JAVA_MAIN = [
    "vectors/LocalChromaServer.java",
    "vectors/ChromaRestClient.java",
    "vectors/VectorRepository.java",
    "vectors/ChromaQuotaValidator.java",
    "http/MigrationHandler.java",
    "db/MigrationJobRepository.java",
]

DELETED_JAVA_TEST = [
    "vectors/VectorIntegrationTest.java",
    "vectors/DualRunHarnessIntegrationTest.java",
    "vectors/HybridParityIntegrationTest.java",
    "http/MigrationHandlerIngestCloudTest.java",
]

# Tokens that must not appear in service main source post-deletion.
JAVA_FORBIDDEN_TOKENS = ["api.trychroma.com", "chroma run", "NX_CHROMA_BINARY"]

# ---------------------------------------------------------------------------
# 4. Bridge artifacts (nexus-0rwwv sweep — die with guided_upgrade/detection)
# ---------------------------------------------------------------------------

BRIDGE_TOKENS = [
    "pending_migration_notice",
    "legacy_footprint_pending",
    "endpoint_failure_migration_hint",
    "NX_MIGRATION_NOTICE",
]


def _py_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts and p != GATE_FILE]


def test_deleted_src_modules_absent_by_path() -> None:
    present = [rel for rel in DELETED_SRC if (SRC / rel).exists()]
    assert present == [], (
        "P2 whole-file deletes still present under src/nexus/ "
        f"({len(present)} of {len(DELETED_SRC)}): {present}"
    )


def test_dying_test_files_absent_by_path() -> None:
    present = [rel for rel in DELETED_TESTS if (TESTS / rel).exists()]
    assert present == [], (
        f"dying test files still present under tests/ ({len(present)}): {present}"
    )


def test_retired_spike_scripts_absent() -> None:
    present = [rel for rel in DELETED_SCRIPTS if (REPO_ROOT / rel).exists()]
    assert present == [], f"retired dev/spike scripts still present: {present}"


def test_ingest_cloud_gate_retired() -> None:
    # The standalone RDR-176 live gate can never pass once the engine's
    # /v1/migration/ingest-cloud route is deleted ([21096] J4).
    assert not (REPO_ROOT / "tests" / "e2e" / "ingest_cloud_gate.py").exists(), (
        "tests/e2e/ingest_cloud_gate.py must retire with the ingest-cloud endpoint"
    )


def test_no_imports_of_deleted_modules() -> None:
    """No import statement anywhere in src/ or tests/ names a deleted module.

    Matches import STATEMENTS (including deferred function-local ones),
    not prose mentions in docstrings/comments.
    """
    pattern = re.compile(
        r"^\s*(?:from|import)\s+("
        + "|".join(re.escape(m) for m in DELETED_MODULE_IMPORTS)
        + r")\b",
        re.MULTILINE,
    )
    offenders: dict[str, list[str]] = {}
    for path in _py_files(SRC) + _py_files(TESTS):
        hits = pattern.findall(path.read_text(encoding="utf-8"))
        if hits:
            offenders[str(path.relative_to(REPO_ROOT))] = sorted(set(hits))
    assert offenders == {}, f"imports of deleted modules remain: {offenders}"


def test_bridge_artifact_tokens_gone_from_src() -> None:
    # nexus-0rwwv: the substrate-migration bridge lives OUTSIDE
    # migration/ (upgrade banner chain, doctor row, SessionStart hook,
    # endpoint-failure hints) and does not die with the module deletion.
    offenders: dict[str, list[str]] = {}
    for path in _py_files(SRC):
        text = path.read_text(encoding="utf-8")
        hits = [t for t in BRIDGE_TOKENS if t in text]
        if hits:
            offenders[str(path.relative_to(REPO_ROOT))] = hits
    assert offenders == {}, f"bridge-artifact tokens remain in src/: {offenders}"


def test_cli_registrations_gone() -> None:
    cli_text = (SRC / "cli.py").read_text(encoding="utf-8")
    for token in ("storage_cmd", "migrate_cmd", "guided_upgrade_cmd", "migration_audit_cmd"):
        assert token not in cli_text, f"cli.py still references {token}"


def test_health_has_no_chromadb_import_or_dead_checks() -> None:
    text = (SRC / "health.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(?:from|import)\s+chromadb\b", text, re.MULTILINE), (
        "health.py still imports chromadb (top-level import was line 18 pre-P2)"
    )
    for dead in ("_check_legacy_id_census", "_check_t3_daemon_version", "_check_migration_reports"):
        assert dead not in text, f"health.py still carries {dead}"


def test_t3_daemon_discovery_gone() -> None:
    offenders = [
        str(p.relative_to(REPO_ROOT))
        for p in _py_files(SRC)
        if "find_t3_daemon" in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"find_t3_daemon still referenced: {offenders}"


# ---------------------------------------------------------------------------
# Survivor pins — the gate is two-sided: deleting a file with live
# consumers to satisfy path-absence must fail HERE.
# ---------------------------------------------------------------------------


def _module_defs(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def test_survivor_symbols_present() -> None:
    expectations = {
        SRC / "db" / "reconcile.py": {
            "iter_collection_chunks",       # nexus-jg74b rehome
            "list_collection_names",
            "verify_fill_collections",      # P0e rehome
            "verify_fill_pg_source",
            "resolve_local_service_endpoint",
        },
        SRC / "upgrade_ladder" / "provisioning.py": {
            "establish_verified_service",   # P0e rehome (D-C)
            "provision_and_serve",
            "wait_for_service_health",
            "verify_service_version",
            "verify_voyage_capability",
        },
        SRC / "db" / "t2" / "rekey_client.py": {
            "HttpRekeyClient",              # P0e rekey split (D-D)
        },
    }
    for path, symbols in expectations.items():
        assert path.exists(), f"survivor module missing: {path.relative_to(REPO_ROOT)}"
        missing = symbols - _module_defs(path)
        assert missing == set(), (
            f"{path.relative_to(REPO_ROOT)} lost rehomed survivor symbols: {sorted(missing)}"
        )


def test_surviving_migration_modules_present() -> None:
    # The migration PACKAGE survives the wave (state sentinel, banner,
    # pg-source read leg, hidden state verbs). Deleting the package
    # wholesale per the STALE 2026-06 bead text must fail here.
    for rel in ("state.py", "banner.py", "pg_read.py", "__init__.py"):
        assert (SRC / "migration" / rel).exists(), f"migration/{rel} must survive P2"
    for rel in ("migration_cmd.py",):
        assert (SRC / "commands" / rel).exists(), f"commands/{rel} must survive P2"


def test_ladder_registry_is_rekey_only() -> None:
    text = (SRC / "upgrade_ladder" / "registry.py").read_text(encoding="utf-8")
    assert "T2SchemaRung" not in text, "registry still wires T2SchemaRung"
    assert "SubstrateEtlRung" not in text, "registry still wires SubstrateEtlRung"
    assert "default_chash_rekey_rung" in text, (
        "chash-rekey rung must SURVIVE (D-D) — the ladder is RDR-185's standing "
        "convergence mechanism, not migration plumbing"
    )


def test_preconditions_census_leg_excised() -> None:
    text = (SRC / "upgrade_ladder" / "preconditions.py").read_text(encoding="utf-8")
    assert "_chroma_footprint_present" not in text, (
        "preconditions.py still consumes the deleted census chroma gate"
    )


# ---------------------------------------------------------------------------
# Java engine assertions ([21096] P2-J)
# ---------------------------------------------------------------------------


def test_java_dying_files_absent() -> None:
    present = [rel for rel in DELETED_JAVA_MAIN if (SERVICE_MAIN / rel).exists()]
    present += [f"test:{rel}" for rel in DELETED_JAVA_TEST if (SERVICE_TEST / rel).exists()]
    assert present == [], f"dying Java files still present: {present}"


def test_java_main_has_no_chroma_client_tokens() -> None:
    offenders: dict[str, list[str]] = {}
    for path in SERVICE_MAIN.rglob("*.java"):
        text = path.read_text(encoding="utf-8")
        hits = [t for t in JAVA_FORBIDDEN_TOKENS if t in text]
        if hits:
            offenders[path.name] = hits
    assert offenders == {}, f"chroma client tokens remain in service main: {offenders}"


def test_java_survivors_present() -> None:
    # Persisted-data exemption class: the chroma:// URI resolver survives
    # forever (the scheme literal is persisted catalog data), as does the
    # MiniLM embedder the default test suite depends on.
    for rel in (
        "resolver/ChromaSchemeHandler.java",
        "vectors/OnnxEmbedder.java",
        "vectors/PgVectorRepository.java",
    ):
        assert (SERVICE_MAIN / rel).exists(), f"Java survivor missing: {rel}"


def test_nexus_service_migration_wiring_excised() -> None:
    text = (SERVICE_MAIN / "NexusService.java").read_text(encoding="utf-8")
    assert "MigrationJobRepository" not in text, (
        "NexusService still wires MigrationJobRepository"
    )
    assert "/v1/migration" not in text, (
        "NexusService still registers the /v1/migration context"
    )
