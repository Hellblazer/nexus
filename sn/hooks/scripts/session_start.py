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

Stdlib only: hooks run under system python with no conexus installed.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _hook_boundary  # noqa: E402 — bundled sibling, resolved by the path insert above

SECTION = "session-start-section.md"


def main() -> int:
    text = (pathlib.Path(__file__).resolve().parent / SECTION).read_text(encoding="utf-8")
    sys.stdout.write(text)  # write, not print: `cat` added no trailing newline and neither does this
    return 0


if __name__ == "__main__":
    sys.exit(_hook_boundary.guard(main, "sn-session-start"))
