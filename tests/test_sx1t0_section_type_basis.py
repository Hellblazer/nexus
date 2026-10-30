# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-sx1t0 (RDR-236 Phase 2): ``store_get_many(structured=True)`` says HOW each section type was decided.

``section_types[i] == ""`` is both an unstamped chunk and a chunk whose owning documents disagree on its
section type (RDR-189's "ambiguous"), so a caller could not tell them apart. The result gains an aligned
``section_type_basis`` list, an additive key: ``section_types`` keeps its meaning (every filter still
treats ``""`` as keep, and the runner still drops only an exact ``imports``).

The runner's tumbler hydration holds each document's own manifest label, so the owner section-type read
behind ``store_get_many`` was a ``chash_positions`` round trip it discarded; it is skipped when no row in
the batch is a legacy one.

Storage-free: T3 and the catalog are stubbed.
"""
from __future__ import annotations

from unittest.mock import patch

from nexus.catalog.types import ManifestRow

from tests.test_6pfgb11_provenance_from_catalog import COLL, X, _Catalog, _entry, _hydrate, _t3

Y = "b" * 64
Z = "c" * 64


def _owner(chash: str, doc: str, section_type: str | None, *, written: bool = True) -> dict:
    return {"chash": chash, "doc_id": doc, "position": 0, "chunk_count": 1, "collection": COLL,
            "section_type": section_type, "section_title": None, "page_number": None,
            "labels_written": written}


def _rows(**by_chash) -> dict[str, dict]:
    return {c: {"content": f"text {n}", **extra} for n, (c, extra) in enumerate(by_chash.items())}


def test_unstamped_ambiguous_and_agreed_chunks_are_told_apart() -> None:
    """The three-way pin. Both the unstamped and the ambiguous chunk read ``""`` in ``section_types``;
    the basis is what differs."""
    catalog = _Catalog(
        [],
        [
            # X: an unstamped chunk with no owner at all (a note, or no manifest row)
            # Y: two written owners that disagree
            _owner(Y, "1.1.1", "imports"), _owner(Y, "1.1.2", "method"),
            # Z: two written owners that agree
            _owner(Z, "1.1.1", "class"), _owner(Z, "1.1.2", "class"),
        ],
    )
    t3 = _t3({X: {"content": "x"}, Y: {"content": "y", "section_type": "imports"}, Z: {"content": "z"}})

    out = _hydrate(catalog, t3, [X, Y, Z])

    assert out["section_types"] == ["", "", "class"]
    assert out["section_type_basis"] == ["stored", "ambiguous", "agreed"]


def test_a_mix_of_written_and_legacy_owners_is_ambiguous_a_lone_legacy_owner_is_stored() -> None:
    catalog = _Catalog(
        [],
        [_owner(X, "1.1.1", "method"), _owner(X, "1.1.2", None, written=False),
         _owner(Y, "1.1.1", None, written=False)],
    )
    t3 = _t3({X: {"content": "x"}, Y: {"content": "y", "section_type": "imports"}})

    out = _hydrate(catalog, t3, [X, Y])

    assert out["section_types"] == ["", "imports"]
    assert out["section_type_basis"] == ["ambiguous", "stored"]


def test_agreed_on_an_empty_label_is_agreed_not_ambiguous() -> None:
    catalog = _Catalog([], [_owner(X, "1.1.1", ""), _owner(X, "1.1.2", "")])

    out = _hydrate(catalog, _t3({X: {"content": "x", "section_type": "references"}}), [X])

    assert (out["section_types"], out["section_type_basis"]) == ([""], ["agreed"])


def test_a_missing_id_is_missing_and_the_basis_aligns_with_the_ids() -> None:
    catalog = _Catalog([], [_owner(Y, "1.1.1", "class")])

    out = _hydrate(catalog, _t3({Y: {"content": "y"}}), [X, Y])

    assert out["missing"] == [X]
    assert out["section_type_basis"] == ["missing", "agreed"]


def test_the_key_is_additive_and_the_existing_ones_keep_their_meaning() -> None:
    catalog = _Catalog([], [])
    out = _hydrate(catalog, _t3({X: {"content": "x", "section_type": "imports"}}), [X])
    assert set(out) == {"contents", "missing", "section_types", "source_notes", "section_type_basis"}
    assert out["section_types"] == ["imports"], "no owner: the stored value, as before"


def test_a_full_hydration_dump_is_still_classified_as_a_hydration_dump() -> None:
    """The shape table matched ``{contents, missing, section_types}`` only, so a real structured
    result (which also carries source_notes) read as an answer; the new key must not widen that."""
    import json

    from nexus.plans.answer_shape import AnswerShape, classify_answer_shape

    out = _hydrate(_Catalog([], []), _t3({X: {"content": "x"}}), [X])
    assert classify_answer_shape(json.dumps(out)) is AnswerShape.HYDRATION_DUMP


# -- the tumbler hydration no longer pays for an owner read it discards ---------------------------

def _tumbler_hydrate(manifest_rows: dict[str, list[ManifestRow]], entries, positions, rows):
    from nexus.plans.runner import _hydrate_tumbler_ids

    catalog = _Catalog(entries, positions, manifest_rows)
    with patch("nexus.mcp_infra.get_catalog", return_value=catalog), patch(
        "nexus.mcp.core._get_t3", return_value=_t3(rows),
    ), patch("nexus.mcp.core._get_catalog", return_value=catalog), patch(
        "nexus.mcp.core._read_scope", return_value=[COLL],
    ):
        return catalog, _hydrate_tumbler_ids(list(manifest_rows))


def _labelled(chash: str, label: str) -> ManifestRow:
    return ManifestRow(position=0, chash=chash, collection=COLL, section_type=label, labels_written=True)


def _legacy(chash: str) -> ManifestRow:
    return ManifestRow(position=0, chash=chash, collection=COLL, labels_written=False)


def test_tumbler_hydration_of_labelled_documents_makes_no_chash_positions_call() -> None:
    """Pin: 0 ``chash_positions`` calls when every row carries its document's own label (it made one
    before, and threw the answer away)."""
    catalog, out = _tumbler_hydrate(
        {"1.1.1": [_labelled(X, "class")], "1.1.2": [_labelled(Y, "imports")]},
        [_entry("1.1.1", "A", "2026-09-01T00:00:00+00:00"), _entry("1.1.2", "B", "2026-09-01T00:00:00+00:00")],
        [_owner(X, "1.1.1", "class"), _owner(Y, "1.1.2", "imports")],
        {X: {"content": "kept"}, Y: {"content": "import line"}},
    )

    assert catalog.position_calls == []
    assert "kept" in out["contents"][0], "a labelled non-import chunk stays"
    assert out["contents"][1] == "" and out["missing"] == ["1.1.2"], "the document's own 'imports' label still drops it"


def test_tumbler_hydration_with_a_legacy_row_still_reads_the_owners_for_it() -> None:
    catalog, out = _tumbler_hydrate(
        {"1.1.1": [_legacy(X)]},
        [_entry("1.1.1", "A", "2026-09-01T00:00:00+00:00")],
        [_owner(X, "1.1.1", "imports", written=False)],
        {X: {"content": "legacy import", "section_type": "imports"}},
    )

    assert len(catalog.position_calls) == 1
    assert out["contents"][0] == "" and out["missing"] == ["1.1.1"], "the stored 'imports' still drops a legacy row"


def test_the_two_switches_make_store_get_many_skip_every_owner_lookup() -> None:
    from nexus.mcp import core

    catalog = _Catalog([], [_owner(X, "1.1.1", "class")])
    with core.without_source_notes(), core.without_owner_section_types():
        out = _hydrate(catalog, _t3({X: {"content": "x", "section_type": "imports"}}), [X])

    assert catalog.position_calls == [] and catalog.resolve_calls == []
    assert out["section_types"] == ["imports"] and out["section_type_basis"] == ["stored"]
    # scoped to the block: the next call asks again
    assert _hydrate(catalog, _t3({X: {"content": "x"}}), [X])["section_type_basis"] == ["agreed"]
