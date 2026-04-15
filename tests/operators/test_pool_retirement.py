# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for RDR-079 P2.1 (commit C) — retirement, shutdown, singleton.

Covers:
  * ``retire_worker(worker)`` — drain in-flight, spawn replacement,
    kill retiree (SC-3).
  * ``dispatch_with_rotation(prompt)`` — picks a worker, retires if
    cumulative tokens exceed threshold, dispatches on the successor.
  * ``shutdown()`` — kill all workers + teardown pool session (SC-13
    graceful stop).
  * ``mcp_infra.get_operator_pool()`` singleton — lazy-init idempotent.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

STUB_PATH = Path(__file__).parent / "fixtures" / "claude_stub.py"

pytestmark = pytest.mark.asyncio


@pytest.fixture
def stub_pool(monkeypatch):
    """Pool with stub-claude workers, no auth, pre-set session env."""
    import nexus.operators.pool as pool_mod
    from nexus.operators.pool import OperatorPool

    monkeypatch.setenv("NEXUS_T1_SESSION_ID", "pool-test-xyz")
    monkeypatch.setattr(pool_mod, "check_auth", lambda: None)
    monkeypatch.setattr(
        pool_mod, "build_worker_cmdline",
        lambda **kw: [sys.executable, str(STUB_PATH)],
    )

    pool = OperatorPool(size=1, max_budget_usd=1.0, max_turns=6,
                        retirement_token_threshold=300)
    yield pool
    # Teardown — kill any live workers
    for w in list(pool.workers):
        if w.process.returncode is None:
            try:
                w.process.kill()
            except ProcessLookupError:
                pass


# ── retire_worker ──────────────────────────────────────────────────────────


async def test_retire_worker_kills_retiree_and_spawns_replacement(
    stub_pool,
) -> None:
    """SC-3: retire_worker kills the retiree's subprocess AND adds a
    replacement to the pool."""
    w1 = await stub_pool.spawn_worker()
    assert len(stub_pool.workers) == 1
    original_pid = w1.process.pid

    w2 = await stub_pool.retire_worker(w1)

    # Original killed
    await asyncio.sleep(0.05)
    assert w1.process.returncode is not None
    assert w1.alive is False
    # Replacement spawned, pool size maintained
    assert len(stub_pool.workers) == 1  # old removed, new added
    assert w2 is not w1
    assert w2.process.pid != original_pid
    assert w2.alive is True


async def test_retire_worker_drains_in_flight_before_kill(
    stub_pool,
) -> None:
    """SC-3: retirement must wait for in-flight dispatches to complete
    before killing the retiree. Simulate by marking in_flight, launching
    retire_worker, observing it waits."""
    w1 = await stub_pool.spawn_worker()
    w1.in_flight = 1  # pretend a dispatch is mid-flight

    retire_task = asyncio.create_task(stub_pool.retire_worker(w1))
    await asyncio.sleep(0.1)
    # Retirement should still be waiting because in_flight > 0
    assert not retire_task.done(), "retire_worker killed a busy worker"
    assert w1.process.returncode is None

    # Drain
    w1.in_flight = 0
    # Now retirement should complete within a reasonable window
    await asyncio.wait_for(retire_task, timeout=5.0)


# ── dispatch_with_rotation ────────────────────────────────────────────────


async def test_dispatch_with_rotation_retires_over_threshold(
    stub_pool, monkeypatch,
) -> None:
    """When a worker's cumulative tokens exceed retirement_token_threshold,
    dispatch_with_rotation transparently retires it and dispatches on a
    fresh worker. Zero-downtime handoff."""
    # Stub emits 250 tokens per turn; threshold is 300 (set in fixture).
    monkeypatch.setenv("STUB_INPUT_TOKENS_PER_TURN", "250")
    monkeypatch.setenv("STUB_OUTPUT_TOKENS_PER_TURN", "100")

    # Pre-spawn one worker so pool is ready
    await stub_pool.spawn_worker()
    initial_worker = stub_pool.workers[0]

    # First dispatch — worker accumulates 350 tokens; threshold=300 crossed.
    r1 = await stub_pool.dispatch_with_rotation(
        prompt="first", timeout=15.0,
    )
    assert isinstance(r1, dict)

    # Second dispatch — the original worker should have been retired.
    r2 = await stub_pool.dispatch_with_rotation(
        prompt="second", timeout=15.0,
    )
    assert isinstance(r2, dict)

    # The original worker must be dead (or at least not in the pool).
    assert initial_worker not in stub_pool.workers or not initial_worker.alive


async def test_dispatch_with_rotation_auto_spawns_first_worker(
    stub_pool,
) -> None:
    """If the pool has no workers yet, dispatch_with_rotation spawns one
    lazily and uses it."""
    assert stub_pool.workers == []

    r = await stub_pool.dispatch_with_rotation(prompt="bootstrap", timeout=15.0)
    assert isinstance(r, dict)
    assert len(stub_pool.workers) >= 1


# ── shutdown ──────────────────────────────────────────────────────────────


async def test_shutdown_kills_all_workers(stub_pool) -> None:
    """SC-13 partial: graceful shutdown terminates every worker subprocess."""
    w1 = await stub_pool.spawn_worker()
    w2 = await stub_pool.spawn_worker()

    await stub_pool.shutdown()

    await asyncio.sleep(0.05)
    assert w1.process.returncode is not None
    assert w2.process.returncode is not None
    assert all(not w.alive for w in [w1, w2])


async def test_shutdown_is_idempotent(stub_pool) -> None:
    """Calling shutdown twice must not raise."""
    await stub_pool.spawn_worker()
    await stub_pool.shutdown()
    await stub_pool.shutdown()  # must not raise


# ── mcp_infra singleton ───────────────────────────────────────────────────


def test_get_operator_pool_singleton_returns_same_instance(monkeypatch) -> None:
    """``get_operator_pool()`` is a module-level singleton mirroring the
    T1/T3 pattern in mcp_infra.py. Multiple calls return the same object."""
    from nexus import mcp_infra

    # Reset any previous cached pool
    monkeypatch.setattr(mcp_infra, "_operator_pool", None, raising=False)

    p1 = mcp_infra.get_operator_pool()
    p2 = mcp_infra.get_operator_pool()
    assert p1 is p2


def test_get_operator_pool_uses_config_size(monkeypatch, tmp_path) -> None:
    """``get_operator_pool()`` reads ``.nexus.yml: operators.pool_size``
    when initializing the singleton (default 2 per RDR-079)."""
    from nexus import mcp_infra

    monkeypatch.setattr(mcp_infra, "_operator_pool", None, raising=False)

    def fake_load_config():
        return {"operators": {"pool_size": 5, "max_budget_usd": 0.25}}

    monkeypatch.setattr(mcp_infra, "_load_config_for_pool", fake_load_config,
                        raising=False)

    pool = mcp_infra.get_operator_pool()
    assert pool.size == 5
    assert pool.max_budget_usd == 0.25


def test_reset_operator_pool_for_testing(monkeypatch) -> None:
    """Test-injection seam: ``reset_operator_pool()`` clears the cached
    singleton so tests that need a fresh one can re-init."""
    from nexus import mcp_infra

    monkeypatch.setattr(mcp_infra, "_operator_pool", None, raising=False)
    p1 = mcp_infra.get_operator_pool()
    mcp_infra.reset_operator_pool()
    p2 = mcp_infra.get_operator_pool()
    assert p1 is not p2


def test_get_operator_pool_wires_session_and_env(monkeypatch, tmp_path) -> None:
    """P3 critical: ``get_operator_pool()`` must call ``create_pool_session()``
    AND set ``NEXUS_T1_SESSION_ID`` in os.environ — otherwise every
    operator MCP tool's first call raises PoolConfigError because
    ``spawn_worker()`` sees no env. (Review finding #7.)"""
    from nexus import mcp_infra
    from nexus.operators import pool as pool_mod

    monkeypatch.delenv("NEXUS_T1_SESSION_ID", raising=False)
    monkeypatch.setattr(mcp_infra, "_operator_pool", None, raising=False)

    # Stub T1 server so we don't actually start ChromaDB.
    fake_start = lambda: ("127.0.0.1", 65432, 11111, str(tmp_path))  # noqa: E731
    monkeypatch.setattr(pool_mod, "_start_t1_server", fake_start)
    # Route session files to tmp_path so the test doesn't touch the real dir.
    monkeypatch.setattr(pool_mod, "SESSIONS_DIR", tmp_path)

    pool = mcp_infra.get_operator_pool()
    assert pool.pool_session is not None
    assert pool.pool_session.session_id.startswith("pool-")
    # Env var is set — subsequent spawn_worker() calls will pass the guard.
    assert os.environ.get("NEXUS_T1_SESSION_ID") == pool.pool_session.session_id
    # Session file was written under the test's SESSIONS_DIR.
    assert (tmp_path / f"{pool.pool_session.session_id}.session").exists()


def test_reset_singletons_also_clears_operator_pool(monkeypatch) -> None:
    """Review finding #8: ``reset_singletons()`` must clear the pool too,
    otherwise tests using the broader reset helper leak a live pool
    across test boundaries."""
    from nexus import mcp_infra

    monkeypatch.setattr(mcp_infra, "_operator_pool", None, raising=False)
    p1 = mcp_infra.get_operator_pool()
    assert mcp_infra._operator_pool is p1
    mcp_infra.reset_singletons()
    assert mcp_infra._operator_pool is None


def test_dispatch_raises_on_dead_worker(stub_pool) -> None:
    """Review finding #4: dispatch must refuse a worker whose ``alive``
    flag is False rather than queueing behind a dead subprocess.
    Protects P3 callers holding stale worker references."""
    import asyncio as _asyncio
    from nexus.operators.pool import PoolSpawnError

    async def scenario():
        w = await stub_pool.spawn_worker()
        w.alive = False  # simulate post-timeout state
        with pytest.raises(PoolSpawnError, match="not alive"):
            await stub_pool.dispatch(w, prompt="noop", timeout=5.0)

    _asyncio.new_event_loop().run_until_complete(scenario())
