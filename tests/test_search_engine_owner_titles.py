# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-sis0m.5, nexus-fv2u4: a search hit's display title is the catalog title of the
document the hit is attributed to (``doc_id``), never the chunk row's last-writer title and
never a join of the other documents sharing the chunk (shakedown 7.94.0 class B)."""
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
        return {
            d: SimpleNamespace(title=self._live[d][0], file_path="", physical_collection=self._live[d][1])
            for d in doc_ids if d in self._live
        }


def _hit(chash: str, title: str) -> SearchResult:
    return SearchResult(id=chash, content="x", distance=0.1, collection="c",
                        metadata={"chunk_text_hash": chash, "title": title})


def test_a_shared_chunk_shows_the_attributed_owner_only() -> None:
    cat = _Catalog(
        [{"chash": "h", "doc_id": "1.1.1", "position": 0, "chunk_count": 1},
         {"chash": "h", "doc_id": "1.1.2", "position": 0, "chunk_count": 1}],
        {"1.1.1": ("note A", "c"), "1.1.2": ("note B", "c")},
    )
    hits = [_hit("h", "note B")]
    assert _attach_from_chash_positions(hits, ["h"], cat)
    _attach_display_paths(hits, cat)
    # The lookup attributes the hit to the smallest doc_id (1.1.1); B's title is not joined on.
    assert hits[0].metadata["doc_id"] == "1.1.1"
    assert display_title(hits[0].metadata) == "note A"


def test_a_shared_chunk_whose_row_names_its_owner_shows_that_owner() -> None:
    cat = _Catalog(
        [{"chash": "h", "doc_id": "1.1.1", "position": 0, "chunk_count": 1},
         {"chash": "h", "doc_id": "1.1.2", "position": 0, "chunk_count": 1}],
        {"1.1.1": ("note A", "c"), "1.1.2": ("note B", "c")},
    )
    hit = _hit("h", "note A")
    hit.metadata["doc_id"] = "1.1.2"
    assert _attach_from_chash_positions([hit], ["h"], cat)
    _attach_display_paths([hit], cat)
    assert display_title(hit.metadata) == "note B"


def test_json_title_is_the_attributed_owners_catalog_title() -> None:
    """The ``title`` key of --json output, not only ``_display_title``: the live tenant
    printed the chunk row's ``path:lines`` on 10 of 10 sampled hits."""
    import json

    from nexus.formatters import format_json

    cat = _Catalog(
        [{"chash": "h", "doc_id": "1.1.1", "position": 0, "chunk_count": 1},
         {"chash": "h", "doc_id": "1.1.2", "position": 0, "chunk_count": 1}],
        {"1.1.1": ("main.py", "c"), "1.1.2": ("__init__.py", "c")},
    )
    hits = [_hit("h", "src/m3docrag/datasets/__init__.py:1-15")]
    assert _attach_from_chash_positions(hits, ["h"], cat)
    _attach_display_paths(hits, cat)
    row = json.loads(format_json(hits))[0]
    assert row["title"] == "main.py"
    assert row["_display_title"] == "main.py"


def test_json_title_is_null_when_the_catalog_names_none() -> None:
    """No catalog title resolved: the row's title is not stood in (R1: never a label), and the key stays
    present as null so a consumer's schema does not change with whether the catalog resolved."""
    import json

    from nexus.formatters import format_json

    row = json.loads(format_json([_hit("h", "src/other.py:1-15")]))[0]
    assert "title" in row and row["title"] is None


def test_a_deleted_last_writer_is_not_named() -> None:
    """After delete --title removes the last writer, the row still carries
    its title; the surviving owner is what the hit shows."""
    cat = _Catalog(
        [{"chash": "h", "doc_id": "1.1.2", "position": 0, "chunk_count": 1}],
        {"1.1.2": ("survivor", "c")},
    )
    hits = [_hit("h", "deleted note")]
    assert _attach_from_chash_positions(hits, ["h"], cat)
    _attach_display_paths(hits, cat)
    assert display_title(hits[0].metadata) == "survivor"


def test_owner_titles_caps_a_long_list() -> None:
    assert owner_titles(["d", "a", "c", "b", "a"]) == "a · b · c (+1 more)"


def test_an_owner_in_another_collection_is_not_named() -> None:
    """chash owner lookups are tenant-wide; identical text in another
    collection (a license header, an import copy) must not label this hit."""
    cat = _Catalog(
        [{"chash": "h", "doc_id": "1.1.1", "position": 0, "chunk_count": 1},
         {"chash": "h", "doc_id": "1.1.9", "position": 0, "chunk_count": 1}],
        {"1.1.1": ("home note", "c"), "1.1.9": ("elsewhere", "other-collection")},
    )
    hits = [_hit("h", "elsewhere")]
    assert _attach_from_chash_positions(hits, ["h"], cat)
    _attach_display_paths(hits, cat)
    assert display_title(hits[0].metadata) == "home note"


def test_json_output_keeps_the_owner_list_internal() -> None:
    import json

    from nexus.formatters import format_json

    hit = _hit("h", "t")
    hit.metadata.update({"_owner_doc_ids": ["1.1.1"], "_display_title": "note A"})
    row = json.loads(format_json([hit]))[0]
    assert "_owner_doc_ids" not in row
    assert row["_display_title"] == "note A"
