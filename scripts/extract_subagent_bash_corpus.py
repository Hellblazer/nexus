#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Extract (command, cwd) pairs for SUBAGENT Bash calls from local Claude Code
transcripts (nexus-sby49.1, plugin-portability plan section 7 Phase 0).

Hand script, read-only. It reads ``~/.claude/projects/*/<session>/subagents/*.jsonl``
(the transcripts of Agent-tool subagents; the main-session ``<session>.jsonl``
files are not read) and writes one JSON object per Bash tool_use to the output
file: ``{"command", "cwd", "session", "agent", "ts"}``. The corpus is real
command text from Sam's own sessions: it stays on this box. The default output
directory is outside the repo (``~/.local/state/nexus/plans/corpus/``) and the
script refuses an output path inside a git checkout.

Usage::

    uv run python scripts/extract_subagent_bash_corpus.py [--days 30] [--out FILE]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

DEFAULT_OUT_DIR = Path.home() / ".local" / "state" / "nexus" / "plans" / "corpus"
PROJECTS = Path.home() / ".claude" / "projects"
_CWD_RE = re.compile(r'"cwd":"((?:[^"\\]|\\.)*)"')


def _in_checkout(path: Path) -> bool:
    for parent in [path.resolve(), *path.resolve().parents]:
        if (parent / ".git").exists():
            return True
    return False


def iter_pairs(projects: Path, since: dt.datetime | None):
    """Yield one record per subagent Bash tool_use."""
    for f in sorted(projects.glob("*/*/subagents/*.jsonl")):
        session = f.parent.parent.name
        try:
            fh = f.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            last_cwd: str | None = None  # entries without their own cwd inherit it
            for line in fh:
                m = _CWD_RE.search(line)
                if m:
                    last_cwd = json.loads('"' + m.group(1) + '"')
                if '"Bash"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                msg = d.get("message")
                content = msg.get("content") if isinstance(msg, dict) else None
                if not isinstance(content, list):
                    continue
                ts = d.get("timestamp") or ""
                if since is not None and ts:
                    try:
                        when = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except ValueError:
                        when = None
                    if when is not None and when < since:
                        continue
                for block in content:
                    if (isinstance(block, dict) and block.get("type") == "tool_use"
                            and block.get("name") == "Bash"):
                        cmd = (block.get("input") or {}).get("command")
                        if isinstance(cmd, str):
                            yield {"command": cmd, "cwd": d.get("cwd") or last_cwd,
                                   "session": session, "agent": d.get("agentId"),
                                   "ts": ts}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=30,
                    help="only calls newer than N days (0 = all; default 30)")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--projects", type=Path, default=PROJECTS)
    args = ap.parse_args(argv)

    out = args.out or DEFAULT_OUT_DIR / (
        "subagent-bash-%dd.jsonl" % args.days if args.days else "subagent-bash-all.jsonl")
    out = out.expanduser()
    if _in_checkout(out.parent):
        print(f"refusing: {out} is inside a git checkout; the corpus stays out of repos",
              file=sys.stderr)
        return 2
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
             if args.days else None)

    out.parent.mkdir(parents=True, exist_ok=True)
    pairs = 0
    distinct: set[tuple[str, str | None]] = set()
    cwds: set[str] = set()
    no_cwd = 0
    with out.open("w", encoding="utf-8") as fh:
        for rec in iter_pairs(args.projects, since):
            fh.write(json.dumps(rec) + "\n")
            pairs += 1
            distinct.add((rec["command"], rec["cwd"]))
            if rec["cwd"]:
                cwds.add(rec["cwd"])
            else:
                no_cwd += 1
    exist = sum(1 for c in cwds if Path(c).is_dir())
    print(f"out={out}")
    print(f"window_days={args.days or 'all'}")
    print(f"pairs={pairs} distinct_pairs={len(distinct)} no_cwd={no_cwd}")
    print(f"distinct_cwds={len(cwds)} cwds_existing_on_disk={exist}")
    return 0 if pairs else 1


if __name__ == "__main__":
    raise SystemExit(main())
