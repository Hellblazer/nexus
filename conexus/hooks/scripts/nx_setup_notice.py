#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# /// script
# requires-python = ">=3.12"
# ///
"""Tell the model at session start the truth about Nexus on this machine.

RDR-228 Phase 1 Step 3 (nexus-sv2wx.7) and Phase 2 Step 4 (nexus-sv2wx.17). The
plugin's servers and most of its hooks are programs of the Nexus CLI, and a machine
can have the plugin and no CLI, a CLI that cannot start, or a CLI and no service. This
hook is the one place a session says so. Every other entry that finds no CLI writes
one line and stays out of the way.

It reports the first of eight states that matches, in this order (the record's list,
with Sam's 2026-10-14 state inserted as 2):

1. A setup run is live: its stage.
2. The CLI is found and cannot start (``_cli_lookup.cannot_start``: an interpreter ``uv``
   pruned): the launcher serves the stand-in, and so does this say so. Setup repairs it.
   A last run that failed or was interrupted is added.
3. The CLI resolves and a service is usable: nothing, whatever an earlier run left.
4. A run was interrupted: say so, name the log, say that ``setup`` called again resumes.
5. The last run failed: the failure and the log.
6. No CLI: Nexus is not installed; the one question, with the table of choices. With the
   "not now" marker it asks nothing and names ``/conexus:setup``.
7. The CLI resolves, no usable service, a local service is provisioned: it is down.
8. The CLI resolves, no usable service, none provisioned: none is set up.

4, 5, 7 and 8 are one quiet line with the marker, and 7 and 8 with a recorded CLI-only
choice (below).

The user's choice is honoured (Sam, 2026-10-14: "honour the choice"). The "not now" marker
turns state 6 into one quiet line, as before, and now also states 7 and 8 and the stale
report of a failed or interrupted run (4, 5): one quiet line that names ``/conexus:setup``
(and, for 4 and 5, the log) and asks nothing. A recorded CLI-only choice (the status says
mode ``cli-only``, state ``done``) turns 7 and 8 into the same quiet line, because a service
that is not set up is what the user chose. A live run (1), a CLI that cannot start (2) and a
usable service (3) are not affected. Any other state that suggests a call to ``setup``
says to ask the user first, in the setup tools' own words: states 1, 4 and 5 are
``nx_standin.render`` of ``nx_setup.status_view``, the text ``setup_status`` returns, and
the table is the stand-in's ``QUESTION_TABLE``.

A usable service is the CLI's own definition, ``_endpoint_resolve.endpoint_usable``:
``resolve_service_endpoint()`` returning. A saved address with no token is not usable.

In a remote workspace (``CLAUDE_CODE_REMOTE`` is true) it prints nothing and reads
nothing: a Desktop conversation's loop runs in a cloud workspace whose servers are on
the user's machine, and the hook cannot see them from there (measured 2026-10-13,
nexus-217c3).

It starts no process, opens no socket, makes no network request and writes no file,
its own bytecode cache included. It does not read its payload. Its file reads (the
status file and its error record, the lease) retry a Windows sharing violation for at
most ``HOOK_READ_BUDGET_S`` ALL TOGETHER, so a held file cannot stretch the hook past its
100 ms. Standard output carries the hook's JSON and nothing else, nothing is written
to standard error, and it exits 0 whatever happens: a session start must not be
disturbed by this hook failing.

Standard library only, and no ``nexus`` import: it runs under ``uv tool run python`` on a
machine with no CLI. The commands, the table and the install guide's address are the
stand-in's (``conexus/launch/nx_standin.py``), so the two cannot differ.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # before the sibling imports: no .pyc in the plugin directory

import json  # noqa: E402
import os  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

#: The program whose absence means "no CLI": the one the launcher serves the
#: stand-in for.
PROBE_PROGRAM = "nx-mcp"

#: Claude Code sets this to ``true`` where the session's loop, and so this hook,
#: runs in a remote workspace and not on the user's machine.
REMOTE_ENV = "CLAUDE_CODE_REMOTE"

#: The most time the hook's file reads may spend, ALL TOGETHER, waiting out a Windows
#: sharing violation (the status file, its error record, the lease): 30 ms of the hook's
#: 100. The setup process's own reads wait two seconds; this hook has no such time. A
#: file still held when the budget is spent reads as absent, which is the safe side
#: of the question the hook asks again at the next session start.
#: ``_endpoint_resolve.HOOK_LEASE_RETRY_BUDGET_S`` is the same number
#: (``tests/hooks/test_usable_service_agreement.py`` pins them equal).
HOOK_READ_BUDGET_S = 0.03

#: The siblings, imported by :func:`load_siblings` and not at module scope: a plugin
#: laid out without one of them must end in silence and exit 0, not a traceback.
_cli_lookup: Any = None
_endpoint_resolve: Any = None
nx_setup: Any = None
nx_standin: Any = None


def load_siblings() -> None:
    """Import the plugin parts this hook decides with. Idempotent."""
    global _cli_lookup, _endpoint_resolve, nx_setup, nx_standin  # noqa: PLW0603 -- see the module docstring
    if nx_standin is not None:
        return
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(here)), "launch"))
    sys.path.insert(0, here)
    import _cli_lookup as cli_lookup  # noqa: PLC0415 -- after the sys.path insert
    import _endpoint_resolve as endpoint_resolve  # noqa: PLC0415
    import nx_setup as setup  # noqa: PLC0415
    import nx_standin as standin  # noqa: PLC0415

    _cli_lookup, _endpoint_resolve, nx_setup = cli_lookup, endpoint_resolve, setup
    nx_standin = standin  # last: it is the "loaded" flag


def in_remote_workspace() -> bool:
    return (os.environ.get(REMOTE_ENV) or "").strip().lower() in ("true", "1")


def _remaining(deadline: float | None) -> float:
    """What is left of the hook's read budget at *deadline* (``None``: all of it)."""
    return HOOK_READ_BUDGET_S if deadline is None else max(0.0, deadline - time.monotonic())


def service_usable(config: Path, deadline: float | None = None) -> bool:
    """Whether a Nexus service is usable: the CLI's own resolution, with the lease
    read bounded by what is left of the hook's read budget."""
    load_siblings()
    return bool(_endpoint_resolve.endpoint_usable(config, retry_budget_s=_remaining(deadline)))


# -- the words -----------------------------------------------------------------------------------


def not_installed_text(commands: tuple[str, ...], install_page: str) -> str:
    """State 6: no CLI. Puts the one question."""
    inline = ", then ".join(f"`{command}`" for command in commands)
    return (
        "Nexus is not installed on this machine, or its install is incomplete: the conexus "
        f"plugin is present and the Nexus CLI program it runs (`{PROBE_PROGRAM}`) was not "
        "found, so this session has no Nexus tools beyond the setup tools. The user may want it installed. "
        f"{nx_standin.question()} Setup runs in the background for several minutes; follow it with "
        "setup_status. Afterwards the user reconnects the `nexus` server or starts a new session. "
        "If the user would rather install it themselves, "
        f"they run in a terminal: {inline}. Do not run these yourself unless the user asks. "
        f"Install guide: {install_page}"
    )


def declined_text() -> str:
    """State 6 with the "not now" marker: one line, no question."""
    return (
        "Nexus is not installed on this machine, and the user chose not to install it for now. "
        "Do not raise it unprompted; /conexus:setup installs it if they change their mind."
    )


def quiet_unfinished_text(view: dict[str, Any]) -> str:
    """States 4 and 5 with the "not now" marker: one quiet line, no question."""
    what = nx_standin.LAST_RUN_WORDS.get(str(view.get("state")), "did not end well")
    log = f" The log is at {view['log']}." if view.get("log") else ""
    return (
        f"The last Nexus setup run {what}, and the user chose not to be asked about Nexus for now. "
        f"Do not raise it unprompted; /conexus:setup picks it up again.{log}"
    )


def quiet_down_text() -> str:
    """State 7 with the "not now" marker or a recorded CLI-only choice: one quiet line."""
    return (
        "Nexus is installed and its local service is not running; the user chose to leave it so "
        "(CLI only, or not now). Do not raise it unprompted; /conexus:setup starts the service if they change their mind."
    )


def quiet_no_service_text() -> str:
    """State 8 with the "not now" marker or a recorded CLI-only choice: one quiet line."""
    return (
        "Nexus is installed and no Nexus service is set up, as the user chose (CLI only, or not now). "
        "Do not raise it unprompted; /conexus:setup sets one up if they change their mind."
    )


def chose_cli_only(view: dict[str, Any]) -> bool:
    """Whether the setup record says the user chose the CLI alone and that run finished."""
    return view.get("mode") == "cli-only" and view.get("state") == "done"


def cannot_start_text(reason: str, view: dict[str, Any]) -> str:
    """State 2: the CLI is found and cannot start."""
    text = (
        f"{nx_standin.broken_text(reason)} This session has no Nexus tools beyond the setup tools. "
        f"{nx_standin.BROKEN_REPAIR} The user can also run /conexus:setup. Afterwards the user "
        "reconnects the `nexus` server or starts a new session."
    )
    if view.get("state") in _UNFINISHED:
        words = nx_standin.LAST_RUN_WORDS.get(str(view.get("state")), "did not end well")
        text += f" The last setup run {words}. {nx_standin.render(view)}"
    return text


def service_down_text() -> str:
    """State 7: a local service is provisioned and not running."""
    return (
        "Nexus is installed, but its local service is not running on this machine (it was stopped, "
        "or the machine restarted), so the Nexus tools that need storage will fail. "
        "/conexus:setup starts it; ask the user first, and act only on their answer."
    )


def no_service_text() -> str:
    """State 8: the CLI resolves and no service is set up."""
    return (
        "Nexus is installed, but no Nexus service is set up, so the Nexus tools that need storage will "
        "fail. /conexus:setup sets one up on this machine; ask the user first, and act only on their answer."
    )


#: Status states that mean the last run did not end well, for state 5 (``failed``,
#: ``doctor-failed``, ``unsupported``) and, with ``interrupted``, for state 2's addition.
_FAILURES = ("failed", "doctor-failed", "unsupported")
_UNFINISHED = (*_FAILURES, "interrupted")


# -- the decision --------------------------------------------------------------------------------


def _status(config: Path, deadline: float | None = None) -> dict[str, Any]:
    """The setup directory's view, through the one implementation in ``nx_setup.py``,
    with its reads bounded by what is left of the read budget. Any failure reads as
    no record."""
    try:
        view = nx_setup.status_view(config / "setup", read_budget_s=_remaining(deadline))
    except Exception:  # noqa: BLE001 -- see the module docstring: the hook never fails a session start
        return {}
    return view if isinstance(view, dict) else {}


def decide() -> str | None:
    """The text for the model, or ``None`` for silence."""
    if in_remote_workspace():
        return None
    load_siblings()
    deadline = time.monotonic() + HOOK_READ_BUDGET_S  # after the import: that time is not the reads'
    config = nx_setup.config_dir()
    view = _status(config, deadline)
    state = view.get("state")
    if state == "running" and view.get("live"):  # 1
        return f"A Nexus setup run is live on this machine. {nx_standin.render(view)}"
    exe = _cli_lookup.resolve(PROBE_PROGRAM)
    if exe is not None:
        reason = _cli_lookup.cannot_start(exe)
        if reason is not None:  # 2
            return cannot_start_text(reason, view)
        if service_usable(config, deadline):  # 3
            return None
    not_now = bool(view.get("not_now"))
    if state == "interrupted" or state in _FAILURES:  # 4, 5
        return quiet_unfinished_text(view) if not_now else nx_standin.render(view)
    if exe is None:  # 6
        if not_now:
            return declined_text()
        return not_installed_text(nx_standin.manual_commands(), nx_standin.INSTALL_PAGE)
    quiet = not_now or chose_cli_only(view)
    if nx_setup.provisioned(config):  # 7
        return quiet_down_text() if quiet else service_down_text()
    return quiet_no_service_text() if quiet else no_service_text()  # 8


def main() -> int:
    text = decide()
    if text is None or sys.stdout is None:
        return 0
    out = json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}})
    try:
        sys.stdout.write(out + "\n")
        sys.stdout.flush()
    except OSError:
        # The reader went away. Point fd 1 at the null device so the
        # interpreter's own flush at exit does not report the dead pipe.
        os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 -- see the docstring: exit 0 whatever happens
        pass
    sys.exit(0)
