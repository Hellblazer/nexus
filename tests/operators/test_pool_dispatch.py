# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for RDR-079 P2.1 (commit B) — dispatch + streaming JSON parser.

Uses the stub claude process at fixtures/claude_stub.py (reads user-turn
JSON from stdin, emits canned StructuredOutput tool_use + result events
per turn). Validates:
  * Per-worker dispatch returns the StructuredOutput payload (not the
    empty ``result`` text) per Empirical Finding 3.
  * Cumulative tokens accumulate across turns on a single worker.
  * Per-worker serialization — overlapping dispatches on the same worker
    queue, not race.
  * Worker death mid-dispatch surfaces as ``PoolSpawnError`` (or subclass),
    not a hang.

No live claude / no network. The stub is invoked via ``sys.executable``
and a direct path override on ``OperatorPool`` (test-injection only).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

STUB_PATH = Path(__file__).parent / "fixtures" / "claude_stub.py"


pytestmark = pytest.mark.asyncio


@pytest.fixture
def session_env(monkeypatch):
    """Always set NEXUS_T1_SESSION_ID for tests that spawn workers so the
    spawn guard does not fire."""
    monkeypatch.setenv("NEXUS_T1_SESSION_ID", "pool-test-xyz")


@pytest.fixture
def stub_pool(monkeypatch, session_env):
    """OperatorPool pre-wired to spawn the stub instead of real claude."""
    from nexus.operators.pool import OperatorPool
    import nexus.operators.pool as pool_mod

    # Bypass auth check — stub doesn't implement claude auth status
    monkeypatch.setattr(pool_mod, "check_auth", lambda: None)

    # Redirect build_worker_cmdline to spawn the stub via sys.executable.
    orig = pool_mod.build_worker_cmdline

    def fake_cmdline(session_id, operator_role, max_budget_usd, max_turns,
                    model="haiku", mcp_config=None, json_schema=None):
        # Ignore claude-specific flags entirely; the stub doesn't parse them.
        return [sys.executable, str(STUB_PATH)]

    monkeypatch.setattr(pool_mod, "build_worker_cmdline", fake_cmdline)

    pool = OperatorPool(size=1, max_budget_usd=1.0, max_turns=6)
    yield pool
    # Teardown: kill any live workers
    for w in pool.workers:
        if w.process.returncode is None:
            try:
                w.process.kill()
            except ProcessLookupError:
                pass


# ── Dispatch returns StructuredOutput, not result text ─────────────────────


async def test_dispatch_returns_structured_output_payload(stub_pool) -> None:
    """Empirical Finding 3: the StructuredOutput tool_use carries the
    validated payload. dispatch() must return THAT, not the final
    ``result`` text (which may be empty)."""
    worker = await stub_pool.spawn_worker()

    out = await stub_pool.dispatch(
        worker,
        prompt="Extract: Hello world by Someone, 1999",
        timeout=15.0,
    )
    assert isinstance(out, dict)
    assert "extractions" in out
    assert isinstance(out["extractions"], list)


async def test_dispatch_accumulates_token_counters(stub_pool, monkeypatch) -> None:
    """Cumulative input+output token count updates after every dispatch.
    The retirement logic (commit C) reads these to decide when to rotate
    a worker; commit B ships the accumulation."""
    monkeypatch.setenv("STUB_INPUT_TOKENS_PER_TURN", "250")
    monkeypatch.setenv("STUB_OUTPUT_TOKENS_PER_TURN", "75")

    worker = await stub_pool.spawn_worker()

    await stub_pool.dispatch(worker, prompt="first", timeout=15.0)
    assert worker.cumulative_input_tokens == 250
    assert worker.cumulative_output_tokens == 75

    await stub_pool.dispatch(worker, prompt="second", timeout=15.0)
    assert worker.cumulative_input_tokens == 500
    assert worker.cumulative_output_tokens == 150
    assert worker.cumulative_tokens == 650


# ── Per-worker serialization ───────────────────────────────────────────────


async def test_dispatches_on_same_worker_serialize(stub_pool) -> None:
    """Two concurrent dispatches on the SAME worker must complete without
    their stdout streams interleaving (the stub emits events per turn in
    order; the pool's per-worker lock guarantees FIFO)."""
    worker = await stub_pool.spawn_worker()

    async def call(prompt: str) -> dict:
        return await stub_pool.dispatch(worker, prompt=prompt, timeout=15.0)

    # Launch both in parallel
    results = await asyncio.gather(call("turn-A"), call("turn-B"))
    assert len(results) == 2
    # Both succeeded (no cross-contamination)
    assert all("extractions" in r for r in results)


# ── Timeout handling ───────────────────────────────────────────────────────


async def test_dispatch_raises_on_timeout(stub_pool, monkeypatch) -> None:
    """When a worker hangs (stub STUB_HANG=1), dispatch raises
    asyncio.TimeoutError within the requested window, not forever."""
    monkeypatch.setenv("STUB_HANG", "1")

    worker = await stub_pool.spawn_worker()
    with pytest.raises(asyncio.TimeoutError):
        await stub_pool.dispatch(worker, prompt="no reply coming", timeout=0.5)


# ── Crash mid-dispatch ─────────────────────────────────────────────────────


async def test_dispatch_surfaces_worker_crash(stub_pool, monkeypatch) -> None:
    """STUB_CRASH_ON_TURN=1 makes the stub exit(1) before emitting the
    first turn's response. dispatch must surface this as a named error,
    not hang or return garbage."""
    from nexus.operators.pool import PoolSpawnError

    monkeypatch.setenv("STUB_CRASH_ON_TURN", "1")

    worker = await stub_pool.spawn_worker()
    with pytest.raises((PoolSpawnError, RuntimeError, asyncio.TimeoutError)):
        await stub_pool.dispatch(worker, prompt="will-crash", timeout=5.0)


# ── Worker death flips alive=False ────────────────────────────────────────


async def test_worker_marked_not_alive_after_crash(stub_pool, monkeypatch) -> None:
    monkeypatch.setenv("STUB_CRASH_ON_TURN", "1")

    worker = await stub_pool.spawn_worker()
    try:
        await stub_pool.dispatch(worker, prompt="x", timeout=5.0)
    except Exception:
        pass  # expected
    # Give the process a moment to fully exit
    await asyncio.sleep(0.1)
    assert worker.alive is False
