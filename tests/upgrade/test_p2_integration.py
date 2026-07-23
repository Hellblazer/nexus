# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-185 P2.V (nexus-n7u38.21): Phase-2 integration on real substrates.

A REAL Chroma EphemeralClient source (the actual RDR-176 read leg:
``iter_collection_chunks`` paging through ``ChromaReadSource``) holding
the incident shape — legacy 16-char ids, an identical-text pair, mixed
conformant rows — driven through the wire re-id seam end to end, with
crash-ordering resume, cascade against real-DDL tmp SQLite, and
rollback-via-map. EXACT assertions throughout (fixture-regression
directive). The real-pgvector leg belongs to the P4 era-spanning
rehearsal; the target here is a recording double behind the EtlTarget
port.
"""
from __future__ import annotations

import hashlib
import pathlib
import sqlite3
from typing import Any

import pytest

from nexus.migration.etl_ports import ChromaReadSource, run_batched_etl
from nexus.migration.remap_cascade import cascade_remap
from nexus.migration.vector_etl import rollback_collections
from nexus.migration.detection import CollectionClassification
from nexus.migration.wire_reid import ChashRemapStore, make_wire_reid_transform
from nexus.upgrade_ladder.rungs.substrate_etl import (
    SourceGoneDecision,
    SubstratePlan,
    plan_substrate_legs,
    run_substrate_migration,
)
from tests.conftest import make_vector_test_client

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _isolate_watermarks(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))

COLL = "knowledge__old_store"
TEXT_DUP = "duplicated note text"
TEXT_UNIQ = "unique note text"
TEXT_CONF = "conformant row text"


def _sha32(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _sha64(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def chroma_source() -> Any:
    chromadb = pytest.importorskip("chromadb")
    client = make_vector_test_client()
    # EphemeralClient shares process state (known gotcha): a collection
    # created by a previous test in this process is still there. Clean slate.
    try:
        client.delete_collection(COLL)
    except Exception:  # noqa: BLE001 — absent collection on first test
        pass
    col = client.create_collection(COLL)
    # The incident shape: legacy 16-char ids (pre-RDR-108), one identical-text
    # pair, plus one 32-hex half-digest row (the pre-RDR-180 "conformant"
    # shape — itself legacy since the full-digest cutover, so the transform
    # re-maps it too). Embeddings supplied explicitly so the fixture never
    # invokes an embedder (deterministic, offline).
    col.add(
        ids=["legacy-0000000001", "legacy-0000000002", "legacy-0000000003", _sha32(TEXT_CONF)],
        documents=[TEXT_DUP, TEXT_DUP, TEXT_UNIQ, TEXT_CONF],
        metadatas=[
            {"chunk_text_hash": _sha64(TEXT_DUP)},
            {"chunk_text_hash": _sha64(TEXT_DUP)},
            {"chunk_text_hash": _sha64(TEXT_UNIQ)},
            {"chunk_text_hash": _sha64(TEXT_CONF)},
        ],
        embeddings=[[0.1, 0.2], [0.1, 0.2], [0.3, 0.4], [0.5, 0.6]],
    )
    return client


class RecordingTarget:
    def __init__(self, crash_after_batches: int | None = None) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.batches = 0
        self._crash_after = crash_after_batches

    def upsert_chunks(self, collection, ids, documents, metadatas, *, embeddings=None):
        if self._crash_after is not None and self.batches >= self._crash_after:
            raise ValueError("simulated crash between map persist and target write")
        self.batches += 1
        for cid, doc, meta in zip(ids, documents, metadatas):
            self.rows[cid] = {"doc": doc, "meta": meta}

    def count(self, collection: str) -> int:
        return len(self.rows)


class RollbackVectorClient:
    """The rollback-side vector double: per-collection row sets with the
    handle.get/delete + count contract rollback_collections drives."""

    def __init__(self, rows: dict[str, set[str]]) -> None:
        self._rows = rows

    def get_or_create_collection(self, name: str):
        rows = self._rows.setdefault(name, set())

        class _Handle:
            def get(self, ids=None, limit=None):
                return {"ids": [i for i in ids if i in rows]}

            def delete(self, ids):
                for i in ids:
                    rows.discard(i)

        return _Handle()

    def count(self, name: str) -> int:
        return len(self._rows.get(name, set()))


def test_incident_shape_converges_end_to_end(
    chroma_source: Any, tmp_path: pathlib.Path
) -> None:
    """Legacy Chroma collection → conformant target, exact rows, exact map,
    source byte-untouched."""
    source = ChromaReadSource(chroma_source)
    target = RecordingTarget()
    with ChashRemapStore(tmp_path / "chash_remap.db") as store:
        transform = make_wire_reid_transform(
            store, source_collection=COLL, target_collection=COLL, provenance="p2v"
        )
        result = run_batched_etl(
            source, target, source_collection=COLL, target_collection=COLL,
            page=2,  # forces multi-batch paging through the real read leg
            transform=transform,
        )
        assert result.ok is True
        assert result.source_count == 4
        assert result.written == 3  # the identical-text pair collapsed

        assert set(target.rows) == {_sha64(TEXT_DUP), _sha64(TEXT_UNIQ), _sha64(TEXT_CONF)}
        # Metadata (incl. chunk_text_hash) carried through to the new id.
        assert target.rows[_sha64(TEXT_DUP)]["meta"]["chunk_text_hash"] == _sha64(TEXT_DUP)

        # The map: all four source ids — the three 16-char legacies AND the
        # 32-hex half-digest row (legacy since RDR-180's full-digest cutover).
        assert store.entries_for_collection(COLL) == {
            "legacy-0000000001": _sha64(TEXT_DUP),
            "legacy-0000000002": _sha64(TEXT_DUP),
            "legacy-0000000003": _sha64(TEXT_UNIQ),
            _sha32(TEXT_CONF): _sha64(TEXT_CONF),
        }

    # Source byte-untouched (RDR-176): same ids, same count, same order.
    col = chroma_source.get_collection(COLL)
    assert col.count() == 4
    assert sorted(col.get(include=[])["ids"]) == sorted(
        ["legacy-0000000001", "legacy-0000000002", "legacy-0000000003", _sha32(TEXT_CONF)]
    )


def test_crash_ordering_resume_converges(
    chroma_source: Any, tmp_path: pathlib.Path
) -> None:
    """Crash after the FIRST target batch (map already durable for the
    second batch too, since its transform ran? No — batches interleave:
    transform(b1)→upsert(b1)→transform(b2)→CRASH before upsert(b2)). The
    map holds every id transformed so far; the re-run converges to the
    exact same terminal state — never target-without-map."""
    source = ChromaReadSource(chroma_source)
    crash_target = RecordingTarget(crash_after_batches=1)
    map_path = tmp_path / "chash_remap.db"
    with ChashRemapStore(map_path) as store:
        transform = make_wire_reid_transform(
            store, source_collection=COLL, target_collection=COLL, provenance="run-1"
        )
        result = run_batched_etl(
            source, crash_target, source_collection=COLL, target_collection=COLL,
            page=2, transform=transform,
        )
        assert result.ok is False
        assert "simulated crash" in result.reason
        written_before_crash = dict(crash_target.rows)
        # INVARIANT: every target row's id is in the map (post-RDR-180 every
        # seeded id re-maps, the 32-hex half-digest included) —
        # target-without-map is unrepresentable.
        mapped = set(store.entries_for_collection(COLL).values())
        for cid in written_before_crash:
            assert cid in mapped

    # Resume: fresh run, same map store, healthy target.
    with ChashRemapStore(map_path) as store:
        transform = make_wire_reid_transform(
            store, source_collection=COLL, target_collection=COLL, provenance="run-2"
        )
        target = RecordingTarget()
        result = run_batched_etl(
            ChromaReadSource(chroma_source), target,
            source_collection=COLL, target_collection=COLL, page=2, transform=transform,
        )
        assert result.ok is True
        assert set(target.rows) == {_sha64(TEXT_DUP), _sha64(TEXT_UNIQ), _sha64(TEXT_CONF)}
        # Deterministic re-derivation: the map is unchanged by the resume.
        assert store.entries_for_collection(COLL) == {
            "legacy-0000000001": _sha64(TEXT_DUP),
            "legacy-0000000002": _sha64(TEXT_DUP),
            "legacy-0000000003": _sha64(TEXT_UNIQ),
            _sha32(TEXT_CONF): _sha64(TEXT_CONF),
        }


def test_rollback_via_map_against_real_source(
    chroma_source: Any, tmp_path: pathlib.Path
) -> None:
    """Rollback drives the REAL Chroma read leg for the source id stream and
    deletes exactly the migrated rows via the map — exact counts."""
    with ChashRemapStore(tmp_path / "chash_remap.db") as store:
        transform = make_wire_reid_transform(
            store, source_collection=COLL, target_collection=COLL, provenance="p"
        )
        target = RecordingTarget()
        run_batched_etl(
            ChromaReadSource(chroma_source), target,
            source_collection=COLL, target_collection=COLL, page=2, transform=transform,
        )
        vector = RollbackVectorClient({COLL: set(target.rows)})
        deleted = rollback_collections(
            chroma_source, vector, collections=[COLL], remap_store=store
        )
        assert deleted == {COLL: 3}  # 2 collapsed + 1 unique + 1 conformant = 3 rows
        assert vector.count(COLL) == 0
    # Source still byte-untouched after rollback.
    assert chroma_source.get_collection(COLL).count() == 4


def test_cascade_on_real_ddl_after_migration(
    chroma_source: Any, tmp_path: pathlib.Path
) -> None:
    """The full local flow: migrate (building the map), then cascade the
    real-DDL local stores — manifest positions preserved, topic collapse
    deduped, exact rows."""
    catalog_db = tmp_path / "catalog.db"
    memory_db = tmp_path / "memory.db"
    conn = sqlite3.connect(catalog_db)
    conn.executescript(
        "CREATE TABLE document_chunks (doc_id TEXT NOT NULL, position INTEGER NOT NULL,"
        " chash TEXT NOT NULL, chunk_index INTEGER, PRIMARY KEY (doc_id, position));"
    )
    conn.executemany(
        "INSERT INTO document_chunks (doc_id, position, chash) VALUES (?,?,?)",
        [
            ("1.2.3", 0, "legacy-0000000001"),
            ("1.2.3", 1, "legacy-0000000002"),
            ("1.2.3", 2, "legacy-0000000003"),
        ],
    )
    conn.commit()
    conn.close()
    conn = sqlite3.connect(memory_db)
    conn.executescript(
        "CREATE TABLE chash_index (chash TEXT NOT NULL, physical_collection TEXT NOT NULL,"
        " created_at TEXT NOT NULL, PRIMARY KEY (chash, physical_collection));"
        "CREATE TABLE topic_assignments (doc_id TEXT NOT NULL, topic_id INTEGER NOT NULL,"
        " assigned_by TEXT NOT NULL DEFAULT 'x', PRIMARY KEY (doc_id, topic_id));"
        "CREATE TABLE frecency (chunk_id TEXT PRIMARY KEY, embedded_at TEXT NOT NULL DEFAULT '',"
        " ttl_days INTEGER NOT NULL DEFAULT 0, frecency_score REAL NOT NULL DEFAULT 0,"
        " miss_count INTEGER NOT NULL DEFAULT 0, last_hit_at TEXT NOT NULL DEFAULT '');"
        "CREATE TABLE relevance_log (id INTEGER PRIMARY KEY, query TEXT NOT NULL,"
        " chunk_id TEXT NOT NULL, collection TEXT, action TEXT NOT NULL, timestamp TEXT NOT NULL);"
        "CREATE TABLE document_aspects (collection TEXT NOT NULL, source_path TEXT NOT NULL,"
        " source_uri TEXT, extracted_at TEXT NOT NULL DEFAULT '', PRIMARY KEY (collection, source_path));"
        "CREATE TABLE aspect_extraction_queue (collection TEXT NOT NULL, source_path TEXT NOT NULL,"
        " content_hash TEXT NOT NULL DEFAULT '', PRIMARY KEY (collection, source_path));"
    )
    conn.executemany(
        "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?,?)",
        [("legacy-0000000001", 7), ("legacy-0000000002", 7)],  # collapse pair, same topic
    )
    conn.commit()
    conn.close()

    with ChashRemapStore(tmp_path / "chash_remap.db") as store:
        transform = make_wire_reid_transform(
            store, source_collection=COLL, target_collection=COLL, provenance="p"
        )
        run_batched_etl(
            ChromaReadSource(chroma_source), RecordingTarget(),
            source_collection=COLL, target_collection=COLL, page=2, transform=transform,
        )
        results = cascade_remap(store, catalog_db=catalog_db, memory_db=memory_db)

    by = {r.store: r for r in results}
    assert by["document_chunks"].rewritten == 3
    assert by["topic_assignments"].rewritten == 1
    assert by["topic_assignments"].deduped == 1  # the collapse sibling

    conn = sqlite3.connect(catalog_db)
    manifest = conn.execute(
        "SELECT position, chash FROM document_chunks ORDER BY position"
    ).fetchall()
    conn.close()
    # Positions preserved; the collapsed pair points at ONE chash at two rows.
    assert manifest == [
        (0, _sha64(TEXT_DUP)), (1, _sha64(TEXT_DUP)), (2, _sha64(TEXT_UNIQ)),
    ]
    conn = sqlite3.connect(memory_db)
    topics = conn.execute("SELECT doc_id, topic_id FROM topic_assignments").fetchall()
    conn.close()
    assert topics == [(_sha64(TEXT_DUP), 7)]


def test_chained_plan_execute_cascade_rollback(
    chroma_source: Any, tmp_path: pathlib.Path
) -> None:
    """P2 critique High-2: the audit §2 ordering exercised as ONE chain —
    plan_substrate_legs → run_substrate_migration (execute_leg per leg,
    then the 7-store cascade, in order) → rollback_collections — against
    one shared map and the same fixtures. Exact assertions throughout."""
    classification = CollectionClassification(
        collection=COLL,
        leg="local",
        model="voyage-context-3",
        dim=1024,
        support="unsupported",  # legacy probe flips support; model stays wired
        source_count=4,
        has_data=True,
        legacy_ids=True,
    )
    plan = plan_substrate_legs(
        [classification], prior_collections=frozenset(), voyage_key_present=True
    )
    assert len(plan.legs) == 1
    assert plan.legs[0].needs_reid is True
    assert plan.legs[0].needs_reembed is False  # wired model: re-id only
    assert plan.billed_reembed is False

    catalog_db = tmp_path / "catalog.db"
    memory_db = tmp_path / "memory.db"
    conn = sqlite3.connect(catalog_db)
    conn.executescript(
        "CREATE TABLE document_chunks (doc_id TEXT NOT NULL, position INTEGER NOT NULL,"
        " chash TEXT NOT NULL, PRIMARY KEY (doc_id, position));"
    )
    conn.execute(
        "INSERT INTO document_chunks VALUES ('1.1', 0, 'legacy-0000000001')"
    )
    conn.commit()
    conn.close()
    conn = sqlite3.connect(memory_db)
    conn.executescript(
        "CREATE TABLE chash_index (chash TEXT NOT NULL, physical_collection TEXT NOT NULL,"
        " created_at TEXT NOT NULL, PRIMARY KEY (chash, physical_collection));"
        "CREATE TABLE topic_assignments (doc_id TEXT NOT NULL, topic_id INTEGER NOT NULL,"
        " PRIMARY KEY (doc_id, topic_id));"
        "CREATE TABLE frecency (chunk_id TEXT PRIMARY KEY, embedded_at TEXT NOT NULL DEFAULT '',"
        " ttl_days INTEGER NOT NULL DEFAULT 0, frecency_score REAL NOT NULL DEFAULT 0,"
        " miss_count INTEGER NOT NULL DEFAULT 0, last_hit_at TEXT NOT NULL DEFAULT '');"
        "CREATE TABLE relevance_log (id INTEGER PRIMARY KEY, query TEXT NOT NULL,"
        " chunk_id TEXT NOT NULL, collection TEXT, action TEXT NOT NULL, timestamp TEXT NOT NULL);"
        "CREATE TABLE document_aspects (collection TEXT NOT NULL, source_path TEXT NOT NULL,"
        " source_uri TEXT, PRIMARY KEY (collection, source_path));"
        "CREATE TABLE aspect_extraction_queue (collection TEXT NOT NULL, source_path TEXT NOT NULL,"
        " PRIMARY KEY (collection, source_path));"
    )
    conn.commit()
    conn.close()

    source = ChromaReadSource(chroma_source)
    target = RecordingTarget()
    with ChashRemapStore(tmp_path / "chash_remap.db") as store:
        leg_results, cascade_results = run_substrate_migration(
            plan, source, target,
            map_store=store, catalog_db=catalog_db, memory_db=memory_db,
            page=2, provenance="chain",
        )
        assert [r.ok for r in leg_results] == [True]
        assert leg_results[0].written == 3  # collapse
        by = {r.store: r for r in cascade_results}
        assert by["document_chunks"].rewritten == 1
        assert all(r.ok for r in cascade_results)

        # Rollback via the SAME map, same source stream.
        vector = RollbackVectorClient({COLL: set(target.rows)})
        deleted = rollback_collections(
            chroma_source, vector, collections=[COLL], remap_store=store
        )
        assert deleted == {COLL: 3}
        assert vector.count(COLL) == 0

    conn = sqlite3.connect(catalog_db)
    assert conn.execute("SELECT chash FROM document_chunks").fetchall() == [
        (_sha64(TEXT_DUP),)
    ]
    conn.close()
    # Source untouched through the entire chain.
    assert chroma_source.get_collection(COLL).count() == 4


def test_chain_refuses_unresolved_decisions(tmp_path: pathlib.Path) -> None:
    """Consent is never implicit: a plan carrying a source-gone decision
    refuses to run."""
    plan = SubstratePlan(decisions=[SourceGoneDecision("knowledge__vanished")])
    with ChashRemapStore(tmp_path / "chash_remap.db") as store:
        with pytest.raises(RuntimeError, match="unresolved genuine decisions"):
            run_substrate_migration(
                plan, None, None,  # type: ignore[arg-type]
                map_store=store, catalog_db=tmp_path / "c.db",
                memory_db=tmp_path / "m.db", page=2, provenance="p",
            )
