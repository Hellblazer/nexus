# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-mgqs8: collection="knowledge" on the store read tools means the
scope search and query give corpus="knowledge": every live knowledge__*
collection (Sam's ruling).

Before, store_get, store_get_many and store_list read the bare literal as
the single knowledge__knowledge placeholder, so a model that found a note
with search and fetched it with the default collection got a silent miss.

The catalog listing is patched to two live knowledge collections held in a
fake T3, plus a code collection that must stay out; resolve_corpus and the
tools run for real.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from nexus.corpus import resolve_corpus
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction
from nexus.db.t3 import T3Database
from nexus.mcp import core
from nexus.mcp.core import store_get, store_get_many, store_list
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
        "shared_a": t3.put(collection=ALPHA, content="Shared title, alpha side.", title="shared"),
        "shared_b": t3.put(collection=BETA, content="Shared title, beta side.", title="shared"),
    }
    monkeypatch.setattr(core, "_get_collection_names", lambda: [ALPHA, BETA, CODE])
    monkeypatch.setattr(
        "nexus.mcp_infra.get_collection_row",
        lambda name: {"content_type": name.split("__", 1)[0], "lifecycle_state": "live"},
    )
    monkeypatch.setattr(core, "_get_collection_counts", lambda: {ALPHA: 2, BETA: 2, CODE: 5})
    with patch("nexus.mcp.core._get_t3", return_value=t3):
        yield t3, ids


def test_the_store_scope_is_the_search_scope(scope) -> None:
    t3, _ = scope
    assert core._read_scope(t3, "knowledge") == resolve_corpus("knowledge", [ALPHA, BETA, CODE])
    assert core._read_scope(t3, "knowledge") == [ALPHA, BETA]


def test_store_get_by_id_with_the_default_finds_a_note_in_any_knowledge_collection(scope) -> None:
    _, ids = scope
    out = store_get(ids["b"])
    assert "Beta note about vector clocks." in out, out
    assert f"Collection: {BETA}" in out


def test_store_get_by_a_unique_title_with_the_default(scope) -> None:
    out = store_get("beta-note")
    assert "Beta note about vector clocks." in out, out


def test_a_title_in_two_collections_names_both_instead_of_guessing(scope) -> None:
    out = store_get("shared")
    assert ALPHA in out and BETA in out, out
    assert "Shared title" not in out


def test_an_explicit_collection_still_means_that_one_collection(scope) -> None:
    _, ids = scope
    assert store_get(ids["b"], ALPHA).startswith("Not found"), "an explicit name must not fan out"


def test_store_get_many_with_the_default_hydrates_across_collections(scope) -> None:
    _, ids = scope
    res = store_get_many([ids["a"], ids["b"]], structured=True)
    assert res["missing"] == [], res
    assert any("Alpha note" in c for c in res["contents"])
    assert any("Beta note" in c for c in res["contents"])


def test_per_id_routing_to_the_bare_name_still_fans_out(scope) -> None:
    """A list aligned 1:1 with ids routes per id; "knowledge" as one of those
    targets must mean the whole scope, not the placeholder."""
    _, ids = scope
    res = store_get_many([ids["a"], ids["b"]], ["knowledge", "knowledge"], structured=True)
    assert res["missing"] == [], res


def test_store_list_with_the_default_lists_the_knowledge_subjects(scope) -> None:
    out = store_list()
    assert ALPHA in out and BETA in out, out
    assert CODE not in out
