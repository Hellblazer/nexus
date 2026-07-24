# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD for the CLI-dedicated T1 session path (nexus-rn3wo.1).

LOCKED design: T2 ``nexus/design-t1-service-local-cutover-2026-07-11.md``
(twice-audited via ``nx_plan_audit``). A bare CLI invocation with no
inherited live MCP session (``NX_T1_SESSION`` / ``NX_T1_SESSION_ID`` both
unset) mints its OWN persisted, purpose-built session id — cached under
``nexus_config_dir()`` — and uses it to mint a T1 session token via
``HttpTokenStore.start_session``. This id is NEVER derived from
``resolve_active_session_id()`` / ``NX_SESSION_ID`` / ``current_session``:
that chain resolves a LIVE Claude session's id, and a bare CLI re-minting
against it would rotate the live MCP server's token out from under it
(``HttpTokenStore.start_session`` is ``ON CONFLICT DO UPDATE``).

A second ``nx_plan_audit`` pass flagged a MEDIUM residual: two concurrent
bare-CLI processes sharing the SAME dedicated id could each re-mint the
session token, invalidating the other's in-flight token. The fix is a
self-heal: on a 401 (``SESSION_UNAUTHORIZED_MARKER``) from the
CLI-dedicated path, re-mint once and retry the failed operation before
propagating.

Test approach: an in-process fake HTTP server that implements BOTH the
``/v1/sessions/start`` (token mint) and ``/v1/t1/*`` (scratch CRUD)
contracts, with token validation on the T1 endpoints so 401 races can be
simulated deterministically.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

SERVICE_TOKEN = "fake-cli-dedicated-service-token"

# ── In-process fake service state (module-level, reset per test) ──────────────

_SCRATCH: dict[str, dict[str, Any]] = {}
_VALID_TOKENS: dict[str, str] = {}  # session_id -> currently-valid minted token
_ALWAYS_401: set[str] = set()  # session_ids that always 401 on t1 ops (persistent-failure sim)
_MINT_CALLS: list[str] = []  # session_ids passed to /v1/sessions/start, in call order
_MINT_FAILS: bool = False  # nexus-c8yvj finding 2: simulate a broken NX_SERVICE_TOKEN (mint 403s)
_MINT_TTL_SECONDS: float = 3600  # nexus-ngcpo: overridable per-test so refresh-cadence tests don't wait real TTLs


def _reset_fake_service_state() -> None:
    _SCRATCH.clear()
    _VALID_TOKENS.clear()
    _ALWAYS_401.clear()
    _MINT_CALLS.clear()
    global _MINT_FAILS, _MINT_TTL_SECONDS
    _MINT_FAILS = False
    _MINT_TTL_SECONDS = 3600


class _FakeHandler(BaseHTTPRequestHandler):
    """Faithful-enough stub of the Java session-mint + T1-scratch contract."""

    def log_message(self, fmt, *args):  # noqa: A002 — matches BaseHTTPRequestHandler signature
        pass  # suppress test noise

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

    def _check_bearer(self) -> bool:
        if self.headers.get("Authorization", "") != f"Bearer {SERVICE_TOKEN}":
            self._send(401, {"error": "unauthorized"})
            return False
        return True

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        body = self._read_body()

        if path == "/v1/sessions/start":
            if not self._check_bearer():
                return
            session_id = body["session_id"]
            if _MINT_FAILS:
                # nexus-c8yvj finding 2: simulate a server-level auth failure
                # (e.g. a bad NX_SERVICE_TOKEN) -- a NON-bearer-check 403 so
                # callers see the same httpx.HTTPStatusError shape
                # raise_for_status() produces on any non-2xx.
                self._send(403, {"error": "mint forbidden (simulated)"})
                return
            _MINT_CALLS.append(session_id)
            token = f"tok-{session_id}-{uuid.uuid4().hex[:8]}"
            _VALID_TOKENS[session_id] = token
            self._send(
                200,
                {
                    "session_token": token,
                    "session_id": session_id,
                    "expires_in_seconds": _MINT_TTL_SECONDS,
                },
            )
            return

        if path == "/v1/sessions/close":
            if not self._check_bearer():
                return
            session_id = body.get("session_id", "")
            _VALID_TOKENS.pop(session_id, None)
            self._send(200, {"closed": 1})
            return

        if path.startswith("/v1/t1/"):
            if not self._check_bearer():
                return
            session_id = body.get("session_id", "")
            session_header = self.headers.get("X-Nexus-T1-Session", "")
            if session_id in _ALWAYS_401 or _VALID_TOKENS.get(session_id) != session_header:
                self._send(401, {"error": "unauthorized"})
                return
            self._handle_t1(path, body)
            return

        self._send(404, {"error": "not found"})

    def _handle_t1(self, path: str, body: dict[str, Any]) -> None:
        if path == "/v1/t1/put":
            id_ = body["id"]
            _SCRATCH[id_] = {
                "id": id_,
                "content": body["content"],
                "session_id": body["session_id"],
                "tags": body.get("tags", ""),
                "flagged": body.get("flagged", False),
                "flush_project": body.get("flush_project") or "",
                "flush_title": body.get("flush_title") or "",
                "agent": body.get("agent") or "",
                "access_count": 0,
                "last_accessed": "",
                "ts": "2026-07-11T00:00:00Z",
            }
            self._send(200, {"id": id_})
        elif path == "/v1/t1/get":
            id_ = body.get("id", "")
            session = body.get("session_id", "")
            entry = _SCRATCH.get(id_)
            if entry is None or entry["session_id"] != session:
                self._send(200, {"found": False})
            else:
                entry["access_count"] += 1
                self._send(200, dict(entry))
        elif path == "/v1/t1/list":
            session = body.get("session_id", "")
            entries = [e for e in _SCRATCH.values() if e["session_id"] == session]
            self._send(200, {"entries": entries})
        elif path == "/v1/t1/flagged":
            session = body.get("session_id", "")
            entries = [
                e for e in _SCRATCH.values()
                if e["session_id"] == session and e.get("flagged")
            ]
            self._send(200, {"entries": entries})
        elif path == "/v1/t1/resolve_prefix":
            prefix = body.get("prefix", "")
            session = body.get("session_id", "")
            matching = [
                e["id"] for e in _SCRATCH.values()
                if e["session_id"] == session and e["id"].startswith(prefix)
            ]
            self._send(200, {"ids": matching})
        elif path == "/v1/t1/session/close":
            session = body.get("session_id", "")
            to_delete = [k for k, v in _SCRATCH.items() if v["session_id"] == session]
            for k in to_delete:
                del _SCRATCH[k]
            self._send(200, {"deleted": len(to_delete)})
        else:
            self._send(404, {"error": "not found"})


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_service(monkeypatch: pytest.MonkeyPatch):
    """Start the fake service; point the SERVICE backend env at it.

    Every env var relevant to CLI-dedicated-path routing is reset here so
    each test starts from "bare CLI, no inherited session" — the autouse
    ``_isolate_t1_sessions`` fixture (conftest) sets ``NX_T1_ISOLATED=1``
    process-wide; tests in this file always delenv it (Path C otherwise
    wins ahead of the SERVICE routing this module tests).
    """
    _reset_fake_service_state()
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _FakeHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", str(port))
    monkeypatch.setenv("NX_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
    monkeypatch.delenv("NX_T1_ISOLATED", raising=False)
    monkeypatch.delenv("NX_T1_SESSION", raising=False)
    monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)

    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh, empty config dir for the cache-file tests.

    The conftest ``_isolate_config_dir`` autouse fixture already redirects
    ``NEXUS_CONFIG_DIR`` per test; this fixture just hands back the resolved
    path so assertions can inspect the cache file directly.
    """
    from nexus.config import nexus_config_dir

    return nexus_config_dir()


# ── Cache file: creation, reuse, race-safety ──────────────────────────────────


class TestDedicatedIdCacheFile:
    def test_created_on_first_use(self, fake_service, config_dir) -> None:
        from nexus.db.t1 import get_t1_database

        cache_path = config_dir / "t1_cli_dedicated_session"
        assert not cache_path.exists()
        get_t1_database()
        assert cache_path.exists()
        assert cache_path.read_text().strip()  # non-empty

    def test_reused_across_separate_invocations(self, fake_service, config_dir) -> None:
        """Two SEPARATE get_t1_database() calls (simulating two separate bare-CLI
        process invocations) must resolve the SAME dedicated session id — this is
        the continuity assertion the very first draft plan (fresh uuid4 per
        invocation) lacked."""
        from nexus.db.t1 import get_t1_database

        store1 = get_t1_database()
        store2 = get_t1_database()
        assert store1.session_id == store2.session_id

    def test_two_invocations_see_each_others_writes(self, fake_service, config_dir) -> None:
        """Real continuity: a write from one get_t1_database() call is visible
        to a second, independently-constructed get_t1_database() call."""
        from nexus.db.t1 import get_t1_database

        store1 = get_t1_database()
        doc_id = store1.put("shared across bare-CLI invocations", tags="continuity")

        store2 = get_t1_database()
        entry = store2.get(doc_id)
        assert entry is not None
        assert entry["content"] == "shared across bare-CLI invocations"

    def test_independent_of_resolve_active_session_id(
        self, fake_service, config_dir, monkeypatch
    ) -> None:
        """The dedicated id must NEVER equal NX_SESSION_ID / current_session's
        resolved id -- it is a separate, purpose-built identity namespace."""
        monkeypatch.setenv("NX_SESSION_ID", "impostor-live-mcp-session")
        from nexus.db.t1 import get_t1_database

        store = get_t1_database()
        assert store.session_id != "impostor-live-mcp-session"

        cache_path = config_dir / "t1_cli_dedicated_session"
        assert cache_path.read_text().strip() != "impostor-live-mcp-session"

    def test_first_creation_race_is_safe(self, fake_service, config_dir) -> None:
        """Two concurrent threads racing on first-use (no cache file yet) must
        converge on the SAME id -- the fcntl-election + atomic-publish pattern,
        not a last-write-wins clobber."""
        from nexus.db.t1 import _cli_dedicated_session_id

        results: list[str] = []
        barrier = threading.Barrier(8)

        def _worker() -> None:
            barrier.wait()
            results.append(_cli_dedicated_session_id(config_dir))

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert len(results) == 8
        assert len(set(results)) == 1, f"race produced divergent ids: {set(results)}"


# ── Live-MCP-session-present: unchanged behavior, no cache-file touch ─────────


class TestLiveSessionInherited:
    def test_does_not_touch_dedicated_cache_file(
        self, fake_service, config_dir, monkeypatch
    ) -> None:
        """When NX_T1_SESSION/NX_T1_SESSION_ID are present (inherited live MCP
        session), the factory must not read OR write the CLI-dedicated cache
        file at all."""
        # Seed a session token the inherited path can use directly.
        import httpx

        resp = httpx.post(
            f"{fake_service}/v1/sessions/start",
            json={"session_id": "live-mcp-session"},
            headers={
                "Authorization": f"Bearer {SERVICE_TOKEN}",
                "X-Nexus-Tenant": "default",
                "Content-Type": "application/json",
            },
        )
        minted = resp.json()["session_token"]

        monkeypatch.setenv("NX_T1_SESSION", minted)
        monkeypatch.setenv("NX_T1_SESSION_ID", "live-mcp-session")

        cache_path = config_dir / "t1_cli_dedicated_session"
        assert not cache_path.exists()

        from nexus.db.http_scratch_store import HttpScratchStore
        from nexus.db.t1 import get_t1_database

        result = get_t1_database()
        assert isinstance(result, HttpScratchStore)
        assert result.session_id == "live-mcp-session"
        assert not cache_path.exists(), (
            "inherited-session path must never read/write the CLI-dedicated cache file"
        )


# ── storage_backend_for: no more T1 special case ───────────────────────────────


def test_storage_backend_for_t1_no_special_case(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.db.storage_mode import StorageBackend, storage_backend_for

    monkeypatch.delenv("NX_STORAGE_BACKEND_T1", raising=False)
    monkeypatch.delenv("NX_STORAGE_BACKEND", raising=False)
    assert storage_backend_for("t1") == StorageBackend.SERVICE


# ── 401 self-heal: exactly one remint + retry ──────────────────────────────────


class TestSelfHeal:
    def test_stale_token_selfheals_and_recovers(self, fake_service, config_dir) -> None:
        """Simulate a concurrent-CLI-race: after the store's token was minted,
        a sibling process re-mints (rotating) the SAME dedicated session's
        token before this store's next operation. The wrapper must re-mint
        once and retry transparently -- the caller sees success, not a 401."""
        from nexus.db.t1 import _cli_dedicated_session_id, get_t1_database

        dedicated_id = _cli_dedicated_session_id(config_dir)
        store = get_t1_database()
        assert store.session_id == dedicated_id

        # Simulate a racing sibling rotating the token out from under us.
        rotate_calls_before = len(_MINT_CALLS)
        import httpx

        httpx.post(
            f"{fake_service}/v1/sessions/start",
            json={"session_id": dedicated_id},
            headers={
                "Authorization": f"Bearer {SERVICE_TOKEN}",
                "X-Nexus-Tenant": "default",
                "Content-Type": "application/json",
            },
        )
        assert len(_MINT_CALLS) == rotate_calls_before + 1

        # This store still holds the OLD (now-invalid) token -- put() must
        # self-heal (re-mint once) and succeed anyway.
        doc_id = store.put("selfheal probe content")
        assert isinstance(doc_id, str)
        assert store.get(doc_id)["content"] == "selfheal probe content"

    def test_persistent_401_retries_exactly_once_then_raises(
        self, fake_service, config_dir
    ) -> None:
        """A genuinely broken session (server always 401s regardless of token)
        must fail after exactly one remint retry -- not loop forever."""
        from nexus.db.http_scratch_store import SESSION_UNAUTHORIZED_MARKER
        from nexus.db.t1 import _cli_dedicated_session_id, get_t1_database

        dedicated_id = _cli_dedicated_session_id(config_dir)
        _ALWAYS_401.add(dedicated_id)

        store = get_t1_database()
        mint_calls_before = len(_MINT_CALLS)

        with pytest.raises(RuntimeError) as exc_info:
            store.put("this will never succeed")
        assert SESSION_UNAUTHORIZED_MARKER in str(exc_info.value)

        # Exactly one extra mint (the self-heal remint) -- not an unbounded retry loop.
        assert len(_MINT_CALLS) == mint_calls_before + 1


# ── Live-MCP-session lease (nexus-c8yvj finding 1) ─────────────────────────────
#
# A detached process (no NX_T1_SESSION/NX_T1_SESSION_ID inherited) that CAN
# resolve a live session's id via resolve_active_session_id() (e.g.
# NX_SESSION_ID env, mirroring the SessionEnd hook grandchild) must reach
# THAT session's data via a published lease -- never the disjoint
# CLI-dedicated identity, and never by re-minting (which would rotate the
# live MCP's token out from under it).


def _mint_live_session_token(fake_service: str, session_id: str) -> str:
    """Mint a token directly against the fake service, simulating what
    mcp/core.py's _t1_lifespan Branch 0 does for a live MCP session."""
    import httpx

    resp = httpx.post(
        f"{fake_service}/v1/sessions/start",
        json={"session_id": session_id},
        headers={
            "Authorization": f"Bearer {SERVICE_TOKEN}",
            "X-Nexus-Tenant": "default",
            "Content-Type": "application/json",
        },
    )
    resp.raise_for_status()
    return resp.json()["session_token"]


class TestLiveSessionLease:
    def test_publish_read_clear_roundtrip(self, tmp_path: Path) -> None:
        """Unit-level: the lease helpers round-trip a token and clear cleanly."""
        from nexus.db.t1 import (
            clear_t1_session_lease,
            publish_t1_session_lease,
            read_t1_session_lease,
        )

        assert read_t1_session_lease("sess-1", tmp_path) is None

        publish_t1_session_lease("sess-1", "secret-token", tmp_path)
        assert read_t1_session_lease("sess-1", tmp_path) == "secret-token"
        # A DIFFERENT session id must not see this lease.
        assert read_t1_session_lease("sess-2", tmp_path) is None

        clear_t1_session_lease("sess-1", tmp_path)
        assert read_t1_session_lease("sess-1", tmp_path) is None
        # Double-clear is a no-op, not an error.
        clear_t1_session_lease("sess-1", tmp_path)

    def test_lease_file_mode_is_0600(self, tmp_path: Path) -> None:
        import stat

        from nexus.db.t1 import _t1_session_lease_path, publish_t1_session_lease

        publish_t1_session_lease("sess-perm", "secret-token", tmp_path)
        path = _t1_session_lease_path("sess-perm", tmp_path)
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    def test_get_t1_database_uses_published_lease_over_cli_dedicated(
        self, fake_service, config_dir, monkeypatch
    ) -> None:
        """A resolvable session id (NX_SESSION_ID, mirroring the SessionEnd
        hook's resolve_active_session_id() chain) with a published lease
        must resolve to THAT session -- not the CLI-dedicated identity --
        and must NOT touch the CLI-dedicated cache file at all."""
        from nexus.db.http_scratch_store import HttpScratchStore
        from nexus.db.t1 import get_t1_database, publish_t1_session_lease

        live_session_id = "live-mcp-session-c8yvj"
        live_token = _mint_live_session_token(fake_service, live_session_id)
        publish_t1_session_lease(live_session_id, live_token, config_dir)

        monkeypatch.setenv("NX_SESSION_ID", live_session_id)

        cache_path = config_dir / "t1_cli_dedicated_session"
        assert not cache_path.exists()

        result = get_t1_database()
        assert isinstance(result, HttpScratchStore)
        assert result.session_id == live_session_id
        assert not cache_path.exists(), (
            "the lease path must never read/write the CLI-dedicated cache file"
        )

    def test_hook_process_reads_same_data_live_mcp_wrote(
        self, fake_service, config_dir, monkeypatch
    ) -> None:
        """End-to-end proof of the fix: a write made under the live MCP
        session's own directly-constructed store (simulating in-MCP-process
        writes) is visible to a SEPARATE get_t1_database() call that only
        has NX_SESSION_ID set (simulating the detached SessionEnd hook
        grandchild) via the published lease -- not silently empty."""
        from nexus.db.http_scratch_store import HttpScratchStore
        from nexus.db.t1 import get_t1_database, publish_t1_session_lease

        live_session_id = "live-mcp-session-writer"
        live_token = _mint_live_session_token(fake_service, live_session_id)
        publish_t1_session_lease(live_session_id, live_token, config_dir)

        live_store = HttpScratchStore(session_id=live_session_id, _session_token=live_token)
        doc_id = live_store.put("written by the live MCP session", tags="c8yvj")

        # Simulate the detached hook process: NX_T1_SESSION/NX_T1_SESSION_ID
        # unset, only the on-disk-resolvable NX_SESSION_ID present.
        monkeypatch.delenv("NX_T1_SESSION", raising=False)
        monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)
        monkeypatch.setenv("NX_SESSION_ID", live_session_id)

        hook_store = get_t1_database()
        entry = hook_store.get(doc_id)
        assert entry is not None, (
            "the hook process must see the live session's data via the lease, "
            "not silently read an empty, disjoint CLI-dedicated session"
        )
        assert entry["content"] == "written by the live MCP session"

    def test_no_lease_falls_through_to_cli_dedicated(
        self, fake_service, config_dir, monkeypatch
    ) -> None:
        """A resolvable session id with NO published lease (e.g. a stale
        current_session pointer with no live MCP, or a live MCP that never
        resolved a session id itself) must fall through to the unchanged
        CLI-dedicated path -- not raise, not silently do nothing."""
        from nexus.db.t1 import _CliDedicatedScratchStore, _cli_dedicated_session_id, get_t1_database

        monkeypatch.setenv("NX_SESSION_ID", "resolvable-but-no-lease-published")

        result = get_t1_database()
        assert isinstance(result, _CliDedicatedScratchStore)
        assert result.session_id == _cli_dedicated_session_id(config_dir)
        assert result.session_id != "resolvable-but-no-lease-published"

    def test_stale_lease_after_clear_falls_through(
        self, fake_service, config_dir, monkeypatch
    ) -> None:
        """After clear_t1_session_lease (MCP teardown), a later process with
        the same resolvable session id must fall through to CLI-dedicated,
        not read a removed lease."""
        from nexus.db.t1 import (
            _CliDedicatedScratchStore,
            _cli_dedicated_session_id,
            clear_t1_session_lease,
            get_t1_database,
            publish_t1_session_lease,
        )

        live_session_id = "live-mcp-session-torn-down"
        live_token = _mint_live_session_token(fake_service, live_session_id)
        publish_t1_session_lease(live_session_id, live_token, config_dir)
        clear_t1_session_lease(live_session_id, config_dir)

        monkeypatch.setenv("NX_SESSION_ID", live_session_id)

        result = get_t1_database()
        assert isinstance(result, _CliDedicatedScratchStore)
        assert result.session_id == _cli_dedicated_session_id(config_dir)

    def test_stale_unlinked_lease_degrades_to_clean_runtime_error(
        self, fake_service, config_dir, monkeypatch
    ) -> None:
        """A lease that outlives its session (live MCP crashed without
        teardown, e.g. SIGKILL -- the token was never server-side revoked
        so it is actually still valid here, but simulate the harsher case:
        the server has since forgotten the token) surfaces as a plain
        RuntimeError on first use, matching the pre-existing best-effort
        "T1 unavailable, logged, flush skipped" contract -- not a crash."""
        from nexus.db.http_scratch_store import SESSION_UNAUTHORIZED_MARKER
        from nexus.db.t1 import get_t1_database, publish_t1_session_lease

        live_session_id = "live-mcp-session-crashed"
        publish_t1_session_lease(live_session_id, "no-longer-a-real-token", config_dir)
        _ALWAYS_401.add(live_session_id)

        monkeypatch.setenv("NX_SESSION_ID", live_session_id)

        store = get_t1_database()
        with pytest.raises(RuntimeError) as exc_info:
            store.flagged_entries()
        assert SESSION_UNAUTHORIZED_MARKER in str(exc_info.value)


# ── Lease freshness / TTL (nexus-ngcpo Finding 2) ──────────────────────────────
#
# Pre-ngcpo, read_t1_session_lease was a bare file read with no TTL check at
# all -- any lease file that happened to exist was trusted forever, including
# one abandoned by a prior MCP session that exited uncleanly. These tests
# lock the fix: a lease past its stored expiry (or one that fails to parse as
# the new JSON format, e.g. a stale pre-ngcpo bare-token file) is treated as
# absent, while a genuinely fresh lease is still returned exactly as before.


class TestLeaseFreshness:
    def test_fresh_lease_is_returned(self, tmp_path: Path) -> None:
        from nexus.db.t1 import publish_t1_session_lease, read_t1_session_lease

        publish_t1_session_lease("sess-fresh", "tok-fresh", tmp_path, ttl_seconds=3600)
        assert read_t1_session_lease("sess-fresh", tmp_path) == "tok-fresh"

    def test_stale_lease_is_not_returned(self, tmp_path: Path) -> None:
        """A negative ttl_seconds publishes a lease already past its expiry --
        simulating an owner that stopped refreshing well before this read."""
        from nexus.db.t1 import publish_t1_session_lease, read_t1_session_lease

        publish_t1_session_lease("sess-stale", "tok-stale", tmp_path, ttl_seconds=-1.0)
        assert read_t1_session_lease("sess-stale", tmp_path) is None

    def test_lease_at_exact_expiry_boundary_is_stale(self, tmp_path: Path) -> None:
        """expires_at == now must be treated as stale (>=, not >) -- an exact
        tie should never be trusted as fresh."""
        import json

        from nexus.db.t1 import _t1_session_lease_path, read_t1_session_lease

        path = _t1_session_lease_path("sess-boundary", tmp_path)
        path.write_text(json.dumps({"token": "tok-boundary", "expires_at": 0.0}))
        assert read_t1_session_lease("sess-boundary", tmp_path) is None

    def test_legacy_bare_token_file_is_treated_as_absent(self, tmp_path: Path) -> None:
        """A pre-ngcpo lease file (plain token text, not JSON) must not crash
        the reader and must not be trusted indefinitely -- fail-safe on a
        format bump, not fail-open."""
        from nexus.db.t1 import _t1_session_lease_path, read_t1_session_lease

        path = _t1_session_lease_path("sess-legacy", tmp_path)
        path.write_text("legacy-plain-token-no-json")
        assert read_t1_session_lease("sess-legacy", tmp_path) is None

    def test_default_ttl_used_when_unspecified(self, tmp_path: Path) -> None:
        """Existing call sites that omit ttl_seconds (back-compat) must still
        publish a lease that reads back fresh."""
        from nexus.db.t1 import publish_t1_session_lease, read_t1_session_lease

        publish_t1_session_lease("sess-default-ttl", "tok-default", tmp_path)
        assert read_t1_session_lease("sess-default-ttl", tmp_path) == "tok-default"


class TestStaleLeaseFallthrough:
    """Integration: a stale lease must not be borrowed by either consumer --
    get_t1_database()'s tier-2 borrow path falls through to CLI-dedicated
    (this class), and mcp.core's Branch-0 self-check mints fresh + takes
    ownership (TestBranch0StaleLeaseRecovery below)."""

    def test_stale_lease_falls_through_to_cli_dedicated(
        self, fake_service, config_dir, monkeypatch
    ) -> None:
        from nexus.db.t1 import (
            _CliDedicatedScratchStore,
            _cli_dedicated_session_id,
            get_t1_database,
            publish_t1_session_lease,
        )

        live_session_id = "live-mcp-session-stale-ttl"
        live_token = _mint_live_session_token(fake_service, live_session_id)
        # Already expired: nobody has refreshed this lease since it was
        # published, simulating a dead original owner.
        publish_t1_session_lease(live_session_id, live_token, config_dir, ttl_seconds=-1.0)

        monkeypatch.setenv("NX_SESSION_ID", live_session_id)

        result = get_t1_database()
        assert isinstance(result, _CliDedicatedScratchStore)
        assert result.session_id == _cli_dedicated_session_id(config_dir)
        assert result.session_id != live_session_id


class TestSessionEndFlushViaLease:
    """The actual regression this bead fixes: session_end_flush() (the
    SessionEnd hook) must flush a flagged entry written under the live MCP
    session, not silently report 0 because it read the disjoint
    CLI-dedicated identity."""

    def test_flagged_entry_is_flushed_via_published_lease(
        self, fake_service, config_dir, monkeypatch, tmp_path
    ) -> None:
        from nexus.db.http_scratch_store import HttpScratchStore
        from nexus.db.t1 import publish_t1_session_lease
        from nexus.hooks import session_end_flush

        # T1 only: keep T2 (memory.db / telemetry / expire) on its normal
        # test-suite SQLite default -- the fake service in this file only
        # implements the T1 + session-mint contract, not T2's. The global
        # override from `fake_service` is per-store-overridable; T1 wins on
        # the per-store var, T2 falls back to the (conftest-default) global.
        monkeypatch.setenv("NX_STORAGE_BACKEND_T1", "service")
        monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")

        live_session_id = "live-mcp-session-flush"
        live_token = _mint_live_session_token(fake_service, live_session_id)
        publish_t1_session_lease(live_session_id, live_token, config_dir)

        # Simulate the live MCP session's own in-process writes: a
        # pre-flagged (persist=True) scratch entry, matching how a real
        # ``nx scratch put --persist`` call flags on insert.
        live_store = HttpScratchStore(session_id=live_session_id, _session_token=live_token)
        live_store.put(
            "flagged content that must reach T2",
            persist=True,
            flush_project="c8yvj_test_project",
            flush_title="c8yvj_test_title",
        )
        assert len(live_store.flagged_entries()) == 1

        # The SessionEnd hook: a detached process with NX_T1_SESSION/
        # NX_T1_SESSION_ID unset, only NX_SESSION_ID resolvable.
        monkeypatch.delenv("NX_T1_SESSION", raising=False)
        monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)
        monkeypatch.setenv("NX_SESSION_ID", live_session_id)

        def _no_daemon(**_kwargs):
            from nexus.daemon.t2_client import T2DaemonNotReachableError
            raise T2DaemonNotReachableError("no daemon in tests")

        import unittest.mock as mock
        with mock.patch("nexus.daemon.t2_client.make_t2_client", _no_daemon):
            output = session_end_flush()

        assert "Flushed 1" in output, (
            f"expected the flagged entry to be flushed via the published lease, got: {output!r}"
        )

    def test_flush_still_reports_zero_without_a_lease_or_env(
        self, fake_service, config_dir, monkeypatch
    ) -> None:
        """Sanity companion: with NO resolvable session id at all (the
        original pre-rn3wo.1 'nothing to flush' case), the hook still
        reports 0 without raising -- this asserts the fix is additive, not
        a change to the no-session baseline."""
        from nexus.hooks import session_end_flush

        monkeypatch.setenv("NX_STORAGE_BACKEND_T1", "service")
        monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")
        monkeypatch.delenv("NX_T1_SESSION", raising=False)
        monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)
        monkeypatch.delenv("NX_SESSION_ID", raising=False)

        def _no_daemon(**_kwargs):
            from nexus.daemon.t2_client import T2DaemonNotReachableError
            raise T2DaemonNotReachableError("no daemon in tests")

        import unittest.mock as mock
        with mock.patch("nexus.daemon.t2_client.make_t2_client", _no_daemon):
            output = session_end_flush()

        assert "Flushed 0" in output


# ── Clean-error wrapping for HttpTokenStore.start_session (nexus-c8yvj finding 2) ──
#
# HttpTokenStore._post calls resp.raise_for_status(), which raises
# httpx.HTTPStatusError on a non-2xx -- NOT a RuntimeError. Every downstream
# handler meant to turn this into a clean click.ClickException
# (_CliDedicatedScratchStore._call's `except RuntimeError`,
# commands/scratch.py's _clean_service_errors) only catches RuntimeError, so
# an unwrapped httpx error would propagate as a raw traceback instead.


class TestMintErrorWrapping:
    def test_construction_time_mint_failure_raises_runtime_error(
        self, fake_service, config_dir
    ) -> None:
        """get_t1_database()'s CLI-dedicated construction-time mint must
        wrap a non-RuntimeError mint failure (e.g. httpx.HTTPStatusError
        from a bad NX_SERVICE_TOKEN) as a clean RuntimeError."""
        import httpx

        from nexus.db.t1 import get_t1_database

        global _MINT_FAILS
        _MINT_FAILS = True
        try:
            with pytest.raises(RuntimeError) as exc_info:
                get_t1_database()
            assert not isinstance(exc_info.value, httpx.HTTPStatusError)
        finally:
            _MINT_FAILS = False

    def test_remint_failure_raises_runtime_error_not_httpx_error(
        self, fake_service, config_dir
    ) -> None:
        """_CliDedicatedScratchStore._remint()'s re-mint (triggered by a
        self-heal 401) must ALSO wrap a non-RuntimeError mint failure as a
        clean RuntimeError, not let it propagate raw past _call's
        `except RuntimeError`."""
        import httpx

        from nexus.db.t1 import _cli_dedicated_session_id, get_t1_database

        dedicated_id = _cli_dedicated_session_id(config_dir)
        store = get_t1_database()
        assert store.session_id == dedicated_id

        # Force the self-heal path (stale token) AND make the re-mint itself
        # fail server-side (simulated broken auth discovered mid-session).
        _ALWAYS_401.add(dedicated_id)
        global _MINT_FAILS
        _MINT_FAILS = True
        try:
            with pytest.raises(RuntimeError) as exc_info:
                store.put("this remint must fail cleanly")
            assert not isinstance(exc_info.value, httpx.HTTPStatusError)
        finally:
            _MINT_FAILS = False

    def test_branch0_mint_failure_DEFERS_and_the_server_starts(
        self, fake_service, config_dir, monkeypatch
    ) -> None:
        """nexus-brw1s (GH #1405, field report stevengharris): a startup mint
        failure must NEVER crash the server. The old contract raised here; the
        RuntimeError escaped the stdio TaskGroup, the whole MCP server died,
        and Claude Code cached the dead connection for the session's entire
        lifetime — every nexus tool gone, including the majority that never
        touch T1, because a scratch precondition could not reach a service
        that was merely not up yet.

        New contract, all pinned here: the lifespan REACHES YIELD (the server
        starts); the deferred-mint retry hook is registered with mcp_infra;
        no session env vars are set (Phase E require-minted holds — there is
        no bare-id fallback); and lifespan exit unregisters the hook and
        clears the deferred state so nothing dangles."""
        import asyncio

        from nexus import mcp_infra
        from nexus.mcp import core as mcp_core

        live_session_id = "mcp-branch0-mint-deferral-test"
        monkeypatch.setattr(
            "nexus.session.resolve_active_session_id", lambda: live_session_id
        )
        monkeypatch.delenv("NX_T1_SESSION", raising=False)
        monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)

        global _MINT_FAILS
        _MINT_FAILS = True
        reached = {"yield": False}

        async def _run() -> None:
            async with mcp_core._t1_lifespan(None):
                reached["yield"] = True
                # During the deferred window: hook registered, state present,
                # NO session env (a bare-id or CLI-dedicated fallback would be
                # the session-isolation regression the lifespan documents).
                assert mcp_infra._t1_pre_init_hook is mcp_core._retry_deferred_t1_mint
                assert mcp_core._DEFERRED_T1_MINT["session_id"] == live_session_id
                assert "NX_T1_SESSION" not in _t1_env()

        def _t1_env() -> dict:
            import os
            return {k: v for k, v in os.environ.items() if k == "NX_T1_SESSION"}

        try:
            asyncio.run(_run())
        finally:
            _MINT_FAILS = False

        assert reached["yield"], "the server must start despite the mint failure"
        # Exit cleaned up: no dangling hook, no dangling deferred state.
        assert mcp_infra._t1_pre_init_hook is None
        assert not mcp_core._DEFERRED_T1_MINT

    def test_deferred_mint_retry_fails_per_call_and_stays_retryable(
        self, fake_service, config_dir, monkeypatch
    ) -> None:
        """While the service stays down, each T1-touching call fails with an
        ACTIONABLE error and nothing is cached — the next call retries. The
        walk to recovery is 'start the service', never 'restart the Claude
        session'."""
        from nexus import mcp_infra
        from nexus.mcp import core as mcp_core

        monkeypatch.setattr(mcp_infra, "_t1_instance", None)
        monkeypatch.setattr(mcp_infra, "_t1_isolated", False)
        mcp_core._DEFERRED_T1_MINT.clear()
        mcp_core._DEFERRED_T1_MINT.update(
            session_id="deferred-retry-test", config_dir=config_dir, loop=None
        )
        mcp_infra.set_t1_pre_init_hook(mcp_core._retry_deferred_t1_mint)

        calls = {"n": 0}

        def _still_down(_sid, _cfg):
            calls["n"] += 1
            raise RuntimeError("connect refused")

        monkeypatch.setattr(
            "nexus.db.t1._lock_guarded_mint_or_borrow", _still_down
        )
        try:
            for attempt in (1, 2):
                with pytest.raises(RuntimeError) as exc_info:
                    mcp_infra.get_t1()
                assert "nx daemon service start" in str(exc_info.value)
                # The honest blast-radius claim (critic SIG-1): T1 only —
                # never "every non-T1 tool is unaffected", which is false in
                # cloud mode (the nexus-5t1jp probe cache).
                assert "affects T1 scratch only" in str(exc_info.value)
                assert mcp_infra._t1_instance is None, "a failure must cache nothing"
                assert calls["n"] == attempt, "every call must retry the mint"
            # The hook stays registered for the next call.
            assert mcp_infra._t1_pre_init_hook is not None
        finally:
            mcp_core._DEFERRED_T1_MINT.clear()
            mcp_infra.set_t1_pre_init_hook(None)
            mcp_infra._t1_instance = None

    def test_deferred_mint_completes_when_the_service_arrives(
        self, fake_service, config_dir, monkeypatch
    ) -> None:
        """The service came up mid-session: the FIRST T1-touching call mints
        through the same flock/borrow discipline, sets the session env,
        records ownership, unregisters the hook, and construction proceeds —
        the session behaves as if the mint had succeeded at startup.

        The construction stub RECORDS the env it saw, so this also pins the
        ORDER: hook completes the mint BEFORE the T1 handle is built (built
        with no session env, the handle would route into the shared
        CLI-dedicated identity — the security regression)."""
        import os

        from nexus import mcp_infra
        from nexus.mcp import core as mcp_core

        monkeypatch.setattr(mcp_infra, "_t1_instance", None)
        monkeypatch.setattr(mcp_infra, "_t1_isolated", False)
        monkeypatch.delenv("NX_T1_SESSION", raising=False)
        monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)
        mcp_core._OWNED_T1_SESSION.pop("session_id", None)
        mcp_core._DEFERRED_T1_MINT.clear()
        mcp_core._DEFERRED_T1_MINT.update(
            session_id="deferred-arrival-test", config_dir=config_dir, loop=None
        )
        mcp_infra.set_t1_pre_init_hook(mcp_core._retry_deferred_t1_mint)

        minted = {"n": 0}

        def _service_up(_sid, _cfg):
            minted["n"] += 1
            return "tok-deferred-arrival", True, 3600.0

        monkeypatch.setattr(
            "nexus.db.t1._lock_guarded_mint_or_borrow", _service_up
        )
        seen_env = {}

        def _construct_stub():
            seen_env["NX_T1_SESSION"] = os.environ.get("NX_T1_SESSION")
            seen_env["NX_T1_SESSION_ID"] = os.environ.get("NX_T1_SESSION_ID")
            return object()

        monkeypatch.setattr("nexus.db.t1.get_t1_database", _construct_stub)
        try:
            t1_first, _ = mcp_infra.get_t1()
            # The mint landed BEFORE construction, with the minted values.
            assert seen_env["NX_T1_SESSION"] == "tok-deferred-arrival"
            assert seen_env["NX_T1_SESSION_ID"] == "deferred-arrival-test"
            # Ownership recorded (SIGTERM/atexit revocation still works);
            # hook + state cleared; loop=None -> refresh skipped with a
            # warning, never a failure of the successful mint.
            assert mcp_core._OWNED_T1_SESSION.get("session_id") == "deferred-arrival-test"
            assert mcp_infra._t1_pre_init_hook is None
            assert not mcp_core._DEFERRED_T1_MINT
            # Cached: the second call re-mints nothing and reuses the handle.
            t1_second, _ = mcp_infra.get_t1()
            assert t1_second is t1_first
            assert minted["n"] == 1
        finally:
            mcp_core._OWNED_T1_SESSION.pop("session_id", None)
            mcp_infra._t1_instance = None
            os.environ.pop("NX_T1_SESSION", None)
            os.environ.pop("NX_T1_SESSION_ID", None)


# ── mcp/core.py Branch 0 wiring: publish-on-mint, clear-on-teardown ────────────
#
# The lease helpers and get_t1_database()'s consumption of them are covered
# above; this class proves the OTHER half -- that _t1_lifespan's
# Branch 0 (the live MCP session's own mint path) actually calls
# publish_t1_session_lease / clear_t1_session_lease at the right times, not
# just that the helpers work in isolation. code-review-expert previously
# caught a real wiring gap in this exact function (the NX_T1_ISOLATED HIGH
# finding on nexus-rn3wo.1's first pass), so the wiring itself -- not only
# the helper functions -- needs its own regression lock.


class TestMcpCoreLeaseWiring:
    @pytest.fixture(autouse=True)
    def _reset_branch0_shutdown_state(self):
        """nexus-5daww: Branch 0's normal post-yield teardown now routes
        through the SAME ``_t1_shutdown()`` used by Branch 3 / SIGTERM
        / atexit, which sets the module-sticky ``_SHUTDOWN_IN_FLIGHT`` flag
        ("once set, never cleared: shutdown is one-shot per process") and
        clears ``_OWNED_T1_SESSION``. Without a per-test reset, the FIRST
        test in this class to exercise a real mint+teardown would leave
        ``_SHUTDOWN_IN_FLIGHT=True`` for the rest of the test process, and
        every later test's ``_t1_shutdown()`` call would silently
        no-op (lease never cleared, token never closed) -- mirroring the
        existing save/restore idiom in
        ``tests/test_t1_discovery.py::test_sigterm_path_cleans_up_via_owned_chroma``.
        """
        from nexus.mcp import core as mcp_core

        prev_inflight = mcp_core._SHUTDOWN_IN_FLIGHT
        prev_owned_session = dict(mcp_core._OWNED_T1_SESSION)
        mcp_core._SHUTDOWN_IN_FLIGHT = False
        mcp_core._OWNED_T1_SESSION.clear()
        # nexus-ngcpo: also guard against a refresh task leaking across
        # tests -- every test in this class exercises the real lifespan and
        # its own teardown path cancels the task it started, but a failure
        # mid-test should not leave a stray task pending for the NEXT test.
        assert mcp_core._T1_SESSION_REFRESH_TASK is None, (
            "a prior test in this class leaked a live refresh task"
        )
        try:
            yield
        finally:
            mcp_core._SHUTDOWN_IN_FLIGHT = prev_inflight
            mcp_core._OWNED_T1_SESSION.clear()
            mcp_core._OWNED_T1_SESSION.update(prev_owned_session)
            if mcp_core._T1_SESSION_REFRESH_TASK is not None:
                mcp_core._T1_SESSION_REFRESH_TASK.cancel()
                mcp_core._T1_SESSION_REFRESH_TASK = None

    def test_branch0_publishes_lease_on_mint_and_clears_on_teardown(
        self, fake_service, config_dir, monkeypatch
    ) -> None:
        import asyncio
        import os as _os_mod

        from nexus.db.t1 import _t1_session_lease_path, read_t1_session_lease
        from nexus.mcp import core as mcp_core

        live_session_id = "mcp-branch0-wiring-test"
        monkeypatch.setattr(
            "nexus.session.resolve_active_session_id", lambda: live_session_id
        )

        lease_path = _t1_session_lease_path(live_session_id, config_dir)
        assert not lease_path.exists()

        seen: dict[str, str | None] = {"lease_token": None, "env_token": None}

        async def _run() -> None:
            async with mcp_core._t1_lifespan(None):
                assert lease_path.exists(), (
                    "Branch 0 must publish the lease during the live session, "
                    "before yield"
                )
                seen["lease_token"] = read_t1_session_lease(live_session_id, config_dir)
                seen["env_token"] = _os_mod.environ.get("NX_T1_SESSION")

        asyncio.run(_run())

        assert seen["lease_token"] is not None
        assert seen["lease_token"] == seen["env_token"], (
            "the published lease must carry the SAME token the live MCP "
            "session minted for itself"
        )
        assert not lease_path.exists(), (
            "Branch 0 teardown must clear the lease so a later process never "
            "reads a stale one"
        )

    def test_nested_mcp_with_inherited_token_does_not_remint(
        self, fake_service, config_dir, monkeypatch
    ) -> None:
        """nexus-5daww CRITICAL repro: a nested `nx-mcp` subprocess that
        inherited an ALREADY-LIVE NX_T1_SESSION/NX_T1_SESSION_ID from its
        parent (e.g. via operators.dispatch.claude_dispatch's tool-granting
        env, pre-defense-in-depth-strip) must use that token AS-IS -- never
        call HttpTokenStore.start_session again for the same session id,
        which would rotate (ON CONFLICT DO UPDATE) the parent's live token
        out from under it.
        """
        import asyncio
        import os as _os_mod

        from nexus.mcp import core as mcp_core

        live_session_id = "mcp-nested-inherited-test"

        # Simulate the PARENT having already minted (first Branch 0 pass).
        import httpx

        resp = httpx.post(
            f"{fake_service}/v1/sessions/start",
            json={"session_id": live_session_id},
            headers={
                "Authorization": f"Bearer {SERVICE_TOKEN}",
                "X-Nexus-Tenant": "default",
                "Content-Type": "application/json",
            },
        )
        parent_token = resp.json()["session_token"]
        mint_calls_before = len(_MINT_CALLS)

        # Simulate the NESTED subprocess inheriting the parent's env verbatim
        # (the pre-fix / no-defense-in-depth scenario).
        monkeypatch.setenv("NX_T1_SESSION", parent_token)
        monkeypatch.setenv("NX_T1_SESSION_ID", live_session_id)
        monkeypatch.setattr(
            "nexus.session.resolve_active_session_id", lambda: live_session_id
        )

        async def _run() -> None:
            async with mcp_core._t1_lifespan(None):
                # The inherited token must be left untouched, in place.
                assert _os_mod.environ.get("NX_T1_SESSION") == parent_token

        asyncio.run(_run())

        assert len(_MINT_CALLS) == mint_calls_before, (
            "a nested MCP inheriting a live token must NEVER call "
            "HttpTokenStore.start_session again for the same session id"
        )
        # The nested process does not own the session -- its exit must not
        # revoke the parent's still-live token.
        assert _VALID_TOKENS.get(live_session_id) == parent_token, (
            "a nested MCP that only borrowed an inherited token must not "
            "close/revoke it on its own exit -- it does not own the session"
        )

    def test_resolved_session_with_published_lease_does_not_remint(
        self, fake_service, config_dir, monkeypatch
    ) -> None:
        """nexus-5daww defense-in-depth: even WITHOUT a directly-inherited
        NX_T1_SESSION (e.g. operators.dispatch's env-stripping fix removed
        it), a nested MCP that resolves the SAME session id as a live
        ancestor must consult the ancestor's PUBLISHED LEASE
        (nexus-c8yvj's publish_t1_session_lease / read_t1_session_lease)
        before minting -- and must NOT mint when a live lease is found.
        """
        import asyncio

        from nexus.db.t1 import publish_t1_session_lease
        from nexus.mcp import core as mcp_core

        live_session_id = "mcp-leased-no-inherit-test"

        # Simulate an ancestor's successful mint + lease publish.
        import httpx

        resp = httpx.post(
            f"{fake_service}/v1/sessions/start",
            json={"session_id": live_session_id},
            headers={
                "Authorization": f"Bearer {SERVICE_TOKEN}",
                "X-Nexus-Tenant": "default",
                "Content-Type": "application/json",
            },
        )
        ancestor_token = resp.json()["session_token"]
        publish_t1_session_lease(live_session_id, ancestor_token, config_dir)
        mint_calls_before = len(_MINT_CALLS)

        # NO inherited NX_T1_SESSION / NX_T1_SESSION_ID in env (the
        # defense-in-depth-stripped scenario) -- only a resolvable session id.
        monkeypatch.delenv("NX_T1_SESSION", raising=False)
        monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)
        monkeypatch.setattr(
            "nexus.session.resolve_active_session_id", lambda: live_session_id
        )

        seen: dict[str, str | None] = {"env_token": None}

        async def _run() -> None:
            async with mcp_core._t1_lifespan(None):
                import os as _os_mod
                seen["env_token"] = _os_mod.environ.get("NX_T1_SESSION")

        asyncio.run(_run())

        assert len(_MINT_CALLS) == mint_calls_before, (
            "a resolvable session id with a LIVE published lease must "
            "reuse that lease's token, never mint a competing one"
        )
        assert seen["env_token"] == ancestor_token

    def test_sigterm_before_normal_exit_revokes_token_and_clears_lease(
        self, fake_service, config_dir, monkeypatch
    ) -> None:
        """The second nexus-5daww CRITICAL: `_t1_shutdown()` must
        close the Branch-0-minted token and clear its lease even when
        invoked from a SIGTERM / atexit path that never resumes the paused
        lifespan generator past its `yield` (the documented NORMAL stdio
        shutdown path -- `_sigterm_handler` calls `os._exit(0)` right after
        `_t1_shutdown()`). Simulated here by calling
        `_t1_shutdown()` directly from INSIDE the `async with` block,
        before ever letting the lifespan's own post-yield teardown run.
        """
        import asyncio

        from nexus.db.t1 import _t1_session_lease_path, read_t1_session_lease
        from nexus.mcp import core as mcp_core

        live_session_id = "mcp-sigterm-branch0-test"
        monkeypatch.setattr(
            "nexus.session.resolve_active_session_id", lambda: live_session_id
        )

        lease_path = _t1_session_lease_path(live_session_id, config_dir)

        async def _run() -> None:
            async with mcp_core._t1_lifespan(None):
                assert lease_path.exists()
                assert _VALID_TOKENS.get(live_session_id) is not None

                # SIGTERM-equivalent: the signal handler / atexit path calls
                # this directly, WITHOUT the generator ever resuming past
                # this yield.
                mcp_core._t1_shutdown()

                assert not lease_path.exists(), (
                    "SIGTERM path must clear the lease file, not just the "
                    "normal async-exit teardown"
                )
                assert _VALID_TOKENS.get(live_session_id) is None, (
                    "SIGTERM path must revoke the minted token server-side, "
                    "not just the normal async-exit teardown"
                )
                assert read_t1_session_lease(live_session_id, config_dir) is None

            # Exiting the `async with` now runs the lifespan's own finally,
            # which must be idempotent (no error, no double-close) since
            # `_t1_shutdown` already ran and cleared its state.

        asyncio.run(_run())


# ── Refresh task (nexus-ngcpo Finding 1) ───────────────────────────────────────
#
# Branch 0 previously minted a session token ONCE at MCP startup and never
# again. These tests prove the periodic refresh task actually re-mints
# before the token's TTL would expire -- using a short test TTL (via the
# fake service's overridable ``_MINT_TTL_SECONDS``) rather than waiting 24h.


class TestBranch0RefreshTask:
    @pytest.fixture(autouse=True)
    def _reset_branch0_state(self):
        from nexus.mcp import core as mcp_core

        prev_inflight = mcp_core._SHUTDOWN_IN_FLIGHT
        prev_owned_session = dict(mcp_core._OWNED_T1_SESSION)
        mcp_core._SHUTDOWN_IN_FLIGHT = False
        mcp_core._OWNED_T1_SESSION.clear()
        try:
            yield
        finally:
            mcp_core._SHUTDOWN_IN_FLIGHT = prev_inflight
            mcp_core._OWNED_T1_SESSION.clear()
            mcp_core._OWNED_T1_SESSION.update(prev_owned_session)
            if mcp_core._T1_SESSION_REFRESH_TASK is not None:
                mcp_core._T1_SESSION_REFRESH_TASK.cancel()
                mcp_core._T1_SESSION_REFRESH_TASK = None

    def test_refresh_loop_remints_before_ttl_expiry(
        self, fake_service, config_dir, monkeypatch
    ) -> None:
        """Unit-level: call `_t1_session_refresh_loop` directly with a tiny
        interval and observe it re-mint (and republish the lease with a
        fresh token + expiry) multiple times, without waiting anywhere near
        a real 24h TTL."""
        import asyncio

        from nexus.db.t1 import read_t1_session_lease
        from nexus.mcp.core import _t1_session_refresh_loop

        session_id = "refresh-loop-direct-test"
        # Seed an initial token/lease as if a mint had already happened.
        first_token = _mint_live_session_token(fake_service, session_id)
        from nexus.db.t1 import publish_t1_session_lease
        publish_t1_session_lease(session_id, first_token, config_dir, ttl_seconds=3600)

        monkeypatch.setenv("NX_T1_SESSION", first_token)
        mint_calls_before_loop = len(_MINT_CALLS)

        async def _run() -> None:
            task = asyncio.create_task(_t1_session_refresh_loop(session_id, 0.05))
            try:
                await asyncio.sleep(0.23)
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

        asyncio.run(_run())

        # At least a couple of refresh ticks fired in ~0.23s at a 0.05s cadence.
        assert len(_MINT_CALLS) >= mint_calls_before_loop + 2
        refreshed_token = read_t1_session_lease(session_id, config_dir)
        assert refreshed_token is not None
        assert refreshed_token != first_token, (
            "the lease must carry a NEWLY minted token after refresh ticks, "
            "not the original pre-loop token"
        )
        import os as _os_mod
        assert _os_mod.environ.get("NX_T1_SESSION") == refreshed_token, (
            "the refresh loop must update the process's own NX_T1_SESSION "
            "env var to the freshly minted token"
        )

    def test_branch0_wires_refresh_task_and_it_advances_the_token(
        self, fake_service, config_dir, monkeypatch
    ) -> None:
        """End-to-end: a live Branch-0 session with a short mint TTL sees its
        OWN env token change to a newer mint within the lifespan's lifetime
        -- proving the task started by `_t1_lifespan` is actually
        live and ticking, not just constructed."""
        import asyncio
        import os as _os_mod

        from nexus.mcp import core as mcp_core

        global _MINT_TTL_SECONDS
        # A tiny (but non-zero -- `_mint_ttl or DEFAULT` in production code
        # would otherwise substitute the 24h default for a falsy 0) TTL, so
        # `ttl * fraction` is negligible and the floor below sets the cadence.
        _MINT_TTL_SECONDS = 0.001
        # Shrink the floor so the test observes several ticks without a real
        # multi-second sleep -- the floor, not the (here-negligible) TTL
        # fraction, drives the cadence in this test.
        monkeypatch.setattr(mcp_core, "_T1_SESSION_REFRESH_MIN_INTERVAL_S", 0.05)

        live_session_id = "mcp-branch0-refresh-e2e-test"
        monkeypatch.setattr(
            "nexus.session.resolve_active_session_id", lambda: live_session_id
        )

        seen: dict[str, Any] = {}

        async def _run() -> None:
            async with mcp_core._t1_lifespan(None):
                assert mcp_core._T1_SESSION_REFRESH_TASK is not None
                initial_token = _os_mod.environ.get("NX_T1_SESSION")
                mint_calls_before = len(_MINT_CALLS)
                await asyncio.sleep(0.3)
                seen["initial_token"] = initial_token
                seen["later_token"] = _os_mod.environ.get("NX_T1_SESSION")
                seen["mint_calls_after"] = len(_MINT_CALLS)
                seen["mint_calls_before"] = mint_calls_before

        asyncio.run(_run())

        assert seen["mint_calls_after"] > seen["mint_calls_before"], (
            "the refresh task must have re-minted at least once"
        )
        assert seen["later_token"] != seen["initial_token"], (
            "the live session's own NX_T1_SESSION must advance to the newly "
            "refreshed token"
        )


# ── Ownership recovery on a stale lease (nexus-ngcpo Finding 3) ────────────────
#
# The subtlest of the three findings: when the ORIGINAL minting owner has
# died (its lease is stale because nobody has refreshed it since), a fresh
# Branch-0 process resolving the SAME session id must not borrow the dead
# lease -- it must mint its own fresh token, republish the lease, and take
# ownership (participate in refresh + teardown) going forward. A FRESH lease,
# by contrast, is still just borrowed (TestMcpCoreLeaseWiring's
# `test_resolved_session_with_published_lease_does_not_remint` locks that
# unchanged behavior) since the original owner is presumed alive and
# refreshing it itself.


class TestBranch0StaleLeaseRecovery:
    @pytest.fixture(autouse=True)
    def _reset_branch0_state(self):
        from nexus.mcp import core as mcp_core

        prev_inflight = mcp_core._SHUTDOWN_IN_FLIGHT
        prev_owned_session = dict(mcp_core._OWNED_T1_SESSION)
        mcp_core._SHUTDOWN_IN_FLIGHT = False
        mcp_core._OWNED_T1_SESSION.clear()
        try:
            yield
        finally:
            mcp_core._SHUTDOWN_IN_FLIGHT = prev_inflight
            mcp_core._OWNED_T1_SESSION.clear()
            mcp_core._OWNED_T1_SESSION.update(prev_owned_session)
            if mcp_core._T1_SESSION_REFRESH_TASK is not None:
                mcp_core._T1_SESSION_REFRESH_TASK.cancel()
                mcp_core._T1_SESSION_REFRESH_TASK = None

    def test_branch0_mints_fresh_and_takes_ownership_when_lease_stale(
        self, fake_service, config_dir, monkeypatch
    ) -> None:
        import asyncio
        import os as _os_mod

        from nexus.db.t1 import publish_t1_session_lease, read_t1_session_lease
        from nexus.mcp import core as mcp_core

        live_session_id = "mcp-branch0-stale-recovery-test"
        monkeypatch.setattr(
            "nexus.session.resolve_active_session_id", lambda: live_session_id
        )

        # Simulate an abandoned lease from a since-dead prior owner: published
        # but already past its expiry (it stopped refreshing when it died).
        publish_t1_session_lease(
            live_session_id, "dead-owners-old-token", config_dir, ttl_seconds=-1.0
        )
        mint_calls_before = len(_MINT_CALLS)

        seen: dict[str, Any] = {}

        async def _run() -> None:
            async with mcp_core._t1_lifespan(None):
                seen["owned_session_id"] = mcp_core._OWNED_T1_SESSION.get("session_id")
                seen["refresh_task"] = mcp_core._T1_SESSION_REFRESH_TASK
                seen["env_token"] = _os_mod.environ.get("NX_T1_SESSION")
                seen["lease_token"] = read_t1_session_lease(live_session_id, config_dir)

        asyncio.run(_run())

        # Must have minted its OWN fresh token -- exactly once, not borrowed.
        assert len(_MINT_CALLS) == mint_calls_before + 1
        assert seen["env_token"] != "dead-owners-old-token"
        assert seen["owned_session_id"] == live_session_id, (
            "a process that mints fresh after finding a stale lease must "
            "take ownership of the session (participate in refresh + "
            "teardown), unlike the fresh-lease borrow path which does not"
        )
        assert seen["refresh_task"] is not None, (
            "the recovering process must start its own refresh task -- it "
            "is now the owner keeping this session id alive"
        )
        # The republished lease reflects the NEW owner's fresh token.
        assert seen["lease_token"] == seen["env_token"]

        # Clean teardown: this new owner's exit clears the lease exactly
        # like any other Branch-0 owner (TestMcpCoreLeaseWiring's roundtrip).
        assert read_t1_session_lease(live_session_id, config_dir) is None


class TestResolveT1RoutingTiers:
    """Unit tests for the shared tier-1/tier-2 decision function
    (nexus-1si7z). Both get_t1_database() and mcp.core's Branch 0 now call
    THIS function for "is there an inherited token, or a fresh leased one" --
    these tests exercise the shared function directly, independent of
    either caller, so a future change to the decision logic gets a single,
    fast, unit-level regression signal before either caller's own
    (much heavier, fake-HTTP-server-backed) test suite would catch it."""

    def test_inherited_nx_t1_session_wins_over_everything(
        self, tmp_path, monkeypatch
    ) -> None:
        from nexus.db.t1 import T1RoutingAction, resolve_t1_routing_tiers

        monkeypatch.setenv("NX_T1_SESSION", "some-live-token")
        monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)

        decision = resolve_t1_routing_tiers(tmp_path)
        assert decision.action == T1RoutingAction.USE_INHERITED
        assert decision.session_id is None
        assert decision.session_token is None

    def test_inherited_nx_t1_session_id_alone_does_not_win(
        self, tmp_path, monkeypatch
    ) -> None:
        """NX_T1_SESSION_ID alone (no NX_T1_SESSION token) must NOT
        short-circuit to USE_INHERITED -- there is no live token to use.
        Stacked review of the nexus-1si7z extraction (code-review-expert +
        substantive-critic, independently) caught that an earlier draft's
        `bool(NX_T1_SESSION) or bool(NX_T1_SESSION_ID)` check would yield
        USE_INHERITED here despite no token existing; the fix narrowed the
        check to the token alone (see resolve_t1_routing_tiers's own
        comment), so this id-alone case now falls through to tier 2/3
        instead. With no NX_SESSION_ID/CLAUDE_CODE_SESSION_ID/flat-file
        session resolvable either, tier 2 (lease lookup) also misses, so
        this lands on MINT with session_id=None -- exactly
        test_unresolvable_session_id_mints_with_none_session_id's case,
        confirming NX_T1_SESSION_ID is not itself part of the resolution
        chain resolve_active_session_id() walks."""
        from nexus.db.t1 import T1RoutingAction, resolve_t1_routing_tiers

        monkeypatch.delenv("NX_T1_SESSION", raising=False)
        monkeypatch.setenv("NX_T1_SESSION_ID", "some-live-session-id")
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        decision = resolve_t1_routing_tiers(tmp_path)
        assert decision.action == T1RoutingAction.MINT
        assert decision.session_id is None
        assert decision.session_token is None

    def test_fresh_lease_for_resolvable_id_is_used_over_minting(
        self, tmp_path, monkeypatch
    ) -> None:
        from nexus.db.t1 import (
            T1RoutingAction,
            publish_t1_session_lease,
            resolve_t1_routing_tiers,
        )

        monkeypatch.delenv("NX_T1_SESSION", raising=False)
        monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)
        monkeypatch.setenv("NX_SESSION_ID", "resolvable-session")
        publish_t1_session_lease("resolvable-session", "leased-secret", tmp_path)

        decision = resolve_t1_routing_tiers(tmp_path)
        assert decision.action == T1RoutingAction.USE_LEASED
        assert decision.session_id == "resolvable-session"
        assert decision.session_token == "leased-secret"

    def test_no_inherited_no_lease_resolvable_id_mints_with_that_id(
        self, tmp_path, monkeypatch
    ) -> None:
        from nexus.db.t1 import T1RoutingAction, resolve_t1_routing_tiers

        monkeypatch.delenv("NX_T1_SESSION", raising=False)
        monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)
        monkeypatch.setenv("NX_SESSION_ID", "resolvable-no-lease")
        # No lease published for this id.

        decision = resolve_t1_routing_tiers(tmp_path)
        assert decision.action == T1RoutingAction.MINT
        assert decision.session_id == "resolvable-no-lease"
        assert decision.session_token is None

    def test_stale_lease_falls_through_to_mint_not_use_leased(
        self, tmp_path, monkeypatch
    ) -> None:
        """nexus-ngcpo: a stale (past-expiry) lease must be treated as
        absent, not borrowed. Publish one with a negative TTL so it is
        already expired at read time."""
        from nexus.db.t1 import (
            T1RoutingAction,
            publish_t1_session_lease,
            resolve_t1_routing_tiers,
        )

        monkeypatch.delenv("NX_T1_SESSION", raising=False)
        monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)
        monkeypatch.setenv("NX_SESSION_ID", "resolvable-stale-lease")
        publish_t1_session_lease(
            "resolvable-stale-lease", "stale-secret", tmp_path, ttl_seconds=-1.0
        )

        decision = resolve_t1_routing_tiers(tmp_path)
        assert decision.action == T1RoutingAction.MINT
        assert decision.session_id == "resolvable-stale-lease"
        assert decision.session_token is None

    def test_unresolvable_session_id_mints_with_none_session_id(
        self, tmp_path, monkeypatch
    ) -> None:
        """No resolvable session id at all (no env, no flat file) -- MINT
        action fires with session_id=None. Callers decide independently
        what that means (Branch 0 forces isolation; get_t1_database()
        falls to the CLI-dedicated identity regardless)."""
        from nexus.db.t1 import T1RoutingAction, resolve_t1_routing_tiers

        monkeypatch.delenv("NX_T1_SESSION", raising=False)
        monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        # No current_session flat file written either (fresh tmp config dir).

        decision = resolve_t1_routing_tiers(tmp_path)
        assert decision.action == T1RoutingAction.MINT
        assert decision.session_id is None
        assert decision.session_token is None

    def test_both_callers_now_route_through_the_shared_function(self) -> None:
        """Structural regression guard (the bead's own 'at minimum' fallback
        recommendation, satisfied here as a belt-and-suspenders on top of
        the full extraction above): if a future edit reverts either caller
        to a hand-rolled tier-1/tier-2 check instead of calling
        resolve_t1_routing_tiers, this test catches it via source
        inspection rather than relying on behavioral tests alone to notice.

        Strengthened per code-review-expert + substantive-critic (nexus-1si7z
        stacked review, round 2): a bare substring check on the function name
        is satisfiable by a stray comment or docstring mention with no real
        call underneath, and would not catch a caller that calls the function
        but then ignores its `.action` (e.g. always falling through to MINT
        regardless of what was decided). This version requires (a) an actual
        assignment-form call (`= resolve_t1_routing_tiers(`), not just the
        name appearing anywhere, and (b) an explicit dispatch condition for
        both USE_INHERITED and USE_LEASED (each caller handles them via an
        early-return `if decision.action == T1RoutingAction.<MEMBER>:`).

        MINT is the implicit fallthrough after both, by construction the
        only remaining action -- there is no third explicit comparison to
        check. round 1 of this test checked for the substring "MINT"
        anywhere in the function source, but BOTH reviewers independently
        proved empirically (via inspect.getsource + grep against the live
        code) that every "MINT" occurrence in both functions is inside a
        `#` comment, so that check was satisfiable by pre-existing prose
        alone and would NOT catch the exact silent-fallthrough-breakage
        this test exists to guard against. Fixed here by scoping to the
        source AFTER the last USE_LEASED dispatch block and requiring an
        actual call to `mint_t1_session_token(` there -- a real behavioral
        signal (the fallthrough actually mints) rather than a comment."""
        import inspect

        from nexus.db import t1 as t1_module
        from nexus.mcp import core as mcp_core_module

        explicit_actions = ("USE_INHERITED", "USE_LEASED")

        get_t1_database_src = inspect.getsource(t1_module.get_t1_database)
        assert "= resolve_t1_routing_tiers(" in get_t1_database_src, (
            "get_t1_database() must actually CALL the shared "
            "resolve_t1_routing_tiers (not just mention its name) -- a "
            "hand-rolled tier-1/tier-2 check here can silently diverge "
            "from mcp.core's Branch 0 again (nexus-1si7z)"
        )
        for action in explicit_actions:
            assert f"T1RoutingAction.{action}" in get_t1_database_src, (
                f"get_t1_database() must dispatch on T1RoutingAction.{action} "
                "-- computing the decision but not consuming one of its "
                "branches is the same silent-divergence risk this test guards "
                "against (nexus-1si7z)"
            )
        get_t1_database_post_dispatch = get_t1_database_src.rsplit(
            "T1RoutingAction.USE_LEASED", 1
        )[-1]
        # nexus-jc33g: the fallthrough now routes through the shared
        # flock-guarded mint-or-borrow primitive (which itself calls
        # mint_t1_session_token on the mint path) instead of minting
        # unconditionally — either call form is real behavioral evidence
        # that the fallthrough can still mint.
        assert (
            "mint_t1_session_token(" in get_t1_database_post_dispatch
            or "_lock_guarded_mint_or_borrow(" in get_t1_database_post_dispatch
        ), (
            "get_t1_database()'s fallthrough-to-mint branch must actually "
            "mint (mint_t1_session_token or the _lock_guarded_mint_or_borrow "
            "primitive) after the USE_LEASED dispatch -- a comment mentioning "
            "MINT is not sufficient evidence the fallthrough still mints "
            "(nexus-1si7z / nexus-jc33g)"
        )

        lifespan_src = inspect.getsource(mcp_core_module._t1_lifespan)
        assert "= resolve_t1_routing_tiers(" in lifespan_src, (
            "_t1_lifespan's Branch 0 must actually CALL the shared "
            "resolve_t1_routing_tiers (not just mention its name) -- a "
            "hand-rolled tier-1/tier-2 check here can silently diverge "
            "from get_t1_database() again (nexus-1si7z)"
        )
        for action in explicit_actions:
            assert f"T1RoutingAction.{action}" in lifespan_src, (
                f"_t1_lifespan's Branch 0 must dispatch on "
                f"T1RoutingAction.{action} -- computing the decision but not "
                "consuming one of its branches is the same silent-divergence "
                "risk this test guards against (nexus-1si7z)"
            )
        lifespan_post_dispatch = lifespan_src.rsplit("T1RoutingAction.USE_LEASED", 1)[-1]
        # nexus-jwqjm: Branch 0's fallthrough-to-mint arm no longer calls
        # mint_t1_session_token(...) directly -- it routes through
        # _lock_guarded_mint_or_borrow(...) (src/nexus/db/t1.py), which
        # flock-serializes the mint-or-borrow critical section so concurrent
        # stale-lease recoverers for the SAME session_id converge on one real
        # mint instead of each rotating the other's token. The mint call
        # itself now lives inside that helper (verified by
        # tests/db/test_t1_mint_race.py), so the real behavioral signal this
        # test checks for at THIS call site is that the fallthrough branch
        # actually invokes the mint-or-borrow helper, not a comment mention.
        assert "_lock_guarded_mint_or_borrow(" in lifespan_post_dispatch, (
            "_t1_lifespan's Branch 0 fallthrough-to-mint branch must "
            "actually CALL _lock_guarded_mint_or_borrow(...) after the "
            "USE_LEASED dispatch -- a comment mentioning MINT is not "
            "sufficient evidence the fallthrough still mints (nexus-1si7z, "
            "superseded call path per nexus-jwqjm)"
        )


class TestDeferredMintBoundaries:
    """nexus-brw1s review round: the guarantees asserted ONE FRAME UP from
    where the first three pins stopped (critic SIG-2 — 'the tests assert the
    precondition, not the guarantee'), plus the shutdown sentinel (reviewer
    LOW-1) and the real-loop refresh scheduling (reviewer LOW-3)."""

    def _arm_deferred(self, mcp_core, mcp_infra, config_dir, monkeypatch, loop=None):
        monkeypatch.setattr(mcp_infra, "_t1_instance", None)
        monkeypatch.setattr(mcp_infra, "_t1_isolated", False)
        mcp_core._DEFERRED_T1_MINT.clear()
        mcp_core._DEFERRED_T1_MINT.update(
            session_id="deferred-boundary-test", config_dir=config_dir, loop=loop
        )
        mcp_infra.set_t1_pre_init_hook(mcp_core._retry_deferred_t1_mint)

    def _disarm(self, mcp_core, mcp_infra):
        import os

        mcp_core._DEFERRED_T1_MINT.clear()
        mcp_infra.set_t1_pre_init_hook(None)
        mcp_infra._t1_instance = None
        mcp_core._OWNED_T1_SESSION.pop("session_id", None)
        os.environ.pop("NX_T1_SESSION", None)
        os.environ.pop("NX_T1_SESSION_ID", None)

    def test_scratch_tool_returns_an_error_string_not_a_raise(
        self, fake_service, config_dir, monkeypatch
    ) -> None:
        """THE user-facing guarantee (critic SIG-2 part 1): in the deferred
        window with the service still down, the REAL scratch tool returns an
        actionable error STRING — it does not raise out of the worker. The
        earlier pins asserted get_t1() raises; this asserts the boundary
        catches it. Remove scratch's try/except and this goes red while every
        other pin stays green — which is exactly why it exists."""
        from nexus import mcp_infra
        from nexus.mcp import core as mcp_core
        from nexus.mcp.core import scratch

        self._arm_deferred(mcp_core, mcp_infra, config_dir, monkeypatch)

        def _still_down(_sid, _cfg):
            raise RuntimeError("connect refused")

        monkeypatch.setattr("nexus.db.t1._lock_guarded_mint_or_borrow", _still_down)
        try:
            result = scratch(action="put", content="deferred-window write")
            assert isinstance(result, str)
            assert "nx daemon service start" in result, result
            assert mcp_infra._t1_instance is None, "a failed call must cache nothing"
        finally:
            self._disarm(mcp_core, mcp_infra)

    def test_deferral_logs_warning_and_never_the_fatal_error_event(
        self, fake_service, config_dir, monkeypatch
    ) -> None:
        """The severity transition IS the fix (critic SIG-2 part 2): the old
        path emitted error-level t1_session_mint_failed and died; the new path
        emits warning-level t1_session_mint_deferred and serves. Pin both
        directions — the WARNING fires, the ERROR never does."""
        import asyncio

        from structlog.testing import capture_logs

        from nexus.mcp import core as mcp_core

        monkeypatch.setattr(
            "nexus.session.resolve_active_session_id",
            lambda: "mcp-deferral-severity-test",
        )
        monkeypatch.delenv("NX_T1_SESSION", raising=False)
        monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)

        global _MINT_FAILS
        _MINT_FAILS = True

        async def _run() -> None:
            async with mcp_core._t1_lifespan(None):
                pass

        try:
            with capture_logs() as cap:
                asyncio.run(_run())
        finally:
            _MINT_FAILS = False

        deferred = [e for e in cap if e.get("event") == "t1_session_mint_deferred"]
        assert deferred, f"the deferral warning must fire: {cap}"
        assert all(e.get("log_level") == "warning" for e in deferred)
        assert not [e for e in cap if e.get("event") == "t1_session_mint_failed"], (
            "the fatal-path error event must be gone"
        )

    def test_deferred_refresh_task_is_scheduled_on_the_real_loop(
        self, fake_service, config_dir, monkeypatch
    ) -> None:
        """Reviewer LOW-3: the loop=None arrival test never exercised the real
        scheduling path. Here the hook runs in a genuine worker thread while
        the lifespan's loop is alive, and the refresh task must actually be
        created on it."""
        import asyncio

        from nexus import mcp_infra
        from nexus.mcp import core as mcp_core

        monkeypatch.setattr(
            "nexus.db.t1._lock_guarded_mint_or_borrow",
            lambda _sid, _cfg: ("tok-real-loop", True, 3600.0),
        )
        monkeypatch.setattr("nexus.db.t1.get_t1_database", lambda: object())

        async def _run() -> None:
            self._arm_deferred(
                mcp_core, mcp_infra, config_dir, monkeypatch,
                loop=asyncio.get_running_loop(),
            )
            await asyncio.to_thread(mcp_infra.get_t1)  # a real worker thread
            await asyncio.sleep(0.05)  # let call_soon_threadsafe land
            assert mcp_core._T1_SESSION_REFRESH_TASK is not None, (
                "the refresh task was never scheduled on the live loop"
            )
            await mcp_core._cancel_t1_session_refresh_task()

        try:
            asyncio.run(_run())
        finally:
            self._disarm(mcp_core, mcp_infra)

    def test_mint_completing_during_shutdown_is_discarded(
        self, fake_service, config_dir, monkeypatch
    ) -> None:
        """Reviewer LOW-1, the shutdown sentinel: a mint that completes AFTER
        the lifespan finally cleared the deferred state must commit NOTHING —
        no ownership (it would land after the revoke: the nexus-5daww leak
        class), no env (an env-less construction would route into the shared
        CLI-dedicated identity), no cached instance. It raises instead."""
        import os

        import pytest as _pytest

        from nexus import mcp_infra
        from nexus.mcp import core as mcp_core

        self._arm_deferred(mcp_core, mcp_infra, config_dir, monkeypatch)

        def _mint_then_shutdown(_sid, _cfg):
            # The lifespan finally runs while this mint is in flight.
            mcp_core._DEFERRED_T1_MINT.clear()
            return "tok-too-late", True, 3600.0

        monkeypatch.setattr(
            "nexus.db.t1._lock_guarded_mint_or_borrow", _mint_then_shutdown
        )
        try:
            with _pytest.raises(RuntimeError, match="session is ending"):
                mcp_infra.get_t1()
            assert "session_id" not in mcp_core._OWNED_T1_SESSION
            assert "NX_T1_SESSION" not in os.environ
            assert mcp_infra._t1_instance is None
        finally:
            self._disarm(mcp_core, mcp_infra)


# ── nexus-jc33g: CLI-dedicated TOKEN is cached; no per-invocation re-mint ─────


class TestDedicatedTokenCache:
    def test_second_invocation_does_not_remint(self, fake_service, config_dir) -> None:
        """The dedicated-id file gave id continuity but every fresh bare-CLI
        process still paid a full mint round trip. The token now rides the
        same published-lease mechanism (nexus-ngcpo freshness rules): a
        second invocation borrows the cached fresh token instead of minting."""
        from nexus.db.t1 import get_t1_database

        store1 = get_t1_database()
        assert len(_MINT_CALLS) == 1

        store2 = get_t1_database()
        assert len(_MINT_CALLS) == 1, "fresh cached token must be borrowed, not re-minted"

        doc_id = store2.put("no remint", tags="jc33g")
        entry = store2.get(doc_id)
        assert entry is not None and entry["content"] == "no remint"

    def test_expired_cached_token_remints(self, fake_service, config_dir) -> None:
        """A cached token past its lease expiry is ABSENT (ngcpo fail-safe
        rules) — the next invocation mints fresh rather than borrowing blind."""
        import time as _time

        global _MINT_TTL_SECONDS
        _MINT_TTL_SECONDS = 1
        from nexus.db.t1 import get_t1_database

        get_t1_database()
        assert len(_MINT_CALLS) == 1
        _time.sleep(1.2)
        get_t1_database()
        assert len(_MINT_CALLS) == 2, "expired cached token must trigger a fresh mint"

    def test_selfheal_republishes_for_future_invocations(
        self, fake_service, config_dir
    ) -> None:
        """After a 401 self-heal re-mint, the fresh token is republished so
        the NEXT bare-CLI invocation borrows it instead of minting again."""
        import httpx

        from nexus.db.t1 import get_t1_database

        store1 = get_t1_database()
        sid = store1.session_id
        # A racing sibling rotates the token server-side (ON CONFLICT DO
        # UPDATE) — store1's in-hand token and the published lease are now
        # both stale.
        httpx.post(
            f"{fake_service}/v1/sessions/start",
            json={"session_id": sid},
            headers={
                "Authorization": f"Bearer {SERVICE_TOKEN}",
                "X-Nexus-Tenant": "default",
                "Content-Type": "application/json",
            },
        )
        mints_before = len(_MINT_CALLS)
        doc_id = store1.put("healed", tags="jc33g")
        assert doc_id
        assert len(_MINT_CALLS) == mints_before + 1  # exactly the self-heal mint

        store2 = get_t1_database()
        assert len(_MINT_CALLS) == mints_before + 1, (
            "self-heal must republish the fresh token; the next invocation borrows it"
        )
        entry = store2.get(doc_id)
        assert entry is not None and entry["content"] == "healed"


# ── nexus-by875: total wall-clock budget on the CLI op stack ─────────────────


class TestCliOpBudget:
    def test_exhausted_budget_skips_selfheal_retry_leg(
        self, fake_service, config_dir, monkeypatch
    ) -> None:
        """The mint->op->401->re-mint->retry stack must never compound past
        the budget: with the budget exhausted, a 401 propagates with the
        remedy message instead of starting another mint+retry round trip."""
        from nexus.db.t1 import get_t1_database

        store = get_t1_database()
        _ALWAYS_401.add(store.session_id)
        monkeypatch.setenv("NX_T1_CLI_BUDGET_S", "0")

        mints_before = len(_MINT_CALLS)
        with pytest.raises(RuntimeError, match="budget"):
            store.put("never lands", tags="by875")
        assert len(_MINT_CALLS) == mints_before, (
            "no re-mint leg may start once the budget is exhausted"
        )

    def test_selfheal_still_runs_within_default_budget(
        self, fake_service, config_dir
    ) -> None:
        """Control: with the default budget the pre-existing self-heal
        semantics are unchanged (TestSelfHeal's contract still holds)."""
        import httpx

        from nexus.db.t1 import get_t1_database

        store = get_t1_database()
        httpx.post(
            f"{fake_service}/v1/sessions/start",
            json={"session_id": store.session_id},
            headers={
                "Authorization": f"Bearer {SERVICE_TOKEN}",
                "X-Nexus-Tenant": "default",
                "Content-Type": "application/json",
            },
        )
        doc_id = store.put("within budget", tags="by875")
        entry = store.get(doc_id)
        assert entry is not None and entry["content"] == "within budget"


class TestCliOpBudgetEnforcement:
    """Reviewer H1 + critic Critical folds (nexus-by875): the budget must
    ACTUALLY bind — an intermediate value distinguishes enforcing code from
    non-enforcing (budget=0 is trivially exceeded; 60s never binds)."""

    def _store_with(self, first_op, mint):
        """A wrapper around fakes: first_op raises/returns per call; mint is
        monkeypatched at module level by the caller."""
        from nexus.db.t1 import _CliDedicatedScratchStore

        class _FakeStore:
            session_id = "dedicated-x"

            def __init__(self) -> None:
                self.calls = 0

            def put(self, *a, **k):
                self.calls += 1
                return first_op(self.calls)

        return _CliDedicatedScratchStore("dedicated-x", _FakeStore())

    def test_intermediate_budget_blocks_retry_after_slow_first_leg(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """First leg consumes more than the (small, non-zero) budget then
        401s: the re-mint leg must never start."""
        import time as _time

        from nexus.db import t1 as t1_mod
        from nexus.db.http_scratch_store import SESSION_UNAUTHORIZED_MARKER

        monkeypatch.setenv("NX_T1_CLI_BUDGET_S", "0.05")
        minted: list[str] = []
        monkeypatch.setattr(
            t1_mod, "mint_t1_session_token",
            lambda sid, context: minted.append(sid) or {"session_token": "t"},
        )

        def _slow_401(_calls: int):
            _time.sleep(0.1)
            raise RuntimeError(f"boom {SESSION_UNAUTHORIZED_MARKER}")

        store = self._store_with(_slow_401, None)
        with pytest.raises(RuntimeError, match="budget"):
            store.put("x")
        assert minted == [], "re-mint leg must not start past the budget"

    def test_post_remint_exhaustion_blocks_retry_leg(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fast first 401 within budget, but the re-mint itself consumes the
        remainder: the retry leg must not start (the reviewer's exact gap —
        pre-fold code retried unconditionally after remint)."""
        import time as _time

        from nexus.db import t1 as t1_mod
        from nexus.db.http_scratch_store import SESSION_UNAUTHORIZED_MARKER

        monkeypatch.setenv("NX_T1_CLI_BUDGET_S", "0.05")

        def _slow_mint(sid, context):
            _time.sleep(0.1)
            return {"session_token": "fresh"}

        monkeypatch.setattr(t1_mod, "mint_t1_session_token", _slow_mint)
        # _remint constructs a fresh HttpScratchStore (endpoint resolution
        # this unit test deliberately lacks) — stub the class; if the gate
        # works the retry never touches the stub anyway.
        import types as _types

        monkeypatch.setattr(
            "nexus.db.http_scratch_store.HttpScratchStore",
            lambda session_id, _session_token: _types.SimpleNamespace(
                session_id=session_id,
            ),
        )

        def _fast_401(calls: int):
            raise RuntimeError(f"boom {SESSION_UNAUTHORIZED_MARKER}")

        store = self._store_with(_fast_401, None)
        first_leg_store = store._store  # _remint swaps _store for the stub
        with pytest.raises(RuntimeError, match="after the self-heal re-mint"):
            store.put("x")
        # first leg ran once; the retry (which would hit the post-remint stub
        # and AttributeError on .put) never started.
        assert first_leg_store.calls == 1

    def test_construction_deadline_bounds_lock_wait(
        self, fake_service, config_dir, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Critic Critical: a sibling holding the mint lock must not wedge a
        budgeted caller — the bounded-poll acquire raises the remedy at the
        deadline instead of blocking forever."""
        import fcntl as _fcntl

        from nexus.db.t1 import _lock_guarded_mint_or_borrow, _t1_session_mint_lock_path

        lock_path = _t1_session_mint_lock_path("held-id", config_dir)
        config_dir.mkdir(parents=True, exist_ok=True)
        holder_fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        _fcntl.flock(holder_fd, _fcntl.LOCK_EX)
        try:
            import time as _time

            with pytest.raises(RuntimeError, match="mint lock"):
                _lock_guarded_mint_or_borrow(
                    "held-id", config_dir, context="CLI-dedicated session mint",
                    deadline=_time.monotonic() + 0.2,
                )
        finally:
            _fcntl.flock(holder_fd, _fcntl.LOCK_UN)
            os.close(holder_fd)

    def test_budget_env_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.db.t1 import _t1_cli_op_budget_seconds

        monkeypatch.delenv("NX_T1_CLI_BUDGET_S", raising=False)
        assert _t1_cli_op_budget_seconds() == 60.0
        monkeypatch.setenv("NX_T1_CLI_BUDGET_S", "12.5")
        assert _t1_cli_op_budget_seconds() == 12.5
        monkeypatch.setenv("NX_T1_CLI_BUDGET_S", "abc")
        assert _t1_cli_op_budget_seconds() == 60.0  # malformed -> default
        monkeypatch.setenv("NX_T1_CLI_BUDGET_S", "-3")
        assert _t1_cli_op_budget_seconds() == -3.0  # negative = strictest, never unlimited


class TestLeaseFreshnessMargin:
    def test_near_expiry_lease_reads_absent(self, tmp_path: Path) -> None:
        """jc33g critic fold: a lease inside the freshness margin is not
        borrowed — the borrower would 401 on first use."""
        from nexus.db.t1 import publish_t1_session_lease, read_t1_session_lease

        publish_t1_session_lease("margin-id", "tok", tmp_path, ttl_seconds=3.0)
        assert read_t1_session_lease("margin-id", tmp_path) is None  # < 5s margin

        publish_t1_session_lease("margin-id2", "tok2", tmp_path, ttl_seconds=60.0)
        assert read_t1_session_lease("margin-id2", tmp_path) == "tok2"
