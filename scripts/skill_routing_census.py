#!/usr/bin/env python3
"""Skill-routing census (bead nexus-dg7i6.1): which skills the model loads.

Baseline instrument for the skill-routing epic (nexus-dg7i6). Reads Claude
Code transcripts read-only and reports, per window, what the model loaded on
its own initiative next to what the user typed and which agents were
dispatched instead. A skill that never loads while its agent is dispatched
hundreds of times is the under-loading pattern of arXiv 2610.18522 (T3
1.11.758); this script is how that is counted before and after a listing
change.

DEFINITIONS
  run          one transcript file. Main = <ROOT>/<session>.jsonl;
               subagent = <ROOT>/<session>/subagents/*.jsonl
  window       a run is in window if its FIRST record timestamp is in
               [start, end). Whole runs only.
  skill call   an assistant tool_use named "Skill"; keyed by input.skill.
               In a main run, records with isSidechain == true are excluded
               (subagent traffic replayed into the session file).
  slash        a user record whose text carries <command-name>/x</command-name>
               (main runs only): the user invoked x, the model did not.
  dispatch     an assistant tool_use named "Agent" or "Task"; keyed by
               input.subagent_type, "(default)" when absent.
  long run     a run with at least --long assistant records (default 50).
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT_DEFAULT = os.path.expanduser("~/.claude/projects/-Users-hal-hildebrand-git-nexus")
DISPATCH_TOOLS = {"Agent", "Task"}


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _user_text(rec: dict) -> str:
    content = rec.get("message", {}).get("content")
    if isinstance(content, str):
        return content
    return " ".join(c.get("text", "") for c in content or [] if isinstance(c, dict))


def scan_run(path: Path, *, main: bool) -> dict | None:
    """Counts for one transcript, or None when it has no timestamp."""
    first = None
    skills: collections.Counter[str] = collections.Counter()
    slash: collections.Counter[str] = collections.Counter()
    dispatch: collections.Counter[str] = collections.Counter()
    turns = 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if first is None and rec.get("timestamp"):
                first = rec["timestamp"]
            if main and rec.get("isSidechain"):
                continue
            kind = rec.get("type")
            if kind == "assistant":
                turns += 1
                for block in rec.get("message", {}).get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    inp = block.get("input") or {}
                    if block.get("name") == "Skill":
                        skills[inp.get("skill", "?")] += 1
                    elif block.get("name") in DISPATCH_TOOLS:
                        dispatch[inp.get("subagent_type") or "(default)"] += 1
            elif kind == "user" and main:
                text = _user_text(rec)
                if "<command-name>" in text:
                    name = text.split("<command-name>", 1)[1].split("</command-name>", 1)[0]
                    slash[name.strip().lstrip("/")] += 1
    if first is None:
        return None
    return {"first": first, "turns": turns, "skills": skills, "slash": slash,
            "dispatch": dispatch}


def census(root: Path, start: datetime, end: datetime, long_turns: int) -> dict:
    buckets = {k: {"runs": 0, "runs_with_skill": 0, "long_runs": 0,
                   "long_runs_without_skill": 0, "skill_calls": collections.Counter(),
                   "slash_commands": collections.Counter(),
                   "agent_dispatches": collections.Counter()}
               for k in ("main", "subagent")}
    paths = [(p, True) for p in sorted(root.glob("*.jsonl"))]
    paths += [(p, False) for p in sorted(root.glob("*/subagents/*.jsonl"))]
    for path, is_main in paths:
        run = scan_run(path, main=is_main)
        if run is None or not (start <= _ts(run["first"]) < end):
            continue
        b = buckets["main" if is_main else "subagent"]
        b["runs"] += 1
        b["runs_with_skill"] += bool(run["skills"])
        if run["turns"] >= long_turns:
            b["long_runs"] += 1
            b["long_runs_without_skill"] += not run["skills"]
        b["skill_calls"].update(run["skills"])
        b["slash_commands"].update(run["slash"])
        b["agent_dispatches"].update(run["dispatch"])
    return {k: {f: (dict(v.most_common()) if isinstance(v, collections.Counter) else v)
                for f, v in b.items()} for k, b in buckets.items()}


def render(result: dict, top: int) -> str:
    lines = []
    for kind in ("main", "subagent"):
        b = result[kind]
        lines.append(f"{kind}: runs={b['runs']} with-skill={b['runs_with_skill']} "
                     f"long={b['long_runs']} long-without-skill={b['long_runs_without_skill']}")
        for field in ("skill_calls", "slash_commands", "agent_dispatches"):
            items = list(b[field].items())
            if not items:
                continue
            lines.append(f"  {field} (total {sum(v for _, v in items)}):")
            lines.extend(f"    {v:5d} {k}" for k, v in items[:top])
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Skill-routing census over Claude Code transcripts.")
    ap.add_argument("--root", default=ROOT_DEFAULT, help="Claude Code project transcript dir")
    ap.add_argument("--start", required=True, help="window start, ISO 8601 with offset")
    ap.add_argument("--end", required=True, help="window end (exclusive), ISO 8601 with offset")
    ap.add_argument("--long", type=int, default=50, help="assistant records for a long run")
    ap.add_argument("--top", type=int, default=30, help="rows per table in text output")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of tables")
    args = ap.parse_args()
    result = census(Path(args.root), _ts(args.start), _ts(args.end), args.long)
    if result["main"]["runs"] + result["subagent"]["runs"] == 0:
        sys.stderr.write(f"no runs in window [{args.start}, {args.end}) under {args.root}\n")
        return 2
    sys.stdout.write((json.dumps(result, indent=2) if args.json else render(result, args.top)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
