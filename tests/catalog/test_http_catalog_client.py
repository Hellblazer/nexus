# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-152 bead nexus-gmiaf.18 (P2.7): HttpCatalogClient + factory seam tests.

Tests:
1. _resolve_config raises cleanly when NX_SERVICE_PORT/TOKEN are absent
2. Constructor produces correct base_url + headers from override args
3. Each major category of HTTP verbs (GET/POST/DELETE) routes correctly
4. Factory seam: make_catalog_reader returns HttpCatalogClient when env set
5. Factory seam: make_catalog_writer returns _ServiceCatalogWriter when env set
6. _ServiceCatalogWriter enforces CATALOG_WRITE_OPS whitelist
7. Guarded methods raise NotImplementedError (rebuild, defrag, compact, sync, pull)
8. Fake server round-trip for register + resolve + link + links_from
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from unittest import mock

import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient, _resolve_config
from nexus.daemon.catalog_write_shim import CATALOG_WRITE_OPS


# ── helpers ───────────────────────────────────────────────────────────────────

def _fake_tumbler() -> str:
    return "1.1.1"


def _entry_dict(**kwargs: Any) -> dict:
    """Minimal server response dict that _to_entry accepts."""
    base = {
        "tumbler": _fake_tumbler(),
        "title": "Test Doc",
        "content_type": "paper",
        "chunk_count": 0,
    }
    base.update(kwargs)
    return base


class FakeCatalogHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler for catalog endpoint round-trip tests."""

    def log_message(self, *args: Any) -> None:
        pass  # suppress test noise

    def _send_json(self, body: Any, code: int = 200) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return json.loads(self.rfile.read(length))
        return {}

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path.endswith("/stats"):
            self._send_json({"doc_count": 7, "link_count": 3, "owner_count": 2})
        elif path.startswith("/v1/catalog/show/"):
            self._send_json(_entry_dict())
        elif path.startswith("/v1/catalog/search"):
            self._send_json({"documents": [_entry_dict()]})
        elif path.startswith("/v1/catalog/documents"):
            self._send_json({"documents": [_entry_dict(), _entry_dict(title="Second")]})
        elif path.startswith("/v1/catalog/links_from"):
            self._send_json({"links": [{"from_tumbler": "1.1.1", "to_tumbler": "1.1.2", "link_type": "cites"}]})
        elif path.startswith("/v1/catalog/links_to"):
            self._send_json({"links": []})
        elif path.startswith("/v1/catalog/manifest/") and path.endswith("/chashes"):
            self._send_json({"chashes": ["abc123", "def456"]})
        elif "/v1/catalog/manifest/" in path:
            self._send_json({"chunks": [{"position": 0, "chash": "abc123"}]})
        elif path.startswith("/v1/catalog/collections"):
            self._send_json({"collections": [{"name": "code__test__voyage-code-3__v1"}]})
        elif path.startswith("/v1/catalog/by_owner"):
            self._send_json({"documents": [_entry_dict()]})
        elif path.startswith("/v1/catalog/by_source_uri"):
            self._send_json(_entry_dict(source_uri="file:///tmp/a.md"))
        elif path.startswith("/v1/catalog/traverse"):
            self._send_json({"nodes": [], "edges": []})
        else:
            self._send_json({"error": f"unknown path: {path}"}, 404)

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        _ = self._read_body()
        if path == "/v1/catalog/register":
            self._send_json({"tumbler": _fake_tumbler()})
        elif path == "/v1/catalog/link":
            self._send_json({"created": True})
        elif path == "/v1/catalog/unlink":
            self._send_json({"deleted": True})
        elif path == "/v1/catalog/owners/register":
            self._send_json({"tumbler": "1.1"})
        elif path == "/v1/catalog/owners/ensure":
            self._send_json({"tumbler": "1.1"})
        elif path == "/v1/catalog/update":
            self._send_json({"ok": True})
        elif path == "/v1/catalog/manifest/write":
            self._send_json({"ok": True})
        elif path == "/v1/catalog/manifest/atomic_replace":
            self._send_json({"ok": True})
        elif path == "/v1/catalog/manifest/resync":
            self._send_json({"ok": True})
        elif path == "/v1/catalog/collections/register":
            self._send_json({"ok": True})
        elif path == "/v1/catalog/collections/supersede":
            self._send_json({"updated": 5})
        elif path == "/v1/catalog/collections/rename":
            self._send_json({"updated": 3})
        elif path == "/v1/catalog/bulk_unlink":
            self._send_json({"deleted": 2})
        elif path == "/v1/catalog/documents/update_collection_batch":
            self._send_json({"updated": 4})
        else:
            self._send_json({"ok": True})

    def do_DELETE(self) -> None:
        path = self.path.split("?")[0]
        if "/v1/catalog/documents/" in path:
            self._send_json({"deleted": True})
        elif "/v1/catalog/collections/" in path:
            self._send_json({"deleted": True})
        else:
            self._send_json({"deleted": True})


def start_fake_server() -> tuple[HTTPServer, str]:
    """Start a local fake catalog HTTP server; return (server, base_url)."""
    server = HTTPServer(("127.0.0.1", 0), FakeCatalogHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    # Brief wait for server to be ready
    time.sleep(0.05)
    return server, f"http://127.0.0.1:{port}"


# ── _resolve_config tests ──────────────────────────────────────────────────────

class TestResolveConfig:
    def test_missing_port_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NX_SERVICE_PORT", raising=False)
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        with pytest.raises(RuntimeError, match="NX_SERVICE_PORT"):
            _resolve_config()

    def test_non_integer_port_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NX_SERVICE_PORT", "not_a_port")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        with pytest.raises(RuntimeError, match="NX_SERVICE_PORT must be an integer"):
            _resolve_config()

    def test_missing_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NX_SERVICE_PORT", "9090")
        monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="NX_SERVICE_TOKEN"):
            _resolve_config()

    def test_valid_config_returns_tuple(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NX_SERVICE_HOST", "10.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9090")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok123")
        host, port, token = _resolve_config()
        assert host == "10.0.0.1"
        assert port == 9090
        assert token == "tok123"

    def test_default_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NX_SERVICE_HOST", raising=False)
        monkeypatch.setenv("NX_SERVICE_PORT", "9090")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        host, _, _ = _resolve_config()
        assert host == "127.0.0.1"


# ── HttpCatalogClient round-trip tests ─────────────────────────────────────────

@pytest.fixture(scope="module")
def fake_server():
    server, url = start_fake_server()
    yield url
    server.shutdown()


@pytest.fixture
def client(fake_server: str):
    with HttpCatalogClient(
        base_url=fake_server,
        tenant="tenant_abc",
        _token="test_tok",
    ) as c:
        yield c


class TestHttpCatalogClientRoundTrip:
    def test_is_initialized(self, client: HttpCatalogClient) -> None:
        assert client.is_initialized() is True

    def test_stats(self, client: HttpCatalogClient) -> None:
        s = client.stats()
        assert s["doc_count"] == 7

    def test_register_returns_tumbler(self, client: HttpCatalogClient) -> None:
        from nexus.catalog.catalog import Tumbler
        t = client.register(
            owner="1.1",
            title="My Paper",
            content_type="paper",
        )
        assert isinstance(t, Tumbler)

    def test_resolve_returns_entry(self, client: HttpCatalogClient) -> None:
        entry = client.resolve("1.1.1")
        assert entry is not None
        assert entry.title == "Test Doc"

    def test_find_returns_list(self, client: HttpCatalogClient) -> None:
        results = client.find("test query")
        assert len(results) >= 1
        assert results[0].title == "Test Doc"

    def test_link_returns_dict(self, client: HttpCatalogClient) -> None:
        result = client.link("1.1.1", "1.1.2", "cites")
        assert isinstance(result, dict)

    def test_links_from(self, client: HttpCatalogClient) -> None:
        links = client.links_from("1.1.1")
        assert len(links) == 1
        assert links[0]["link_type"] == "cites"

    def test_links_to(self, client: HttpCatalogClient) -> None:
        links = client.links_to("1.1.2")
        assert links == []

    def test_all_documents(self, client: HttpCatalogClient) -> None:
        docs = client.all_documents()
        assert len(docs) == 2

    def test_register_owner(self, client: HttpCatalogClient) -> None:
        from nexus.catalog.catalog import Tumbler
        t = client.register_owner(name="acme")
        assert isinstance(t, Tumbler)

    def test_ensure_owner_for_repo(self, client: HttpCatalogClient) -> None:
        from nexus.catalog.catalog import Tumbler
        t = client.ensure_owner_for_repo(repo="/tmp/myrepo")
        assert isinstance(t, Tumbler)

    def test_delete_document(self, client: HttpCatalogClient) -> None:
        result = client.delete_document("1.1.1")
        assert result is True

    def test_get_manifest(self, client: HttpCatalogClient) -> None:
        chunks = client.get_manifest("1.1.1")
        assert len(chunks) == 1

    def test_get_chunk_chashes(self, client: HttpCatalogClient) -> None:
        chashes = client.get_chunk_chashes("1.1.1")
        assert "abc123" in chashes

    def test_list_collections(self, client: HttpCatalogClient) -> None:
        colls = client.list_collections()
        assert len(colls) == 1

    def test_supersede_collection(self, client: HttpCatalogClient) -> None:
        n = client.supersede_collection("old__coll", superseded_by="new__coll")
        assert n == 5

    def test_rename_collection(self, client: HttpCatalogClient) -> None:
        n = client.rename_collection("old__coll", "new__coll")
        assert n == 3

    def test_bulk_unlink(self, client: HttpCatalogClient) -> None:
        n = client.bulk_unlink(link_type="cites")
        assert n == 2

    def test_update_documents_collection_batch(self, client: HttpCatalogClient) -> None:
        n = client.update_documents_collection_batch(["1.1.1", "1.1.2"], "new__coll")
        assert n == 4

    def test_by_owner(self, client: HttpCatalogClient) -> None:
        docs = client.by_owner("1.1")
        assert len(docs) == 1

    def test_by_source_uri(self, client: HttpCatalogClient) -> None:
        entry = client.by_source_uri("file:///tmp/a.md")
        assert entry is not None

    def test_graph(self, client: HttpCatalogClient) -> None:
        # Just verifies it doesn't crash (fake server returns {} for /traverse)
        result = client.graph("1.1.1")
        assert isinstance(result, dict)

    def test_doc_count(self, client: HttpCatalogClient) -> None:
        assert client.doc_count() == 7


# ── Guarded methods ───────────────────────────────────────────────────────────

class TestGuardedMethods:
    def test_rebuild_raises(self, client: HttpCatalogClient) -> None:
        with pytest.raises(NotImplementedError, match="rebuild"):
            client.rebuild()

    def test_defrag_raises(self, client: HttpCatalogClient) -> None:
        with pytest.raises(NotImplementedError, match="defrag"):
            client.defrag()

    def test_compact_raises(self, client: HttpCatalogClient) -> None:
        with pytest.raises(NotImplementedError, match="compact"):
            client.compact()

    def test_sync_raises(self, client: HttpCatalogClient) -> None:
        with pytest.raises(NotImplementedError, match="sync"):
            client.sync()

    def test_pull_raises(self, client: HttpCatalogClient) -> None:
        with pytest.raises(NotImplementedError, match="pull"):
            client.pull()

    def test_rebuild_if_stale_noop(self, client: HttpCatalogClient) -> None:
        # Must NOT raise
        client.rebuild_if_stale()

    def test_catalog_path_is_none(self, client: HttpCatalogClient) -> None:
        assert client.catalog_path is None


# ── Factory seam tests ────────────────────────────────────────────────────────

class TestFactorySeam:
    def test_make_catalog_reader_service_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        from nexus.catalog.factory import make_catalog_reader

        reader = make_catalog_reader()
        assert isinstance(reader, HttpCatalogClient)
        reader.close()

    def test_make_catalog_reader_sqlite_mode_returns_none_when_uninit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "sqlite")
        monkeypatch.delenv("NX_STORAGE_BACKEND", raising=False)
        # Point catalog_path to an empty dir
        monkeypatch.setattr(
            "nexus.config.catalog_path", lambda: tmp_path / "catalog"
        )
        from nexus.catalog.factory import make_catalog_reader

        result = make_catalog_reader()
        assert result is None

    def test_make_catalog_writer_service_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        from nexus.catalog.factory import _ServiceCatalogWriter, make_catalog_writer

        writer = make_catalog_writer()
        assert isinstance(writer, _ServiceCatalogWriter)
        assert writer.routed is True
        writer.close()

    def test_service_catalog_writer_whitelist_enforced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        from nexus.catalog.factory import make_catalog_writer

        writer = make_catalog_writer()
        # A read method not in whitelist must raise
        with pytest.raises(AttributeError, match="not a catalog write op"):
            _ = writer.resolve  # read op — blocked
        writer.close()

    def test_service_catalog_writer_whitelist_allows_write_ops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        from nexus.catalog.factory import make_catalog_writer

        writer = make_catalog_writer()
        # Every whitelisted write op must be accessible (not raise AttributeError)
        for op in CATALOG_WRITE_OPS:
            attr = getattr(writer, op, None)
            assert attr is not None, f"write op {op!r} missing from _ServiceCatalogWriter"
        writer.close()

    def test_no_production_bypass_of_factory(self) -> None:
        """Assert that no Python source file in src/ bare-constructs HttpCatalogClient
        outside of factory.py (seam audit).
        """
        import subprocess
        result = subprocess.run(
            ["grep", "-rn", "HttpCatalogClient(", "--include=*.py", "src/"],
            cwd="/Users/hal.hildebrand/git/nexus",
            capture_output=True, text=True,
        )
        hits = [
            line for line in result.stdout.splitlines()
            if "factory.py" not in line
            and "http_catalog_client.py" not in line
        ]
        assert hits == [], (
            "Production code constructs HttpCatalogClient directly, bypassing factory:\n"
            + "\n".join(hits)
        )
