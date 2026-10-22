# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx store get/list/delete with live model siblings (nexus-9ts7n), and the
MCP store_get title lookup that names one sibling (nexus-xkc4n).

RDR-210 Phase 4 Step 1 (nexus-50uw6) made the MCP store tools read every live
sibling of a corpus (``corpus.t3_collection_names_for_read``); the CLI verbs
still read one name, so the two surfaces disagreed for a corpus with a
second-family sibling.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

import nexus.catalog.store_hook as store_hook
import nexus.commands.store as store_cmd
import nexus.mcp_infra as mcp_infra
from nexus.cli import main
from nexus.corpus import t3_collection_name
from nexus.mcp import core as mcp_core

ODD = "art-notes-in-the-second-family"  # a sibling registered under a non-four-segment name
H_PRIMARY = "a" * 64
H_ODD = "b" * 64


def _row(name: str, model: str) -> dict:
    return {"name": name, "content_type": "knowledge", "owner_id": "art",
            "embedding_model": model, "lifecycle_state": "live", "count": 1}


class _SiblingT3:
    """Two live siblings of corpus ``art``: one note in each, both titled ``shared``."""

    def __init__(self) -> None:
        self.primary = t3_collection_name("art")
        self.rows = [_row(self.primary, "local-model"), _row(ODD, "voyage-model-b")]
        self.docs = {
            self.primary: {H_PRIMARY: {"id": H_PRIMARY, "title": "shared", "content": "in the primary"}},
            ODD: {H_ODD: {"id": H_ODD, "title": "shared", "content": "in the odd sibling"}},
        }
        self.deleted: list[tuple[str, str]] = []
        self.reaped: list[tuple] = []

    def collection_exists(self, name: str) -> bool:
        return name in self.docs

    def list_collections(self) -> list[dict]:
        return [dict(r) for r in self.rows]

    def collection_info(self, name: str) -> dict:
        return {"count": len(self.docs.get(name, {})), "metadata": {}}

    def list_store(self, name: str, limit: int = 20, offset: int = 0) -> list[dict]:
        return list(self.docs.get(name, {}).values())[offset:offset + limit]

    def get_by_id(self, name: str, doc_id: str) -> dict | None:
        return self.docs.get(name, {}).get(doc_id)

    def find_ids_by_title(self, name: str, title: str) -> list[str]:
        return [d["id"] for d in self.docs.get(name, {}).values() if d["title"] == title]

    def delete_by_id(self, name: str, doc_id: str) -> bool:
        self.deleted.append((name, doc_id))
        return self.docs.get(name, {}).pop(doc_id, None) is not None


@pytest.fixture
def t3(monkeypatch):
    t3 = _SiblingT3()
    monkeypatch.setattr(mcp_infra, "_collections_cache", mcp_infra._collections_cache)
    mcp_infra.prime_collections_cache(t3.rows)
    monkeypatch.setattr(store_cmd, "_t3", lambda **_: t3)
    monkeypatch.setattr(mcp_core, "_get_t3", lambda: t3)
    monkeypatch.setattr(store_hook, "split_note_text", lambda *a, **k: None)
    monkeypatch.setattr(store_cmd, "_split_note_text", lambda *a, **k: None)
    monkeypatch.setattr(store_hook, "catalog_chashes_for_title", lambda *a, **k: [])
    monkeypatch.setattr(store_hook, "live_holders_of_chashes", lambda *a, **k: {})
    monkeypatch.setattr(
        store_cmd, "_reap_catalog_for_doc_ids", lambda ids, **k: t3.reaped.append((tuple(ids), k)),
    )
    yield t3
    mcp_infra.invalidate_collections_cache()


def _nx(*args: str):
    return CliRunner().invoke(main, ["store", *args], catch_exceptions=False)


# ── nexus-9ts7n: the CLI reads every live sibling, as the MCP tools do ────────


def test_get_finds_a_note_held_by_the_sibling_the_name_does_not_pick(t3):
    out = _nx("get", H_ODD, "--collection", "art")
    assert out.exit_code == 0, out.output
    assert f"Collection: {ODD}" in out.output
    assert "in the odd sibling" in out.output


def test_get_of_an_id_in_no_sibling_names_every_collection_it_looked_in(t3):
    result = CliRunner().invoke(main, ["store", "get", "c" * 64, "--collection", "art"])
    assert result.exit_code != 0
    assert t3.primary in result.output and ODD in result.output


def test_list_pages_the_named_collection_and_names_its_siblings(t3):
    out = _nx("list", "--collection", "art")
    assert out.exit_code == 0, out.output
    assert f"{t3.primary}  (showing 1-1; 1 stored)" in out.output
    assert f"also in this corpus: {ODD}" in out.output
    odd = _nx("list", "--collection", ODD)
    assert f"{ODD}  (showing 1-1; 1 stored)" in odd.output
    assert f"also in this corpus: {t3.primary}" in odd.output


def test_delete_stays_one_collection_and_names_the_sibling_that_holds_the_id(t3):
    result = CliRunner().invoke(main, ["store", "delete", "--collection", "art", "--id", H_ODD, "--yes"])
    assert result.exit_code != 0
    assert f"--collection {ODD}" in result.output, result.output
    assert t3.deleted == [], "delete reached into a sibling it was not given"
    assert t3.reaped == [], "a refused delete reaped catalog rows"
    ok = _nx("delete", "--collection", ODD, "--id", H_ODD, "--yes")
    assert ok.exit_code == 0, ok.output
    assert t3.deleted == [(ODD, H_ODD)]


# ── nexus-xkc4n: a title in several siblings, and naming one of them ──────────


def test_store_get_title_in_two_siblings_is_ambiguous_under_the_corpus_name(t3):
    out = mcp_core.store_get(doc_id="shared", collection="art")
    assert "matches documents in 2 collections" in out, out
    assert ODD in out and t3.primary in out


@pytest.mark.parametrize("which", ["primary", "odd"])
def test_store_get_title_names_one_sibling_by_its_exact_name(t3, which):
    name, text = (t3.primary, "in the primary") if which == "primary" else (ODD, "in the odd sibling")
    out = mcp_core.store_get(doc_id="shared", collection=name)
    assert f"Collection: {name}" in out, out
    assert text in out, out
    other = ODD if which == "primary" else t3.primary
    if which == "odd":  # a conformant name is one collection: nothing else is in its scope
        assert f"Also titled in: {other}" in out, out


def test_get_json_names_the_collection(t3):
    import json

    out = _nx("get", H_ODD, "--collection", "art", "--json")
    assert json.loads(out.stdout)["collection"] == ODD


def test_a_fully_qualified_name_is_that_one_collection(t3):
    result = CliRunner().invoke(main, ["store", "get", H_ODD, "--collection", t3.primary])
    assert result.exit_code != 0
    assert f"not found in {t3.primary}\n" in result.output, result.output


def test_a_listing_that_cannot_be_read_leaves_the_one_name(t3, monkeypatch):
    import nexus.db.http_vector_client as hvc

    def _broken(_t3):
        raise RuntimeError("listing unavailable")

    monkeypatch.setattr(hvc, "sibling_candidate_rows", _broken)
    assert _nx("get", H_PRIMARY, "--collection", "art").exit_code == 0
    result = CliRunner().invoke(main, ["store", "get", H_ODD, "--collection", "art"])
    assert result.exit_code != 0 and ODD not in result.output


def test_an_empty_pick_still_names_its_siblings(t3):
    t3.docs[t3.primary].clear()
    out = _nx("list", "--collection", "art")
    assert f"No entries in {t3.primary}" in out.output
    assert f"also in this corpus: {ODD}" in out.output
    mcp = mcp_core.store_list(collection="art")
    assert f"No entries in {t3.primary}." in mcp and f"also in this corpus: {ODD}" in mcp, mcp


def test_store_get_title_held_by_one_sibling_reads_across_the_corpus(t3):
    """The nexus-50uw6 property the exact-name preference must not touch."""
    t3.docs[ODD]["d" * 64] = {"id": "d" * 64, "title": "only-odd", "content": "unique"}
    out = mcp_core.store_get(doc_id="only-odd", collection="art")
    assert f"Collection: {ODD}" in out and "unique" in out, out
    assert "Also titled in" not in out


def test_store_get_catalog_title_in_two_siblings_picks_the_named_one(t3, monkeypatch):
    """The catalog-document branch (a title no chunk row carries) narrows the same way."""
    held = {t3.primary: [[H_PRIMARY]], ODD: [[H_ODD]]}
    monkeypatch.setattr(store_hook, "catalog_chashes_for_title", lambda c, title: held.get(c, []) if title == "cat" else [])
    assert "names 2 documents in 2 collections" in mcp_core.store_get(doc_id="cat", collection="art")
    out = mcp_core.store_get(doc_id="cat", collection=ODD)
    assert f"Collection: {ODD}" in out and "in the odd sibling" in out, out
    assert f"Also titled in: {t3.primary}" in out, out



# ── nexus-jald5: delete --title and the MCP store_delete name the holding sibling ──


@pytest.mark.parametrize("yes", [True, False], ids=["yes", "prompted"])
def test_delete_by_title_names_the_sibling_that_holds_it_and_deletes_nothing(t3, monkeypatch, yes):
    t3.docs[ODD]["e" * 64] = {"id": "e" * 64, "title": "odd-only", "content": "x"}
    title_reaps: list[str] = []
    monkeypatch.setattr(
        store_hook, "reap_catalog_documents_by_title", lambda col, *a, **k: title_reaps.append(col),
    )
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: None)
    argv = ["store", "delete", "--collection", "art", "--title", "odd-only"] + (["--yes"] if yes else [])
    result = CliRunner().invoke(main, argv)
    assert result.exit_code != 0
    assert f"--collection {ODD}" in result.output, result.output
    assert t3.deleted == [] and t3.reaped == []
    assert set(title_reaps) <= {t3.primary}, "a title reap reached into a sibling"


def test_no_holder_means_no_hint_on_either_surface(t3, monkeypatch):
    """The negative control: an id or title no collection holds gets the plain answer."""
    monkeypatch.setattr(store_hook, "reap_catalog_documents_by_title", lambda *a, **k: None)
    by_id = CliRunner().invoke(main, ["store", "delete", "--collection", "art", "--id", "f" * 64, "--yes"])
    by_title = CliRunner().invoke(main, ["store", "delete", "--collection", "art", "--title", "nowhere", "--yes"])
    mcp = mcp_core.store_delete(doc_id="f" * 64, collection="art")
    for out in (by_id.output, by_title.output, mcp):
        assert "it is in" not in out, out


def test_mcp_store_delete_names_the_sibling_that_holds_the_id(t3):
    out = mcp_core.store_delete(doc_id=H_ODD, collection="art")
    assert out.startswith(f"Not found: {H_ODD!r} in {t3.primary}"), out
    assert f"collection={ODD!r}" in out, out
    assert t3.deleted == []


def test_a_hint_probe_that_fails_leaves_the_plain_not_found(t3, monkeypatch):
    real = t3.get_by_id

    def _sibling_down(name, doc_id):
        if name == ODD:
            raise RuntimeError("the engine does not serve this family")
        return real(name, doc_id)

    monkeypatch.setattr(t3, "get_by_id", _sibling_down)
    result = CliRunner().invoke(main, ["store", "delete", "--collection", "art", "--id", H_ODD, "--yes"])
    assert result.exit_code != 0 and "Traceback" not in result.output
    assert result.output.strip().endswith(f"not found in {t3.primary}"), result.output
    out = mcp_core.store_delete(doc_id=H_ODD, collection="art")
    assert out == f"Not found: {H_ODD!r} in {t3.primary}", out

