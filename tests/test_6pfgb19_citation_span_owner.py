# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.19 (RDR-236 Phase 2, census row 18): a citation span resolves to the CITED document's
own title and manifest span, never the last writer's (the shared chunk row).

Storage-free on purpose: T3 and the catalog are stubbed, so no test here mints a tenant.
"""
from __future__ import annotations

import copy
from unittest.mock import patch

from nexus.catalog.tumbler import Tumbler
from nexus.catalog.types import CatalogEntry, ManifestRow

COLL = "knowledge__papers"
CHASH = "c" * 64  # one chunk, in document C at lines 10-20 and in document D at 40-50; D wrote last
SPAN = f"chash:{CHASH}"

_D_ROW = {
    "chunk_text_hash": CHASH, "title": "Doc D", "line_start": 40, "line_end": 50,
    "chunk_start_char": 400, "chunk_end_char": 500, "section_title": "D section", "section_type": "d-type",
    "page_number": 9, "indexed_at": "2026-09-05T00:00:00+00:00",
}


def _entry(tumbler: str, title: str) -> CatalogEntry:
    return CatalogEntry(
        tumbler=Tumbler.parse(tumbler), title=title, author="", year=0, content_type="prose",
        file_path="", corpus="", physical_collection=COLL, chunk_count=1, head_hash="",
        indexed_at="2026-08-01T00:00:00+00:00",
    )


C, D = _entry("1.1.1", "Doc C"), _entry("1.1.2", "Doc D")


class _Col:
    def __init__(self, row: dict) -> None:
        self.row = row

    def get(self, where=None, include=None, **_kw):
        return {"ids": [CHASH], "documents": ["shared text"], "metadatas": [self.row]}


class _T3:
    def __init__(self, row: dict) -> None:
        self.row = row

    def get_collection(self, _name: str) -> _Col:
        return _Col(self.row)


class _Catalog:
    def __init__(self, manifests: dict[str, list[ManifestRow]]) -> None:
        self.manifests = manifests
        self.manifest_calls: list[str] = []

    def get_manifest(self, doc_id: str):
        self.manifest_calls.append(doc_id)
        return self.manifests.get(doc_id, [])


def _catalog() -> _Catalog:
    return _Catalog({
        "1.1.1": [
            ManifestRow(position=0, chash="f" * 64),
            ManifestRow(
                position=3, chash=CHASH, line_start=10, line_end=20, char_start=100, char_end=200,
                section_title="C section", section_type="c-type", page_number=2, labels_written=True,
            ),
            ManifestRow(position=7, chash=CHASH, line_start=70, line_end=80),  # a repeat: lowest position wins
        ],
        "1.1.2": [ManifestRow(position=0, chash=CHASH, line_start=40, line_end=50, char_start=400, char_end=500)],
    })


def test_a_citation_into_each_document_returns_that_documents_title_and_span() -> None:
    """Failing-first pin (row 18): D wrote last, so the row says D for both citations."""
    from nexus.catalog.catalog_spans import resolve_span_in_t3

    t3 = _T3(copy.deepcopy(_D_ROW))
    catalog = _catalog()

    into_c = resolve_span_in_t3(SPAN, COLL, t3, entry=C, catalog=catalog)
    into_d = resolve_span_in_t3(SPAN, COLL, t3, entry=D, catalog=catalog)

    assert into_c["chunk_text"] == "shared text"
    c = into_c["metadata"]
    assert (c["title"], c["line_start"], c["line_end"]) == ("Doc C", 10, 20)
    assert (c["chunk_start_char"], c["chunk_end_char"]) == (100, 200)
    assert (c["section_title"], c["section_type"], c["page_number"]) == ("C section", "c-type", 2)
    d = into_d["metadata"]
    assert (d["title"], d["line_start"], d["line_end"]) == ("Doc D", 40, 50)


def test_the_row_the_t3_client_returned_is_not_mutated() -> None:
    from nexus.catalog.catalog_spans import resolve_span_in_t3

    row = copy.deepcopy(_D_ROW)
    resolve_span_in_t3(SPAN, COLL, _T3(row), entry=C, catalog=_catalog())

    assert row == _D_ROW


def test_a_manifest_field_the_document_does_not_state_is_removed_never_borrowed() -> None:
    """C's manifest row has lines and no chars: the row's chars are D's, so they are dropped."""
    from nexus.catalog.catalog_spans import resolve_span_in_t3

    catalog = _Catalog({"1.1.1": [ManifestRow(position=0, chash=CHASH, line_start=10, line_end=20)]})
    meta = resolve_span_in_t3(SPAN, COLL, _T3(copy.deepcopy(_D_ROW)), entry=C, catalog=catalog)["metadata"]

    assert meta["line_start"] == 10
    assert "chunk_start_char" not in meta and "chunk_end_char" not in meta


def test_labels_of_a_legacy_manifest_row_stay_the_rows_fallback() -> None:
    """labels_written False means the NULL labels were never recorded: the row is the only source."""
    from nexus.catalog.catalog_spans import resolve_span_in_t3

    catalog = _Catalog({"1.1.1": [ManifestRow(position=0, chash=CHASH, line_start=10, line_end=20)]})
    meta = resolve_span_in_t3(SPAN, COLL, _T3(copy.deepcopy(_D_ROW)), entry=C, catalog=catalog)["metadata"]

    assert meta["section_title"] == "D section"


def test_a_document_whose_manifest_lacks_the_chash_keeps_the_row_span_and_its_own_title() -> None:
    """Fallback, stated: no manifest position for the cited document, so the row span stands
    (it may be the document's own: a manifest-less legacy document). The title is still the entry's."""
    from nexus.catalog.catalog_spans import resolve_span_in_t3

    catalog = _Catalog({"1.1.1": [ManifestRow(position=0, chash="f" * 64)]})
    meta = resolve_span_in_t3(SPAN, COLL, _T3(copy.deepcopy(_D_ROW)), entry=C, catalog=catalog)["metadata"]

    assert meta["title"] == "Doc C"
    assert (meta["line_start"], meta["line_end"]) == (40, 50)


def test_an_entry_with_no_title_does_not_speak_with_the_rows() -> None:
    from nexus.catalog.catalog_spans import resolve_span_in_t3

    meta = resolve_span_in_t3(
        SPAN, COLL, _T3(copy.deepcopy(_D_ROW)), entry=_entry("1.1.1", ""), catalog=_catalog(),
    )["metadata"]

    assert "title" not in meta


def test_a_manifest_read_failure_degrades_to_the_row_span_with_the_entrys_title() -> None:
    from nexus.catalog.catalog_spans import resolve_span_in_t3

    class _Broken:
        def get_manifest(self, _doc_id):
            raise RuntimeError("catalog unreachable")

    meta = resolve_span_in_t3(SPAN, COLL, _T3(copy.deepcopy(_D_ROW)), entry=C, catalog=_Broken())["metadata"]

    assert meta["title"] == "Doc C"
    assert meta["line_start"] == 40


def test_without_an_owner_the_row_is_returned_as_before() -> None:
    from nexus.catalog.catalog_spans import resolve_span_in_t3

    assert resolve_span_in_t3(SPAN, COLL, _T3(copy.deepcopy(_D_ROW)))["metadata"] == _D_ROW


def test_resolve_span_text_for_entry_passes_the_owner_and_fetches_no_manifest() -> None:
    """The text-only caller names the cited document, so the title it would hand any metadata
    consumer is the document's own; it pays no manifest read for metadata nobody asked for."""
    from nexus.catalog import catalog_spans

    seen: dict = {}
    real = catalog_spans.resolve_span_in_t3

    def spy(span, coll, t3, **kw):
        seen.update(kw)
        return real(span, coll, t3, **kw)

    catalog = _catalog()
    with patch("nexus.db.make_t3", return_value=_T3(copy.deepcopy(_D_ROW))), patch.object(
        catalog_spans, "resolve_span_in_t3", spy,
    ):
        text = catalog_spans.resolve_span_text_for_entry(C, SPAN, catalog=catalog)

    assert text == "shared text"
    assert seen.get("entry") is C
    assert catalog.manifest_calls == []
