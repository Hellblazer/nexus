# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for orphan-backfill: real catalog register +
write_manifest against the active catalog substrate.

Pure-logic tests live in test_orphan_backfill.py; this file covers the
end-to-end path where catalog Documents get created and the manifest
table gets populated.

nexus-i711w terminal deletion: ported off the local SQLite ``Catalog``
(tmp-path seeding + ``_db.execute`` reads) onto ``ActiveCatalog`` — the
subject (``nexus.catalog.orphan_backfill``) survives; only the harness
was local. Chashes are full 64-hex now (RDR-180: the engine stores
``bytea(32)``, so the old junk labels like ``"h1"`` are derived via
sha256).

Beads: nexus-h2pm, nexus-4fw8, nexus-oa9k.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from nexus.catalog import orphan_backfill as ob
from tests._catalog_fixture_ops import ActiveCatalog


def _hx(label: str) -> str:
    """Deterministic full-64-hex chash for a test label."""
    return hashlib.sha256(label.encode()).hexdigest()


@pytest.fixture
def cat() -> ActiveCatalog:
    """Active catalog + minimal owner needed by orphan-backfill."""
    c = ActiveCatalog()
    # Register a curator owner matching the DEFAULT_COLLECTION_OWNER
    # entry for ``knowledge__*``. Owner_type='curator' means no repo_root
    # so source_uri normalization stays out of the picture.
    c.register_owner("papers", "curator", repo_hash="")
    return c


def _owner(cat: ActiveCatalog):
    from nexus.catalog.tumbler import Tumbler

    rows = [o for o in cat.list_owners() if o.get("name") == "papers"]
    assert rows, "test fixture failed: papers curator not registered"
    return Tumbler.parse(rows[0]["tumbler_prefix"])


def _docs_in(cat: ActiveCatalog, collection: str) -> list[Any]:
    return sorted(cat.list_by_collection(collection), key=lambda e: e.title)


class TestRegisterDtLinked:
    def test_registers_one_doc_per_match_with_dt_uri(self, cat: ActiveCatalog) -> None:
        owner = _owner(cat)
        matches = [
            ob.DTMatch(
                title="Test Paper One",
                dt_uuid="UUID-AAAA-0001",
                dt_name="Test Paper One (DT name)",
                score=0.92,
                chunks=[
                    ob.ChunkRef(cid="c1", chash=_hx("abc111"), chunk_index=0),
                    ob.ChunkRef(cid="c2", chash=_hx("abc222"), chunk_index=1),
                ],
            ),
            ob.DTMatch(
                title="Test Paper Two",
                dt_uuid="UUID-BBBB-0002",
                dt_name="Test Paper Two",
                score=0.88,
                chunks=[ob.ChunkRef(cid="c3", chash=_hx("xyz333"), chunk_index=0)],
            ),
        ]
        docs, links = ob.register_dt_linked(
            cat, owner, "knowledge__art-papers", matches,
        )
        assert docs == 2
        assert links == 3

        # Verify Documents written with the DT URI scheme.
        rows = _docs_in(cat, "knowledge__art-papers")
        assert len(rows) == 2
        assert rows[0].source_uri.startswith("x-devonthink-item://UUID-AAAA")
        assert rows[1].source_uri.startswith("x-devonthink-item://UUID-BBBB")

    def test_writes_chunks_manifest_in_position_order(
        self, cat: ActiveCatalog,
    ) -> None:
        owner = _owner(cat)
        matches = [
            ob.DTMatch(
                title="Manifest Order Test",
                dt_uuid="UUID-ORD-001",
                dt_name="Manifest Order Test",
                score=1.0,
                chunks=[
                    ob.ChunkRef(cid=f"c{i}", chash=_hx(f"h{i:02d}"),
                                chunk_index=i)
                    for i in range(5)
                ],
            ),
        ]
        ob.register_dt_linked(
            cat, owner, "knowledge__art-papers", matches,
        )
        docs = [e for e in cat.list_by_collection("knowledge__art-papers")
                if e.title == "Manifest Order Test"]
        assert len(docs) == 1
        rows = cat.get_manifest(str(docs[0].tumbler))
        assert len(rows) == 5
        positions = [r.position for r in rows]
        chashes = [r.chash for r in rows]
        assert positions == [0, 1, 2, 3, 4]
        assert chashes == [_hx(f"h{i:02d}") for i in range(5)]

    def test_metadata_carries_backfill_provenance(self, cat: ActiveCatalog) -> None:
        owner = _owner(cat)
        matches = [
            ob.DTMatch(
                title="Provenance Test",
                dt_uuid="UUID-PROV-001",
                dt_name="Different DT Name",
                score=0.81,
                chunks=[ob.ChunkRef(cid="c1", chash=_hx("h1"), chunk_index=0)],
            ),
        ]
        ob.register_dt_linked(
            cat, owner, "knowledge__art-papers", matches,
        )
        docs = [e for e in cat.list_by_collection("knowledge__art-papers")
                if e.title == "Provenance Test"]
        assert len(docs) == 1
        meta = docs[0].meta
        assert meta.get("backfill_from") == "t3_orphan"
        assert meta.get("backfill_mode") == "dt_link"
        assert meta.get("dt_uuid") == "UUID-PROV-001"
        assert meta.get("dt_name") == "Different DT Name"
        assert meta.get("fuzzy_score") == 0.81


class TestRegisterSynthetic:
    def test_titled_groups_get_one_doc_each_with_synthetic_uri(
        self, cat: ActiveCatalog,
    ) -> None:
        owner = _owner(cat)
        groups = [
            ob.TitleGroup(
                title="Unmatched Paper Alpha",
                chunks=[ob.ChunkRef(cid="c1", chash=_hx("a1"), chunk_index=0),
                        ob.ChunkRef(cid="c2", chash=_hx("a2"), chunk_index=1)],
            ),
            ob.TitleGroup(
                title="Unmatched Paper Beta",
                chunks=[ob.ChunkRef(cid="c3", chash=_hx("b1"), chunk_index=0)],
            ),
        ]
        docs, links = ob.register_synthetic(
            cat, owner, "knowledge__art-papers", groups,
        )
        assert docs == 2
        assert links == 3

        rows = _docs_in(cat, "knowledge__art-papers")
        assert len(rows) == 2
        assert all(r.source_uri.startswith("nx-orphan-backfill://") for r in rows)
        # URI carries collection + title for operator legibility.
        assert "knowledge__art-papers/Unmatched Paper Alpha" in rows[0].source_uri

    def test_untitled_group_falls_back_to_per_chash_singletons(
        self, cat: ActiveCatalog,
    ) -> None:
        owner = _owner(cat)
        hashes = [_hx("hash-001"), _hx("hash-002"), _hx("hash-003")]
        groups = [
            ob.TitleGroup(
                title="",
                chunks=[
                    ob.ChunkRef(cid="c1", chash=hashes[0]),
                    ob.ChunkRef(cid="c2", chash=hashes[1]),
                    ob.ChunkRef(cid="c3", chash=hashes[2]),
                ],
            ),
        ]
        docs, links = ob.register_synthetic(
            cat, owner, "knowledge__art", groups,
        )
        # 3 chunks -> 3 singleton Documents (chash-based fallback).
        assert docs == 3
        assert links == 3
        uris = sorted(
            e.source_uri for e in cat.list_by_collection("knowledge__art")
        )
        assert uris == sorted(
            f"nx-orphan-backfill://knowledge__art/chash/{h}" for h in hashes
        )


class TestApplyCsv:
    def test_unmatched_csv_with_operator_uuid_creates_dt_linked_docs(
        self, cat: ActiveCatalog, tmp_path: Path,
    ) -> None:
        owner = _owner(cat)
        # Original gather would have produced these chunk lookups.
        chunk_lookup = {
            "Curated Title One": [
                ob.ChunkRef(cid="c1", chash=_hx("h1"), chunk_index=0),
                ob.ChunkRef(cid="c2", chash=_hx("h2"), chunk_index=1),
            ],
            "Curated Title Two": [
                ob.ChunkRef(cid="c3", chash=_hx("h3"), chunk_index=0),
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
        uris = {
            e.title: e.source_uri
            for e in cat.list_by_collection("knowledge__art-papers")
        }
        assert uris["Curated Title One"] == "x-devonthink-item://DT-UUID-AAA"
        assert uris["Curated Title Two"] == "x-devonthink-item://DT-UUID-BBB"

    def test_low_confidence_approve_picks_candidate_uuid(
        self, cat: ActiveCatalog, tmp_path: Path,
    ) -> None:
        owner = _owner(cat)
        chunk_lookup = {
            "Borderline Paper": [
                ob.ChunkRef(cid="c1", chash=_hx("h1"), chunk_index=0),
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
        rows = [e for e in cat.list_by_collection("knowledge__art-papers")
                if e.title == "Borderline Paper"]
        assert len(rows) == 1
        assert rows[0].source_uri == "x-devonthink-item://SUGGESTED-UUID"

    def test_rows_without_uuid_are_skipped(
        self, cat: ActiveCatalog, tmp_path: Path,
    ) -> None:
        owner = _owner(cat)
        chunk_lookup = {
            "Skip Me": [ob.ChunkRef(cid="c1", chash=_hx("h1"))],
            "Include Me": [ob.ChunkRef(cid="c2", chash=_hx("h2"))],
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
        titles = [e.title for e in cat.list_by_collection("knowledge__art-papers")]
        assert "Include Me" in titles
        assert "Skip Me" not in titles

    def test_unknown_title_logs_warning_but_does_not_crash(
        self, cat: ActiveCatalog, tmp_path: Path,
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
