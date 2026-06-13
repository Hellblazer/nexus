# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-159 .28 (bead nexus-ue6g7.28) — the epic-exit E2E oracle.

THE MINIMUM VIABLE VALIDATION (RDR-159 §Validation): a fresh checkout reaches a
served-on-pgvector state via ONE command (``run_guided_upgrade``) with zero
manual sequencing, verified for both upgrade paths, with the unsupported-model
case BLOCKED pre-migration and a forced failure recoverable via rollback.

Two layers, mirroring ``test_vector_etl.py`` (the hybrid design, RDR-159 P4
review):

* **Hermetic** (this module, default — CI-enforceable): drives the REAL
  ``run_guided_upgrade`` orchestration — real detection, real per-leg vector
  ETL over a real on-disk ``PersistentClient`` source, real validation
  arithmetic (counts + taxonomy + manifest-orphans) — with fakes ONLY at the
  service seam (``FakeVectorClient`` + a fake catalog client) and the T2 ladder
  represented by an injected clean report (``run_guided_upgrade`` does not
  expose ``run_t2``; its real ``migrate_all`` is a SQLite→PG-service ETL that
  needs the live stack). The model gate stays REAL so the unsupported-model
  block is genuinely exercised; quiescence is injected (environment-dependent,
  unit-covered by ``test_quiesce.py``).

  The cloud leg cannot run hermetically — ``open_read_legs`` / ``migrate_cloud``
  resolve the real ChromaCloud client from credentials. The cloud and two-leg
  scenarios back the "cloud" leg with a second on-disk ``PersistentClient``
  store injected at the documented seams, so the multi-leg composition
  (``_CompositeReadClient``) runs in CI without cloud creds.

* **Integration** (``@pytest.mark.integration`` — real Java service + Postgres +
  real ChromaCloud + Voyage): the literal "served-on-pgvector" MVV, run in the
  sandbox once the service-stack install story lands (release-blocker
  ``nexus-luxe6``). Skipped in CI.

Invariant asserted across ALL hermetic scenarios: the user never reaches a clean
``ok=True`` over an unvalidated or empty migration, and a block leaves rollback
OFFERED (never auto-invoked) with the Chroma source unmodified (copy-not-move).
Exact assertions (``== N``) throughout.

SCOPING (the "never a bare empty index" invariant): this oracle covers the
SENTINEL-WRITE end — a block leaves ``migrated-failed`` (degrade-LOUD), never a
cleared sentinel over moved data. The read-surface end (each MCP surface —
``search`` / ``store_get`` / ``store_get_many`` / plan runner / CLI — prepending
the degrade banner when the phase is ``migrating`` / ``migrated-failed``) is
owned by ``test_read_surface_degrade.py`` (in CI). The end-to-end composition is
sentinel-write (here) + banner-consumption (there); neither file alone proves
the full chain, and that split is deliberate (no duplication).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import chromadb
import pytest

from nexus.migration import driver
from nexus.migration import sequencer as _seq_mod
from nexus.migration.orchestrator import EtlSources
from nexus.migration.state import MIGRATED_FAILED, current_phase, read_state
from nexus.migration.vector_etl import (
    migrate_collections,
    rollback_collections,
)

# Reuse the locked copy-not-move ETL fakes + seeding helpers (single source of
# truth; a drift in the HttpVectorClient surface subset trips both suites).
from tests.migration.test_vector_etl import (  # noqa: PLC2701 — shared test fakes
    FakeVectorClient,
    _chash,
    _coll,
    _seed_source,
)

_MODEL_384 = "minilm-l6-v2-384"
_MODEL_768 = "bge-base-en-v15-768"  # unsupported: 768-dim, not a wired model
_MODEL_VOYAGE = "voyage-context-3"  # 1024-dim cloud model


# ── Fakes at the service seam ─────────────────────────────────────────────────


class _FakeCatalogClient:
    """Catalog-client stand-in for the manifest-orphan validation leg.

    ``build_manifest_orphan_check`` calls ``relation_counts`` (a zero/absent
    count raises ``ValidationCheckVacuous`` — a loud block), then
    ``manifest_backfill`` BEFORE ``manifest_orphans(dim)`` per dim. A non-zero
    ``doc_count`` with zero per-dim orphans is the clean migrated catalog.
    """

    def __init__(self, *, doc_count: int = 1, orphans_by_dim: dict[int, int] | None = None) -> None:
        self._doc_count = doc_count
        self._orphans = orphans_by_dim or {}
        self.backfill_calls = 0
        self.orphan_dim_queries: list[int] = []

    def relation_counts(self, relations: list[str]) -> dict[str, int]:
        return {r: self._doc_count for r in relations}

    def manifest_backfill(self) -> int:
        self.backfill_calls += 1
        return 0

    def manifest_orphans(self, dim: int) -> dict[str, int]:
        self.orphan_dim_queries.append(dim)
        return {"count": self._orphans.get(dim, 0)}


class _FakeReadCollection:
    """A Chroma-collection read stub: name + count + paged get (the subset the
    detection enumeration, the ETL read, and verify_counts touch)."""

    def __init__(self, name: str, rows: list[tuple[str, str, dict]]) -> None:
        self.name = name
        self._rows = rows

    def count(self) -> int:
        return len(self._rows)

    def get(
        self,
        ids: list[str] | None = None,
        where: dict | None = None,
        include: list[str] | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> dict:
        page = self._rows[offset : offset + limit]
        return {
            "ids": [r[0] for r in page],
            "documents": [r[1] for r in page],
            "metadatas": [r[2] for r in page],
        }


class _FakeReadStore:
    """In-memory Chroma read client for the CLOUD leg.

    The real cloud leg resolves ChromaCloud from credentials and can never run
    hermetically, so the cloud source is an injected store either way. A pure
    in-memory fake (rather than a second on-disk PersistentClient) also sidesteps
    the cross-client rust-binding invalidation that interleaving two real
    PersistentClients in one process triggers (the PersistentClient analog of
    the EphemeralClient shared-state trap)."""

    def __init__(self) -> None:
        self._cols: dict[str, _FakeReadCollection] = {}

    def seed(self, name: str, n: int, *, prefix: str = "cloud chunk") -> list[str]:
        texts = [f"{prefix} {i:04d}" for i in range(n)]
        ids = [_chash(t) for t in texts]
        self._cols[name] = _FakeReadCollection(
            name, [(ids[i], texts[i], {"position": i, "tag": "etl"}) for i in range(n)]
        )
        return ids

    def list_collections(self) -> list[_FakeReadCollection]:
        return list(self._cols.values())

    def get_collection(self, name: str) -> _FakeReadCollection:
        from chromadb.errors import NotFoundError

        if name not in self._cols:
            raise NotFoundError(f"collection {name!r} not found")
        return self._cols[name]


def _make_t2(path: Path, *, assignments: tuple[str, ...] = ()) -> Path:
    """Create a tmp T2 SQLite with a ``topic_assignments`` table.

    ``verify_taxonomy_consistency`` reads it ``mode=ro``; an empty table means
    no referenced source_collection, so the taxonomy floor is clean. Seed
    ``assignments`` with migrated collection names to keep it clean, or a
    non-migrated name to force a taxonomy block.
    """
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE topic_assignments (source_collection TEXT)")
        conn.executemany(
            "INSERT INTO topic_assignments(source_collection) VALUES (?)",
            [(a,) for a in assignments],
        )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The migration sentinel writes under the config dir — isolate it so a
    scenario's begin/clear/mark_failed never touches the real ~/.config/nexus
    and each test reads a fresh sentinel."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    return cfg


def _seed_local_store(path: Path, collections: dict[str, int]) -> dict[str, list[str]]:
    """Seed a real on-disk PersistentClient store; return chash ids per collection."""
    client = chromadb.PersistentClient(path=str(path))
    ids: dict[str, list[str]] = {}
    for name, n in collections.items():
        ids[name] = _seed_source(client, name, n)
    return ids


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    *,
    local_path: Path,
    vector_client: Any,
    catalog_client: Any,
    t2_path: Path,
    voyage_key_present: bool,
    cloud_store: Any | None = None,
    reopen_overrides: dict[str, Any] | None = None,
) -> driver.GuidedUpgradeResult:
    """Drive the REAL ``run_guided_upgrade`` with the T2 ladder + quiescence
    injected and (optionally) a real second store backing the cloud leg.

    The model gate stays REAL (the unsupported-model block must be genuine).
    """
    real_seq = _seq_mod.run_sequenced_migration

    def _seq(detection, **kw):  # type: ignore[no-untyped-def]
        return real_seq(
            detection,
            run_t2=lambda _sources: {"summary": {"total_failed": 0}},
            quiesce_check=lambda: None,
            **kw,
        )

    monkeypatch.setattr(driver, "run_sequenced_migration", _seq)

    # ALWAYS isolate detection from the real ChromaCloud: a credentialed dev env
    # would otherwise pull the operator's live cloud collections into the
    # footprint. The hermetic oracle's footprint is EXACTLY the seeded stores.
    local_client = chromadb.PersistentClient(path=str(local_path))

    def _open_read_legs(_lp: Any = None):  # type: ignore[no-untyped-def]
        return local_client, cloud_store

    def _migrate_cloud(vc: Any, on_result: Any = None):  # type: ignore[no-untyped-def]
        assert cloud_store is not None
        return migrate_collections(cloud_store, vc, leg="cloud", on_result=on_result)

    monkeypatch.setattr(driver, "open_read_legs", _open_read_legs)
    monkeypatch.setattr(driver, "migrate_cloud", _migrate_cloud)

    def _reopen(leg: str) -> Any:
        if reopen_overrides and leg in reopen_overrides:
            return reopen_overrides[leg]
        if leg == "cloud":
            assert cloud_store is not None
            return cloud_store
        # Mirror production (_default_reopen_leg): validation reopens a FRESH
        # local read client. The detection client was already closed before the
        # ETL; reusing it for validation only "works" by a chromadb refcount
        # accident (CRITICAL-1) and would flip every clean scenario to ok=False
        # the moment migrate_local closes its client properly.
        return chromadb.PersistentClient(path=str(local_path))

    reopen_leg = _reopen

    return driver.run_guided_upgrade(
        sources=EtlSources(sqlite_path=t2_path, catalog_db_path=t2_path),
        vector_client=vector_client,
        catalog_client=catalog_client,
        t2_db_path=t2_path,
        local_path=local_path,
        voyage_key_present=voyage_key_present,
        reopen_leg=reopen_leg,
    )


# ── Hermetic scenarios ────────────────────────────────────────────────────────


class TestHermeticOracle:
    def test_scenario2_local_only_onnx_unlocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LOCAL-ONLY/ONNX: NO key, minilm-384 collection, one command runs
        detect→migrate→verify→unlock; exact counts, sentinel cleared."""
        store = tmp_path / "chroma"
        name = _coll("oracle-local", model=_MODEL_384)
        ids = _seed_local_store(store, {name: 5})
        # Seed a taxonomy assignment that resolves to the migrated collection, so
        # the taxonomy floor runs NON-vacuously (referenced != {} AND maps to a
        # migrated collection -> clean, not clean-because-empty).
        t2 = _make_t2(tmp_path / "t2.db", assignments=(name,))
        vc = FakeVectorClient()
        cc = _FakeCatalogClient(doc_count=1, orphans_by_dim={384: 0})

        result = _drive(
            monkeypatch,
            local_path=store,
            vector_client=vc,
            catalog_client=cc,
            t2_path=t2,
            voyage_key_present=False,
        )

        assert result.ok is True
        assert result.validation is not None and result.validation.unlocked is True
        assert result.rollback_available is False
        # Exact copy: all 5 source chunks landed in the target collection.
        assert vc.count(name) == 5
        assert set(vc.store[name].keys()) == set(ids[name])
        # The migrated catalog was backfilled before the orphan scan, dim-scoped.
        assert cc.backfill_calls == 1
        assert cc.orphan_dim_queries == [384]
        # Sentinel cleared on a clean unlock (serving normal again).
        assert read_state() is None

    def test_scenario3_unsupported_model_blocks_pre_migration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """UNSUPPORTED bge-768: detected and BLOCKED by the real model gate
        BEFORE any ETL; NO data written, no validation, no clean ok."""
        store = tmp_path / "chroma"
        name = _coll("oracle-bge", model=_MODEL_768)
        _seed_local_store(store, {name: 4})
        t2 = _make_t2(tmp_path / "t2.db")
        vc = FakeVectorClient()
        cc = _FakeCatalogClient()

        result = _drive(
            monkeypatch,
            local_path=store,
            vector_client=vc,
            catalog_client=cc,
            t2_path=t2,
            voyage_key_present=False,
        )

        assert result.ok is False
        assert result.validation is None  # never reached validation
        # NO data touched — the block is pre-migration.
        assert vc.store == {}
        assert cc.backfill_calls == 0
        # The sentinel is migrated-failed (NOT a bare-empty index): the sequencer
        # sets it BEFORE the pre-gate so reads degrade-LOUD, and a model block
        # leaves it there with rollback offered. "No dead end" = recoverable
        # (fix the model + re-run, idempotent), not a cleared sentinel. This
        # matches the locked P2 contract (test_sequencer.py model-gate-block).
        assert current_phase() == MIGRATED_FAILED

    def test_scenario1_cloud_voyage_unlocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLOUD/Voyage: key present, voyage-1024 collection on the (injected)
        cloud leg, one command runs detect→migrate→verify→unlock."""
        local = tmp_path / "chroma-local"
        _seed_local_store(local, {})  # empty local leg
        cloud = _FakeReadStore()
        cname = _coll("oracle-cloud", model=_MODEL_VOYAGE)
        cloud_ids = cloud.seed(cname, 6)
        t2 = _make_t2(tmp_path / "t2.db")
        vc = FakeVectorClient()
        cc = _FakeCatalogClient(doc_count=1, orphans_by_dim={1024: 0})

        result = _drive(
            monkeypatch,
            local_path=local,
            vector_client=vc,
            catalog_client=cc,
            t2_path=t2,
            voyage_key_present=True,
            cloud_store=cloud,
        )

        assert result.ok is True
        assert result.validation is not None and result.validation.unlocked is True
        assert vc.count(cname) == 6
        assert set(vc.store[cname].keys()) == set(cloud_ids)
        assert cc.orphan_dim_queries == [1024]
        assert read_state() is None

    def test_scenario4_forced_failure_rollback_restores(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FORCED-FAILURE: the T3 copy lands but validation BLOCKS (manifest
        orphans); rollback is OFFERED (never auto-invoked). The user accepts it
        and is returned to a fully-working pre-upgrade state — pgvector emptied,
        the Chroma source intact (copy-not-move)."""
        store = tmp_path / "chroma"
        name = _coll("oracle-rb", model=_MODEL_384)
        ids = _seed_local_store(store, {name: 7})
        t2 = _make_t2(tmp_path / "t2.db")
        vc = FakeVectorClient()
        cc = _FakeCatalogClient(doc_count=1, orphans_by_dim={384: 2})

        result = _drive(
            monkeypatch,
            local_path=store,
            vector_client=vc,
            catalog_client=cc,
            t2_path=t2,
            voyage_key_present=False,
        )

        # Blocked, not unlocked; rollback offered; sentinel degraded-LOUD.
        assert result.ok is False
        assert result.validation is not None and result.validation.unlocked is False
        assert result.rollback_available is True
        assert result.validation.manifest_orphan_count == 2
        assert current_phase() == MIGRATED_FAILED  # never a bare empty index
        # The copy DID land (rollback has something to undo).
        assert vc.count(name) == 7

        # The user accepts the offered rollback (the engine never auto-invokes).
        read = chromadb.PersistentClient(path=str(store))
        rolled = rollback_collections(read, vc, collections=[name])
        assert rolled[name] == 7
        # Pre-upgrade state restored: pgvector emptied, Chroma source intact.
        assert vc.count(name) == 0
        assert read.get_collection(name).count() == 7
        source_ids = read.get_collection(name).get()["ids"]
        assert set(source_ids) == set(ids[name])

    def test_scenario5_two_leg_simultaneous_unlocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TWO-LEG SIMULTANEOUS (review SIG-3): local minilm-384 AND cloud
        voyage-1024 in ONE run; both legs migrate, validation counts both via
        the composite read client routing each collection to its source leg,
        and the run unlocks clean. Closes the only known multi-leg integration
        gap (unit-covered by test_driver.test_two_leg_composes...)."""
        local = tmp_path / "chroma-local"
        lname = _coll("oracle-2leg-local", model=_MODEL_384)
        lids = _seed_local_store(local, {lname: 3})
        cloud = _FakeReadStore()
        cname = _coll("oracle-2leg-cloud", model=_MODEL_VOYAGE)
        cids = cloud.seed(cname, 4)
        t2 = _make_t2(tmp_path / "t2.db")
        vc = FakeVectorClient()
        cc = _FakeCatalogClient(doc_count=2, orphans_by_dim={384: 0, 1024: 0})

        result = _drive(
            monkeypatch,
            local_path=local,
            vector_client=vc,
            catalog_client=cc,
            t2_path=t2,
            voyage_key_present=True,
            cloud_store=cloud,
        )

        assert result.ok is True
        assert result.validation is not None and result.validation.unlocked is True
        # Both legs migrated; exact per-collection counts via the composite read.
        assert vc.count(lname) == 3
        assert vc.count(cname) == 4
        assert set(vc.store[lname].keys()) == set(lids[lname])
        assert set(vc.store[cname].keys()) == set(cids)
        # Both dims validated for manifest orphans.
        assert sorted(cc.orphan_dim_queries) == [384, 1024]
        assert read_state() is None

    def test_scenario_c_mixed_models_blocks_on_voyage_subset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MIXED-MODELS (RDR §Test Plan (c)): ONE local store with both an
        onnx-384 AND a voyage-1024 collection, NO key. The pre-gate is
        all-or-nothing, so the whole run BLOCKS pre-migration (no data moved),
        and the diagnostic names ONLY the voyage subset as the blocker — the
        onnx collection is not at fault."""
        store = tmp_path / "chroma"
        onnx_name = _coll("oracle-mixed-onnx", model=_MODEL_384)
        voyage_name = _coll("oracle-mixed-voyage", model=_MODEL_VOYAGE)
        _seed_local_store(store, {onnx_name: 3, voyage_name: 2})
        t2 = _make_t2(tmp_path / "t2.db")
        vc = FakeVectorClient()
        cc = _FakeCatalogClient()

        result = _drive(
            monkeypatch,
            local_path=store,
            vector_client=vc,
            catalog_client=cc,
            t2_path=t2,
            voyage_key_present=False,  # voyage collection is unsupported here
        )

        assert result.ok is False
        assert result.validation is None
        assert vc.store == {}  # all-or-nothing: NOTHING migrated, not even onnx
        assert current_phase() == MIGRATED_FAILED
        # The diagnostic names only the voyage subset as the blocker.
        reason = result.sequence.blocked_reason or ""
        assert voyage_name in reason
        assert onnx_name not in reason


# ── Integration layer (real stack) — the literal served-on-pgvector MVV ───────


@pytest.mark.integration
class TestServedOnPgvectorMVV:
    """The literal RDR-159 §Validation MVV against the real stack: a real
    PG16 + Java service + ChromaCloud + Voyage reached via ONE command.

    Runs in the sandbox once the service-stack install story lands (release
    blocker ``nexus-luxe6``); excluded from CI (no service/keys). This registers
    the obligation rather than silently omitting it (the test_vector_etl
    deferred-obligation discipline). The hermetic layer above is the
    CI-enforceable gate; this is the end-to-end confirmation that the real
    service actually serves the migrated vectors on pgvector.
    """

    def test_served_on_pgvector_end_to_end(self, tmp_path: Path) -> None:
        """Drive the REAL guided upgrade end to end against a live service:
        seed a local ONNX store, run ``run_guided_upgrade`` with the real
        ``HttpVectorClient`` + catalog client (the exact wiring
        ``nx migrate-to-service`` uses), and assert the migrated chunks are
        SERVED on pgvector. A SINGLE conditional skip (no unconditional skip):
        the body is reachable the moment a credentialed service is present, so
        the obligation is genuinely deferred, not permanently inert."""
        import os

        token = os.environ.get("NX_SERVICE_TOKEN")
        if not token:
            pytest.skip(
                "served-on-pgvector MVV needs a live nexus-service "
                "(NX_SERVICE_TOKEN + reachable endpoint + Voyage key); run in "
                "the sandbox once nexus-luxe6's install story lands."
            )

        from nexus.catalog.factory import make_catalog_client_for_migration
        from nexus.db.http_vector_client import HttpVectorClient, _resolve_endpoint

        try:
            _resolve_endpoint()
        except RuntimeError as exc:
            pytest.skip(f"nexus-service endpoint unresolved: {exc}")

        store = tmp_path / "chroma"
        name = _coll("oracle-integration", model=_MODEL_384)
        ids = _seed_local_store(store, {name: 5})
        t2 = _make_t2(tmp_path / "t2.db", assignments=(name,))

        result = driver.run_guided_upgrade(
            sources=EtlSources(sqlite_path=t2, catalog_db_path=t2),
            vector_client=HttpVectorClient(),
            catalog_client=make_catalog_client_for_migration(token=token),
            t2_db_path=t2,
            local_path=store,
            voyage_key_present=False,
        )

        assert result.ok is True
        assert result.validation is not None and result.validation.unlocked is True
        # The literal MVV: the migrated chunks are SERVED on pgvector.
        served = HttpVectorClient()
        assert served.count(name) == 5
        assert len(ids[name]) == 5
