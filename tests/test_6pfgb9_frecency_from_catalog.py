# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.9 (RDR-236 Phase 1): ``frecency_score`` is a DOCUMENT key. Its home is the
document's catalog ``meta`` beside a ``frecency_computed_at``, and the scorer reads the
attributed owner's value, never the shared chunk row's (the last writer's file score).

Storage-free on purpose: the catalog is a stub, so no test here mints a tenant.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.catalog.tumbler import Tumbler
from nexus.catalog.types import CatalogEntry
from nexus.types import SearchResult

COLL = "code__repo"
X = "a" * 64  # one chunk shared by documents A and B
Y = "b" * 64  # a chunk only C holds
Z = "c" * 64  # a chunk only D holds


def _entry(tumbler: str, *, meta: dict | None = None, collection: str = COLL) -> CatalogEntry:
    return CatalogEntry(
        tumbler=Tumbler.parse(tumbler), title=f"doc {tumbler}", author="", year=0, content_type="code",
        file_path=f"{tumbler}.py", corpus="", physical_collection=collection, chunk_count=1,
        head_hash="", indexed_at="2026-09-01T00:00:00+00:00", bib_year=0, meta=meta or {},
    )


def _position(chash: str, doc: str) -> dict:
    return {"chash": chash, "doc_id": doc, "position": 0, "chunk_count": 1, "collection": COLL}


class _Catalog:
    def __init__(self, entries: list[CatalogEntry], positions: list[dict]):
        self.entries = {str(e.tumbler): e for e in entries}
        self.positions = positions
        self.resolve_calls: list[list[str]] = []

    def resolve_many(self, doc_ids):
        self.resolve_calls.append(list(doc_ids))
        return {d: self.entries[d] for d in doc_ids if d in self.entries}

    def chash_positions(self, chashes):
        return [p for p in self.positions if p["chash"] in chashes]


def _hit(
    chash: str, *, doc_id: str = "", row_frecency: float | None = None, distance: float = 0.2,
    hit_id: str = "",
) -> SearchResult:
    meta: dict = {"chunk_text_hash": chash}
    if doc_id:
        meta["doc_id"] = doc_id
    if row_frecency is not None:
        meta["frecency_score"] = row_frecency
    return SearchResult(id=hit_id or chash, content="x", distance=distance, collection=COLL, metadata=meta)


def _rank(hits: list[SearchResult], catalog: _Catalog | None) -> list[SearchResult]:
    """The search tail that matters here: attribute owners, resolve the page once, then score."""
    from nexus.scoring import apply_hybrid_scoring
    from nexus.search_engine import _attach_display_paths, _attach_from_chash_positions

    if catalog is not None:
        assert _attach_from_chash_positions(hits, [h.metadata["chunk_text_hash"] for h in hits], catalog)
    _attach_display_paths(hits, catalog)
    # the collection's registry row: the scorer weighs frecency for code collections only
    with patch("nexus.mcp_infra.get_collection_row", return_value={"content_type": "code"}):
        return apply_hybrid_scoring(hits, hybrid=True)


# -- the reader ----------------------------------------------------------------------------


def test_a_shared_chunk_scores_by_its_attributed_owners_frecency_not_the_rows() -> None:
    """Failing-first pin. A (0.1) and B (0.9) share X; B wrote last, so the row says 0.9. The hit
    attributed to A scores with A's 0.1, below C's 0.5; reading the row would put it above."""
    a = _entry("1.1.1", meta={"frecency_score": 0.1, "frecency_computed_at": "2026-10-01T00:00:00+00:00"})
    b = _entry("1.1.2", meta={"frecency_score": 0.9})
    c = _entry("1.1.3", meta={"frecency_score": 0.5})
    catalog = _Catalog([a, b, c], [_position(X, "1.1.1"), _position(X, "1.1.2"), _position(Y, "1.1.3")])
    shared = _hit(X, doc_id="1.1.1", row_frecency=0.9)
    other = _hit(Y, row_frecency=0.5)

    ranked = _rank([shared, other], catalog)

    assert ranked[0] is other, [r.hybrid_score for r in ranked]
    assert shared.hybrid_score < other.hybrid_score


def test_two_owners_of_one_chunk_each_score_by_their_own_frecency() -> None:
    """The same shared chunk, hit once as A and once as B: the two hits score differently, A's
    low and B's high, whichever of them wrote the row."""
    a = _entry("1.1.1", meta={"frecency_score": 0.1})
    b = _entry("1.1.2", meta={"frecency_score": 0.9})
    catalog = _Catalog([a, b], [_position(X, "1.1.1"), _position(X, "1.1.2")])
    as_a = _hit(X, doc_id="1.1.1", row_frecency=0.9)
    as_b = _hit(X, doc_id="1.1.2", row_frecency=0.9, hit_id="second")

    _rank([as_a, as_b], catalog)

    assert as_a.hybrid_score < as_b.hybrid_score


def test_a_shared_chunk_with_no_row_doc_id_scores_by_the_best_of_its_owners() -> None:
    """Review S2 pin. The row names no document, so the owner is chosen by the engine's order
    (B first, 0.2): that choice is arbitrary, and a hit's frecency must not depend on it. The hit
    scores with the MAX of its live owners' catalog scores, taken from the page's one lookup."""
    a = _entry("1.1.1", meta={"frecency_score": 0.9})
    b = _entry("1.1.2", meta={"frecency_score": 0.2})
    c = _entry("1.1.3", meta={"frecency_score": 0.5})
    catalog = _Catalog([a, b, c], [_position(X, "1.1.2"), _position(X, "1.1.1"), _position(Y, "1.1.3")])
    shared = _hit(X, row_frecency=0.2)  # no doc_id on the row
    other = _hit(Y)

    ranked = _rank([shared, other], catalog)

    assert shared.metadata["doc_id"] == "1.1.2", "the display attribution is unchanged: engine order"
    assert shared.metadata["_owner_frecency"] == 0.9
    assert ranked[0] is shared, [r.hybrid_score for r in ranked]
    assert len(catalog.resolve_calls) == 1, "no new catalog call"


def test_the_best_of_the_owners_skips_an_owner_that_states_no_score() -> None:
    a = _entry("1.1.1")  # states none
    b = _entry("1.1.2", meta={"frecency_score": 0.2})
    catalog = _Catalog([a, b], [_position(X, "1.1.1"), _position(X, "1.1.2")])
    shared = _hit(X)

    _rank([shared], catalog)

    assert shared.metadata["_owner_frecency"] == 0.2


def test_the_best_of_the_owners_holds_on_the_manifest_path_too() -> None:
    """An engine without the chash-positions route attributes through the reverse lookup."""
    from nexus.search_engine import _attach_display_paths, _attach_doc_ids_from_catalog

    a = _entry("1.1.1", meta={"frecency_score": 0.9})
    b = _entry("1.1.2", meta={"frecency_score": 0.2})

    class _ManifestCatalog(_Catalog):
        chash_positions = None

        def docs_for_chashes(self, chashes):
            return {X: ["1.1.2", "1.1.1"]}

        def get_manifests(self, doc_ids):
            return {d: [{"chash": X, "position": 0}] for d in doc_ids}

    catalog = _ManifestCatalog([a, b], [])
    shared = _hit(X)

    _attach_doc_ids_from_catalog([shared], catalog)
    _attach_display_paths([shared], catalog)

    assert shared.metadata["doc_id"] == "1.1.2"
    assert shared.metadata["_owner_frecency"] == 0.9


def test_a_multi_owner_hit_whose_owner_has_no_catalog_score_scores_zero_not_the_row() -> None:
    a = _entry("1.1.1")  # no frecency in meta
    b = _entry("1.1.2", meta={"frecency_score": 0.9})
    c = _entry("1.1.3", meta={"frecency_score": 0.5})
    catalog = _Catalog([a, b, c], [_position(X, "1.1.1"), _position(X, "1.1.2"), _position(Y, "1.1.3")])
    shared = _hit(X, doc_id="1.1.1", row_frecency=0.9)
    other = _hit(Y)

    _rank([shared, other], catalog)

    assert shared.hybrid_score < other.hybrid_score  # 0.0 against 0.5


def test_a_single_owner_chunk_without_a_catalog_score_still_scores_from_its_row() -> None:
    """Legacy fallback: nothing has stamped the document yet (the stamp verb is 6pfgb.10)."""
    c = _entry("1.1.3")  # no frecency in meta
    d = _entry("1.1.4", meta={"frecency_score": 0.5})
    catalog = _Catalog([c, d], [_position(Y, "1.1.3"), _position(Z, "1.1.4")])
    legacy = _hit(Y, row_frecency=0.9)
    other = _hit(Z)

    ranked = _rank([legacy, other], catalog)

    assert ranked[0] is legacy


def test_no_catalog_scores_from_the_row_as_before() -> None:
    low = _hit(Y, row_frecency=0.1)
    high = _hit(Z, row_frecency=0.9)

    ranked = _rank([low, high], None)

    assert ranked[0] is high


@pytest.mark.parametrize("bad", [True, "0.9", float("nan"), float("inf"), None])
def test_a_malformed_catalog_score_is_not_a_score(bad: object) -> None:
    c = _entry("1.1.3", meta={"frecency_score": bad})
    catalog = _Catalog([c], [_position(Y, "1.1.3")])
    hit = _hit(Y, row_frecency=0.9)

    _rank([hit], catalog)

    assert hit.metadata.get("_owner_frecency") is None


def test_the_page_resolves_its_owners_once_and_the_frecency_rides_on_that_call() -> None:
    chashes = [format(i, "x").rjust(64, "0") for i in range(1, 7)]
    entries = [_entry(f"1.1.{i}", meta={"frecency_score": i / 10}) for i in range(1, 7)]
    catalog = _Catalog(entries, [_position(c, f"1.1.{i}") for i, c in enumerate(chashes, start=1)])
    hits = [_hit(c) for c in chashes]

    ranked = _rank(hits, catalog)

    assert len(catalog.resolve_calls) == 1
    assert [r.metadata["_owner_frecency"] for r in hits] == [i / 10 for i in range(1, 7)]
    assert ranked[0] is hits[-1]  # equal distances: the highest frecency leads


def test_the_owner_frecency_working_key_stays_out_of_json() -> None:
    from nexus.formatters import format_json

    c = _entry("1.1.3", meta={"frecency_score": 0.5})
    catalog = _Catalog([c], [_position(Y, "1.1.3")])
    hit = _hit(Y)

    _rank([hit], catalog)

    assert hit.metadata["_owner_frecency"] == 0.5
    assert "_owner_frecency" not in json.dumps(json.loads(format_json([hit])))


def test_an_owner_in_another_collection_lends_no_frecency() -> None:
    other = _entry("1.1.3", meta={"frecency_score": 0.5}, collection="code__elsewhere")
    catalog = _Catalog([other], [_position(Y, "1.1.3")])
    hit = _hit(Y, row_frecency=0.9)

    _rank([hit], catalog)

    assert "_owner_frecency" not in hit.metadata


# -- the writer ------------------------------------------------------------------------------


class _Writer:
    """A catalog writer with ``update`` only (the SQLite-era shape): records each call."""

    def __init__(self, fail_on: str = "") -> None:
        self.updates: list[tuple[str, dict]] = []
        self.fail_on = fail_on

    def update(self, tumbler, **fields):
        if str(tumbler) == self.fail_on:
            raise RuntimeError("boom")
        self.updates.append((str(tumbler), fields))


class _BatchWriter(_Writer):
    def __init__(self) -> None:
        super().__init__()
        self.batches: list[list[dict]] = []

    def update_many(self, updates):
        self.batches.append(updates)
        return [1] * len(updates)


def test_the_stamp_sends_only_the_two_owned_keys_per_document() -> None:
    from nexus.indexer import _write_frecency_meta

    w = _Writer()
    n = _write_frecency_meta({"1.1.1": 0.25, "1.1.2": 0.75}, "2026-10-30T00:00:00+00:00", writer=w)

    assert n == 2
    assert w.updates == [
        ("1.1.1", {"meta": {"frecency_score": 0.25, "frecency_computed_at": "2026-10-30T00:00:00+00:00"}}),
        ("1.1.2", {"meta": {"frecency_score": 0.75, "frecency_computed_at": "2026-10-30T00:00:00+00:00"}}),
    ]


def test_the_stamp_batches_when_the_writer_can() -> None:
    from nexus.indexer import _write_frecency_meta

    w = _BatchWriter()
    _write_frecency_meta({"1.1.1": 0.25, "1.1.2": 0.75}, "T", writer=w)

    assert w.updates == []
    assert w.batches == [[
        {"tumbler": "1.1.1", "meta": {"frecency_score": 0.25, "frecency_computed_at": "T"}},
        {"tumbler": "1.1.2", "meta": {"frecency_score": 0.75, "frecency_computed_at": "T"}},
    ]]


def test_one_documents_failed_stamp_does_not_stop_the_rest() -> None:
    from nexus.indexer import _write_frecency_meta

    w = _Writer(fail_on="1.1.1")
    n = _write_frecency_meta({"1.1.1": 0.25, "1.1.2": 0.75}, "T", writer=w)

    assert n == 1 and [u[0] for u in w.updates] == ["1.1.2"]


def test_a_non_finite_score_is_not_stamped() -> None:
    from nexus.indexer import _write_frecency_meta

    w = _Writer()
    n = _write_frecency_meta({"1.1.1": float("nan"), "1.1.2": 0.5}, "T", writer=w)

    assert n == 1 and [u[0] for u in w.updates] == ["1.1.2"]


class _ClosingWriter(_BatchWriter):
    def __init__(self) -> None:
        super().__init__()
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def test_a_writer_the_stamp_created_is_closed_one_it_was_handed_is_not(monkeypatch) -> None:
    """Review L1: one leaked HTTP client per index run. The caller owns what it injects."""
    import nexus.catalog.factory as factory
    from nexus.indexer import _write_frecency_meta

    made = _ClosingWriter()
    monkeypatch.setattr(factory, "make_catalog_writer", lambda: made)
    assert _write_frecency_meta({"1.1.1": 0.25}, "T") == 1
    assert made.closed == 1, "the writer the stamp created is closed"

    handed = _ClosingWriter()
    assert _write_frecency_meta({"1.1.1": 0.25}, "T", writer=handed) == 1
    assert handed.closed == 0, "the caller's writer is the caller's to close"


def test_a_created_writer_is_closed_when_the_stamp_fails(monkeypatch) -> None:
    import nexus.catalog.factory as factory
    from nexus.indexer import _write_frecency_meta

    class _Boom(_ClosingWriter):
        def update_many(self, updates):
            raise RuntimeError("catalog down")

    made = _Boom()
    monkeypatch.setattr(factory, "make_catalog_writer", lambda: made)
    assert _write_frecency_meta({"1.1.1": 0.25}, "T") == 0
    assert made.closed == 1


def _frecency_only(tmp_path: Path, writer, *, doc_ids: bool = True):
    from nexus.indexer import _run_index_frecency_only

    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "main.py"
    src.write_text("x = 1\n")
    other = repo / "other.py"
    other.write_text("y = 2\n")
    col = MagicMock()
    col.get.return_value = {"ids": ["c1"], "metadatas": [{}]}
    db = MagicMock()
    db.get_collection.return_value = col
    reg = MagicMock()
    reg.get.return_value = {"collection": "code__repo", "code_collection": "code__repo", "docs_collection": ""}
    mapping = {src: "1.1.1", other: "1.1.2"} if doc_ids else {}
    with patch("nexus.indexer._build_frecency_doc_id_map", return_value=mapping), \
         patch("nexus.frecency.batch_frecency", return_value={src: 0.75, other: 0.25}), \
         patch("nexus.catalog.factory.make_catalog_reader", return_value=None), \
         patch("nexus.catalog.factory.make_catalog_writer", return_value=writer), \
         patch("nexus.indexer._repo_collection_or_legacy", return_value=""), \
         patch("nexus.db.make_t3", return_value=db), \
         patch("nexus.db.http_vector_client.get_http_vector_client", return_value=db):
        _run_index_frecency_only(repo, reg)
    return db


def test_frecency_only_stamps_each_owning_document_once_with_both_keys(tmp_path: Path) -> None:
    w = _Writer()
    _frecency_only(tmp_path, w)

    assert sorted(t for t, _ in w.updates) == ["1.1.1", "1.1.2"]
    by_doc = {t: f["meta"] for t, f in w.updates}
    assert set(by_doc["1.1.1"]) == {"frecency_score", "frecency_computed_at"}
    assert by_doc["1.1.1"]["frecency_score"] == 0.75 and by_doc["1.1.2"]["frecency_score"] == 0.25
    assert by_doc["1.1.1"]["frecency_computed_at"] == by_doc["1.1.2"]["frecency_computed_at"]
    assert by_doc["1.1.1"]["frecency_computed_at"]  # present on every write


def test_frecency_only_skips_a_file_with_no_document(tmp_path: Path) -> None:
    w = _Writer()
    _frecency_only(tmp_path, w, doc_ids=False)

    assert w.updates == []


def test_frecency_only_survives_a_catalog_that_cannot_write(tmp_path: Path) -> None:
    class _Down:
        def update(self, *a, **k):
            raise RuntimeError("catalog down")

    db = _frecency_only(tmp_path, _Down())

    db.update_chunks.assert_called()  # the row write is unaffected
