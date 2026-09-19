# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-cnzei.6 (injection audit S5): the SessionStart combined-budget
check (test_session_start_combined_budget.py) has no SubagentStart
counterpart, even though the injection audit measured a real SubagentStart
context at ~10-12KB (T2 nexus/llm-guidance-audit-injection-2026-09-13).
This is that counterpart.

The sn emitter is a subprocess-invoked shell script with its own
JSON-envelope contract; the conexus emitter is the ported
``nexus.hooks.subagent_start.run()`` (RDR-215 bead nexus-q02nx.21 deleted
``conexus/hooks/scripts/subagent-start.sh``), driven in a CHILD PROCESS
via ``_CONEXUS_PY_DRIVER`` for the same reason ``test_subagent_start_hook.py``
drives it that way — these tests vary ``cwd`` per case, which is
process-global. A general-purpose, no-task/no-prompt payload is used
throughout so the census reflects what a REAL dispatch actually receives
(see test_subagent_start_hook.py::TestAgentTypeClassification for why a
fabricated task/prompt field would be misleading here).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SN_SCRIPT = _REPO_ROOT / "sn" / "hooks" / "scripts" / "mcp-inject.sh"

_CONEXUS_PY_DRIVER = """
import json, sys
from nexus._hook_runtime._io import never_fail
from nexus.hooks import subagent_start

raw = sys.stdin.read()
try:
    payload = json.loads(raw) if raw.strip() else None
except Exception:
    payload = None
if not isinstance(payload, dict):
    payload = None
result = never_fail(lambda: subagent_start.run(payload), "subagent_start")
if result.stdout is not None:
    sys.stdout.write(result.stdout + "\\n")
sys.exit(result.exit_code)
"""

#: Shaped like a real dispatch: agent_id/agent_type/session_id/prompt_id,
#: no task, no prompt (see the injection audit's own measured payload
#: shape, and TestAgentTypeClassification in test_subagent_start_hook.py).
_REAL_SHAPE_PAYLOAD = json.dumps({
    "session_id": "budget-census-session",
    "hook_event_name": "SubagentStart",
    "agent_id": "abudget00000000000000000",
    "agent_type": "general-purpose",
    "prompt_id": "abc123",
})

#: Budget for the common (non-worktree) case. Measured 2026-09-19 against
#: the RDR-215 Python port (nexus-q02nx.21 deleted the bash script this
#: used to measure): conexus 4098B + sn 4100B = 8198B, stable across
#: repeated runs -- conexus grew ~587B over the bash script's own 3511B,
#: which is why this budget moves rather than carrying over unchanged.
#: nexus-cnzei.6 fix round (critic Critical 1) set the ~8% headroom
#: convention this keeps: 8198 * 1.08 ~= 8854, rounded down. conexus's own
#: content includes a live "Ready Beads"/"Active Bead" section (T2/bd
#: state on this box), so a future budget failure may be live-state churn
#: rather than a code regression -- re-measure before concluding either
#: way.
_SUBAGENT_START_BUDGET_BYTES = 8850

#: Budget for an isolation:worktree dispatch: sn's mcp-inject.sh adds
#: worktree-section.md on top of its normal output, and conexus's ported
#: subagent_start.py resolves T2/Knowledge-Map paths via --git-common-dir
#: instead of --show-toplevel (see TestWorktreeProjectResolution in
#: test_subagent_start_hook.py) -- a different code path, not just a size
#: delta, so this is measured end-to-end through a real linked worktree
#: rather than computed as budget-plus-delta. Re-measured 2026-09-19
#: against the Python port: conexus 3502B (smaller than its own
#: non-worktree case -- the Knowledge Map cache lookup and Ready-Beads
#: section behave differently under a throwaway worktree fixture repo)
#: + sn 6054B = 9556B, stable across repeated runs. This budget was
#: already comfortable headroom over that (9556 * 1.077 ~= 10293) before
#: the port, so it carries over unchanged; re-measure before concluding
#: either way on a future failure, same as the budget above.
_SUBAGENT_START_WORKTREE_BUDGET_BYTES = 10300


def _run_conexus(*, cwd: str | None = None, payload: str = _REAL_SHAPE_PAYLOAD, timeout: float = 15) -> str:
    env = {**os.environ, "PATH": os.environ.get("PATH", "")}
    result = subprocess.run(
        [sys.executable, "-c", _CONEXUS_PY_DRIVER],
        input=payload,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=cwd,
    )
    assert result.returncode == 0, f"nexus.hooks.subagent_start exited {result.returncode}: {result.stderr}"
    payload_out = json.loads(result.stdout)
    return payload_out["hookSpecificOutput"]["additionalContext"]


def _run_sn(*, payload: str = _REAL_SHAPE_PAYLOAD, timeout: float = 15) -> str:
    env = {**os.environ, "PATH": os.environ.get("PATH", "")}
    result = subprocess.run(
        ["bash", str(_SN_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    assert result.returncode == 0, f"{_SN_SCRIPT.name} exited {result.returncode}: {result.stderr}"
    payload_out = json.loads(result.stdout)
    return payload_out["hookSpecificOutput"]["additionalContext"]


def test_sn_subagent_start_script_exists() -> None:
    """The conexus half is the ported ``nexus.hooks.subagent_start``
    module now (RDR-215 bead nexus-q02nx.21); an importable module needs
    no existence check the way a script path does."""
    assert _SN_SCRIPT.exists(), _SN_SCRIPT


def test_combined_subagent_start_total_under_budget() -> None:
    conexus_ctx = _run_conexus()
    sn_ctx = _run_sn()
    conexus_bytes = len(conexus_ctx.encode("utf-8"))
    sn_bytes = len(sn_ctx.encode("utf-8"))
    total = conexus_bytes + sn_bytes
    assert total < _SUBAGENT_START_BUDGET_BYTES, (
        f"combined SubagentStart total {total}B (conexus {conexus_bytes}B + "
        f"sn {sn_bytes}B) >= budget {_SUBAGENT_START_BUDGET_BYTES}B"
    )
    # Non-vacuity: both scripts must actually have produced content, or the
    # budget "passes" by measuring nothing.
    assert conexus_bytes > 1000, "nexus.hooks.subagent_start produced suspiciously little content"
    assert sn_bytes > 100, "sn mcp-inject.sh produced suspiciously little content"


def test_combined_subagent_start_total_under_budget_in_a_worktree(tmp_path) -> None:
    """nexus-cnzei.6 fix round (critic Critical 1): a worktree dispatch is
    a real, named, larger case (injection audit: ~12KB vs ~10KB) that the
    non-worktree test above cannot see — sn's mcp-inject.sh only emits
    worktree-section.md when it detects a linked worktree
    (sn/hooks/scripts/worktree_guard.py::is_linked_worktree, keyed on the
    payload's own `cwd` field), and conexus's ported subagent_start.py only takes
    its --git-common-dir path when its OS-level cwd actually is one. Both
    are exercised for real here: a throwaway git repo + `git worktree add`
    under tmp_path (same construction as
    TestWorktreeProjectResolution.test_t2_scan_uses_main_repo_name_not_worktree_dir_name
    in test_subagent_start_hook.py), conexus run with that directory as
    its subprocess cwd, sn given it via the payload's `cwd` field."""
    main_repo = tmp_path / "the-real-project"
    main_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(main_repo)], check=True)
    subprocess.run(
        ["git", "-C", str(main_repo), "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
    )
    worktree_dir = tmp_path / "agent-worktree-budget-probe"
    subprocess.run(
        ["git", "-C", str(main_repo), "worktree", "add", "-q", str(worktree_dir), "-b", "wt-branch"],
        check=True,
    )

    conexus_ctx = _run_conexus(cwd=str(worktree_dir))
    worktree_payload = json.dumps({
        "session_id": "budget-census-session",
        "hook_event_name": "SubagentStart",
        "agent_id": "abudget00000000000000000",
        "agent_type": "general-purpose",
        "prompt_id": "abc123",
        "cwd": str(worktree_dir),
    })
    sn_ctx = _run_sn(payload=worktree_payload)
    assert "worktree" in sn_ctx.lower(), (
        "sn mcp-inject.sh did not emit worktree-section.md for a real linked "
        "worktree cwd — the worktree-detection path this test exercises is "
        "not firing, so this test would vacuously pass the smaller, wrong case"
    )

    conexus_bytes = len(conexus_ctx.encode("utf-8"))
    sn_bytes = len(sn_ctx.encode("utf-8"))
    total = conexus_bytes + sn_bytes
    assert total < _SUBAGENT_START_WORKTREE_BUDGET_BYTES, (
        f"combined SubagentStart worktree-mode total {total}B (conexus "
        f"{conexus_bytes}B + sn {sn_bytes}B) >= budget "
        f"{_SUBAGENT_START_WORKTREE_BUDGET_BYTES}B"
    )
