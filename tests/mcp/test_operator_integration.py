# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration test for RDR-079 P3 operator tools through the real
async dispatch path.

Unlike the unit tests (test_operator_extract.py, test_operator_tools.py)
which monkeypatch `get_operator_pool`, this test exercises the full
stack: `operator_extract` → `OperatorPool.dispatch_with_rotation` →
worker subprocess (claude_stub.py) → streaming JSON reader → return
value. This is the test that would have caught C-1 (``asyncio.run()``
inside a running event loop) — review-gap from the initial P3 commit.

No live ``claude`` or API key — the stub simulates the streaming
protocol with canned StructuredOutput payloads.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

STUB_PATH = Path(__file__).parent.parent / "operators" / "fixtures" / "claude_stub.py"


pytestmark = pytest.mark.asyncio


@pytest.fixture
def stub_operator_pool(monkeypatch):
    """Wire up mcp_infra.get_operator_pool to return a real OperatorPool
    whose workers are the claude_stub, not real claude. Bypasses auth
    check. The pool singleton caches per operator_name so the test
    reuses one pool instance."""
    from nexus import mcp_infra
    from nexus.operators.pool import OperatorPool
    import nexus.operators.pool as pool_mod

    # Reset any cached pool state from prior tests.
    mcp_infra.reset_operator_pool()
    monkeypatch.setenv("NEXUS_T1_SESSION_ID", "pool-integration-test")
    monkeypatch.setattr(pool_mod, "check_auth", lambda: None)
    monkeypatch.setattr(
        pool_mod, "build_worker_cmdline",
        lambda **kw: [sys.executable, str(STUB_PATH)],
    )

    pools_by_name: dict[str, OperatorPool] = {}

    def fake_get(operator_name=None, *, operator_role=None, json_schema=None):
        key = operator_name or "__default__"
        if key not in pools_by_name:
            p = OperatorPool(
                size=1, max_budget_usd=0.5, max_turns=4,
                retirement_token_threshold=10_000,
                operator_role=operator_role or "You are test",
                json_schema=json_schema,
            )
            pools_by_name[key] = p
        return pools_by_name[key]

    monkeypatch.setattr(mcp_infra, "get_operator_pool", fake_get)
    yield pools_by_name
    # Teardown: kill any spawned workers
    for pool in pools_by_name.values():
        for w in list(pool.workers):
            if w.process.returncode is None:
                try:
                    w.process.kill()
                except ProcessLookupError:
                    pass


async def test_operator_extract_full_async_dispatch_path(stub_operator_pool) -> None:
    """Round-trip through the full async dispatch stack.

    This is the test that would have caught ``asyncio.run()`` inside a
    running event loop (review finding C-1). Because pytest-asyncio
    runs this test inside its own event loop, calling a sync operator
    tool that used ``asyncio.run`` would raise ``RuntimeError: cannot
    be called from a running event loop``. With the fix in place
    (operators are native ``async def`` and ``await`` the pool
    directly), this test passes cleanly."""
    from nexus.mcp.core import operator_extract

    # The stub echoes input text into extractions[].echo and returns
    # {"extractions": [{"echo": <verbatim user text preview>}]}.
    result = await operator_extract(
        inputs='["The Rise of Arcaneum by Hal Hildebrand, 2024"]',
        fields="title,year,author",
        timeout=30.0,
    )
    assert isinstance(result, dict)
    assert "extractions" in result
    assert isinstance(result["extractions"], list)
    # Stub produces exactly one extraction per dispatch turn
    assert len(result["extractions"]) == 1
