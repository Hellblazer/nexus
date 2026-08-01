# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-qnp5s: TDD tests for catalog._db consumer migration to public API.

Verifies that:
1. SQLite Catalog has all new public API methods with correct semantics.
2. HttpCatalogClient routes the new methods to the correct HTTP endpoints.
3. The migrated sites (repos.py, scoring.py, health.py, etc.) call public
   API rather than ._db — validated by the storage_boundary_lint.
"""
from __future__ import annotations

import json
import pathlib
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient


# ── shared fake server ────────────────────────────────────────────────────────


class FakeNewEndpointHandler(BaseHTTPRequestHandler):
    """Routes for the new nexus-qnp5s endpoints only."""

    def log_message(self, *args: Any) -> None:
        pass

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
        params = self._query_params()

        if op == "/owners/by_name":
            name = params.get("name", "")
            if name == "myrepo":
                self._send_json({
                    "owners": [
                        {"tumbler_prefix": "1.1", "name": "myrepo",
                         "owner_type": "curator"},
                        {"tumbler_prefix": "1.2", "name": "myrepo",
                         "owner_type": "repo"},
                    ]
                })
            else:
                self._send_json({"owners": []})
        elif op == "/owners/show":
            prefix = params.get("tumbler_prefix", "")
            if prefix == "1.1":
                self._send_json({
                    "tumbler_prefix": "1.1",
                    "name": "myrepo",
                    "owner_type": "curator",
                    "repo_hash": None,
                    "description": None,
                    "repo_root": "/tmp/myrepo",
                    "head_hash": "abc123",
                })
            else:
                self._send_json({})
        elif op == "/stats":
            self._send_json({"doc_count": 5, "link_count": 3, "owner_count": 2})
        elif op == "/collections/list":
            self._send_json({
                "collections": [
                    {"name": "code__owner1__voyage-code-3__v1", "owner_id": "owner1",
                     "content_type": "code"},
                    {"name": "knowledge__owner2__voyage-context-3__v1",
                     "owner_id": "owner2", "content_type": "knowledge"},
                ]
            })
        else:
            self._send_json({"error": f"unknown GET op: {op}"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        op = path.removeprefix("/v1/catalog")
        body = self._read_body()

        if op == "/owners/by_type":
            owner_type = body.get("owner_type", "")
            if owner_type == "repo":
                self._send_json({
                    "owners": [
                        {"tumbler_prefix": "1.2", "name": "testrepo",
                         "owner_type": "repo", "repo_hash": None,
                         "description": None, "repo_root": "/tmp/testrepo",
                         "head_hash": None},
                    ]
                })
            elif owner_type == "curator":
                self._send_json({
                    "owners": [
                        {"tumbler_prefix": "1.1", "name": "mycurator",
                         "owner_type": "curator", "repo_hash": None,
                         "description": None, "repo_root": "",
                         "head_hash": None},
                    ]
                })
            else:
                self._send_json({"owners": []})
        elif op == "/docs/chunk-counts":
            doc_ids = body.get("doc_ids", [])
            counts = {d: (i + 1) * 10 for i, d in enumerate(doc_ids)}
            self._send_json(counts)
        elif op == "/links/from-batch":
            tumblers = body.get("tumblers", [])
            result = {}
            for t in tumblers:
                result[t] = [{"from_tumbler": t, "link_type": "cites"}]
            self._send_json(result)
        else:
            self._send_json({"ok": True})


@pytest.fixture(scope="module")
def fake_server():
    server = HTTPServer(("127.0.0.1", 0), FakeNewEndpointHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.05)
    return f"http://127.0.0.1:{port}"


@pytest.fixture(scope="module")
def http_client(fake_server):
    return HttpCatalogClient(
        base_url=fake_server,
        _token="test-token",
    )


# ── SQLite Catalog new methods: RETIRED (nexus-i711w terminal deletion) ─────
# The sqlite_cat fixture and TestSQLiteCatalogNewMethods (18 tests over the
# local Catalog's nexus-qnp5s API surface) died with nexus.catalog.catalog.
# The HttpCatalogClient half below is the surviving side of the parity pair.


# ── HttpCatalogClient new endpoints ──────────────────────────────────────────


class TestHttpCatalogClientNewMethods:
    """Verify HttpCatalogClient routes new nexus-qnp5s methods to HTTP endpoints."""

    def test_curator_owner_tumbler_by_name_curator(self, http_client):
        result = http_client.curator_owner_tumbler_by_name("myrepo")
        assert result is not None
        assert str(result) == "1.1"

    def test_curator_owner_tumbler_by_name_filters_repo_type(self, http_client):
        """The GET /owners/by_name returns both curator and repo; method must
        filter to owner_type='curator' only."""
        # The fake server returns curator (1.1) and repo (1.2) for 'myrepo'.
        result = http_client.curator_owner_tumbler_by_name("myrepo")
        assert str(result) == "1.1"  # must pick the curator one

    def test_curator_owner_tumbler_by_name_miss(self, http_client):
        result = http_client.curator_owner_tumbler_by_name("nobody")
        assert result is None

    def test_get_owner_by_prefix_hit(self, http_client):
        result = http_client.get_owner_by_prefix("1.1")
        assert result is not None
        assert result["tumbler_prefix"] == "1.1"
        assert result["head_hash"] == "abc123"

    def test_get_owner_by_prefix_miss(self, http_client):
        result = http_client.get_owner_by_prefix("9.9")
        assert result is None

    def test_list_owners_by_type_repo(self, http_client):
        result = http_client.list_owners_by_type("repo")
        assert len(result) == 1
        assert result[0]["owner_type"] == "repo"
        assert result[0]["repo_root"] == "/tmp/testrepo"

    def test_list_owners_by_type_empty(self, http_client):
        result = http_client.list_owners_by_type("none")
        assert result == []

    def test_chunk_counts_for_docs(self, http_client):
        result = http_client.chunk_counts_for_docs(["1.1.1", "1.1.2"])
        assert "1.1.1" in result
        assert "1.1.2" in result
        assert isinstance(result["1.1.1"], int)

    def test_chunk_counts_for_docs_empty(self, http_client):
        result = http_client.chunk_counts_for_docs([])
        assert result == {}

    def test_links_from_batch(self, http_client):
        result = http_client.links_from_batch(["1.1.1"])
        assert "1.1.1" in result
        links = result["1.1.1"]
        assert any(lnk["link_type"] == "cites" for lnk in links)

    def test_links_from_batch_empty(self, http_client):
        result = http_client.links_from_batch([])
        assert result == {}

    def test_collections_by_owner(self, http_client):
        result = http_client.collections_by_owner("owner1")
        assert len(result) == 1
        assert result[0]["name"] == "code__owner1__voyage-code-3__v1"

    def test_stats_via_http(self, http_client):
        s = http_client.stats()
        assert s["doc_count"] == 5
        assert s["link_count"] == 3
        assert s["owner_count"] == 2


# ── API parity: both backends expose same interface ───────────────────────────


_REQUIRED_METHODS = [
    "curator_owner_tumbler_by_name",
    "stats",
    "collections_by_owner",
    "get_owner_by_prefix",
    "list_owners_by_type",
    "chunk_counts_for_docs",
    "links_from_batch",
    "close",
]


# test_sqlite_catalog_has_method retired (nexus-i711w terminal deletion):
# the local Catalog side of the API-parity pair is gone.


@pytest.mark.parametrize("method_name", _REQUIRED_METHODS)
def test_http_catalog_client_has_method(method_name):
    """HttpCatalogClient must also expose every method."""
    assert hasattr(HttpCatalogClient, method_name), (
        f"HttpCatalogClient is missing {method_name!r}"
    )


# ── Storage boundary lint: migrated sites contain no ._db accesses ─────────


def test_scoring_py_has_no_raw_db_access():
    """scoring.py must not access ._db directly — all scoring paths use
    chunk_counts_for_docs() and links_from_batch() (nexus-qnp5s Group E)."""
    import ast

    src = (
        pathlib.Path(__file__).parent.parent
        / "src" / "nexus" / "scoring.py"
    ).read_text()
    tree = ast.parse(src)
    lines = src.splitlines()
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "_db":
            line_text = lines[node.lineno - 1]
            # (The old "epsilon-allow" comment-exemption is retired —
            # nexus-146xx.18: the token grants nothing anywhere.)
            violations.append((node.lineno, line_text.strip()))
    assert violations == [], (
        f"scoring.py has ._db accesses that should have been migrated: {violations}"
    )


def test_repos_py_has_no_raw_db_access():
    """repos.py must not access ._db directly — all paths use the public API
    (nexus-qnp5s Groups B/D)."""
    import ast

    src = (
        pathlib.Path(__file__).parent.parent
        / "src" / "nexus" / "repos.py"
    ).read_text()
    tree = ast.parse(src)
    lines = src.splitlines()
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "_db":
            line_text = lines[node.lineno - 1]
            # (The old "epsilon-allow" comment-exemption is retired —
            # nexus-146xx.18: the token grants nothing anywhere.)
            violations.append((node.lineno, line_text.strip()))
    assert violations == [], (
        f"repos.py has ._db accesses that should have been migrated: {violations}"
    )


def test_health_py_has_no_raw_db_access():
    """health.py must not access ._db directly — uses cat.stats() (Group C)."""
    import ast

    src = (
        pathlib.Path(__file__).parent.parent
        / "src" / "nexus" / "health.py"
    ).read_text()
    tree = ast.parse(src)
    lines = src.splitlines()
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "_db":
            line_text = lines[node.lineno - 1]
            # (The old "epsilon-allow" comment-exemption is retired —
            # nexus-146xx.18: the token grants nothing anywhere.)
            violations.append((node.lineno, line_text.strip()))
    assert violations == [], (
        f"health.py has ._db accesses that should have been migrated: {violations}"
    )


def test_pipeline_stages_py_has_no_raw_db_access():
    """pipeline_stages.py must not access ._db (nexus-qnp5s Groups A/B)."""
    import ast

    src = (
        pathlib.Path(__file__).parent.parent
        / "src" / "nexus" / "pipeline_stages.py"
    ).read_text()
    tree = ast.parse(src)
    lines = src.splitlines()
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "_db":
            line_text = lines[node.lineno - 1]
            # (The old "epsilon-allow" comment-exemption is retired —
            # nexus-146xx.18: the token grants nothing anywhere.)
            violations.append((node.lineno, line_text.strip()))
    assert violations == [], (
        f"pipeline_stages.py has ._db accesses: {violations}"
    )
