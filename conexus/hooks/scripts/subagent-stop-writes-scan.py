#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unlanded-write scan for the SubagentStop hook (nexus-piqm5 Layer 1).

Prints exactly one verdict token line: ``CLEAN`` / ``UNLANDED <n> <tools>``
/ ``SCANERROR``. Lives in a sibling file rather than a heredoc for the same
reason as subagent-stop-scan.py: bash 5.3 pipes heredoc bodies to the child
and a >512-byte body deadlocks when macOS degrades pipe buffers (bead
nexus-2gcqk; pinned by tests/hooks/test_heredoc_pipe_budget.py).

WHY A SEPARATE SCRIPT rather than a second token from subagent-stop-scan.py:
that script's contract is one verdict about whether a REPORT was sent, and
its caller matches FOUND / NOTFOUND exactly with ``*) exit 0`` fail-open.
Adding a token there would make the report check fail open on the new value.
Two questions, two scripts, two contracts.

WHAT THIS DETECTS. A subagent's T1/T2/T3 write can fail for its whole
session and the only signal today is whether the agent narrated it in prose
(nexus-piqm5; production loss 2026-08-25, when two reviewers' findings
survived only because both happened to mention the outage). Every write
failure funnels through ``nexus.mcp.core._mcp_tool_error`` and comes back as
a plain ``str`` starting ``"Error: "`` — the SAME TYPE as a success string.
Nothing downstream distinguishes them. This scan reads the agent's own
transcript, correlates each write tool_use to its tool_result by
``tool_use_id``, and reports results that came back failed.

WHY THE TRANSCRIPT AND NOT THE tier_writes LEDGER. The ledger read goes
through the service. When persistence is broken — the case this exists to
catch — that read fails too, and "no rows" is indistinguishable from "the
agent wrote nothing": the defect one level up, which nexus-piqm5 explicitly
forbids. The transcript is a local file and is readable precisely when the
store is not.

POSITIVE-EVIDENCE ONLY. This reports UNLANDED solely on an affirmative
failed result. A missing, unreadable, truncated or unparseable transcript
yields SCANERROR, and an entry it cannot parse is skipped rather than
counted. That keeps it compatible with subagent-stop.sh's documented
fail-open contract ("never block a stop on missing evidence"): it can only
ever add evidence, never manufacture it from absence.

SCOPE — WHAT IT CANNOT SEE. A store that returns a success string while
landing nothing (a silent no-op) is invisible here, because the transcript
records the success string. Catching that needs a read-back against a live
store, which is Layer 2 (tracked separately). Do not read a CLEAN verdict as
"the writes landed"; it means "no write reported failure".
"""
import json
import sys

# Suffix-matched against the MCP tool name (which arrives fully qualified,
# e.g. mcp__plugin_conexus_nexus__memory_put). Value is the input field that
# must hold one of the listed actions for the call to count as a write, or
# None when every call to the tool is a write.
_WRITE_TOOLS = {
    "memory_put": None,
    "store_put": None,
    "scratch": ("action", {"put"}),
    "scratch_manage": ("action", {"promote", "flag"}),
}

# Every _mcp_tool_error return begins with this, for all three of its
# shapes (bare, connection-hint, and the T1-401/SESSION_UNAUTHORIZED_MARKER
# branch). src/nexus/mcp/core.py:72-124.
_ERROR_PREFIX = "Error:"


def _tool_key(name):
    """Bare tool name from a possibly MCP-qualified one."""
    return (name or "").rsplit("__", 1)[-1]


def _is_write_call(name, tool_input):
    spec = _WRITE_TOOLS.get(_tool_key(name))
    if spec is None:
        return _tool_key(name) in _WRITE_TOOLS
    field, allowed = spec
    if not isinstance(tool_input, dict):
        return False
    return str(tool_input.get(field, "")).strip().lower() in allowed


def _result_text(block):
    """Best-effort flatten of a tool_result content payload to str."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _blocks(entry):
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    return content if isinstance(content, list) else []


def scan(path):
    """Return (unlanded_count, sorted tool names) for failed write results."""
    pending = {}          # tool_use_id -> bare tool name, for write calls
    failed = {}           # tool_use_id -> bare tool name, result came back failed
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                # One junk line must not void the whole scan, but it also
                # must not be counted as evidence either way.
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


try:
    count, tools = scan(sys.argv[1])
    if count:
        print("UNLANDED {} {}".format(count, ",".join(tools)))
    else:
        print("CLEAN")
except Exception:
    print("SCANERROR")
