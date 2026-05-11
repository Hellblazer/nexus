# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nexus.catalog.orphan_backfill``.

Pure-logic functions (best_match, classify_groups, dt_multi_search shape,
CSV I/O) get unit tests with injected fakes. Catalog integration tests
that hit Catalog.register + write_manifest live in a separate file
(test_orphan_backfill_integration.py) and use a tmp_path catalog.

Beads: nexus-h2pm, nexus-4fw8, nexus-oa9k.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from nexus.catalog import orphan_backfill as ob


# ── best_match ───────────────────────────────────────────────────────────────


class TestBestMatch:
    def test_returns_none_on_empty_candidates(self) -> None:
        assert ob.best_match("anything", []) is None

    def test_returns_top_candidate_when_above_threshold(self) -> None:
        cands = [
            ("uuid-a", "Carpenter Grossberg 1992 Fuzzy ARTMAP"),
            ("uuid-b", "Random Other Paper"),
        ]
        result = ob.best_match(
            "Carpenter Grossberg 1992 Fuzzy Artmap",
            cands, min_score=0.6,
        )
        assert result is not None
        score, u, n = result
        assert u == "uuid-a"
        assert score >= 0.85

    def test_returns_none_when_top_candidate_below_threshold(self) -> None:
        cands = [
            ("uuid-x", "Totally Different Paper Title"),
        ]
        # Title "Foo Bar Baz" vs "Totally Different..." scores low.
        assert ob.best_match("Foo Bar Baz", cands, min_score=0.75) is None

    def test_picks_highest_score_when_multiple_candidates(self) -> None:
        cands = [
            ("uuid-low", "Loose Match"),
            ("uuid-high", "Carpenter Grossberg 1992 Fuzzy Artmap"),
            ("uuid-mid", "Carpenter 1992"),
        ]
        result = ob.best_match(
            "Carpenter Grossberg 1992 Fuzzy Artmap",
            cands, min_score=0.5,
        )
        assert result is not None
        _, uuid_str, _ = result
        assert uuid_str == "uuid-high"


# ── classify_groups ──────────────────────────────────────────────────────────


def _fake_searcher(mapping: dict[str, list[tuple[str, str]]]):
    """Return a searcher that maps title -> candidates from ``mapping``."""

    def _search(title: str, top_k: int = 30) -> list[tuple[str, str]]:
        return mapping.get(title, [])

    return _search


class TestClassifyGroups:
    def test_high_score_groups_go_to_matched(self) -> None:
        groups = [
            ob.TitleGroup(
                title="Carpenter Grossberg 1992 Fuzzy Artmap",
                chunks=[ob.ChunkRef(cid="c1", chash="abc")],
            ),
        ]
        searcher = _fake_searcher({
            "Carpenter Grossberg 1992 Fuzzy Artmap": [
                ("uuid-1", "Carpenter Grossberg 1992 Fuzzy ARTMAP"),
            ],
        })
        matched, low, unmatched = ob.classify_groups(
            groups, min_score=0.75, low_conf_floor=0.55,
            searcher=searcher,
        )
        assert len(matched) == 1
        assert len(low) == 0
        assert len(unmatched) == 0
        assert matched[0].dt_uuid == "uuid-1"

    def test_borderline_score_goes_to_low_confidence(self) -> None:
        groups = [
            ob.TitleGroup(
                title="Foo Bar Baz Qux",
                chunks=[ob.ChunkRef(cid="c1", chash="abc")],
            ),
        ]
        # Mid-score candidate (~0.67 against Foo Bar Baz Qux).
        searcher = _fake_searcher({
            "Foo Bar Baz Qux": [("uuid-mid", "Foo Bar Baz Different")],
        })
        matched, low, unmatched = ob.classify_groups(
            groups, min_score=0.75, low_conf_floor=0.55,
            searcher=searcher,
        )
        assert len(matched) == 0
        assert len(low) == 1
        assert low[0].dt_uuid == "uuid-mid"
        assert 0.55 <= low[0].score < 0.75

    def test_no_candidates_goes_to_unmatched(self) -> None:
        groups = [
            ob.TitleGroup(
                title="No Match At All",
                chunks=[ob.ChunkRef(cid="c1", chash="abc")],
            ),
        ]
        searcher = _fake_searcher({})
        matched, low, unmatched = ob.classify_groups(
            groups, searcher=searcher,
        )
        assert matched == []
        assert low == []
        assert len(unmatched) == 1
        assert unmatched[0].title == "No Match At All"

    def test_empty_title_group_routes_to_unmatched_without_searching(
        self,
    ) -> None:
        # Synthetic mode handles untitled chunks; classify_groups must
        # not call the searcher with an empty string.
        sentinel = []

        def tracking_searcher(title, top_k=30):
            sentinel.append(title)
            return []

        groups = [
            ob.TitleGroup(
                title="",
                chunks=[ob.ChunkRef(cid="c1", chash="abc")],
            ),
        ]
        _, _, unmatched = ob.classify_groups(
            groups, searcher=tracking_searcher,
        )
        assert len(unmatched) == 1
        assert sentinel == [], "searcher must not be called for empty title"


# ── CSV I/O ──────────────────────────────────────────────────────────────────


class TestDumpCsvs:
    def test_writes_three_files_under_collection_dir(
        self, tmp_path: Path,
    ) -> None:
        matched = [ob.DTMatch(
            title="High Conf",
            dt_uuid="u1", dt_name="High Conf Doc", score=0.91,
            chunks=[ob.ChunkRef(cid="c1", chash="a"),
                    ob.ChunkRef(cid="c2", chash="b")],
        )]
        low = [ob.DTMatch(
            title="Borderline",
            dt_uuid="u2", dt_name="Borderline Doc", score=0.62,
            chunks=[ob.ChunkRef(cid="c3", chash="c")],
        )]
        unmatched = [ob.TitleGroup(
            title="Lost",
            chunks=[ob.ChunkRef(cid="c4", chash="d")],
        )]
        m_path, l_path, u_path = ob.dump_csvs(
            tmp_path, "knowledge__art-papers",
            matched, low, unmatched,
        )
        assert m_path.exists()
        assert l_path.exists()
        assert u_path.exists()
        # Verify header + one data row in each.
        with m_path.open() as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["dt_uuid"] == "u1"
        assert rows[0]["chunk_count"] == "2"
        with l_path.open() as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["operator_decision"] == ""
        with u_path.open() as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["operator_dt_uuid"] == ""

    def test_collection_subdir_is_created(self, tmp_path: Path) -> None:
        out = tmp_path / "queue"
        m, _, _ = ob.dump_csvs(out, "docs__default", [], [], [])
        assert m.parent == out / "docs__default"
        assert m.parent.is_dir()


# ── dt_multi_search query construction ───────────────────────────────────────


class TestQueryConstruction:
    """Indirect tests: dt_multi_search calls dt_search multiple times
    with different query slices. We assert on the queries it issues
    by intercepting dt_search via monkeypatch.
    """

    def test_emits_year_stripped_first_six_words(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        recorded: list[str] = []

        def fake_search(query: str, top_k: int = 30, timeout: int = 20):
            recorded.append(query)
            return []

        monkeypatch.setattr(ob, "dt_search", fake_search)
        ob.dt_multi_search(
            "Carpenter Grossberg 1991 Artmap Supervised Learning Real Time",
        )
        assert any(
            "Carpenter Grossberg" in q and "1991" not in q
            for q in recorded
        ), f"recorded={recorded}"

    def test_returns_empty_when_title_has_no_alpha_words(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        called = []

        def fake_search(query: str, top_k: int = 30, timeout: int = 20):
            called.append(query)
            return []

        monkeypatch.setattr(ob, "dt_search", fake_search)
        result = ob.dt_multi_search("!!! 2024 ???")
        assert result == []
        # year-strip + punct-strip leaves nothing; searcher not called.
        assert called == []

    def test_dedupes_candidates_by_uuid_across_query_variants(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Multiple variants return overlapping candidates; merged list
        # must contain each UUID once, in first-seen order.
        responses = iter([
            [("u1", "First"), ("u2", "Second")],
            [("u2", "Second again"), ("u3", "Third")],
            [("u1", "First again")],
            [],
            [],
        ])

        def fake_search(query: str, top_k: int = 30, timeout: int = 20):
            return next(responses, [])

        monkeypatch.setattr(ob, "dt_search", fake_search)
        merged = ob.dt_multi_search("Some Long Enough Title For Variants")
        uuids = [u for u, _ in merged]
        assert uuids == ["u1", "u2", "u3"]


# ── gather_titled_chunks ─────────────────────────────────────────────────────


class _FakeChromaCollection:
    def __init__(self, chunks: list[dict]) -> None:
        self._chunks = chunks

    def count(self) -> int:
        return len(self._chunks)

    def get(self, *, limit: int, offset: int, include) -> dict:
        page = self._chunks[offset:offset + limit]
        return {
            "ids": [c["id"] for c in page],
            "metadatas": [c["meta"] for c in page],
        }


class _FakeT3:
    def __init__(self, by_collection: dict[str, list[dict]]) -> None:
        self._by_collection = by_collection
        self._client = self  # _client.get_collection in the function call

    def get_collection(self, *, name: str):
        return _FakeChromaCollection(self._by_collection.get(name, []))


class TestGatherTitledChunks:
    def test_groups_by_title_stripping_page_suffix(self) -> None:
        t3 = _FakeT3({
            "knowledge__art-papers": [
                {"id": "c1", "meta": {
                    "title": "Carpenter Grossberg:page-1",
                    "chunk_text_hash": "aaaa", "chunk_index": 0,
                }},
                {"id": "c2", "meta": {
                    "title": "Carpenter Grossberg:page-2",
                    "chunk_text_hash": "bbbb", "chunk_index": 1,
                }},
                {"id": "c3", "meta": {
                    "title": "Other Paper",
                    "chunk_text_hash": "cccc", "chunk_index": 0,
                }},
            ],
        })
        groups = ob.gather_titled_chunks(t3, "knowledge__art-papers")
        titles = {g.title: len(g.chunks) for g in groups}
        assert titles == {
            "Carpenter Grossberg": 2,
            "Other Paper": 1,
        }

    def test_chunks_without_title_collapse_to_empty_key(self) -> None:
        t3 = _FakeT3({
            "knowledge__art": [
                {"id": "c1", "meta": {
                    "chunk_text_hash": "aaaa", "chunk_index": 0,
                }},
                {"id": "c2", "meta": {
                    "title": "", "chunk_text_hash": "bbbb",
                }},
            ],
        })
        groups = ob.gather_titled_chunks(t3, "knowledge__art")
        assert len(groups) == 1
        assert groups[0].title == ""
        assert len(groups[0].chunks) == 2

    def test_chash_falls_back_to_cid_prefix_when_metadata_missing(
        self,
    ) -> None:
        t3 = _FakeT3({
            "x": [{"id": "abcdefghij" * 4, "meta": {"title": "T"}}],
        })
        groups = ob.gather_titled_chunks(t3, "x")
        assert groups[0].chunks[0].chash == ("abcdefghij" * 4)[:32]
