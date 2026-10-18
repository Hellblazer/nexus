#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the dispatched subagents did, from their own transcripts (nexus-qsmc5).

    agent_facts.py <projects-dir> <nested-token-ere> <blocked-token-ere>

Reads every ``agent-*.jsonl`` under <projects-dir> and prints key=value lines:

    dispatched=1           some subagent called the Task/Agent tool and that call came back
                           without ``is_error`` (a call that errored, or never returned, is not
                           a dispatch)
    dispatch_ids=A,B       the agent ids the successful dispatches named (the result's
                           ``agentId: <id>`` text, or the entry's toolUseResult.agentId)
    nested_elsewhere=1     the nested token is in the assistant text of the transcript of an
                           agent a successful dispatch started (the file ``agent-<id>.jsonl``,
                           <id> one of dispatch_ids) and that made NO Task/Agent call itself:
                           the leaf agent's own transcript, never the fork's (a fork that was
                           handed the pieces and wrote the token itself, or echoed it, does not
                           count; neither does a token in some other agent's transcript)
    nested_unlinked=1      the token is in a transcript that made no Task/Agent call but is not
                           the transcript of any agent a successful dispatch started
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


_AGENT_ID = re.compile(r"agentId:\s*([A-Za-z0-9]+)")
_PREFIX = "agent-"


def _entries(path: pathlib.Path):
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict):
            yield d


def _blocks(entry: dict):
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                yield entry.get("type"), block


def _text_of(content: object) -> str:
    """A tool_result's content is a string or a list of text blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_text_of(c.get("text") if isinstance(c, dict) else c) for c in content)
    return ""


def _agent_ids(entry: dict, block: dict) -> set[str]:
    """The ids of the agents a tool_result says it started: ``agentId: <id>`` in its text, or the
    entry's toolUseResult / tool_use_result ``agentId``."""
    ids = set(_AGENT_ID.findall(_text_of(block.get("content"))))
    for key in ("toolUseResult", "tool_use_result"):
        meta = entry.get(key)
        if isinstance(meta, dict) and isinstance(meta.get("agentId"), str):
            ids.add(meta["agentId"])
    return ids


def facts(projects: pathlib.Path, nested_re: str, blocked_re: str) -> dict[str, str]:
    nested, blocked = re.compile(nested_re), re.compile(blocked_re)
    blocked_any = blocked_searched = blocked_dispatched = False
    tools: set[str] = set()
    dispatch_ids: set[str] = set()
    dispatched = False
    token_in: list[tuple[str, bool]] = []   # (the transcript's agent id, whether it made a dispatch call)
    for path in sorted(projects.rglob(f"{_PREFIX}*.jsonl")):
        calls: dict[str, str] = {}      # tool_use id -> name
        results: dict[str, tuple[bool, set[str]]] = {}   # tool_use id -> (is_error, agent ids), for calls that came back
        texts: list[str] = []
        for entry in _entries(path):
            for kind, block in _blocks(entry):
                btype = block.get("type")
                if kind == "assistant" and btype == "tool_use":
                    calls[str(block.get("id"))] = str(block.get("name", ""))
                elif kind == "assistant" and btype == "text":
                    texts.append(str(block.get("text", "")))
                elif kind == "user" and btype == "tool_result":
                    results[str(block.get("tool_use_id"))] = (bool(block.get("is_error")), _agent_ids(entry, block))
        names = set(calls.values())
        tools |= names
        dispatches = [i for i, n in calls.items() if n in DISPATCH]
        for i in dispatches:
            if i in results and not results[i][0]:
                dispatched = True
                dispatch_ids |= results[i][1]
        text = "\n".join(texts)
        if nested.search(text):
            token_in.append((path.stem[len(_PREFIX):], bool(dispatches)))
        if blocked.search(text):
            blocked_any = True
            blocked_searched |= "ToolSearch" in names
            blocked_dispatched |= bool(dispatches)
    # the token counts only in the transcript of an agent a successful dispatch started, and only
    # if that agent made no dispatch call of its own (the fork echoing the token is not the leaf)
    nested_elsewhere = any(aid in dispatch_ids and not made_call for aid, made_call in token_in)
    nested_unlinked = any(aid not in dispatch_ids and not made_call for aid, made_call in token_in)
    return {
        "dispatched": str(int(dispatched)),
        "dispatch_ids": ",".join(sorted(dispatch_ids)),
        "nested_elsewhere": str(int(nested_elsewhere)),
        "nested_unlinked": str(int(nested_unlinked)),
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
