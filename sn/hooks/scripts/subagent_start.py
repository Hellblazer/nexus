# SPDX-License-Identifier: AGPL-3.0-or-later
"""sn SubagentStart hook — inject Serena + Context7 guidance into every subagent.

Carries the body of the deleted ``mcp-inject.sh`` (RDR-215 bead
nexus-q02nx.23). What it emits, and why, is unchanged; what went away is
the bash plumbing that got it there.

DELIVERY CONTRACT (Claude Code SubagentStart): the documented JSON envelope
``{"hookSpecificOutput": {"hookEventName": "SubagentStart",
"additionalContext": "<text>"}}``. Plain stdout was the prior shape and once
worked, but the envelope makes the emit intent unambiguous, so a Claude Code
change that tightens parsing will not silently drop the content. The conexus
plugin migrated to it on 2026-05-05 (commit 68854ca); sn missed that
migration and caught up at nexus-t5q2.

The bash built that envelope by redirecting the body's stdout into a
``mktemp`` buffer and wrapping it from an ``EXIT`` trap, so the body could
stay plain ``cat`` calls. That idiom had a real failure mode — when
``mktemp`` failed, the capture no-opped and the sections went to stdout
UNWRAPPED, which is precisely the silent-drop shape the envelope exists to
prevent. Accumulating strings and dumping once removes it: there is no
half-wrapped state to reach.

Section bodies still live in sibling ``.md`` files rather than inline. They
were moved out of heredocs because bash 5.3 feeds a heredoc through an
anonymous pipe that macOS degrades to 512 bytes under memory pressure,
deadlocking any larger body (``tests/hooks/test_heredoc_pipe_budget.py``).
That reason is gone here, but the files are, and the markdown stays
editable as markdown.

Both sections go to every subagent (nexus-jbt5x). The former task-text
heuristic skipped Serena for any prompt containing "investigate", "audit",
"package", "dependency" or "migrate", which is most debugger and developer
briefs; the two together are about 3 KB, cheaper than one subagent
re-deriving a tool name.

The only branch is on the caller's cwd (nexus-ftpk3): a subagent dispatched
with ``isolation: "worktree"`` runs in a LINKED git worktree, and Serena's
shared server would write its edits into the primary checkout
(``worktree_guard.py`` has the history). Such an agent gets the worktree
section FIRST, so the routing table's "prefer Serena for edits" is already
overridden when it is read. Detection is delegated to ``worktree_guard.py``
so this hook and the PreToolUse guard cannot disagree on what a worktree is.

Stdlib only: hooks run under system python with no conexus installed.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _hook_boundary  # noqa: E402 — bundled sibling, resolved by the path insert above
from worktree_guard import cwd_from_payload, is_linked_worktree  # noqa: E402

_HERE = pathlib.Path(__file__).resolve().parent

WORKTREE_SECTION = "worktree-section.md"
SERENA_SECTION = "serena-section.md"
CONTEXT7_SECTION = "context7-section.md"


def _section(name: str) -> str:
    """A section file's text, or '' if it cannot be read.

    Per-file rather than one boundary around the whole build, because the
    bash was per-file too: each section was its own ``cat``, so a missing
    one lost that section and the rest still reached the subagent. A single
    outer boundary would turn one unreadable file into an empty envelope.
    """
    try:
        return (_HERE / name).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[sn-subagent-start] section unavailable: {name}: {exc}", file=sys.stderr)  # noqa: T201 — stderr is this hook's only diagnostic surface
        return ""


def _in_worktree(payload: str) -> bool:
    """Whether the payload's ``cwd`` is a linked worktree; False if undecidable.

    The bash ran this detection through ``python3 ... 2>/dev/null`` and read
    an empty result as "not a worktree", so a detection failure degraded to
    the common case rather than losing the injection. Preserved deliberately:
    without this, a raising detector would reach the outer boundary and the
    subagent would get no sections at all.
    """
    try:
        return is_linked_worktree(cwd_from_payload(payload))
    except Exception as exc:  # noqa: BLE001 — matches the bash's 2>/dev/null degrade-to-false
        print(f"[sn-subagent-start] worktree detection failed: {exc}", file=sys.stderr)  # noqa: T201 — stderr is this hook's only diagnostic surface
        return False


def build(payload: str) -> str:
    """The ``additionalContext`` body for a dispatch carrying *payload*."""
    parts = []
    if _in_worktree(payload):
        parts.append(_section(WORKTREE_SECTION))
    parts.append(_section(SERENA_SECTION))
    parts.append(_section(CONTEXT7_SECTION))
    return "".join(parts)


def main() -> int:
    payload = sys.stdin.read()
    envelope = {
        "hookSpecificOutput": {
            "hookEventName": "SubagentStart",
            "additionalContext": build(payload),
        },
    }
    print(json.dumps(envelope))  # noqa: T201 — stdout IS this hook's delivery channel
    return 0


if __name__ == "__main__":
    sys.exit(_hook_boundary.guard(main, "sn-subagent-start"))
