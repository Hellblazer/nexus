# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for HttpScratchStore.

Test approach: faithful in-process fake HTTP server implementing the
/v1/t1/* contract.  The fake mirrors the real Java ScratchHandler shape:

  - id: TEXT UUID (client-generated)
  - content: str
  - session_id: str (column-scoped, not GUC)
  - tags: always "" (never null)
  - flagged: bool
  - flush_project / flush_title: str (always "", not null)
  - agent: str (always "", not null)
  - access_count: int (0 on first put; incremented by get)
  - last_accessed: str (always "", not null when never accessed)
  - ts: ISO-8601 UTC

Verifies:
  - HttpScratchStore makes correct HTTP calls (right paths, headers, payloads)
  - Response → Python object mapping is correct
  - Auth header (Bearer) and X-Nexus-Tenant are sent on every request
  - Session isolation: get with wrong session_id returns None
  - Prefix resolution: short id resolves to full UUID via /resolve_prefix
  - close_session: deletes all entries, returns count
  - BEHAVIOR CHANGE: search uses /v1/t1/search (FTS) not vector

Full cross-language end-to-end (HttpScratchStore ↔ live Java service ↔ PG)
is in tests/db/test_http_scratch_store_integration.py (marked integration).
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from nexus.db.http_scratch_store import DEFAULT_TENANT, HttpScratchStore

TOKEN = "fake-scratch-token-abc123"
SESSION = "test-session-unit"
OTHER_SESSION = "other-session-unit"

# ── In-process fake T1 server ──────────────────────────────────────────────────

# {id: entry_dict}
_STORE: dict[str, dict[str, Any]] = {}
_STORE_LOCK = threading.Lock()


def _make_entry(id: str, content: str, session_id: str, **kwargs: Any) -> dict[str, Any]:
    return {
        "id": id,
        "content": content,
        "session_id": session_id,
        "tags": kwargs.get("tags", ""),
        "flagged": kwargs.get("flagged", False),
        "flush_project": kwargs.get("flush_project", ""),
        "flush_title": kwargs.get("flush_title", ""),
        "agent": kwargs.get("agent", ""),
        "access_count": kwargs.get("access_count", 0),
        "last_accessed": "",
        "ts": "2026-06-07T00:00:00Z",
    }


class _FakeScratchHandler(BaseHTTPRequestHandler):
    """Faithful in-process stub of ScratchHandler (Java)."""

    def log_message(self, fmt, *args):
        pass  # suppress test noise

    def _check_auth(self) -> bool:
        auth = self.headers.get("Authorization", "")
        tenant = self.headers.get("X-Nexus-Tenant", "")
        if auth != f"Bearer {TOKEN}":
            self._send(401, {"error": "unauthorized"})
            return False
        if not tenant:
            self._send(400, {"error": "missing X-Nexus-Tenant"})
            return False
        return True

    def _send(self, status: int, body: Any) -> None:
        self.send_response(status)
        payload = json.dumps(body).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_POST(self):  # noqa: N802
        if not self._check_auth():
            return
        path = self.path.split("?")[0]
        body = self._read_body()

        if path == "/v1/t1/put":
            with _STORE_LOCK:
                id_ = body["id"]
                _STORE[id_] = _make_entry(
                    id_, body["content"], body["session_id"],
                    tags=body.get("tags", ""),
                    flagged=body.get("flagged", False),
                    flush_project=body.get("flush_project") or "",
                    flush_title=body.get("flush_title") or "",
                    agent=body.get("agent") or "",
                )
            self._send(200, {"id": id_})

        elif path == "/v1/t1/get":
            id_ = body.get("id", "")
            session = body.get("session_id", "")
            with _STORE_LOCK:
                entry = _STORE.get(id_)
            if entry is None or entry["session_id"] != session:
                self._send(200, {"found": False})
            else:
                # Increment access_count
                with _STORE_LOCK:
                    _STORE[id_]["access_count"] += 1
                    result = dict(_STORE[id_])
                self._send(200, result)

        elif path == "/v1/t1/search":
            query = body.get("query", "").lower()
            session = body.get("session_id", "")
            limit = body.get("limit", 10)
            with _STORE_LOCK:
                results = [
                    e for e in _STORE.values()
                    if e["session_id"] == session and query in e["content"].lower()
                ]
            self._send(200, {"results": results[:limit]})

        elif path == "/v1/t1/list":
            session = body.get("session_id", "")
            with _STORE_LOCK:
                entries = [e for e in _STORE.values() if e["session_id"] == session]
            self._send(200, {"entries": entries})

        elif path == "/v1/t1/flagged":
            session = body.get("session_id", "")
            with _STORE_LOCK:
                entries = [
                    e for e in _STORE.values()
                    if e["session_id"] == session and e.get("flagged")
                ]
            self._send(200, {"entries": entries})

        elif path == "/v1/t1/flag":
            id_ = body.get("id", "")
            session = body.get("session_id", "")
            with _STORE_LOCK:
                entry = _STORE.get(id_)
            if entry is None or entry["session_id"] != session:
                self._send(200, {"ok": False})
            else:
                with _STORE_LOCK:
                    _STORE[id_]["flagged"] = True
                    _STORE[id_]["flush_project"] = body.get("flush_project", "")
                    _STORE[id_]["flush_title"] = body.get("flush_title", "")
                self._send(200, {"ok": True})

        elif path == "/v1/t1/unflag":
            id_ = body.get("id", "")
            session = body.get("session_id", "")
            with _STORE_LOCK:
                entry = _STORE.get(id_)
            if entry is None or entry["session_id"] != session:
                self._send(200, {"ok": False})
            else:
                with _STORE_LOCK:
                    _STORE[id_]["flagged"] = False
                    _STORE[id_]["flush_project"] = ""
                    _STORE[id_]["flush_title"] = ""
                self._send(200, {"ok": True})

        elif path == "/v1/t1/delete":
            id_ = body.get("id", "")
            session = body.get("session_id", "")
            with _STORE_LOCK:
                entry = _STORE.get(id_)
                if entry is not None and entry["session_id"] == session:
                    del _STORE[id_]
                    self._send(200, {"deleted": True})
                else:
                    self._send(200, {"deleted": False})

        elif path == "/v1/t1/resolve_prefix":
            prefix = body.get("prefix", "")
            session = body.get("session_id", "")
            with _STORE_LOCK:
                matching = [
                    e["id"] for e in _STORE.values()
                    if e["session_id"] == session and e["id"].startswith(prefix)
                ]
            self._send(200, {"ids": matching})

        elif path == "/v1/t1/session/close":
            session = body.get("session_id", "")
            with _STORE_LOCK:
                to_delete = [k for k, v in _STORE.items() if v["session_id"] == session]
                for k in to_delete:
                    del _STORE[k]
            self._send(200, {"deleted": len(to_delete)})

        elif path == "/v1/t1/sweep":
            self._send(200, {"swept": 0})

        else:
            self._send(404, {"error": "not found"})


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def fake_server():
    """Start the fake T1 HTTP server for the module, tear down after."""
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _FakeScratchHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture
def store(fake_server):
    """Fresh HttpScratchStore pointing at the fake server, shared SESSION."""
    _STORE.clear()
    return HttpScratchStore(
        base_url=fake_server,
        tenant=DEFAULT_TENANT,
        session_id=SESSION,
        _token=TOKEN,
    )


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestPutGet:
    def test_put_returns_uuid(self, store):
        id_ = store.put("hello scratch content", tags="a,b")
        assert isinstance(id_, str)
        assert len(id_) == 36  # UUID format

    def test_get_returns_entry(self, store):
        id_ = store.put("get me content", tags="t1")
        result = store.get(id_)
        assert result is not None
        assert result["id"] == id_
        assert result["content"] == "get me content"
        assert result["session_id"] == SESSION

    def test_get_absent_returns_none(self, store):
        assert store.get("nonexistent-uuid-1234") is None

    def test_get_increments_access_count(self, store):
        id_ = store.put("access count test content")
        r1 = store.get(id_)
        r2 = store.get(id_)
        assert r2["access_count"] > r1["access_count"]

    def test_get_wrong_session_returns_none(self, fake_server):
        """get() must return None when session_id doesn't match (session isolation)."""
        # Store A puts an entry
        store_a = HttpScratchStore(
            base_url=fake_server, tenant=DEFAULT_TENANT,
            session_id=SESSION, _token=TOKEN,
        )
        id_ = store_a.put("session A secret content")

        # Store B (other session) tries to get it
        store_b = HttpScratchStore(
            base_url=fake_server, tenant=DEFAULT_TENANT,
            session_id=OTHER_SESSION, _token=TOKEN,
        )
        assert store_b.get(id_) is None


class TestSearch:
    def test_search_returns_matching_entry(self, store):
        store.put("neural network training optimization uniqueterm12345")
        results = store.search("uniqueterm12345")
        assert len(results) >= 1
        assert any("uniqueterm12345" in r["content"] for r in results)

    def test_search_session_scoped(self, fake_server):
        """search() must not return entries from other sessions."""
        term = "crosssessionterm999"
        store_a = HttpScratchStore(
            base_url=fake_server, tenant=DEFAULT_TENANT,
            session_id=SESSION, _token=TOKEN,
        )
        store_b = HttpScratchStore(
            base_url=fake_server, tenant=DEFAULT_TENANT,
            session_id=OTHER_SESSION, _token=TOKEN,
        )
        id_b = store_b.put(f"{term} in session B")

        results_a = store_a.search(term)
        ids_a = [r["id"] for r in results_a]
        assert id_b not in ids_a

    def test_search_empty_when_no_match(self, store):
        results = store.search("xyzzy_nonexistent_term_9999")
        assert results == []


class TestListFlagged:
    def test_list_entries_returns_all_session_entries(self, store):
        id1 = store.put("entry one list test")
        id2 = store.put("entry two list test")
        entries = store.list_entries()
        ids = [e["id"] for e in entries]
        assert id1 in ids
        assert id2 in ids

    def test_list_entries_empty_initially(self, store):
        assert store.list_entries() == []

    def test_flagged_entries_only_flagged(self, store):
        id_flag = store.put("will be flagged content", persist=True)
        id_no_flag = store.put("not flagged content")
        flagged = store.flagged_entries()
        flagged_ids = [e["id"] for e in flagged]
        assert id_flag in flagged_ids
        assert id_no_flag not in flagged_ids


class TestFlagUnflag:
    def test_flag_unflag_cycle(self, store):
        id_ = store.put("flag unflag test content")
        store.flag(id_, project="proj", title="title")
        flagged = store.flagged_entries()
        assert any(e["id"] == id_ for e in flagged)

        store.unflag(id_)
        after = store.flagged_entries()
        assert not any(e["id"] == id_ for e in after)

    def test_flag_absent_raises_key_error(self, store):
        with pytest.raises(KeyError):
            store.flag("no-such-id")

    def test_unflag_absent_raises_key_error(self, store):
        with pytest.raises(KeyError):
            store.unflag("no-such-id")

    def test_put_persist_true_pre_flags(self, store):
        id_ = store.put("persisted scratch content", persist=True)
        flagged = store.flagged_entries()
        assert any(e["id"] == id_ for e in flagged)


class TestDelete:
    def test_delete_returns_true(self, store):
        id_ = store.put("delete me content")
        assert store.delete(id_) is True
        assert store.get(id_) is None

    def test_delete_twice_returns_false(self, store):
        id_ = store.put("delete twice test")
        store.delete(id_)
        assert store.delete(id_) is False

    def test_delete_wrong_session_returns_false(self, fake_server):
        store_a = HttpScratchStore(
            base_url=fake_server, tenant=DEFAULT_TENANT,
            session_id=SESSION, _token=TOKEN,
        )
        id_ = store_a.put("cross-session delete target")

        store_b = HttpScratchStore(
            base_url=fake_server, tenant=DEFAULT_TENANT,
            session_id=OTHER_SESSION, _token=TOKEN,
        )
        assert store_b.delete(id_) is False
        assert store_a.get(id_) is not None


class TestResolvePrefix:
    def test_resolve_prefix_finds_full_uuid(self, store):
        id_ = store.put("prefix resolution test content")
        prefix = id_[:8]
        candidates = store.resolve_prefix_candidates(prefix)
        assert id_ in candidates

    def test_resolve_prefix_empty_for_absent(self, store):
        assert store.resolve_prefix_candidates("00000000-ffff") == []

    def test_get_resolves_prefix(self, store):
        id_ = store.put("get by prefix test content")
        prefix = id_[:8]
        result = store.get(prefix)
        assert result is not None
        assert result["id"] == id_


class TestSessionClose:
    def test_close_session_deletes_all_entries(self, store):
        store.put("entry one close test")
        store.put("entry two close test")
        deleted = store.close_session()
        assert deleted == 2
        assert store.list_entries() == []

    def test_close_session_idempotent(self, store):
        store.put("idempotent close content")
        store.close_session()
        assert store.close_session() == 0

    def test_clear_is_alias_for_close_session(self, store):
        store.put("clear alias content")
        store.put("clear alias content 2")
        n = store.clear()
        assert n == 2
        assert store.list_entries() == []


class TestAuthHeaders:
    def test_bad_token_raises(self, fake_server):
        bad_store = HttpScratchStore(
            base_url=fake_server, tenant=DEFAULT_TENANT,
            session_id=SESSION, _token="wrong-token-xyz",
        )
        with pytest.raises(RuntimeError, match="401"):
            bad_store.put("should fail")

    def test_session_id_on_store(self, store):
        assert store.session_id == SESSION


class TestPromoteNotImplemented:
    def test_promote_raises_not_implemented(self, store):
        with pytest.raises(NotImplementedError, match="promote"):
            store.promote("some-id", "project", "title", object())


class TestGetT1DatabaseFactory:
    def test_factory_returns_t1database_by_default(self, monkeypatch):
        """Without NX_STORAGE_BACKEND_T1=service, returns a T1Database."""
        monkeypatch.delenv("NX_STORAGE_BACKEND_T1", raising=False)
        monkeypatch.delenv("NX_STORAGE_BACKEND", raising=False)
        from nexus.db.t1 import T1Database, get_t1_database
        # Can't fully construct T1Database without a live Chroma server;
        # just verify the routing resolves to T1Database path (import works, no error)
        from nexus.db.storage_mode import StorageBackend, storage_backend_for
        assert storage_backend_for("t1") == StorageBackend.SQLITE

    def test_factory_routes_to_http_when_service(self, monkeypatch):
        """With NX_STORAGE_BACKEND_T1=service, factory targets HttpScratchStore."""
        monkeypatch.setenv("NX_STORAGE_BACKEND_T1", "service")
        from nexus.db.storage_mode import StorageBackend, storage_backend_for
        assert storage_backend_for("t1") == StorageBackend.SERVICE
