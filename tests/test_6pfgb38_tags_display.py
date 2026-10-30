# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.38 (RDR-236 RF-17): ``store_get`` / ``store_list`` show a note's tags from its
catalog entry's ``meta`` array, in the MCP tools and the ``nx store`` verbs alike.

A chunk row carries the LAST writer's tags, and the catalog entry is per note, so two notes sharing
a chunk each show their own. The row's comma string stays only as the fallback for a chunk whose
holders carry no ``tags`` key at all (an indexed file, a note stored before nexus-6pfgb.5). The
catalog is a stub reader here; the array itself, against the real engine, is
``tests/integration/test_6pfgb5_note_meta_homes.py``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import nexus.catalog.factory as catalog_factory
import nexus.catalog.store_hook as store_hook
import nexus.commands.store as store_cmd
import nexus.mcp_infra as mcp_infra
from nexus.catalog.store_hook import catalog_tags_for_chunks
from nexus.cli import main
from nexus.corpus import t3_collection_name
from nexus.mcp import core as mcp_core

H_A = "a" * 64
H_B = "b" * 64
H_FILE = "c" * 64


class _StubT3:
    def __init__(self) -> None:
        self.name = t3_collection_name("art")
        self.rows = [{"name": self.name, "content_type": "knowledge", "owner_id": "art",
                      "embedding_model": "local-model", "lifecycle_state": "live", "count": 3}]
        self.docs = {
            # the row holds the LAST writer's tags: "beta" even where the catalog says otherwise
            H_A: {"id": H_A, "title": "note-a", "content": "shared text", "tags": "beta,row-only"},
            H_B: {"id": H_B, "title": "note-b", "content": "b text", "tags": "legacy-row"},
            H_FILE: {"id": H_FILE, "title": "file", "content": "file text", "tags": "row-file"},
        }

    def collection_exists(self, name: str) -> bool:
        return name == self.name

    def list_collections(self) -> list[dict]:
        return [dict(r) for r in self.rows]

    def collection_info(self, name: str) -> dict:
        return {"count": len(self.docs), "metadata": {}}

    def list_store(self, name: str, limit: int = 20, offset: int = 0) -> list[dict]:
        return [dict(d) for d in list(self.docs.values())[offset:offset + limit]]

    def get_by_id(self, name: str, doc_id: str) -> dict | None:
        return dict(self.docs[doc_id]) if name == self.name and doc_id in self.docs else None

    def find_ids_by_title(self, name: str, title: str) -> list[str]:
        return []


class _StubReader:
    def __init__(self, owners: dict[str, list[str]], entries: dict[str, SimpleNamespace]) -> None:
        self.owners, self.entries = owners, entries
        self.closed = 0

    def docs_for_chashes(self, chashes):
        return {c: list(self.owners[c]) for c in chashes if c in self.owners}

    def resolve_many(self, tumblers):
        return {t: self.entries[t] for t in tumblers if t in self.entries}

    def close(self) -> None:
        self.closed += 1


def _entry(title: str, meta: dict, collection: str = "") -> SimpleNamespace:
    return SimpleNamespace(title=title, meta=meta, physical_collection=collection)


@pytest.fixture
def world(monkeypatch):
    t3 = _StubT3()
    state = SimpleNamespace(owners={}, entries={}, t3=t3, fail=False)

    def make_reader():
        if state.fail:
            raise RuntimeError("catalog down")
        return _StubReader(state.owners, state.entries)

    def live_holders(collection, chashes):
        return {
            c: [(t, state.entries[t].title) for t in state.owners[c] if t in state.entries]
            for c in chashes if c in state.owners
        }

    monkeypatch.setattr(mcp_infra, "_collections_cache", mcp_infra._collections_cache)
    mcp_infra.prime_collections_cache(t3.rows)
    monkeypatch.setattr(store_cmd, "_t3", lambda **_: t3)
    monkeypatch.setattr(mcp_core, "_get_t3", lambda: t3)
    monkeypatch.setattr(store_hook, "split_note_text", lambda *a, **k: None)
    monkeypatch.setattr(store_cmd, "_split_note_text", lambda *a, **k: None)
    monkeypatch.setattr(store_hook, "catalog_chashes_for_title", lambda *a, **k: [])
    monkeypatch.setattr(store_hook, "live_holders_of_chashes", live_holders)
    monkeypatch.setattr(catalog_factory, "make_catalog_reader", make_reader)
    state.owners = {H_A: ["1.1.1", "1.1.2"], H_B: ["1.1.3"], H_FILE: ["1.1.4"]}
    state.entries = {
        "1.1.1": _entry("note-a", {"tags": ["alpha", "one"]}),
        "1.1.2": _entry("note-a2", {"tags": ["alpha", "two"]}),
        "1.1.3": _entry("note-b", {"tags": []}),
        "1.1.4": _entry("file", {}),
    }
    yield state
    mcp_infra.invalidate_collections_cache()


def _mcp_get(doc_id: str) -> str:
    return mcp_core.store_get(doc_id=doc_id, collection="art")


def _cli_get(doc_id: str) -> str:
    return CliRunner().invoke(main, ["store", "get", doc_id, "--collection", "art"], catch_exceptions=False).output


def _tags_line(text: str) -> str | None:
    return next((ln for ln in text.splitlines() if ln.startswith("Tags:")), None)


# ── the helper ──────────────────────────────────────────────────────────────────────────────


def test_the_helper_unions_the_holders_arrays_in_order_and_skips_holders_without_the_key(world) -> None:
    got = catalog_tags_for_chunks(world.t3.name, [H_A, H_B, H_FILE])
    assert got == {H_A: ["alpha", "one", "two"], H_B: []}, "H_FILE's only holder has no tags key: row fallback"


def test_the_helper_reads_a_legacy_string_in_meta_as_its_array(world) -> None:
    world.entries["1.1.3"] = _entry("note-b", {"tags": "x, y,x"})
    assert catalog_tags_for_chunks(world.t3.name, [H_B]) == {H_B: ["x", "y"]}


def test_the_helper_leaves_out_a_holder_in_another_collection_and_filters_by_title(world) -> None:
    world.entries["1.1.2"] = _entry("note-a2", {"tags": ["alpha", "two"]}, collection="other__x__m__v1")
    assert catalog_tags_for_chunks(world.t3.name, [H_A]) == {H_A: ["alpha", "one"]}
    assert catalog_tags_for_chunks(world.t3.name, [H_A], title="note-a") == {H_A: ["alpha", "one"]}
    assert catalog_tags_for_chunks(world.t3.name, [H_A], title="nobody") == {}


def test_the_helper_is_best_effort(world) -> None:
    world.fail = True
    assert catalog_tags_for_chunks(world.t3.name, [H_A]) == {}
    assert catalog_tags_for_chunks(world.t3.name, []) == {}


# ── the four surfaces ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("get", [_mcp_get, _cli_get], ids=["mcp", "cli"])
def test_get_shows_the_catalog_array_not_the_shared_rows_last_writer(world, get) -> None:
    assert _tags_line(get(H_A)) == "Tags:       alpha, one, two"


@pytest.mark.parametrize("get", [_mcp_get, _cli_get], ids=["mcp", "cli"])
def test_get_shows_no_tags_line_for_a_retracted_array_whatever_the_row_says(world, get) -> None:
    assert _tags_line(get(H_B)) is None


@pytest.mark.parametrize("get", [_mcp_get, _cli_get], ids=["mcp", "cli"])
def test_get_falls_back_to_the_row_when_no_holder_has_the_key(world, get) -> None:
    assert _tags_line(get(H_FILE)) == "Tags:       row-file"


def _list_lines_mcp() -> list[str]:
    return mcp_core.store_list(collection="art").splitlines()


def _list_lines_cli() -> list[str]:
    return CliRunner().invoke(main, ["store", "list", "--collection", "art"], catch_exceptions=False).output.splitlines()


@pytest.mark.parametrize("lines", [_list_lines_mcp, _list_lines_cli], ids=["mcp", "cli"])
def test_list_shows_each_chunks_tags_from_the_catalog_with_the_row_as_fallback(world, lines) -> None:
    by_id = {ln.split()[0]: ln for ln in lines() if ln.strip().startswith(("a" * 8, "b" * 8, "c" * 8))}
    assert by_id[H_A].rstrip().endswith("[alpha, one, two]")
    assert "[" not in by_id[H_B], "a retracted array shows no tags, not the row's"
    assert by_id[H_FILE].rstrip().endswith("[row-file]")


@pytest.mark.parametrize("lines", [_list_lines_mcp, _list_lines_cli], ids=["mcp", "cli"])
def test_list_makes_one_lookup_for_the_page(world, lines, monkeypatch) -> None:
    calls: list[list[str]] = []
    real = store_hook.catalog_tags_for_chunks

    def counting(collection, chashes, **kw):
        calls.append(list(chashes))
        return real(collection, chashes, **kw)

    monkeypatch.setattr(store_hook, "catalog_tags_for_chunks", counting)
    lines()
    assert len(calls) == 1 and set(calls[0]) == {H_A, H_B, H_FILE}


@pytest.mark.parametrize("lines", [_list_lines_mcp, _list_lines_cli], ids=["mcp", "cli"])
def test_list_survives_a_down_catalog_with_the_rows_tags(world, lines) -> None:
    world.fail = True
    text = "\n".join(lines())
    assert "[beta,row-only]" in text and "[row-file]" in text
