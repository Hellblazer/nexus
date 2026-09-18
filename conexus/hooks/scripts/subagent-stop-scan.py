#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transcript report-scan for the SubagentStop hook (subagent-stop.sh).

Prints exactly one verdict token: FOUND / NOTFOUND / SCANERROR. A report
is an assistant tool_use named ``SendMessage`` OR ``SubagentHandback``
(bead nexus-4xo3k): the harness's own hand-back call is how a background
agent's final report reaches its spawner, and before this the scan saw
only SendMessage, so every hand-back-reporting agent was blocked once and
re-sent the same report through SendMessage. Lives in
a sibling file rather than a heredoc because bash 5.3 pipes heredoc
bodies to the child and a >512-byte body deadlocks when macOS degrades
pipe buffers under pressure (bead nexus-2gcqk; pinned by
tests/hooks/test_heredoc_pipe_budget.py).
"""
import json
import sys

#: The tool names that carry a completion report to the orchestrator.
REPORT_TOOLS: frozenset[str] = frozenset({"SendMessage", "SubagentHandback"})


def scan(path) -> bool:
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or ('"SendMessage"' not in line and '"SubagentHandback"' not in line):
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "assistant":
                continue
            msg = entry.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") in REPORT_TOOLS
                ):
                    return True
    return False


try:
    print("FOUND" if scan(sys.argv[1]) else "NOTFOUND")
except Exception:
    print("SCANERROR")
