# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.17 (RDR-236 Phase 2, census row 9): the contradiction flag reads each OWNER's
``source_agent`` from its catalog entry's meta, never the shared chunk row's (the last writer's).

Storage-free on purpose: T3 and the catalog are stubbed, so no test here mints a tenant.
"""
from __future__ import annotations

import numpy as np

from nexus.catalog.tumbler import Tumbler
from nexus.catalog.types import CatalogEntry
from nexus.types import SearchResult

COLL = "knowledge__notes"
SHARED = "a" * 64  # one chunk, held by notes N1 (agent X) and N2 (agent Y); Y wrote last
OTHER = "b" * 64  # a near chunk held by note N3 (agent Y)


def _entry(tumbler: str, agent: str | None, collection: str = COLL) -> CatalogEntry:
    return CatalogEntry(
        tumbler=Tumbler.parse(tumbler), title=f"note {tumbler}", author="", year=0, content_type="prose",
        file_path="", corpus="", physical_collection=collection, chunk_count=1, head_hash="",
        indexed_at="2026-09-01T00:00:00+00:00", meta={"source_agent": agent} if agent else {},
    )


class _Catalog:
    def __init__(self, entries: list[CatalogEntry], owners: dict[str, list[str]]) -> None:
        self.entries = {str(e.tumbler): e for e in entries}
        self.owners = owners
        self.resolve_calls: list[list[str]] = []

    def resolve_many(self, doc_ids):
        self.resolve_calls.append(list(doc_ids))
        return {d: self.entries[d] for d in doc_ids if d in self.entries}

    def chash_positions(self, chashes):
        return [
            {"chash": c, "doc_id": d, "position": 0, "chunk_count": 1, "collection": COLL}
            for c in chashes for d in self.owners.get(c, [])
        ]


class _Vectors:
    """``get_embeddings`` stub: every row is a near neighbour of every other."""

    def get_embeddings(self, collection, ids):
        return np.array([[1.0, 0.0, 0.0] for _ in ids], dtype=np.float32)


def _hit(chash: str, row_agent: str) -> SearchResult:
    return SearchResult(
        id=chash, content="", distance=0.1, collection=COLL,
        metadata={"chunk_text_hash": chash, "source_agent": row_agent},
    )


def _page(catalog: _Catalog, *hits: SearchResult) -> list[SearchResult]:
    from nexus.search_engine import _attach_display_paths, _attach_doc_ids_from_catalog

    rows = list(hits)
    _attach_doc_ids_from_catalog(rows, catalog)
    _attach_display_paths(rows, catalog)
    return rows


def test_a_shared_chunk_is_attributed_to_each_notes_own_agent() -> None:
    """Failing-first pin (row 9): the row says Y (last writer); the owners say X and Y."""
    from nexus.search_engine import _row_agents

    catalog = _Catalog(
        [_entry("1.1.1", "agent-x"), _entry("1.1.2", "agent-y")], {SHARED: ["1.1.1", "1.1.2"]},
    )
    (shared,) = _page(catalog, _hit(SHARED, "agent-y"))

    assert _row_agents(shared) == {"agent-x", "agent-y"}


def test_the_flag_names_x_against_a_y_neighbour_that_the_row_alone_would_miss() -> None:
    """Notes X and Y share a chunk (row: Y); a neighbouring chunk is Y's. Read from the row the
    pair is Y-Y and nothing is flagged; X's note is in the page and disagrees with Y's."""
    from nexus.search_engine import flag_displayed_contradictions

    catalog = _Catalog(
        [_entry("1.1.1", "agent-x"), _entry("1.1.2", "agent-y"), _entry("1.1.3", "agent-y")],
        {SHARED: ["1.1.1", "1.1.2"], OTHER: ["1.1.3"]},
    )
    page = _page(catalog, _hit(SHARED, "agent-y"), _hit(OTHER, "agent-y"))

    out = flag_displayed_contradictions(page, _Vectors())

    assert [bool(r.metadata.get("_contradiction_flag")) for r in out] == [True, True]


def test_two_chunks_with_one_owner_agent_each_are_not_flagged() -> None:
    from nexus.search_engine import flag_displayed_contradictions

    catalog = _Catalog(
        [_entry("1.1.1", "agent-y"), _entry("1.1.3", "agent-y")], {SHARED: ["1.1.1"], OTHER: ["1.1.3"]},
    )
    page = _page(catalog, _hit(SHARED, "agent-x"), _hit(OTHER, "agent-y"))

    out = flag_displayed_contradictions(page, _Vectors())

    # the row of SHARED says x (a stale last writer); its one owner says y
    assert not any(r.metadata.get("_contradiction_flag") for r in out)


def test_a_shared_chunk_alone_is_not_a_contradiction_with_itself() -> None:
    from nexus.search_engine import _contradiction_candidates

    catalog = _Catalog(
        [_entry("1.1.1", "agent-x"), _entry("1.1.2", "agent-y")], {SHARED: ["1.1.1", "1.1.2"]},
    )
    page = _page(catalog, _hit(SHARED, "agent-y"))

    assert _contradiction_candidates(page) == []


def test_a_legacy_single_owner_chunk_falls_back_to_its_row() -> None:
    from nexus.search_engine import _row_agents

    catalog = _Catalog([_entry("1.1.1", None)], {SHARED: ["1.1.1"]})
    (hit,) = _page(catalog, _hit(SHARED, "agent-x"))

    assert _row_agents(hit) == {"agent-x"}


def test_a_legacy_owner_of_a_shared_chunk_does_not_borrow_the_row() -> None:
    """X stamped meta, Y's note is legacy: the row's last writer (Y) is not attributed to it."""
    from nexus.search_engine import _row_agents

    catalog = _Catalog(
        [_entry("1.1.1", "agent-x"), _entry("1.1.2", None)], {SHARED: ["1.1.1", "1.1.2"]},
    )
    (hit,) = _page(catalog, _hit(SHARED, "agent-z"))

    assert _row_agents(hit) == {"agent-x"}


def test_a_shared_chunk_whose_owners_are_all_legacy_states_no_agent() -> None:
    from nexus.search_engine import _row_agents

    catalog = _Catalog([_entry("1.1.1", None), _entry("1.1.2", None)], {SHARED: ["1.1.1", "1.1.2"]})
    (hit,) = _page(catalog, _hit(SHARED, "agent-z"))

    assert _row_agents(hit) == set()


def test_an_owner_in_another_collection_is_not_this_hits_owner() -> None:
    from nexus.search_engine import _row_agents

    catalog = _Catalog(
        [_entry("1.1.1", "agent-x"), _entry("1.1.2", "agent-y", collection="knowledge__elsewhere")],
        {SHARED: ["1.1.1", "1.1.2"]},
    )
    (hit,) = _page(catalog, _hit(SHARED, "agent-y"))

    assert _row_agents(hit) == {"agent-x"}


def test_a_row_with_no_owner_data_keeps_its_own_agent() -> None:
    """No catalog, or a chunk the manifest does not name: the row is the only evidence."""
    from nexus.search_engine import _attach_display_paths, _row_agents

    hit = _hit(SHARED, "agent-y")
    _attach_display_paths([hit], None)

    assert _row_agents(hit) == {"agent-y"}


def test_the_page_resolves_owner_agents_with_no_extra_catalog_call() -> None:
    catalog = _Catalog(
        [_entry("1.1.1", "agent-x"), _entry("1.1.2", "agent-y"), _entry("1.1.3", "agent-y")],
        {SHARED: ["1.1.1", "1.1.2"], OTHER: ["1.1.3"]},
    )
    _page(catalog, _hit(SHARED, "agent-y"), _hit(OTHER, "agent-y"))

    assert len(catalog.resolve_calls) == 1


def test_the_owner_agents_stay_off_the_json_wire() -> None:
    import json

    from nexus.formatters import format_json

    catalog = _Catalog([_entry("1.1.1", "agent-x")], {SHARED: ["1.1.1"]})
    page = _page(catalog, _hit(SHARED, "agent-x"))

    assert "_owner_agents" in page[0].metadata
    assert "_owner_agents" not in json.dumps(json.loads(format_json(page)))


def test_every_owner_working_key_stays_off_the_json_wire() -> None:
    """Critique of Phase 2 (.12): ``_INTERNAL_METADATA_KEYS`` gained only the agents key, so
    ``nx search --json`` emitted ``_owner_bib``, ``_owner_quality`` and the rest. Every ``OWNER_*_KEY``
    working key the search stamps (and the inferred-owner flag) is internal, enumerated from
    ``context_annotations`` so a key added later is held to the same rule."""
    import json

    from nexus import context_annotations as ca
    from nexus.formatters import format_json

    keys = sorted({v for n, v in vars(ca).items() if n.startswith("OWNER_") and n.endswith("_KEY") and isinstance(v, str)})
    assert len(keys) >= 6, f"the census found only {keys}: the enumeration is vacuous"
    hit = SearchResult(id="h", content="", distance=0.1, collection=COLL,
                       metadata={"chunk_text_hash": SHARED, **{k: {"x": 1} for k in keys}})
    wire = json.loads(format_json([hit]))[0]
    assert [k for k in keys if k in wire] == []

