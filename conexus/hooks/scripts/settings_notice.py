#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# /// script
# requires-python = ">=3.12"
# ///
"""Tell the user at session start when a conexus settings file has a problem (RDR-259 Decision 4 item 1, nexus-sby49.12).

Two files feed the hooks: a project's tracked ``.nexus/project.json`` and the user's ``conexus.json``. A broken one is not
an error the hooks can show on every call, and a misspelled key (``subagent-git``) changes nothing visible, so this hook is the
one place a session says so. It runs on ``startup``, ``resume`` and ``clear`` and says nothing on ``compact`` or ``fork``, and
nothing at all when there is no problem.

It prints one JSON object with two parts:

* a top-level ``systemMessage``, the user's message. A plugin command hook's SessionStart output shows the user only this field
  in the CLI (measured in nexus-sby49.15, item 1: ``additionalContext`` alone, plain stdout and stderr show nothing). Each line
  names a file, the problem and the value used; a misspelled key carries the reader's "did you mean". Any text a file
  controls (a key name, a rejected value) is already cleaned, quoted and cut to 60 bytes by the reader; the line is cleaned
  and cut again here. Desktop's display of it is an open hand check, so the message ends with the command that prints the same
  list, and the model's line below says where to send a user who did not see it.
* ``additionalContext``, one line for the model: the count, the file names and the pointer. It never carries file text.

The pointer is ``nx settings show`` when the ``nx`` found has a ``settings`` command (it is asked, once, and only when there is
a problem: an older CLI beside a newer plugin is normal, RDR-259 record 36), else the plugin's own copy of the reader run by its
absolute path, which works on a machine with no CLI.

Each problem is also logged once per session (``hook.log``, through ``note_problems``). The message itself is shown on every
start, resume and clear.

Standard library only, no ``nexus`` import: it runs under ``uv tool run python`` on a machine that may have no CLI. A repository
with no settings file starts no process. The settings helper is imported inside a ``try``; without it the hook is silent. It
exits 0 whatever happens: a session start must not be disturbed by this hook failing.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # before the sibling imports: no .pyc in the plugin directory

import json  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The helper reads the two files and the committed copy; a helper that cannot be imported leaves the hook silent.
try:
    from _audience import (
        clean_text as _clean,
        format_problem as _format,
        note_problems as _note,
        settings_problems as _problems,
    )
except BaseException:  # noqa: BLE001 -- the fallbacks are the contract: no problems, nothing to say
    def _clean(text: object, limit: int = 60) -> str:
        return ""

    def _format(problem: object) -> str:
        return ""

    def _note(payload: object, problems: object) -> None:
        return None

    def _problems(payload: object, head: bool = True) -> list:
        return []


#: The ``source`` values a session start speaks on. ``compact`` and ``fork`` are not here.
SOURCES = ("startup", "resume", "clear")

#: Problems printed one to a line; the rest are counted.
MAX_SHOWN = 5

#: The most bytes one problem line, and one file name in the model's line, may take.
LINE_BYTES = 200
NAME_BYTES = 60

#: The most files the model's line names, and the most seconds the question to ``nx`` may take.
MAX_FILES = 4
PROBE_TIMEOUT_S = 8


def _read_payload() -> dict:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError, AttributeError):
        return {}
    return data if isinstance(data, dict) else {}


def _nx() -> "str | None":
    """The ``nx`` program, found the way the plugin's launcher finds it (the generation layout, then PATH off the current
    directory), or None. A project directory may hold a program of that name, so the bare PATH lookup is not used."""
    try:
        import _cli_lookup  # noqa: PLC0415 -- only a session with a problem pays for it

        return _cli_lookup.resolve("nx")
    except BaseException:  # noqa: BLE001
        return None


def _nx_has_settings() -> bool:
    """Whether the installed ``nx`` has the ``settings`` command: ``nx settings --help`` exits 0 (an older CLI exits 2)."""
    exe = _nx()
    if exe is None:
        return False
    try:
        proc = subprocess.run(  # noqa: S603 -- the resolved CLI, a fixed argv, no shell
            [exe, "settings", "--help"], stdin=subprocess.DEVNULL, capture_output=True, timeout=PROBE_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _stdlib_command() -> str:
    """The plugin's own reader, run by its absolute path (works with no CLI)."""
    reader = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_audience.py")
    if " " in reader:
        reader = '"' + reader + '"'
    return ("python" if os.name == "nt" else "python3") + " " + reader + " show"


def _plural(n: int) -> str:
    return f"{n} problem" + ("" if n == 1 else "s")


def _message(problems: list, has_nx: bool) -> str:
    lines = [f"conexus settings: {_plural(len(problems))}"]
    for p in problems[:MAX_SHOWN]:
        lines.append("  " + _clean(_format(p), LINE_BYTES))
    if len(problems) > MAX_SHOWN:
        lines.append(f"  and {len(problems) - MAX_SHOWN} more")
    if has_nx:
        lines.append("Run `nx settings show` for every setting and where it came from, or `nx settings check` to list only the problems.")
    else:
        lines.append("Run `" + _stdlib_command() + "` for every setting and where it came from.")
    return "\n".join(lines)


def _context(problems: list, has_nx: bool) -> str:
    files: list[str] = []
    for p in problems:
        name = _clean(getattr(p, "file", ""), NAME_BYTES)
        if name and name not in files:
            files.append(name)
    named = ", ".join(files[:MAX_FILES]) + (" and more" if len(files) > MAX_FILES else "")
    run = "`nx settings show` (`nx settings check` lists only the problems)" if has_nx else "`" + _stdlib_command() + "`"
    return (
        f"conexus settings: {_plural(len(problems))} in {named}. The user was shown them at session start; "
        f"if they did not see them, tell them to run {run}."
    )


def _output(problems: list, has_nx: bool) -> dict:
    return {
        "systemMessage": _message(problems, has_nx),
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": _context(problems, has_nx)},
    }


def _speaks_on(payload: dict) -> bool:
    source = payload.get("source")
    return not isinstance(source, str) or source in SOURCES


def main() -> int:
    try:
        payload = _read_payload()
        if not _speaks_on(payload) or sys.stdout is None:
            return 0
        problems = _problems(payload)
        if not problems:
            return 0
        _note(payload, problems)
        sys.stdout.write(json.dumps(_output(problems, _nx_has_settings())) + "\n")
        sys.stdout.flush()
    except OSError:
        # The reader went away. Point fd 1 at the null device so the interpreter's own flush at exit does not report the dead pipe.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
        except OSError:
            pass
    except BaseException:  # noqa: BLE001 -- see the docstring: exit 0 whatever happens
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
