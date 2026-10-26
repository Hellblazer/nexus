# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.11: the provenance line on hydrated text, and a search hit's age and
expiry, name each document's own title, indexed date, year and expiry from the catalog
owner(s), never from the shared T3 chunk row (the last writer's).

Storage-free on purpose: T3 and the catalog are stubbed, so no test here mints a tenant.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from nexus.catalog.tumbler import Tumbler
from nexus.catalog.types import CatalogEntry, ManifestRow
from nexus.types import SearchResult

COLL = "knowledge__notes"
X = "a" * 64  # shared by documents A and B


def _entry(
    tumbler: str, title: str, indexed_at: str, *, year: int = 0, bib_year: int = 0,
    collection: str = COLL, meta: dict | None = None, file_path: str = "",
) -> CatalogEntry:
    return CatalogEntry(
        tumbler=Tumbler.parse(tumbler), title=title, author="", year=year, content_type="prose",
        file_path=file_path, corpus="", physical_collection=collection, chunk_count=1,
        head_hash="", indexed_at=indexed_at, bib_year=bib_year, meta=meta or {},
    )


def _position(chash: str, doc: str, collection: str = COLL) -> dict:
    return {"chash": chash, "doc_id": doc, "position": 0, "chunk_count": 1, "collection": collection}


class _Catalog:
    """The catalog reads the hydration paths make, with a call log."""

    def __init__(self, entries: list[CatalogEntry], positions: list[dict], manifests: dict | None = None):
        self.entries = {str(e.tumbler): e for e in entries}
        self.positions = positions
        self.manifests = manifests or {}
        self.resolve_calls: list[list[str]] = []
        self.position_calls: list[list[str]] = []

    def get_manifests(self, doc_ids):
        return {d: self.manifests.get(d, []) for d in doc_ids}

    def resolve_many(self, doc_ids):
        self.resolve_calls.append(list(doc_ids))
        return {d: self.entries[d] for d in doc_ids if d in self.entries}

    def chash_positions(self, chashes):
        self.position_calls.append(list(chashes))
        return [p for p in self.positions if p["chash"] in chashes]


def _t3(rows: dict[str, dict]):
    """A T3 handle whose batched ``get`` serves *rows* (chash -> content + metadata)."""

    class _Stub:
        def get(self, ids=None, **_kw):
            found = [i for i in (ids or []) if i in rows]
            return {
                "ids": found,
                "documents": [rows[i].get("content", "") for i in found],
                "metadatas": [{k: v for k, v in rows[i].items() if k != "content"} for i in found],
            }

    t3 = MagicMock()
    t3.get_or_create_collection = lambda _name: _Stub()
    return t3


def _hydrate(catalog, t3, ids, *, collections=COLL):
    from nexus.mcp.core import store_get_many

    with patch("nexus.mcp.core._get_t3", return_value=t3), patch(
        "nexus.mcp.core._get_catalog", return_value=catalog,
    ), patch("nexus.mcp.core._read_scope", return_value=[COLL]):
        return store_get_many(ids=ids, collections=collections, structured=True)


# The chunk row is the LAST WRITER's: document B's title, date, year, and a ttl.
_B_ROW = {
    "content": "shared text", "title": "Doc B", "indexed_at": "2026-09-05T00:00:00+00:00",
    "bib_year": 2001, "ttl_days": 3,
}


def test_a_chunk_shared_by_two_documents_gets_each_documents_own_note_under_its_tumbler() -> None:
    """Failing-first pin (row 1): hydrated under each document's tumbler, the note names that
    document's title, indexed date and year, where the chunk row names B for both."""
    from nexus.plans.runner import _hydrate_tumbler_ids

    a = _entry("1.1.1", "Doc A", "2026-08-01T00:00:00+00:00", year=1999)
    b = _entry("1.1.2", "Doc B", "2026-09-05T00:00:00+00:00", bib_year=2001)
    manifests = {
        "1.1.1": [ManifestRow(position=0, chash=X, collection=COLL)],
        "1.1.2": [ManifestRow(position=0, chash=X, collection=COLL)],
    }
    catalog = _Catalog([a, b], [_position(X, "1.1.1"), _position(X, "1.1.2")], manifests)

    with patch("nexus.mcp_infra.get_catalog", return_value=catalog), patch(
        "nexus.mcp.core._get_t3", return_value=_t3({X: _B_ROW}),
    ), patch("nexus.mcp.core._get_catalog", return_value=catalog), patch(
        "nexus.mcp.core._read_scope", return_value=[COLL],
    ):
        out = _hydrate_tumbler_ids(["1.1.1", "1.1.2"])

    note_a, body_a = out["contents"][0].split("\n", 1)
    note_b, body_b = out["contents"][1].split("\n", 1)
    assert body_a == body_b == "shared text"
    assert note_a.startswith(f"[source: Doc A · {COLL} · indexed 2026-08-01")
    assert "published 1999" in note_a
    assert "Doc B" not in note_a and "2026-09-05" not in note_a and "2001" not in note_a
    assert note_b.startswith(f"[source: Doc B · {COLL} · indexed 2026-09-05")
    assert "published 2001" in note_b
    assert "Doc A" not in note_b and "2026-08-01" not in note_b and "1999" not in note_b
    # The runner's own resolve is the only one: the chunk-level owner lookup behind
    # ``source_notes`` is never asked for here, so the documents are not resolved twice.
    assert catalog.resolve_calls == [["1.1.1", "1.1.2"]], catalog.resolve_calls


def test_owners_that_disagree_are_named_by_title_and_their_dates_are_left_out() -> None:
    """Failing-first pin 2: a bare chunk id (no document) held by two documents that disagree
    gets a note naming both owners' titles and the collection, never the row's last-writer
    title or either owner's date."""
    a = _entry("1.1.1", "Doc A", "2026-08-01T00:00:00+00:00", year=1999)
    b = _entry("1.1.2", "Doc B", "2026-09-05T00:00:00+00:00", bib_year=2001)
    catalog = _Catalog([a, b], [_position(X, "1.1.1"), _position(X, "1.1.2")])

    out = _hydrate(catalog, _t3({X: _B_ROW}), [X])

    assert out["source_notes"] == [f'"Doc A", "Doc B" · {COLL}']


def test_owner_titles_in_a_note_are_capped_at_three_with_a_count() -> None:
    entries = [_entry(f"1.1.{i}", f"Doc {i}", f"2026-09-0{i}T00:00:00+00:00") for i in range(1, 5)]
    catalog = _Catalog(entries, [_position(X, f"1.1.{i}") for i in range(1, 5)])

    out = _hydrate(catalog, _t3({X: _B_ROW}), [X])

    assert out["source_notes"] == [f'"Doc 1", "Doc 2", "Doc 3" (+1 more) · {COLL}']


def test_an_untitled_owner_is_counted_in_the_note_not_dropped() -> None:
    """Owners {titled A, untitled B}: the note must not read as the single document A."""
    a = _entry("1.1.1", "Doc A", "2026-08-01T00:00:00+00:00")
    b = _entry("1.1.2", "", "2026-09-05T00:00:00+00:00")
    catalog = _Catalog([a, b], [_position(X, "1.1.1"), _position(X, "1.1.2")])

    out = _hydrate(catalog, _t3({X: _B_ROW}), [X])

    assert out["source_notes"] == [f'"Doc A" (1 untitled) · {COLL}']


def test_each_field_is_kept_only_when_every_owner_gives_the_same_value() -> None:
    """Title and year agree, indexed date differs: the note keeps the first two and drops the
    date; an expiry needs the indexed stamp, so it is dropped with it."""
    a = _entry("1.1.1", "Same", "2026-08-01T00:00:00+00:00", bib_year=2020, meta={"ttl_days": 30})
    b = _entry("1.1.2", "Same", "2026-09-05T00:00:00+00:00", bib_year=2020, meta={"ttl_days": 30})
    catalog = _Catalog([a, b], [_position(X, "1.1.1"), _position(X, "1.1.2")])

    note = _hydrate(catalog, _t3({X: _B_ROW}), [X])["source_notes"][0]

    assert note == f"Same · {COLL} · published 2020"


def test_one_owner_supplies_title_date_year_and_expiry_from_the_catalog() -> None:
    c = "c" * 64
    # A ten-year ttl from 2026-09-01 is still running until 2036, whatever the clock says.
    only = _entry("1.1.1", "Only Doc", "2026-09-01T00:00:00+00:00", bib_year=2024, meta={"ttl_days": 3650})
    catalog = _Catalog([only], [_position(c, "1.1.1")])
    row = {"content": "body", "title": "Stale Row Title", "indexed_at": "2020-01-01T00:00:00+00:00",
           "bib_year": 1990, "ttl_days": 0}

    note = _hydrate(catalog, _t3({c: row}), [c])["source_notes"][0]

    assert note.startswith(f"Only Doc · {COLL} · indexed 2026-09-01")
    assert "published 2024" in note
    assert any(t.startswith("expires in ") for t in note.split(" · ")), note
    assert "Stale Row Title" not in note and "2020-01-01" not in note and "1990" not in note


def test_a_lapsed_owner_ttl_reads_expired() -> None:
    c = "c" * 64
    lapsed = _entry("1.1.1", "Old Doc", "2026-01-01T00:00:00+00:00", meta={"ttl_days": 3})
    catalog = _Catalog([lapsed], [_position(c, "1.1.1")])

    note = _hydrate(catalog, _t3({c: {"content": "body"}}), [c])["source_notes"][0]

    assert note.split(" · ")[-1] == "expired", note


def test_no_resolvable_owner_means_no_title_date_year_or_expiry_from_the_row() -> None:
    """A chunk no live document owns (no manifest row), a non-chash id, and a catalog that
    cannot answer all get the collection only; the row's values are never a fallback."""
    ghost, plain = "d" * 64, "doc-1"
    row = {"content": "body", "title": "Row Title", "indexed_at": "2026-09-05T00:00:00+00:00",
           "bib_year": 2001, "ttl_days": 3}
    catalog = _Catalog([], [])
    out = _hydrate(catalog, _t3({ghost: row, plain: row}), [ghost, plain])
    assert out["source_notes"] == [COLL, COLL]

    out = _hydrate(None, _t3({ghost: row}), [ghost])
    assert out["source_notes"] == [COLL]

    class _Broken(_Catalog):
        def chash_positions(self, chashes):
            raise RuntimeError("engine down")

    out = _hydrate(_Broken([], []), _t3({ghost: row}), [ghost])
    assert out["source_notes"] == [COLL]


def test_an_owner_in_another_collection_is_not_named() -> None:
    home = _entry("1.1.1", "Home Note", "2026-09-01T00:00:00+00:00")
    elsewhere = _entry("1.1.9", "Elsewhere", "2026-01-01T00:00:00+00:00", collection="knowledge__other")
    catalog = _Catalog([home, elsewhere], [_position(X, "1.1.1"), _position(X, "1.1.9", "knowledge__other")])

    note = _hydrate(catalog, _t3({X: _B_ROW}), [X])["source_notes"][0]

    assert note.startswith(f"Home Note · {COLL} · indexed 2026-09-01")
    assert "Elsewhere" not in note


def test_an_other_collection_owner_the_catalog_did_not_resolve_does_not_silence_the_note() -> None:
    """The manifest also names an owner in another collection, and ``resolve_many`` returns
    nothing for it. That owner is not an unheard owner of THIS chunk: the row-level collection
    filter drops it before the unresolved-owner rule looks (the later ``physical_collection``
    filter cannot, as the entry is absent)."""
    home = _entry("1.1.1", "Home Note", "2026-09-01T00:00:00+00:00")
    catalog = _Catalog([home], [_position(X, "1.1.1"), _position(X, "1.1.9", "knowledge__other")])

    note = _hydrate(catalog, _t3({X: _B_ROW}), [X])["source_notes"][0]

    assert note.startswith(f"Home Note · {COLL} · indexed 2026-09-01")


def test_an_eleventh_owner_in_another_collection_does_not_trip_the_hydration_cap() -> None:
    """The ten-owner cap counts the owners in the chunk's own collection: ten here plus one
    in another collection is still ten."""
    from nexus.context_annotations import OWNER_RESOLVE_CAP

    n = OWNER_RESOLVE_CAP
    entries = [_entry(f"1.1.{i}", "Same", "2026-09-01T00:00:00+00:00") for i in range(1, n + 1)]
    positions = [_position(X, f"1.1.{i}") for i in range(1, n + 1)]
    positions.append(_position(X, "1.1.99", "knowledge__other"))
    catalog = _Catalog(entries, positions)

    note = _hydrate(catalog, _t3({X: _B_ROW}), [X])["source_notes"][0]

    assert note.startswith(f"Same · {COLL} · indexed 2026-09-01")


def test_owner_reads_are_batched_once_per_hydration_not_per_chunk() -> None:
    chashes = [format(i, "x").rjust(64, "0") for i in range(1, 9)]
    entries = [_entry(f"1.1.{i}", f"Doc {i}", "2026-09-01T00:00:00+00:00") for i in range(1, 9)]
    positions = [_position(c, f"1.1.{i}") for i, c in enumerate(chashes, start=1)]
    catalog = _Catalog(entries, positions)
    rows = {c: {"content": f"t{i}"} for i, c in enumerate(chashes)}

    out = _hydrate(catalog, _t3(rows), chashes)

    assert [n.split(" · ")[0] for n in out["source_notes"]] == [f"Doc {i}" for i in range(1, 9)]
    assert len(catalog.position_calls) == 1, "chash_positions ran per chunk"
    assert len(catalog.resolve_calls) == 1, "resolve_many ran per chunk"


def test_an_owner_the_manifest_names_but_resolve_did_not_return_states_nothing() -> None:
    """Owners A and B hold the chunk; resolve_many answers for A only. A's title and date must
    not stand for the chunk: B could disagree and went unheard."""
    a = _entry("1.1.1", "Doc A", "2026-08-01T00:00:00+00:00", year=1999)
    catalog = _Catalog([a], [_position(X, "1.1.1"), _position(X, "1.1.2")])

    out = _hydrate(catalog, _t3({X: _B_ROW}), [X])

    assert out["source_notes"] == [COLL]


def test_the_hydration_owner_cap_is_ten_documents() -> None:
    from nexus.context_annotations import OWNER_RESOLVE_CAP

    def note_for(n: int) -> str:
        entries = [_entry(f"1.1.{i}", "Same", "2026-09-01T00:00:00+00:00") for i in range(1, n + 1)]
        catalog = _Catalog(entries, [_position(X, f"1.1.{i}") for i in range(1, n + 1)])
        return _hydrate(catalog, _t3({X: _B_ROW}), [X])["source_notes"][0]

    assert note_for(OWNER_RESOLVE_CAP).startswith(f"Same · {COLL} · indexed 2026-09-01")
    assert note_for(OWNER_RESOLVE_CAP + 1) == COLL


def test_a_failing_or_malformed_resolve_many_degrades_to_the_collection_only() -> None:
    a = _entry("1.1.1", "Doc A", "2026-08-01T00:00:00+00:00")

    class _Raises(_Catalog):
        def resolve_many(self, doc_ids):
            raise RuntimeError("engine down")

    class _NotADict(_Catalog):
        def resolve_many(self, doc_ids):
            return [a]

    for cls in (_Raises, _NotADict):
        out = _hydrate(cls([a], [_position(X, "1.1.1")]), _t3({X: _B_ROW}), [X])
        assert out["source_notes"] == [COLL], cls.__name__


def test_without_source_notes_skips_the_owner_resolve_but_keeps_the_section_read() -> None:
    from nexus.mcp import core

    a = _entry("1.1.1", "Doc A", "2026-08-01T00:00:00+00:00")
    catalog = _Catalog([a], [_position(X, "1.1.1")])

    with core.without_source_notes():
        out = _hydrate(catalog, _t3({X: _B_ROW}), [X])
    assert out["source_notes"] == [""]
    assert out["contents"] == ["shared text"]
    assert catalog.resolve_calls == []
    assert len(catalog.position_calls) == 1  # the section-type read still asks

    # the switch is scoped to the block
    assert _hydrate(catalog, _t3({X: _B_ROW}), [X])["source_notes"][0].startswith("Doc A")


def test_the_tidy_prefetch_does_not_pay_for_notes() -> None:
    from nexus.mcp import core

    a = _entry("1.1.1", "Doc A", "2026-08-01T00:00:00+00:00")
    catalog = _Catalog([a], [_position(X, "1.1.1")])
    hits = {"ids": [X], "chunk_collections": [COLL]}

    with patch("nexus.mcp.core.search", return_value=hits), patch(
        "nexus.mcp.core._get_t3", return_value=_t3({X: _B_ROW}),
    ), patch("nexus.mcp.core._get_catalog", return_value=catalog), patch(
        "nexus.mcp.core._read_scope", return_value=[COLL],
    ):
        block, n, reason, _genuine = core._tidy_prefetch("topic", COLL)

    assert n == 1 and "shared text" in block, (n, reason)
    assert catalog.resolve_calls == []


def test_the_human_mode_does_not_pay_for_notes() -> None:
    catalog = _Catalog([_entry("1.1.1", "Doc A", "2026-08-01T00:00:00+00:00")], [_position(X, "1.1.1")])
    from nexus.mcp.core import store_get_many

    with patch("nexus.mcp.core._get_t3", return_value=_t3({X: _B_ROW})), patch(
        "nexus.mcp.core._get_catalog", return_value=catalog,
    ), patch("nexus.mcp.core._read_scope", return_value=[COLL]):
        store_get_many(ids=[X], collections=COLL, structured=False)

    assert catalog.resolve_calls == [] and catalog.position_calls == []


# -- row 4: a search hit's own age and expiry --------------------------------------------


def _hit(chash: str, **meta) -> SearchResult:
    return SearchResult(
        id=chash, content="x", distance=0.1, collection=COLL,
        metadata={"chunk_text_hash": chash, **meta},
    )


def test_a_hits_age_line_reads_the_owners_not_the_row() -> None:
    """The row says indexed 2020, published 1990, expires; the one live owner says otherwise."""
    from nexus.context_annotations import hit_annotation_line
    from nexus.search_engine import _attach_display_paths, _attach_from_chash_positions

    only = _entry("1.1.1", "Only Doc", "2026-09-01T00:00:00+00:00", bib_year=2024)
    catalog = _Catalog([only], [_position(X, "1.1.1")])
    hit = _hit(X, indexed_at="2020-01-01T00:00:00+00:00", bib_year=1990, ttl_days=3)

    assert _attach_from_chash_positions([hit], [X], catalog)
    _attach_display_paths([hit], catalog)
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    assert hit_annotation_line(hit.metadata, now=now) == "indexed 2026-09-01 (7d ago) · published 2024"


def test_a_hit_whose_owners_disagree_prints_no_age_and_no_row_value() -> None:
    from nexus.context_annotations import hit_annotation_line
    from nexus.search_engine import _attach_display_paths, _attach_from_chash_positions

    a = _entry("1.1.1", "Doc A", "2026-08-01T00:00:00+00:00", bib_year=1999)
    b = _entry("1.1.2", "Doc B", "2026-09-05T00:00:00+00:00", bib_year=2001)
    catalog = _Catalog([a, b], [_position(X, "1.1.1"), _position(X, "1.1.2")])
    hit = _hit(X, indexed_at="2026-09-05T00:00:00+00:00", bib_year=2001, ttl_days=3)

    assert _attach_from_chash_positions([hit], [X], catalog)
    _attach_display_paths([hit], catalog)

    assert hit_annotation_line(hit.metadata) == ""


def test_a_hit_with_no_catalog_prints_no_age() -> None:
    from nexus.context_annotations import hit_annotation_line
    from nexus.search_engine import _attach_display_paths

    hit = _hit(X, indexed_at="2026-09-05T00:00:00+00:00", bib_year=2001)
    _attach_display_paths([hit], None)

    assert hit_annotation_line(hit.metadata) == ""


def test_the_page_resolves_its_owners_in_one_call_and_json_keeps_the_facts_internal() -> None:
    from nexus.formatters import format_json
    from nexus.search_engine import _attach_display_paths, _attach_from_chash_positions

    chashes = [format(i, "x").rjust(64, "0") for i in range(1, 6)]
    entries = [_entry(f"1.1.{i}", f"Doc {i}", "2026-09-01T00:00:00+00:00") for i in range(1, 6)]
    catalog = _Catalog(entries, [_position(c, f"1.1.{i}") for i, c in enumerate(chashes, start=1)])
    hits = [_hit(c) for c in chashes]

    assert _attach_from_chash_positions(hits, chashes, catalog)
    _attach_display_paths(hits, catalog)

    assert len(catalog.resolve_calls) == 1
    assert all(h.metadata.get("_owner_facts", {}).get("indexed_at") for h in hits)
    assert "_owner_facts" not in json.loads(format_json(hits))[0]


def test_a_chunk_with_more_owners_than_the_cap_is_not_annotated() -> None:
    """Past the owner cap the hit cannot say its owners agree, so it says nothing."""
    from nexus.context_annotations import hit_annotation_line
    from nexus.search_engine import _OWNER_TITLE_RESOLVE_CAP, _attach_display_paths, _attach_from_chash_positions

    n = _OWNER_TITLE_RESOLVE_CAP + 1
    entries = [_entry(f"1.1.{i}", "Same", "2026-09-01T00:00:00+00:00") for i in range(1, n + 1)]
    catalog = _Catalog(entries, [_position(X, f"1.1.{i}") for i in range(1, n + 1)])
    hit = _hit(X)

    assert _attach_from_chash_positions([hit], [X], catalog)
    _attach_display_paths([hit], catalog)

    assert hit_annotation_line(hit.metadata) == ""


def test_a_stub_entry_without_dates_yields_no_facts_not_an_error() -> None:
    from nexus.search_engine import _attach_display_paths, _attach_from_chash_positions

    class _Bare(_Catalog):
        def resolve_many(self, doc_ids):
            return {d: SimpleNamespace(title="t", file_path="", physical_collection=COLL) for d in doc_ids}

    catalog = _Bare([], [_position(X, "1.1.1")])
    hit = _hit(X)
    assert _attach_from_chash_positions([hit], [X], catalog)
    _attach_display_paths([hit], catalog)
    from nexus.context_annotations import hit_annotation_line

    assert hit_annotation_line(hit.metadata) == ""



def test_a_hit_with_an_owner_the_catalog_did_not_resolve_prints_no_age() -> None:
    """Owners A and B hold the chunk; the batch resolves A only. A's age must not stand for
    the hit (B could disagree), though A's title is still shown."""
    from nexus.context_annotations import hit_annotation_line
    from nexus.search_engine import _attach_display_paths, _attach_from_chash_positions

    a = _entry("1.1.1", "Doc A", "2026-08-01T00:00:00+00:00", bib_year=1999)
    catalog = _Catalog([a], [_position(X, "1.1.1"), _position(X, "1.1.2")])
    hit = _hit(X)

    assert _attach_from_chash_positions([hit], [X], catalog)
    _attach_display_paths([hit], catalog)

    assert hit_annotation_line(hit.metadata) == ""
    assert hit.metadata.get("_display_title") == "Doc A"


def test_an_owner_in_another_collection_does_not_silence_a_hit() -> None:
    """An owner that resolved to another collection is not an unheard owner: the hit's own
    collection's owner still states its age."""
    from nexus.context_annotations import hit_annotation_line
    from nexus.search_engine import _attach_display_paths, _attach_from_chash_positions

    home = _entry("1.1.1", "Home", "2026-09-01T00:00:00+00:00")
    elsewhere = _entry("1.1.9", "Elsewhere", "2026-01-01T00:00:00+00:00", collection="knowledge__other")
    catalog = _Catalog([home, elsewhere], [_position(X, "1.1.1"), _position(X, "1.1.9", "knowledge__other")])
    hit = _hit(X)

    assert _attach_from_chash_positions([hit], [X], catalog)
    _attach_display_paths([hit], catalog)

    assert hit_annotation_line(hit.metadata, now=datetime(2026, 9, 8, tzinfo=UTC)).startswith("indexed 2026-09-01")


def test_an_owner_with_a_home_but_no_facts_is_unheard_not_a_key_error() -> None:
    """A prefetched lookup whose maps hold an owner's home but not its facts (a lookup that
    stopped mid-loop) must not raise out of the search render."""
    from nexus.context_annotations import hit_annotation_line
    from nexus.search_engine import _attach_display_paths

    class _Prefetched:
        doc_ids = {"1.1.1", "1.1.2"}

        def maps(self):
            return (
                {}, {"1.1.1": "Doc A", "1.1.2": "Doc B"}, {"1.1.1": COLL, "1.1.2": COLL},
                {"1.1.1": {"indexed_at": "2026-08-01T00:00:00+00:00"}},
            )

    hit = _hit(X, doc_id="1.1.1")
    hit.metadata["_owner_doc_ids"] = ["1.1.1", "1.1.2"]

    _attach_display_paths([hit], object(), prefetched=_Prefetched())

    assert hit_annotation_line(hit.metadata) == ""


def test_a_hit_with_no_owner_list_falls_back_to_the_rows_legacy_doc_id() -> None:
    """No ``_owner_doc_ids`` (the lookup did not run): the row's own doc_id is the one owner, and
    its catalog entry (not the row's stamps) supplies the age."""
    from nexus.context_annotations import hit_annotation_line
    from nexus.search_engine import _attach_display_paths

    only = _entry("1.1.1", "Only Doc", "2026-09-01T00:00:00+00:00", bib_year=2024)
    catalog = _Catalog([only], [])
    hit = _hit(X, doc_id="1.1.1", indexed_at="2020-01-01T00:00:00+00:00", bib_year=1990)

    _attach_display_paths([hit], catalog)

    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    assert hit_annotation_line(hit.metadata, now=now) == "indexed 2026-09-01 (7d ago) · published 2024"
