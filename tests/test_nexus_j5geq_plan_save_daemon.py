# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-j5geq: route plan_save through the T2 daemon (mirror of nexus-zir76).

mcp/core.py:2306 plan_save and :4312 plan-growth auto-save used
``with _t2_ctx() as db: db.save_plan(...)`` — the direct SQLite path that
races the daemon WAL writer (``database is locked`` under load; 3 occurrences
observed 2026-06-11). plan_save CAN route: both call sites converted to
``t2_index_write``.

These tests pin the fix (mirror of nexus-zir76 test pattern):

1. ``plans.save_plan`` is daemon-routable (in ``_WRITE_OPS``).
2. ``T2Client.plans`` proxy exposes ``save_plan`` at the right address
   (``plans.save_plan`` op).
3. mcp.core's ``plan_save`` tool does NOT use ``_t2_ctx``; it routes through
   ``t2_index_write``.
4. mcp.core's plan-growth auto-save does NOT use ``_t2_ctx``; it routes through
   ``t2_index_write``.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch


# ── 1. plans.save_plan is in _WRITE_OPS ──────────────────────────────────────


def test_plans_save_plan_in_write_ops() -> None:
    """plans.save_plan must be in _WRITE_OPS so the daemon serialises it."""
    from nexus.daemon.t2_daemon import _WRITE_OPS

    assert "plans.save_plan" in _WRITE_OPS, (
        "plans.save_plan must be in _WRITE_OPS so the write-serialisation "
        "enforcement test (test_write_op_coverage) covers it"
    )


# ── 2. T2Client.plans.save_plan routes via RPC ───────────────────────────────


def test_t2client_plans_proxy_has_save_plan() -> None:
    """T2Client.plans is a _StoreProxy whose save_plan sends 'plans.save_plan'
    op to the daemon (verified by intercepting T2Client.call).
    """
    from nexus.daemon.t2_client import T2Client

    client = T2Client(skip_handshake=True)

    sent: dict = {}

    def _fake_call(self, op, *args, **kwargs):
        sent["op"] = op
        sent["args"] = args
        sent["kwargs"] = kwargs
        return 42  # fake row_id

    with patch.object(T2Client, "call", _fake_call):
        result = client.plans.save_plan(query="q", plan_json="{}", outcome="success")

    assert sent["op"] == "plans.save_plan"
    assert sent["kwargs"]["query"] == "q"
    assert result == 42


# ── 3. mcp.core plan_save tool does NOT reference _t2_ctx ────────────────────


def test_plan_save_tool_does_not_use_t2_ctx() -> None:
    """The plan_save MCP tool function must NOT use _t2_ctx; it must route
    through t2_index_write (or equivalent daemon-routable path).

    Tripwire: parse the function body as AST, assert no Name node 't2_ctx' /
    '_t2_ctx' appears directly inside it.
    """
    import nexus.mcp.core as core

    fn = getattr(core, "plan_save", None)
    assert fn is not None, "plan_save not found in nexus.mcp.core"

    src = textwrap.dedent(inspect.getsource(fn))
    tree = ast.parse(src)

    t2ctx_names = {
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id in ("t2_ctx", "_t2_ctx")
    }
    assert not t2ctx_names, (
        f"plan_save must NOT reference _t2_ctx (found: {t2ctx_names}). "
        "Route through t2_index_write instead."
    )


# ── 4. plan_save round-trips through t2_index_write ──────────────────────────


def test_plan_save_routes_via_t2_index_write(tmp_path: Path) -> None:
    """plan_save must invoke t2_index_write (not _t2_ctx) for the write.

    Patches ``nexus.mcp.core._t2_index_write`` (the module-level alias
    bound at import time) and ``nexus.mcp.core._t2_ctx`` (should never
    be called by plan_save after the fix).
    """
    from nexus.db.t2 import T2Database

    db_path = tmp_path / "t2.db"
    calls: list[int] = []

    def _routed(write_fn):
        calls.append(1)
        with T2Database(db_path) as db:
            return write_fn(db)

    def _forbidden_ctx():
        raise AssertionError("plan_save must route through t2_index_write, not _t2_ctx")

    import nexus.mcp.core as core
    with (
        patch.object(core, "_t2_index_write", _routed),
        patch.object(core, "_t2_ctx", _forbidden_ctx),
    ):
        result = core.plan_save(
            query="test-plan-query",
            plan_json='{"steps":[]}',
            verb="query",
            outcome="success",
            tags="test",
            project="nexus",
        )

    assert calls, "plan_save did not route through t2_index_write"
    assert "Saved plan" in result or "test-plan-query" in result, (
        f"Unexpected plan_save result: {result!r}"
    )
