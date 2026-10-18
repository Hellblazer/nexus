#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read Claude Code's --debug-file log for a settings.json command hook (nexus-qsmc5).

    debug_log.py finished <log> <event> <needle>   exit 0 when a <event> hook whose command
                                                   contains <needle> finished with status 0
    debug_log.py context <log> <event>             print, one per line, the size of every
                                                   non-empty additionalContext Claude Code
                                                   accepted from a <event> hook; exit 0 when
                                                   there is at least one

The log records a command hook as ``Hooks: <Event>:<matcher> [<command>] finished with status
0 (Nms)`` and an accepted injection as ``Hook <Event> (<command>) provided additionalContext
(N chars)``. A command may span lines (a printf with a newline in its JSON-decoded text), so
both are matched over the whole file. A hook that runs and prints nothing leaves only the first
line; the second is the proof Claude Code took the text as context.

Stdlib only.
"""
from __future__ import annotations

import pathlib
import re
import sys


def finished(text: str, event: str, needle: str) -> bool:
    pattern = (
        rf"Hooks: {re.escape(event)}[^\[\n]*\[[^\]]*?{re.escape(needle)}[^\]]*\]"
        r" finished with status 0"
    )
    return re.search(pattern, text) is not None


def context_sizes(text: str, event: str) -> list[int]:
    pattern = rf"Hook {re.escape(event)} \([^)]{{0,2000}}\) provided additionalContext \(([1-9][0-9]*) chars\)"
    return [int(m) for m in re.findall(pattern, text)]


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[1] not in {"finished", "context"}:
        print(__doc__, file=sys.stderr)
        return 2
    path = pathlib.Path(argv[2])
    if not path.is_file():
        return 1
    text = path.read_text(encoding="utf-8", errors="replace")
    if argv[1] == "finished":
        return 0 if len(argv) == 5 and finished(text, argv[3], argv[4]) else 1
    sizes = context_sizes(text, argv[3])
    for size in sizes:
        print(size)
    return 0 if sizes else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
