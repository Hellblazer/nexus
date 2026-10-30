# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.13 (RDR-236 Phase 2): ``store_get`` / ``store_list`` show a note's tags, TTL and index date
from its OWN catalog entry, in the MCP tools and the ``nx store`` verbs alike.

A T3 chunk row is one per chunk text, so its ``tags``, ``ttl_days``, ``indexed_at`` and ``title`` belong to
whichever note wrote that text last. Two notes with identical text share it; note B wrote last, and today a
read about note A prints B's values. Each owner's catalog entry holds its own (``meta.tags``,
``meta.ttl_days`` with its clock ``meta.put_at``, the ``indexed_at`` column). The catalog is a stub reader
here (the real-engine twin is ``tests/integration/test_6pfgb13_note_display.py``); every case builds the
same world: A and B share ``H_S``, B wrote its row last.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import nexus.catalog.factory as catalog_factory
import nexus.catalog.store_hook as store_hook
import nexus.commands.store as store_cmd
import nexus.mcp_infra as mcp_infra
from nexus.catalog.store_hook import note_display
from nexus.cli import main
from nexus.corpus import t3_collection_name
from nexus.mcp import core as mcp_core

H_S = "5" * 64  # the text A and B share
H_A1 = "a" * 64  # a chunk only A holds
H_K = "c" * 64  # a chunk held by one keyless (legacy) note
H_K2 = "d" * 64  # a chunk held by two keyless notes
H_U = "e" * 64  # a chunk nobody holds

#: B wrote the shared row last, so the row says B's tags, TTL and time.
ROW_S = {
    "id": H_S, "title": "note-b", "content": "shared text", "tags": "beta,row-b", "ttl_days": 30,
    "indexed_at": "2026-10-20T00:00:00+00:00", "catalog_doc_id": "1.1.2",
}


class _StubT3:
    def __init__(self) -> None:
        self.name = t3_collection_name("art")
        self.rows = [{"name": self.name, "content_type": "knowledge", "owner_id": "art",
                      "embedding_model": "local-model", "lifecycle_state": "live", "count": 5}]
        self.docs = {
            H_S: dict(ROW_S),
            # A's own chunk, but the row carries a departed co-owner's tags, TTL and time
            H_A1: {"id": H_A1, "title": "ghost", "content": "a only", "tags": "ghost-tag", "ttl_days": 99,
                   "indexed_at": "2026-09-01T00:00:00+00:00", "catalog_doc_id": "9.9.9"},
            # a keyless sole owner whose row IS its own write (stamp and title agree)
            H_K: {"id": H_K, "title": "legacy", "content": "legacy text", "tags": "row-legacy", "ttl_days": 5,
                  "indexed_at": "2026-10-10T00:00:00+00:00", "catalog_doc_id": "1.1.4"},
            H_K2: {"id": H_K2, "title": "legacy-b", "content": "two legacy", "tags": "row-two", "ttl_days": 5,
                   "indexed_at": "2026-10-10T00:00:00+00:00", "catalog_doc_id": "1.1.6"},
            H_U: {"id": H_U, "title": "unowned", "content": "ghost", "tags": "row-u", "ttl_days": 2,
                  "indexed_at": "2026-10-11T00:00:00+00:00"},
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
    def __init__(self, state) -> None:
        self.state = state

    def docs_for_chashes(self, chashes):
        self.state.owner_calls += 1
        return {c: list(self.state.owners[c]) for c in chashes if c in self.state.owners}

    def resolve_many(self, tumblers):
        self.state.resolve_calls += 1
        return {t: self.state.entries[t] for t in tumblers if t in self.state.entries}

    def close(self) -> None:
        pass


def _entry(tumbler: str, title: str, meta: dict, indexed_at: str, collection: str = "") -> SimpleNamespace:
    return SimpleNamespace(tumbler=tumbler, title=title, meta=meta, indexed_at=indexed_at, physical_collection=collection)


@pytest.fixture
def world(monkeypatch):
    t3 = _StubT3()
    state = SimpleNamespace(owners={}, entries={}, t3=t3, fail=False, title_hits={}, owner_calls=0, resolve_calls=0)

    def make_reader():
        if state.fail:
            raise RuntimeError("catalog down")
        return _StubReader(state)

    monkeypatch.setattr(mcp_infra, "_collections_cache", mcp_infra._collections_cache)
    mcp_infra.prime_collections_cache(t3.rows)
    monkeypatch.setattr(store_cmd, "_t3", lambda **_: t3)
    monkeypatch.setattr(mcp_core, "_get_t3", lambda: t3)
    monkeypatch.setattr(store_hook, "split_note_text", lambda *a, **k: None)
    monkeypatch.setattr(store_cmd, "_split_note_text", lambda *a, **k: None)
    monkeypatch.setattr(
        store_hook, "catalog_chashes_for_title", lambda col, title: [[c] for c in state.title_hits.get(title, [])][:1],
    )
    monkeypatch.setattr(catalog_factory, "make_catalog_reader", make_reader)
    state.owners = {
        H_S: ["1.1.1", "1.1.2"], H_A1: ["1.1.1"], H_K: ["1.1.4"], H_K2: ["1.1.5", "1.1.6"],
    }
    state.entries = {
        # A: tags [alpha], 7 days from its own put on 2026-10-01
        "1.1.1": _entry("1.1.1", "note-a", {"tags": ["alpha"], "ttl_days": 7, "put_at": "2026-10-01T00:00:00+00:00"},
                        "2026-10-02T00:00:00+00:00"),
        # B: tags [beta], 30 days from 2026-10-20
        "1.1.2": _entry("1.1.2", "note-b", {"tags": ["beta"], "ttl_days": 30, "put_at": "2026-10-20T00:00:00+00:00"},
                        "2026-10-20T00:00:00+00:00"),
        "1.1.4": _entry("1.1.4", "legacy", {}, "2026-10-10T00:00:00+00:00"),
        "1.1.5": _entry("1.1.5", "legacy-a", {}, "2026-10-10T00:00:00+00:00"),
        "1.1.6": _entry("1.1.6", "legacy-b", {}, "2026-10-10T00:00:00+00:00"),
    }
    state.title_hits = {"note-a": [H_S], "note-b": [H_S]}
    yield state
    mcp_infra.invalidate_collections_cache()


def _mcp_get(doc_id: str) -> str:
    return mcp_core.store_get(doc_id=doc_id, collection="art")


def _cli_get(doc_id: str, *extra: str) -> str:
    return CliRunner().invoke(main, ["store", "get", doc_id, "--collection", "art", *extra], catch_exceptions=False).output


def _line(text: str, prefix: str) -> str | None:
    return next((ln for ln in text.splitlines() if ln.startswith(prefix)), None)


# ── store_get by title: the two notes that share a chunk each show their own ─────────────────


def test_get_by_title_shows_each_notes_own_tags_expiry_and_date_when_the_other_wrote_last(world) -> None:
    a, b = _mcp_get("note-a"), _mcp_get("note-b")
    assert _line(a, "Tags:") == "Tags:       alpha"
    assert _line(a, "Indexed:") == "Indexed:    2026-10-02"
    assert _line(a, "TTL:") == "TTL:        expires 2026-10-08", "A's put_at + 7 days, not B's row"
    assert _line(a, "Title:") == "Title:      note-a"
    assert _line(b, "Tags:") == "Tags:       beta"
    assert _line(b, "Indexed:") == "Indexed:    2026-10-20"
    assert _line(b, "TTL:") == "TTL:        expires 2026-11-19"
    assert "row-b" not in a + b and "beta" not in a


def test_the_expiry_counts_from_put_at_not_the_catalog_indexed_at(world) -> None:
    """The engine refreshes the catalog's ``indexed_at`` on every manifest write; the sweep counts from
    ``put_at``, so the display must too or it names a later day than the sweep acts on."""
    world.entries["1.1.1"] = _entry(
        "1.1.1", "note-a", {"tags": ["alpha"], "ttl_days": 7, "put_at": "2026-10-01T00:00:00+00:00"},
        "2026-10-25T00:00:00+00:00",  # a heal refreshed the column
    )
    out = _mcp_get("note-a")
    assert _line(out, "TTL:") == "TTL:        expires 2026-10-08"
    assert _line(out, "Indexed:") == "Indexed:    2026-10-25", "the Indexed line is the catalog column"


def test_a_permanent_note_prints_no_ttl_line(world) -> None:
    world.entries["1.1.2"] = _entry("1.1.2", "note-b", {"tags": ["beta"], "ttl_days": 0, "put_at": "2026-10-20T00:00:00+00:00"},
                                    "2026-10-20T00:00:00+00:00")
    assert _line(_mcp_get("note-b"), "TTL:") is None, "B's retraction wins over the row's 30 days"


# ── store_get by id: two owners disagree, so only what they agree on is shown ─────────────────


@pytest.mark.parametrize("get", [_mcp_get, _cli_get], ids=["mcp", "cli"])
def test_get_by_id_of_a_shared_chunk_unions_tags_and_shows_no_date_or_expiry_owners_disagree_on(world, get) -> None:
    out = get(H_S)
    assert _line(out, "Tags:") == "Tags:       alpha, beta"
    assert _line(out, "Indexed:") is None, "A and B state different dates: say nothing, never B's from the row"
    assert _line(out, "TTL:") is None
    assert "2026-10-20" not in out and "expires" not in out


@pytest.mark.parametrize("get", [_mcp_get, _cli_get], ids=["mcp", "cli"])
def test_get_by_id_shows_the_date_two_owners_agree_on(world, get) -> None:
    world.entries["1.1.1"].indexed_at = "2026-10-20T00:00:00Z"  # the same instant as B's, spelled otherwise
    assert _line(get(H_S), "Indexed:") == "Indexed:    2026-10-20"


@pytest.mark.parametrize("get", [_mcp_get, _cli_get], ids=["mcp", "cli"])
def test_a_sole_owners_catalog_values_beat_a_departed_co_owners_row(world, get) -> None:
    out = get(H_A1)
    assert _line(out, "Tags:") == "Tags:       alpha"
    assert _line(out, "TTL:") == "TTL:        expires 2026-10-08"
    assert _line(out, "Indexed:") == "Indexed:    2026-10-02"
    assert "ghost-tag" not in out and "2026-09-01" not in out


# ── the legacy fallback: the row speaks only for a keyless chunk with at most one owner ───────


@pytest.mark.parametrize("get", [_mcp_get, _cli_get], ids=["mcp", "cli"])
def test_a_keyless_sole_owner_shows_the_row_it_wrote(world, get) -> None:
    out = get(H_K)
    assert _line(out, "Tags:") == "Tags:       row-legacy"
    assert _line(out, "TTL:") == "TTL:        expires 2026-10-15", "the row is the note's own write (stamp and title)"
    assert _line(out, "Indexed:") == "Indexed:    2026-10-10"


@pytest.mark.parametrize("get", [_mcp_get, _cli_get], ids=["mcp", "cli"])
def test_a_keyless_sole_owner_does_not_take_a_ttl_from_a_row_another_note_wrote(world, get) -> None:
    world.t3.docs[H_K]["catalog_doc_id"] = "9.9.9"  # a departed co-owner's write
    out = get(H_K)
    assert _line(out, "Tags:") == "Tags:       row-legacy", "tags may come from a sole owner's row"
    assert _line(out, "TTL:") is None, "a TTL the sole owner did not write is not shown as its own"


@pytest.mark.parametrize("get", [_mcp_get, _cli_get], ids=["mcp", "cli"])
def test_a_chunk_of_two_keyless_notes_shows_none_of_the_rows_values(world, get) -> None:
    out = get(H_K2)
    assert _line(out, "Tags:") is None and _line(out, "TTL:") is None
    assert "row-two" not in out and "2026-10-15" not in out


@pytest.mark.parametrize("get", [_mcp_get, _cli_get], ids=["mcp", "cli"])
def test_an_unowned_chunk_and_a_down_catalog_show_the_row(world, get) -> None:
    out = get(H_U)
    assert _line(out, "Tags:") == "Tags:       row-u" and _line(out, "Indexed:") == "Indexed:    2026-10-11"
    world.fail = True
    down = get(H_S)
    assert _line(down, "Tags:") == "Tags:       beta,row-b", "no owner could be read: the row is the only record"


# ── store_list: one lookup for the page, each chunk from its own owners ───────────────────────


def _list_mcp() -> list[str]:
    return mcp_core.store_list(collection="art").splitlines()


def _list_cli() -> list[str]:
    return CliRunner().invoke(main, ["store", "list", "--collection", "art"], catch_exceptions=False).output.splitlines()


def _row_for(lines: list[str], chash: str) -> str:
    return next(ln for ln in lines if ln.strip().startswith(chash))


@pytest.mark.parametrize("lines", [_list_mcp, _list_cli], ids=["mcp", "cli"])
def test_list_shows_each_chunk_from_its_owners_never_the_last_writers_row(world, lines) -> None:
    got = lines()
    shared, only_a = _row_for(got, H_S), _row_for(got, H_A1)
    assert shared.rstrip().endswith("[alpha, beta]")
    assert "unknown" in shared, "two owners with different expiries: no expiry, and certainly not B's 30 days"
    assert "2026-10-20" not in shared and "2026-11-19" not in shared
    assert "ghost" not in only_a and "ghost-tag" not in only_a
    assert "expires 2026-10-08" in only_a and "2026-10-02" in only_a and only_a.rstrip().endswith("[alpha]")
    assert "note-a" in only_a, "the title is the owner's, not the row's last writer"
    assert "note-a" in shared and "note-b" in shared, "a shared chunk names every owner"


@pytest.mark.parametrize("lines", [_list_mcp, _list_cli], ids=["mcp", "cli"])
def test_list_keyless_rows_follow_the_same_rule_as_get(world, lines) -> None:
    got = lines()
    legacy, two, unowned = _row_for(got, H_K), _row_for(got, H_K2), _row_for(got, H_U)
    assert "expires 2026-10-15" in legacy and legacy.rstrip().endswith("[row-legacy]")
    assert "row-two" not in two and "[" not in two
    assert "legacy-a" in two and "legacy-b" in two
    assert "unowned" in unowned and unowned.rstrip().endswith("[row-u]"), "a chunk nobody holds is shown as its row"


@pytest.mark.parametrize("surface", [_list_mcp, _list_cli, lambda: _mcp_get(H_S).splitlines(), lambda: _cli_get(H_S).splitlines()],
                         ids=["mcp-list", "cli-list", "mcp-get", "cli-get"])
def test_one_owner_lookup_per_page_or_get(world, surface) -> None:
    surface()
    assert (world.owner_calls, world.resolve_calls) == (1, 1), "one docs_for_chashes and one resolve_many, however many fields"


def test_the_docs_view_prints_the_documents_own_index_date_not_the_first_chunk_rows(world, monkeypatch) -> None:
    monkeypatch.setattr(
        store_hook, "_manifest_doc_index_full",
        lambda _col: (
            {H_S: ("1.1.1", "1.1.2")}, {"1.1.1": "note-a", "1.1.2": "note-b"}, {"1.1.1": H_S, "1.1.2": H_S},
            {"1.1.1": "2026-10-02T00:00:00+00:00", "1.1.2": "2026-10-20T00:00:00+00:00"}, "",
        ),
    )
    world.t3.docs = {H_S: dict(ROW_S)}
    for text in (
        mcp_core.store_list(collection="art", docs=True),
        CliRunner().invoke(main, ["store", "list", "--collection", "art", "--docs"], catch_exceptions=False).output,
    ):
        a = next(ln for ln in text.splitlines() if "note-a" in ln)
        b = next(ln for ln in text.splitlines() if "note-b" in ln)
        assert a.rstrip().endswith("2026-10-02") and b.rstrip().endswith("2026-10-20")


def test_json_keeps_the_row_keys_and_adds_the_owners_values_as_note(world) -> None:
    import json

    out = json.loads(_cli_get(H_A1, "--json"))
    assert out["tags"] == "ghost-tag" and out["ttl_days"] == 99, "the raw chunk row, unchanged for scripts that read it"
    assert out["note"] == {"title": "note-a", "tags": "alpha", "indexed": "2026-10-02", "ttl": "expires 2026-10-08"}


# ── the rule itself ───────────────────────────────────────────────────────────────────────────


def _display(owners, **row):
    base = {"row": {}, "row_title": "", "row_tags": "", "row_ttl_days": None, "row_indexed_at": ""}
    base.update(row)
    return note_display(owners, **base)


def test_the_rule_reads_an_array_and_a_legacy_string_alike() -> None:
    arr = _entry("1.1.1", "n", {"tags": ["arch", "decision"]}, "")
    old = _entry("1.1.2", "n", {"tags": "arch, decision"}, "")
    assert _display([arr]).tags == _display([old]).tags == "arch, decision"


def test_the_rule_never_uses_the_row_for_a_keyed_owner_even_when_its_array_is_empty() -> None:
    retracted = _entry("1.1.1", "n", {"tags": []}, "")
    assert _display([retracted], row_tags="stale").tags == ""


def test_the_rule_blanks_what_a_keyless_owner_among_several_cannot_state() -> None:
    keyed = _entry("1.1.1", "a", {"ttl_days": 3, "put_at": "2026-10-01T00:00:00Z"}, "2026-10-01T00:00:00Z")
    keyless = _entry("1.1.2", "b", {}, "2026-10-01T00:00:00Z")
    assert _display([keyed, keyless], row_ttl_days=3, row_indexed_at="2026-10-01T00:00:00Z").ttl == "unknown"
    assert _display([keyed]).ttl == "expires 2026-10-04"


def test_a_title_filter_over_two_keyless_owners_does_not_make_the_shared_row_the_filtered_owners() -> None:
    """Phase 2 review H2: ``held`` was already narrowed to the requested title, so a chunk with owners A
    "alpha" and B "beta" read as A's alone and B's row tags (and index date) printed for A. Sole ownership
    is judged over ALL live owners, before the title filter."""
    a = _entry("1.1.1", "alpha", {}, "")
    b = _entry("1.1.2", "beta", {}, "")
    row = {"catalog_doc_id": "1.1.2", "title": "beta"}
    shown = _display([a, b], title="alpha", row=row, row_tags="b-tags", row_indexed_at="2026-10-20T00:00:00+00:00")
    assert shown.tags == ""
    assert shown.indexed == ""
    assert shown.title == "alpha"
    # control: the unfiltered read of the same two owners already blanked both
    both = _display([a, b], row=row, row_tags="b-tags", row_indexed_at="2026-10-20T00:00:00+00:00")
    assert both.tags == "" and both.indexed == ""


def test_a_sole_keyless_owner_takes_the_rows_tags_only_when_another_note_did_not_write_it() -> None:
    """The tags path goes through ``row_may_give_tags`` as the export does: a row whose stamp AND title
    name another note is not this note's."""
    a = _entry("1.1.1", "alpha", {}, "2026-10-01T00:00:00+00:00")
    other = {"catalog_doc_id": "9.9.9", "title": "someone-else"}
    assert _display([a], row=other, row_tags="stale").tags == ""
    assert _display([a], row={"catalog_doc_id": "1.1.1", "title": "alpha"}, row_tags="mine").tags == "mine"
    assert _display([a], row={}, row_tags="legacy-unstamped").tags == "legacy-unstamped"

