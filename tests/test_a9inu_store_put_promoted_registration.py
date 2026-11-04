# SPDX-License-Identifier: AGPL-3.0-or-later
"""MCP ``store_put`` registers a promoted voyage-named collection under its promoted name (nexus-a9inu).

``tests/test_mcp_server.py::test_no_legacy_collection_promotes_to_conformant`` pins the promoted NAME
the store path hands its two sinks, but that module replaces ``write_note`` with a double that reaches
no registrar, and ``tests/test_z0o2p12_note_write.py`` uses bge names only. Nothing pinned the
registration itself: a ``store_put`` that handed a short or wrong-token name to the write would have
passed every test.

This test drives ``store_put`` under ``cloud_mode`` on a greenfield subject and lets the REAL
``put_note`` -> ``write_note`` -> ``HttpCatalogClient.write_manifest_many`` ->
``ensure_collection_registered`` chain run against a fake engine (``httpx.MockTransport``). The fake
records the ``register_collection`` request and refuses the manifest write that follows it, so nothing
lands anywhere. Only the catalog document registration and the index-run fence are stood in for: they
sit before the write and are not its subject.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from unittest.mock import MagicMock

import httpx
import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.mcp_server import _inject_t3, _reset_singletons, store_put

pytestmark = pytest.mark.usefixtures("cloud_mode")

SUBJECT = "knowledge__greenfield"
PROMOTED = "knowledge__greenfield__voyage-context-3__v1"


def _is_registration(request: httpx.Request) -> bool:
    return request.method == "POST" and request.url.path == "/v1/catalog/collections/upsert"


@pytest.fixture()
def fake_engine(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """A catalog writer over a fake engine; returns the requests it received, in order."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if _is_registration(request):
            return httpx.Response(200, json=json.loads(request.content))
        if request.url.path == "/v1/catalog/embedding_profile":
            return httpx.Response(200, json={"profile": [], "count": 0})  # an unprofiled tenant: no model pinned
        if request.url.path == "/v1/catalog/collections/get":
            return httpx.Response(404, json={"error": "not found"})  # greenfield: nothing registered yet
        # Every other call (the manifest write first among them) is refused, so the note never lands
        # and the store path's own rollback runs; the test reads only what was registered.
        return httpx.Response(400, json={"error": "fake engine: not served (nexus-a9inu test)"})

    def _writer(**_kw: object) -> HttpCatalogClient:
        cat = HttpCatalogClient(base_url="http://a9inu.fake", _token="t")
        cat._client = httpx.Client(base_url="http://a9inu.fake", transport=httpx.MockTransport(handler))
        return cat

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_writer", _writer)
    # The registration's profile check reads through the ambient reader: the same fake engine.
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", _writer)
    return seen


@pytest.fixture()
def greenfield_store_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No collection exists yet; the catalog document registration and the fence are stood in for."""
    _reset_singletons()
    t3 = MagicMock()
    t3.list_collections.return_value = []
    _inject_t3(t3)
    monkeypatch.setattr("nexus.db.serving_engine_version.require_note_uri_engine", lambda: None)
    monkeypatch.setattr(
        "nexus.catalog.store_hook.catalog_store_hook_tracked", lambda **_kw: ("9.9.9", True),
    )
    monkeypatch.setattr("nexus.catalog.store_hook.rollback_minted_catalog_entry", lambda *a, **kw: True)
    monkeypatch.setattr("nexus.doc_indexer._fence_begin", lambda *a, **kw: None)
    monkeypatch.setattr("nexus.doc_indexer._fence_fail", lambda *a, **kw: None)
    yield
    _reset_singletons()


def test_store_put_registers_the_promoted_voyage_collection(fake_engine, greenfield_store_path) -> None:
    result = store_put(content="Greenfield content", collection=SUBJECT, title="greenfield-doc")

    registrations = [json.loads(r.content) for r in fake_engine if _is_registration(r)]
    assert len(registrations) == 1, (
        f"the store path registers its collection exactly once before the write; result={result!r}, "
        f"requests={[(r.method, r.url.path) for r in fake_engine]}"
    )
    body = registrations[0]
    assert body.get("name") == PROMOTED, body
    assert body.get("embedding_model") == "voyage-context-3", body
    assert body.get("content_type") == "knowledge", body
    # The registration comes before the manifest write it guards.
    first_write = next(
        (i for i, r in enumerate(fake_engine) if not _is_registration(r) and r.method == "POST"), None,
    )
    first_reg = next(i for i, r in enumerate(fake_engine) if _is_registration(r))
    assert first_write is None or first_reg < first_write, [(r.method, r.url.path) for r in fake_engine]
