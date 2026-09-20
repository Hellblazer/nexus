#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Report the PREVIOUS session's raw thinking/decision counts at SessionStart.

Measured 2026-09-19 over 25 transcripts of one project: 105
sequentialthinking calls against 2,905 decision-bearing calls. Both are
invisible while a session runs, which is why prose alone has never moved
them.

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

WHY RAW COUNTS AND NEVER A COMPLIANCE VERDICT. T3 "Design: restoring the
nexus agent tool-guidance layer" (2026-07-31) states the governing
constraint: before mechanizing compliance for any capability, a census must
be able to distinguish "not reached for because forgotten" from "not
reached for because correctly rejected". A tool call absent from a
transcript looks identical whether it was forgotten or correctly rejected,
and this census can never tell those apart -- so it reports raw counts and
explicitly refuses to compute a coverage ratio, a delegation rate, or any
other percentage. An earlier version of this script DID compute one
("deliberation 34.6%") from two defects that both inflated it: a window
counter that advanced only on a decision, not on every tool call (so a
single stub thought could "cover" seven decisions at unbounded distance
from it), and a decision set so narrow (~436 of 4,653 real tool calls) that
most state-mutating Bash calls under the harness's auto mode simply never
counted. Fixing both moved the reported figure from 34.6% to 6.4% on the
same corpus -- a 5.4x swing from bugs, not behaviour, which is exactly the
failure mode a raw count cannot produce: there is no ratio to be wrong
about.

DISPATCH IS A RAW COUNT TOO, NOT ONLY THE RATIO THAT WAS DELETED. "N
dispatched, M performed by this session directly" is exactly as honest as
the thought/decision counts above -- it says nothing about whether M should
have been smaller, only what happened. Only the percentage built from it
("delegation 25.0%") implied a verdict, so only the percentage is gone. An
Agent/Task dispatch is also counted in the decision set below and so also
appears in the distance distribution: the dispatch line answers "how much
was handed off", the distance line answers "how much was deliberated
before it happened", and one call legitimately answers both questions at
once.

NO SCHEMA, NO STORE. Deliberately computes from the transcript rather than
reading a stored row: storing would mean new ``capability_census`` columns,
hence a Liquibase changeset, an engine tag, a PITR-fork walk rehearsal and
a paired deploy, to display a couple of numbers.

Failure mode is always "print nothing and move on". Exit code is always 0;
a census must never block a session opening.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

#: Tool calls this session performed itself rather than dispatching. A raw
#: count paired against `dispatch` below (Agent/Task calls) -- "N
#: dispatched, M by hand" -- with no ratio between them.
HANDS_ON = {"Read", "Edit", "Write", "Grep", "Glob", "Bash", "NotebookEdit"}

#: What counts as a decision: a tool call that changes state outside the
#: transcript itself. Reviewable in ONE place, not scattered predicates.
#:
#: Deliberately INCLUDES: file mutation (Edit/Write/NotebookEdit), agent
#: dispatch (Agent/Task -- hooks.json's own dispatch matcher is
#: "Agent|Task", so both belong here), cross-session communication
#: (SendMessage, mailbox_send), permanent-store writes (memory_put,
#: store_put), and the one tool that halts execution for a human answer
#: (AskUserQuestion).
#:
#: Deliberately EXCLUDES: every read-only tool (Read/Grep/Glob/search/
#: query/hover/...) and plain informational Bash. A census that fires on
#: every tool call stops meaning anything.
DECISION_TOOL_NAMES = {
    "Edit", "Write", "NotebookEdit",
    "Agent", "Task",
    "SendMessage", "mailbox_send",
    "memory_put", "store_put",
    "AskUserQuestion",
}

#: Bash is a decision only when its command contains one of these
#: substrings. Grouped by what each family mutates:
#:   - git history / working tree: commit, push, tag, reset, merge,
#:     rebase, checkout (the repo's own git guard treats this group as
#:     the most destructive one there is)
#:   - review / ship verbs: bd close/update, gh pr create/merge
#:   - build/test dispatch: uv run
#:   - auto-mode shell shapes that mutate a file without ever touching the
#:     Edit tool: ``sed -i`` (in-place edit), ``cat >``/``cat >>``
#:     (redirect writes -- ``cat >`` is a prefix of ``cat >>`` so one entry
#:     covers both, including the common ``cat > file <<'EOF'`` heredoc
#:     shape), and ``tee `` (the same heredoc-write shape via tee).
#: Measured 2026-09-19: under the harness's auto mode, Bash is 82% of all
#: tool calls and the literal ``Edit`` tool appears twice in 4,653 calls,
#: so a decision set that only recognizes ``Edit`` misses nearly every
#: real file mutation.
#: Deliberately EXCLUDES: read-only git (status/log/diff/show) and plain
#: ``cat file`` with no redirect.
DECISION_BASH_SUBSTRINGS = (
    "git commit", "git push", "git tag",
    "git reset", "git merge", "git rebase", "git checkout",
    "bd close", "bd update", "set-status",
    "gh pr create", "gh pr merge",
    "uv run",
    "sed -i", "cat >", "tee ",
)

#: One line, printed on every report: the whole point of this rewrite.
REFUSAL = (
    "This is not a compliance measure: a tool call absent from the "
    "transcript looks identical whether it was forgotten or correctly "
    "rejected. These are raw counts, not a verdict."
)


class Counts:
    __slots__ = ("think", "decisions", "tool_calls", "distances", "ungrounded", "dispatch", "hands_on")

    def __init__(self) -> None:
        self.think = 0
        self.decisions = 0
        self.tool_calls = 0
        #: Agent/Task calls -- handed off, not performed by this session.
        self.dispatch = 0
        #: Calls in HANDS_ON -- performed by this session directly.
        self.hands_on = 0
        #: One entry per decision that had at least one preceding thought
        #: anywhere earlier in the transcript: the tool-call distance
        #: (this call counts as 1) from the NEAREST such thought.
        self.distances: list[int] = []
        #: Decisions with no preceding thought anywhere earlier in the
        #: transcript at all -- excluded from `distances` because there is
        #: no distance to report, not because they don't count.
        self.ungrounded = 0


def _percentile(data: list[int], q: float) -> float:
    """Linear-interpolation percentile of a non-empty list, q in [0, 100]."""
    s = sorted(data)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * (q / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    if lo == hi:
        return float(s[lo])
    return s[lo] * (hi - k) + s[hi] * (k - lo)


def census(path: Path) -> Counts:
    """Count one transcript: thoughts, decisions, and each decision's
    tool-call distance from the nearest PRECEDING thought. Only the
    session's own calls: a ``isSidechain`` entry is a subagent's and is
    never the orchestrator's own decision.

    isSidechain filtering matches ZERO records in real transcripts as of
    2026-09-19 -- subagent work lives in separate transcript files, so a
    session's own transcript never contains a sidechain-marked record.
    Retained anyway as defense-in-depth against a future transcript shape
    where it might, and because a synthetic record shaped this way is
    constructible today (see the accompanying test).
    """
    c = Counts()
    since_thought: int | None = None  # None: no thought seen yet in this transcript
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
                    since_thought = 0
                    continue
                # Every non-thought tool call moves the transcript one step
                # further from the nearest preceding thought -- not just
                # the decisions among them. Counting only on a decision was
                # the shipped bug: it let an unbounded run of intervening
                # calls go unmeasured whenever none of them happened to be
                # a decision.
                if since_thought is not None:
                    since_thought += 1
                if base in ("Agent", "Task"):
                    c.dispatch += 1
                elif base in HANDS_ON:
                    c.hands_on += 1
                command = (block.get("input") or {}).get("command") or ""
                is_decision = base in DECISION_TOOL_NAMES or (
                    base == "Bash" and any(k in command for k in DECISION_BASH_SUBSTRINGS)
                )
                if is_decision:
                    c.decisions += 1
                    if since_thought is None:
                        c.ungrounded += 1
                    else:
                        c.distances.append(since_thought)
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
    """Newest first, excluding the session now being written.

    KNOWN LIMITATION (2026-09-19, not fixed here): globs every ``*.jsonl``
    by mtime with nothing distinguishing a session transcript from a
    subagent's own transcript file, so "the previous session" can in fact
    be an agent's. Out of scope for this raw-counts rewrite.
    """
    files = [p for p in pdir.glob("*.jsonl") if p.stem != current_id]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def _report(c: Counts) -> None:
    print("## Session behaviour census (previous session)\n")
    print(REFUSAL + "\n")
    print(f"- {c.think} thought(s), {c.decisions} state-mutating action(s)")
    print(f"- {c.dispatch} dispatched (Agent/Task calls), {c.hands_on} performed by this session directly (the HANDS_ON set)")
    if c.distances:
        med = _percentile(c.distances, 50.0)
        p25 = _percentile(c.distances, 25.0)
        p75 = _percentile(c.distances, 75.0)
        print(
            f"- tool-call distance from the nearest prior thought "
            f"({len(c.distances)} of {c.decisions} action(s) had one): "
            f"median {med:g}, p25 {p25:g}, p75 {p75:g}"
        )
    if c.ungrounded:
        print(f"- {c.ungrounded} action(s) had no prior thought anywhere earlier in the transcript")


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
        # nothing. Say so rather than print a 0, which reads identically.
        print("## Session behaviour census\n")
        print(f"Could not read `{prior[0].name}`: no tool calls parsed. Census skipped, not zero.")
        return 0

    _report(last)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — last resort: a census never fails a session start
        sys.exit(0)
