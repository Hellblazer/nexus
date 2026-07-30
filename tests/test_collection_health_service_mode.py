# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-dsu5z: TDD tests for collection_health_meta service port.

Verifies:
1. Catalog (SQLite): collection_health_meta(collection) returns
   {last_indexed, orphan_count} with EXACT values from seeded data.
2. HttpCatalogClient: collection_health_meta routes to
   GET /v1/catalog/collections/health?collection=<name>.
3. collection_health.py _default_catalog_stats_fn uses the public method
   (no hasattr(_db) guard required after fix).
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from unittest.mock import MagicMock, patch

import pytest


# ── Catalog (SQLite) tests ────────────────────────────────────────────────────


class TestCatalogCollectionHealthMeta:
    """collection_health_meta semantics against the ACTIVE catalog.

    nexus-i711w terminal deletion: was the SQLite-arm semantic pin (raw
    pinned-tumbler INSERTs + local Catalog). The semantics — MAX(indexed_at)
    aggregation, orphan = zero incoming links, cross-collection isolation —
    are the engine's to honour now, so the seeds go through register/link
    and the reads through the live reader. The routing half stays pinned by
    TestHttpCatalogClientCollectionHealthMeta below.
    """

    @pytest.fixture()
    def cat(self):
        from tests._catalog_fixture_ops import ActiveCatalog
        return ActiveCatalog()

    def test_empty_collection_returns_none_last_indexed_zero_orphans(
        self, cat
    ) -> None:
        """Collection with no documents: last_indexed=None, orphan_count=0."""
        result = cat.collection_health_meta("nonexistent__collection__v1")
        assert result["last_indexed"] is None
        assert result["orphan_count"] == 0

    def test_last_indexed_is_max_indexed_at(self, cat) -> None:
        """last_indexed = MAX(indexed_at) for documents in the collection."""
        owner = cat.register_owner("health-owner1", "repo", repo_hash="h1")
        a = cat.register(owner, "doc-a", physical_collection="test__coll__v1")
        b = cat.register(owner, "doc-b", physical_collection="test__coll__v1")
        stamps = {cat.resolve(a).indexed_at, cat.resolve(b).indexed_at}

        result = cat.collection_health_meta("test__coll__v1")
        # The aggregate must be the MAX of the per-doc stamps the catalog
        # itself reports (register stamps indexed_at server-side).
        assert result["last_indexed"] == max(stamps)
        assert result["orphan_count"] == 2  # no incoming links for either doc

    def test_orphan_count_excludes_linked_docs(self, cat) -> None:
        """orphan_count = docs with zero incoming links (to_tumbler)."""
        owner = cat.register_owner("health-owner2", "repo", repo_hash="h2")
        d1 = cat.register(owner, "doc-0", physical_collection="linked__coll__v1")
        d2 = cat.register(owner, "doc-1", physical_collection="linked__coll__v1")
        cat.register(owner, "doc-2", physical_collection="linked__coll__v1")
        # One link pointing TO d2 (makes it a non-orphan)
        cat.link(d1, d2, "cites", created_by="test")

        result = cat.collection_health_meta("linked__coll__v1")
        # d1 and doc-2 have no incoming links -> orphans; d2 has one.
        assert result["orphan_count"] == 2

    def test_cross_collection_orphan_does_not_bleed(self, cat) -> None:
        """orphan_count must not include docs from other collections."""
        owner = cat.register_owner("health-owner3", "repo", repo_hash="h3")
        cat.register(owner, "doc-target", physical_collection="target__coll__v1")
        cat.register(owner, "doc-other", physical_collection="other__coll__v1")

        result = cat.collection_health_meta("target__coll__v1")
        assert result["orphan_count"] == 1  # only the target-collection doc

    def test_returns_exact_types(self, cat) -> None:
        """last_indexed is str|None; orphan_count is int."""
        owner = cat.register_owner("health-owner4", "repo", repo_hash="h4")
        cat.register(owner, "typed-doc", physical_collection="typed__coll__v1")

        result = cat.collection_health_meta("typed__coll__v1")
        assert isinstance(result["last_indexed"], str)
        assert isinstance(result["orphan_count"], int)


# ── HttpCatalogClient routing tests ───────────────────────────────────────────


class _FakeHealthHandler(BaseHTTPRequestHandler):
    """Minimal fake server: handles GET /v1/catalog/collections/health."""

    COLLECTION_DATA: dict[str, dict] = {
        "test__health__v1": {
            "last_indexed": "2026-05-01T08:00:00",
            "orphan_count": 3,
        },
    }

    def log_message(self, *args: Any) -> None:
        pass

    def _send_json(self, body: Any, code: int = 200) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _query_params(self) -> dict[str, str]:
        qs = urlparse(self.path).query
        return {k: v[0] for k, v in parse_qs(qs).items()} if qs else {}

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        op = path.removeprefix("/v1/catalog")
        params = self._query_params()

        if op == "/collections/health":
            collection = params.get("collection", "")
            data = self.COLLECTION_DATA.get(collection)
            if data is None:
                self._send_json({"last_indexed": None, "orphan_count": 0})
            else:
                self._send_json(data)
        elif op == "/stats":
            self._send_json({"doc_count": 0, "link_count": 0, "owner_count": 0})
        else:
            self._send_json({"error": f"unexpected GET {op}"}, 404)


def _start_server() -> tuple[HTTPServer, str]:
    srv = HTTPServer(("127.0.0.1", 0), _FakeHealthHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{port}"


class TestHttpCatalogClientCollectionHealthMeta:
    """HttpCatalogClient.collection_health_meta routes to correct endpoint."""

    @pytest.fixture(scope="class")
    def client(self):
        srv, base_url = _start_server()
        import os
        _saved_token = os.environ.get("NX_SERVICE_TOKEN")
        os.environ["NX_SERVICE_TOKEN"] = "test-token"
        from nexus.catalog.http_catalog_client import HttpCatalogClient
        c = HttpCatalogClient(base_url=base_url, tenant="test", _token="test-token")
        yield c
        c.close()
        srv.shutdown()
        # Restore: a leaked token poisons later env-resolving modules (nexus-edwlp).
        if _saved_token is None:
            os.environ.pop("NX_SERVICE_TOKEN", None)
        else:
            os.environ["NX_SERVICE_TOKEN"] = _saved_token

    def test_routes_to_collections_health_endpoint(self, client) -> None:
        """collection_health_meta hits GET /v1/catalog/collections/health."""
        result = client.collection_health_meta("test__health__v1")
        assert result["last_indexed"] == "2026-05-01T08:00:00"
        assert result["orphan_count"] == 3

    def test_unknown_collection_returns_none_indexed_zero_orphans(self, client) -> None:
        """Unknown collection → {last_indexed: None, orphan_count: 0}."""
        result = client.collection_health_meta("unknown__coll__v1")
        assert result["last_indexed"] is None
        assert result["orphan_count"] == 0


# ── collection_health.py integration ─────────────────────────────────────────


class TestCollectionHealthDefaultCatalogStatsFn:
    """_default_catalog_stats_fn calls cat.collection_health_meta (no _db guard)."""

    def test_calls_collection_health_meta_not_db(self) -> None:
        """_default_catalog_stats_fn must call collection_health_meta(), not _db.

        nexus-i711w terminal deletion: was a real local Catalog with a
        mocked method; a spec-restricted mock is a STRONGER form of the
        same pin — any ``_db`` (or other) reach-in raises AttributeError.
        """
        cat = MagicMock(spec=["collection_health_meta"])
        cat.collection_health_meta = MagicMock(
            return_value={"last_indexed": "2026-01-01", "orphan_count": 5}
        )

        with patch("nexus.collection_health._open_catalog", return_value=cat):
            from nexus.collection_health import _default_catalog_stats_fn
            result = _default_catalog_stats_fn("any__coll__v1")

        cat.collection_health_meta.assert_called_once_with("any__coll__v1")
        assert result["last_indexed"] == "2026-01-01"
        assert result["orphan_count"] == 5

    def test_service_mode_no_degradation_warning(self, tmp_path: Path) -> None:
        """In service mode, _default_catalog_stats_fn must NOT emit the
        'collection_health_service_mode_degraded' warning — that was the
        old guarded path.  After the fix, HttpCatalogClient.collection_health_meta
        works directly.

        Verify by ensuring collection_health_meta is called (proving the code
        went through the new path, not the old hasattr guard that returned early).
        """
        mock_cat = MagicMock()
        mock_cat.collection_health_meta.return_value = {
            "last_indexed": "2026-06-07T10:00:00",
            "orphan_count": 0,
        }

        with patch("nexus.collection_health._open_catalog", return_value=mock_cat):
            from nexus.collection_health import _default_catalog_stats_fn
            result = _default_catalog_stats_fn("any__coll__v1")

        # If the old guard fired, collection_health_meta would NOT be called
        # and result would be {"last_indexed": None, "orphan_count": 0}
        mock_cat.collection_health_meta.assert_called_once_with("any__coll__v1")
        assert result["last_indexed"] == "2026-06-07T10:00:00"
