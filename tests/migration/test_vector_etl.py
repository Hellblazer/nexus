# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-155 P5.1 (bead nexus-unp61) — copy-not-move ETL integrity suite, TDD-RED.

Drives ``src/nexus/migration/vector_etl.py`` (implemented by P5.2, bead
nexus-9n4pn): the Chroma → pgvector migration ETL. Reads through the
surviving read client (``nexus.migration.chroma_read`` — the ONLY allowed
Chroma constructors since P4a), writes through the Seam B HTTP vector
client (server-side embed into ``nexus.chunks_<dim>``).

Locked constraints this suite encodes (recorded on nexus-unp61 notes —
do not weaken):

* **Vector-identity decision (a)** (P5.1 kickoff, 2026-06-10): chunk TEXT
  transfers byte-verbatim and the chash (chunk natural ID) is preserved
  verbatim; the pgvector side re-embeds server-side (Seam B). NO source
  embedding vectors cross the ETL (``iter_collection_chunks`` deliberately
  omits them — RDR-109 cross-model-contamination guard). Recall equivalence
  with identical embedders is established by the P3 dual-run harness.
* **Manifest-FK validation is DIRECT SQL** (P2.1 critic constraint):
  ``catalog_document_chunks LEFT JOIN chunks_<dim> ... WHERE chash IS
  NULL`` — NOT ``PgVectorRepository.fetchDocumentChunks``, which fails
  loud on ANY partially-migrated document by design and must stay that way.
* **Collection names preserved VERBATIM** (RDR §Migrate): no namespace
  normalization, so ``topic_assignments.source_collection`` references
  stay valid (the string-copy-orphan class RDR-108 fixed).
* **COPY-NOT-MOVE**: the Chroma source is never modified — not by
  migration, not by rollback.
* **BOTH legs**: local ``PersistentClient`` copy + ChromaCloud REST/auth
  read (an ETL with only one leg is a silent half-migration).
* **Exact-count assertions** (``== N``, never ``>=``).

Two test levels, mirroring ``tests/db/test_catalog_etl.py``:

Unit (fast, hermetic — EphemeralClient source, fake vector client):
  migration counts/payload fidelity, verbatim names, page-aligned upsert
  batching, non-conformant/failed/missing collection reporting, dry-run,
  copy-not-move, leg routing, rollback, count verification, taxonomy
  consistency, manifest SQL artifact contracts.

Integration (``@pytest.mark.integration`` — real Java service + hermetic
Postgres 16 + real on-disk PersistentClient source):
  exact row counts source Chroma == pgvector via direct SQL, chash + text
  + collection-name verbatim round-trip, idempotency, copy-not-move,
  rollback, manifest backfill + orphan detection (clean state, cross-dim
  scoping, deliberate orphan), taxonomy source_collection resolution.

DEFERRED OBLIGATION (registered, not silently omitted): the CLOUD leg has
unit-level routing coverage only (``TestLegRouting`` monkeypatches the
opener). The real ChromaCloud REST/auth surface — credential resolution,
``_apply_chroma_http_timeout``, CloudClient pagination/error behaviour —
cannot be exercised hermetically. P5.G (bead nexus-a0i5u) owns it: either
a credential-gated ``migrate_cloud()`` integration run against the live
ChromaCloud tenant, or an explicit accept of that run as a cutover
pre-condition. Recorded on beads nexus-unp61 and nexus-a0i5u.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

import chromadb
import pytest

from nexus.corpus import CANONICAL_EMBEDDING_MODELS
from nexus.db.http_vector_client import VectorServiceError
from nexus.migration.chroma_read import iter_collection_chunks
from nexus.migration.vector_etl import (
    CollectionResult,
    MigrationReport,
    manifest_backfill_sql,
    manifest_orphan_sql,
    migrate_cloud,
    migrate_collections,
    migrate_local,
    rollback_collections,
    verify_counts,
    verify_taxonomy_consistency,
)

# ── Conformant test collection names (minilm token: hermetic, 384-dim,    ──
# ── embeds on the service's bundled ONNX fallback — no cloud credentials) ──

_MODEL_384 = "minilm-l6-v2-384"
_MODEL_768 = "bge-base-en-v15-768"


def _coll(owner: str, *, model: str = _MODEL_384, version: int = 1) -> str:
    return f"knowledge__{owner}__{model}__v{version}"


def _chash(text: str) -> str:
    """Chunk natural ID: sha256(text)[:32] (the repo-wide chash convention)."""
    return hashlib.sha256(text.encode()).hexdigest()[:32]


# ── Fake vector client (HttpVectorClient surface subset) ─────────────────────


class _FakeCollectionHandle:
    """Collection-handle stub mirroring ``_ServiceCollectionStub``."""

    def __init__(self, client: "FakeVectorClient", name: str) -> None:
        self._client = client
        self._name = name

    def delete(self, ids: list[str]) -> None:
        col = self._client.store.get(self._name, {})
        for chunk_id in ids:
            col.pop(chunk_id, None)

    def get(
        self,
        ids: list[str] | None = None,
        where: dict | None = None,
        include: list[str] | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> dict:
        col = self._client.store.get(self._name, {})
        keys = [i for i in (ids or list(col)) if i in col]
        return {
            "ids": keys,
            "documents": [col[k][0] for k in keys],
            "metadatas": [col[k][1] for k in keys],
        }


class FakeVectorClient:
    """Hermetic stand-in for ``HttpVectorClient`` (same surface subset).

    ``upsert_chunks`` deliberately has NO ``embeddings`` parameter: the
    vector-identity decision (a) pins that no source vectors cross the ETL
    — an implementation that forwards embeddings TypeErrors here.

    ``count_delta`` simulates a lossy target (service wrote fewer rows than
    sent) so the ETL's post-write count verification can be proven
    non-vacuous.
    """

    def __init__(self, *, count_delta: dict[str, int] | None = None) -> None:
        # collection -> {chash: (document, metadata)}
        self.store: dict[str, dict[str, tuple[str, dict]]] = {}
        # (collection, [ids]) per upsert call, in call order
        self.upsert_calls: list[tuple[str, list[str]]] = []
        self._count_delta = count_delta or {}

    def upsert_chunks(
        self,
        collection: str,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict] | None = None,
    ) -> None:
        metas = metadatas or [{}] * len(ids)
        self.upsert_calls.append((collection, list(ids)))
        col = self.store.setdefault(collection, {})
        for chunk_id, doc, meta in zip(ids, documents, metas):
            col[chunk_id] = (doc, dict(meta or {}))

    def count(self, collection: str) -> int:
        return len(self.store.get(collection, {})) + self._count_delta.get(
            collection, 0
        )

    def list_collections(self) -> list[dict]:
        return [
            {"name": name, "count": len(col)}
            for name, col in sorted(self.store.items())
            if col
        ]

    def collection_exists(self, name: str) -> bool:
        return bool(self.store.get(name))

    def delete_by_id(self, collection: str, doc_id: str) -> bool:
        col = self.store.get(collection, {})
        return col.pop(doc_id, None) is not None

    def get_collection(self, name: str) -> _FakeCollectionHandle:
        from chromadb.errors import NotFoundError

        if name not in self.store:
            raise NotFoundError(f"collection {name!r} not found")
        return _FakeCollectionHandle(self, name)

    def get_or_create_collection(self, name: str) -> _FakeCollectionHandle:
        self.store.setdefault(name, {})
        return _FakeCollectionHandle(self, name)


class FailingUpsertClient(FakeVectorClient):
    """Fake that rejects upserts for one collection (service-side 4xx/5xx)."""

    def __init__(self, *, fail_for: str) -> None:
        super().__init__()
        self._fail_for = fail_for

    def upsert_chunks(self, collection, ids, documents, metadatas=None):  # type: ignore[override]
        if collection == self._fail_for:
            raise VectorServiceError(
                f"POST /v1/vectors/upsert-chunks → HTTP 400: injected for {collection}"
            )
        super().upsert_chunks(collection, ids, documents, metadatas)


# ── Source seeding ────────────────────────────────────────────────────────────


def _seed_source(client, name: str, n: int, *, text_prefix: str = "chunk text") -> list[str]:
    """Seed *n* chunks into a Chroma collection; returns the chash ids.

    Ids follow the chash convention (sha256(text)[:32]) so the migrated
    pgvector ``chash`` column round-trips the natural ID verbatim. Explicit
    tiny embeddings: the SOURCE vectors are never read by the ETL
    (decision (a)), so their dimension is deliberately nonsensical (2).
    """
    texts = [f"{text_prefix} {i:04d}" for i in range(n)]
    ids = [_chash(t) for t in texts]
    if n:
        col = client.get_or_create_collection(name)
        col.add(
            ids=ids,
            documents=texts,
            metadatas=[{"position": i, "tag": "etl"} for i in range(n)],
            embeddings=[[float(i), 1.0] for i in range(n)],
        )
    else:
        client.get_or_create_collection(name)
    return ids


@pytest.fixture()
def source_client():
    c = chromadb.EphemeralClient()
    # EphemeralClient shares one in-process backend — clear leftovers.
    for col in c.list_collections():
        c.delete_collection(col.name)
    return c


# ── Unit: migrate_collections ────────────────────────────────────────────────


class TestMigrateCollectionsUnit:
    def test_exact_counts_and_verbatim_payload(self, source_client) -> None:
        """(a)+(b): exact row counts and verbatim text/chash/metadata.

        Vector-identity decision (a): the assertion is on TEXT and chash,
        never on embedding floats — the target re-embeds server-side.
        """
        name = _coll("etlunit1")
        ids = _seed_source(source_client, name, 7)
        fake = FakeVectorClient()

        report = migrate_collections(source_client, fake, leg="local")

        assert isinstance(report, MigrationReport)
        assert report.leg == "local"
        assert len(report.results) == 1
        result = report.results[0]
        assert isinstance(result, CollectionResult)
        assert result.collection == name
        assert result.status == "migrated"
        assert result.source_count == 7
        assert result.written_count == 7
        assert report.ok is True
        assert report.total_source == 7
        assert report.total_written == 7

        # chash verbatim: target ids are exactly the source ids.
        assert sorted(fake.store[name]) == sorted(ids)
        # text byte-identical + metadata dict equal.
        doc, meta = fake.store[name][_chash("chunk text 0003")]
        assert doc == "chunk text 0003"
        assert meta == {"position": 3, "tag": "etl"}

    def test_collection_name_preserved_verbatim(self, source_client) -> None:
        """RDR §Migrate: NO namespace normalization — the pgvector
        ``collection`` value is the source name byte-for-byte, so
        ``topic_assignments.source_collection`` references stay valid."""
        name = f"knowledge__Weird.Owner-1__{_MODEL_384}__v2"
        _seed_source(source_client, name, 2)
        fake = FakeVectorClient()

        report = migrate_collections(source_client, fake, leg="local")

        assert list(fake.store) == [name]
        assert report.results[0].collection == name

    def test_upsert_batches_follow_read_pages(self, source_client) -> None:
        """Writes are paged with the reads: page_size=3 over 7 chunks
        produces upsert batches of exactly [3, 3, 1]."""
        name = _coll("etlunit-pages")
        _seed_source(source_client, name, 7)
        fake = FakeVectorClient()

        migrate_collections(source_client, fake, leg="local", page_size=3)

        assert [len(batch) for _, batch in fake.upsert_calls] == [3, 3, 1]

    def test_no_embeddings_cross_the_etl(self, source_client) -> None:
        """Decision (a) pin: the fake's ``upsert_chunks`` has no
        ``embeddings`` parameter — an ETL forwarding source vectors would
        TypeError. The target store holds (text, metadata) only."""
        name = _coll("etlunit-noemb")
        _seed_source(source_client, name, 3)
        fake = FakeVectorClient()

        report = migrate_collections(source_client, fake, leg="local")

        assert report.ok is True
        assert all(
            isinstance(doc, str) and isinstance(meta, dict)
            for doc, meta in fake.store[name].values()
        )

    def test_nonconformant_collection_skipped_loud(self, source_client) -> None:
        """A non-four-segment name cannot dim-dispatch
        (``PgVectorRepository.dimForCollection`` fails loud server-side).
        The ETL must REPORT it as skipped — never silently drop it, never
        attempt the upsert."""
        legacy = "knowledge__legacy"
        good = _coll("etlunit-good")
        _seed_source(source_client, legacy, 2)
        _seed_source(source_client, good, 3)
        fake = FakeVectorClient()

        report = migrate_collections(source_client, fake, leg="local")

        by_name = {r.collection: r for r in report.results}
        assert set(by_name) == {legacy, good}
        assert by_name[legacy].status == "skipped"
        assert "conformant" in by_name[legacy].reason
        assert by_name[legacy].written_count == 0
        assert by_name[good].status == "migrated"
        assert by_name[good].written_count == 3
        # No upsert was even attempted for the legacy name.
        assert all(c != legacy for c, _ in fake.upsert_calls)
        assert report.ok is False

    def test_failed_collection_does_not_abort_run(self, source_client) -> None:
        """A service-rejected collection is reported failed; the remaining
        collections still migrate (no abort-all, no silent loss)."""
        bad = _coll("etlunit-bad")
        good = _coll("etlunit-good2")
        _seed_source(source_client, bad, 2)
        _seed_source(source_client, good, 4)
        fake = FailingUpsertClient(fail_for=bad)

        report = migrate_collections(source_client, fake, leg="local")

        by_name = {r.collection: r for r in report.results}
        assert by_name[bad].status == "failed"
        assert by_name[bad].reason != ""
        assert by_name[good].status == "migrated"
        assert by_name[good].written_count == 4
        assert report.ok is False

    def test_count_mismatch_fails_loud(self, source_client) -> None:
        """Post-write count verification: a target reporting fewer rows
        than were sent is a FAILED migration, not a green one (the
        integrity hook that makes 'exact counts' non-vacuous)."""
        name = _coll("etlunit-lossy")
        _seed_source(source_client, name, 5)
        fake = FakeVectorClient(count_delta={name: -1})

        report = migrate_collections(source_client, fake, leg="local")

        result = report.results[0]
        assert result.status == "failed"
        assert result.source_count == 5
        assert "count" in result.reason.lower()
        assert report.ok is False

    def test_dry_run_writes_nothing(self, source_client) -> None:
        name = _coll("etlunit-dry")
        _seed_source(source_client, name, 4)
        fake = FakeVectorClient()

        report = migrate_collections(source_client, fake, leg="local", dry_run=True)

        result = report.results[0]
        assert result.status == "dry-run"
        assert result.source_count == 4
        assert result.written_count == 0
        assert fake.upsert_calls == []
        assert fake.store == {}
        assert report.ok is True

    def test_empty_collection_migrates_cleanly(self, source_client) -> None:
        name = _coll("etlunit-empty")
        _seed_source(source_client, name, 0)
        fake = FakeVectorClient()

        report = migrate_collections(source_client, fake, leg="local")

        result = report.results[0]
        assert result.status == "migrated"
        assert result.source_count == 0
        assert result.written_count == 0
        assert report.ok is True

    def test_missing_collection_fails_loud(self, source_client) -> None:
        """An explicitly requested collection absent from the source is a
        reported failure, never a silent skip."""
        fake = FakeVectorClient()
        ghost = _coll("etlunit-ghost")

        report = migrate_collections(
            source_client, fake, leg="local", collections=[ghost]
        )

        result = report.results[0]
        assert result.collection == ghost
        assert result.status == "failed"
        assert report.ok is False

    def test_copy_not_move_source_unmodified(self, source_client) -> None:
        """COPY-NOT-MOVE: after migration the source collection re-reads
        byte-identically (same ids, texts, metadata, same count)."""
        name = _coll("etlunit-copy")
        ids = _seed_source(source_client, name, 6)
        before = list(iter_collection_chunks(source_client, name))

        migrate_collections(source_client, FakeVectorClient(), leg="local")

        after = list(iter_collection_chunks(source_client, name))
        assert source_client.get_collection(name).count() == 6
        assert sorted(r["id"] for r in after) == sorted(ids)
        assert {r["id"]: (r["document"], r["metadata"]) for r in after} == {
            r["id"]: (r["document"], r["metadata"]) for r in before
        }


# ── Unit: both legs route through the surviving read client ─────────────────


class TestLegRouting:
    """RDR §Migrate: BOTH legs are required. The local leg opens the
    retired daemon's on-disk store; the cloud leg reads via the Chroma
    REST/auth API. Each must construct its client through
    ``nexus.migration.chroma_read`` (the P4a.1 construction allowlist)."""

    def test_local_leg_routes_through_local_opener(
        self, source_client, monkeypatch, tmp_path
    ) -> None:
        name = _coll("etlleg-local")
        _seed_source(source_client, name, 3)
        opened: list[Path] = []

        def fake_open(path):
            opened.append(Path(path))
            return source_client

        monkeypatch.setattr(
            "nexus.migration.vector_etl.open_local_read_client", fake_open
        )
        fake = FakeVectorClient()

        report = migrate_local(tmp_path / "chroma", fake)

        assert opened == [tmp_path / "chroma"]
        assert report.leg == "local"
        assert report.results[0].collection == name
        assert report.results[0].written_count == 3

    def test_cloud_leg_routes_through_cloud_opener(
        self, source_client, monkeypatch
    ) -> None:
        name = _coll("etlleg-cloud")
        _seed_source(source_client, name, 2)
        opened: list[bool] = []

        def fake_open(**kwargs):
            opened.append(True)
            return source_client

        monkeypatch.setattr(
            "nexus.migration.vector_etl.open_cloud_read_client", fake_open
        )
        fake = FakeVectorClient()

        report = migrate_cloud(fake)

        assert opened == [True]
        assert report.leg == "cloud"
        assert report.results[0].collection == name
        assert report.results[0].written_count == 2

    def test_default_scope_is_every_source_collection(self, source_client) -> None:
        """``collections=None`` migrates the WHOLE source (sorted) — a
        partial default would be a silent half-migration."""
        a = _coll("etlleg-a")
        b = _coll("etlleg-b")
        _seed_source(source_client, a, 1)
        _seed_source(source_client, b, 2)
        fake = FakeVectorClient()

        report = migrate_collections(source_client, fake, leg="local")

        assert [r.collection for r in report.results] == sorted([a, b])
        assert report.total_source == 3
        assert report.total_written == 3


# ── Unit: rollback ────────────────────────────────────────────────────────────


class TestRollbackUnit:
    def test_rollback_removes_exactly_migrated_rows(self, source_client) -> None:
        """The rollback flag undoes the copy: every migrated chash is
        deleted from the target; the source is untouched (it IS the
        rollback manifest)."""
        name = _coll("etlrb-1")
        _seed_source(source_client, name, 7)
        fake = FakeVectorClient()
        migrate_collections(source_client, fake, leg="local")
        assert fake.count(name) == 7

        deleted = rollback_collections(source_client, fake)

        assert deleted == {name: 7}
        assert fake.count(name) == 0
        assert source_client.get_collection(name).count() == 7

    def test_rollback_leaves_other_collections(self, source_client) -> None:
        keep = _coll("etlrb-keep")
        drop = _coll("etlrb-drop")
        _seed_source(source_client, keep, 3)
        _seed_source(source_client, drop, 4)
        fake = FakeVectorClient()
        migrate_collections(source_client, fake, leg="local")

        deleted = rollback_collections(source_client, fake, collections=[drop])

        assert deleted == {drop: 4}
        assert fake.count(drop) == 0
        assert fake.count(keep) == 3

    def test_rollback_of_unmigrated_collection_deletes_zero(
        self, source_client
    ) -> None:
        name = _coll("etlrb-virgin")
        _seed_source(source_client, name, 2)
        fake = FakeVectorClient()

        deleted = rollback_collections(source_client, fake, collections=[name])

        assert deleted == {name: 0}

    def test_rollback_refuses_false_clean_zero(self, source_client) -> None:
        """P5.2 review fix (CRE H1 / critic S1, additive strengthening):
        the service collection handle SWALLOWS transport errors and returns
        empty lookups — a target that holds chunks while not a single
        source chash resolves is indistinguishable from a failed read, and
        must fail loud instead of reporting a clean ``deleted == 0``."""
        name = _coll("etlrb-swallow")
        _seed_source(source_client, name, 3)
        # Target claims 6 chunks (count_delta) but the lookup layer
        # resolves nothing (empty store) — the swallowed-error signature.
        fake = FakeVectorClient(count_delta={name: 6})

        with pytest.raises(RuntimeError, match="refusing to report a clean zero"):
            rollback_collections(source_client, fake, collections=[name])

    def test_rollback_detects_swallowed_deletes(self, source_client) -> None:
        """P5.G gate fix (gate-CRE M1, additive strengthening): the DELETE
        leg of the collection handle also swallows transport errors — a
        rollback whose deletes silently no-op must fail loud (post-delete
        count verification), not report deleted == N."""
        name = _coll("etlrb-noop-delete")
        _seed_source(source_client, name, 4)

        class _SwallowedDeleteClient(FakeVectorClient):
            def get_or_create_collection(self, n: str):
                handle = super().get_or_create_collection(n)
                handle.delete = lambda ids: None  # transport swallow: no-op
                return handle

        fake = _SwallowedDeleteClient()
        migrate_collections(source_client, fake, leg="local")
        assert fake.count(name) == 4

        with pytest.raises(RuntimeError, match="deletes may have been swallowed"):
            rollback_collections(source_client, fake, collections=[name])


# ── Unit: count verification ──────────────────────────────────────────────────


class TestVerifyCounts:
    def test_exact_source_target_pairs(self, source_client) -> None:
        a = _coll("etlvc-a")
        b = _coll("etlvc-b")
        _seed_source(source_client, a, 5)
        _seed_source(source_client, b, 2)
        fake = FakeVectorClient()
        migrate_collections(source_client, fake, leg="local")

        counts = verify_counts(source_client, fake, [a, b])

        assert counts == {a: (5, 5), b: (2, 2)}

    def test_mismatch_is_visible(self, source_client) -> None:
        name = _coll("etlvc-miss")
        _seed_source(source_client, name, 3)
        fake = FakeVectorClient()  # nothing migrated

        counts = verify_counts(source_client, fake, [name])

        assert counts == {name: (3, 0)}


# ── Unit: T2 taxonomy consistency (d) ────────────────────────────────────────


def _make_t2_with_assignments(tmp_path: Path, source_collections: list[str | None]) -> Path:
    """Minimal T2 SQLite with the ``topic_assignments`` columns the check
    reads (mirrors ``nexus.db.migrations`` — chunk_id/topic_id plus the
    projection-quality columns including ``source_collection``)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "nexus.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE topic_assignments ("
        " chunk_id TEXT, topic_id INTEGER, assigned_by TEXT,"
        " similarity REAL, assigned_at TEXT, source_collection TEXT)"
    )
    for i, src in enumerate(source_collections):
        conn.execute(
            "INSERT INTO topic_assignments"
            " (chunk_id, topic_id, assigned_by, source_collection)"
            " VALUES (?, ?, 'hdbscan', ?)",
            (f"chunk-{i}", i, src),
        )
    conn.commit()
    conn.close()
    return db_path


class TestVerifyTaxonomyConsistencyUnit:
    """(d): every ``topic_assignments.source_collection`` value must
    resolve to a migrated pgvector collection — no orphaned taxonomy
    attribution post-cutover (the string-copy-orphan class RDR-108
    fixed)."""

    def test_all_resolved_returns_empty(self, tmp_path) -> None:
        name = _coll("etltax-ok")
        fake = FakeVectorClient()
        fake.upsert_chunks(name, ["id1"], ["text"], [{}])
        db = _make_t2_with_assignments(tmp_path, [name, name])

        assert verify_taxonomy_consistency(db, fake) == []

    def test_unresolved_collections_reported_exactly(self, tmp_path) -> None:
        ok = _coll("etltax-ok2")
        ghost_a = _coll("etltax-ghost-a")
        ghost_b = _coll("etltax-ghost-b")
        fake = FakeVectorClient()
        fake.upsert_chunks(ok, ["id1"], ["text"], [{}])
        db = _make_t2_with_assignments(tmp_path, [ok, ghost_a, ghost_b, ghost_a])

        unresolved = verify_taxonomy_consistency(db, fake)

        assert unresolved == sorted([ghost_a, ghost_b])

    def test_null_and_empty_source_collection_ignored(self, tmp_path) -> None:
        """Pre-projection rows carry NULL/'' source_collection — they are
        not orphans, they are unattributed."""
        name = _coll("etltax-null")
        fake = FakeVectorClient()
        fake.upsert_chunks(name, ["id1"], ["text"], [{}])
        db = _make_t2_with_assignments(tmp_path, [name, None, ""])

        assert verify_taxonomy_consistency(db, fake) == []

    def test_no_visible_collections_fails_loud(self, tmp_path) -> None:
        """P5.2 review fix (CRE M2, additive strengthening):
        ``list_collections()`` swallows service errors into ``[]`` — a down
        service and a never-run migration would both read as 'everything is
        an orphan'. Neither deserves a quiet all-orphan verdict."""
        db = _make_t2_with_assignments(tmp_path, [_coll("etltax-down")])

        with pytest.raises(RuntimeError, match="no migrated collections"):
            verify_taxonomy_consistency(db, FakeVectorClient())


# ── Unit: manifest SQL artifacts (c) ─────────────────────────────────────────


class TestManifestSqlArtifacts:
    """(c): manifest-FK validation is DIRECT SQL by constraint —
    ``PgVectorRepository.fetchDocumentChunks`` fails loud on ANY
    partially-migrated document by design (P2.1) and must not be weakened
    into a validation seam. These artifacts are the validation seam."""

    def test_orphan_sql_is_the_direct_left_join(self) -> None:
        sql = manifest_orphan_sql(384)
        flat = " ".join(sql.split()).lower()
        assert "catalog_document_chunks" in flat
        assert "chunks_384" in flat
        assert "left join" in flat
        assert "is null" in flat
        # Unbackfilled rows (collection IS NULL) are not orphans — they are
        # pre-backfill state and excluded from the resolution check.
        assert "collection is not null" in flat

    def test_orphan_sql_scopes_to_the_dim_model_tokens(self) -> None:
        """A 768-collection manifest row LEFT JOINed against chunks_384
        would be a false orphan; the query must scope manifest rows to
        collections whose model segment dispatches to THIS dim."""
        assert _MODEL_384 in manifest_orphan_sql(384)
        assert _MODEL_768 in manifest_orphan_sql(768)
        assert _MODEL_384 not in manifest_orphan_sql(768)

    def test_orphan_sql_1024_covers_every_cloud_token(self) -> None:
        """1024 is the cloud/Voyage lane (``PgVectorRepository.MODEL_DIMS``).
        Tokens are asserted from the canonical-set AUTHORITY
        (``nexus.corpus.CANONICAL_EMBEDDING_MODELS``) rather than literals,
        so the pin tracks the registry — and the test stays RDR-109
        mode-lint clean (no cloud-token literals in this source)."""
        sql = manifest_orphan_sql(1024)
        assert "chunks_1024" in sql
        for token in CANONICAL_EMBEDDING_MODELS:
            assert token in sql
        # The legacy generic 1024 token in MODEL_DIMS, scoped here too.
        assert "voyage-3" in sql
        assert _MODEL_384 not in sql
        assert _MODEL_768 not in sql

    def test_orphan_sql_rejects_unknown_dim(self) -> None:
        with pytest.raises(ValueError):
            manifest_orphan_sql(512)

    def test_backfill_sql_fills_only_null_collections(self) -> None:
        """Backfill derives ``catalog_document_chunks.collection`` from the
        owning document's ``physical_collection`` and touches ONLY
        not-yet-backfilled rows (idempotent re-run)."""
        sql = manifest_backfill_sql()
        flat = " ".join(sql.split()).lower()
        assert "update" in flat
        assert "catalog_document_chunks" in flat
        assert "physical_collection" in flat
        assert "collection is null" in flat


class TestModelDimsJavaParity:
    """P5.2 review fix (CRE M1 / critic S2, additive strengthening): the
    Python ``_MODEL_DIMS`` registry MIRRORS the Java authority
    ``PgVectorRepository.MODEL_DIMS``. A Java-side token added without the
    Python mirror would make the ETL skip that model's collections as
    non-conformant — reported, but a confusing silent-partial-migration
    trap. This parses the Java source so drift fails mechanically."""

    def test_python_mirror_matches_java_authority(self) -> None:
        import re

        from nexus.migration.vector_etl import _MODEL_DIMS

        java_file = (
            Path(__file__).resolve().parents[2]
            / "service/src/main/java/dev/nexus/service/vectors/PgVectorRepository.java"
        )
        java_src = java_file.read_text()
        block_match = re.search(r"MODEL_DIMS\s*=\s*Map\.of\((.*?)\);", java_src, re.S)
        assert block_match is not None, "MODEL_DIMS Map.of block not found in Java source"
        java_map = {
            token: int(dim)
            for token, dim in re.findall(r'"([^"]+)",\s*(\d+)', block_match.group(1))
        }
        assert java_map, "parsed an empty MODEL_DIMS from the Java source"
        assert _MODEL_DIMS == java_map


class TestEmbedderModeParityJava:
    """nexus-pebfx.2 (critic finding): extends the TestModelDimsJavaParity
    pattern to MODE parity. EmbedderRouter's cloud-mode dispatch table is the
    Java authority for WHICH model tokens are servable; every dispatchable
    token must be a known RDR-103 token (subset of MODEL_DIMS), and the
    onnx-local table must be exactly the local ONNX token. A Java-side
    embedder added with a token Python doesn't know would dispatch to a
    chunks_<dim> table the ETL/serving layers treat as non-conformant —
    this parses the Java source so drift fails mechanically."""

    _ROUTER_JAVA = (
        Path(__file__).resolve().parents[2]
        / "service/src/main/java/dev/nexus/service/vectors/EmbedderRouter.java"
    )

    def test_cloud_mode_dispatch_tokens_are_known_models(self) -> None:
        import re

        from nexus.migration.vector_etl import _MODEL_DIMS

        src = self._ROUTER_JAVA.read_text()
        # The cloud-mode table self-keys via <embedder>.modelToken(); resolve
        # each key through the literal each embedder is constructed with.
        block = re.search(
            r"this\.modelEmbedders\s*=\s*Map\.of\((.*?)\);", src, re.S,
        )
        assert block is not None, "cloud-mode modelEmbedders Map.of not found"
        # Tokens appear as constructor literals in the same file: the ONNX
        # token via OnnxEmbedder.modelToken() override, voyage tokens as
        # VoyageEmbedder constructor args, CCE via CceEmbedder override.
        voyage_tokens = set(re.findall(r'new VoyageEmbedder\([^)]*"(voyage-[^"]+)"', src))
        assert voyage_tokens, "no VoyageEmbedder construction literals found"
        onnx_token = "minilm-l6-v2-384"
        cce_token = "voyage-context-3"
        dispatchable = voyage_tokens | {onnx_token, cce_token}
        unknown = dispatchable - set(_MODEL_DIMS)
        assert not unknown, (
            f"Java EmbedderRouter dispatches tokens unknown to Python "
            f"_MODEL_DIMS: {sorted(unknown)} — add them to BOTH registries "
            f"or remove the embedder"
        )

    def test_embedder_model_tokens_match_java_overrides(self) -> None:
        import re

        from nexus.migration.vector_etl import _MODEL_DIMS

        # Each concrete embedder's modelToken() override must return a known
        # RDR-103 token (same-dim wrong-model guard depends on this identity).
        for java_name, expected in (
            ("OnnxEmbedder.java", "minilm-l6-v2-384"),
            ("CceEmbedder.java", "voyage-context-3"),
        ):
            src = (self._ROUTER_JAVA.parent / java_name).read_text()
            m = re.search(
                r"public String modelToken\(\)\s*\{\s*return\s+\"([^\"]+)\";",
                src,
            )
            assert m is not None, f"{java_name}: modelToken() override not found"
            assert m.group(1) == expected
            assert m.group(1) in _MODEL_DIMS


# ══ Integration: real Java service + hermetic Postgres 16 ════════════════════

from tests.db._service_fixture import SERVICE_ROLES_SQL  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_JAR       = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
_PG_BIN    = Path("/opt/homebrew/opt/postgresql@16/bin")
_INITDB    = _PG_BIN / "initdb"
_PG_CTL    = _PG_BIN / "pg_ctl"
_PSQL      = _PG_BIN / "psql"
_CREATEDB  = _PG_BIN / "createdb"
_JAVA_HOME = os.environ.get("JAVA_HOME", "")
_JAVA = Path(_JAVA_HOME) / "bin" / "java" if _JAVA_HOME else Path(shutil.which("java") or "java")

_ALL_PREREQS = (
    _JAR.exists()
    and _INITDB.exists()
    and _PG_CTL.exists()
    and _PSQL.exists()
    and _CREATEDB.exists()
    and (_JAVA.exists() if _JAVA_HOME else shutil.which("java") is not None)
)

_SKIP_INTEGRATION = pytest.mark.skipif(
    not _ALL_PREREQS,
    reason=(
        "skipped: missing jar or pg16 binaries "
        f"(jar={_JAR.exists()}, pg16={_PG_CTL.exists()}, java={_JAVA})"
    ),
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_tcp(host: str, port: int, timeout: float = 40.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.15)
    raise TimeoutError(f"port {port} on {host} not reachable after {timeout}s")


def _psql_exec(pg: dict, sql: str) -> str:
    """Run *sql* as the OS superuser (trust auth — bypasses FORCE RLS, which
    is exactly what the cutover operator's direct-SQL validation does)."""
    proc = subprocess.run(
        [
            str(_PSQL),
            "-h", "127.0.0.1",
            "-p", str(pg["port"]),
            "-U", pg["user"],
            "-d", pg["dbname"],
            "-v", "ON_ERROR_STOP=1",
            "-tA",
            "-c", sql,
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"psql failed (rc={proc.returncode}):\n"
            f"sql={sql}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return proc.stdout


def _psql_rows(pg: dict, sql: str) -> list[str]:
    return [line for line in _psql_exec(pg, sql).splitlines() if line.strip()]


@pytest.fixture(scope="module")
def vec_etl_pg_instance():
    """Hermetic PostgreSQL 16 (net63 pattern, same as test_catalog_etl.py):
    no schema pre-application — the JAR's Liquibase run owns DDL; only the
    nexus_svc role must pre-exist (grants-nexus-svc.xml runAlways)."""
    if not _ALL_PREREQS:
        pytest.skip("missing jar or pg16 binaries")

    pgdata = tempfile.mkdtemp(prefix="nexus_vec_etl_pg_")
    pg_port = _free_port()
    pglog = os.path.join(pgdata, "pg.log")
    pg_user = os.environ["USER"]

    try:
        subprocess.run(
            [str(_INITDB), "-D", pgdata, "--no-locale", "-E", "UTF8", "--auth=trust"],
            check=True, capture_output=True,
        )
        with open(os.path.join(pgdata, "postgresql.conf"), "a") as f:
            f.write(f"\nport = {pg_port}\nlisten_addresses = '127.0.0.1'\n")
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "-l", pglog,
             "-o", f"-p {pg_port} -k {pgdata}",
             "start", "-w"],
            check=True, capture_output=True,
        )
        subprocess.run(
            [str(_CREATEDB), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "nexus_vec_etltest"],
            check=True, capture_output=True,
        )

        pg = {"port": pg_port, "dbname": "nexus_vec_etltest", "user": pg_user, "pgdata": pgdata}
        proc = subprocess.run(
            [
                str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port),
                "-U", pg_user, "-d", pg["dbname"],
                "-v", "ON_ERROR_STOP=1", "-c", SERVICE_ROLES_SQL,
            ],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"role bootstrap failed: {proc.stderr}")

        yield pg
    finally:
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "stop", "-m", "immediate"],
            capture_output=True,
        )
        shutil.rmtree(pgdata, ignore_errors=True)


@pytest.fixture(scope="module")
def vec_etl_service(vec_etl_pg_instance):
    """Java service on the hermetic Postgres. Two-role config (net63):
    admin pool = OS superuser (Liquibase DDL); app pool = nexus_svc
    (NOSUPERUSER NOBYPASSRLS) so FORCE RLS applies to served writes.
    No Voyage key: the EmbedderRouter's bundled ONNX fallback embeds the
    minilm-token test collections server-side at 384 dims."""
    svc_port = _free_port()
    token = "vec-etl-bearer-token-abc123"
    pg = vec_etl_pg_instance
    pg_user = pg["user"]
    pg_jdbc = f"jdbc:postgresql://127.0.0.1:{pg['port']}/{pg['dbname']}"

    env = {
        **os.environ,
        "NX_SERVICE_PORT":  str(svc_port),
        "NX_SERVICE_TOKEN": token,
        "NX_DB_URL":  pg_jdbc,
        "NX_DB_USER": "nexus_svc",
        "NX_DB_PASS": "nexus_svc_pass",
        "NX_POOL_SIZE": "3",
        "NX_DB_ADMIN_URL":  pg_jdbc,
        "NX_DB_ADMIN_USER": pg_user,
        "NX_DB_ADMIN_PASS": "",
    }
    env.pop("NX_STORAGE_BACKEND", None)
    env.pop("NX_VOYAGE_API_KEY", None)

    proc = subprocess.Popen(
        [str(_JAVA), "-jar", str(_JAR)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )
    try:
        _wait_tcp("127.0.0.1", svc_port, timeout=40.0)
        yield f"http://127.0.0.1:{svc_port}", token, proc
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.fixture(scope="module")
def vec_etl_vector_client(vec_etl_service):
    """Real HttpVectorClient against the booted service (tenant: the
    bootstrap token is server-bound to 'default' — every migrated row's
    tenant_id is asserted against that value)."""
    from nexus.db.http_vector_client import HttpVectorClient

    base_url, token, _ = vec_etl_service
    saved = {k: os.environ.get(k) for k in ("NX_SERVICE_URL", "NX_SERVICE_TOKEN")}
    os.environ["NX_SERVICE_URL"] = base_url
    os.environ["NX_SERVICE_TOKEN"] = token
    yield HttpVectorClient(tenant="default")
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _make_local_store(root: Path, name: str, n: int) -> tuple[Path, list[str], list[str]]:
    """On-disk PersistentClient store: the local-leg source. Returns
    (store_path, chash_ids, texts)."""
    store = root / "chroma_src"
    store.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(store))
    texts = [f"vec etl chunk {name} {i:04d}" for i in range(n)]
    ids = [_chash(t) for t in texts]
    col = client.get_or_create_collection(name)
    col.add(
        ids=ids,
        documents=texts,
        metadatas=[{"position": i} for i in range(n)],
        embeddings=[[float(i), 1.0] for i in range(n)],
    )
    # WAL single-opener discipline (chroma_read.py): release this client
    # deterministically before migrate_local opens its own PersistentClient
    # on the same store path.
    del col, client
    return store, ids, texts


@pytest.mark.integration
@_SKIP_INTEGRATION
class TestVectorEtlIntegration:
    """Full local-leg ETL against the real service + Postgres, validated
    with DIRECT SQL (the cutover operator's view, superuser — bypasses
    RLS exactly as the P5.G validation run will)."""

    def test_exact_counts_chash_text_collection_verbatim(
        self, vec_etl_pg_instance, vec_etl_vector_client, tmp_path
    ) -> None:
        pg = vec_etl_pg_instance
        name = _coll("etlint1")
        store, ids, texts = _make_local_store(tmp_path, name, 5)

        report = migrate_local(store, vec_etl_vector_client)

        assert report.ok is True
        result = {r.collection: r for r in report.results}[name]
        assert (result.source_count, result.written_count) == (5, 5)

        # (a) exact row count, direct SQL.
        rows = _psql_rows(
            pg, f"SELECT count(*) FROM nexus.chunks_384 WHERE collection = '{name}'"
        )
        assert rows == ["5"]
        # (b) chash verbatim — pgvector chash set == source Chroma id set.
        chashes = _psql_rows(
            pg, f"SELECT chash FROM nexus.chunks_384 WHERE collection = '{name}'"
        )
        assert sorted(chashes) == sorted(ids)
        # (b) text byte-identical for a spot chunk.
        text = _psql_rows(
            pg,
            "SELECT chunk_text FROM nexus.chunks_384"
            f" WHERE collection = '{name}' AND chash = '{ids[3]}'",
        )
        assert text == [texts[3]]
        # Collection name verbatim and tenant stamping.
        tenants = _psql_rows(
            pg,
            "SELECT DISTINCT tenant_id FROM nexus.chunks_384"
            f" WHERE collection = '{name}'",
        )
        assert tenants == ["default"]
        # The vector embedded server-side at the dispatched dim (re-embed,
        # decision (a)) — its existence IS the identity criterion, not its
        # float values.
        dims = _psql_rows(
            pg,
            "SELECT DISTINCT vector_dims(embedding) FROM nexus.chunks_384"
            f" WHERE collection = '{name}'",
        )
        assert dims == ["384"]

    def test_second_run_is_idempotent(
        self, vec_etl_pg_instance, vec_etl_vector_client, tmp_path
    ) -> None:
        pg = vec_etl_pg_instance
        name = _coll("etlint2")
        store, _, _ = _make_local_store(tmp_path, name, 4)

        first = migrate_local(store, vec_etl_vector_client)
        second = migrate_local(store, vec_etl_vector_client)

        assert first.ok is True
        assert second.ok is True
        rows = _psql_rows(
            pg, f"SELECT count(*) FROM nexus.chunks_384 WHERE collection = '{name}'"
        )
        assert rows == ["4"]

    def test_copy_not_move_source_store_intact(
        self, vec_etl_vector_client, tmp_path
    ) -> None:
        name = _coll("etlint3")
        store, ids, texts = _make_local_store(tmp_path, name, 3)

        migrate_local(store, vec_etl_vector_client)

        client = chromadb.PersistentClient(path=str(store))
        col = client.get_collection(name)
        assert col.count() == 3
        got = col.get(include=["documents"])
        assert sorted(got["ids"]) == sorted(ids)
        assert sorted(got["documents"]) == sorted(texts)

    def test_rollback_clears_pgvector_only(
        self, vec_etl_pg_instance, vec_etl_vector_client, tmp_path
    ) -> None:
        pg = vec_etl_pg_instance
        name = _coll("etlint4")
        store, _, _ = _make_local_store(tmp_path, name, 6)
        migrate_local(store, vec_etl_vector_client)
        assert _psql_rows(
            pg, f"SELECT count(*) FROM nexus.chunks_384 WHERE collection = '{name}'"
        ) == ["6"]

        from nexus.migration.chroma_read import open_local_read_client

        deleted = rollback_collections(
            open_local_read_client(store), vec_etl_vector_client, collections=[name]
        )

        assert deleted == {name: 6}
        assert _psql_rows(
            pg, f"SELECT count(*) FROM nexus.chunks_384 WHERE collection = '{name}'"
        ) == ["0"]
        client = chromadb.PersistentClient(path=str(store))
        assert client.get_collection(name).count() == 6


@pytest.mark.integration
@_SKIP_INTEGRATION
class TestManifestFkIntegration:
    """(c) manifest FK: documents.tumbler → catalog_document_chunks
    (collection, chash) → chunks_<dim> row resolves — validated by the
    DIRECT-SQL artifacts, never by fetchDocumentChunks (P2.1: that API
    fails loud on partial documents by design)."""

    def test_backfill_then_orphan_detection(
        self, vec_etl_pg_instance, vec_etl_vector_client, tmp_path
    ) -> None:
        pg = vec_etl_pg_instance
        name = _coll("etlmanifest")
        store, ids, _ = _make_local_store(tmp_path, name, 5)
        try:
            self._run_manifest_flow(pg, name, ids, store, vec_etl_vector_client)
        finally:
            # The PG instance is module-scoped: clear the seeded catalog +
            # 768-lane rows so the deliberate orphan cannot leak into any
            # test added to this module later.
            _psql_exec(
                pg,
                "DELETE FROM nexus.catalog_document_chunks"
                " WHERE doc_id IN ('9000.1', '9000.2');"
                " DELETE FROM nexus.catalog_documents"
                " WHERE tumbler IN ('9000.1', '9000.2');"
                " DELETE FROM nexus.catalog_owners"
                " WHERE tumbler_prefix = '9000';"
                " DELETE FROM nexus.chunks_768"
                f" WHERE collection = 'docs__etlv__{_MODEL_768}__v1'",
            )

    def _run_manifest_flow(self, pg, name, ids, store, vec_etl_vector_client) -> None:
        # Manifest fixture: owner -> document (physical_collection = the
        # migrated collection) -> 5 chunk rows, collection NOT yet
        # backfilled (NULL — the pre-Phase-5 state).
        _psql_exec(
            pg,
            "INSERT INTO nexus.catalog_owners"
            " (tenant_id, tumbler_prefix, name, owner_type)"
            " VALUES ('default', '9000', 'etl-owner', 'repo')",
        )
        _psql_exec(
            pg,
            "INSERT INTO nexus.catalog_documents"
            " (tenant_id, tumbler, title, physical_collection)"
            f" VALUES ('default', '9000.1', 'ETL Doc', '{name}')",
        )
        for pos, chash in enumerate(ids):
            _psql_exec(
                pg,
                "INSERT INTO nexus.catalog_document_chunks"
                " (tenant_id, doc_id, position, chash)"
                f" VALUES ('default', '9000.1', {pos}, '{chash}')",
            )

        migrate_local(store, vec_etl_vector_client)

        # Backfill stamps collection from the owning document; idempotent.
        _psql_exec(pg, manifest_backfill_sql())
        backfilled = _psql_rows(
            pg,
            "SELECT count(*) FROM nexus.catalog_document_chunks"
            f" WHERE doc_id = '9000.1' AND collection = '{name}'",
        )
        assert backfilled == ["5"]

        # Clean state: zero orphans at 384.
        assert _psql_rows(pg, manifest_orphan_sql(384)) == []

        # Cross-dim scoping: a 768-collection manifest row resolved by a
        # chunks_768 row must NOT appear as a 384 orphan (and is clean at
        # its own dim).
        name768 = f"docs__etlv__{_MODEL_768}__v1"
        chash768 = _chash("a 768-lane chunk")
        vec768 = "[" + ",".join(["0"] * 768) + "]"
        _psql_exec(
            pg,
            "INSERT INTO nexus.catalog_documents"
            " (tenant_id, tumbler, title, physical_collection)"
            f" VALUES ('default', '9000.2', 'ETL Doc 768', '{name768}')",
        )
        _psql_exec(
            pg,
            "INSERT INTO nexus.catalog_document_chunks"
            " (tenant_id, doc_id, position, chash, collection)"
            f" VALUES ('default', '9000.2', 0, '{chash768}', '{name768}')",
        )
        _psql_exec(
            pg,
            "INSERT INTO nexus.chunks_768"
            " (tenant_id, collection, chash, chunk_text, embedding)"
            f" VALUES ('default', '{name768}', '{chash768}',"
            f" 'a 768-lane chunk', '{vec768}')",
        )
        assert _psql_rows(pg, manifest_orphan_sql(384)) == []
        assert _psql_rows(pg, manifest_orphan_sql(768)) == []

        # Deliberate orphan: a manifest row whose chash was never migrated
        # MUST be detected (non-vacuous validation).
        bogus = "feedfacefeedfacefeedfacefeedface"
        _psql_exec(
            pg,
            "INSERT INTO nexus.catalog_document_chunks"
            " (tenant_id, doc_id, position, chash, collection)"
            f" VALUES ('default', '9000.1', 99, '{bogus}', '{name}')",
        )
        orphans = _psql_rows(pg, manifest_orphan_sql(384))
        assert len(orphans) == 1
        assert bogus in orphans[0]


@pytest.mark.integration
@_SKIP_INTEGRATION
class TestTaxonomyConsistencyIntegration:
    def test_source_collection_resolution_post_etl(
        self, vec_etl_vector_client, tmp_path
    ) -> None:
        """(d) against the real service: assignments pointing at the
        migrated collection resolve; a never-migrated collection is
        reported as exactly the unresolved set."""
        name = _coll("etltaxint")
        ghost = _coll("etltaxint-ghost")
        store, _, _ = _make_local_store(tmp_path, name, 3)
        migrate_local(store, vec_etl_vector_client)

        clean_db = _make_t2_with_assignments(tmp_path / "clean", [name, name])
        assert verify_taxonomy_consistency(clean_db, vec_etl_vector_client) == []

        drift_db = _make_t2_with_assignments(tmp_path / "drift", [name, ghost])
        assert verify_taxonomy_consistency(drift_db, vec_etl_vector_client) == [ghost]


# ── Additive (nexus-rvfwj, 2026-06-10): NUL-byte boundary documentation ──────


class TestNulByteBoundary:
    """NUL (0x00) handling — documents the DELIBERATE fake/real divergence.

    The ETL transfers chunk text VERBATIM (vector-identity decision (a)),
    including NUL bytes that Chroma/SQLite tolerated for years in
    PDF-extraction noise (62 of 5,233 production dt-papers chunks). The REAL
    service sanitizes NULs at the storage boundary — PgVectorRepository
    strips 0x00 from chunk text and metadata strings before embed+bind,
    because Postgres text/jsonb physically cannot store them (bead
    nexus-rvfwj; Java contract test
    PgVectorRepositoryContractTest.upsert_nulBytesInTextAndMetadata_sanitizedNotRejected).

    FakeVectorClient deliberately does NOT sanitize: this suite pins the
    ETL's own obligation (send verbatim), not the service's storage
    behavior. Consequence on the real side, by design: for NUL-bearing
    chunks sha256(stored_text)[:32] != chash — the chash is carried as the
    caller's identity and never recomputed from stored text.
    """

    def test_etl_sends_nul_text_verbatim_fake_stores_passthrough(
        self, source_client
    ) -> None:
        name = _coll("etlnul")
        dirty = "nul\x00 bearing \x00\x00chunk"
        cid = _chash(dirty)
        col = source_client.get_or_create_collection(name)
        col.add(
            ids=[cid],
            documents=[dirty],
            metadatas=[{"tag": "etl\x00meta"}],
            embeddings=[[1.0, 0.0]],
        )

        fake = FakeVectorClient()
        report = migrate_collections(source_client, fake, leg="local")

        assert report.ok is True
        assert report.total_written == 1
        # The ETL sent the NUL-bearing text VERBATIM — byte-identical,
        # NULs included. Sanitization is the service's job, not the ETL's.
        doc, meta = fake.store[name][cid]
        assert doc == dirty
        assert meta == {"tag": "etl\x00meta"}
