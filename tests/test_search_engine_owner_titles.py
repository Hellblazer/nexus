# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-sis0m.5: a search hit's display title comes from the catalog's live
documents holding its chunk, not the chunk row's last-writer title."""
from __future__ import annotations

from types import SimpleNamespace

from nexus.formatters import display_title
from nexus.search_engine import (
    _attach_display_paths,
    _attach_from_chash_positions,
    owner_titles,
)
from nexus.types import SearchResult


class _Catalog:
    def __init__(self, rows, live):
        self._rows, self._live = rows, live

    def chash_positions(self, chashes):
        return [r for r in self._rows if r["chash"] in chashes]

    def resolve_many(self, doc_ids):
        return {d: SimpleNamespace(title=self._live[d], file_path="") for d in doc_ids if d in self._live}


def _hit(chash: str, title: str) -> SearchResult:
    return SearchResult(id=chash, content="x", distance=0.1, collection="c",
                        metadata={"chunk_text_hash": chash, "title": title})


def test_a_shared_chunk_names_every_live_owner() -> None:
    cat = _Catalog(
        [{"chash": "h", "doc_id": "1.1.1", "position": 0, "chunk_count": 1},
         {"chash": "h", "doc_id": "1.1.2", "position": 0, "chunk_count": 1}],
        {"1.1.1": "note A", "1.1.2": "note B"},
    )
    hits = [_hit("h", "note B")]
    assert _attach_from_chash_positions(hits, ["h"], cat)
    _attach_display_paths(hits, cat)
    assert display_title(hits[0].metadata) == "note A · note B"


def test_a_deleted_last_writer_is_not_named() -> None:
    """After delete --title removes the last writer, the row still carries
    its title; the surviving owner is what the hit shows."""
    cat = _Catalog(
        [{"chash": "h", "doc_id": "1.1.2", "position": 0, "chunk_count": 1}],
        {"1.1.2": "survivor"},
    )
    hits = [_hit("h", "deleted note")]
    assert _attach_from_chash_positions(hits, ["h"], cat)
    _attach_display_paths(hits, cat)
    assert display_title(hits[0].metadata) == "survivor"


def test_owner_titles_caps_a_long_list() -> None:
    assert owner_titles(["d", "a", "c", "b", "a"]) == "a · b · c (+1 more)"
