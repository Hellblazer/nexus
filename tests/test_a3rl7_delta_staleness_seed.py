# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-a3rl7: ``seed_staleness_cache_from_catalog`` builds a ``--since-head`` delta's
staleness cache from the catalog for the delta's own documents, so a delta file the last run
already indexed reads current.

The delta path (nexus-fltb4) used to hand ``check_staleness`` an empty ``StalenessCache``: a
cached branch with nothing in it, so every delta file was a miss and re-indexed even at an
unchanged hash. The seed fills the same entries the full run's sweep plus fence overlay would
(``(fence hash, model)`` for a document present in T3 and fenced ``'complete'``), but for the
delta's documents only, from one batched manifest read and batched chunk reads, never a sweep
of the collection.

Storage-free (the python_unit tenant ceiling is a hard ratchet): the catalog and the
collection are doubles. The journey through ``index_repository(since_head=True)`` on the real
engine is ``tests/integration/test_a3rl7_since_head_skips_unchanged_delta.py``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.indexer_utils import StalenessCache, check_staleness, seed_staleness_cache_from_catalog

H = "a" * 64
H_OLD = "0" * 64
MODEL = "voyage-context-3"


class _Col:
    """T3 double: ``get(ids=...)`` returns the rows it holds, records every call."""

    name = "docs__t__voyage-context-3__v1"

    def __init__(self, rows: dict[str, dict], *, fail: bool = False) -> None:
        self.rows = rows
        self.fail = fail
        self.calls: list[list[str]] = []

    def get(self, ids=None, include=None, **_kw):
        self.calls.append(list(ids or []))
        if self.fail:
            raise RuntimeError("t3 away")
        picked = [(c, self.rows[c]) for c in (ids or []) if c in self.rows]
        return {"ids": [c for c, _ in picked], "metadatas": [m for _, m in picked]}


class _Cat:
    def __init__(self, manifests: dict[str, list[tuple[str, int]]], *, fail: bool = False) -> None:
        self.manifests = {
            d: [SimpleNamespace(chash=c, position=p) for c, p in rows] for d, rows in manifests.items()
        }
        self.fail = fail
        self.requests: list[list[str]] = []

    def get_manifests(self, doc_ids):
        self.requests.append(list(doc_ids))
        if self.fail:
            raise RuntimeError("catalog away")
        return {d: self.manifests.get(d, []) for d in doc_ids}

    def close(self) -> None:
        pass


def _patch(monkeypatch, cat: _Cat) -> None:
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: cat, raising=False)


def _current(cache: StalenessCache, doc_id: str, file_hash: str = H, model: str = MODEL) -> bool:
    return check_staleness(None, "f.md", file_hash, model, doc_id=doc_id, cache=cache)


def test_an_unchanged_delta_document_present_in_t3_reads_current(monkeypatch) -> None:
    """The bug: an empty cache made this a miss. Fence complete at the file's hash, the first
    manifest chunk present in the collection with the target model."""
    _patch(monkeypatch, _Cat({"1.1.1": [("c1", 0), ("c2", 1)]}))
    col = _Col({"c1": {"embedding_model": MODEL, "content_hash": "another-doc-wrote-last"}})
    cache = StalenessCache()
    assert seed_staleness_cache_from_catalog(cache, col, ["1.1.1"], {"1.1.1": H}) == 1
    assert _current(cache, "1.1.1") is True
    assert col.calls == [["c1"]], "the document's first manifest position is the probe"


def test_a_changed_delta_document_reads_stale(monkeypatch) -> None:
    """Regression guard (an empty cache already missed): the fence is at the old hash."""
    _patch(monkeypatch, _Cat({"1.1.1": [("c1", 0)]}))
    cache = StalenessCache()
    seed_staleness_cache_from_catalog(cache, _Col({"c1": {"embedding_model": MODEL}}), ["1.1.1"], {"1.1.1": H_OLD})
    assert _current(cache, "1.1.1") is False


def test_a_chunk_row_stamped_with_the_new_hash_does_not_make_a_changed_document_current(monkeypatch) -> None:
    """The seed takes the hash from the fence, never from the shared chunk row."""
    _patch(monkeypatch, _Cat({"1.1.1": [("c1", 0)]}))
    cache = StalenessCache()
    col = _Col({"c1": {"embedding_model": MODEL, "content_hash": H}})
    seed_staleness_cache_from_catalog(cache, col, ["1.1.1"], {"1.1.1": H_OLD})
    assert _current(cache, "1.1.1") is False


def test_a_document_without_a_complete_fence_is_not_seeded(monkeypatch) -> None:
    cat = _Cat({"1.1.1": [("c1", 0)]})
    _patch(monkeypatch, cat)
    cache = StalenessCache()
    col = _Col({"c1": {"embedding_model": MODEL}})
    assert seed_staleness_cache_from_catalog(cache, col, ["1.1.1"], {}) == 0
    assert _current(cache, "1.1.1") is False
    assert cat.requests == [] and col.calls == [], "nothing to decide, nothing read"


def test_a_document_whose_chunk_is_not_in_t3_is_not_seeded(monkeypatch) -> None:
    _patch(monkeypatch, _Cat({"1.1.1": [("c1", 0)]}))
    cache = StalenessCache()
    assert seed_staleness_cache_from_catalog(cache, _Col({}), ["1.1.1"], {"1.1.1": H}) == 0
    assert _current(cache, "1.1.1") is False


def test_a_document_with_no_manifest_row_is_not_seeded(monkeypatch) -> None:
    _patch(monkeypatch, _Cat({"1.1.1": []}))
    cache = StalenessCache()
    assert seed_staleness_cache_from_catalog(cache, _Col({}), ["1.1.1"], {"1.1.1": H}) == 0
    assert _current(cache, "1.1.1") is False


def test_a_document_stored_with_another_model_reads_stale(monkeypatch) -> None:
    _patch(monkeypatch, _Cat({"1.1.1": [("c1", 0)]}))
    cache = StalenessCache()
    seed_staleness_cache_from_catalog(cache, _Col({"c1": {"embedding_model": "old-model"}}), ["1.1.1"], {"1.1.1": H})
    assert _current(cache, "1.1.1") is False


def test_a_stranded_fence_still_wins_over_a_seeded_entry(monkeypatch) -> None:
    _patch(monkeypatch, _Cat({"1.1.1": [("c1", 0)]}))
    cache = StalenessCache(never_fresh=frozenset({"1.1.1"}))
    seed_staleness_cache_from_catalog(cache, _Col({"c1": {"embedding_model": MODEL}}), ["1.1.1"], {"1.1.1": H})
    assert _current(cache, "1.1.1") is False


def test_only_the_delta_documents_are_read_in_batches_not_one_call_each(monkeypatch) -> None:
    """The cost the delta path exists to keep small: one manifest read and one chunk read for
    the whole delta, whatever its size, and nothing for a document outside it."""
    manifests = {f"1.1.{i}": [(f"c{i}", 0)] for i in range(1, 31)}
    cat = _Cat(manifests)
    _patch(monkeypatch, cat)
    col = _Col({f"c{i}": {"embedding_model": MODEL} for i in range(1, 31)})
    cache = StalenessCache()
    delta = [f"1.1.{i}" for i in range(1, 11)]
    fences = {d: H for d in manifests}
    assert seed_staleness_cache_from_catalog(cache, col, delta, fences) == 10
    assert len(cat.requests) == 1 and sorted(cat.requests[0]) == sorted(delta)
    assert len(col.calls) == 1 and len(col.calls[0]) == 10
    assert set(cache.by_doc_id) == set(delta)


def test_a_catalog_failure_seeds_nothing_and_does_not_raise(monkeypatch) -> None:
    _patch(monkeypatch, _Cat({"1.1.1": [("c1", 0)]}, fail=True))
    cache = StalenessCache()
    assert seed_staleness_cache_from_catalog(
        cache, _Col({"c1": {"embedding_model": MODEL}}), ["1.1.1"], {"1.1.1": H}) == 0
    assert _current(cache, "1.1.1") is False


def test_a_t3_failure_seeds_nothing_and_does_not_raise(monkeypatch) -> None:
    _patch(monkeypatch, _Cat({"1.1.1": [("c1", 0)]}))
    cache = StalenessCache()
    assert seed_staleness_cache_from_catalog(
        cache, _Col({"c1": {"embedding_model": MODEL}}, fail=True), ["1.1.1"], {"1.1.1": H}) == 0
    assert _current(cache, "1.1.1") is False


@pytest.mark.parametrize("col", [None])
def test_no_collection_seeds_nothing(col, monkeypatch) -> None:
    cat = _Cat({"1.1.1": [("c1", 0)]})
    _patch(monkeypatch, cat)
    cache = StalenessCache()
    assert seed_staleness_cache_from_catalog(cache, col, ["1.1.1"], {"1.1.1": H}) == 0
    assert cat.requests == []
