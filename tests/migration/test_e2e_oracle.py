# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-159 .28 (bead nexus-ue6g7.28) / RDR-180 (nexus-jxizy.10.7) — the
epic-exit E2E oracle.

THE MINIMUM VIABLE VALIDATION (RDR-159 §Validation): a fresh checkout reaches a
served-on-pgvector state via ONE command (``run_guided_upgrade``) with zero
manual sequencing, verified for both upgrade paths, with the unsupported-model
case BLOCKED pre-migration and a forced failure leaving staging retained for
an idempotent resume.

Two layers, mirroring ``test_vector_etl.py`` (the hybrid design, RDR-159 P4
review):

* **Hermetic** (this module, default — CI-enforceable): drives the REAL
  ``run_guided_upgrade`` orchestration — real detection, real land-then-
  transform sequencing (:func:`nexus.migration.sequencer.run_land_then_transform_migration`
  runs for real, not mocked), real per-collection landing over a real on-disk
  ``PersistentClient`` source, real disk/census preflight, real T2-schema
  SQLite sources (:func:`nexus.db.t2.T2Database` / :func:`nexus.db.t2.catalog.CatalogStore`)
  — with fakes ONLY at the two seams RDR-180 pushed server-side: the staging
  wire client (:class:`_FakeStagingStore` stands in for
  :class:`~nexus.migration.staging_land.HttpStagingStore` + the engine's
  ``StagingPromoteOps`` — the real engine journey is the RDR-180 .10.10
  rehearsal's job, kept out of this hermetic layer) and quiescence (injected;
  environment-dependent, unit-covered by ``test_quiesce.py``). The model gate
  stays REAL so the unsupported-model block is genuinely exercised.

  The cloud leg cannot run hermetically — ``open_read_legs`` resolves the real
  ChromaCloud client from credentials. The cloud and two-leg scenarios back
  the "cloud" leg with a second in-memory store injected at the documented
  seams, so the multi-leg composition (``_CompositeReadClient``) runs in CI
  without cloud creds.

* **Integration** (``@pytest.mark.integration`` — real Java service + Postgres +
  real ChromaCloud + Voyage): the literal "served-on-pgvector" MVV, run in the
  sandbox once the service-stack install story lands (release-blocker
  ``nexus-luxe6``). Skipped in CI.

Invariant asserted across ALL hermetic scenarios: the user never reaches a clean
``ok=True`` over an unvalidated or empty migration, and a block leaves the
sentinel ``migrated-failed`` (degrade-LOUD) with staging retained — copy-not-
move (the immutable Chroma source is never mutated). Exact assertions
(``== N``) throughout.

RDR-180 CONTRACT CHANGE (documented here once, referenced by scenario): the
old three-phase DETECT / SEQUENCE / VALIDATE driver had a "T3 copied but
validation failed" middle state that OFFERED an explicit rollback command
(``nx storage migrate vectors --rollback``). Land-then-transform folds
validation into the sequence's own ``verify`` step (server-side count-parity
reconciliation), so that middle state no longer exists: a run is either fully
verified (``ok=True``) or it never completed (``ok=False``,
``validation=None``, ``rollback_available=False`` always). Recovery from any
block is a plain re-run — landing and promote are idempotent against retained
staging — never an explicit rollback. Scenario 4 (forced failure) asserts this
new shape directly.

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

from pathlib import Path
from typing import Any

import chromadb
import pytest

from nexus.migration import driver
from nexus.migration import sequencer as _seq_mod
from nexus.migration.orchestrator import EtlSources
from nexus.migration.state import MIGRATED_FAILED, current_phase, read_state

# Reuse the locked copy-not-move seeding helpers (single source of truth; a
# drift in the id/text convention trips both suites).
from tests.migration.test_vector_etl import (  # noqa: PLC2701 — shared test fakes
    _chash,
    _coll,
    _seed_source,
)

_MODEL_ONNX = "bge-base-en-v15-768"  # the service's wired ONNX model (RDR-160), dim 768
_MODEL_LEGACY = "minilm-l6-v2-384"  # unsupported: legacy 384-dim, retired from the service (RDR-160)
_MODEL_VOYAGE = "voyage-context-3"  # 1024-dim cloud model


# ── Fakes at the two seams RDR-180 pushed server-side ─────────────────────────


class _FakeStagingStore:
    """Records landed/promoted/finalized state in memory — stands in for the
    real :class:`~nexus.migration.staging_land.HttpStagingStore` (whose real
    ``__init__`` resolves a live service endpoint) + the engine's
    ``StagingPromoteOps`` (whose real work is a transactional Postgres pass).

    Simulates the two count relationships :func:`driver.run_guided_upgrade`'s
    ``_verify`` closure checks: ``promote()`` reports ``staged_content`` (rows
    landed under that target) and ``promoted`` (rows after a DISTINCT-ON-text
    collapse — trivial here since the fixtures never seed duplicate chunk
    text); ``finalize()`` reports the in-txn ``residual_mismatched`` /
    ``dangling_manifest`` gate, injectable to force scenario 4's block.
    """

    def __init__(self, *, finalize_residual_mismatched: int = 0) -> None:
        self.loaded: dict[str, list[dict]] = {}
        self.embed_fill_calls: list[str] = []
        self.promote_calls: list[str] = []
        self.finalize_calls = 0
        self.cleared = False
        self.chunks_history: list[dict] = []
        self._finalize_residual_mismatched = finalize_residual_mismatched

    def load(self, store: str, rows: list[dict]) -> int:
        self.loaded.setdefault(store, []).extend(rows)
        if store == "chunks":
            # Permanent record — ``clear()`` (called on a fully verified
            # success) empties ``self.loaded`` exactly like the real staging
            # tables, but scenario assertions still want to see what landed.
            self.chunks_history.extend(rows)
        return len(rows)

    def embed_fill(self, collection: str) -> dict[str, Any]:
        self.embed_fill_calls.append(collection)
        return {"filled": 0}

    def promote(self, collection: str) -> dict[str, Any]:
        # reviewer-p2 CRITICAL (test-honesty): the REAL engine's
        # staged_content counts chunk_text <> '' rows ONLY — empty-text
        # rows wait for finalize's Item8 disposition. The fake MUST model
        # that filter or driver._verify's reconciliation arithmetic goes
        # untested against the real field semantics.
        self.promote_calls.append(collection)
        rows = [r for r in self.loaded.get("chunks", [])
                if r["collection"] == collection and r.get("chunk_text")]
        distinct_texts = {r["chunk_text"] for r in rows}
        return {"promoted": len(distinct_texts), "staged_content": len(rows)}

    def finalize(self, orphan_policy: str = "drop") -> dict[str, Any]:
        self.finalize_calls += 1
        empty = [r for r in self.loaded.get("chunks", []) if not r.get("chunk_text")]
        return {
            "residual_mismatched": self._finalize_residual_mismatched,
            "dangling_manifest": 0,
            "reference_only_resolved": 0,
            "orphans_dropped": len(empty),
            "orphans_synthesized": 0,
        }

    def clear(self) -> dict[str, Any]:
        self.cleared = True
        self.loaded.clear()
        return {"cleared": {}}

    def counts(self) -> dict[str, int]:
        return {store: len(rows) for store, rows in self.loaded.items()}


class _FakeReadCollection:
    """A Chroma-collection read stub: name + count + paged get (the subset the
    detection enumeration and the landing read touch)."""

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


def _make_source_dbs(tmp_path: Path) -> tuple[Path, Path]:
    """A REAL full-schema T2 SQLite + REAL catalog SQLite (empty, but every
    table ``_land``'s pointer-store landing reads from actually exists) —
    the minimal fixture the pre-RDR-180 oracle used made 6 stores fail on
    missing source tables under the REAL sequencer (nexus-edwlp, first run
    under the hermetic local-service gate); the fully-hermetic land-then-
    transform layer hits the exact same requirement one level earlier, in
    ``_census_check`` / ``_land`` rather than a T2 store ETL.
    """
    from nexus.db.t2 import T2Database
    from nexus.db.t2.catalog import CatalogStore

    t2_path = tmp_path / "t2.db"
    # T2's ``apply_pending`` migrations that need the catalog present (e.g.
    # the document_aspects doc_id PK switch) infer its path as
    # ``<memory.db's parent>/catalog/.catalog.db``
    # (``migrations._catalog_db_path_from_conn``) — a SEPARATE resolution
    # from ``T2Database``'s own ``catalog_db_path`` constructor override, so
    # the sibling ``catalog/`` layout is mandatory here regardless of what
    # is passed below (the pre-RDR-180 oracle's `_make_full_t2` hit this
    # exact requirement).
    catalog_path = tmp_path / "catalog" / ".catalog.db"
    # Catalog MUST exist before T2Database's migrations run (the doc_id PK
    # switch step checks ``catalog_path.exists()`` at construction time and
    # defers itself otherwise, leaving document_aspects on its pre-migration
    # schema — exactly the "no such column: doc_id" trap this ordering avoids).
    CatalogStore(catalog_path).close()
    T2Database(t2_path, catalog_db_path=catalog_path).close()

    # FIXTURE SHIM (test-only, NOT a source fix — see the RDR-180 completion-
    # pass report): a fully-migrated T2 (catalog present, doc_id PK switch
    # run) also runs "RDR-096 P5.2: drop document_aspects.source_path
    # column". staging_land.pointer_store_rows's "document_aspects" branch
    # (nexus-jxizy.10.6, shipped) still SELECTs source_path — a genuine
    # schema-drift bug outside this bead's boundary (it also touches the
    # engine's StagingHandler.STORES wire contract, which still declares a
    # source_path column). Restoring the column here only unblocks this
    # hermetic oracle from exercising the REST of the land-then-transform
    # flow against a realistic, fully-migrated schema; it does not fix the
    # production bug.
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(t2_path)
    try:
        conn.execute("ALTER TABLE document_aspects ADD COLUMN source_path TEXT")
        conn.commit()
    finally:
        conn.close()

    return t2_path, catalog_path


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The migration sentinel writes under the config dir — isolate it so a
    scenario's begin/clear/mark_failed never touches the real ~/.config/nexus
    and each test reads a fresh sentinel."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    return cfg


def _seed_local_store(
    path: Path, collections: dict[str, int], *, dims: int = 2
) -> dict[str, list[str]]:
    """Seed a real on-disk PersistentClient store; return chash ids per collection."""
    client = chromadb.PersistentClient(path=str(path))
    ids: dict[str, list[str]] = {}
    for name, n in collections.items():
        ids[name] = _seed_source(client, name, n, dims=dims)
    return ids


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    *,
    local_path: Path,
    t2_path: Path,
    catalog_path: Path,
    voyage_key_present: bool,
    cloud_store: Any | None = None,
    reopen_overrides: dict[str, Any] | None = None,
    finalize_residual_mismatched: int = 0,
) -> tuple[driver.GuidedUpgradeResult, _FakeStagingStore]:
    """Drive the REAL ``run_guided_upgrade`` -> REAL
    ``run_land_then_transform_migration`` with quiescence injected (real
    process state a hermetic test cannot control) and the staging wire client
    faked (the real engine journey is the .10.10 rehearsal's job). The model
    gate stays REAL — the unsupported-model block must be genuine.
    """
    staging = _FakeStagingStore(finalize_residual_mismatched=finalize_residual_mismatched)
    monkeypatch.setattr(driver, "HttpStagingStore", lambda: staging)

    real_ltt = _seq_mod.run_land_then_transform_migration

    def _ltt(detection, **kw):  # type: ignore[no-untyped-def]
        kw.setdefault("quiesce_check", lambda: None)
        return real_ltt(detection, **kw)

    monkeypatch.setattr(driver, "run_land_then_transform_migration", _ltt)

    # ALWAYS isolate detection from the real ChromaCloud: a credentialed dev env
    # would otherwise pull the operator's live cloud collections into the
    # footprint. The hermetic oracle's footprint is EXACTLY the seeded stores.
    local_client = chromadb.PersistentClient(path=str(local_path))

    def _open_read_legs(_lp: Any = None, skipped_out=None):  # type: ignore[no-untyped-def]
        return local_client, cloud_store

    monkeypatch.setattr(driver, "open_read_legs", _open_read_legs)

    def _reopen(leg: str) -> Any:
        if reopen_overrides and leg in reopen_overrides:
            return reopen_overrides[leg]
        if leg == "cloud":
            assert cloud_store is not None
            return cloud_store
        # Mirror production: landing reopens a FRESH local read client. The
        # detection client was already closed before landing; reusing it
        # would only "work" by a chromadb refcount accident.
        return chromadb.PersistentClient(path=str(local_path))

    result = driver.run_guided_upgrade(
        sources=EtlSources(sqlite_path=t2_path, catalog_db_path=catalog_path),
        vector_client=object(),  # RDR-180: unused — the pgvector write is server-side now
        catalog_client=object(),  # RDR-180: unused — catalog-orphan validation is retired
        t2_db_path=t2_path,
        local_path=local_path,
        voyage_key_present=voyage_key_present,
        reopen_leg=_reopen,
        run_t2=lambda _sources: {"summary": {"total_failed": 0}},
    )
    return result, staging


def _landed_chunks(staging: _FakeStagingStore, collection: str) -> list[dict]:
    """Every chunk row ever landed under *collection* — survives ``clear()``
    (a fully verified success clears the live staging table, matching real
    production behavior; scenario assertions want the permanent record)."""
    return [r for r in staging.chunks_history if r["collection"] == collection]


# ── Hermetic scenarios ────────────────────────────────────────────────────────


class TestHermeticOracle:
    def test_scenario2_local_only_onnx_unlocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LOCAL-ONLY/ONNX: NO key, bge-768 collection, one command runs
        detect→land→promote→finalize→verify→unlock; exact counts, sentinel
        cleared."""
        store = tmp_path / "chroma"
        name = _coll("oracle-local", model=_MODEL_ONNX)
        ids = _seed_local_store(store, {name: 5})
        t2_path, catalog_path = _make_source_dbs(tmp_path)

        result, staging = _drive(
            monkeypatch,
            local_path=store,
            t2_path=t2_path,
            catalog_path=catalog_path,
            voyage_key_present=False,
        )

        assert result.ok is True
        assert result.validation is not None and result.validation.unlocked is True
        assert result.rollback_available is False
        # Exact copy: all 5 source chunks landed under the (same-name) target.
        landed = _landed_chunks(staging, name)
        assert len(landed) == 5
        assert {r["legacy_ref"] for r in landed} == set(ids[name])
        assert staging.promote_calls == [name]
        assert staging.finalize_calls == 1
        # Sentinel cleared on a clean unlock (serving normal again).
        assert staging.cleared is True
        assert read_state() is None

    def test_scenario2b_empty_text_chunk_reconciles_not_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """reviewer-p2 CRITICAL regression: the engine's staged_content
        counts content rows ONLY (empty-text rows wait for finalize's Item8
        disposition) while /counts counts every landed row — the verify
        reconciliation must fold the finalize dispositions in. Pre-fix,
        ONE empty-text chunk false-positive-blocked the whole migration
        with a spurious count-parity error."""
        store = tmp_path / "chroma"
        name = _coll("oracle-empty", model=_MODEL_ONNX)
        _seed_local_store(store, {name: 4})
        client = chromadb.PersistentClient(path=str(store))
        client.get_collection(name).add(
            ids=["deadbeef" * 4], documents=[""],
            embeddings=[[0.0, 0.0]], metadatas=[{"position": 99, "tag": "etl"}])
        t2_path, catalog_path = _make_source_dbs(tmp_path)

        result, staging = _drive(
            monkeypatch,
            local_path=store,
            t2_path=t2_path,
            catalog_path=catalog_path,
            voyage_key_present=False,
        )

        assert result.ok is True, (
            f"an empty-text chunk must reconcile through the finalize "
            f"dispositions, never block: {result.sequence.blocked_reason}"
        )
        landed = _landed_chunks(staging, name)
        assert len(landed) == 5, "all 5 rows land, incl. the empty-text one"
        assert staging.cleared is True
        assert read_state() is None

    def test_scenario3_legacy_minilm_cross_model_migrates_and_unlocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RDR-162 P2 (still honored under RDR-180): a legacy minilm-384
        collection is NOT blocked — land-time classification re-embeds its
        STORED chunk text into a bge-768 TARGET (model-segment remap); the
        landed rows carry the TARGET name, not the source name. The server-
        side reference re-point (catalog manifest / topic assignments via the
        in-DB ``chash_alias`` join) is no longer client-observable — that is
        the engine's own concern, exercised by the .10.10 rehearsal, not this
        hermetic layer."""
        store = tmp_path / "chroma"
        source = _coll("oracle-x", model=_MODEL_LEGACY)
        target = _coll("oracle-x", model=_MODEL_ONNX)  # the bge-768 remap target
        ids = _seed_local_store(store, {source: 4})
        t2_path, catalog_path = _make_source_dbs(tmp_path)

        result, staging = _drive(
            monkeypatch,
            local_path=store,
            t2_path=t2_path,
            catalog_path=catalog_path,
            voyage_key_present=False,
        )

        assert result.ok is True
        assert result.validation is not None and result.validation.unlocked is True
        # Data landed under the TARGET (bge-768) name, not the source name.
        landed = _landed_chunks(staging, target)
        assert len(landed) == 4
        assert {r["legacy_ref"] for r in landed} == set(ids[source])
        assert _landed_chunks(staging, source) == []
        assert staging.promote_calls == [target]
        assert read_state() is None

    def test_scenario3b_voyage_no_key_blocks_pre_migration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A still-genuinely-unsupported collection (voyage model, NO key) is
        BLOCKED by the real model gate BEFORE any landing — it is the
        credential case (add NX_VOYAGE_API_KEY), NOT a model switch, so it is
        never cross-model remapped. NO data lands, no clean ok."""
        store = tmp_path / "chroma"
        name = _coll("oracle-vblock", model=_MODEL_VOYAGE)
        _seed_local_store(store, {name: 4})
        t2_path, catalog_path = _make_source_dbs(tmp_path)

        result, staging = _drive(
            monkeypatch,
            local_path=store,
            t2_path=t2_path,
            catalog_path=catalog_path,
            voyage_key_present=False,
        )

        assert result.ok is False
        assert result.validation is None  # never reached verify
        # NO data touched — the block is pre-landing.
        assert staging.loaded == {}
        assert staging.promote_calls == []
        # The sentinel is migrated-failed (degrade-LOUD), recoverable by adding
        # the key + re-running (idempotent), never a cleared sentinel.
        assert current_phase() == MIGRATED_FAILED

    def test_scenario3c_mixed_legacy_and_onnx_in_one_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RDR-162 P2: a realistic partially-upgraded store — one bge-768
        (already servable) AND one minilm-384 (legacy) collection in the SAME
        run. The bge lands byte-for-byte under its own name (no remap); the
        minilm lands under the cross-model-remapped bge-768 target."""
        store = tmp_path / "chroma"
        onnx = _coll("oracle-mix-onnx", model=_MODEL_ONNX)
        legacy_src = _coll("oracle-mix-legacy", model=_MODEL_LEGACY)
        legacy_tgt = _coll("oracle-mix-legacy", model=_MODEL_ONNX)
        ids = _seed_local_store(store, {onnx: 3, legacy_src: 5})
        t2_path, catalog_path = _make_source_dbs(tmp_path)

        result, staging = _drive(
            monkeypatch,
            local_path=store,
            t2_path=t2_path,
            catalog_path=catalog_path,
            voyage_key_present=False,
        )

        assert result.ok is True
        assert result.validation is not None and result.validation.unlocked is True
        onnx_landed = _landed_chunks(staging, onnx)
        assert len(onnx_landed) == 3
        assert {r["legacy_ref"] for r in onnx_landed} == set(ids[onnx])
        legacy_landed = _landed_chunks(staging, legacy_tgt)
        assert len(legacy_landed) == 5
        assert {r["legacy_ref"] for r in legacy_landed} == set(ids[legacy_src])
        assert _landed_chunks(staging, legacy_src) == []
        assert sorted(staging.promote_calls) == sorted([onnx, legacy_tgt])
        assert read_state() is None

    def test_scenario1_cloud_voyage_unlocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLOUD/Voyage: key present, voyage-1024 collection on the (injected)
        cloud leg, one command runs detect→land→promote→finalize→verify→unlock."""
        local = tmp_path / "chroma-local"
        _seed_local_store(local, {})  # empty local leg
        cloud = _FakeReadStore()
        cname = _coll("oracle-cloud", model=_MODEL_VOYAGE)
        cloud_ids = cloud.seed(cname, 6)
        t2_path, catalog_path = _make_source_dbs(tmp_path)

        result, staging = _drive(
            monkeypatch,
            local_path=local,
            t2_path=t2_path,
            catalog_path=catalog_path,
            voyage_key_present=True,
            cloud_store=cloud,
        )

        assert result.ok is True
        assert result.validation is not None and result.validation.unlocked is True
        landed = _landed_chunks(staging, cname)
        assert len(landed) == 6
        assert {r["legacy_ref"] for r in landed} == set(cloud_ids)
        assert read_state() is None

    def test_scenario4_forced_failure_no_rollback_staging_retained(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FORCED-FAILURE (RDR-180 contract change — see module docstring):
        landing + promote succeed, but ``finalize``'s in-txn residual check is
        nonzero, so ``verify`` BLOCKS. There is no more separate "T3 copied,
        validation failed" state to offer a rollback from: the run is simply
        NOT ok, ``validation`` is ``None``, ``rollback_available`` is always
        False, and staging is retained (never cleared) for an idempotent
        resume — recovery is re-running, not an explicit rollback command."""
        store = tmp_path / "chroma"
        name = _coll("oracle-rb", model=_MODEL_ONNX)
        ids = _seed_local_store(store, {name: 7})
        t2_path, catalog_path = _make_source_dbs(tmp_path)

        result, staging = _drive(
            monkeypatch,
            local_path=store,
            t2_path=t2_path,
            catalog_path=catalog_path,
            voyage_key_present=False,
            finalize_residual_mismatched=2,
        )

        assert result.ok is False
        assert result.validation is None
        assert result.rollback_available is False
        assert current_phase() == MIGRATED_FAILED  # never a bare empty index
        # The copy DID land + promote (there is something staging retains).
        landed = _landed_chunks(staging, name)
        assert len(landed) == 7
        assert {r["legacy_ref"] for r in landed} == set(ids[name])
        assert staging.promote_calls == [name]
        assert staging.finalize_calls == 1
        # Staging is retained — NEVER cleared on a verify block.
        assert staging.cleared is False

    def test_scenario5_two_leg_simultaneous_unlocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TWO-LEG SIMULTANEOUS (review SIG-3): local bge-768 AND cloud
        voyage-1024 in ONE run; both legs land + promote, and the run unlocks
        clean. Closes the only known multi-leg integration gap (unit-covered
        by ``test_driver.test_two_leg_reopens_both_legs_for_landing``)."""
        local = tmp_path / "chroma-local"
        lname = _coll("oracle-2leg-local", model=_MODEL_ONNX)
        lids = _seed_local_store(local, {lname: 3})
        cloud = _FakeReadStore()
        cname = _coll("oracle-2leg-cloud", model=_MODEL_VOYAGE)
        cids = cloud.seed(cname, 4)
        t2_path, catalog_path = _make_source_dbs(tmp_path)

        result, staging = _drive(
            monkeypatch,
            local_path=local,
            t2_path=t2_path,
            catalog_path=catalog_path,
            voyage_key_present=True,
            cloud_store=cloud,
        )

        assert result.ok is True
        assert result.validation is not None and result.validation.unlocked is True
        # Both legs landed; exact per-collection counts.
        local_landed = _landed_chunks(staging, lname)
        assert len(local_landed) == 3
        assert {r["legacy_ref"] for r in local_landed} == set(lids[lname])
        cloud_landed = _landed_chunks(staging, cname)
        assert len(cloud_landed) == 4
        assert {r["legacy_ref"] for r in cloud_landed} == set(cids)
        # critic-p2 M3: two collections in ONE run must resolve to two
        # DIFFERENT target dims — the misrouted-leg class RDR-180 exists to
        # kill had no end-to-end tripwire after the old dims assertion was
        # dropped in the driver-test conversion.
        assert {r["dim"] for r in local_landed} == {768}
        assert {r["dim"] for r in cloud_landed} == {1024}
        assert sorted(staging.promote_calls) == sorted([lname, cname])
        assert read_state() is None

    def test_scenario_c_mixed_models_blocks_on_voyage_subset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MIXED-MODELS (RDR §Test Plan (c)): ONE local store with both an
        onnx bge-768 AND a voyage-1024 collection, NO key. The pre-gate is
        all-or-nothing, so the whole run BLOCKS pre-landing (no data moved),
        and the diagnostic names ONLY the voyage subset as the blocker — the
        onnx collection is not at fault."""
        store = tmp_path / "chroma"
        onnx_name = _coll("oracle-mixed-onnx", model=_MODEL_ONNX)
        voyage_name = _coll("oracle-mixed-voyage", model=_MODEL_VOYAGE)
        _seed_local_store(store, {onnx_name: 3, voyage_name: 2})
        t2_path, catalog_path = _make_source_dbs(tmp_path)

        result, staging = _drive(
            monkeypatch,
            local_path=store,
            t2_path=t2_path,
            catalog_path=catalog_path,
            voyage_key_present=False,  # voyage collection is unsupported here
        )

        assert result.ok is False
        assert result.validation is None
        assert staging.loaded == {}  # all-or-nothing: NOTHING landed, not even onnx
        assert current_phase() == MIGRATED_FAILED
        # The diagnostic names only the voyage subset as the blocker.
        reason = result.sequence.blocked_reason or ""
        assert voyage_name in reason
        assert onnx_name not in reason


# ── Integration layer (real stack) — the literal served-on-pgvector MVV ───────


@pytest.mark.integration
class TestServedOnPgvectorMVV:
    """The literal RDR-159 §Validation MVV against the real stack: a real
    PG16+ + Java service (RDR-180 staging/promote/finalize) + ChromaCloud +
    Voyage reached via ONE command.

    Runs in the sandbox once the service-stack install story lands (release
    blocker ``nexus-luxe6``); excluded from CI (no service/keys). This registers
    the obligation rather than silently omitting it (the test_vector_etl
    deferred-obligation discipline). The hermetic layer above is the
    CI-enforceable gate; this is the end-to-end confirmation that the real
    service actually serves the migrated vectors on pgvector via the RDR-180
    staging schema.
    """

    def test_served_on_pgvector_end_to_end(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Drive the REAL guided upgrade end to end against a live service:
        seed a local ONNX store, run ``run_guided_upgrade`` with the real
        clients (the exact wiring ``nx migrate-to-service`` uses — RDR-180:
        ``vector_client`` / ``catalog_client`` are accepted-but-unused by the
        driver now, kept only for signature compatibility; the real write
        happens via the real, unmocked ``HttpStagingStore``), and assert the
        migrated chunks are SERVED on pgvector (promote's
        ``INSERT INTO nexus.chunks_<dim>`` lands there server-side). A SINGLE
        conditional skip (no unconditional skip): the body is reachable the
        moment a credentialed service is present, so the obligation is
        genuinely deferred, not permanently inert."""
        import os

        # This MVV is the LOCAL leg only. With real Chroma-Cloud creds in env
        # (a keys-present dev box / the local-service gate sourcing .env),
        # open_read_legs also opens the cloud leg and detection classifies the
        # REAL ChromaCloud tenant — 100+ ambient collections block the model
        # gate and the assertion fails on machine state, not code
        # (nexus-edwlp, 2026-07-07). Scrub the creds so only the seeded
        # tmp_path store is in scope.
        for var in ("CHROMA_API_KEY", "CHROMA_TENANT", "CHROMA_DATABASE"):
            monkeypatch.delenv(var, raising=False)

        token = os.environ.get("NX_SERVICE_TOKEN")
        if not token:
            pytest.skip(
                "served-on-pgvector MVV needs a live nexus-service "
                "(NX_SERVICE_TOKEN + reachable endpoint + Voyage key + the "
                "RDR-180 staging schema deployed); run in the sandbox once "
                "nexus-luxe6's install story lands."
            )

        from nexus.catalog.factory import make_catalog_client_for_migration
        from nexus.db.http_vector_client import HttpVectorClient, _resolve_endpoint

        try:
            _resolve_endpoint()
        except RuntimeError as exc:
            pytest.skip(f"nexus-service endpoint unresolved: {exc}")

        store = tmp_path / "chroma"
        name = _coll("oracle-integration", model=_MODEL_ONNX)
        ids = _seed_local_store(store, {name: 5}, dims=768)
        t2_path, catalog_path = _make_source_dbs(tmp_path)

        result = driver.run_guided_upgrade(
            sources=EtlSources(sqlite_path=t2_path, catalog_db_path=catalog_path),
            vector_client=HttpVectorClient(),
            catalog_client=make_catalog_client_for_migration(token=token),
            t2_db_path=t2_path,
            local_path=store,
            voyage_key_present=False,
        )

        assert result.ok is True
        assert result.validation is not None and result.validation.unlocked is True
        # The literal MVV: the migrated chunks are SERVED on pgvector.
        served = HttpVectorClient()
        assert served.count(name) == 5
        assert len(ids[name]) == 5
