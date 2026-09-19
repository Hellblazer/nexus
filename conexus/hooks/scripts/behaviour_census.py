#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Report the PREVIOUS session's delegation and deliberation rates at SessionStart.

Measured 2026-09-19 over 25 transcripts of one project: 728 Agent
dispatches against 16,397 hands-on tool calls (4.3% delegation, 23 things
done by hand per handoff), and 105 sequentialthinking calls against 2,905
decision-bearing calls, with only 16.8% of decisions preceded by a thought
within six tool calls. One session dispatched nothing across 486 calls.
Both rates are far below what the project's own CLAUDE.md mandates, and
both are invisible while a session runs, which is why prose alone has never
moved them.

WHY SessionStart AND NOT SessionEnd. ``nexus._session_end_census`` already
runs a capability census at SessionEnd, and its docstring records that the
grandchild's stdio goes to /dev/null before import, so anything printed
there is provably invisible. It also records that a visible parent-side
line was considered and REJECTED on measured cost: a full capability census
re-walks every transcript for the session (~0.21s for a 70-file session,
~0.50s at 88MB), unbounded and data-dependent. That rejection does not
transfer here, and the difference is the whole design: this reads ONE
transcript and counts two things. Measured on the same corpus: 12.5ms for a
6.7MB transcript, 178ms for the largest on disk at 165MB.

WHY POSITIONAL COVERAGE AND NOT A THINKING COUNT. A raw count is satisfied
by a single stub thought, so it would go green while nothing changed. The
positional form asks whether a thought preceded each decision, so the
cheapest way to move it is the behaviour itself.

NO SCHEMA, NO STORE. Deliberately computes from the transcript rather than
reading a stored row: storing would mean new ``capability_census`` columns,
hence a Liquibase changeset, an engine tag, a PITR-fork walk rehearsal and
a paired deploy, to display two percentages.

Failure mode is always "print nothing and move on". Exit code is always 0;
a census must never block a session opening.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HANDS_ON = {"Read", "Edit", "Write", "Grep", "Glob", "Bash", "NotebookEdit"}
DECISION = {"Edit", "Write", "Agent", "NotebookEdit"}
#: Bash is a decision only when it commits, publishes or closes something.
DECISION_BASH = ("git commit", "git push", "git tag", "set-status", "bd close")
#: A thought counts for a decision at most this many tool calls later.
WINDOW = 6
#: Prior transcripts read for the personal baseline, and the wall-clock cap.
BASELINE_MAX_FILES = 10
BASELINE_BUDGET_S = 0.40


class Counts:
    __slots__ = ("think", "dispatch", "hands_on", "decisions", "covered", "tool_calls")

    def __init__(self) -> None:
        self.think = self.dispatch = self.hands_on = 0
        self.decisions = self.covered = self.tool_calls = 0

    def delegation(self) -> float | None:
        total = self.dispatch + self.hands_on
        return 100.0 * self.dispatch / total if total else None

    def coverage(self) -> float | None:
        return 100.0 * self.covered / self.decisions if self.decisions else None


def census(path: Path) -> Counts:
    """Count one transcript. Only the session's OWN calls: a ``isSidechain``
    entry is a subagent's and is never the orchestrator's refusal to delegate."""
    c = Counts()
    recent_thought = False
    since_thought = 0
    with path.open(errors="replace") as fh:
        for line in fh:
            if '"tool_use"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("isSidechain"):
                continue
            for block in (rec.get("message") or {}).get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name") or ""
                base = name.rsplit("__", 1)[-1]
                c.tool_calls += 1
                if "sequentialthinking" in name:
                    c.think += 1
                    recent_thought = True
                    since_thought = 0
                    continue
                if base in HANDS_ON:
                    c.hands_on += 1
                if base == "Agent":
                    c.dispatch += 1
                is_decision = base in DECISION or (
                    base == "Bash"
                    and any(k in ((block.get("input") or {}).get("command") or "") for k in DECISION_BASH)
                )
                if is_decision:
                    c.decisions += 1
                    if recent_thought and since_thought <= WINDOW:
                        c.covered += 1
                    since_thought += 1
                    if since_thought > WINDOW:
                        recent_thought = False
    return c


def project_dir(payload: dict) -> Path | None:
    tp = payload.get("transcript_path")
    if tp:
        parent = Path(tp).expanduser().parent
        if parent.is_dir():
            return parent
    # Fallback only: real hook payloads carry transcript_path. The slug
    # replaces BOTH separators and dots, so /Users/hal.hildebrand/git/nexus
    # becomes -Users-hal-hildebrand-git-nexus; handling only os.sep yields a
    # directory that never exists and a hook that prints nothing forever.
    cwd = payload.get("cwd") or os.getcwd()
    slug = str(Path(cwd).resolve()).replace(os.sep, "-").replace(".", "-")
    guess = Path.home() / ".claude" / "projects" / slug
    return guess if guess.is_dir() else None


def previous_transcripts(pdir: Path, current_id: str | None) -> list[Path]:
    """Newest first, excluding the session now being written."""
    files = [p for p in pdir.glob("*.jsonl") if p.stem != current_id]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def main() -> int:
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001 — a malformed payload must not block a session opening
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    pdir = project_dir(payload)
    if pdir is None:
        return 0
    prior = previous_transcripts(pdir, payload.get("session_id"))
    if not prior:
        return 0

    last = census(prior[0])
    if last.tool_calls == 0:
        # Non-vacuity: a parse that saw nothing is not a session that did
        # nothing. Say so rather than print 0%, which reads identically.
        print("## Session behaviour census\n")
        print(f"Could not read `{prior[0].name}`: no tool calls parsed. Census skipped, not zero.")
        return 0

    base = Counts()
    files_read = 0
    deadline = time.monotonic() + BASELINE_BUDGET_S
    for p in prior[:BASELINE_MAX_FILES]:
        if time.monotonic() > deadline:
            break
        c = census(p)
        if not c.tool_calls:
            continue
        base.dispatch += c.dispatch
        base.hands_on += c.hands_on
        base.decisions += c.decisions
        base.covered += c.covered
        base.think += c.think
        files_read += 1

    dele, cov = last.delegation(), last.coverage()
    print("## Session behaviour census (previous session)\n")
    parts = []
    if dele is not None:
        parts.append(f"delegation **{dele:.1f}%** ({last.dispatch} dispatched, {last.hands_on} by hand)")
    if cov is not None:
        parts.append(f"deliberation **{cov:.1f}%** of {last.decisions} decisions had a prior thought")
    else:
        parts.append(f"deliberation n/a (no decision-bearing calls; {last.think} thoughts)")
    print("- " + "\n- ".join(parts))
    bd, bc = base.delegation(), base.coverage()
    if files_read > 1 and bd is not None:
        cov_txt = f", deliberation {bc:.1f}%" if bc is not None else ""
        print(f"- your last {files_read} sessions: delegation {bd:.1f}%{cov_txt}")
    print(
        "\nA brief states task, deliverable and bar before anyone acts, so delegating "
        "produces deliberation by construction. Both rates measure the same habit."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — last resort: a census never fails a session start
        sys.exit(0)
