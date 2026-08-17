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

import os
import threading
from typing import Any

import pytest

from nexus.db.http_scratch_store import DEFAULT_TENANT, HttpScratchStore
from nexus.db.t1 import publish_t1_session_lease
from tests.db._fake_t2_server import FakeT2HandlerBase, fake_http_server

TOKEN = "fake-scratch-token-abc123"
SESSION = "test-session-unit"
OTHER_SESSION = "other-session-unit"

# ── In-process fake T1 server ──────────────────────────────────────────────────

# {id: entry_dict}
_STORE: dict[str, dict[str, Any]] = {}
_STORE_LOCK = threading.Lock()

#: nexus-g5hzk: when {"token": <str>} is set, the fake 401s any request whose
#: X-Nexus-T1-Session header differs (the require-minted gate). {"token": None}
#: (the default) disables the gate so the legacy contract tests run unchanged.
_REQUIRED_T1_TOKEN: dict[str, Any] = {"token": None}


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


class _FakeScratchHandler(FakeT2HandlerBase):
    """Faithful in-process stub of ScratchHandler (Java)."""

    TOKEN = TOKEN

    def _check_auth(self) -> bool:
        if not super()._check_auth():
            return False
        # nexus-g5hzk: optional require-minted-session gate, mirroring the
        # Java AuthFilter — active only when a test sets _REQUIRED_T1_TOKEN.
        required = _REQUIRED_T1_TOKEN.get("token")
        if required is not None and self.headers.get("X-Nexus-T1-Session", "") != required:
            self._send(401, {"error": "unauthorized"})
            return False
        return True

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


@pytest.fixture(scope="module")
def fake_server():
    """Start the fake T1 HTTP server for the module, tear down after."""
    with fake_http_server(_FakeScratchHandler) as url:
        yield url


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
    # test_put_returns_uuid / test_get_absent_returns_none moved to the
    # store-parametrized contract in test_t2_store_crud_contract.py
    # (test-suite-compression P1d) — memory, plan_library, and scratch all
    # share the same synthetic-identifier put/get/delete shape.

    def test_get_returns_entry_with_session_id(self, store):
        """Store-specific detail beyond the generic contract: the returned
        entry carries the OWNING session_id verbatim (content/id round-trip
        itself is covered by the shared crud-contract journey)."""
        id_ = store.put("get me content", tags="t1")
        result = store.get(id_)
        assert result is not None
        assert result["session_id"] == SESSION

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
    # test_delete_returns_true / test_delete_twice_returns_false moved to
    # the store-parametrized contract in test_t2_store_crud_contract.py
    # (test-suite-compression P1d).

    def test_delete_removes_entry(self, store):
        """Store-specific detail beyond the generic contract: a deleted
        entry is truly gone from get(), not merely reported deleted."""
        id_ = store.put("delete me content")
        assert store.delete(id_) is True
        assert store.get(id_) is None

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


class TestPromote:
    def test_promote_absent_id_raises_key_error(self, store):
        """promote() raises KeyError when the entry is not found in this session."""
        with pytest.raises(KeyError, match="some-missing-id"):
            store.promote("some-missing-id", "project", "title", object())

    def test_promote_calls_t2_put_and_returns_report(self, store):
        """promote() fetches from T1, calls t2.put(), returns a PromotionReport."""
        # Put an entry into the fake store
        entry_id = store.put("promote test content", tags="promo-tag")

        # Minimal T2 double that records .put() calls
        class _FakeT2:
            def __init__(self):
                self.calls = []
                self.memory = self  # promote() uses t2.memory.search in overlap detection

            def put(self, **kwargs):
                self.calls.append(kwargs)

            def search(self, query, project="", access=""):
                return []  # no overlaps

        fake_t2 = _FakeT2()
        from nexus.types import PromotionReport
        report = store.promote(entry_id, "my-project", "my-title", fake_t2)
        assert isinstance(report, PromotionReport)
        assert report.action == "new"
        assert len(fake_t2.calls) == 1
        assert fake_t2.calls[0]["project"] == "my-project"
        assert fake_t2.calls[0]["title"] == "my-title"
        assert fake_t2.calls[0]["content"] == "promote test content"


class TestGetT1DatabaseFactory:
    def test_factory_defaults_to_service_like_every_other_store(self, monkeypatch):
        """nexus-rn3wo.1: without any override, T1 follows the SERVICE hard
        default like every other store — the earlier T1-only SQLite carve-out
        is gone now that HttpScratchStore has a CLI-dedicated session path."""
        monkeypatch.delenv("NX_STORAGE_BACKEND_T1", raising=False)
        monkeypatch.delenv("NX_STORAGE_BACKEND", raising=False)
        from nexus.db.storage_mode import StorageBackend, storage_backend_for
        assert storage_backend_for("t1") == StorageBackend.SERVICE

    def test_factory_routes_to_http_when_service(self, monkeypatch):
        """With NX_STORAGE_BACKEND_T1=service, factory targets HttpScratchStore."""
        monkeypatch.setenv("NX_STORAGE_BACKEND_T1", "service")
        from nexus.db.storage_mode import StorageBackend, storage_backend_for
        assert storage_backend_for("t1") == StorageBackend.SERVICE

    def test_get_t1_database_returns_http_scratch_store_on_service_backend(
        self, monkeypatch, fake_server
    ):
        """get_t1_database() returns HttpScratchStore when NX_STORAGE_BACKEND_T1=service.

        This is the critical routing assertion: the factory MUST return HttpScratchStore,
        not T1Database, on the service path.  This test is the production-entry-point
        guard that the code-review critiqued as missing.
        """
        # fake_server is a URL string like "http://127.0.0.1:<port>"
        port = fake_server.split(":")[-1]
        monkeypatch.setenv("NX_STORAGE_BACKEND_T1", "service")
        monkeypatch.delenv("NX_T1_ISOLATED", raising=False)  # defensive: retired var hard-fails if set (nexus-4lkmz)
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", port)
        monkeypatch.setenv("NX_SERVICE_TOKEN", TOKEN)
        monkeypatch.setenv("NX_T1_SESSION", "factory-session")

        from nexus.db.http_scratch_store import HttpScratchStore
        from nexus.db.t1 import get_t1_database
        result = get_t1_database()
        assert isinstance(result, HttpScratchStore), (
            f"get_t1_database() must return HttpScratchStore when "
            f"NX_STORAGE_BACKEND_T1=service, got {type(result).__name__}"
        )

    def test_get_t1_database_hard_errors_on_explicit_sqlite_opt_out(
        self, monkeypatch
    ):
        """RDR-158 P3 (nexus-7bomn): the =sqlite opt-out is retired. The
        factory must fail LOUD with the stranded-install redirect — never
        silently hand back the service store to a shell that explicitly
        asked for the old baseline."""
        monkeypatch.delenv("NX_STORAGE_BACKEND_T1", raising=False)
        monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")
        monkeypatch.delenv("NX_T1_ISOLATED", raising=False)

        import pytest as _pytest

        from nexus.db.storage_mode import StorageModeFlagError
        from nexus.db.t1 import get_t1_database

        with _pytest.raises(StorageModeFlagError, match="retired SQLite storage backend"):
            get_t1_database()

    def test_get_t1_database_isolated_leg_retired_hard_fails(self, monkeypatch):
        """nexus-4lkmz: NX_T1_ISOLATED=1 (the formerly-documented in-process
        ephemeral scratch escape hatch) is retired outright — T1 is PG-only.
        It still wins BEFORE env validation (same check-first position as
        before), but now as a hard failure naming the real remedy instead of
        a silent redirect to an in-process store."""
        monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")
        monkeypatch.setenv("NX_T1_ISOLATED", "1")

        from nexus.db.t1 import T1IsolatedLegRetiredError, get_t1_database

        with pytest.raises(T1IsolatedLegRetiredError, match="retired"):
            get_t1_database()

# ── nexus-g5hzk: 401 -> lease-reread -> retry-once (borrower self-heal) ────────


class TestTokenRotationSelfHeal:
    """Live incident 2026-07-14: the owner's half-TTL re-mint ROTATES the
    server-side token; a borrower's cached string stops resolving and every
    call 401s for the rest of the process. The owner republishes the lease
    file at each rotation — on 401 the store re-reads it and retries once."""

    FRESH = "fresh-rotated-token"
    STALE = "stale-borrowed-token"

    @pytest.fixture
    def gated_store(self, fake_server, tmp_path, monkeypatch):
        _STORE.clear()
        _REQUIRED_T1_TOKEN["token"] = self.FRESH  # server only accepts FRESH
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        # Review M2: the SUT itself mutates NX_T1_SESSION[_ID] on a heal;
        # pre-registering them with monkeypatch makes teardown revert the
        # SUT's raw os.environ writes too (no cross-test leak).
        monkeypatch.setenv("NX_T1_SESSION", self.STALE)
        monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)
        monkeypatch.setenv("NX_T1_SESSION_ID", SESSION)
        store = HttpScratchStore(
            base_url=fake_server,
            tenant=DEFAULT_TENANT,
            session_id=SESSION,
            _token=TOKEN,
            _session_token=self.STALE,  # the borrower's snapshot, now rotated out
        )
        yield store
        _REQUIRED_T1_TOKEN["token"] = None

    def _publish_lease(self, tmp_path, token, ttl=300.0):
        publish_t1_session_lease(SESSION, token, tmp_path, ttl_seconds=ttl)

    def test_401_rereads_lease_and_retries_once(self, gated_store, tmp_path, monkeypatch):
        self._publish_lease(tmp_path, self.FRESH)  # the owner's republished lease
        id_ = gated_store.put("healed content")
        assert id_  # the retry with the adopted token succeeded
        assert gated_store._session_token == self.FRESH
        # Subprocess children must inherit the LIVE token — and the id
        # stays the ID (review M1: the ctor fallback chain must never
        # collapse the token into a session id for a later store).
        assert os.environ.get("NX_T1_SESSION") == self.FRESH
        assert os.environ.get("NX_T1_SESSION_ID") == SESSION
        # And subsequent calls ride the fresh token with no further 401 dance.
        assert gated_store.get(id_) is not None

    def test_401_with_no_lease_raises_marker(self, gated_store):
        with pytest.raises(RuntimeError, match="unauthorized T1 session"):
            gated_store.put("no lease to heal from")

    def test_401_with_unchanged_token_raises_not_loops(self, gated_store, tmp_path):
        # The lease holds OUR OWN (dead) token: refresh declines, the 401
        # stands, and there is no retry loop.
        self._publish_lease(tmp_path, self.STALE)
        with pytest.raises(RuntimeError, match="unauthorized T1 session"):
            gated_store.put("own token is genuinely dead")

    def test_401_with_expired_lease_raises_marker(self, gated_store, tmp_path):
        self._publish_lease(tmp_path, self.FRESH, ttl=-1.0)  # already expired
        with pytest.raises(RuntimeError, match="unauthorized T1 session"):
            gated_store.put("expired lease is never borrowed")


# ── nexus-wrwb7: mint_token resolution seam (RDR-005 2a self-minting) ───────
#
# HttpScratchStore does NOT ride RefreshableHttpStoreMixin (bespoke
# bearer-header caching) -- this is its OWN copy of the resolution-seam
# wiring, tested independently of the T2 mixin's equivalent in
# tests/db/test_refreshable_client.py.

_MINT_CREDENTIAL = "mint-cred-for-t1-tests"
_MINTED_DATA_TOKEN: dict[str, str | None] = {"value": None}
_MINT_CALLS = 0


class _FakeMonotonicClock:
    """Injectable clock for DataTokenManager -- deterministic proactive-
    refresh tests with no real sleep (critic S4)."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _reset_mint_state() -> None:
    global _MINT_CALLS
    _MINTED_DATA_TOKEN["value"] = None
    _MINT_CALLS = 0


class _MintAwareScratchHandler(FakeT2HandlerBase):
    """Minimal fake T1 service: /v1/t1/put + /v1/data-tokens/mint. Accepts
    EITHER the static TOKEN or the most recently minted data token."""

    TOKEN = TOKEN

    def _check_auth(self) -> bool:
        auth = self.headers.get("Authorization", "")
        tenant = self.headers.get("X-Nexus-Tenant", "")
        valid = {f"Bearer {self.TOKEN}"}
        if _MINTED_DATA_TOKEN["value"]:
            valid.add(f"Bearer {_MINTED_DATA_TOKEN['value']}")
        if auth not in valid:
            self._send(401, {"error": "unauthorized"})
            return False
        if not tenant:
            self._send(400, {"error": "missing X-Nexus-Tenant header"})
            return False
        return True

    def _handle_mint(self) -> None:
        global _MINT_CALLS
        _MINT_CALLS += 1
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {_MINT_CREDENTIAL}":
            self._send(401, {"error": "bad mint credential"})
            return
        _MINTED_DATA_TOKEN["value"] = f"minted-t1-token-{_MINT_CALLS}"
        self._send(200, {"data_token": _MINTED_DATA_TOKEN["value"], "expires_in_seconds": 300})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/v1/data-tokens/mint":
            self._handle_mint()
            return
        if not self._check_auth():
            return
        body = self._read_body()
        if path == "/v1/t1/put":
            self._send(200, {"id": body.get("id", "gen-id")})
        else:
            self._send(404, {"error": "not found"})


@pytest.fixture
def mint_fake_server():
    _reset_mint_state()
    with fake_http_server(_MintAwareScratchHandler) as url:
        yield url
    _reset_mint_state()


class TestMintTokenResolutionSeam:
    def _reset_manager(self) -> None:
        from nexus.db.data_token import reset_data_token_manager
        reset_data_token_manager()

    def test_unconfigured_uses_static_token_unchanged(
        self, mint_fake_server, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_MINT_TOKEN", raising=False)
        monkeypatch.setenv("NX_SERVICE_TOKEN", TOKEN)
        self._reset_manager()
        store = HttpScratchStore(
            base_url=mint_fake_server, tenant=DEFAULT_TENANT, session_id=SESSION,
        )
        result = store.put("hello")
        assert result
        assert _MINT_CALLS == 0
        assert store._headers["Authorization"] == f"Bearer {TOKEN}"

    def test_mint_token_configured_presents_data_token(
        self, mint_fake_server, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_MINT_TOKEN", _MINT_CREDENTIAL)
        monkeypatch.setenv("NX_SERVICE_TOKEN", "static-token-must-not-be-used")
        self._reset_manager()
        try:
            store = HttpScratchStore(
                base_url=mint_fake_server, tenant=DEFAULT_TENANT, session_id=SESSION,
            )
            result = store.put("hello mint")
            assert result
            assert _MINT_CALLS == 1
            assert store._headers["Authorization"] == f"Bearer {_MINTED_DATA_TOKEN['value']}"
        finally:
            self._reset_manager()

    def test_pinned_token_never_overridden(
        self, mint_fake_server, monkeypatch, tmp_path
    ) -> None:
        """A caller that pins ``_token=`` explicitly (fake-server test
        fixtures throughout this file) must never have it silently swapped
        for a self-minted data token."""
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_MINT_TOKEN", _MINT_CREDENTIAL)
        self._reset_manager()
        try:
            store = HttpScratchStore(
                base_url=mint_fake_server, tenant=DEFAULT_TENANT, session_id=SESSION,
                _token=TOKEN,
            )
            assert store._headers["Authorization"] == f"Bearer {TOKEN}"
            assert _MINT_CALLS == 0
        finally:
            self._reset_manager()

    def test_401_falls_back_to_data_token_remint_after_session_refresh_declines(
        self, mint_fake_server, monkeypatch, tmp_path
    ) -> None:
        """No T1-session lease exists, so the session-token self-heal
        (nexus-g5hzk) declines -- the store must THEN try a data-token
        re-mint (nexus-wrwb7) rather than leaving the 401 to stand."""
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_MINT_TOKEN", _MINT_CREDENTIAL)
        self._reset_manager()
        try:
            store = HttpScratchStore(
                base_url=mint_fake_server, tenant=DEFAULT_TENANT, session_id=SESSION,
            )
            baseline = store.put("before revoke")
            assert baseline
            assert _MINT_CALLS == 1

            _MINTED_DATA_TOKEN["value"] = "revoked-elsewhere"  # simulate an out-of-band rotation

            result = store.put("after revoke")
            assert result
            assert _MINT_CALLS == 2, "expected exactly one re-mint, not zero and not a loop"
        finally:
            self._reset_manager()

    def test_persistent_mint_rejection_fails_loud(
        self, mint_fake_server, monkeypatch, tmp_path
    ) -> None:
        from nexus.db.data_token import DataTokenMintError

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_MINT_TOKEN", "wrong-credential")
        self._reset_manager()
        try:
            with pytest.raises(DataTokenMintError):
                HttpScratchStore(
                    base_url=mint_fake_server, tenant=DEFAULT_TENANT, session_id=SESSION,
                )
            assert _MINT_CALLS == 1
        finally:
            self._reset_manager()

    def test_proactive_refresh_below_twenty_percent_ttl_no_401_involved(
        self, mint_fake_server, monkeypatch, tmp_path
    ) -> None:
        """critic S4 (nexus-ssqk9): the design-of-record <20%-TTL PROACTIVE
        refresh must fire from ORDINARY per-request resolution, not only
        reactively after a 401. Advancing an INJECTED clock past the
        threshold -- with NO out-of-band token rotation at all -- must
        still trigger a re-mint on the next plain ``put()``, proving the
        per-request path goes through ``bearer_for()`` (like T3) rather
        than the construction-time-cached header."""
        import nexus.db.data_token as data_token_module

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_MINT_TOKEN", _MINT_CREDENTIAL)
        self._reset_manager()
        try:
            clock = _FakeMonotonicClock()
            manager = data_token_module.DataTokenManager(
                clock=clock, mint_credential=lambda: _MINT_CREDENTIAL,
            )
            monkeypatch.setattr(data_token_module, "_default_manager", manager)

            store = HttpScratchStore(
                base_url=mint_fake_server, tenant=DEFAULT_TENANT, session_id=SESSION,
            )
            assert _MINT_CALLS == 1

            clock.advance(300 * 0.85)  # 15% of the 300s TTL remains -- below threshold

            result = store.put("after ttl decay, no 401 involved")

            assert result
            assert _MINT_CALLS == 2, (
                "proactive per-request bearer_for() must re-mint BEFORE any "
                "401 occurs -- the fake server accepts whatever token was "
                "last minted regardless of client-side TTL bookkeeping, so "
                "a 401 never fires in this scenario"
            )
        finally:
            self._reset_manager()
