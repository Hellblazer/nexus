# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for RDR-079 P2.1 (commit A) — pool errors, spawn guard, auth guard.

Covers the contract surface of OperatorPool that can be validated without
a live claude worker:
  * PoolConfigError when NEXUS_T1_SESSION_ID is unset (SC-15, invariant I-4).
  * PoolAuthUnavailableError when `claude auth status` reports not logged in
    (SC-10, graceful degradation).
  * Worker spawn command-line shape matches RDR-079 §Worker pool.

Subsequent commits (B: dispatch + streaming parser; C: retirement + health)
build on this skeleton with in-process subprocess stubs.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest


# ── Error types ────────────────────────────────────────────────────────────


def test_pool_config_error_importable() -> None:
    from nexus.operators.pool import PoolConfigError

    err = PoolConfigError("NEXUS_T1_SESSION_ID is required")
    assert isinstance(err, Exception)
    assert "NEXUS_T1_SESSION_ID" in str(err)


def test_pool_auth_unavailable_error_importable() -> None:
    from nexus.operators.pool import PoolAuthUnavailableError

    err = PoolAuthUnavailableError("run `claude auth login`")
    assert isinstance(err, Exception)


def test_pool_spawn_error_importable() -> None:
    from nexus.operators.pool import PoolSpawnError

    err = PoolSpawnError("claude not on PATH")
    assert isinstance(err, Exception)


# ── Spawn guard (SC-15, invariant I-4) ─────────────────────────────────────


def test_spawn_worker_raises_pool_config_error_without_session_env(
    monkeypatch,
) -> None:
    """SC-15: OperatorPool.spawn_worker() MUST raise PoolConfigError when
    NEXUS_T1_SESSION_ID is missing from env — the env var is the single
    load-bearing mechanism for worker T1 isolation. A silent spawn
    without it would let the worker fall back to PPID-walk and land
    on the user's T1 session.
    """
    from nexus.operators.pool import OperatorPool, PoolConfigError

    monkeypatch.delenv("NEXUS_T1_SESSION_ID", raising=False)
    pool = OperatorPool()
    with pytest.raises(PoolConfigError, match="NEXUS_T1_SESSION_ID"):
        asyncio.get_event_loop().run_until_complete(pool.spawn_worker())


def test_spawn_worker_raises_pool_config_error_on_empty_env(monkeypatch) -> None:
    """Empty-string env is equivalent to unset (defensive guard)."""
    from nexus.operators.pool import OperatorPool, PoolConfigError

    monkeypatch.setenv("NEXUS_T1_SESSION_ID", "")
    pool = OperatorPool()
    with pytest.raises(PoolConfigError):
        asyncio.get_event_loop().run_until_complete(pool.spawn_worker())


# ── Auth guard (SC-10) ─────────────────────────────────────────────────────


def test_check_auth_raises_when_logged_out(monkeypatch) -> None:
    """SC-10: graceful degradation — if `claude auth status --json` reports
    loggedIn=false, the pool raises PoolAuthUnavailableError with clear
    guidance so the operator MCP tools can surface it to callers."""
    from nexus.operators import pool as pool_mod

    def fake_run(cmd, **kw):
        class FakeResult:
            returncode = 0
            stdout = json.dumps({"loggedIn": False})
            stderr = ""
        return FakeResult()

    monkeypatch.setattr(pool_mod.subprocess, "run", fake_run)

    with pytest.raises(pool_mod.PoolAuthUnavailableError):
        pool_mod.check_auth()


def test_check_auth_raises_when_json_malformed(monkeypatch) -> None:
    """Defensive parse: if claude auth returns unparseable JSON (future CLI
    schema drift), the pool refuses to start rather than silently proceed."""
    from nexus.operators import pool as pool_mod

    def fake_run(cmd, **kw):
        class FakeResult:
            returncode = 0
            stdout = "not json"
            stderr = ""
        return FakeResult()

    monkeypatch.setattr(pool_mod.subprocess, "run", fake_run)

    with pytest.raises(pool_mod.PoolAuthUnavailableError):
        pool_mod.check_auth()


def test_check_auth_raises_when_logged_in_key_missing(monkeypatch) -> None:
    """Defensive parse: if the JSON parses but has no `loggedIn` key (schema
    drift — e.g. renamed to `isLoggedIn`), refuse to proceed. RDR-079 risk
    section mitigation."""
    from nexus.operators import pool as pool_mod

    def fake_run(cmd, **kw):
        class FakeResult:
            returncode = 0
            stdout = json.dumps({"isLoggedIn": True})  # wrong key
            stderr = ""
        return FakeResult()

    monkeypatch.setattr(pool_mod.subprocess, "run", fake_run)

    with pytest.raises(pool_mod.PoolAuthUnavailableError):
        pool_mod.check_auth()


def test_check_auth_passes_when_logged_in(monkeypatch) -> None:
    """Happy path: claude auth status reports loggedIn=true → no error."""
    from nexus.operators import pool as pool_mod

    def fake_run(cmd, **kw):
        class FakeResult:
            returncode = 0
            stdout = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
            stderr = ""
        return FakeResult()

    monkeypatch.setattr(pool_mod.subprocess, "run", fake_run)

    # Must return without raising
    pool_mod.check_auth()


def test_check_auth_raises_when_claude_not_on_path(monkeypatch) -> None:
    """If `claude` is not installed at all, `subprocess.run` raises
    FileNotFoundError. The pool surfaces this as PoolAuthUnavailableError
    with a message naming the problem."""
    from nexus.operators import pool as pool_mod

    def fake_run(cmd, **kw):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(pool_mod.subprocess, "run", fake_run)

    with pytest.raises(pool_mod.PoolAuthUnavailableError, match="claude"):
        pool_mod.check_auth()


# ── Worker command-line shape (RDR-079 §Worker pool) ───────────────────────


def test_build_worker_cmdline_contains_required_flags() -> None:
    """The spawn command-line must carry the streaming-RPC flags documented
    in RDR-079 §Worker pool (Empirical Finding 1 + 3). Also carries the
    per-worker session_id and the system prompt for the operator role.
    Testing the command-line shape without actually spawning claude."""
    from nexus.operators.pool import build_worker_cmdline

    cmd = build_worker_cmdline(
        session_id="worker-abc-123",
        operator_role="You are extract",
        max_budget_usd=0.50,
        max_turns=6,
        model="haiku",
    )
    # Core streaming RPC flags (Empirical Finding 1)
    assert "claude" in cmd[0] or cmd[0].endswith("claude")
    assert "-p" in cmd
    assert "--input-format" in cmd
    assert "stream-json" in cmd
    assert "--output-format" in cmd
    assert "--verbose" in cmd  # required by --output-format=stream-json
    # Per-worker isolation + identity
    assert "--session-id" in cmd
    assert "worker-abc-123" in cmd
    # Operator role preamble
    assert "--append-system-prompt" in cmd
    # Cost guards
    assert "--max-budget-usd" in cmd
    assert "--max-turns" in cmd
    # Model
    assert "--model" in cmd
    assert "haiku" in cmd


def test_build_worker_cmdline_omits_bare_flag() -> None:
    """Workers must NOT use --bare — per RDR-079 Empirical Finding 4,
    --bare disables OAuth inheritance and forces API-key auth. Keeping
    OAuth is the point of the whole design."""
    from nexus.operators.pool import build_worker_cmdline

    cmd = build_worker_cmdline(
        session_id="w1",
        operator_role="r",
        max_budget_usd=1.0,
        max_turns=6,
    )
    assert "--bare" not in cmd


def test_build_worker_cmdline_requests_mcp_worker_mode(monkeypatch) -> None:
    """Workers must spawn with NEXUS_MCP_WORKER_MODE=1 in their env so the
    nested nx-mcp server they talk to (if any) drops the dispatch-surface
    tools. Tested at the worker_env() level since cmdline does not
    include env."""
    from nexus.operators.pool import worker_env

    monkeypatch.setenv("NEXUS_T1_SESSION_ID", "pool-abc")
    env = worker_env(pool_session_id="pool-abc")
    assert env["NEXUS_T1_SESSION_ID"] == "pool-abc"
    assert env["NEXUS_MCP_WORKER_MODE"] == "1"


def test_worker_env_inherits_parent_env(monkeypatch) -> None:
    """Worker env must inherit the parent's env (PATH, HOME, ANTHROPIC_*,
    etc.) — we only override NEXUS-namespaced vars."""
    monkeypatch.setenv("PATH", "/custom/path")
    monkeypatch.setenv("HOME", "/custom/home")
    from nexus.operators.pool import worker_env

    env = worker_env(pool_session_id="pool-abc")
    assert env["PATH"] == "/custom/path"
    assert env["HOME"] == "/custom/home"


# ── Pool construction ─────────────────────────────────────────────────────


def test_operator_pool_constructs_with_defaults() -> None:
    """OperatorPool() constructs without touching the network, spawning
    subprocesses, or reading claude auth. Lazy initialization — side
    effects only on first spawn."""
    from nexus.operators.pool import OperatorPool

    pool = OperatorPool()
    assert pool.size == 2  # default from RDR-079
    assert pool.model == "haiku"
    assert pool.workers == []


def test_operator_pool_accepts_custom_size_and_model() -> None:
    from nexus.operators.pool import OperatorPool

    pool = OperatorPool(size=4, model="sonnet")
    assert pool.size == 4
    assert pool.model == "sonnet"
