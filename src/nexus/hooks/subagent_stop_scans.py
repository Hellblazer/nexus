# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The two SubagentStop transcript scans (RDR-215 bead nexus-q02nx.12).

Port of ``conexus/hooks/scripts/subagent-stop-scan.py`` and
``subagent-stop-writes-scan.py``. They stay two functions with two
contracts, exactly as they were two scripts with two contracts: the report
scan answers "did this agent send its report", the writes scan answers "did
any of its storage writes come back failed". The writes scan's own header
gives the reason and it survives the port unchanged -- the report scan's
caller matches ``FOUND``/``NOTFOUND`` exactly and treats everything else as
fail-open, so folding a third verdict into it would make the report check
fail open on the new value.

**The sibling-file constraint is gone, and that is the point.** Both bodies
lived outside ``subagent-stop.sh`` because bash 5.3 pipes heredoc bodies to
the child and a >512-byte body deadlocks when macOS degrades pipe buffers
(bead nexus-2gcqk, pinned by ``tests/hooks/test_heredoc_pipe_budget.py``).
Nothing here pipes anything: the hook imports these functions and calls
them in-process. No ``python3`` on ``PATH`` to find, no subprocess to spawn,
no pipe to deadlock. That is one of the two costs RDR-215 set out to
remove, and this module is where the removal actually happens for the
hook whose timing budget is the epic's ranked risk.

**Verdict TOKENS, not booleans.** The callers in
``tests/hooks/test_subagent_stop_hook.py`` assert these strings, and the
bash decision table is written in terms of them. Returning the token keeps
the port checkable line by line against the table it replaces.
"""
from __future__ import annotations

import json
import os
from typing import Any

__all__ = [
    "REPORT_TOOLS",
    "report_scan",
    "report_verdict",
    "writes_scan",
    "writes_verdict",
]

#: The tool names that carry a completion report to the orchestrator. The
#: harness's own hand-back call IS a background agent's report (bead
#: nexus-4xo3k): before it counted, every hand-back-reporting agent was
#: blocked once and re-sent the same report as a SendMessage.
REPORT_TOOLS: frozenset[str] = frozenset({"SendMessage", "SubagentHandback"})

#: Suffix-matched against the MCP tool name, which arrives fully qualified
#: (``mcp__plugin_conexus_nexus__memory_put``). Value is the input field that
#: must hold one of the listed actions for the call to count as a write, or
#: ``None`` when every call to the tool is a write.
_WRITE_TOOLS: dict[str, tuple[str, frozenset[str]] | None] = {
    "memory_put": None,
    "store_put": None,
    "scratch": ("action", frozenset({"put"})),
    "scratch_manage": ("action", frozenset({"promote", "flag"})),
}

#: Every ``_mcp_tool_error`` return begins with this, for all three of its
#: shapes (bare, connection-hint, and the T1-401/SESSION_UNAUTHORIZED_MARKER
#: branch). ``src/nexus/mcp/core.py``.
_ERROR_PREFIX = "Error:"


def _readable_file(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.R_OK)


def _blocks(entry: dict) -> list:
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    return content if isinstance(content, list) else []


def report_scan(path: str) -> bool:
    """True iff an ASSISTANT ``tool_use`` names a report tool.

    Scoped to assistant tool_use blocks so SendMessage-shaped JSON the agent
    merely READ -- nested inside a tool_result -- never counts as its own
    report.
    """
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or (
                '"SendMessage"' not in line and '"SubagentHandback"' not in line
            ):
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or entry.get("type") != "assistant":
                continue
            for block in _blocks(entry):
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") in REPORT_TOOLS
                ):
                    return True
    return False


def report_verdict(path: str) -> str:
    """``FOUND`` / ``NOTFOUND`` / ``SKIP`` / ``SCANERROR``.

    ``SKIP`` is the missing-or-unreadable transcript, which the bash caller
    produced itself before ever reaching the scan; ``SCANERROR`` is a crash
    inside it. The decision table treats both as fail-open, but they are
    kept distinct because they mean different things to anyone reading a
    ledger beside a transcript.
    """
    if not _readable_file(path):
        return "SKIP"
    try:
        return "FOUND" if report_scan(path) else "NOTFOUND"
    except Exception:  # noqa: BLE001 — fail open; a crashed scan must never block
        return "SCANERROR"


def _tool_key(name: Any) -> str:
    """Bare tool name from a possibly MCP-qualified one."""
    return str(name or "").rsplit("__", 1)[-1]


def _is_write_call(name: Any, tool_input: Any) -> bool:
    key = _tool_key(name)
    if key not in _WRITE_TOOLS:
        return False
    spec = _WRITE_TOOLS[key]
    if spec is None:
        return True
    field, allowed = spec
    if not isinstance(tool_input, dict):
        return False
    return str(tool_input.get(field, "")).strip().lower() in allowed


def _result_text(block: dict) -> str:
    """Best-effort flatten of a tool_result content payload to str."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item["text"]
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return ""


def writes_scan(path: str) -> tuple[int, list[str]]:
    """``(unlanded_count, sorted tool names)`` for failed write results.

    Correlates each write ``tool_use`` to its ``tool_result`` by
    ``tool_use_id``. POSITIVE EVIDENCE ONLY: a line it cannot parse is
    skipped rather than counted either way, which is what keeps this
    compatible with the hook's fail-open contract. It can add evidence,
    never manufacture it from absence.
    """
    pending: dict[Any, str] = {}
    failed: dict[Any, str] = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            for block in _blocks(entry):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use":
                    if _is_write_call(block.get("name"), block.get("input")):
                        pending[block.get("id")] = _tool_key(block.get("name"))
                elif btype == "tool_result":
                    tid = block.get("tool_use_id")
                    if tid not in pending:
                        continue
                    text = _result_text(block).lstrip()
                    if block.get("is_error") is True or text.startswith(_ERROR_PREFIX):
                        failed[tid] = pending[tid]
    return len(failed), sorted(set(failed.values()))


def writes_verdict(path: str) -> str:
    """``CLEAN`` or ``UNLANDED <n> <comma-joined tools>``.

    Everything that is not an affirmative failure reads as ``CLEAN``,
    including a crash -- the bash caller collapsed ``SCANERROR`` the same
    way. CLEAN means "no write reported failure", NOT "the writes landed":
    a store that returns a success string while landing nothing is
    invisible here, because the transcript records the success string.
    """
    if not _readable_file(path):
        return "CLEAN"
    try:
        count, tools = writes_scan(path)
    except Exception:  # noqa: BLE001 — fail open, exactly as the bash did
        return "CLEAN"
    return f"UNLANDED {count} {','.join(tools)}" if count else "CLEAN"
