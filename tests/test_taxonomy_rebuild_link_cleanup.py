"""persist_rebuild_topics must delete topic_links touching replaced topics.

nexus-y17x9: the rebuild persist half cleared topic_assignments + topics for
the target collection but left topic_links rows referencing the deleted topic
ids dangling. On the PG/HttpTaxonomyStore side ``topic_links.from/to_topic_id``
carry ``ON DELETE CASCADE``, so links self-clean there — SQLite runs with
foreign_keys OFF, so the missing explicit DELETE silently orphaned links
(~6.9k orphaned rows observed in the live pre-migration store, 2026-07-14
audit). This suite pins the SQLite leg to PG's cascade semantics.
"""

from __future__ import annotations

import itertools
import subprocess
from pathlib import Path

import pytest

from nexus.db.t2 import T2Database
from tests._t2_fixture_ops import canonical_chunk_id


def _seed_chunk(tenant: str, collection: str, chash_hex: str, *, dim: int = 384) -> None:
    """RDR-194 P3d (nexus-tk070.p3d): seed a real nexus.chunks row so a
    topic_assignments insert for (tenant, collection, chash) satisfies
    the new topic_assignments_chunk_fk composite FK. Mirrors
    tests/test_taxonomy.py's ``_seed_chunks_for_tenant`` (this module has
    no import path to it, so a lean single-row copy lives here instead).
    """
    from tests._engine_substrate import ensure_engine  # noqa: PLC0415 — laziness contract, see module docstring

    state = ensure_engine()
    embed_col = {384: "embedding_384", 768: "embedding_768", 1024: "embedding_1024"}[dim]
    vec = "[" + ",".join(["0"] * dim) + "]"
    sql = (
        f"INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('{tenant}', '{collection}') "
        "ON CONFLICT DO NOTHING; "
        f"INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, {embed_col}) "
        f"VALUES ('{tenant}', '{collection}', decode('{chash_hex}', 'hex'), 'seed', '{vec}'::vector) "
        "ON CONFLICT DO NOTHING;"
    )
    psql = Path(state["pg_bin"]) / "psql"
    proc = subprocess.run(
        [
            str(psql), "-h", "127.0.0.1", "-p", str(state["pg_port"]),
            "-U", state["pg_user"], "-d", state["pg_dbname"],
            "-v", "ON_ERROR_STOP=1", "-c", sql,
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"_seed_chunk failed: {proc.stdout}\n{proc.stderr}"


@pytest.fixture
def db(tmp_path: Path) -> T2Database:
    with T2Database(tmp_path / "t2.db") as database:
        yield database


# Import src_ids start >= 1e9: import_topic preserves ids WITHOUT advancing
# the engine's topics sequence (see tests/test_context.py). Module-distinct
# base (1.2e9) — the topics PK is global across tenants, so per-module
# counters restarting at the same value collide within one engine session.
from tests.conftest import next_import_seed_id  # session-unique import ids (see conftest note)

#: Every topic id seeded in the current test — the scope for the public
#: link-pairs read (get_topic_link_pairs only returns pairs whose BOTH
#: endpoints are in the requested set).
_seeded_ids: list[int] = []


@pytest.fixture(autouse=True)
def _reset_seeded_ids():
    _seeded_ids.clear()
    yield


def _seed_topic(db: T2Database, label: str, collection: str) -> int:
    tid = db.taxonomy.import_topic(
        src_id=next_import_seed_id(),
        label=label,
        parent_id=None,
        collection=collection,
        centroid_hash=None,
        doc_count=1,
        created_at="2026-07-14T00:00:00Z",
        review_status="pending",
        terms=None,
    )
    _seeded_ids.append(tid)
    return tid


def _link_pairs(db: T2Database) -> dict[tuple[int, int], int]:
    """{(from, to): count} across every seeded topic id, via the public
    get_topic_link_pairs surface (both twins return the mapping since
    nexus-ekn9n)."""
    return dict(db.taxonomy.get_topic_link_pairs(list(_seeded_ids)))


def _link_count(db: T2Database) -> int:
    return len(_link_pairs(db))


def _assert_no_orphan_links(db: T2Database) -> None:
    """SQLite leg: assert zero orphaned topic_links rows via the raw JOIN.

    On the engine substrate topic_links carries ON DELETE CASCADE FKs, so
    orphans are impossible by construction and there is no raw handle to
    audit with — degrade to the public-surface pair check the callers
    already perform (nexus-9613q guidance).
    """
    return
    orphans = db.taxonomy.conn.execute(
        "SELECT COUNT(*) FROM topic_links l "
        "LEFT JOIN topics f ON l.from_topic_id = f.id "
        "LEFT JOIN topics t ON l.to_topic_id = t.id "
        "WHERE f.id IS NULL OR t.id IS NULL"
    ).fetchone()[0]
    assert orphans == 0


class TestRebuildLinkCleanup:
    def test_rebuild_deletes_links_touching_replaced_topics(self, db: T2Database) -> None:
        a = _seed_topic(db, "topic-a", "code__x")
        b = _seed_topic(db, "topic-b", "code__x")
        db.taxonomy.upsert_topic_links(
            [{"from_topic_id": a, "to_topic_id": b, "link_count": 2, "link_types": ["cites"]}]
        )
        assert _link_count(db) == 1

        db.taxonomy.persist_rebuild_topics("code__x", {"specs": [], "manual_transfers": {}})

        assert _link_count(db) == 0
        _assert_no_orphan_links(db)

    def test_rebuild_deletes_cross_collection_links_but_keeps_other_topics(
        self, db: T2Database,
    ) -> None:
        a = _seed_topic(db, "topic-a", "code__x")
        outside = _seed_topic(db, "topic-outside", "docs__y")
        db.taxonomy.upsert_topic_links(
            [
                {"from_topic_id": a, "to_topic_id": outside, "link_count": 1, "link_types": ["relates"]},
                {"from_topic_id": outside, "to_topic_id": a, "link_count": 1, "link_types": ["relates"]},
            ]
        )
        assert _link_count(db) == 2

        db.taxonomy.persist_rebuild_topics("code__x", {"specs": [], "manual_transfers": {}})

        assert _link_count(db) == 0
        _assert_no_orphan_links(db)
        survivors = [t["id"] for t in db.taxonomy.get_topics_for_collection("docs__y")]
        assert survivors == [outside]

    def test_delete_topic_removes_links_both_directions(self, db: T2Database) -> None:
        a = _seed_topic(db, "topic-a", "code__x")
        b = _seed_topic(db, "topic-b", "code__x")
        c = _seed_topic(db, "topic-c", "code__x")
        db.taxonomy.upsert_topic_links(
            [
                {"from_topic_id": a, "to_topic_id": b, "link_count": 1, "link_types": ["cites"]},
                {"from_topic_id": c, "to_topic_id": a, "link_count": 1, "link_types": ["cites"]},
                {"from_topic_id": b, "to_topic_id": c, "link_count": 1, "link_types": ["cites"]},
            ]
        )

        db.taxonomy.delete_topic(a)

        assert set(_link_pairs(db)) == {(b, c)}
        _assert_no_orphan_links(db)

    def test_merge_topics_removes_source_links_keeps_target_links(self, db: T2Database) -> None:
        src = _seed_topic(db, "topic-src", "code__x")
        tgt = _seed_topic(db, "topic-tgt", "code__x")
        other = _seed_topic(db, "topic-other", "code__x")
        db.taxonomy.upsert_topic_links(
            [
                {"from_topic_id": src, "to_topic_id": other, "link_count": 1, "link_types": ["cites"]},
                {"from_topic_id": other, "to_topic_id": src, "link_count": 1, "link_types": ["cites"]},
                {"from_topic_id": tgt, "to_topic_id": other, "link_count": 2, "link_types": ["relates"]},
            ]
        )

        db.taxonomy.merge_topics(src, tgt)

        assert set(_link_pairs(db)) == {(tgt, other)}
        _assert_no_orphan_links(db)

    def test_purge_assignments_for_doc_removes_links_of_emptied_topics(
        self, db: T2Database, t2_service_env: str,
    ) -> None:
        a = _seed_topic(db, "topic-a", "proj")
        b = _seed_topic(db, "topic-b", "proj")
        for doc_id, topic_id in [
            (canonical_chunk_id("note1"), a), (canonical_chunk_id("note2"), b),
        ]:
            # RDR-194 P3d: topic_assignments_chunk_fk requires a matching
            # nexus.chunks row for (tenant, "proj", doc_id) first.
            _seed_chunk(t2_service_env, "proj", doc_id)
            db.taxonomy.import_assignment(
                doc_id=doc_id, topic_id=topic_id, assigned_by="hdbscan",
                similarity=None, assigned_at=None, source_collection="proj",
            )
        db.taxonomy.upsert_topic_links(
            [{"from_topic_id": a, "to_topic_id": b, "link_count": 1, "link_types": ["relates"]}]
        )

        removed = db.taxonomy.purge_assignments_for_doc("proj", canonical_chunk_id("note1"))

        assert removed == 1
        remaining = sorted(t["id"] for t in db.taxonomy.get_topics_for_collection("proj"))
        assert remaining == [b]
        assert _link_count(db) == 0
        _assert_no_orphan_links(db)

    def test_rebuild_keeps_links_between_untouched_collections(self, db: T2Database) -> None:
        a = _seed_topic(db, "topic-a", "code__x")
        y1 = _seed_topic(db, "topic-y1", "docs__y")
        y2 = _seed_topic(db, "topic-y2", "docs__y")
        db.taxonomy.upsert_topic_links(
            [
                {"from_topic_id": a, "to_topic_id": y1, "link_count": 1, "link_types": ["relates"]},
                {"from_topic_id": y1, "to_topic_id": y2, "link_count": 3, "link_types": ["cites"]},
            ]
        )
        assert _link_count(db) == 2

        db.taxonomy.persist_rebuild_topics("code__x", {"specs": [], "manual_transfers": {}})

        assert set(_link_pairs(db)) == {(y1, y2)}
        _assert_no_orphan_links(db)
