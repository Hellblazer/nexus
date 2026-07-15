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

from pathlib import Path

import pytest

from nexus.db.t2 import T2Database


@pytest.fixture
def db(tmp_path: Path) -> T2Database:
    with T2Database(tmp_path / "t2.db") as database:
        yield database


def _seed_topic(db: T2Database, label: str, collection: str) -> int:
    cur = db.taxonomy.conn.execute(
        "INSERT INTO topics (label, collection, doc_count, created_at) VALUES (?, ?, ?, ?)",
        (label, collection, 1, "2026-07-14T00:00:00Z"),
    )
    db.taxonomy.conn.commit()
    return cur.lastrowid


def _link_count(db: T2Database) -> int:
    return db.taxonomy.conn.execute("SELECT COUNT(*) FROM topic_links").fetchone()[0]


def _orphan_link_count(db: T2Database) -> int:
    return db.taxonomy.conn.execute(
        "SELECT COUNT(*) FROM topic_links l "
        "LEFT JOIN topics f ON l.from_topic_id = f.id "
        "LEFT JOIN topics t ON l.to_topic_id = t.id "
        "WHERE f.id IS NULL OR t.id IS NULL"
    ).fetchone()[0]


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
        assert _orphan_link_count(db) == 0

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
        assert _orphan_link_count(db) == 0
        survivors = db.taxonomy.conn.execute(
            "SELECT id FROM topics WHERE collection = ?", ("docs__y",),
        ).fetchall()
        assert [row[0] for row in survivors] == [outside]

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

        rows = db.taxonomy.conn.execute(
            "SELECT from_topic_id, to_topic_id FROM topic_links"
        ).fetchall()
        assert rows == [(b, c)]
        assert _orphan_link_count(db) == 0

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

        rows = db.taxonomy.conn.execute(
            "SELECT from_topic_id, to_topic_id FROM topic_links"
        ).fetchall()
        assert rows == [(tgt, other)]
        assert _orphan_link_count(db) == 0

    def test_purge_assignments_for_doc_removes_links_of_emptied_topics(
        self, db: T2Database,
    ) -> None:
        a = _seed_topic(db, "topic-a", "proj")
        b = _seed_topic(db, "topic-b", "proj")
        for doc_id, topic_id in [("note1", a), ("note2", b)]:
            db.taxonomy.conn.execute(
                "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
                (doc_id, topic_id),
            )
        db.taxonomy.conn.commit()
        db.taxonomy.upsert_topic_links(
            [{"from_topic_id": a, "to_topic_id": b, "link_count": 1, "link_types": ["relates"]}]
        )

        removed = db.taxonomy.purge_assignments_for_doc("proj", "note1")

        assert removed == 1
        remaining = db.taxonomy.conn.execute(
            "SELECT id FROM topics ORDER BY id"
        ).fetchall()
        assert [row[0] for row in remaining] == [b]
        assert _link_count(db) == 0
        assert _orphan_link_count(db) == 0

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

        rows = db.taxonomy.conn.execute(
            "SELECT from_topic_id, to_topic_id FROM topic_links"
        ).fetchall()
        assert rows == [(y1, y2)]
        assert _orphan_link_count(db) == 0
