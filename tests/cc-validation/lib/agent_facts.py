#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the dispatched subagents did, from their own transcripts (nexus-qsmc5).

    agent_facts.py <projects-dir> <nested-token-ere> <blocked-token-ere>

Reads every ``agent-*.jsonl`` under <projects-dir> and prints key=value lines:

    dispatched=1           some subagent called the Task/Agent tool and that call came back
                           without ``is_error`` (a call that errored, or never returned, is not
                           a dispatch)
    nested_elsewhere=1     the nested token is in the assistant text of a transcript that made
                           NO Task/Agent call: the leaf agent's own transcript, never the
                           fork's (a fork that was handed the pieces and wrote the token
                           itself, or echoed it, does not count)
    blocked=1              the blocked token is in some subagent's assistant text
    blocked_searched=1     that subagent called ToolSearch (it tried to load the deferred tool)
    blocked_dispatched=1   that subagent also called the Task/Agent tool (so "blocked" is false)
    tools=A,B,C            every tool name any subagent called

Stdlib only (the container's python3). The text a model writes can claim anything; the
tool_use blocks and the tool_result they got back are what Claude Code recorded.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

DISPATCH = {"Task", "Agent"}


def _blocks(path: pathlib.Path):
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        content = (d.get("message") or {}).get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    yield d.get("type"), block


def facts(projects: pathlib.Path, nested_re: str, blocked_re: str) -> dict[str, str]:
    nested, blocked = re.compile(nested_re), re.compile(blocked_re)
    dispatched = nested_elsewhere = blocked_any = blocked_searched = blocked_dispatched = False
    tools: set[str] = set()
    for path in sorted(projects.rglob("agent-*.jsonl")):
        calls: dict[str, str] = {}      # tool_use id -> name
        errored: dict[str, bool] = {}   # tool_use id -> is_error, for calls that came back
        texts: list[str] = []
        for kind, block in _blocks(path):
            btype = block.get("type")
            if kind == "assistant" and btype == "tool_use":
                calls[str(block.get("id"))] = str(block.get("name", ""))
            elif kind == "assistant" and btype == "text":
                texts.append(str(block.get("text", "")))
            elif kind == "user" and btype == "tool_result":
                errored[str(block.get("tool_use_id"))] = bool(block.get("is_error"))
        names = set(calls.values())
        tools |= names
        dispatches = [i for i, n in calls.items() if n in DISPATCH]
        if any(i in errored and not errored[i] for i in dispatches):
            dispatched = True
        text = "\n".join(texts)
        if nested.search(text) and not dispatches:
            nested_elsewhere = True
        if blocked.search(text):
            blocked_any = True
            blocked_searched |= "ToolSearch" in names
            blocked_dispatched |= bool(dispatches)
    return {
        "dispatched": str(int(dispatched)),
        "nested_elsewhere": str(int(nested_elsewhere)),
        "blocked": str(int(blocked_any)),
        "blocked_searched": str(int(blocked_searched)),
        "blocked_dispatched": str(int(blocked_dispatched)),
        "tools": ",".join(sorted(tools)),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    for key, value in facts(pathlib.Path(argv[1]), argv[2], argv[3]).items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
