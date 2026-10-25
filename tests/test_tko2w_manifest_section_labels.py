# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-tko2w (RDR-108): section_title / section_type / page_number are per-position labels, so
they travel on the catalog manifest row, not only on the shared T3 chunk row.

Pure unit tests: the manifest producer, the wire dict a manifest re-write sends, the typed row the
client parses, and the owner-resolved ``section_types`` of ``store_get_many``. The engine half (the
column, the where filter answered per occurrence) is pinned in
``PgVectorManifestLabelFilterTest`` and ``CatalogRepositoryTest``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import patch

from nexus.catalog.http_catalog_client import _manifest_row_from_dict
from nexus.catalog.store_hook import _retract_manifest_rows_for_chash
from nexus.catalog.types import ManifestRow, manifest_row_wire
from nexus.mcp.core import resolve_owner_section_types
from nexus.mcp_infra import _manifest_chunk_rows

X = "a" * 64
Y = "b" * 64


# ── the producer ─────────────────────────────────────────────────────────────

def test_manifest_chunk_rows_carry_the_labels_of_each_position() -> None:
    rows = _manifest_chunk_rows([
        (0, {"chunk_text_hash": X, "chunk_index": 0, "section_title": "References",
             "section_type": "references", "page_number": 9}),
        (1, {"chunk_text_hash": Y, "chunk_index": 1, "section_title": "Intro",
             "section_type": "body", "page_number": 1}),
    ])

    assert [(r["section_title"], r["section_type"], r["page_number"]) for r in rows] == [
        ("References", "references", 9), ("Intro", "body", 1),
    ]


def test_manifest_chunk_rows_send_null_for_the_chunkers_none_here_defaults() -> None:
    """"" (a chunk before the first heading) and 0 (a non-paged document) are "no label"."""
    (row,) = _manifest_chunk_rows([
        (0, {"chunk_text_hash": X, "section_title": "", "section_type": "", "page_number": 0}),
    ])
    assert (row["section_title"], row["section_type"], row["page_number"]) == (None, None, None)

    (bare,) = _manifest_chunk_rows([(0, {"chunk_text_hash": X})])
    assert (bare["section_title"], bare["section_type"], bare["page_number"]) == (None, None, None)


# ── a manifest re-write must not drop the labels ─────────────────────────────

def test_manifest_row_wire_carries_every_field_of_the_row() -> None:
    row = ManifestRow(
        position=3, chash=X, chunk_index=3, line_start=1, line_end=2, char_start=10, char_end=20,
        collection="docs__c", section_title="Methods", section_type="methods", page_number=4,
    )

    assert manifest_row_wire(row) == {
        "chash": X, "position": 3, "chunk_index": 3, "line_start": 1, "line_end": 2,
        "char_start": 10, "char_end": 20,
        "section_title": "Methods", "section_type": "methods", "page_number": 4,
    }


def test_manifest_row_wire_tolerates_a_double_with_fewer_fields() -> None:
    wire = manifest_row_wire(SimpleNamespace(chash=X, position=0))

    assert wire["chash"] == X and wire["position"] == 0
    assert wire["section_type"] is None and wire["page_number"] is None


@dataclass
class _Row:
    chash: str
    position: int = 0
    chunk_index: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    section_title: str | None = None
    section_type: str | None = None
    page_number: int | None = None


class _Reader:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    def get_manifest(self, tumbler: str) -> list[_Row]:
        return self._rows


@dataclass
class _Writer:
    calls: list[tuple[str, list[dict], str]] = field(default_factory=list)

    def write_manifest(self, tumbler: str, remaining: list[dict], *, collection: str) -> None:
        self.calls.append((tumbler, remaining, collection))


def test_retracting_one_chash_rewrites_the_others_with_their_labels() -> None:
    """The retraction reads the whole manifest and writes the rest back. A row that came back
    without its labels would be rewritten with NULL ones, and only a re-index restores them."""
    reader = _Reader([
        _Row(chash=X, position=0, section_title="Refs", section_type="references", page_number=9),
        _Row(chash=Y, position=1, section_title="Intro", section_type="body", page_number=1),
    ])
    writer = _Writer()
    entry = SimpleNamespace(tumbler="1.2.3", physical_collection="docs__c")

    _retract_manifest_rows_for_chash(reader, writer, entry, X)

    ((_, remaining, collection),) = writer.calls
    assert collection == "docs__c"
    assert [(r["chash"], r["section_title"], r["section_type"], r["page_number"]) for r in remaining] == [
        (Y, "Intro", "body", 1),
    ]


# ── the typed row the client parses ──────────────────────────────────────────

def test_the_client_parses_the_labels_and_tolerates_a_pre_field_engine() -> None:
    labelled = _manifest_row_from_dict({
        "position": 2, "chash": X, "section_title": "Intro", "section_type": "body", "page_number": 7,
    })
    assert (labelled.section_title, labelled.section_type, labelled.page_number) == ("Intro", "body", 7)

    old = _manifest_row_from_dict({"position": 2, "chash": X})
    assert (old.section_title, old.section_type, old.page_number) == (None, None, None)


# ── store_get_many: the label of a chunk is read from its owners ─────────────

class _Catalog:
    def __init__(self, rows) -> None:
        self._rows = rows

    def chash_positions(self, chashes: list[str]):
        return self._rows


def _owner(chash: str, doc: str, section_type: str | None, collection: str = "docs__c") -> dict:
    return {"chash": chash, "doc_id": doc, "position": 0, "chunk_count": 1,
            "collection": collection, "section_type": section_type, "section_title": None,
            "page_number": None}


def _resolve(rows, *, stored: str = "", collection: str = "docs__c", chunk: str = X) -> list[str]:
    with patch("nexus.mcp.core._get_catalog", return_value=_Catalog(rows)):
        return resolve_owner_section_types([chunk], [True], [collection], [stored])


def test_owners_that_agree_give_their_label_over_the_chunk_rows_last_writer() -> None:
    rows = [_owner(X, "1.1.1", "references"), _owner(X, "1.1.2", "references")]
    assert _resolve(rows, stored="body") == ["references"]


def test_owners_that_disagree_make_the_label_ambiguous_and_empty() -> None:
    """A bare chunk id does not say which document's position it means. Empty is the value every
    consumer keeps (the plan runner drops only an exact ``imports``)."""
    rows = [_owner(X, "1.1.1", "imports"), _owner(X, "1.1.2", "method")]
    assert _resolve(rows, stored="imports") == [""]


def test_an_unlabelled_owner_does_not_vote() -> None:
    rows = [_owner(X, "1.1.1", None), _owner(X, "1.1.2", "method")]
    assert _resolve(rows, stored="imports") == ["method"]


def test_no_labelled_owner_falls_back_to_the_chunk_rows_stored_value() -> None:
    """A manifest written before catalog-046 has no labels; the stored value is what this field
    always answered, and a re-index replaces it."""
    assert _resolve([_owner(X, "1.1.1", None)], stored="imports") == ["imports"]
    assert _resolve([], stored="imports") == ["imports"]


def test_only_the_owners_in_the_collection_the_chunk_was_found_in_count() -> None:
    rows = [_owner(X, "1.1.1", "imports", "docs__c"), _owner(X, "2.2.2", "method", "docs__other")]
    assert _resolve(rows, stored="", collection="docs__c") == ["imports"]


def test_an_engine_without_the_route_or_a_failing_catalog_leaves_the_stored_value() -> None:
    with patch("nexus.mcp.core._get_catalog", return_value=SimpleNamespace()):
        assert resolve_owner_section_types([X], [True], ["docs__c"], ["imports"]) == ["imports"]
    with patch("nexus.mcp.core._get_catalog", return_value=_Catalog(None)):
        assert resolve_owner_section_types([X], [True], ["docs__c"], ["imports"]) == ["imports"]
    with patch("nexus.mcp.core._get_catalog", side_effect=RuntimeError("catalog down")):
        assert resolve_owner_section_types([X], [True], ["docs__c"], ["imports"]) == ["imports"]


def test_ids_that_are_not_chashes_and_missing_entries_are_never_looked_up() -> None:
    class _Boom:
        def chash_positions(self, chashes):
            raise AssertionError("no chash to look up")

    with patch("nexus.mcp.core._get_catalog", return_value=_Boom()):
        assert resolve_owner_section_types(["doc-1", X], [True, False], ["c", "c"], ["a", "b"]) == ["a", "b"]
