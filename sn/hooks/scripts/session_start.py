# SPDX-License-Identifier: AGPL-3.0-or-later
"""sn SessionStart hook — remind the main conversation about Serena + Context7.

Carries the body of the deleted ``session-start.sh`` (RDR-215 bead
nexus-q02nx.23). SubagentStart injects the full tool signatures; this is the
compact reminder.

Plain text, not a JSON envelope: a SessionStart hook's stdout is taken as
context as it stands, and that is what the bash emitted. Wrapping it in
``hookSpecificOutput`` here would be a rewrite rather than a port.

The one behavioural change is the boundary. ``session-start.sh`` was a bare
``cat`` with no ``2>/dev/null`` and no fallback — the single script in the
whole hook set whose exit code was not unconditionally 0, so a missing
sibling ``session-start-section.md`` failed the event. Now it emits nothing
and says why.

The body lives in a sibling ``.md`` rather than inline for the reason the
bash records: a heredoc past 512 bytes deadlocks under bash 5.3 when macOS
shrinks pipes (``tests/hooks/test_heredoc_pipe_budget.py``). That constraint
does not apply to Python, but the file does, and the markdown stays
editable as markdown.

Second behavioural change (nexus-ebx0s): this hook now also READS stdin, to
record the git working-tree root of this session's startup cwd against its
session_id (``worktree_guard.record_startup_root``), for the PreToolUse
guard's relocated-session check to compare against later. This is a second,
independent purpose bolted onto a script whose primary job is emitting the
reminder text, so it is wrapped in its OWN try/except and never allowed to
cost that primary job: a malformed payload, an undecodable stdin, or a
missing sibling ``worktree_guard.py`` each log to stderr and fall through to
printing the section text regardless. Recording happens ONLY when the
payload's ``source`` field is exactly ``"startup"`` — see
``worktree_guard.record_startup_root``'s docstring for why the other three
SessionStart sources (``resume``/``clear``/``compact``) must not write here.

Stdlib only: hooks run under system python with no conexus installed.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _hook_boundary  # noqa: E402 — bundled sibling, resolved by the path insert above

SECTION = "session-start-section.md"


def _record_root_if_startup() -> None:
    """Best-effort: record this session's Serena root, iff ``source == "startup"``.

    Deliberately self-contained rather than routed through
    ``_hook_boundary.guard``: an exception raised here must never prevent
    the section text below from printing, and ``guard`` only protects the
    EVENT's exit code, not this function's caller continuing past it. Every
    branch that gives up logs to stderr instead of raising, including a
    ``sys.stdin.read()`` that itself raises on undecodable input.
    """
    try:
        raw = sys.stdin.read()
    except Exception as exc:  # noqa: BLE001 — must never block the section text below
        print(f"[sn-session-start] could not read stdin: {exc}", file=sys.stderr)
        return
    try:
        data = json.loads(raw) if raw.strip() else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return
    if data.get("source") != "startup":
        return
    session_id = data.get("session_id")
    cwd = data.get("cwd")
    if not isinstance(session_id, str) or not session_id:
        return
    if not isinstance(cwd, str) or not cwd:
        return
    try:
        from worktree_guard import git_toplevel, record_startup_root
    except Exception as exc:  # noqa: BLE001 — including SyntaxError, which is not an ImportError
        print(f"[sn-session-start] worktree_guard unavailable; not recording Serena root: {exc}",
              file=sys.stderr)
        return
    root = git_toplevel(cwd)
    if root is None:
        print(f"[sn-session-start] cwd {cwd!r} is not inside a git working tree; not recording",
              file=sys.stderr)
        return
    record_startup_root(session_id, root)


def main() -> int:
    _record_root_if_startup()
    text = (pathlib.Path(__file__).resolve().parent / SECTION).read_text(encoding="utf-8")
    sys.stdout.write(text)  # write, not print: `cat` added no trailing newline and neither does this
    return 0


if __name__ == "__main__":
    sys.exit(_hook_boundary.guard(main, "sn-session-start"))
