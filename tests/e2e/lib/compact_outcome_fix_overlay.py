#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The candidate fix for the compaction-outcome gate, as a harness-owned OVERLAY (nothing in
conexus/hooks changes). The gate's `fix` arm adds two hooks beside the real compact_checkpoint.py:

  compact_outcome_fix_overlay.py pre      PreCompact: capture the open work into
                                          <NEXUS_CONFIG_DIR>/compact-checkpoints/<session>.openwork.json
                                          (running background tasks from the transcript, branches with
                                          unmerged commits, unchecked TODO.md items)
  compact_outcome_fix_overlay.py inject   SessionStart:compact: print the list as additionalContext,
                                          ending with the directive that the checkpoint is the
                                          continuation and the session runs it now, without waiting
                                          for the user

This is the behaviour under test, written down so the gate can run it; the spec for the real change
is separate and this gate becomes its acceptance test. Standard library only. Never exits non-zero.

Against today's compact_checkpoint.py (checked 2026-11-19): the real checkpoint now lists the session's running
processes and background work itself ("Running for this session", the identity's work rows) and checks the board
against the facts, so the overlay's task and branch list overlaps it. What the real hooks still do not say is the
directive, and that is the part this overlay adds. The overlay keeps its own file, <session>.openwork.json: the real
hook's prune takes only *.md, *.injected and .tmp-* files, and the overlay's inject deletes only its own file
(tests/hooks/test_compact_outcome_harness.py pins both). It needs nothing from the plugin's sibling modules.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

DIRECTIVE = (
    "This checkpoint is the continuation. The session runs it now, without waiting for the user: "
    "work every open item above until each is done or genuinely blocked, then end your turn. "
    "Do not stop after the item you were last asked about, and do not ask whether to continue."
)
_BG_RE = re.compile(r'Command running in background with ID: (\w+)\. Output is being written to: ([^\s"\\]+)')


def _dir() -> Path:
    return Path(os.environ.get("NEXUS_CONFIG_DIR") or Path.home() / ".config" / "nexus") / "compact-checkpoints"


def _git(cwd: str, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def background_tasks(transcript: str) -> list[dict]:
    """Tasks started with run_in_background and not reported finished later in the transcript."""
    try:
        lines = Path(transcript).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    started: dict[str, str] = {}
    for line in lines:
        for match in _BG_RE.finditer(line):
            started[match.group(1)] = match.group(2).rstrip(".")
    done = set()
    for line in lines:
        if '"type":"user"' not in line.replace(" ", "") and '"type": "user"' not in line:
            continue
        for task_id in started:
            if task_id in line and re.search(r"(completed|exited|was killed|was stopped|failed)", line) \
                    and "Command running in background" not in line:
                done.add(task_id)
    return [{"id": i, "output": p} for i, p in started.items() if i not in done]


def capture(payload: dict) -> dict:
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) and os.path.isdir(payload.get("cwd")) else os.getcwd()
    unmerged = [b for b in _git(cwd, "branch", "--no-merged", "HEAD", "--format=%(refname:short)").split() if b]
    todo: list[str] = []
    try:
        for line in Path(cwd, "TODO.md").read_text(encoding="utf-8").splitlines():
            if re.match(r"\s*[-*]\s+\[ \]", line):
                todo.append(line.strip())
    except OSError:
        pass
    return {"tasks": background_tasks(str(payload.get("transcript_path") or "")), "branches": unmerged, "todo": todo,
            "head_branch": _git(cwd, "rev-parse", "--abbrev-ref", "HEAD").strip()}


def render(work: dict) -> str:
    lines = ["OPEN WORK when this session was compacted (captured before the summary was written):"]
    if work["tasks"]:
        lines.append("- Background tasks still running (check each one's output file before calling anything done):")
        lines += [f"    - task {t['id']}, output: {t['output']}" for t in work["tasks"]]
    if work["branches"]:
        lines.append(f"- Branches with commits not merged into {work.get('head_branch') or 'HEAD'}: " + ", ".join(work["branches"]))
    if work["todo"]:
        lines.append("- TODO.md items still open:")
        lines += [f"    {t}" for t in work["todo"]]
    if len(lines) == 1:
        return ""
    return "\n".join(lines) + "\n\n" + DIRECTIVE + "\n"


def main(argv: list[str]) -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        sid = str(payload.get("session_id") or "")
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", sid):
            return 0
        path = _dir() / f"{sid}.openwork.json"
        if argv and argv[0] == "pre":
            _dir().mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(capture(payload)), encoding="utf-8")
        elif argv and argv[0] == "inject":
            if not path.is_file():
                return 0
            text = render(json.loads(path.read_text(encoding="utf-8")))
            path.unlink()
            if text:
                sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}) + "\n")
    except Exception as exc:  # noqa: BLE001 -- a harness hook never fails the session
        sys.stderr.write(f"compact-outcome overlay: {type(exc).__name__}: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
