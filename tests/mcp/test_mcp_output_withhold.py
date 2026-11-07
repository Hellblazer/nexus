# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 3 Step 3.1 (nexus-rfvyy.8.2): the MCP output posture.

Every MCP read tool hands what it returns to the session model, so to Anthropic. A hit whose engine
``sensitive`` field is true is therefore replaced, by default, with one line, ``[sensitive] <tumbler>: content
withheld``, with no title, path or snippet, and the response says how many hits it withheld. The config key
``sensitivity.mcp_output: flag`` shows the content with a ``[sensitive]`` badge and adds the reader clause. There is
no tool argument, and a repository's ``.nexus.yml`` cannot set the key.

Each test names a canary string that lives only in the sensitive hit's content and title. The pins carry the
negative: the canary is absent from the WHOLE response, text and structured alike.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from nexus.context_annotations import READER_INSTRUCTION, SENSITIVE_READER_CLAUSE
from nexus.mcp.core import (
    search_aspect_scoped,
    search_graph_hop,
    search_metadata_scoped,
    search_topic_scoped,
    store_get_many,
)
from nexus.mcp_server import _inject_t3, _reset_singletons, _search_render, query, store_get
from nexus.types import SearchResult

CANARY = f"CANARY-rfvyy-3-1-{uuid4()}"
SECRET_TITLE = f"SECRET-TITLE-{uuid4()}"
SECRET_PATH = f"secret/{uuid4()}.md"
CLEAN_BODY = "an ordinary public paragraph about gardening"
CHASH_S = "a" * 64
CHASH_C = "b" * 64
COLLECTION = "code__test"


@pytest.fixture(autouse=True)
def _world(tmp_path, monkeypatch):
    """A tmp config dir, the default (``include``) read posture, and a clean T3/T1 slate."""
    cfg_dir = tmp_path / "nexus-config"
    cfg_dir.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))
    # The conftest sets llm_bound for every test; the MCP tools would then read with ``exclude`` and the engine would
    # drop the sensitive hit. The posture under test is the default one, where the engine returns it with its flag.
    monkeypatch.delenv("NX_SENSITIVITY_POSTURE", raising=False)
    _reset_singletons()
    yield cfg_dir
    _reset_singletons()


def _set_mcp_output(cfg_dir, value: str | None) -> None:
    (cfg_dir / "config.yml").write_text("" if value is None else f"sensitivity:\n  mcp_output: {value}\n")


def _sensitive_hit(owners: list[str] | None = None) -> SearchResult:
    meta: dict = {
        "sensitive": True, "chunk_text_hash": CHASH_S, "doc_id": "1.2.7",
        "_display_title": SECRET_TITLE, "_display_path": SECRET_PATH, "title": SECRET_TITLE,
    }
    if owners is not None:
        meta["sensitive_owners"] = owners
    return SearchResult(id=CHASH_S, content=f"{CANARY} the secret body", distance=0.10,
                        collection=COLLECTION, metadata=meta)


def _clean_hit() -> SearchResult:
    return SearchResult(
        id=CHASH_C, content=CLEAN_BODY, distance=0.20, collection=COLLECTION,
        metadata={"sensitive": False, "chunk_text_hash": CHASH_C, "doc_id": "1.3.1",
                  "_display_title": "Gardening", "_display_path": "gardening.md", "title": "Gardening"},
    )


def _mock_t3(collections: list[str] | None = None) -> MagicMock:
    names = collections or [COLLECTION]
    t3 = MagicMock()
    t3.list_collections.return_value = [
        {"name": n, "count": 3, "content_type": n.partition("__")[0], "owner_id": "1-1",
         "embedding_model": "test-model", "lifecycle_state": "live"}
        for n in names
    ]
    _inject_t3(t3)
    return t3


def _fake_search(hits: list[SearchResult]):
    def fake(*_a, **_kw):
        return [SearchResult(id=h.id, content=h.content, distance=h.distance, collection=h.collection,
                             metadata=dict(h.metadata)) for h in hits]
    return fake


def _render(hits: list[SearchResult], *, structured: bool = False, query_text: str = "posture pin"):
    """``_search_render`` over *hits*, no T1 trace, no config merge."""
    _mock_t3()
    with patch("nexus.search_engine.search_cross_corpus", _fake_search(hits)), \
         patch("nexus.config.load_config", return_value={"search": {"query_sanitizer": False}}), \
         patch("nexus.mcp.core._get_t1", side_effect=RuntimeError("no T1 in this test")), \
         patch("nexus.mcp.core._page_cache_get", return_value=None), \
         patch("nexus.mcp.core._page_cache_put"):
        return _search_render(query=query_text, corpus=COLLECTION, cluster_by="", structured=structured,
                              limit=10, sensitivity="include")


def _has_badge(text: str) -> bool:
    """The ``[sensitive]`` badge appears OUTSIDE the reader clause, which quotes the word itself."""
    return "[sensitive]" in text.replace(SENSITIVE_READER_CLAUSE, "")


def _stub(tumbler: str) -> str:
    return f"[sensitive] {tumbler}: content withheld"


# ── search ────────────────────────────────────────────────────────────────────


def test_search_withhold_is_the_default_and_shows_only_the_stub(_world):
    out = _render([_sensitive_hit(owners=["1.2.7"]), _clean_hit()])

    assert _stub("1.2.7") in out.splitlines()
    assert "1 sensitive hit withheld" in out
    for secret in (CANARY, SECRET_TITLE, SECRET_PATH):
        assert secret not in out
    assert CLEAN_BODY in out and "Gardening" in out
    assert SENSITIVE_READER_CLAUSE not in out  # nothing is shown with its content, so no clause
    assert READER_INSTRUCTION in out


def test_search_withhold_names_the_lowest_owner_by_tumbler_order(_world):
    # Numeric, not lexicographic: 1.9.4 sorts before 1.10.1 by tumbler, after it as text.
    out = _render([_sensitive_hit(owners=["1.10.1", "1.9.4"])])
    assert _stub("1.9.4") in out.splitlines()
    assert "1.10.1" not in out


def test_search_withhold_with_no_resolvable_owner_leaves_the_tumbler_out(_world):
    with patch("nexus.doc_sensitivity.flagged_owners_of_chashes", return_value={}):
        out = _render([_sensitive_hit(owners=None)])
    assert "[sensitive]: content withheld" in out.splitlines()
    assert CANARY not in out


def test_search_resolves_the_owner_through_the_catalog_when_the_hit_names_none(_world):
    with patch("nexus.doc_sensitivity.flagged_owners_of_chashes", return_value={CHASH_S: ["1.9.4", "1.2.7"]}):
        out = _render([_sensitive_hit(owners=None)])
    assert _stub("1.2.7") in out.splitlines()


def test_search_withhold_structured_drops_the_hit_and_states_the_count(_world):
    out = _render([_sensitive_hit(owners=["1.2.7"]), _clean_hit()], structured=True)
    assert CHASH_S not in out["ids"] and CHASH_S not in out["chunk_text_hash"]
    assert out["ids"] == [CHASH_C]
    assert out["sensitive_withheld"] == 1
    flat = repr(out)
    for secret in (CANARY, SECRET_TITLE, SECRET_PATH):
        assert secret not in flat


def test_search_flag_shows_content_with_a_badge_and_the_reader_clause(_world):
    _set_mcp_output(_world, "flag")
    out = _render([_sensitive_hit(owners=["1.2.7"]), _clean_hit()])
    assert CANARY in out
    assert _has_badge(out)
    assert "withheld" not in out
    assert SENSITIVE_READER_CLAUSE in out
    assert out.startswith(READER_INSTRUCTION)  # the constant is unchanged; the clause joins after it


def test_search_flag_structured_marks_which_hits_are_sensitive(_world):
    _set_mcp_output(_world, "flag")
    out = _render([_sensitive_hit(owners=["1.2.7"]), _clean_hit()], structured=True)
    assert out["ids"] == [CHASH_S, CHASH_C]
    assert out["sensitive"] == [True, False]
    assert "sensitive_withheld" not in out


def test_search_a_hit_with_a_sensitive_owner_is_withheld_though_the_engine_flag_is_absent(_world):
    hit = _sensitive_hit(owners=["1.2.7"])
    del hit.metadata["sensitive"]  # a stale engine that never sends the field
    out = _render([hit, _clean_hit()])
    assert CANARY not in out
    assert _stub("1.2.7") in out.splitlines()


@pytest.mark.parametrize("value", [None, "withhold", "bogus", "FLAG-ish", "", "true"])
def test_search_any_value_but_flag_withholds(_world, value):
    _set_mcp_output(_world, value)
    out = _render([_sensitive_hit(owners=["1.2.7"])])
    assert CANARY not in out
    assert _stub("1.2.7") in out.splitlines()


def test_search_flag_value_is_case_and_space_insensitive(_world):
    _set_mcp_output(_world, "'  Flag '")
    assert CANARY in _render([_sensitive_hit(owners=["1.2.7"])])


def test_a_repository_config_cannot_switch_the_posture(_world, tmp_path, monkeypatch):
    repo = tmp_path / "cloned-repo"
    repo.mkdir()
    (repo / ".nexus.yml").write_text("sensitivity:\n  mcp_output: flag\n")
    monkeypatch.chdir(repo)
    out = _render([_sensitive_hit(owners=["1.2.7"])])
    assert CANARY not in out


def test_an_unreadable_config_fails_closed(_world):
    (_world / "config.yml").write_text("sensitivity: [unterminated\n")
    out = _render([_sensitive_hit(owners=["1.2.7"])])
    assert CANARY not in out


@pytest.mark.parametrize("posture", [None, "flag"])
def test_search_with_no_sensitive_hit_is_unchanged_under_either_posture(_world, posture):
    _set_mcp_output(_world, posture)
    text = _render([_clean_hit()])
    assert "[sensitive" not in text and "withheld" not in text
    assert SENSITIVE_READER_CLAUSE not in text
    assert text.startswith(READER_INSTRUCTION + "\n\n")
    struct = _render([_clean_hit()], structured=True)
    assert set(struct) == {"ids", "tumblers", "distances", "hybrid_scores", "collections",
                           "chunk_collections", "chunk_text_hash"}


def test_search_text_is_byte_identical_for_a_clean_page_across_postures(_world):
    _set_mcp_output(_world, "withhold")
    withheld_mode = _render([_clean_hit()])
    _set_mcp_output(_world, "flag")
    flag_mode = _render([_clean_hit()])
    assert withheld_mode == flag_mode


# ── query ─────────────────────────────────────────────────────────────────────


def _query(hits: list[SearchResult], *, structured: bool = False):
    _mock_t3()
    with patch("nexus.search_engine.search_cross_corpus", _fake_search(hits)), \
         patch("nexus.config.load_config", return_value={"search": {"query_sanitizer": False}}):
        return query(question="posture pin", corpus=COLLECTION, structured=structured)


def test_query_withhold_replaces_the_document_card_with_the_stub(_world):
    out = _query([_sensitive_hit(owners=["1.2.7"]), _clean_hit()])
    assert _stub("1.2.7") in out.splitlines()
    assert "1 sensitive hit withheld" in out
    for secret in (CANARY, SECRET_TITLE, SECRET_PATH):
        assert secret not in out
    assert "Gardening" in out
    assert SENSITIVE_READER_CLAUSE not in out


def test_query_withhold_structured_drops_the_hit(_world):
    out = _query([_sensitive_hit(owners=["1.2.7"]), _clean_hit()], structured=True)
    assert out["ids"] == [CHASH_C]
    assert out["sensitive_withheld"] == 1
    assert CANARY not in repr(out)


def test_query_flag_shows_the_content_badge_and_clause(_world):
    _set_mcp_output(_world, "flag")
    out = _query([_sensitive_hit(owners=["1.2.7"]), _clean_hit()])
    assert CANARY in out and _has_badge(out)
    assert SENSITIVE_READER_CLAUSE in out


def test_query_with_no_sensitive_hit_is_unchanged(_world):
    out = _query([_clean_hit()])
    assert "[sensitive" not in out and "withheld" not in out and SENSITIVE_READER_CLAUSE not in out
    assert set(_query([_clean_hit()], structured=True)) == {
        "ids", "tumblers", "distances", "hybrid_scores", "collections", "chunk_collections",
        "also_in", "also_in_ids", "chunk_text_hash"}


# ── store_get ─────────────────────────────────────────────────────────────────


def _store_get(entry: dict, owners):
    t3 = _mock_t3(["knowledge__notes__test-model__v1"])
    t3.get_by_id.return_value = entry
    with patch("nexus.catalog.store_hook.live_owner_entries", return_value={CHASH_S: owners}), \
         patch("nexus.catalog.store_hook.split_note_text", return_value=None):
        return store_get(doc_id=CHASH_S, collection="knowledge__notes__test-model__v1")


def _entry(**extra) -> dict:
    return {"id": CHASH_S, "content": f"{CANARY} the secret body", "title": SECRET_TITLE,
            "tags": "secret-tag", **extra}


def _owner(tumbler: str, sensitive: bool):
    return MagicMock(tumbler=tumbler, sensitive=sensitive, title=SECRET_TITLE, tags="secret-tag",
                     indexed_at="", ttl_days=None, meta={})


def test_store_get_withhold_returns_the_stub_and_nothing_else_of_the_note(_world):
    out = _store_get(_entry(sensitive=True), owners=[_owner("1.9.4", True), _owner("1.2.7", True)])
    assert _stub("1.2.7") in out.splitlines()
    assert "1 sensitive hit withheld" in out
    for secret in (CANARY, SECRET_TITLE, "secret-tag"):
        assert secret not in out


def test_store_get_flag_returns_the_note_with_a_badge_and_clause(_world):
    _set_mcp_output(_world, "flag")
    out = _store_get(_entry(sensitive=True), owners=[_owner("1.2.7", True)])
    assert CANARY in out and _has_badge(out)
    assert SENSITIVE_READER_CLAUSE in out


def test_store_get_of_a_plain_note_is_unchanged(_world):
    out = _store_get(_entry(sensitive=False), owners=[_owner("1.2.7", False)])
    assert CANARY in out
    assert "[sensitive" not in out and SENSITIVE_READER_CLAUSE not in out


# ── store_get_many ────────────────────────────────────────────────────────────

_MANY = "knowledge__notes__test-model__v1"


def _store_get_many(*, structured: bool = False, sensitives=(True, False)):
    t3 = _mock_t3([_MANY])
    t3.get_or_create_collection.return_value.get.return_value = {
        "ids": [CHASH_S, CHASH_C],
        "documents": [f"{CANARY} the secret body", CLEAN_BODY],
        "metadatas": [{"title": SECRET_TITLE}, {"title": "Gardening"}],
        **({"sensitives": list(sensitives)} if sensitives is not None else {}),
    }
    with patch("nexus.doc_sensitivity.flagged_owners_of_chashes", return_value={CHASH_S: ["1.9.4", "1.2.7"]}):
        return store_get_many(ids=[CHASH_S, CHASH_C], collections=_MANY, structured=structured)


def test_store_get_many_withhold_text(_world):
    out = _store_get_many()
    assert _stub("1.2.7") in out.splitlines()
    assert "1 sensitive hit withheld" in out
    assert CANARY not in out and SECRET_TITLE not in out
    assert CLEAN_BODY in out


def test_store_get_many_withhold_structured_keeps_alignment_and_hides_the_text(_world):
    out = _store_get_many(structured=True)
    assert len(out["contents"]) == 2
    assert out["contents"][0] == _stub("1.2.7")
    assert out["contents"][1] == CLEAN_BODY
    assert out["sensitive_withheld"] == 1
    assert CANARY not in repr(out) and SECRET_TITLE not in repr(out)


def test_store_get_many_flag_shows_the_content_and_marks_it(_world):
    _set_mcp_output(_world, "flag")
    out = _store_get_many()
    assert CANARY in out and _has_badge(out)
    assert SENSITIVE_READER_CLAUSE in out
    structured = _store_get_many(structured=True)
    assert structured["sensitive"] == [True, False]


def test_store_get_many_with_no_sensitive_row_is_unchanged(_world):
    out = _store_get_many(sensitives=(False, False))
    assert CANARY in out and "[sensitive" not in out and "withheld" not in out
    structured = _store_get_many(structured=True, sensitives=(False, False))
    assert "sensitive_withheld" not in structured and "sensitive" not in structured


# ── the four scoped searches ──────────────────────────────────────────────────

_ROW_S = {"id": "1.2.7", "content": f"{CANARY} the secret body", "distance": 0.1, "collection": COLLECTION,
          "chash": CHASH_S, "sensitive": True}
_ROW_C = {"id": "1.3.1", "content": CLEAN_BODY, "distance": 0.2, "collection": COLLECTION,
          "chash": CHASH_C, "sensitive": False}


def _scoped_world():
    t3 = _mock_t3()
    for name in ("search_metadata_scoped", "search_topic_scoped", "search_graph_hop", "search_aspect_scoped"):
        getattr(t3, name).side_effect = lambda *a, **k: [dict(_ROW_S), dict(_ROW_C)]
    return t3


def _call_scoped(tool: str, **kw):
    _scoped_world()
    args = {
        "search_metadata_scoped": dict(query="q", corpus=COLLECTION),
        "search_topic_scoped": dict(query="q", topic="t", corpus=COLLECTION),
        "search_graph_hop": dict(query="q", seeds="1.1.1", corpus=COLLECTION),
        "search_aspect_scoped": dict(query="q", corpus=COLLECTION),
    }[tool]
    fn = {"search_metadata_scoped": search_metadata_scoped, "search_topic_scoped": search_topic_scoped,
          "search_graph_hop": search_graph_hop, "search_aspect_scoped": search_aspect_scoped}[tool]
    with patch("nexus.db.http_vector_client.is_service_backed", return_value=True), \
         patch("nexus.mcp.core._resolve_corpus_target", return_value=[COLLECTION]), \
         patch("nexus.mcp.core._grouped_combined_query", lambda target, call, t3=None: call(target)), \
         patch("nexus.doc_sensitivity.flagged_owners_of_chashes", return_value={CHASH_S: ["1.2.7"]}):
        return fn(**args, **kw)


SCOPED = ["search_metadata_scoped", "search_topic_scoped", "search_graph_hop", "search_aspect_scoped"]


@pytest.mark.parametrize("tool", SCOPED)
def test_scoped_search_withhold_text(_world, tool):
    out = _call_scoped(tool)
    assert _stub("1.2.7") in out.splitlines()
    assert "1 sensitive hit withheld" in out
    assert CANARY not in out
    assert CLEAN_BODY in out


@pytest.mark.parametrize("tool", SCOPED)
def test_scoped_search_withhold_structured(_world, tool):
    out = _call_scoped(tool, structured=True)
    assert CANARY not in repr(out)
    assert out["sensitive_withheld"] == 1
    assert len(out["ids"]) == len(out["contents"]) == 1


@pytest.mark.parametrize("tool", SCOPED)
def test_scoped_search_flag_shows_content_and_badge(_world, tool):
    _set_mcp_output(_world, "flag")
    out = _call_scoped(tool)
    assert CANARY in out and _has_badge(out)
    assert SENSITIVE_READER_CLAUSE in out


# ── query, catalog-param path (document-level rows from the combined-query functions) ────────────────────────


def _query_catalog(*, structured: bool = False):
    _scoped_world()
    cat = MagicMock()
    cat.resolve_many.return_value = {}
    cat.get_manifests.return_value = {}
    with patch("nexus.mcp.core._get_catalog", return_value=cat), \
         patch("nexus.db.http_vector_client.is_service_backed", return_value=True), \
         patch("nexus.mcp.core._resolve_corpus_target", return_value=[COLLECTION]), \
         patch("nexus.mcp.core._grouped_combined_query", lambda target, call, t3=None: call(target)), \
         patch("nexus.config.load_config", return_value={"search": {"query_sanitizer": False}}), \
         patch("nexus.doc_sensitivity.flagged_owners_of_chashes", return_value={CHASH_S: ["1.2.7"]}):
        return query(question="q", corpus=COLLECTION, content_type="paper", structured=structured)


def test_query_catalog_path_withhold_text(_world):
    out = _query_catalog()
    assert _stub("1.2.7") in out.splitlines()
    assert "1 sensitive hit withheld" in out
    assert CANARY not in out
    assert CLEAN_BODY in out
    assert SENSITIVE_READER_CLAUSE not in out


def test_query_catalog_path_withhold_structured(_world):
    out = _query_catalog(structured=True)
    assert out["ids"] == ["1.3.1"] and out["chunk_text_hash"] == [CHASH_C]
    assert out["sensitive_withheld"] == 1
    assert CANARY not in repr(out) and CHASH_S not in repr(out)


def test_query_catalog_path_flag_shows_content_badge_and_clause(_world):
    _set_mcp_output(_world, "flag")
    out = _query_catalog()
    assert CANARY in out and _has_badge(out) and SENSITIVE_READER_CLAUSE in out
