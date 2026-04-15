# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for RDR-079 P2.4 — worker-mode MCP tool-surface restriction.

When ``NEXUS_MCP_WORKER_MODE=1`` is set in the environment at module
import time, the ``nexus`` MCP server registers EVERY tool EXCEPT
the seven that would let a worker recurse back into the pool:
``plan_match``, ``plan_run``, and the five ``operator_*`` tools
(``operator_extract``, ``operator_rank``, ``operator_compare``,
``operator_summarize``, ``operator_generate``).

Worker subprocess flow:
  pool.spawn_worker spawns ``claude -p --mcp-config <worker-mode.json>
  --strict-mcp-config`` where the JSON points ``nx-mcp`` at the
  worker-mode variant. Worker's MCP ``tools/list`` therefore does NOT
  include the dispatch-surface tools — recursion impossible by
  construction (SC-12, invariant I-2).

Tests exercise both modes. Because MCP tool registration happens at
module import, mode-switching requires a subprocess — inline
monkeypatching of ``NEXUS_MCP_WORKER_MODE`` after import has no effect.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest


FORBIDDEN_IN_WORKER_MODE: frozenset[str] = frozenset({
    "plan_match",
    "plan_run",
    "operator_extract",
    "operator_rank",
    "operator_compare",
    "operator_summarize",
    "operator_generate",
})

REQUIRED_IN_WORKER_MODE: frozenset[str] = frozenset({
    "search",
    "query",
    "store_get",
    "memory_get",
    "memory_search",
    "scratch",
})


def _tools_in_mode(worker_mode: bool) -> set[str]:
    """Spawn a clean Python subprocess with the env var set as requested,
    import nexus.mcp.core, and print the registered tool names.

    Returns the set of tool names registered on the FastMCP instance.
    """
    env = os.environ.copy()
    if worker_mode:
        env["NEXUS_MCP_WORKER_MODE"] = "1"
    else:
        env.pop("NEXUS_MCP_WORKER_MODE", None)
    result = subprocess.run(
        [
            sys.executable, "-c",
            "from nexus.mcp.core import mcp; "
            "print('\\n'.join(sorted(mcp._tool_manager._tools.keys())))",
        ],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"import failed:\nstdout:{result.stdout}\nstderr:{result.stderr}"
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


# ── Default (non-worker) mode: all tools registered ────────────────────────


def test_default_mode_registers_forbidden_tools() -> None:
    """Without the env var, plan_match / plan_run are registered as usual —
    regression guard so a future refactor doesn't accidentally remove
    them from the full-mode MCP surface."""
    tools = _tools_in_mode(worker_mode=False)
    assert "plan_match" in tools
    assert "plan_run" in tools
    assert "plan_save" in tools
    assert "plan_search" in tools


# ── Worker mode: forbidden tools absent, required tools present ────────────


def test_worker_mode_excludes_plan_tools() -> None:
    """SC-12: worker-mode MCP tools/list must not include plan_match or
    plan_run. Prevents a pool worker from re-entering the pool via
    these dispatch surfaces (invariant I-2)."""
    tools = _tools_in_mode(worker_mode=True)
    assert "plan_match" not in tools, (
        "plan_match must be absent from worker-mode tool list"
    )
    assert "plan_run" not in tools, (
        "plan_run must be absent from worker-mode tool list"
    )


def test_worker_mode_includes_retrieval_tools() -> None:
    """SC-12: workers retain read-side MCP access so operators that
    need store_get or search work — they just can't dispatch plans
    or spawn other operators."""
    tools = _tools_in_mode(worker_mode=True)
    for name in ("search", "query", "store_get"):
        assert name in tools, (
            f"{name!r} must remain in worker-mode tool list "
            f"(read-side access is intentional)"
        )


def test_worker_mode_includes_memory_and_scratch() -> None:
    """memory_* and scratch are allowed in worker mode — writes target
    the pool's isolated T1 session via NEXUS_T1_SESSION_ID, not the
    user's session. See RDR-079 §Worker isolation."""
    tools = _tools_in_mode(worker_mode=True)
    assert "memory_get" in tools
    assert "memory_search" in tools
    assert "scratch" in tools


def test_worker_mode_does_not_break_callability_from_python() -> None:
    """Filtering at registration time must NOT remove the Python
    functions — they stay importable and callable from same-process
    code (the runner still calls plan_match / plan_run in the
    controller process). Only MCP-over-stdio is filtered."""
    env = os.environ.copy()
    env["NEXUS_MCP_WORKER_MODE"] = "1"
    result = subprocess.run(
        [
            sys.executable, "-c",
            "from nexus.mcp.core import plan_match, plan_run; "
            "print('ok', callable(plan_match), callable(plan_run))",
        ],
        env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "ok True True" in result.stdout


def test_worker_mode_env_values_other_than_1_are_ignored() -> None:
    """Only the exact value ``1`` enables worker mode. Accidental values
    like empty string, ``0``, ``false`` do not trigger the restriction.
    This protects against inherited env clutter."""
    for value in ("", "0", "false", "no", "False"):
        env = os.environ.copy()
        env["NEXUS_MCP_WORKER_MODE"] = value
        result = subprocess.run(
            [
                sys.executable, "-c",
                "from nexus.mcp.core import mcp; "
                "print('plan_match' in mcp._tool_manager._tools)",
            ],
            env=env, capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0
        assert "True" in result.stdout, (
            f"NEXUS_MCP_WORKER_MODE={value!r} must NOT trigger worker mode"
        )
