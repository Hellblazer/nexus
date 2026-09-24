# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ``behaviour-census`` hook verb (nexus-t9klx).

Port of ``conexus/hooks/scripts/behaviour_census.py``, one of the five
``hooks.json`` entries that still named a bare ``python3`` after RDR-215
had deleted every shell script from the plugin. Stock Windows has no
``python3`` on PATH; on the host of record the only ``python3.exe`` was a
stale uv trampoline that errors before it runs anything. A console-script
verb gets a real ``.exe`` shim from the installer, and rides something the
installer replaces in lockstep instead of whatever happens to be on PATH.

Reports the PREVIOUS session's raw thinking/decision counts at
SessionStart. The reasoning behind every constant below — why raw counts
and never a ratio, why ``Bash`` needs a substring list, why ``isSidechain``
is filtered though it matches nothing today — is carried with them.

**"Move, do not rewrite" (RDR-215 Approach item 9).** The census itself is
carried across unchanged: same constants, same ``Counts``, same
``census``/``project_dir``/``previous_transcripts``. Two changes were
unavoidable:

1. **``main() -> int`` (read stdin, ``print``, exit 0) becomes
   ``run(payload) -> HookResult``.** The payload arrives as an argument
   rather than as JSON on stdin, and the report accumulates into
   ``HookResult.stdout`` instead of printing. ``nexus._hook_runtime.entry``
   writes ``result.stdout + "\n"`` when it is not ``None`` and nothing at
   all when it is, which reproduces the script's ``print`` and its silence
   respectively.

2. **The bare ``except`` around ``main()`` is gone**, because
   ``nexus._hook_runtime._io.never_fail`` is that guard for every verb and
   a second one here would only hide what it swallowed. The script's own
   "failure mode is always print nothing and move on" is unchanged; it is
   now the shared primitive's promise rather than this file's.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from nexus._hook_runtime._io import HookResult

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

def _report(c: Counts) -> str:
    """The census as text. ``print`` per line in the script; one joined
    string here, which ``entry.main`` writes with the same trailing byte.
    """
    lines = [
        "## Session behaviour census (previous session)",
        "",
        REFUSAL,
        "",
        f"- {c.think} thought(s), {c.decisions} state-mutating action(s)",
        f"- {c.dispatch} dispatched (Agent/Task calls), {c.hands_on} performed "
        "by this session directly (the HANDS_ON set)",
    ]
    if c.distances:
        med = _percentile(c.distances, 50.0)
        p25 = _percentile(c.distances, 25.0)
        p75 = _percentile(c.distances, 75.0)
        lines.append(
            f"- tool-call distance from the nearest prior thought "
            f"({len(c.distances)} of {c.decisions} action(s) had one): "
            f"median {med:g}, p25 {p25:g}, p75 {p75:g}"
        )
    if c.ungrounded:
        lines.append(
            f"- {c.ungrounded} action(s) had no prior thought anywhere "
            "earlier in the transcript"
        )
    return "\n".join(lines)


def run(payload: dict | None) -> HookResult:
    """Census the previous session's transcript, or say nothing."""
    data = payload if isinstance(payload, dict) else {}

    pdir = project_dir(data)
    if pdir is None:
        return HookResult()
    prior = previous_transcripts(pdir, data.get("session_id"))
    if not prior:
        return HookResult()

    last = census(prior[0])
    if last.tool_calls == 0:
        # Non-vacuity: a parse that saw nothing is not a session that did
        # nothing. Say so rather than print a 0, which reads identically.
        return HookResult(
            stdout=(
                "## Session behaviour census\n\n"
                f"Could not read `{prior[0].name}`: no tool calls parsed. "
                "Census skipped, not zero."
            )
        )

    return HookResult(stdout=_report(last))
