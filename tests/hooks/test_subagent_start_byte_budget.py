# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-cnzei.6 (injection audit S5): the SessionStart combined-budget
check (test_session_start_combined_budget.py) has no SubagentStart
counterpart, even though the injection audit measured a real SubagentStart
context at ~10-12KB (T2 nexus/llm-guidance-audit-injection-2026-09-13).
This is that counterpart.

Both emitters here are subprocess-invoked shell scripts with their own
JSON-envelope contract, exactly the pattern test_subagent_start_hook.py
already uses safely (no live mailbox watcher, no session-lease writes —
that hazard is specific to `nx hook session-start`, per AGENTS.md). A
general-purpose, no-task/no-prompt payload is used throughout so the
census reflects what a REAL dispatch actually receives (see
test_subagent_start_hook.py::TestAgentTypeClassification for why a
fabricated task/prompt field would be misleading here).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONEXUS_SCRIPT = _REPO_ROOT / "conexus" / "hooks" / "scripts" / "subagent-start.sh"
_SN_SCRIPT = _REPO_ROOT / "sn" / "hooks" / "scripts" / "mcp-inject.sh"

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

#: Budget: the audit's measured ~10-12KB (non-worktree) real total, plus
#: margin. A worktree dispatch adds sn's own ~1,954B worktree-section.md on
#: top of this — deliberately not included here, since that block is
#: conditional on isolation:worktree and this census is the common-case
#: (non-worktree) total, matching the audit's own "fg and bg identical" ~10KB
#: figure rather than its ~12KB worktree variant.
_SUBAGENT_START_BUDGET_BYTES = 10000


def _run(script: Path, timeout: float = 15) -> str:
    env = {**os.environ, "PATH": os.environ.get("PATH", "")}
    result = subprocess.run(
        ["bash", str(script)],
        input=_REAL_SHAPE_PAYLOAD,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    assert result.returncode == 0, f"{script.name} exited {result.returncode}: {result.stderr}"
    payload = json.loads(result.stdout)
    return payload["hookSpecificOutput"]["additionalContext"]


def test_conexus_and_sn_subagent_start_scripts_exist() -> None:
    assert _CONEXUS_SCRIPT.exists(), _CONEXUS_SCRIPT
    assert _SN_SCRIPT.exists(), _SN_SCRIPT


def test_combined_subagent_start_total_under_budget() -> None:
    conexus_ctx = _run(_CONEXUS_SCRIPT)
    sn_ctx = _run(_SN_SCRIPT)
    conexus_bytes = len(conexus_ctx.encode("utf-8"))
    sn_bytes = len(sn_ctx.encode("utf-8"))
    total = conexus_bytes + sn_bytes
    assert total < _SUBAGENT_START_BUDGET_BYTES, (
        f"combined SubagentStart total {total}B (conexus {conexus_bytes}B + "
        f"sn {sn_bytes}B) >= budget {_SUBAGENT_START_BUDGET_BYTES}B"
    )
    # Non-vacuity: both scripts must actually have produced content, or the
    # budget "passes" by measuring nothing.
    assert conexus_bytes > 1000, "conexus subagent-start.sh produced suspiciously little content"
    assert sn_bytes > 100, "sn mcp-inject.sh produced suspiciously little content"
