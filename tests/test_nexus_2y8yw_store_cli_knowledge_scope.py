# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-2y8yw: the nx store read verbs give bare ``knowledge`` the meaning the
MCP store tools give it (nexus-mgqs8, Sam's ruling: one meaning): every live
knowledge__* collection, the scope search uses. Both surfaces read through
``corpus.store_read_scope``.

Mirrors tests/test_mcp_knowledge_scope.py: two live knowledge collections in a
fake T3 plus a code collection that must stay out; the CLI runs for real.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

import nexus.commands.store as store_cmd
from nexus.cli import main
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction
from nexus.db.t3 import T3Database
from tests.conftest import make_vector_test_client

ALPHA = "knowledge__alpha__minilm-l6-v2-384__v1"
BETA = "knowledge__beta__minilm-l6-v2-384__v1"
CODE = "code__repo__minilm-l6-v2-384__v1"


@pytest.fixture
def scope(monkeypatch):
    t3 = T3Database(_client=make_vector_test_client(), _ef_override=DefaultEmbeddingFunction())
    ids = {
        "a": t3.put(collection=ALPHA, content="Alpha note about raft leases.", title="alpha-note"),
        "b": t3.put(collection=BETA, content="Beta note about vector clocks.", title="beta-note"),
    }
    rows = [
        {"name": n, "count": c, "content_type": n.split("__", 1)[0], "lifecycle_state": "live"}
        for n, c in ((ALPHA, 1), (BETA, 1), (CODE, 5))
    ] + [{"name": "knowledge__gone__minilm-l6-v2-384__v1", "count": 9, "content_type": "knowledge",
          "lifecycle_state": "quarantine"}]
    monkeypatch.setattr(t3, "list_collections", lambda *a, **k: [dict(r) for r in rows])
    monkeypatch.setattr(
        "nexus.mcp_infra.get_collection_row",
        lambda name: {"content_type": name.split("__", 1)[0], "lifecycle_state": "live"},
    )
    monkeypatch.setattr(store_cmd, "_t3", lambda **_: t3)
    monkeypatch.setattr(store_cmd, "_split_note_text", lambda *a, **k: None)  # no catalog: an unsplit note
    monkeypatch.setattr("nexus.catalog.store_hook.catalog_tags_for_chunks", lambda *a, **k: {})  # nor tags (nexus-6pfgb.38)
    return t3, ids


def _nx(*args: str):
    return CliRunner().invoke(main, ["store", *args], catch_exceptions=False)


def test_get_with_the_default_finds_a_note_in_any_knowledge_collection(scope):
    _t3, ids = scope
    out = _nx("get", ids["b"])
    assert out.exit_code == 0, out.output
    assert f"Collection: {BETA}" in out.output
    assert "Beta note about vector clocks." in out.output


def test_list_with_the_default_lists_the_knowledge_subjects_and_their_sizes(scope):
    out = _nx("list")
    assert out.exit_code == 0, out.output
    assert "2 knowledge collections" in out.output
    assert f"  {ALPHA}  1 entries" in out.output and f"  {BETA}  1 entries" in out.output
    assert CODE not in out.output


def test_a_named_knowledge_collection_still_pages_its_entries(scope):
    _t3, ids = scope
    out = _nx("list", "--collection", ALPHA)
    assert out.exit_code == 0, out.output
    assert f"{ALPHA}  (showing 1-1;" in out.output and ids["a"] in out.output
    assert "knowledge collections" not in out.output


def test_the_cli_and_the_mcp_tools_read_the_same_scope(scope, monkeypatch):
    from nexus.mcp import core

    t3, _ids = scope
    monkeypatch.setattr(core, "_get_collection_names", lambda: [ALPHA, BETA, CODE])
    assert core._read_scope(t3, "knowledge") == store_cmd._store_scope("knowledge", t3)[0] == [ALPHA, BETA]
