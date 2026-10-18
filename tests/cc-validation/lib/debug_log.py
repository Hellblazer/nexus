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


# A debug-log record starts on a line that begins with an ISO timestamp, a ``[LEVEL]`` tag, or
# ``Hook``/``Hooks`` (the older, bare shape). A hook command can span lines, so its continuation
# lines belong to the record they follow; they are not record starts.
_RECORD_START = re.compile(r"^(?:\d{4}-\d{2}-\d{2}T\S+ |\[[A-Z]+\] |Hooks?[ :])", re.M)
_HEAD = r"(?:\d{4}-\d{2}-\d{2}T\S+ )?(?:\[[A-Z]+\] )?"
_MAX_COMMAND = 2000


def _records(text: str) -> list[str]:
    starts = [m.start() for m in _RECORD_START.finditer(text)]
    if not starts or starts[0] != 0:
        starts.insert(0, 0)
    return [text[a:b] for a, b in zip(starts, starts[1:] + [len(text)])]


def finished(text: str, event: str, needle: str) -> bool:
    """A ``Hooks: <Event>:<matcher> [<command>] finished with status 0`` record whose command holds
    <needle>. The command is everything up to the ``] finished with status 0`` of its own record, so
    a ']' inside it does not end it."""
    pattern = re.compile(
        rf"{_HEAD}Hooks: {re.escape(event)}[^\[\n]*\[(.{{0,{_MAX_COMMAND}}}?)\] finished with status 0\b", re.S
    )
    return any((m := pattern.match(rec)) is not None and needle in m.group(1) for rec in _records(text))


def context_sizes(text: str, event: str) -> list[int]:
    """The sizes of the non-empty additionalContext accepted from <event> hooks. A record is
    ``Hook <Event> (<command>) provided additionalContext (N chars)``: the command is everything
    between the first '(' and the ``) provided additionalContext (N chars)`` that ends a line of
    the same record, so a ')' inside the command (or a look-alike of the suffix in the middle of
    a line) does not end it, and a record never reads on into the next one."""
    pattern = re.compile(
        rf"{_HEAD}Hook {re.escape(event)} \(.{{0,{_MAX_COMMAND}}}?\) provided additionalContext \(([0-9]+) chars\)[ \t]*$",
        re.S | re.M,
    )
    sizes = []
    for rec in _records(text):
        if (m := pattern.match(rec)) is not None and int(m.group(1)) > 0:
            sizes.append(int(m.group(1)))
    return sizes


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
