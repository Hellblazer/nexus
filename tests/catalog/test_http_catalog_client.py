# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-152 bead nexus-gmiaf.18 (P2.7): HttpCatalogClient + factory seam tests.

Tests:
1. _resolve_config raises cleanly when NX_SERVICE_PORT/TOKEN are absent
2. Constructor produces correct base_url + headers from override args
3. Each major category of HTTP verbs (GET/POST) routes correctly
4. Factory seam: make_catalog_reader returns HttpCatalogClient when env set
5. Factory seam: make_catalog_writer returns _ServiceCatalogWriter when env set
6. _ServiceCatalogWriter enforces CATALOG_WRITE_OPS whitelist
7. Guarded methods raise NotImplementedError (rebuild, defrag, compact, sync, pull)
8. Fake server round-trip exercising the REAL routes from CatalogHandler

Route alignment verified against CatalogHandler.java switch cases (bead nexus-gmiaf.18).
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

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
    """Routes matching the real CatalogHandler.java switch cases exactly."""

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

    def _query_params(self) -> dict[str, str]:
        qs = urlparse(self.path).query
        return {k: v[0] for k, v in parse_qs(qs).items()} if qs else {}

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        op = path.removeprefix("/v1/catalog")

        if op == "/stats":
            self._send_json({"doc_count": 7, "link_count": 3, "owner_count": 2})
        elif op == "/show":
            self._send_json(_entry_dict())
        elif op == "/list":
            self._send_json({"documents": [_entry_dict(), _entry_dict(title="Second")], "count": 2})
        elif op == "/search":
            self._send_json({"documents": [_entry_dict()], "count": 1})
        elif op == "/resolve":
            self._send_json({"documents": [_entry_dict()]})
        elif op == "/links":
            params = self._query_params()
            direction = params.get("direction", "both")
            if direction == "out":
                self._send_json({"links_from": [{"from_tumbler": "1.1.1", "to_tumbler": "1.1.2", "link_type": "cites"}], "links_to": []})
            elif direction == "in":
                self._send_json({"links_from": [], "links_to": []})
            else:
                self._send_json({"links_from": [], "links_to": []})
        elif op == "/link_query":
            self._send_json({"links": [{"from_tumbler": "1.1.1", "to_tumbler": "1.1.2", "link_type": "cites"}], "count": 1})
        elif op == "/manifest/get":
            self._send_json({"rows": [{"position": 0, "chash": "abc123"}], "count": 1})
        elif op == "/manifest/chashes":
            self._send_json({"chashes": ["abc123", "def456"]})
        elif op == "/collections/list":
            self._send_json({"collections": [{"name": "code__test__voyage-code-3__v1"}]})
        elif op == "/collections/get":
            self._send_json({"name": "code__test__voyage-code-3__v1"})
        elif op == "/collections/for_tuple":
            self._send_json({"name": "code__test__voyage-code-3__v1"})
        elif op == "/owners/list":
            self._send_json({"owners": [{"tumbler_prefix": "1.1", "name": "myrepo"}]})
        elif op == "/owners/by_repo":
            self._send_json({"tumbler_prefix": "1.1", "name": "myrepo"})
        elif op == "/owners/by_name":
            self._send_json({"owners": [{"tumbler_prefix": "1.1", "name": "myrepo"}]})
        else:
            self._send_json({"error": f"unknown GET op: {op}"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        op = path.removeprefix("/v1/catalog")
        body = self._read_body()

        if op == "/doc/register":
            self._send_json({"tumbler": _fake_tumbler()})
        elif op == "/register":
            self._send_json({"ok": True})
        elif op == "/update":
            self._send_json({"updated": 1})
        elif op == "/delete":
            self._send_json({"deleted": 1})
        elif op == "/link":
            self._send_json({"ok": True})
        elif op == "/unlink":
            self._send_json({"deleted": 1})
        elif op == "/traverse":
            self._send_json({"nodes": [_entry_dict()], "edges": [{"from_tumbler": "1.1.1", "to_tumbler": "1.1.2", "link_type": "cites"}]})
        elif op == "/manifest/write":
            self._send_json({"ok": True, "count": len(body.get("rows", []))})
        elif op == "/manifest/append":
            self._send_json({"ok": True, "count": len(body.get("rows", []))})
        elif op == "/manifest/purge":
            self._send_json({"deleted": 1})
        elif op == "/manifest/docs_for_chashes":
            # Real server: {"tumblers": [tumbler_string, ...]} (flat list, SELECT DISTINCT)
            self._send_json({"tumblers": ["1.1.1"]})
        elif op == "/owners/upsert":
            self._send_json({"ok": True})
        elif op == "/owners/head_hash":
            self._send_json({"updated": 1})
        elif op == "/collections/upsert":
            self._send_json({"ok": True})
        elif op == "/collections/supersede":
            self._send_json({"updated": 5})
        elif op == "/collections/rename":
            self._send_json({"updated": 3})
        elif op == "/import/owner":
            self._send_json({"imported": 1})
        elif op == "/import/document":
            self._send_json({"imported": 1})
        elif op == "/import/link":
            self._send_json({"imported": 1})
        else:
            self._send_json({"ok": True})


def start_fake_server() -> tuple[HTTPServer, str]:
    """Start a local fake catalog HTTP server; return (server, base_url)."""
    server = HTTPServer(("127.0.0.1", 0), FakeCatalogHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.05)  # brief wait for the thread to reach serve_forever
    return server, f"http://127.0.0.1:{port}"


# ── _resolve_config tests ─────────────────────────────────────────────────────

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


# ── HttpCatalogClient round-trip tests ───────────────────────────────────────

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

    def test_doc_count(self, client: HttpCatalogClient) -> None:
        assert client.doc_count() == 7

    def test_register_returns_tumbler(self, client: HttpCatalogClient) -> None:
        from nexus.catalog.catalog import Tumbler
        # Positional owner+title as in Catalog.register signature
        t = client.register("1.1", "My Paper", content_type="paper")
        assert isinstance(t, Tumbler)
        assert str(t) == "1.1.1"

    def test_register_no_bib_fields(self, client: HttpCatalogClient) -> None:
        """register() must NOT accept bib_year/bib_authors — CatalogEntry has none."""
        import inspect
        from nexus.catalog.http_catalog_client import HttpCatalogClient as HCC
        sig = inspect.signature(HCC.register)
        assert "bib_year" not in sig.parameters
        assert "bib_authors" not in sig.parameters

    def test_resolve_returns_entry(self, client: HttpCatalogClient) -> None:
        entry = client.resolve("1.1.1")
        assert entry is not None
        assert entry.title == "Test Doc"

    def test_resolve_404_returns_none(self, monkeypatch: pytest.MonkeyPatch, fake_server: str) -> None:
        """resolve() must return None (not raise) for 404."""
        import httpx
        with HttpCatalogClient(base_url=fake_server, _token="test_tok") as c:
            # Patch _get to simulate a 404
            def _fake_get(path, **params):
                resp = httpx.Response(404, json={"error": "not found"})
                raise httpx.HTTPStatusError("not found", request=None, response=resp)
            c._get = _fake_get
            result = c.resolve("9.9.9")
            assert result is None

    def test_find_returns_list(self, client: HttpCatalogClient) -> None:
        results = client.find("test query")
        assert len(results) >= 1
        assert results[0].title == "Test Doc"

    def test_all_documents(self, client: HttpCatalogClient) -> None:
        docs = client.all_documents()
        assert len(docs) == 2
        assert docs[1].title == "Second"

    def test_link_returns_dict(self, client: HttpCatalogClient) -> None:
        result = client.link("1.1.1", "1.1.2", "cites")
        assert isinstance(result, dict)

    def test_links_from_uses_direction_out(self, client: HttpCatalogClient) -> None:
        # GET /links?tumbler=X&direction=out
        links = client.links_from("1.1.1")
        assert len(links) == 1
        assert links[0]["link_type"] == "cites"

    def test_links_to_uses_direction_in(self, client: HttpCatalogClient) -> None:
        # GET /links?tumbler=X&direction=in
        links = client.links_to("1.1.2")
        assert links == []

    def test_link_query(self, client: HttpCatalogClient) -> None:
        links = client.link_query(link_type="cites")
        assert len(links) == 1

    def test_graph_post_traverse(self, client: HttpCatalogClient) -> None:
        # graph() must POST /traverse (not GET)
        result = client.graph("1.1.1")
        assert isinstance(result, dict)
        assert "nodes" in result
        assert "edges" in result

    def test_graph_many_post_traverse(self, client: HttpCatalogClient) -> None:
        result = client.graph_many(["1.1.1", "1.1.2"])
        assert isinstance(result, dict)
        assert "nodes" in result

    def test_delete_document_uses_post(self, client: HttpCatalogClient) -> None:
        # POST /delete with body {tumbler: ...} → {"deleted": 1}
        result = client.delete_document("1.1.1")
        assert result is True

    def test_write_manifest_uses_rows_key(self, client: HttpCatalogClient) -> None:
        # Must send 'rows' key not 'chunks'
        client.write_manifest("1.1.1", [{"position": 0, "chash": "abc"}])

    def test_get_manifest_returns_rows(self, client: HttpCatalogClient) -> None:
        # GET /manifest/get?doc_id=X → response key 'rows'
        rows = client.get_manifest("1.1.1")
        assert len(rows) == 1
        assert rows[0]["chash"] == "abc123"

    def test_get_chunk_chashes_from_manifest(self, client: HttpCatalogClient) -> None:
        # Pulls chashes from manifest rows (not a separate endpoint)
        chashes = client.get_chunk_chashes("1.1.1")
        assert "abc123" in chashes

    def test_chashes_for_collection(self, client: HttpCatalogClient) -> None:
        chashes = client.chashes_for_collection("code__test__v1")
        assert "abc123" in chashes

    def test_docs_for_chashes_uses_tumblers_key(self, client: HttpCatalogClient) -> None:
        # Real server returns {"tumblers": [tumbler_string, ...]} — flat list of tumblers,
        # SELECT DISTINCT doc_id WHERE chash IN (...). Not a per-chash map.
        result = client.docs_for_chashes(["abc123"])
        assert isinstance(result, list)
        assert "1.1.1" in result

    def test_list_collections(self, client: HttpCatalogClient) -> None:
        colls = client.list_collections()
        assert len(colls) == 1

    def test_supersede_collection(self, client: HttpCatalogClient) -> None:
        n = client.supersede_collection("old__coll", superseded_by="new__coll")
        assert n == 5

    def test_rename_collection(self, client: HttpCatalogClient) -> None:
        # Sends {old_name, new_name} (canonical form)
        n = client.rename_collection("old__coll", "new__coll")
        assert n == 3

    def test_bulk_unlink_uses_unlink_route(self, client: HttpCatalogClient) -> None:
        # bulk_unlink POSTs to /unlink (the same handler as unlink)
        n = client.bulk_unlink(link_type="cites")
        assert n == 1  # fake server returns {"deleted": 1}

    def test_update_documents_collection_batch(self, client: HttpCatalogClient) -> None:
        # No batch endpoint: iterates update per tumbler
        n = client.update_documents_collection_batch(["1.1.1", "1.1.2"], "new__coll")
        assert n == 2

    def test_register_owner(self, client: HttpCatalogClient) -> None:
        from nexus.catalog.catalog import Tumbler
        # Uses POST /owners/upsert
        t = client.register_owner(name="acme")
        assert isinstance(t, Tumbler)

    def test_ensure_owner_for_repo(self, client: HttpCatalogClient) -> None:
        from nexus.catalog.catalog import Tumbler
        t = client.ensure_owner_for_repo(repo="/tmp/myrepo")
        assert isinstance(t, Tumbler)

    def test_set_owner_head_hash(self, client: HttpCatalogClient) -> None:
        # POST /owners/head_hash {tumbler_prefix, head_hash}
        client.set_owner_head_hash("1.1", "abc123def456")

    def test_resync_chunk_count_is_noop(self, client: HttpCatalogClient) -> None:
        # Must not raise
        client.resync_chunk_count_cache("1.1.1")


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
        client.rebuild_if_stale()  # must NOT raise

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

    def test_service_catalog_writer_whitelist_blocks_read_ops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        from nexus.catalog.factory import make_catalog_writer

        writer = make_catalog_writer()
        with pytest.raises(AttributeError, match="not a catalog write op"):
            _ = writer.resolve  # read op — must be blocked
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
        for op in CATALOG_WRITE_OPS:
            attr = getattr(writer, op, None)
            assert attr is not None, f"write op {op!r} missing from _ServiceCatalogWriter"
        writer.close()

    def test_is_interactive_write_pending_false_in_service_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_ServiceCatalogWriter.is_interactive_write_pending() returns False.

        Correct in service mode: the write-pending state is maintained server-side,
        not in the Python process.  The Python writer is a stateless RPC proxy.
        """
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        from nexus.catalog.factory import make_catalog_writer

        writer = make_catalog_writer()
        assert writer.is_interactive_write_pending() is False
        writer.close()

    def test_mcp_path_routes_to_http_catalog_client_in_service_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The MCP catalog server (mcp_infra.get_catalog_writer) routes to
        HttpCatalogClient when NX_STORAGE_BACKEND_CATALOG=service.

        This is the critical seam: mcp/catalog.py calls _get_catalog_writer() which
        calls mcp_infra.get_catalog_writer() which calls make_catalog_writer() from
        factory.py.  If any step in this chain bypasses the factory, the service
        routing would be silently skipped.  This test locks the full chain.
        """
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok-mcp-test")
        from nexus.catalog.factory import _ServiceCatalogWriter, make_catalog_writer
        from nexus.mcp_infra import get_catalog_writer

        writer = get_catalog_writer()
        assert isinstance(writer, _ServiceCatalogWriter), (
            f"MCP catalog path returned {type(writer)!r} instead of "
            f"_ServiceCatalogWriter; factory seam broken"
        )
        assert writer.routed is True
        writer.close()

    def test_no_production_bypass_of_factory(self) -> None:
        """No Python source file in src/ should bare-construct HttpCatalogClient
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
