# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 (nexus-tr1rj.3): the ``Degraded:`` line of ``nx store get`` and the MCP ``store_get``.

Both print ``Extractor:`` as before and, after it, one ``Degraded:`` line per catalog document
holding the chunk whose ``meta['extraction_pages']`` has a page, from ONE helper
(``nexus.catalog.store_hook.degraded_pages_lines``). The catalog is a stub reader here; the
engine-backed half (a real degraded document, ``nx store get`` over it) is
``tests/integration/test_rdr237_catalog_extraction.py``.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import nexus.catalog.factory as catalog_factory
import nexus.catalog.store_hook as store_hook
import nexus.commands.store as store_cmd
import nexus.mcp_infra as mcp_infra
from nexus.cli import main
from nexus.corpus import t3_collection_name
from nexus.mcp import core as mcp_core
from nexus.pdf_extractor import _DEGRADED_METHODS

CHASH = "d" * 64
NOTE_CHASH = "e" * 64


class _StubT3:
    """One live collection holding a PDF chunk (it has an ``extraction_method``) and a note."""

    def __init__(self) -> None:
        self.name = t3_collection_name("art")
        self.rows = [{"name": self.name, "content_type": "knowledge", "owner_id": "art",
                      "embedding_model": "local-model", "lifecycle_state": "live", "count": 2}]
        self.docs = {
            CHASH: {"id": CHASH, "title": "Some Paper", "content": "pdf chunk text",
                    "extraction_method": "mineru+degraded"},
            NOTE_CHASH: {"id": NOTE_CHASH, "title": "A note", "content": "note text"},
        }

    def collection_exists(self, name: str) -> bool:
        return name == self.name

    def list_collections(self) -> list[dict]:
        return [dict(r) for r in self.rows]

    def collection_info(self, name: str) -> dict:
        return {"count": len(self.docs), "metadata": {}}

    def get_by_id(self, name: str, doc_id: str) -> dict | None:
        return dict(self.docs[doc_id]) if name == self.name and doc_id in self.docs else None

    def find_ids_by_title(self, name: str, title: str) -> list[str]:
        return []


class _StubReader:
    """``resolve_many`` over ``{tumbler: meta}``; counts the lookups so a test can see them."""

    def __init__(self, metas: dict[str, dict]) -> None:
        self.metas = metas
        self.resolved: list[list[str]] = []
        self.closed = False

    def resolve_many(self, tumblers: list[str]) -> dict:
        self.resolved.append(list(tumblers))
        return {t: SimpleNamespace(meta=self.metas[t]) for t in tumblers if t in self.metas}

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def world(monkeypatch):
    """A stub T3 and a catalog whose holders and metas a test sets."""
    t3 = _StubT3()
    state = SimpleNamespace(holders={}, metas={}, reader=None, lookups=0)

    def live_holders(collection, chashes):
        state.lookups += 1
        return {c: list(state.holders[c]) for c in chashes if c in state.holders}

    def make_reader():
        state.reader = _StubReader(state.metas)
        return state.reader

    monkeypatch.setattr(mcp_infra, "_collections_cache", mcp_infra._collections_cache)
    mcp_infra.prime_collections_cache(t3.rows)
    monkeypatch.setattr(store_cmd, "_t3", lambda **_: t3)
    monkeypatch.setattr(mcp_core, "_get_t3", lambda: t3)
    monkeypatch.setattr(store_hook, "split_note_text", lambda *a, **k: None)
    monkeypatch.setattr(store_cmd, "_split_note_text", lambda *a, **k: None)
    monkeypatch.setattr(store_hook, "catalog_chashes_for_title", lambda *a, **k: [])
    monkeypatch.setattr(store_hook, "live_holders_of_chashes", live_holders)
    monkeypatch.setattr(catalog_factory, "make_catalog_reader", make_reader)
    state.t3 = t3
    yield state
    mcp_infra.invalidate_collections_cache()


def _cli_get(*extra: str):
    return CliRunner().invoke(main, ["store", "get", CHASH, "--collection", "art", *extra], catch_exceptions=False)


def _mcp_get(doc_id: str = CHASH) -> str:
    return mcp_core.store_get(doc_id=doc_id, collection="art")


DEGRADED = {"mineru-no-formula": [3, 7], "pymupdf-text": [12]}
LINE = "Degraded:   mineru-no-formula 3,7; pymupdf-text 12"


# ── the helper's text ────────────────────────────────────────────────────────


def test_the_rung_display_order_is_the_ladders_rung_order() -> None:
    """``store_hook`` cannot import the extractor stack, so it keeps its own copy of the rung order;
    a rung added to the ladder without it would sort after the known ones, silently."""
    assert store_hook._DEGRADED_RUNG_ORDER == _DEGRADED_METHODS


def test_the_pages_text_lists_rungs_in_ladder_order_with_sorted_pages() -> None:
    # Keys arrive in jsonb order (shortest first), pages possibly unsorted: the text is stable.
    assert store_hook.format_degraded_pages(
        {"unextracted": [9], "pymupdf-text": [12], "mineru-no-formula": [3, 7]},
    ) == "mineru-no-formula 3,7; pymupdf-text 12; unextracted 9"
    assert store_hook.format_degraded_pages({"zz-new-rung": [1], "pymupdf-text": [2]}) == "pymupdf-text 2; zz-new-rung 1"


# ── the line, in both surfaces ───────────────────────────────────────────────


def test_a_degraded_document_prints_the_line_after_extractor_in_both_surfaces(world) -> None:
    world.holders = {CHASH: [("1.1.1", "Some Paper")]}
    world.metas = {"1.1.1": {"extraction_method": "mineru+degraded", "extraction_pages": DEGRADED}}

    cli = _cli_get().output.splitlines()
    mcp = _mcp_get().splitlines()
    for lines in (cli, mcp):
        at = lines.index("Extractor:  mineru+degraded")
        assert lines[at + 1] == LINE, lines


def test_a_clean_document_prints_no_degraded_line(world) -> None:
    world.holders = {CHASH: [("1.1.1", "Some Paper")]}
    world.metas = {"1.1.1": {"extraction_method": "mineru", "extraction_pages": {}}}
    assert "Degraded:" not in _cli_get().output
    assert "Degraded:" not in _mcp_get()


def test_a_document_that_never_recorded_a_map_prints_no_line(world) -> None:
    """Documents indexed before the field existed (``docling``, ``mineru+docling-degraded``) have
    no ``extraction_pages``: they stay readable and print only ``Extractor:``."""
    world.t3.docs[CHASH]["extraction_method"] = "mineru+docling-degraded"
    world.holders = {CHASH: [("1.1.1", "Some Paper")]}
    world.metas = {"1.1.1": {}}
    for out in (_cli_get().output, _mcp_get()):
        assert "Extractor:  mineru+docling-degraded" in out
        assert "Degraded:" not in out


def test_a_chunk_held_by_two_documents_prints_one_labelled_line_each(world) -> None:
    world.holders = {CHASH: [("1.1.1", "Paper A"), ("1.1.2", "Paper B"), ("1.1.3", "Paper C")]}
    world.metas = {
        "1.1.1": {"extraction_pages": {"mineru-no-formula": [3]}},
        "1.1.2": {"extraction_pages": {}},
        "1.1.3": {"extraction_pages": {"unextracted": [1, 2]}},
    }
    expected = ["Degraded:   Paper A: mineru-no-formula 3", "Degraded:   Paper C: unextracted 1,2"]
    for out in (_cli_get().output, _mcp_get()):
        assert [x for x in out.splitlines() if x.startswith("Degraded:")] == expected


def test_the_mcp_reuses_the_holders_it_already_looked_up(world) -> None:
    world.holders = {CHASH: [("1.1.1", "Some Paper")]}
    world.metas = {"1.1.1": {"extraction_pages": DEGRADED}}
    _mcp_get()
    assert world.lookups == 1, "one chunk-to-document lookup for the title and the Degraded line together"


def test_a_note_chunk_makes_no_catalog_lookup_for_the_line(world) -> None:
    """A chunk without an ``extraction_method`` is not a PDF chunk: no map to look for."""
    out = _mcp_get(NOTE_CHASH)
    assert "Degraded:" not in out
    assert world.reader is None, "no catalog reader was opened for the degraded line"


def test_a_catalog_error_leaves_get_working_in_both_surfaces(world, monkeypatch) -> None:
    world.holders = {CHASH: [("1.1.1", "Some Paper")]}

    def boom():
        raise ConnectionError("catalog down")

    monkeypatch.setattr(catalog_factory, "make_catalog_reader", boom)
    cli = _cli_get()
    assert cli.exit_code == 0 and "Extractor:  mineru+degraded" in cli.output and "pdf chunk text" in cli.output
    mcp = _mcp_get()
    assert "Extractor:  mineru+degraded" in mcp and "pdf chunk text" in mcp
    assert "Degraded:" not in cli.output + mcp


def test_a_missing_catalog_leaves_get_working(world, monkeypatch) -> None:
    monkeypatch.setattr(catalog_factory, "make_catalog_reader", lambda: None)
    world.holders = {CHASH: [("1.1.1", "Some Paper")]}
    assert "pdf chunk text" in _cli_get().output
    assert "pdf chunk text" in _mcp_get()


def test_a_malformed_map_reads_as_clean(world) -> None:
    world.holders = {CHASH: [("1.1.1", "Some Paper")]}
    for bad in ("not json", ["a"], {"mineru-no-formula": "3"}, {"mineru-no-formula": [], "x": ["a"]}):
        world.metas = {"1.1.1": {"extraction_pages": bad}}
        assert "Degraded:" not in _mcp_get(), bad


def test_the_map_stored_as_a_json_string_is_read(world) -> None:
    world.holders = {CHASH: [("1.1.1", "Some Paper")]}
    world.metas = {"1.1.1": {"extraction_pages": json.dumps(DEGRADED)}}
    assert LINE in _mcp_get()


# ── --json ───────────────────────────────────────────────────────────────────


def test_json_carries_degraded_pages_only_when_some_document_has_any(world) -> None:
    world.holders = {CHASH: [("1.1.1", "Some Paper")]}
    world.metas = {"1.1.1": {"extraction_pages": DEGRADED}}
    out = json.loads(_cli_get("--json").output)
    assert out["degraded_pages"] == [
        {"tumbler": "1.1.1", "title": "Some Paper", "extraction_pages": DEGRADED},
    ]

    world.metas = {"1.1.1": {"extraction_pages": {}}}
    assert "degraded_pages" not in json.loads(_cli_get("--json").output), "a clean chunk's JSON is unchanged"

    world.reader, world.lookups = None, 0
    out = json.loads(CliRunner().invoke(main, ["store", "get", NOTE_CHASH, "--collection", "art", "--json"]).output)
    assert "degraded_pages" not in out
    assert world.reader is None and world.lookups == 0, "a note makes no catalog lookup"


def test_unsorted_pages_print_sorted(world) -> None:
    world.holders = {CHASH: [("1.1.1", "Some Paper")]}
    world.metas = {"1.1.1": {"extraction_pages": {"mineru-no-formula": [7, 3, 5]}}}
    assert "Degraded:   mineru-no-formula 3,5,7" in _mcp_get()


def test_a_chunk_nobody_holds_prints_no_line_and_looks_the_holders_up_once(world) -> None:
    """A ghost chunk (no live owner): the MCP's lookup found no holders, and that answer stands."""
    world.holders = {}
    world.metas = {"1.1.1": {"extraction_pages": DEGRADED}}
    out = _mcp_get()
    assert "Degraded:" not in out and "Extractor:  mineru+degraded" in out
    assert world.lookups == 1 and world.reader is None
