"""GH #1524 (nexus-20uv3): the same chunk text indexed in several collections
is one chash in each; ``query()`` collapses it to one result that names the
other collections instead of ranking it once per collection."""

from __future__ import annotations

from nexus.mcp.core import (
    _collapse_identical_chunk_results,
    _collapse_identical_chunk_rows,
    _dedup_by_id,
)
from nexus.types import SearchResult


def _row(tumbler: str, coll: str, chash: str, dist: float) -> dict:
    return {"id": tumbler, "collection": coll, "chash": chash, "distance": dist}


def test_rows_with_one_chash_across_collections_collapse_to_the_best_ranked() -> None:
    rows = [
        _row("1.53.4", "rdr__1-53", "aa" * 32, 0.109),
        _row("1.46.9", "rdr__1-46", "aa" * 32, 0.111),
        _row("1.61.2", "rdr__1-61", "bb" * 32, 0.115),
        _row("1.63.1", "rdr__1-63", "aa" * 32, 0.118),
        _row("1.2.7", "rdr__1-2", "aa" * 32, 0.120),
    ]
    out = _collapse_identical_chunk_rows(rows)
    assert [r["id"] for r in out] == ["1.53.4", "1.61.2"]
    assert out[0]["also_in"] == ["rdr__1-46", "rdr__1-63", "rdr__1-2"]
    assert "also_in" not in out[1]


def test_rows_without_a_chash_are_never_collapsed() -> None:
    rows = [_row("1.1.1", "docs__1-1", "", 0.1), _row("1.1.2", "docs__1-1", "", 0.2)]
    assert _collapse_identical_chunk_rows(rows) == rows


def test_dedup_by_id_applies_the_collapse_after_the_id_dedupe() -> None:
    rows = [
        _row("1.53.4", "rdr__1-53", "aa" * 32, 0.109),
        _row("1.53.4", "rdr__1-53", "aa" * 32, 0.150),
        _row("1.46.9", "rdr__1-46", "aa" * 32, 0.111),
    ]
    out = _dedup_by_id(rows)
    assert [r["id"] for r in out] == ["1.53.4"]
    assert out[0]["also_in"] == ["rdr__1-46"]


def test_search_results_collapse_by_chunk_text_hash_and_record_the_other_collections() -> None:
    def sr(rid: str, coll: str, chash: str, dist: float) -> SearchResult:
        return SearchResult(id=rid, content="Recommendation Decisioning Records", distance=dist,
                            collection=coll, metadata={"chunk_text_hash": chash})

    results = [
        sr("c1", "rdr__1-53__bge-base-en-v15-768__v1", "aa" * 32, 0.109),
        sr("c2", "rdr__1-46__bge-base-en-v15-768__v1", "aa" * 32, 0.111),
        sr("c3", "rdr__1-61__bge-base-en-v15-768__v1", "cc" * 32, 0.115),
        sr("c4", "rdr__1-46__bge-base-en-v15-768__v1", "aa" * 32, 0.116),
    ]
    out = _collapse_identical_chunk_results(results)
    assert [r.id for r in out] == ["c1", "c3"]
    assert out[0].metadata["also_in"] == ["rdr__1-46__bge-base-en-v15-768__v1"]
    assert "also_in" not in out[1].metadata
