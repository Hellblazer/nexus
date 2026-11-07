# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 3 (nexus-rfvyy.1): the other places a sensitive document's title reaches the session model.

``store_list`` prints each chunk's owning note's title (and, with ``docs=True``, each document's), and ``search``
prints a header above a cluster of rows that is the best member's catalog title (Ward clustering) or a topic label
(taxonomy). Under the default posture (``sensitivity.mcp_output: withhold``) a sensitive document's listing line is
the stub, and a header that is, or may be derived from, a withheld row is not printed.

Each test names a canary that lives only in the sensitive document's title and asserts it is absent from the WHOLE
response.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from nexus.catalog.store_hook import DocumentListing, group_documents
from nexus.context_annotations import SENSITIVE_READER_CLAUSE
from nexus.mcp.core import store_list
from nexus.mcp_server import _inject_t3, _reset_singletons, _search_render
from nexus.sensitive_output import mcp_output_posture
from nexus.types import SearchResult

CANARY = f"CANARY-rfvyy-1-{uuid4()}"
SECRET_TITLE = f"{CANARY} board minutes"
COLLECTION = "knowledge__test"
CHASH_S = "a" * 64
CHASH_C = "b" * 64


@pytest.fixture(autouse=True)
def _world(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "nexus-config"
    cfg_dir.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))
    monkeypatch.delenv("NX_SENSITIVITY_POSTURE", raising=False)
    _reset_singletons()
    yield cfg_dir
    _reset_singletons()


def _set_mcp_output(cfg_dir, value: str | None) -> None:
    (cfg_dir / "config.yml").write_text("" if value is None else f"sensitivity:\n  mcp_output: {value}\n")


# ── store_list, chunk view ────────────────────────────────────────────────────


def _list_t3(rows: list[dict]) -> MagicMock:
    t3 = MagicMock()
    t3.list_collections.return_value = [
        {"name": COLLECTION, "count": len(rows), "content_type": "knowledge", "owner_id": "1-1",
         "embedding_model": "test-model", "lifecycle_state": "live"},
    ]
    t3.collection_info.return_value = {"count": len(rows)}
    t3.list_store.return_value = rows
    _inject_t3(t3)
    return t3


def _owner(tumbler: str, title: str, *, sensitive: bool) -> SimpleNamespace:
    return SimpleNamespace(tumbler=tumbler, title=title, sensitive=sensitive, meta={}, indexed_at="2026-11-01",
                           alias_of="")


def _chunk_rows() -> list[dict]:
    return [
        {"id": CHASH_C, "title": "Gardening", "indexed_at": "2026-11-01"},
        {"id": CHASH_S, "title": SECRET_TITLE, "indexed_at": "2026-11-01"},
    ]


def _owners_for_chunks() -> dict[str, list]:
    return {
        CHASH_C: [_owner("1.3.1", "Gardening", sensitive=False)],
        CHASH_S: [_owner("1.2.7", SECRET_TITLE, sensitive=True)],
    }


def test_store_list_chunk_view_withholds_a_sensitive_owners_title():
    _list_t3(_chunk_rows())
    with patch("nexus.catalog.store_hook.live_owner_entries", return_value=_owners_for_chunks()):
        out = store_list(collection=COLLECTION)
    assert "[sensitive] 1.2.7: content withheld" in out
    assert "1 sensitive hit withheld" in out
    assert CHASH_C in out and "Gardening" in out, "the clean row is listed as it always was"
    assert CHASH_S not in out
    assert CANARY not in out


def test_store_list_chunk_view_flag_shows_the_title_with_a_badge_and_the_clause(_world):
    _set_mcp_output(_world, "flag")
    _list_t3(_chunk_rows())
    with patch("nexus.catalog.store_hook.live_owner_entries", return_value=_owners_for_chunks()):
        out = store_list(collection=COLLECTION)
    assert SECRET_TITLE[:40] in out and CHASH_S in out
    assert SENSITIVE_READER_CLAUSE in out
    assert "withheld" not in out


def test_store_list_chunk_view_of_clean_chunks_is_unchanged():
    _list_t3([_chunk_rows()[0]])
    with patch("nexus.catalog.store_hook.live_owner_entries",
               return_value={CHASH_C: _owners_for_chunks()[CHASH_C]}):
        out = store_list(collection=COLLECTION)
    assert "sensitive" not in out and "Gardening" in out


# ── store_list, document view ─────────────────────────────────────────────────


def _document_rows(*, sensitive_first: bool) -> list[DocumentListing]:
    clean = DocumentListing(key="1.3.1", title="Gardening", handle=CHASH_C, chunks=2, entry={}, indexed_at="2026-11-01")
    secret = DocumentListing(key="1.2.7", title=SECRET_TITLE, handle=CHASH_S, chunks=3, entry={},
                             indexed_at="2026-11-01", sensitive=True)
    # group_documents sorts by title, so the sensitive title sorts first here ("CANARY" < "Gardening").
    return [secret, clean] if sensitive_first else [clean, secret]


def test_store_list_docs_view_stubs_a_sensitive_document_and_moves_it_after_the_clean_ones():
    _list_t3(_chunk_rows())
    with patch("nexus.catalog.store_hook.group_documents", return_value=(_document_rows(sensitive_first=True), "")):
        out = store_list(collection=COLLECTION, docs=True)
    lines = out.splitlines()
    clean_at = next(i for i, ln in enumerate(lines) if "Gardening" in ln)
    stub_at = next(i for i, ln in enumerate(lines) if "[sensitive] 1.2.7: content withheld" in ln)
    assert clean_at < stub_at, "the stub does not hold the title's sorted position"
    assert "1 sensitive hit withheld" in out
    assert CHASH_S not in out and CANARY not in out


def test_group_documents_marks_the_documents_the_catalog_flags(monkeypatch):
    """The seam: ``group_documents`` takes the flag from the catalog listing, not from the chunk rows."""
    t3 = MagicMock()
    t3.list_store.side_effect = [[{"id": CHASH_C}, {"id": CHASH_S}], []]
    index = (
        {CHASH_C: ("1.3.1",), CHASH_S: ("1.2.7",)},
        {"1.3.1": "Gardening", "1.2.7": SECRET_TITLE},
        {"1.3.1": CHASH_C, "1.2.7": CHASH_S},
        {"1.3.1": "", "1.2.7": ""},
        "",
    )
    monkeypatch.setattr("nexus.catalog.store_hook._manifest_doc_index_full", lambda _c: index)
    monkeypatch.setattr(
        "nexus.doc_sensitivity.flagged_in_collection",
        lambda _c, **_k: [SimpleNamespace(tumbler="1.2.7", sensitive=True)],
    )
    rows, degraded = group_documents(t3, COLLECTION, 2)
    assert degraded == ""
    assert {r.key: r.sensitive for r in rows} == {"1.3.1": False, "1.2.7": True}


def test_group_documents_treats_every_document_as_sensitive_when_the_catalog_cannot_answer(monkeypatch):
    from nexus import doc_sensitivity

    t3 = MagicMock()
    t3.list_store.side_effect = [[{"id": CHASH_C}], []]
    index = ({CHASH_C: ("1.3.1",)}, {"1.3.1": "Gardening"}, {"1.3.1": CHASH_C}, {"1.3.1": ""}, "")
    monkeypatch.setattr("nexus.catalog.store_hook._manifest_doc_index_full", lambda _c: index)

    def unreadable(_c, **_k):
        raise doc_sensitivity.SensitivityUnknown("catalog down")

    monkeypatch.setattr("nexus.doc_sensitivity.flagged_in_collection", unreadable)
    (row,) = group_documents(t3, COLLECTION, 1)[0]
    assert row.sensitive is True


# ── search cluster headers ────────────────────────────────────────────────────


def _hit(chash: str, *, sensitive: bool, title: str, distance: float, **meta) -> SearchResult:
    return SearchResult(
        id=chash, content="a body", distance=distance, collection=COLLECTION,
        metadata={"sensitive": sensitive, "chunk_text_hash": chash, "doc_id": "1.2.7" if sensitive else "1.3.1",
                  "_display_title": title, "_display_path": f"{title}.md", "title": title, **meta},
    )


def _render(hits: list[SearchResult]) -> str:
    t3 = MagicMock()
    t3.list_collections.return_value = [
        {"name": COLLECTION, "count": 3, "content_type": "knowledge", "owner_id": "1-1",
         "embedding_model": "test-model", "lifecycle_state": "live"},
    ]
    _inject_t3(t3)

    def fake(*_a, **_kw):
        return [SearchResult(id=h.id, content=h.content, distance=h.distance, collection=h.collection,
                             metadata=dict(h.metadata)) for h in hits]

    with patch("nexus.search_engine.search_cross_corpus", fake), \
         patch("nexus.config.load_config", return_value={"search": {"query_sanitizer": False}}), \
         patch("nexus.mcp.core._get_t1", side_effect=RuntimeError("no T1 in this test")), \
         patch("nexus.mcp.core._page_cache_get", return_value=None), \
         patch("nexus.mcp.core._page_cache_put"):
        return _search_render(query="headers", corpus=COLLECTION, cluster_by="semantic", structured=False,
                              limit=10, sensitivity="include")


def test_a_ward_header_that_is_a_withheld_rows_title_is_not_printed():
    """Defense in depth: the label of a cluster whose best member is withheld names that member's title."""
    out = _render([
        _hit(CHASH_S, sensitive=True, title=SECRET_TITLE, distance=0.1, _cluster_label=SECRET_TITLE),
        _hit(CHASH_C, sensitive=False, title="Gardening", distance=0.2, _cluster_label=SECRET_TITLE),
    ])
    assert "── label withheld ──" in out
    assert "Gardening" in out
    assert CANARY not in out


def test_a_topic_header_beside_a_withheld_row_is_not_printed():
    out = _render([
        _hit(CHASH_S, sensitive=True, title=SECRET_TITLE, distance=0.1, _topic_label=f"{CANARY} minutes topic"),
        _hit(CHASH_C, sensitive=False, title="Gardening", distance=0.2, _topic_label=f"{CANARY} minutes topic"),
    ])
    assert "── label withheld ──" in out
    assert CANARY not in out


def test_a_header_no_withheld_row_shares_is_printed():
    out = _render([
        _hit(CHASH_S, sensitive=True, title=SECRET_TITLE, distance=0.1, _topic_label="topic one"),
        _hit(CHASH_C, sensitive=False, title="Gardening", distance=0.2, _topic_label="topic two"),
    ])
    assert "── topic two ──" in out and "topic one" not in out


def test_flag_posture_prints_the_header(_world):
    _set_mcp_output(_world, "flag")
    assert mcp_output_posture() == "flag"
    out = _render([
        _hit(CHASH_S, sensitive=True, title=SECRET_TITLE, distance=0.1, _cluster_label=SECRET_TITLE),
        _hit(CHASH_C, sensitive=False, title="Gardening", distance=0.2, _cluster_label=SECRET_TITLE),
    ])
    assert f"── {SECRET_TITLE} ──" in out


def test_relabel_names_a_cluster_from_its_best_clean_member():
    """The source of the leak: the label was the lowest-distance member's title, sensitive or not."""
    from nexus.search_engine import _CLUSTER_INDEX_KEY, _relabel_clusters

    results = [
        _hit(CHASH_S, sensitive=True, title=SECRET_TITLE, distance=0.1, **{_CLUSTER_INDEX_KEY: 0}),
        _hit(CHASH_C, sensitive=False, title="Gardening", distance=0.2, **{_CLUSTER_INDEX_KEY: 0}),
    ]
    _relabel_clusters(results)
    assert {r.metadata["_cluster_label"] for r in results} == {"Gardening"}


def test_relabel_of_an_all_sensitive_cluster_still_labels_it():
    from nexus.search_engine import _CLUSTER_INDEX_KEY, _relabel_clusters

    results = [_hit(CHASH_S, sensitive=True, title=SECRET_TITLE, distance=0.1, **{_CLUSTER_INDEX_KEY: 0})]
    _relabel_clusters(results)
    assert results[0].metadata["_cluster_label"] == SECRET_TITLE
