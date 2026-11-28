# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.66 fix round 1, item 2: what a person reads when the engine refuses a note's chunk keys.

The real flows, with only the engine's 422 faked: ``put_note`` -> ``failure_message`` and the two
producers that print it, ``nx store put`` and the MCP ``store_put`` tool. The write goes through a real
``HttpCatalogClient`` over an httpx ``MockTransport``, so the typed error reaches the note writer the way
it does in a run, wrapped in the writer's own failure handling.

What must hold: the keys and their homes are named; nothing says the write is safe to retry (a retry is
refused the same way); nothing claims the request left nothing behind (the engine has not shown that
yet, so the text says only that the engine reported the request refused); the sentence has no doubled
period; the action is named.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
from click.testing import CliRunner

import nexus.corpus as corpus

_COLLECTION = "knowledge__6pfgb66-note__bge-base-en-v15-768__v1"

_REFUSED = [
    {"key": "title", "class": "DOCUMENT", "home": "the catalog document"},
    {"key": "line_start", "class": "POSITION", "home": "the manifest row"},
]
_BODY = {
    "reason": "chunk_metadata_key_refused",
    "error": "chunk metadata names keys a chunk row does not carry: title, line_start",
    "route": "/v1/catalog/manifest/write_many",
    "collection": _COLLECTION,
    "collection_id": 7,
    "refused": _REFUSED,
    "refused_chunks": 1,
    "requested_count": 1,
}

_FORBIDDEN = ("retry is safe", "no chunk was left behind", "Nothing was written", "nothing was written", "..")


class _Engine:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, answer: httpx.Response) -> None:
        from nexus.catalog.http_catalog_client import HttpCatalogClient  # noqa: PLC0415 — test-local import

        self.writes = 0

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/manifest/write_many"):
                self.writes += 1
                return answer
            if request.url.path.endswith("/collections/get"):
                return httpx.Response(200, json={"name": _COLLECTION, "superseded_by": ""})
            return httpx.Response(200, json={})

        self.client = HttpCatalogClient(base_url="http://engine.test", tenant="t", _token="x")
        self.client._client = httpx.Client(transport=httpx.MockTransport(handler))
        reader = MagicMock()
        reader.get_manifest.return_value = []
        monkeypatch.setattr("nexus.catalog.factory.make_catalog_writer", lambda *a, **k: self.client)
        monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: reader)


@pytest.fixture(autouse=True)
def _note_collaborators(monkeypatch: pytest.MonkeyPatch):
    """Everything below the write that is not under test: registration, the fence, the reader, the row lookups."""
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()
    monkeypatch.setattr(corpus, "_profile_model_for_content_type", lambda _ct: None)
    monkeypatch.setattr("nexus.db.service_endpoint.guard_production_write", lambda _url: None)
    monkeypatch.setattr("nexus.db.t2._refreshable_client.guard_production_write", lambda _url: None)
    monkeypatch.setattr("nexus.db.serving_engine_version.require_note_uri_engine", lambda *a, **k: None)
    monkeypatch.setattr("nexus.catalog.note_write._register_collection_first", lambda *a, **k: None)
    monkeypatch.setattr("nexus.catalog.store_hook.catalog_store_hook_tracked", lambda **kw: ("1.1.1", True))
    monkeypatch.setattr("nexus.catalog.store_hook.rollback_minted_catalog_entry", lambda *a, **k: True)
    monkeypatch.setattr("nexus.doc_indexer._fence_begin", lambda *a, **k: None)
    monkeypatch.setattr("nexus.doc_indexer._fence_fail", lambda *a, **k: None)
    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", lambda name, **kw: None)
    yield
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()


def _assert_actionable(text: str) -> None:
    assert "title" in text and "the catalog document" in text
    assert "line_start" in text and "the manifest row" in text
    for bad in _FORBIDDEN:
        assert bad not in text, f"{bad!r} in: {text}"
    assert "will not help" in text or "refused the same way" in text
    assert "upgrade nx" in text.lower() and "report this message" in text.lower()


def test_put_note_words_a_key_refusal_without_the_retry_safe_tail(monkeypatch) -> None:
    from nexus.catalog.note_write import NOT_LANDED, failure_message, put_note  # noqa: PLC0415 — test-local import

    engine = _Engine(monkeypatch, httpx.Response(422, json=_BODY))
    outcome = put_note(content="a note body", collection=_COLLECTION, title="t")
    assert outcome.status == NOT_LANDED
    assert engine.writes == 1
    _assert_actionable(failure_message(outcome, subject="'t'", check="store_get") or "")


def test_a_different_engine_refusal_keeps_its_wording_and_loses_the_doubled_period(monkeypatch) -> None:
    """The doubled period came from a reason that already ends in one, joined with '. '."""
    from nexus.catalog.note_write import NOT_LANDED, failure_message, put_note  # noqa: PLC0415 — test-local import

    _Engine(monkeypatch, httpx.Response(422, json={"error": "the engine says no.", "reason": "other"}))
    outcome = put_note(content="a note body", collection=_COLLECTION, title="t")
    assert outcome.status == NOT_LANDED
    text = failure_message(outcome, subject="'t'") or ""
    assert "retry is safe" in text, "an unrelated engine refusal keeps the existing wording"
    assert ".." not in text


def test_nx_store_put_prints_the_refusal_as_one_clear_message(monkeypatch) -> None:
    from nexus.cli import main  # noqa: PLC0415 — test-local import

    engine = _Engine(monkeypatch, httpx.Response(422, json=_BODY))
    monkeypatch.setattr("nexus.commands.store._t3", lambda: MagicMock())
    monkeypatch.setattr("nexus.commands.store.t3_collection_name", lambda *a, **k: _COLLECTION)
    result = CliRunner().invoke(main, ["store", "put", "-", "--collection", "6pfgb66-subject", "--title", "t"], input="a note body\n")
    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert engine.writes == 1
    # the structured log line the writer emits shares the stream; the operator's message is the Error: block
    _assert_actionable(result.output[result.output.index("Error: "):])


def test_mcp_store_put_returns_the_refusal_as_one_clear_message(monkeypatch) -> None:
    from nexus.mcp import core  # noqa: PLC0415 — test-local import

    engine = _Engine(monkeypatch, httpx.Response(422, json=_BODY))
    monkeypatch.setattr(core, "_get_t3", lambda: MagicMock())
    monkeypatch.setattr(core, "t3_collection_name", lambda *a, **k: _COLLECTION)
    text = core.store_put(content="a note body", collection=_COLLECTION, title="t")
    assert text.startswith("Error: store_put:"), text
    assert engine.writes == 1
    _assert_actionable(text)
