# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for HttpChashIndex.

Test approach: in-process fake HTTP server implementing the /v1/chash/*
contract. The fake server mirrors the REAL Java ChashHandler shape faithfully.

Verifies:
  - HttpChashIndex makes correct HTTP calls (right paths, headers, payloads)
  - Response -> Python type mapping is correct
  - HTTP error codes raise RuntimeError
  - Auth header and X-Nexus-Tenant header are sent on every request
  - upsert_many batches at _BATCH_SIZE (200)
  - import /import endpoint for ETL fidelity
  - ValueError on empty chash/collection
  - upsert_many no-op on empty list

Full cross-language end-to-end is in tests/db/test_http_chash_integration.py
(marked integration).
"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from nexus.db.t2.http_chash_index import DEFAULT_TENANT, HttpChashIndex

TOKEN = "fake-chash-service-token-abc"

# ── In-process fake server ────────────────────────────────────────────────────

# in-memory store: (chash, collection) -> {chash, physical_collection, created_at}
_STORE: dict[tuple[str, str], dict[str, str]] = {}
_STORE_LOCK = threading.Lock()

TENANT = DEFAULT_TENANT


class _FakeChashHandler(BaseHTTPRequestHandler):
    """In-process stub of ChashHandler (Java)."""

    def log_message(self, fmt, *args):  # suppress server log noise
        pass

    def _check_auth(self) -> bool:
        auth   = self.headers.get("Authorization", "")
        tenant = self.headers.get("X-Nexus-Tenant", "")
        if auth != f"Bearer {TOKEN}":
            self._send(401, {"error": "unauthorized"})
            return False
        if not tenant:
            self._send(400, {"error": "missing X-Nexus-Tenant header"})
            return False
        return True

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else {}

    def _send(self, status: int, data: Any) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _qs(self) -> dict[str, str]:
        parsed = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in parsed.items()}

    def do_POST(self):  # noqa: N802
        if not self._check_auth():
            return
        pp = urlparse(self.path).path
        body = self._body()

        if pp == "/v1/chash/upsert":
            chash = body.get("chash", "")
            coll  = body.get("collection", "")
            if not chash or not coll:
                self._send(400, {"error": "chash and collection required"})
                return
            with _STORE_LOCK:
                _STORE[(chash, coll)] = {
                    "chash": chash,
                    "physical_collection": coll,
                    "created_at": "2026-06-01T00:00:00Z",
                }
            self._send(200, {"ok": True})

        elif pp == "/v1/chash/upsert_many":
            chashes = body.get("chashes", [])
            coll    = body.get("collection", "")
            if not coll:
                self._send(400, {"error": "collection required"})
                return
            count = 0
            with _STORE_LOCK:
                for ch in chashes:
                    if not isinstance(ch, str) or not ch.strip():
                        continue
                    _STORE[(ch, coll)] = {
                        "chash": ch,
                        "physical_collection": coll,
                        "created_at": "2026-06-01T00:00:00Z",
                    }
                    count += 1
            self._send(200, {"upserted": count})

        elif pp == "/v1/chash/delete_collection":
            coll = body.get("collection", "")
            with _STORE_LOCK:
                before = len(_STORE)
                keys = [k for k in _STORE if k[1] == coll]
                for k in keys:
                    del _STORE[k]
                deleted = before - len(_STORE)
            self._send(200, {"deleted": deleted})

        elif pp == "/v1/chash/rename_collection":
            old = body.get("old", "")
            new = body.get("new", "")
            with _STORE_LOCK:
                # First drop collision rows in new
                old_keys = [k for k in _STORE if k[1] == old]
                new_set  = {k[0] for k in _STORE if k[1] == new}
                count = 0
                for (ch, coll_name) in list(old_keys):
                    if ch in new_set:
                        del _STORE[(ch, new)]
                    row = _STORE.pop((ch, coll_name))
                    row["physical_collection"] = new
                    _STORE[(ch, new)] = row
                    count += 1
            self._send(200, {"updated": count})

        elif pp == "/v1/chash/delete_stale":
            chash = body.get("chash", "")
            coll  = body.get("collection", "")
            with _STORE_LOCK:
                key = (chash, coll)
                if key in _STORE:
                    del _STORE[key]
                    self._send(200, {"deleted": 1})
                else:
                    self._send(200, {"deleted": 0})

        elif pp == "/v1/chash/import":
            rows = body.get("rows", [])
            imported = 0
            with _STORE_LOCK:
                for r in rows:
                    ch   = r.get("chash", "")
                    coll = r.get("collection", "")
                    cat  = r.get("created_at", "1970-01-01T00:00:00Z")
                    if not ch or not coll:
                        continue
                    # Upsert: EXCLUDED verbatim (idempotent; chash = immutable)
                    _STORE[(ch, coll)] = {
                        "chash": ch,
                        "physical_collection": coll,
                        "created_at": cat,
                    }
                    imported += 1
            self._send(200, {"imported": imported})

        else:
            self._send(404, {"error": f"unknown path {pp}"})

    def do_GET(self):  # noqa: N802
        if not self._check_auth():
            return
        pp  = urlparse(self.path).path
        qs  = self._qs()

        if pp == "/v1/chash/lookup":
            chash = qs.get("chash", "")
            with _STORE_LOCK:
                rows = [
                    {"collection": row["physical_collection"], "created_at": row["created_at"]}
                    for (ch, _coll), row in _STORE.items()
                    if ch == chash
                ]
            self._send(200, {"rows": rows})

        elif pp == "/v1/chash/distinct_collections":
            with _STORE_LOCK:
                colls = list({row["physical_collection"] for row in _STORE.values()})
            self._send(200, {"collections": colls})

        elif pp == "/v1/chash/is_empty":
            with _STORE_LOCK:
                empty = len(_STORE) == 0
            self._send(200, {"empty": empty})

        elif pp == "/v1/chash/count_for_collection":
            coll = qs.get("collection", "")
            with _STORE_LOCK:
                count = sum(1 for k in _STORE if k[1] == coll)
            self._send(200, {"count": count})

        else:
            self._send(404, {"error": f"unknown path {pp}"})


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def fake_server():
    """Start an in-process fake ChashHandler server; yield base_url."""
    port   = _free_port()
    server = HTTPServer(("127.0.0.1", port), _FakeChashHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture(autouse=True)
def clear_store():
    """Reset in-memory store between tests."""
    with _STORE_LOCK:
        _STORE.clear()


@pytest.fixture()
def store(fake_server):
    """Return an HttpChashIndex connected to the fake server."""
    s = HttpChashIndex(base_url=fake_server, _token=TOKEN)
    yield s
    s.close()


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestUpsert:
    def test_upsert_inserts_row(self, store):
        store.upsert(chash="abc123", collection="col_a")
        with _STORE_LOCK:
            assert ("abc123", "col_a") in _STORE

    def test_upsert_empty_chash_raises(self, store):
        with pytest.raises(ValueError, match="chash must not be empty"):
            store.upsert(chash="", collection="col_a")

    def test_upsert_empty_collection_raises(self, store):
        with pytest.raises(ValueError, match="collection must not be empty"):
            store.upsert(chash="abc123", collection="")

    def test_upsert_idempotent(self, store):
        store.upsert(chash="abc123", collection="col_a")
        store.upsert(chash="abc123", collection="col_a")
        with _STORE_LOCK:
            assert len(_STORE) == 1


class TestUpsertMany:
    def test_upsert_many_inserts_all(self, store):
        store.upsert_many(chashes=["c1", "c2", "c3"], collection="col_b")
        with _STORE_LOCK:
            assert len(_STORE) == 3
            assert ("c1", "col_b") in _STORE

    def test_upsert_many_skips_blank(self, store):
        store.upsert_many(chashes=["c1", "", "  ", "c2"], collection="col_b")
        with _STORE_LOCK:
            assert len(_STORE) == 2

    def test_upsert_many_empty_list_is_noop(self, store):
        store.upsert_many(chashes=[], collection="col_b")
        with _STORE_LOCK:
            assert len(_STORE) == 0

    def test_upsert_many_empty_collection_raises(self, store):
        with pytest.raises(ValueError, match="collection must not be empty"):
            store.upsert_many(chashes=["c1"], collection="")

    def test_upsert_many_batches_at_200(self, fake_server, monkeypatch):
        """Verify that > _BATCH_SIZE chashes are split into multiple requests."""
        import nexus.db.t2.http_chash_index as mod

        calls: list[list[str]] = []
        orig = HttpChashIndex.upsert_many

        def _spy(self, *, chashes, collection):
            # Capture the effective batch sizes by patching _client.post
            pass

        # Track POST calls to /v1/chash/upsert_many via a counting shim
        post_bodies: list[list[str]] = []
        s = HttpChashIndex(base_url=fake_server, _token=TOKEN)

        original_post = s._client.post

        def spy_post(path, **kwargs):
            if path == "/v1/chash/upsert_many":
                post_bodies.append(kwargs["json"]["chashes"])
            return original_post(path, **kwargs)

        s._client.post = spy_post

        big_list = [f"sha{i:04d}" for i in range(450)]
        s.upsert_many(chashes=big_list, collection="col_big")
        s.close()

        # 450 chashes: batch 0 (200), batch 1 (200), batch 2 (50) = 3 calls
        assert len(post_bodies) == 3, f"Expected 3 batch POSTs, got {len(post_bodies)}"
        assert len(post_bodies[0]) == 200
        assert len(post_bodies[1]) == 200
        assert len(post_bodies[2]) == 50


class TestLookup:
    def test_lookup_returns_all_collections(self, store):
        store.upsert(chash="multi", collection="col_a")
        store.upsert(chash="multi", collection="col_b")
        rows = store.lookup("multi")
        colls = {r["collection"] for r in rows}
        assert colls == {"col_a", "col_b"}

    def test_lookup_unknown_returns_empty(self, store):
        assert store.lookup("nosuchch") == []


class TestDeleteCollection:
    def test_delete_collection_removes_rows(self, store):
        store.upsert(chash="c1", collection="col_a")
        store.upsert(chash="c2", collection="col_a")
        store.upsert(chash="c3", collection="col_b")
        deleted = store.delete_collection("col_a")
        assert deleted == 2
        with _STORE_LOCK:
            assert len(_STORE) == 1

    def test_delete_collection_absent_returns_zero(self, store):
        assert store.delete_collection("no_such") == 0


class TestDistinctCollections:
    def test_distinct_collections_returns_all(self, store):
        store.upsert(chash="c1", collection="col_a")
        store.upsert(chash="c2", collection="col_b")
        store.upsert(chash="c3", collection="col_a")
        result = store.distinct_collections()
        assert result == {"col_a", "col_b"}

    def test_distinct_collections_empty(self, store):
        assert store.distinct_collections() == set()


class TestRenameCollection:
    def test_rename_collection_repoints_rows(self, store):
        store.upsert(chash="c1", collection="old_col")
        store.upsert(chash="c2", collection="old_col")
        updated = store.rename_collection(old="old_col", new="new_col")
        assert updated == 2
        with _STORE_LOCK:
            assert all(k[1] == "new_col" for k in _STORE)

    def test_rename_collection_no_op_on_absent(self, store):
        assert store.rename_collection(old="no_such", new="target") == 0


class TestDeleteStale:
    def test_delete_stale_removes_specific_row(self, store):
        store.upsert(chash="c1", collection="col_a")
        store.upsert(chash="c1", collection="col_b")
        deleted = store.delete_stale(chash="c1", collection="col_a")
        assert deleted == 1
        with _STORE_LOCK:
            assert ("c1", "col_a") not in _STORE
            assert ("c1", "col_b") in _STORE

    def test_delete_stale_idempotent(self, store):
        deleted = store.delete_stale(chash="ghost", collection="nowhere")
        assert deleted == 0


class TestIsEmpty:
    def test_is_empty_true_when_no_rows(self, store):
        assert store.is_empty() is True

    def test_is_empty_false_after_upsert(self, store):
        store.upsert(chash="c1", collection="col_a")
        assert store.is_empty() is False


class TestCountForCollection:
    def test_count_for_collection(self, store):
        store.upsert(chash="c1", collection="col_a")
        store.upsert(chash="c2", collection="col_a")
        store.upsert(chash="c3", collection="col_b")
        assert store.count_for_collection("col_a") == 2
        assert store.count_for_collection("col_b") == 1
        assert store.count_for_collection("col_c") == 0


class TestAuth:
    def test_bad_token_raises(self, fake_server):
        s = HttpChashIndex(base_url=fake_server, _token="wrong-token")
        with pytest.raises(RuntimeError, match="401"):
            s.upsert(chash="c1", collection="col_a")
        s.close()


class TestImportEndpoint:
    def test_import_fidelity_preserves_created_at(self, fake_server):
        """Direct /v1/chash/import stores created_at verbatim (ETL fidelity)."""
        s = HttpChashIndex(base_url=fake_server, _token=TOKEN)
        ts = "2024-01-15T10:30:00Z"
        resp = s._client.post(
            "/v1/chash/import",
            json={"rows": [{"chash": "abc", "collection": "col_a", "created_at": ts}]},
        )
        assert resp.is_success
        with _STORE_LOCK:
            row = _STORE[("abc", "col_a")]
            assert row["created_at"] == ts
        s.close()

    def test_import_idempotent_rerun(self, fake_server):
        """Running /import twice yields same state (upsert semantics)."""
        s = HttpChashIndex(base_url=fake_server, _token=TOKEN)
        ts = "2024-01-15T10:30:00Z"
        payload = {"rows": [{"chash": "abc", "collection": "col_a", "created_at": ts}]}
        s._client.post("/v1/chash/import", json=payload)
        s._client.post("/v1/chash/import", json=payload)
        with _STORE_LOCK:
            assert len(_STORE) == 1
        s.close()


class TestEtl:
    def test_migrate_chash_rows_copies_all(self, fake_server, tmp_path):
        """ETL reads SQLite rows and posts them to /import."""
        import sqlite3

        from nexus.db.t2.chash_etl import migrate_chash_rows

        db = tmp_path / "t2.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE chash_index (chash TEXT, physical_collection TEXT, created_at TEXT)"
        )
        conn.execute("INSERT INTO chash_index VALUES ('sha001','col_a','2024-01-01T00:00:00Z')")
        conn.execute("INSERT INTO chash_index VALUES ('sha002','col_b','2024-01-02T00:00:00Z')")
        conn.commit()
        conn.close()

        s = HttpChashIndex(base_url=fake_server, _token=TOKEN)
        result = migrate_chash_rows(db, s)
        s.close()

        assert result["total"] == 2
        assert result["imported"] == 2
        assert result["errors"] == 0
        with _STORE_LOCK:
            assert ("sha001", "col_a") in _STORE
            assert ("sha002", "col_b") in _STORE

    def test_migrate_chash_rows_idempotent(self, fake_server, tmp_path):
        """Running ETL twice yields same count (idempotent upsert)."""
        import sqlite3

        from nexus.db.t2.chash_etl import migrate_chash_rows

        db = tmp_path / "t2.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE chash_index (chash TEXT, physical_collection TEXT, created_at TEXT)"
        )
        conn.execute("INSERT INTO chash_index VALUES ('sha001','col_a','2024-01-01T00:00:00Z')")
        conn.commit()
        conn.close()

        s = HttpChashIndex(base_url=fake_server, _token=TOKEN)
        r1 = migrate_chash_rows(db, s)
        r2 = migrate_chash_rows(db, s)
        s.close()

        assert r1["imported"] == 1
        assert r2["imported"] == 1
        with _STORE_LOCK:
            assert len(_STORE) == 1  # idempotent: no duplication
