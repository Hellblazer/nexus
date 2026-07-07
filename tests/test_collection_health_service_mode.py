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
import sqlite3
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from unittest.mock import MagicMock, patch

import pytest


# ── Catalog (SQLite) tests ────────────────────────────────────────────────────


class TestCatalogCollectionHealthMeta:
    """SQLite Catalog.collection_health_meta returns exact values."""

    @pytest.fixture()
    def cat(self, tmp_path: Path):
        """Initialised Catalog with seeded documents and links."""
        from nexus.catalog.catalog import Catalog

        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        db_path = cat_dir / ".catalog.db"
        c = Catalog(cat_dir, db_path)
        yield c

    def _seed_owner(self, cat, prefix: str, name: str = "owner") -> None:
        """Seed an owner row directly via cat.register_owner."""
        cat.register_owner(name=name)

    def test_empty_collection_returns_none_last_indexed_zero_orphans(
        self, cat, tmp_path: Path
    ) -> None:
        """Collection with no documents: last_indexed=None, orphan_count=0."""
        result = cat.collection_health_meta("nonexistent__collection__v1")
        assert result["last_indexed"] is None
        assert result["orphan_count"] == 0

    def test_last_indexed_is_max_indexed_at(self, cat, tmp_path: Path) -> None:
        """last_indexed = MAX(indexed_at) for documents in the collection."""
        # Insert two documents with known indexed_at timestamps directly
        db = cat._db
        # Register an owner first
        db.execute(  # epsilon-allow: test fixture seeds owner row with pinned tumbler_prefix to control doc tumblers
            "INSERT OR IGNORE INTO owners (tumbler_prefix, name, owner_type) "
            "VALUES ('1', 'owner1', 'repo')"
        )
        db.execute(  # epsilon-allow: test fixture seeds document with known indexed_at to assert MAX aggregation
            "INSERT INTO documents "
            "(tumbler, title, physical_collection, indexed_at) "
            "VALUES ('1.1', 'doc-a', 'test__coll__v1', '2026-01-01T10:00:00')"
        )
        db.execute(  # epsilon-allow: test fixture seeds document with known indexed_at to assert MAX aggregation
            "INSERT INTO documents "
            "(tumbler, title, physical_collection, indexed_at) "
            "VALUES ('1.2', 'doc-b', 'test__coll__v1', '2026-06-01T12:00:00')"
        )
        db.commit()

        result = cat.collection_health_meta("test__coll__v1")
        assert result["last_indexed"] == "2026-06-01T12:00:00"
        assert result["orphan_count"] == 2  # no incoming links for either doc

    def test_orphan_count_excludes_linked_docs(self, cat, tmp_path: Path) -> None:
        """orphan_count = docs with zero incoming links (to_tumbler)."""
        db = cat._db
        db.execute(  # epsilon-allow: test fixture seeds owner row with pinned tumbler_prefix to control doc tumblers
            "INSERT OR IGNORE INTO owners (tumbler_prefix, name, owner_type) "
            "VALUES ('2', 'owner2', 'repo')"
        )
        # 3 docs in the collection
        for i, t in enumerate(["2.1", "2.2", "2.3"]):
            db.execute(  # epsilon-allow: test fixture seeds documents to assert orphan_count with known link structure
                "INSERT INTO documents "
                "(tumbler, title, physical_collection, indexed_at) "
                f"VALUES ('{t}', 'doc-{i}', 'linked__coll__v1', '2026-03-01T00:00:00')"
            )
        # One link pointing TO 2.2 (makes it a non-orphan)
        db.execute(  # epsilon-allow: test fixture seeds a link to verify non-orphan docs are excluded from orphan_count
            "INSERT INTO links (from_tumbler, to_tumbler, link_type, created_by) "
            "VALUES ('2.1', '2.2', 'cites', 'test')"
        )
        db.commit()

        result = cat.collection_health_meta("linked__coll__v1")
        # 2.1 and 2.3 have no incoming links → orphans; 2.2 has one → not orphan
        assert result["orphan_count"] == 2

    def test_cross_collection_orphan_does_not_bleed(self, cat, tmp_path: Path) -> None:
        """orphan_count must not include docs from other collections."""
        db = cat._db
        db.execute(  # epsilon-allow: test fixture seeds owner row with pinned tumbler_prefix to control doc tumblers
            "INSERT OR IGNORE INTO owners (tumbler_prefix, name, owner_type) "
            "VALUES ('3', 'owner3', 'repo')"
        )
        # doc in target collection
        db.execute(  # epsilon-allow: test fixture seeds document in target collection to assert cross-collection isolation
            "INSERT INTO documents "
            "(tumbler, title, physical_collection, indexed_at) "
            "VALUES ('3.1', 'doc-target', 'target__coll__v1', '2026-01-01T00:00:00')"
        )
        # doc in OTHER collection
        db.execute(  # epsilon-allow: test fixture seeds document in other collection to verify it is excluded from target query
            "INSERT INTO documents "
            "(tumbler, title, physical_collection, indexed_at) "
            "VALUES ('3.2', 'doc-other', 'other__coll__v1', '2026-01-01T00:00:00')"
        )
        db.commit()

        result = cat.collection_health_meta("target__coll__v1")
        assert result["orphan_count"] == 1  # only 3.1 (in target__coll__v1)

    def test_returns_exact_types(self, cat, tmp_path: Path) -> None:
        """last_indexed is str|None; orphan_count is int."""
        db = cat._db
        db.execute(  # epsilon-allow: test fixture seeds owner row with pinned tumbler_prefix to control doc tumblers
            "INSERT OR IGNORE INTO owners (tumbler_prefix, name, owner_type) "
            "VALUES ('4', 'owner4', 'repo')"
        )
        db.execute(  # epsilon-allow: test fixture seeds document to assert return type of collection_health_meta
            "INSERT INTO documents "
            "(tumbler, title, physical_collection, indexed_at) "
            "VALUES ('4.1', 'typed-doc', 'typed__coll__v1', '2026-04-15T09:30:00')"
        )
        db.commit()

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

    def test_calls_collection_health_meta_not_db(self, tmp_path: Path) -> None:
        """_default_catalog_stats_fn must call collection_health_meta(), not _db."""
        from nexus.catalog.catalog import Catalog

        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        cat = Catalog(cat_dir, cat_dir / ".catalog.db")

        # If the implementation still uses hasattr(cat, '_db'), patching
        # collection_health_meta won't help — but if it's ported, the mock
        # is what gets called.
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
