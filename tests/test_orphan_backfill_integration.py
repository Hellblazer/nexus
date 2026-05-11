# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for orphan-backfill: real Catalog.register +
write_manifest against a tmp-path catalog.

Pure-logic tests live in test_orphan_backfill.py; this file covers the
end-to-end path where catalog Documents get created and the manifest
table gets populated.

Beads: nexus-h2pm, nexus-4fw8, nexus-oa9k.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.catalog import orphan_backfill as ob
from nexus.catalog.catalog import Catalog


@pytest.fixture
def cat(tmp_path: Path) -> Catalog:
    """Tmp-path Catalog + minimal owner needed by orphan-backfill."""
    catalog_env = tmp_path / "catalog"
    catalog_env.mkdir()
    c = Catalog(catalog_env, catalog_env / ".catalog.db")
    # Register a curator owner matching the DEFAULT_COLLECTION_OWNER
    # entry for ``knowledge__*``. Owner_type='curator' means no repo_root
    # so source_uri normalization stays out of the picture.
    c.register_owner(
        "papers", "curator", repo_hash="",
    )
    return c


def _owner(cat: Catalog):
    from nexus.catalog.tumbler import Tumbler
    # Owner just-registered will be 1.<N> where N is the next sequence.
    # Look it up by name to avoid pinning the seq number.
    row = cat._db.execute(
        "SELECT tumbler_prefix FROM owners WHERE name = ? LIMIT 1",
        ("papers",),
    ).fetchone()
    assert row, "test fixture failed: papers curator not registered"
    return Tumbler.parse(row[0])


class TestRegisterDtLinked:
    def test_registers_one_doc_per_match_with_dt_uri(self, cat: Catalog) -> None:
        owner = _owner(cat)
        matches = [
            ob.DTMatch(
                title="Test Paper One",
                dt_uuid="UUID-AAAA-0001",
                dt_name="Test Paper One (DT name)",
                score=0.92,
                chunks=[
                    ob.ChunkRef(cid="c1", chash="abc111", chunk_index=0),
                    ob.ChunkRef(cid="c2", chash="abc222", chunk_index=1),
                ],
            ),
            ob.DTMatch(
                title="Test Paper Two",
                dt_uuid="UUID-BBBB-0002",
                dt_name="Test Paper Two",
                score=0.88,
                chunks=[ob.ChunkRef(cid="c3", chash="xyz333", chunk_index=0)],
            ),
        ]
        docs, links = ob.register_dt_linked(
            cat, owner, "knowledge__art-papers", matches,
        )
        assert docs == 2
        assert links == 3

        # Verify Documents written with the DT URI scheme.
        rows = cat._db.execute(
            "SELECT title, source_uri, physical_collection FROM documents "
            "WHERE physical_collection = ? ORDER BY title",
            ("knowledge__art-papers",),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][1].startswith("x-devonthink-item://UUID-AAAA")
        assert rows[1][1].startswith("x-devonthink-item://UUID-BBBB")

    def test_writes_chunks_manifest_in_position_order(
        self, cat: Catalog,
    ) -> None:
        owner = _owner(cat)
        matches = [
            ob.DTMatch(
                title="Manifest Order Test",
                dt_uuid="UUID-ORD-001",
                dt_name="Manifest Order Test",
                score=1.0,
                chunks=[
                    ob.ChunkRef(cid=f"c{i}", chash=f"h{i:02d}",
                                chunk_index=i)
                    for i in range(5)
                ],
            ),
        ]
        ob.register_dt_linked(
            cat, owner, "knowledge__art-papers", matches,
        )
        rows = cat._db.execute(
            "SELECT dc.position, dc.chash FROM document_chunks dc "
            "JOIN documents d ON d.tumbler = dc.doc_id "
            "WHERE d.title = ? ORDER BY dc.position",
            ("Manifest Order Test",),
        ).fetchall()
        assert len(rows) == 5
        positions = [r[0] for r in rows]
        chashes = [r[1] for r in rows]
        assert positions == [0, 1, 2, 3, 4]
        assert chashes == [f"h{i:02d}" for i in range(5)]

    def test_metadata_carries_backfill_provenance(self, cat: Catalog) -> None:
        import json
        owner = _owner(cat)
        matches = [
            ob.DTMatch(
                title="Provenance Test",
                dt_uuid="UUID-PROV-001",
                dt_name="Different DT Name",
                score=0.81,
                chunks=[ob.ChunkRef(cid="c1", chash="h1", chunk_index=0)],
            ),
        ]
        ob.register_dt_linked(
            cat, owner, "knowledge__art-papers", matches,
        )
        row = cat._db.execute(
            "SELECT metadata FROM documents WHERE title = ?",
            ("Provenance Test",),
        ).fetchone()
        meta = json.loads(row[0])
        assert meta.get("backfill_from") == "t3_orphan"
        assert meta.get("backfill_mode") == "dt_link"
        assert meta.get("dt_uuid") == "UUID-PROV-001"
        assert meta.get("dt_name") == "Different DT Name"
        assert meta.get("fuzzy_score") == 0.81


class TestRegisterSynthetic:
    def test_titled_groups_get_one_doc_each_with_synthetic_uri(
        self, cat: Catalog,
    ) -> None:
        owner = _owner(cat)
        groups = [
            ob.TitleGroup(
                title="Unmatched Paper Alpha",
                chunks=[ob.ChunkRef(cid="c1", chash="a1", chunk_index=0),
                        ob.ChunkRef(cid="c2", chash="a2", chunk_index=1)],
            ),
            ob.TitleGroup(
                title="Unmatched Paper Beta",
                chunks=[ob.ChunkRef(cid="c3", chash="b1", chunk_index=0)],
            ),
        ]
        docs, links = ob.register_synthetic(
            cat, owner, "knowledge__art-papers", groups,
        )
        assert docs == 2
        assert links == 3

        rows = cat._db.execute(
            "SELECT title, source_uri FROM documents "
            "WHERE physical_collection = ? ORDER BY title",
            ("knowledge__art-papers",),
        ).fetchall()
        assert len(rows) == 2
        assert all(r[1].startswith("nx-orphan-backfill://") for r in rows)
        # URI carries collection + title for operator legibility.
        assert "knowledge__art-papers/Unmatched Paper Alpha" in rows[0][1]

    def test_untitled_group_falls_back_to_per_chash_singletons(
        self, cat: Catalog,
    ) -> None:
        owner = _owner(cat)
        groups = [
            ob.TitleGroup(
                title="",
                chunks=[
                    ob.ChunkRef(cid="c1", chash="hash-001"),
                    ob.ChunkRef(cid="c2", chash="hash-002"),
                    ob.ChunkRef(cid="c3", chash="hash-003"),
                ],
            ),
        ]
        docs, links = ob.register_synthetic(
            cat, owner, "knowledge__art", groups,
        )
        # 3 chunks -> 3 singleton Documents (chash-based fallback).
        assert docs == 3
        assert links == 3
        rows = cat._db.execute(
            "SELECT source_uri FROM documents "
            "WHERE physical_collection = ?",
            ("knowledge__art",),
        ).fetchall()
        uris = sorted(r[0] for r in rows)
        assert uris == [
            "nx-orphan-backfill://knowledge__art/chash/hash-001",
            "nx-orphan-backfill://knowledge__art/chash/hash-002",
            "nx-orphan-backfill://knowledge__art/chash/hash-003",
        ]


class TestApplyCsv:
    def test_unmatched_csv_with_operator_uuid_creates_dt_linked_docs(
        self, cat: Catalog, tmp_path: Path,
    ) -> None:
        owner = _owner(cat)
        # Original gather would have produced these chunk lookups.
        chunk_lookup = {
            "Curated Title One": [
                ob.ChunkRef(cid="c1", chash="h1", chunk_index=0),
                ob.ChunkRef(cid="c2", chash="h2", chunk_index=1),
            ],
            "Curated Title Two": [
                ob.ChunkRef(cid="c3", chash="h3", chunk_index=0),
            ],
        }
        # Operator fills in operator_dt_uuid for two unmatched rows.
        csv_path = tmp_path / "unmatched.csv"
        csv_path.write_text(
            "title,chunk_count,operator_dt_uuid\n"
            "Curated Title One,2,DT-UUID-AAA\n"
            "Curated Title Two,1,DT-UUID-BBB\n"
        )
        docs, links = ob.apply_csv(
            cat, owner, "knowledge__art-papers", csv_path,
            chunk_lookup=chunk_lookup,
        )
        assert docs == 2
        assert links == 3
        rows = cat._db.execute(
            "SELECT title, source_uri FROM documents "
            "WHERE physical_collection = ? ORDER BY title",
            ("knowledge__art-papers",),
        ).fetchall()
        uris = {r[0]: r[1] for r in rows}
        assert uris["Curated Title One"] == "x-devonthink-item://DT-UUID-AAA"
        assert uris["Curated Title Two"] == "x-devonthink-item://DT-UUID-BBB"

    def test_low_confidence_approve_picks_candidate_uuid(
        self, cat: Catalog, tmp_path: Path,
    ) -> None:
        owner = _owner(cat)
        chunk_lookup = {
            "Borderline Paper": [
                ob.ChunkRef(cid="c1", chash="h1", chunk_index=0),
            ],
        }
        csv_path = tmp_path / "low_confidence.csv"
        csv_path.write_text(
            "title,candidate_dt_uuid,candidate_dt_name,score,"
            "chunk_count,operator_decision\n"
            "Borderline Paper,SUGGESTED-UUID,Suggested Name,0.62,1,approve\n"
        )
        docs, _ = ob.apply_csv(
            cat, owner, "knowledge__art-papers", csv_path,
            chunk_lookup=chunk_lookup,
        )
        assert docs == 1
        row = cat._db.execute(
            "SELECT source_uri FROM documents WHERE title = ?",
            ("Borderline Paper",),
        ).fetchone()
        assert row[0] == "x-devonthink-item://SUGGESTED-UUID"

    def test_rows_without_uuid_are_skipped(
        self, cat: Catalog, tmp_path: Path,
    ) -> None:
        owner = _owner(cat)
        chunk_lookup = {
            "Skip Me": [ob.ChunkRef(cid="c1", chash="h1")],
            "Include Me": [ob.ChunkRef(cid="c2", chash="h2")],
        }
        csv_path = tmp_path / "unmatched.csv"
        csv_path.write_text(
            "title,chunk_count,operator_dt_uuid\n"
            "Skip Me,1,\n"  # operator left UUID blank
            "Include Me,1,UUID-INCLUDED\n"
        )
        docs, _ = ob.apply_csv(
            cat, owner, "knowledge__art-papers", csv_path,
            chunk_lookup=chunk_lookup,
        )
        assert docs == 1
        rows = cat._db.execute(
            "SELECT title FROM documents "
            "WHERE physical_collection = ?",
            ("knowledge__art-papers",),
        ).fetchall()
        titles = [r[0] for r in rows]
        assert "Include Me" in titles
        assert "Skip Me" not in titles

    def test_unknown_title_logs_warning_but_does_not_crash(
        self, cat: Catalog, tmp_path: Path,
    ) -> None:
        owner = _owner(cat)
        chunk_lookup: dict[str, list[ob.ChunkRef]] = {}  # empty
        csv_path = tmp_path / "unmatched.csv"
        csv_path.write_text(
            "title,chunk_count,operator_dt_uuid\n"
            "Ghost Title,5,UUID-GHOST\n"
        )
        docs, links = ob.apply_csv(
            cat, owner, "knowledge__art-papers", csv_path,
            chunk_lookup=chunk_lookup,
        )
        # No chunks for this title -> skip without error.
        assert docs == 0
        assert links == 0
