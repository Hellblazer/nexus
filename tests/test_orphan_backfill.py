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
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from nexus.catalog import orphan_backfill as ob
from nexus.cli import main


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

    def get(self, *, limit: int, offset: int, include, include_non_live: bool = False) -> dict:
        # The walk must read stored rows: an orphan has no owner, so a live
        # read (RDR-192 Step 5) would never return it.
        assert include_non_live is True, "orphan backfill must read stored rows"
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

    def test_chash_falls_back_to_full_cid_when_metadata_missing(
        self,
    ) -> None:
        # RDR-180: the cid IS the full chash — no truncation on fallback.
        t3 = _FakeT3({
            "x": [{"id": "abcdefghij" * 4, "meta": {"title": "T"}}],
        })
        groups = ob.gather_titled_chunks(t3, "x")
        assert groups[0].chunks[0].chash == "abcdefghij" * 4


def test_content_type_inferred_from_collection_prefix() -> None:
    """nexus-s5le: synthesized orphan-backfill rows take their content_type
    from the collection's RDR-103 leading segment, not a hardcoded 'pdf'
    (which never matched a content_type= catalog filter for the real corpus)."""
    from nexus.catalog.orphan_backfill import _content_type_for_collection

    # The embedder segment is a neutral placeholder ("embed3"), not the real
    # voyage-*-3 model name: the function only inspects the leading "__"
    # segment, so the embedder is incidental here, and a literal voyage token
    # would trip the RDR-109 cloud-mode lint (test_mode_declarations_are_explicit)
    # for a test that never touches cloud-mode state.
    assert _content_type_for_collection("code__nexus__embed3__v1") == "code"
    assert _content_type_for_collection(
        "knowledge__delos__embed3__v1"
    ) == "knowledge"
    assert _content_type_for_collection("rdr__nexus__embed3__v1") == "rdr"
    assert _content_type_for_collection("docs__corpus__embed3__v1") == "docs"
    # Non-conformant name with an empty prefix falls back; never the old 'pdf'.
    assert _content_type_for_collection("__weird") == "knowledge"
    assert _content_type_for_collection("code__x") != "pdf"


# ── link_by_content_hash ─────────────────────────────────────────────────────


class _FakeManifestCatalog:
    """Records manifest appends and registrations; ``entries`` are what list_by_collection returns.

    ``manifests`` maps tumbler -> what its manifest already holds, each item a chash, a
    ``(chash, position)`` or a ``(chash, position, collection)`` (a bare chash takes its list
    index and ``docs__x``). The reverse lookups answer from it as the engine does, from LIVE
    manifests only: ``chash_positions`` carries each row's collection; ``docs_for_chashes`` is
    tenant-wide (no collection). ``route=False`` makes ``chash_positions`` answer ``None`` (an
    engine without the route); ``fail`` makes a lookup raise: ``"positions"`` the route,
    ``"manifests"`` the fallback (with ``route=False``); either also fails ``docs_for_chashes``.
    ``keyless=True`` is engine-service-v0.1.164: the route answers, but its rows carry no
    ``collection`` key, so ownership must come from the manifests.
    """

    def __init__(
        self, entries: list, manifests: dict[str, list] | None = None, *,
        route: bool = True, fail: str = "", keyless: bool = False,
    ) -> None:
        self._entries = entries
        self._manifests = manifests or {}
        self._route = route
        self._fail = fail
        self._keyless = keyless
        self.positions_calls = 0
        self.manifest_calls = 0
        self.appended: dict[str, list[dict]] = {}
        self.registered: list[dict] = []
        self.written: dict[str, list[dict]] = {}

    def _held(self, tumbler: str) -> list[tuple[str, int, str]]:
        out = []
        for i, item in enumerate(self._manifests.get(tumbler, [])):
            if isinstance(item, str):
                out.append((item, i, "docs__x"))
            else:
                chash, position, *rest = item
                out.append((chash, position, rest[0] if rest else "docs__x"))
        return out

    def list_by_collection(self, collection: str, *, limit: int):
        return self._entries

    def get_manifest(self, tumbler: str) -> list:
        return [
            SimpleNamespace(chash=c, position=pos, collection=col)
            for c, pos, col in self._held(tumbler)
        ]

    def docs_for_chashes(self, chashes: list[str]) -> dict[str, list[str]]:
        if self._fail:
            raise RuntimeError("docs_for_chashes failed")
        wanted = set(chashes)
        out: dict[str, list[str]] = {}
        for tumbler in self._manifests:
            for c, _pos, _col in self._held(tumbler):
                if c in wanted:
                    out.setdefault(c, []).append(tumbler)
        return out

    def chash_positions(self, chashes: list[str]) -> list[dict] | None:
        if self._fail == "positions":
            raise RuntimeError("chash_positions failed")
        if not self._route:
            return None
        self.positions_calls += 1
        wanted = set(chashes)
        rows = [
            {"chash": c, "doc_id": t, "position": pos, "chunk_count": len(self._held(t)),
             "collection": col}
            for t in self._manifests for c, pos, col in self._held(t) if c in wanted
        ]
        if self._keyless:
            for r in rows:
                del r["collection"]
        return rows

    def docs_and_manifests_for_chashes(self, chashes: list[str]):
        if self._fail == "manifests":
            raise RuntimeError("manifest lookup failed")
        self.manifest_calls += 1
        wanted = set(chashes)
        owners: dict[str, list[str]] = {}
        for t in self._manifests:
            for c, _pos, _col in self._held(t):
                if c in wanted:
                    owners.setdefault(c, []).append(t)
        docs = {t for ts in owners.values() for t in ts}
        return owners, {t: self.get_manifest(t) for t in docs}

    def append_manifest_chunks(self, tumbler: str, chunks: list[dict], *, collection: str) -> None:
        self.appended.setdefault(tumbler, []).extend(chunks)

    def register(self, owner, **kw) -> str:
        self.registered.append(kw)
        return f"9.9.{len(self.registered)}"

    def write_manifest(self, tumbler: str, chunks: list[dict], *, collection: str) -> None:
        self.written[tumbler] = chunks


def _entry(tumbler: str, *, head_hash: str = "", index_content_hash: str = "", meta: dict | None = None):
    return SimpleNamespace(
        tumbler=tumbler, head_hash=head_hash,
        index_content_hash=index_content_hash, meta=meta or {},
    )


def _pdf_chunk(cid: str, content_hash: str, chunk_index: int) -> dict:
    return {"id": cid, "meta": {"content_hash": content_hash, "chunk_index": chunk_index}}


class TestLinkByContentHash:
    """nexus-lhmu1: the join key is the file-content hash, never head_hash
    (the indexing commit for repo rows), as link_generator._pdf_content_hash."""

    def test_joins_on_index_content_hash_not_head_hash(self) -> None:
        commit = "c" * 40
        cat = _FakeManifestCatalog([
            _entry("1.1.1", head_hash=commit, index_content_hash="a" * 64),
            _entry("1.1.2", head_hash=commit, meta={"content_hash": "b" * 64}),
        ])
        t3 = _FakeT3({"docs__x": [
            _pdf_chunk("k2", "a" * 64, 1),
            _pdf_chunk("k1", "a" * 64, 0),
            _pdf_chunk("k3", "b" * 64, 0),
            _pdf_chunk("k4", commit, 0),  # a chunk whose content_hash is the commit joins nothing
        ]})
        linked_chunks, linked_docs, unmatched = ob.link_by_content_hash(cat, t3, "docs__x")
        assert (linked_chunks, linked_docs, unmatched) == (3, 2, 1)
        assert [c["chash"] for c in cat.appended["1.1.1"]] == ["k1", "k2"]
        assert [c["chash"] for c in cat.appended["1.1.2"]] == ["k3"]

    def test_hash_shared_by_two_documents_links_neither(self) -> None:
        cat = _FakeManifestCatalog([
            _entry("1.1.1", index_content_hash="a" * 64),
            _entry("1.1.2", index_content_hash="a" * 64),
        ])
        t3 = _FakeT3({"docs__x": [_pdf_chunk("k1", "a" * 64, 0)]})
        assert ob.link_by_content_hash(cat, t3, "docs__x") == (0, 0, 1)
        assert cat.appended == {}

    def test_head_hash_only_rows_are_not_joinable(self) -> None:
        cat = _FakeManifestCatalog([_entry("1.1.1", head_hash="a" * 64)])
        t3 = _FakeT3({"docs__x": [_pdf_chunk("k1", "a" * 64, 0)]})
        assert ob.link_by_content_hash(cat, t3, "docs__x") == (0, 0, 0)
        assert cat.appended == {}


# ── nexus-6pfgb.22: link-existing links only manifest-less rows ──────────────


def _titled(title: str, chunks: list[tuple[str, int]]) -> "ob.TitleGroup":
    return ob.TitleGroup(
        title=title, chunks=[ob.ChunkRef(cid=c, chash=c, chunk_index=i) for c, i in chunks],
    )


def _titled_entry(tumbler: str, title: str):
    return SimpleNamespace(tumbler=tumbler, title=title)


class TestLinkByTitleManifestLessOnly:
    """The group's chunks carry the title the LAST writer stamped. Only a chunk no live
    manifest names is the target's to take; one the target holds is skipped, one another
    document owns is skipped, and a matched group never falls through to synthetic."""

    def test_appends_only_the_manifest_less_chunk_at_one_new_position(self) -> None:
        cat = _FakeManifestCatalog(
            [_titled_entry("1.1.1", "T")],
            manifests={"1.1.1": ["c1"], "1.1.2": ["c3"]},
        )
        skips = ob.LinkSkips()
        linked, docs, unlinked = ob.link_by_title(
            cat, "docs__x", [_titled("T", [("c1", 0), ("c2", 1), ("c3", 2)])], skips=skips,
        )
        assert (linked, docs, unlinked) == (1, 1, [])
        assert [(c["chash"], c["position"]) for c in cat.appended["1.1.1"]] == [("c2", 1)]
        assert (skips.held_by_target, skips.owned_elsewhere) == (1, 1)

    def test_a_group_with_nothing_manifest_less_is_matched_not_unlinked(self) -> None:
        cat = _FakeManifestCatalog([_titled_entry("1.1.1", "T")], manifests={"1.1.1": ["c1"]})
        linked, docs, unlinked = ob.link_by_title(
            cat, "docs__x", [_titled("T", [("c1", 0)])],
        )
        assert (linked, docs, unlinked) == (0, 0, [])
        assert cat.appended == {}

    def test_dry_run_counts_and_writes_nothing(self) -> None:
        cat = _FakeManifestCatalog([_titled_entry("1.1.1", "T")], manifests={"1.1.2": ["c3"]})
        skips = ob.LinkSkips()
        linked, docs, _ = ob.link_by_title(
            cat, "docs__x", [_titled("T", [("c2", 0), ("c3", 1)])], dry_run=True, skips=skips,
        )
        assert (linked, docs) == (1, 1)
        assert cat.appended == {}
        assert (skips.held_by_target, skips.owned_elsewhere) == (0, 1)

    def test_untitled_and_unmatched_groups_still_pass_through(self) -> None:
        cat = _FakeManifestCatalog([_titled_entry("1.1.1", "T")])
        groups = [_titled("", [("c1", 0)]), _titled("Other", [("c2", 0)])]
        _, _, unlinked = ob.link_by_title(cat, "docs__x", groups)
        assert unlinked == groups


class TestLinkByContentHashManifestLessOnly:
    def test_skips_chunks_a_live_manifest_names(self) -> None:
        cat = _FakeManifestCatalog(
            [_entry("1.1.1", index_content_hash="a" * 64)],
            manifests={"1.1.1": ["k1"], "1.1.2": ["k3"]},
        )
        t3 = _FakeT3({"docs__x": [
            _pdf_chunk("k1", "a" * 64, 0),
            _pdf_chunk("k2", "a" * 64, 1),
            _pdf_chunk("k3", "a" * 64, 2),
        ]})
        skips = ob.LinkSkips()
        linked_chunks, linked_docs, unmatched = ob.link_by_content_hash(
            cat, t3, "docs__x", skips=skips,
        )
        assert (linked_chunks, linked_docs, unmatched) == (1, 1, 0)
        assert [(c["chash"], c["position"]) for c in cat.appended["1.1.1"]] == [("k2", 1)]
        assert (skips.held_by_target, skips.owned_elsewhere) == (1, 1)

    def test_dry_run_writes_nothing(self) -> None:
        cat = _FakeManifestCatalog([_entry("1.1.1", index_content_hash="a" * 64)])
        t3 = _FakeT3({"docs__x": [_pdf_chunk("k1", "a" * 64, 0)]})
        assert ob.link_by_content_hash(cat, t3, "docs__x", dry_run=True) == (1, 1, 0)
        assert cat.appended == {}


# ── nexus-6pfgb.22 fix round: positions, lookup failure, ambiguity, collection, synthetic ──


def _group_for(by: str, chunks: list[tuple[str, int]]):
    """The (callable, label) pair that runs link-existing's selection for *by* on chashes *chunks*."""
    if by == "title":
        return lambda cat, **kw: ob.link_by_title(cat, "docs__x", [_titled("T", chunks)], **kw)
    t3 = _FakeT3({"docs__x": [_pdf_chunk(c, "a" * 64, i) for c, i in chunks]})
    return lambda cat, **kw: ob.link_by_content_hash(cat, t3, "docs__x", **kw)


def _target_entry(by: str):
    return _titled_entry("1.1.1", "T") if by == "title" else _entry("1.1.1", index_content_hash="a" * 64)


class TestAppendPositionsAreSparse:
    """Manifest positions are not dense by design and an append upserts BY position, so new rows
    start past the highest position held, never at the row count."""

    @pytest.mark.parametrize("by", ["title", "content_hash"])
    def test_appends_after_the_highest_position_not_the_row_count(self, by: str) -> None:
        # positions 0, 1, 3: three rows, so a count-based start would overwrite position 3.
        cat = _FakeManifestCatalog(
            [_target_entry(by)],
            manifests={"1.1.1": [("h0", 0), ("h1", 1), ("h3", 3)]},
        )
        run = _group_for(by, [("n1", 0), ("n2", 1)])
        run(cat)
        assert [(c["chash"], c["position"]) for c in cat.appended["1.1.1"]] == [("n1", 4), ("n2", 5)]

    def test_an_empty_manifest_starts_at_zero(self) -> None:
        cat = _FakeManifestCatalog([_titled_entry("1.1.1", "T")])
        ob.link_by_title(cat, "docs__x", [_titled("T", [("n1", 0)])])
        assert [(c["chash"], c["position"]) for c in cat.appended["1.1.1"]] == [("n1", 0)]


class TestALookupFailureLinksNothing:
    """The ownership lookup runs once, before the first write, and raises rather than answer
    partially. Moving it after an append, or swallowing its error and going on, must turn these red."""

    @pytest.mark.parametrize("dry_run", [False, True])
    @pytest.mark.parametrize("by", ["title", "content_hash"])
    @pytest.mark.parametrize("mode", ["positions", "manifests"])
    def test_a_failed_lookup_propagates_and_appends_nothing(
        self, by: str, dry_run: bool, mode: str,
    ) -> None:
        cat = _FakeManifestCatalog(
            [_target_entry(by)], route=(mode == "positions"), fail=mode,
        )
        run = _group_for(by, [("n1", 0), ("n2", 1)])
        with pytest.raises(RuntimeError):
            run(cat, dry_run=dry_run)
        assert cat.appended == {}

    def test_synthetic_registers_nothing_when_the_lookup_fails(self) -> None:
        cat = _FakeManifestCatalog([], fail="positions")
        with pytest.raises(RuntimeError):
            ob.register_synthetic(
                cat, "1.1", "docs__x", [_titled("A", [("c1", 0)]), _titled("", [("c2", 0)])],
                skip_owned=True,
            )
        assert (cat.registered, cat.written) == ([], {})


class TestOwnershipIsPerCollection:
    @pytest.mark.parametrize("route", [True, False])
    def test_a_chunk_a_document_of_another_collection_holds_is_linkable_here(self, route: bool) -> None:
        # Identical text is one chunk row per collection. Doc 2.1.1 of collection docs__y holds
        # "c1"; the orphan "c1" row in docs__x is nobody's, and "c2" is held by a docs__x document.
        cat = _FakeManifestCatalog(
            [_titled_entry("1.1.1", "T")],
            manifests={"2.1.1": [("c1", 0, "docs__y")], "1.1.2": [("c2", 0, "docs__x")]},
            route=route,
        )
        skips = ob.LinkSkips()
        linked, docs, _ = ob.link_by_title(
            cat, "docs__x", [_titled("T", [("c1", 0), ("c2", 1)])], skips=skips,
        )
        assert (linked, docs) == (1, 1)
        assert [c["chash"] for c in cat.appended["1.1.1"]] == ["c1"]
        assert (skips.held_by_target, skips.owned_elsewhere) == (0, 1)

    def test_a_row_with_no_collection_counts_as_owned(self) -> None:
        # A pre-field engine's manifest rows carry no collection: they cannot be shown to belong
        # elsewhere, so the chunk is not taken.
        cat = _FakeManifestCatalog(
            [_titled_entry("1.1.1", "T")], manifests={"1.1.2": [("c1", 0, None)]}, route=False,
        )
        assert ob.link_by_title(cat, "docs__x", [_titled("T", [("c1", 0)])])[:2] == (0, 0)
        assert cat.appended == {}


class TestAmbiguousTitle:
    def test_two_live_documents_with_one_title_link_to_neither_and_are_counted(self) -> None:
        cat = _FakeManifestCatalog([_titled_entry("1.1.1", "T"), _titled_entry("1.1.2", "T")])
        skips = ob.LinkSkips()
        groups = [_titled("T", [("c1", 0), ("c2", 1)]), _titled("U", [("c3", 0)])]
        linked, docs, unlinked = ob.link_by_title(cat, "docs__x", groups, skips=skips)
        assert (linked, docs) == (0, 0)
        assert cat.appended == {}
        assert [g.title for g in unlinked] == ["U"], "the ambiguous group is not handed to the synthetic fallback"
        assert (skips.ambiguous_titles, skips.ambiguous_title_chunks) == (1, 2)

    def test_a_unique_title_still_links_beside_an_ambiguous_one(self) -> None:
        cat = _FakeManifestCatalog([
            _titled_entry("1.1.1", "T"), _titled_entry("1.1.2", "T"), _titled_entry("1.1.3", "V"),
        ])
        linked, docs, _ = ob.link_by_title(
            cat, "docs__x", [_titled("T", [("c1", 0)]), _titled("V", [("c2", 0)])],
        )
        assert (linked, docs) == (1, 1)
        assert list(cat.appended) == ["1.1.3"]


class TestRegisterSyntheticSkipsOwnedChunks:
    def test_owned_chunks_are_not_registered_and_are_counted(self) -> None:
        cat = _FakeManifestCatalog(
            [], manifests={"1.1.9": [("o1", 0), ("o2", 1), ("o3", 2)]},
        )
        skips = ob.LinkSkips()
        groups = [
            _titled("A", [("o1", 0), ("a2", 1)]),   # titled, partly owned
            _titled("B", [("o2", 0)]),              # titled, wholly owned: no Document
            _titled("", [("o3", 0), ("u4", 1)]),    # untitled: owned one gets no singleton
        ]
        docs, links = ob.register_synthetic(
            cat, "1.1", "docs__x", groups, skips=skips, skip_owned=True,
        )
        assert (docs, links) == (2, 2)
        titles = [r["title"] for r in cat.registered]
        assert titles == ["A", "orphan-chunk-u4"]
        assert sorted(c["chash"] for rows in cat.written.values() for c in rows) == ["a2", "u4"]
        assert cat.registered[0]["chunk_count"] == 1
        assert skips.synthetic_owned == 3

    def test_a_chunk_owned_in_another_collection_is_still_registered(self) -> None:
        cat = _FakeManifestCatalog([], manifests={"2.1.1": [("c1", 0, "docs__y")]})
        docs, links = ob.register_synthetic(
            cat, "1.1", "docs__x", [_titled("A", [("c1", 0)])], skip_owned=True,
        )
        assert (docs, links) == (1, 1)

    def test_nothing_to_register_asks_nothing(self) -> None:
        cat = _FakeManifestCatalog([], fail="positions")
        assert ob.register_synthetic(cat, "1.1", "docs__x", [], skip_owned=True) == (0, 0)


class _IdempotentRegisterCatalog(_FakeManifestCatalog):
    """As the engine: ``register`` is idempotent on ``source_uri`` (the second call returns the
    same Document) and ``write_manifest`` REPLACES the Document's rows, which is what the
    reverse lookups then read."""

    def __init__(self) -> None:
        super().__init__([])
        self._by_uri: dict[str, str] = {}

    def register(self, owner, **kw) -> str:
        self.registered.append(kw)
        return self._by_uri.setdefault(kw["source_uri"], f"9.9.{len(self._by_uri) + 1}")

    def write_manifest(self, tumbler: str, chunks: list[dict], *, collection: str) -> None:
        super().write_manifest(tumbler, chunks, collection=collection)
        self._manifests[tumbler] = [(c["chash"], c["position"], collection) for c in chunks]


class TestDirectSyntheticVerbIsRerunnable:
    """The direct ``synthetic`` verb behaves as it did before nexus-6pfgb.22: it registers every
    chunk of a title group, so a rerun that finds one more chunk of the title re-writes the
    synthetic Document's whole manifest instead of replacing it with the new chunk alone. Only
    link-existing's ``--also-synthetic`` (``skip_owned=True``) leaves owned chunks out."""

    def test_a_rerun_with_a_new_chunk_keeps_the_first_runs_rows(self) -> None:
        cat = _IdempotentRegisterCatalog()
        ob.register_synthetic(cat, "1.1", "docs__x", [_titled("T", [("A", 0), ("B", 1), ("C", 2)])])
        ob.register_synthetic(
            cat, "1.1", "docs__x", [_titled("T", [("A", 0), ("B", 1), ("C", 2), ("D", 3)])],
        )
        assert len(cat._by_uri) == 1, "one synthetic Document for the title"
        (rows,) = cat._manifests.values()
        assert sorted(c for c, _p, _col in rows) == ["A", "B", "C", "D"]

    def test_a_rerun_of_singletons_is_idempotent(self) -> None:
        cat = _IdempotentRegisterCatalog()
        for _ in range(2):
            ob.register_synthetic(cat, "1.1", "docs__x", [_titled("", [("u1", 0), ("u2", 1)])])
        assert sorted(rows[0][0] for rows in cat._manifests.values()) == ["u1", "u2"]

    def test_the_direct_verb_asks_no_ownership_question(self) -> None:
        # A lookup that would raise is never made: nothing changes for the direct verb.
        cat = _FakeManifestCatalog([], fail="positions", manifests={"1.1.9": ["o1"]})
        docs, links = ob.register_synthetic(cat, "1.1", "docs__x", [_titled("A", [("o1", 0), ("a2", 1)])])
        assert (docs, links) == (1, 2)
        assert cat.positions_calls == 0 and cat.manifest_calls == 0


class TestKeylessChashPositionsFallsBackToManifests:
    """engine-service-v0.1.164: ``chash_positions`` answers, but its rows carry no ``collection``,
    so on every released engine ownership comes from the manifest fallback. Replacing the
    ``"collection" in r`` test with True makes this a KeyError."""

    @pytest.mark.parametrize("keyless", [False, True])
    def test_the_same_chunks_link_and_the_same_are_skipped(self, keyless: bool) -> None:
        cat = _FakeManifestCatalog(
            [_titled_entry("1.1.1", "T")],
            manifests={
                "2.1.1": [("c1", 0, "docs__y")],   # another collection: c1 is linkable here
                "1.1.2": [("c2", 0, "docs__x")],   # a live Document of this collection owns c2
                "1.1.1": [("c3", 0, "docs__x")],   # the target holds c3
            },
            keyless=keyless,
        )
        skips = ob.LinkSkips()
        linked, docs, _ = ob.link_by_title(
            cat, "docs__x", [_titled("T", [("c1", 0), ("c2", 1), ("c3", 2)])], skips=skips,
        )
        assert (linked, docs) == (1, 1)
        assert [c["chash"] for c in cat.appended["1.1.1"]] == ["c1"]
        assert (skips.held_by_target, skips.owned_elsewhere) == (1, 1)
        assert cat.positions_calls == 1, "the route was asked"
        assert cat.manifest_calls == (1 if keyless else 0), "keyless rows send it to the manifests"


class TestTargetHoldsTheChashUnderAnotherCollection:
    """The target's own manifest names the chash, but under a different collection value (a
    supersede cutover leaves such rows): appending would give it two rows for one chunk."""

    @pytest.mark.parametrize("route", [True, False])
    def test_it_counts_as_held_by_target_and_is_not_appended(self, route: bool) -> None:
        cat = _FakeManifestCatalog(
            [_titled_entry("1.1.1", "T")],
            manifests={"1.1.1": [("c1", 0, "docs__old")]},
            route=route,
        )
        skips = ob.LinkSkips()
        linked, docs, _ = ob.link_by_title(
            cat, "docs__x", [_titled("T", [("c1", 0), ("c2", 1)])], skips=skips,
        )
        assert (linked, docs) == (1, 1)
        assert [c["chash"] for c in cat.appended["1.1.1"]] == ["c2"]
        assert (skips.held_by_target, skips.owned_elsewhere) == (1, 0)

    def test_the_content_hash_path_agrees(self) -> None:
        cat = _FakeManifestCatalog(
            [_entry("1.1.1", index_content_hash="a" * 64)],
            manifests={"1.1.1": [("k1", 0, "docs__old")]},
        )
        t3 = _FakeT3({"docs__x": [_pdf_chunk("k1", "a" * 64, 0), _pdf_chunk("k2", "a" * 64, 1)]})
        skips = ob.LinkSkips()
        assert ob.link_by_content_hash(cat, t3, "docs__x", skips=skips)[:2] == (1, 1)
        assert [c["chash"] for c in cat.appended["1.1.1"]] == ["k2"]
        assert (skips.held_by_target, skips.owned_elsewhere) == (1, 0)

    def test_another_document_of_the_other_collection_does_not_count_as_the_target(self) -> None:
        cat = _FakeManifestCatalog(
            [_titled_entry("1.1.1", "T")], manifests={"2.1.1": [("c1", 0, "docs__old")]},
        )
        skips = ob.LinkSkips()
        assert ob.link_by_title(cat, "docs__x", [_titled("T", [("c1", 0)])], skips=skips)[:2] == (1, 1)
        assert (skips.held_by_target, skips.owned_elsewhere) == (0, 0)


class TestLinkExistingOutput:
    """The verb's own lines: the skip counts, the ambiguous-title line, and the --also-synthetic
    fallback line with its owned-chunk count."""

    @staticmethod
    def _run(monkeypatch: pytest.MonkeyPatch, *args: str):
        cat = _FakeManifestCatalog(
            [_titled_entry("1.1.1", "T"), _titled_entry("1.1.2", "Amb"), _titled_entry("1.1.3", "Amb")],
            manifests={"1.1.1": ["h1"], "1.1.9": ["t3", "u1"]},
        )
        meta = lambda title: {"title": title}  # noqa: E731
        t3 = _FakeT3({"docs__x": [
            {"id": "h1", "meta": meta("T")},      # the target holds it
            {"id": "t2", "meta": meta("T")},      # manifest-less: linked
            {"id": "t3", "meta": meta("T")},      # another live Document owns it
            {"id": "a1", "meta": meta("Amb")},    # ambiguous title group, 2 chunks
            {"id": "a2", "meta": meta("Amb")},
            {"id": "u1", "meta": meta("U")},      # unmatched, owned: left alone
            {"id": "u2", "meta": meta("U")},      # unmatched, ownerless: synthetic
        ]})
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        monkeypatch.setattr("nexus.db.make_t3", lambda: t3)
        monkeypatch.setattr(
            "nexus.commands.catalog_cmds.orphan_backfill._without_deleted", lambda c, coll, g: g,
        )
        result = CliRunner().invoke(
            main, ["catalog", "orphan-backfill", "link-existing", "docs__x", "--by", "title", *args],
        )
        assert result.exit_code == 0, result.output
        return result.output, cat

    def test_real_run_prints_every_skip_line_and_the_synthetic_count(self, monkeypatch) -> None:
        out, cat = self._run(monkeypatch, "--also-synthetic", "--owner", "1.1", "--no-dry-run")
        assert "Linked 1 chunks across 1 existing Documents." in out
        assert "Skipped 1 chunks the target Document already holds." in out
        assert "Skipped 1 chunks another live Document of this collection owns." in out
        assert "Skipped 1 title groups (2 chunks) whose title names more than one live Document." in out
        assert (
            "Synthetic fallback: registered 1 Documents, linked 1 chunks, skipped 1 chunks "
            "a live Document already owns." in out
        )
        assert [c["chash"] for c in cat.appended["1.1.1"]] == ["t2"]
        assert [c["chash"] for rows in cat.written.values() for c in rows] == ["u2"]

    def test_dry_run_says_would_link_and_stops_before_the_synthetic_step(self, monkeypatch) -> None:
        out, cat = self._run(monkeypatch, "--also-synthetic", "--owner", "1.1")
        assert "Would link 1 chunks across 1 existing Documents." in out
        assert "Skipped 1 title groups (2 chunks)" in out
        assert "Synthetic fallback" not in out, "the dry run counts link decisions only"
        assert (cat.appended, cat.registered, cat.written) == ({}, [], {})

    def test_no_ambiguous_line_when_no_title_is_ambiguous(self, monkeypatch) -> None:
        # the line is conditional: pins that it is not printed as an unconditional "0".
        cat = _FakeManifestCatalog([_titled_entry("1.1.1", "T")])
        t3 = _FakeT3({"docs__x": [{"id": "t2", "meta": {"title": "T"}}]})
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        monkeypatch.setattr("nexus.db.make_t3", lambda: t3)
        result = CliRunner().invoke(
            main, ["catalog", "orphan-backfill", "link-existing", "docs__x", "--by", "title"],
        )
        assert result.exit_code == 0, result.output
        assert "whose title names more than one" not in result.output
