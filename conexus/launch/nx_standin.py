#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# /// script
# requires-python = ">=3.12"
# ///
"""The servers ``nx_launch.py`` serves when there is no Nexus CLI.

RDR-228 Phase 1 Step 2 (nexus-sv2wx.6). The plugin's ``nexus`` and
``nexus-catalog`` servers are programs of the CLI package. On a machine with the
plugin and no CLI the launcher serves one of these in their place, so the
application shows a connected server and the model is told the truth.

* In place of ``nx-mcp``: the handshake carries ``instructions`` saying Nexus is
  not installed, the one question with its table of choices, and the commands that
  install it by hand; the server offers two tools, ``setup`` and ``setup_status``.
  The tools' descriptions say what is missing and that ``/conexus:setup`` or the
  ``setup`` tool fixes it, because a Claude Desktop conversation is passed a
  server's tools and not its ``instructions``.
* In place of ``nx-mcp-catalog``: no tools and no instructions, so the notice is
  not given twice.

RDR-228 Phase 2 Step 4 (nexus-sv2wx.17; Sam, 2026-10-14): the launcher serves the
same stand-in when the CLI is FOUND AND CANNOT START (``_cli_lookup.cannot_start``:
an interpreter ``uv`` pruned). It then says so, in the instructions, in both tools'
descriptions and in ``setup_status``: "Nexus is installed but cannot start; setup
repairs it", with the reason. ``setup`` is the same call; its build step does not
skip a CLI that cannot start (``nx_setup.py``).

RDR-228 Phase 2 Step 3 (nexus-sv2wx.16): the two tools do their work. ``setup``
(``mode`` and, for ``local``, ``autostart``) starts ``nx_setup.py``, the file beside
this one, with this process's Python and returns at once with the stage;
``setup_status`` reports the stage, the log path and the next step. Everything about
the setup directory is ``nx_setup.py``'s: this file runs its command line and
formats its one JSON object, and reads and writes no setup file itself. In Phase 1
the tools took no argument, installed nothing and had other descriptions; the names
are the same. The real ``nx-mcp`` registers the same two tools with the same names,
arguments and descriptions (``nexus.mcp.setup_tools``), and
``tests/mcp/test_setup_tools.py`` pins the two copies equal: this file cannot import
``nexus``, so it holds its own.

A stand-in never serves a Nexus tool, and it does not swap itself for the real
server inside one connection. The launcher decides at each start from two facts,
whether the program resolves and, when it does, whether it can start, so the next
start after an install or a repair runs the real server.

When the CLI cannot start, ``setup_status`` asks the plugin's lookup (``_cli_lookup``,
``hooks/scripts``) again at each poll, because the setup record outlives the install it
describes: a ``done`` from an earlier setup says nothing about a Python pruned since.

Transport: MCP stdio, newline-delimited JSON-RPC 2.0 on stdin/stdout. It writes
nothing to stderr: Claude Code records every stderr line of a stdio server as an
``error`` entry in its MCP log. Standard library and the plugin's own ``_cli_lookup``
(also standard library only), and no ``nexus`` import.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

SERVER_VERSION: str = "1.0.0"

#: Protocol revisions this server speaks; the newest is offered when the client
#: asks for one it does not know.
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-06-18", "2025-03-26", "2024-11-05")

#: The site's install guide ("Installing Nexus"), which has the commands per
#: operating system.
INSTALL_PAGE: str = "https://hellblazer.github.io/nexus/"


def manual_commands(platform: str | None = None, mode: str | None = None) -> tuple[str, ...]:
    """The first install for this machine, as README.md and the install page
    give it. On Linux the first command names the CPU build of torch; without
    it uv installs the GPU build on x86-64, 4.3 GB that Nexus never uses. The
    third command sets up the local service; for *mode* ``cloud`` it is the
    wizard that asks for a managed service's address, token and tenant."""
    platform = sys.platform if platform is None else platform
    first = "uv tool install conexus --python 3.12"
    if platform.startswith("linux"):
        first += " --torch-backend cpu"
    return (first, "nx self install", "nx config init" if mode == "cloud" else "nx init")


def platform_note(platform: str | None = None) -> str:
    """What the commands need that the user may not have, or ``""``."""
    platform = sys.platform if platform is None else platform
    if platform.startswith("linux"):
        return (
            " The first command needs uv 0.9.19 or newer; if uv rejects "
            "`--torch-backend`, run `uv self update` first."
        )
    return ""


NOT_INSTALLED: str = (
    "Nexus is not installed on this machine: the conexus plugin is present and the "
    "Nexus CLI it runs is not, so no Nexus tool is available in this session."
)


#: The one question's choices, as the record gives them (RDR-228 "The one question"),
#: with the sizes it measured. The session-start hook (``nx_setup_notice.py``) imports
#: this text, so the hook's message and this server's instructions cannot differ. The
#: Cloud row arrived with the credentials page (nexus-sv2wx.18); a build without that page
#: does not offer it.
QUESTION_TABLE: str = (
    "| Choice | What happens | Download |\n"
    "|---|---|---|\n"
    "| Local | Install the CLI and run a Nexus service on this machine | about 1.3 GB down, 2.4 to 3.1 GB on disk |\n"
    "| Cloud | Install the CLI and connect it to a managed service you have an address, token and tenant for | about 0.7 GB down, 2.1 to 2.3 GB on disk |\n"
    "| CLI only | Install the CLI and decide about a service later | about 0.7 GB down, 2.1 to 2.3 GB on disk |\n"
    "| Not now | Nothing is installed | none |"
)

#: What Local adds to the question, and what a no means.
LOCAL_FOLLOW_UP: str = (
    "With Local, also ask whether the service should start when the user logs in: yes registers a login unit, "
    "and no means the user starts the service again after each reboot."
)

#: What Cloud adds to the question: the token never passes through the conversation.
CLOUD_FOLLOW_UP: str = (
    "With Cloud, setup opens a page in the user's browser where they type the service address, token and tenant themselves: "
    "never ask for the token, and never put it in a message or a tool call. Tell the user to keep that page open until it "
    "says the credentials were accepted."
)

#: Said once to the model wherever the question is put: nothing is installed
#: without the user's answer.
ASK_FIRST: str = "ASK THE USER FIRST, once, and act only on the answer."

#: The words every "installed but cannot start" text opens with (Sam, 2026-10-14).
BROKEN_PHRASE: str = "Nexus is installed but cannot start; setup repairs it"

#: What the repair asks and costs: it builds the CLI again (the work of ``cli-only``).
BROKEN_REPAIR: str = (
    "Setup repairs it by building the CLI again, up to about 0.7 GB to download and several minutes. "
    "ASK THE USER FIRST and act only on the answer: cli-only repairs the CLI, local repairs it and also sets up "
    "or starts a service on this machine (up to about 1.3 GB), cloud repairs it and connects it to a managed "
    "service, not-now leaves it."
)


def question() -> str:
    """The one question for the model to put to the user: the table of choices, the
    Local and Cloud follow-ups and what answering does."""
    return (
        f"{ASK_FIRST} The choices:\n\n{QUESTION_TABLE}\n\n{LOCAL_FOLLOW_UP} {CLOUD_FOLLOW_UP} "
        "Then, with the user's answer, call the setup tool with mode local (autostart true or false), "
        "cloud, cli-only or not-now; the user can also run /conexus:setup."
    )


def broken_text(reason: str) -> str:
    """The opening of every text that reports a found program that cannot start."""
    return f"{BROKEN_PHRASE}. Why it cannot start: {reason}."


#: How the user reconnects this server on each surface. Claude Desktop starts a plugin's server
#: once, when the app starts, and keeps it for every new chat, with no Reconnect command, so only
#: quitting the app and opening it again replaces this stand-in with the real server (nexus-93xx0,
#: measured 2026-10-16).
RECONNECT_HOW: str = (
    "in Claude Code: /mcp, the server, Reconnect, or a new session; in Claude Desktop: quit the app "
    "fully and open it again, because a new chat keeps the running server"
)


def instructions(platform: str | None = None, broken: str | None = None) -> str:
    """The server instructions. With *broken* (the reason a found program cannot
    start), the install is installed-but-broken, not missing."""
    if broken is not None:
        return (
            f"{broken_text(broken)} This server has no Nexus tools until it is repaired. {BROKEN_REPAIR} "
            f"The user can also run /conexus:setup. Afterwards the user reconnects this server ({RECONNECT_HOW})."
        )
    inline = ", then ".join(f"`{command}`" for command in manual_commands(platform))
    return (
        "Nexus is not installed on this machine, so this server has no Nexus tools yet. "
        f"{question()} Setup runs in the background for several minutes; follow it with setup_status, and "
        f"afterwards the user reconnects this server ({RECONNECT_HOW}). To install by hand instead, the user "
        f"runs {inline} in a terminal (install guide: {INSTALL_PAGE})."
    )


#: The one command that repairs an install that cannot start, as ``nx_setup.py``'s build
#: step runs it (there pinned to the plugin's version; here, with no version known, the
#: latest). For the user to run when the setup process is missing from the plugin.
REPAIR_COMMAND: str = "uv tool run --python 3.12 --from conexus nx self install --bootstrap"


def setup_text(platform: str | None = None, broken: str | None = None, mode: str | None = None) -> str:
    """What the tools answer when the setup process (``nx_setup.py``) is missing
    from the plugin: the commands, for the user to run. With *broken* (why a found CLI
    cannot start) the text says Nexus is installed and cannot start, and gives the
    command that repairs it, not the commands of a first install. With *mode* ``cloud``
    the last command is the wizard for a managed service, not the local install."""
    if broken is not None:
        return (
            f"{broken_text(broken)}\n\n"
            "The plugin's setup process is missing, so this tool repaired nothing. "
            "Show the user the command below and let them decide. "
            "It is for the user to run in a terminal; do not run it yourself unless the user asks you to.\n\n"
            f"    {REPAIR_COMMAND}\n\n"
            "It builds the CLI again: up to about 0.7 GB to download and several minutes. Afterwards the user "
            f"reconnects the `nexus` server ({RECONNECT_HOW}).\n\n"
            f"Commands for each operating system, and troubleshooting: {INSTALL_PAGE}"
        )
    if mode == "cloud":
        third = (
            "third asks for the managed service's address, token and tenant (the tenant for a mint credential) and saves them, "
            "and downloads no service. Together they download about 0.7 GB and take several minutes."
        )
    else:
        third = (
            "third downloads and starts the local service and asks whether to start it at login. "
            "Together they download more than a gigabyte and take several minutes."
        )
    return (
        f"{NOT_INSTALLED}\n\n"
        "The plugin's setup process is missing, so this tool installed nothing. "
        "Show the user the commands below and let them decide. "
        "They are for the user to run in a terminal, in order; do not run them yourself "
        "unless the user asks you to.\n\n"
        + "".join(f"    {command}\n" for command in manual_commands(platform, mode))
        + "\nThe first installs the CLI, the second moves it to its permanent place, and the "
        f"{third}{platform_note(platform)} Afterwards the "
        f"user reconnects the `nexus` server ({RECONNECT_HOW}).\n\n"
        f"Commands for each operating system, and troubleshooting: {INSTALL_PAGE}"
    )


# -- the two tools: names, arguments and descriptions ------------------------------------------
#
# These constants, STAGE_WAITS, the refusal and not-now texts, and ``render`` are kept
# equal to ``nexus.mcp.setup_tools`` by tests/mcp/test_setup_tools.py. Edit both.

#: What ``mode`` accepts. ``cloud`` is the managed service, whose credentials page is
#: ``nx_setup.py``'s (nexus-sv2wx.18).
MODES: tuple[str, ...] = ("local", "cloud", "cli-only", "not-now")

SETUP_TITLE: str = "Set up Nexus"
SETUP_STATUS_TITLE: str = "Nexus setup status"

SETUP_DESCRIPTION: str = (
    "Set up Nexus on this machine: install the Nexus CLI and, if asked, a Nexus service for it to use. "
    "Nexus is missing when no Nexus tool is available in this session, or when Nexus tools fail because no "
    "service is set up. The user can also run /conexus:setup. "
    "ASK THE USER FIRST and act only on the answer. The choices: local (the CLI and a service on this machine, "
    "up to about 1.3 GB to download, 2.4 to 3.1 GB on disk), cloud (the CLI and a connection to a managed service the user has an "
    "address, token and tenant for, up to about 0.7 GB to download, 2.1 to 2.3 GB on disk), cli-only (the CLI alone, up to about 0.7 GB to download, 2.1 to 2.3 GB on disk; a service can be added later), "
    "not-now (install nothing and stop asking). For local also ask whether the service should start when the "
    "user logs in; autostart is false unless the user says yes. For cloud, setup opens a page in the user's browser "
    "where they type the service address, token and tenant themselves: never ask for the token, and never put it in a message "
    "or a tool call. Setup takes several minutes. "
    "This call returns at once with the current stage and the work continues in the background: follow it "
    "with setup_status, and do not call setup again while it runs. When nothing is missing, this starts nothing."
)

SETUP_STATUS_DESCRIPTION: str = (
    "Report how far Nexus setup has got, or that nothing is missing. Nexus is missing when no Nexus tool is "
    "available in this session, or when Nexus tools fail because no service is set up; the setup tool, or the "
    "user running /conexus:setup, fixes it. Read-only. The answer gives the stage, the log path and the next "
    "step. A full setup takes 2 to 8 minutes: the CLI build usually 1 to 3 minutes, the local service 2 to 5. "
    "Check again no sooner than the interval the answer names, and not in a loop: each call can ask the user "
    "for approval. When the answer says setup is done, the user reconnects the nexus server before the Nexus "
    "tools appear: in Claude Code with /mcp or a new session; in Claude Desktop the user must quit the app and "
    "open it again, because a new chat keeps the running server."
)

MODE_DESCRIPTION: str = (
    "What the user chose: local, cloud, cli-only or not-now. local installs the CLI and a service on this machine; "
    "cloud installs the CLI and connects it to a managed service, whose address, token and tenant the user types into a page "
    "that opens in their browser; cli-only installs the CLI alone; not-now installs nothing."
)

AUTOSTART_DESCRIPTION: str = (
    "Only for mode local: true if the user said the service should start when they log in; false (the default) "
    "means they start it again after each reboot."
)

#: No ``annotations``: Phase 1 gave none and the real server gives none, so an
#: application that remembered the Phase 1 tool sees the same hints (none) on the
#: Phase 2 one.
SETUP_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "setup",
        "title": SETUP_TITLE,
        "description": SETUP_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "description": MODE_DESCRIPTION},
                "autostart": {"type": "boolean", "default": False, "description": AUTOSTART_DESCRIPTION},
            },
            "required": ["mode"],
        },
    },
    {
        "name": "setup_status",
        "title": SETUP_STATUS_TITLE,
        "description": SETUP_STATUS_DESCRIPTION,
        "inputSchema": {"type": "object", "properties": {}},
    },
)

def setup_tools(broken: str | None = None) -> tuple[dict[str, Any], ...]:
    """The two tools. With *broken* (why a found CLI cannot start), each description
    opens with the installed-but-cannot-start statement and the reason: a Claude
    Desktop conversation is passed the tools and not the server instructions, so
    the descriptions are where its model and user are told. Schemas and titles
    are the normal ones."""
    if broken is None:
        return SETUP_TOOLS
    head = f"{broken_text(broken)} "
    return tuple({**tool, "description": head + tool["description"]} for tool in SETUP_TOOLS)


# -- the words the tools answer with -------------------------------------------------------

#: Seconds to wait before asking again, and how long the stage usually takes, per
#: stage name ``nx_setup.py`` reports.
STAGE_WAITS: dict[str, tuple[int, str]] = {
    "starting": (10, "a few seconds"),
    "build": (60, "1 to 3 minutes"),
    "service": (10, "a few seconds"),
    "service-install": (10, "a few seconds"),
    "service-start": (15, "under a minute"),
    "init": (120, "2 to 5 minutes"),
    "credentials": (30, "as long as the user takes to fill in the page, up to 10 minutes"),
    "verify": (30, "under a minute"),
}
_DEFAULT_WAIT: tuple[int, str] = (30, "a minute or so")

NOT_NOW_TEXT: str = (
    'Recorded "not now": nothing was installed. The record is read by the plugin\'s session-start check, '
    "not by this server, so any note from this server that no service is set up stays: it describes the machine. "
    "The setup tool and /conexus:setup still work if the user changes their mind."
)

_HEADS: dict[str, str] = {
    "done": "Setup is done.",
    "doctor-failed": "Setup finished, but nx doctor reported problems.",
    "failed": "Setup failed.",
    "unsupported": "Setup cannot run on this host.",
    "interrupted": "The last setup run was interrupted.",
    "none": "No setup has run.",
    "error": "Setup could not be started or read.",
}
_DOCTOR_ROWS_SHOWN = 10

#: States where calling setup again would resume the recorded choice.
_RESUMABLE: tuple[str, ...] = ("failed", "interrupted", "doctor-failed")


def _choice_lines(view: dict[str, Any], state: str) -> list[str]:
    """The mode and autostart the last run recorded, so a model that is asked to
    resume does not have to invent them; and for a state that can be resumed, that
    the user is asked first and the same choice is passed."""
    mode = view.get("mode")
    if not mode or state in ("none", "error"):
        return []
    autostart = bool(view.get("autostart"))
    lines = [f"Recorded choice: mode {mode}, autostart {'yes' if autostart else 'no'}."]
    if state in _RESUMABLE:
        again = f"mode {mode}" + (", autostart true" if autostart else "")
        lines.append(f"To resume, ask the user first; if they agree, call setup again with the same choice ({again}).")
    return lines


def mode_refusal(mode: str) -> str:
    """The answer to a ``mode`` the tools do not accept."""
    return f"Unknown mode {mode!r}. Choose local, cloud, cli-only or not-now."


def render(view: dict[str, Any], *, started: bool | None = None) -> str:
    """The text for one ``nx_setup.py`` status object.

    ``started`` is the command's own ``started`` field (``None`` for ``status``).
    A live run also says how long its stage usually takes and the earliest time to
    ask again, which is the model's only guide to polling.
    """
    state = str(view.get("state", "none"))
    stage = view.get("stage")
    mode = view.get("mode")
    refused = view.get("refused")
    lines: list[str] = []
    if refused:
        lines.append(str(refused))
    if state == "running":
        if not refused:
            lines.append(f"Setup started in mode {mode}." if started else f"A setup run is in progress (mode {mode}).")
        lines.append(f"Stage: {stage}.")
    else:
        lines.append(_HEADS.get(state, f"Setup state: {state}."))
        if stage and state not in ("none", "error"):
            lines.append(f"Last stage: {stage}.")
    if view.get("message"):
        lines.append(str(view["message"]))
    lines.extend(_choice_lines(view, state))
    rows = view.get("doctor_failing") or []
    if rows:
        lines.append("Failing nx doctor rows:")
        lines.extend(f"  {row}" for row in rows[:_DOCTOR_ROWS_SHOWN])
    if view.get("next"):
        lines.append(f"Next: {view['next']}")
    if view.get("log"):
        lines.append(f"Log: {view['log']}")
    if state == "running" and (view.get("live") or started):
        seconds, usually = STAGE_WAITS.get(str(stage), _DEFAULT_WAIT)
        lines.append(
            f"Setup keeps running in the background. The {stage} stage usually takes {usually}. "
            f"Check again with setup_status no sooner than {seconds} seconds from now."
        )
    return "\n".join(lines)


# -- starting nx_setup.py ----------------------------------------------------------------------

#: ``CREATE_NO_WINDOW``: the server has no console on Windows.
_CREATE_NO_WINDOW = 0x08000000

#: ``start`` returns when the detached process runs; ``status`` reads one file.
_SCRIPT_TIMEOUT_SECONDS: float = 60.0


class SetupError(Exception):
    """A tool call that fails. The message is for the model."""


def setup_script() -> str | None:
    """``nx_setup.py``, the file beside this one, when it is there."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nx_setup.py")
    return path if os.path.isfile(path) else None


def _call_script(args: list[str], broken: str | None = None, mode: str | None = None) -> dict[str, Any]:
    """Run one ``nx_setup.py`` command and return its JSON object. *broken* is why a
    found CLI cannot start (``None``: it was not found); a failure says which. *mode*
    is the choice being started, so a fallback for ``cloud`` names the cloud wizard."""
    script = setup_script()
    fallback = setup_text(broken=broken, mode=mode)
    if script is None or not sys.executable:
        raise SetupError(fallback)
    try:
        done = subprocess.run(  # noqa: S603 -- our own interpreter and the plugin's script
            [sys.executable, script, *args],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_SCRIPT_TIMEOUT_SECONDS, check=False,
            creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupError(f"The setup process could not be run: {exc}.\n\n{fallback}") from exc
    lines = [line for line in (done.stdout or "").splitlines() if line.strip()]
    if done.returncode != 0 or not lines:
        raise SetupError(f"The setup process failed (exit {done.returncode}) and printed no answer.\n\n{fallback}")
    try:
        view = json.loads(lines[-1])
    except ValueError as exc:
        raise SetupError(f"The setup process printed something other than JSON.\n\n{fallback}") from exc
    if not isinstance(view, dict) or "state" not in view:
        raise SetupError(f"The setup process printed an answer with no state.\n\n{fallback}")
    return view


def _checked(view: dict[str, Any], *, started: bool | None = None) -> str:
    text = render(view, started=started)
    if view.get("state") == "error":
        raise SetupError(text)
    return text


def run_setup(arguments: Any, broken: str | None = None) -> str:
    """The ``setup`` tool. Raises :class:`SetupError` for a refusal or a failure.
    *broken* is why a found CLI cannot start, so a failure to run the setup process
    says "cannot start" and not "not installed"."""
    if not isinstance(arguments, dict):
        raise SetupError("setup takes an object: mode, and for local optionally autostart.")
    mode = arguments.get("mode")
    if not isinstance(mode, str):
        raise SetupError("setup needs a mode. Choose local, cloud, cli-only or not-now.")
    autostart = arguments.get("autostart", False)
    if not isinstance(autostart, bool):
        raise SetupError("autostart must be true or false.")
    if mode not in MODES:
        raise SetupError(mode_refusal(mode))
    if mode == "not-now":
        view = _call_script(["not-now"], broken)
        if view.get("state") == "error":
            return _checked(view)
        return NOT_NOW_TEXT + (("\n" + render(view)) if view.get("live") else "")
    view = _call_script(["start", "--mode", mode] + (["--autostart"] if autostart and mode == "local" else []), broken, mode)
    return _checked(view, started=bool(view.get("started")))


#: The program the launcher judged when it served a "cannot start" stand-in: the lookup is
#: asked about it again at each poll.
CLI_PROGRAM: str = "nx-mcp"

#: How the last setup run ended, in words that fit the sentence "The last setup run ...".
LAST_RUN_WORDS: dict[str, str] = {
    "interrupted": "was interrupted",
    "failed": "failed",
    "doctor-failed": "finished with nx doctor problems",
    "unsupported": "could not run on this host",
}

RECONNECT: str = (
    f"The Nexus CLI can start now: the user reconnects the `nexus` server ({RECONNECT_HOW})."
)


def cli_state() -> tuple[str, str | None]:
    """What the lookup says about the CLI now, asked the way the launcher asked it:
    ``("broken", reason)`` when ``nx-mcp`` resolves and ``cannot_start`` still gives a
    reason, ``("startable", None)`` when it resolves and nothing says it cannot start, and
    ``("missing", None)`` when it no longer resolves. When the lookup cannot be loaded
    (a plugin laid out without it) the answer is ``("broken", None)``: nothing is known
    to have changed, so the launcher's reason stands."""
    scripts = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks", "scripts")
    try:
        sys.dont_write_bytecode = True  # the plugin directory gets no .pyc from this import
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import _cli_lookup  # noqa: PLC0415 -- see the docstring

        exe = _cli_lookup.resolve(CLI_PROGRAM)
        if exe is None:
            return ("missing", None)
        reason = _cli_lookup.cannot_start(exe)
    except Exception:  # noqa: BLE001 -- a status poll never fails because the lookup did
        return ("broken", None)
    return ("broken", reason) if reason is not None else ("startable", None)


def _still_broken(view: dict[str, Any], text: str, reason: str) -> str:
    """The poll's answer while the CLI still cannot start and no run is live: the
    statement and the repair offer, then what the record says, in words that cannot be
    read as a finished repair (a ``done`` on record predates the prune or did not
    repair it)."""
    head = f"{broken_text(reason)} {BROKEN_REPAIR}"
    state = str(view.get("state", "none"))
    if state == "none":
        return f"{head}\n{text}"
    if state == "done":
        mode = f" (mode {view['mode']})" if view.get("mode") else ""
        log = f" Log: {view['log']}" if view.get("log") else ""
        return (
            f"{head}\nA setup run is on record as done{mode}, but the CLI still cannot start, so that run did not "
            f"repair this.{log}"
        )
    words = LAST_RUN_WORDS.get(state)
    if words is None:
        return f"{head}\nThe record of the last setup run follows.\n{text}"
    return f"{head}\nThe last setup run {words}, and the CLI still cannot start.\n{text}"


def _missing(view: dict[str, Any], text: str) -> str:
    """The poll's answer while no CLI resolves and no run is live: "not installed", then
    the record. A ``done`` on record describes an install that is gone now (removed, or
    its tools directory moved), so it is not reported as a finished setup: the one
    question is put again instead."""
    if view.get("state") == "done":
        mode = f" (mode {view['mode']})" if view.get("mode") else ""
        log = f" Log: {view['log']}" if view.get("log") else ""
        return (
            f"{NOT_INSTALLED}\nA setup run is on record as done{mode}, but the Nexus CLI is not found now, so what "
            f"it installed is gone.{log}\n{question()}"
        )
    return f"{NOT_INSTALLED}\n{text}"


def _startable(view: dict[str, Any]) -> str:
    """The poll's answer once the CLI can start and no run is live: the record and the
    reconnect step, once. A ``done`` record's own next step is that step
    (``nx_setup.py`` writes it), so it is kept as it is; any other record's next step was
    written before the CLI could start and is dropped for the reconnect step."""
    if view.get("state") == "done":
        return render(view)
    return f"{render({k: v for k, v in view.items() if k != 'next'})}\n{RECONNECT}"


def run_status(broken: str | None = None) -> str:
    """The ``setup_status`` tool. The stand-in is served until the user reconnects, and
    the record outlives the install it describes, so the lookup is asked again at each
    poll (:func:`cli_state`) and the answer follows what it says now, whichever way the
    launcher found the CLI at start (*broken*: why a found CLI could not start; ``None``:
    it was not found).

    A LIVE run is reported as it is: a poll during the setup or the repair must not offer
    it again. Otherwise: no CLI resolves, "not installed" and the record (a ``done`` is an
    install that is gone, :func:`_missing`); it resolves and cannot start, the statement
    and the repair offer with the record after it (:func:`_still_broken`); it can start,
    the record and the reconnect step (:func:`_startable`)."""
    kind, fresh = cli_state()
    if kind == "broken" and fresh is None:
        # The lookup could not be loaded: nothing is known to have changed since the start.
        kind = "missing" if broken is None else "broken"
    # The fallback when the setup process cannot answer: "not installed" only when no CLI resolves.
    reason = None if kind == "missing" else (fresh or broken)
    view = _call_script(["status"], reason)
    text = _checked(view)
    if view.get("state") == "running":
        return text
    if kind == "missing":
        return _missing(view, text)
    if kind == "startable":
        return _startable(view)
    return _still_broken(view, text, reason or "an unknown reason")


class StandIn:
    """What one stand-in server offers: its name, instructions and tools."""

    def __init__(
        self, name: str, instructions: str, tools: tuple[dict[str, Any], ...], broken: str | None = None,
    ) -> None:
        self.name = name
        self.instructions = instructions
        self.tools = tools
        #: Why the CLI was found and cannot start, or ``None`` when it was not found.
        self.broken = broken


def stand_in(program: str, broken: str | None = None) -> StandIn:
    """The stand-in for the CLI server *program*. *broken* is why the program was
    found and cannot start; ``None`` means it was not found."""
    if program == "nx-mcp":
        name = "nexus-installed-cannot-start" if broken is not None else "nexus-not-installed"
        return StandIn(name, instructions(broken=broken), setup_tools(broken), broken)
    return StandIn("nexus-catalog-not-installed", "", ())


#: The stand-in for each CLI server program, for a program that is not found.
STAND_INS: dict[str, StandIn] = {program: stand_in(program) for program in ("nx-mcp", "nx-mcp-catalog")}


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(server: StandIn, message: Any) -> dict[str, Any] | None:
    """Answer one JSON-RPC message; None for a notification or a response."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "Invalid Request")
    method = message.get("method")
    if "id" not in message or message.get("id") is None:
        return None  # a notification (initialized, cancelled, ...) or a stray response
    request_id = message["id"]
    if not isinstance(method, str):
        return None if ("result" in message or "error" in message) else _error(request_id, -32600, "Invalid Request")
    params = message.get("params") or {}
    match method:
        case "initialize":
            asked = params.get("protocolVersion") if isinstance(params, dict) else None
            version = asked if asked in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
            result: dict[str, Any] = {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": server.name, "version": SERVER_VERSION},
            }
            if server.instructions:
                result["instructions"] = server.instructions
            return _result(request_id, result)
        case "ping":
            return _result(request_id, {})
        case "tools/list":
            return _result(request_id, {"tools": list(server.tools)})
        case "tools/call":
            name = params.get("name") if isinstance(params, dict) else None
            if not any(tool["name"] == name for tool in server.tools):
                return _error(request_id, -32602, f"Unknown tool: {name!r}. Nexus is not installed.")
            arguments = params.get("arguments") if isinstance(params, dict) else None
            try:
                text = run_setup(arguments if arguments is not None else {}, server.broken) if name == "setup" else run_status(server.broken)
            except SetupError as exc:
                return _result(request_id, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
            return _result(request_id, {"content": [{"type": "text", "text": text}]})
        case _:
            return _error(request_id, -32601, f"Method not found: {method}")


def _answer(server: StandIn, line: str) -> dict[str, Any] | None:
    """The reply to one input line. Never raises: an uncaught error would be a
    traceback on standard error and the end of the server."""
    try:
        message = json.loads(line)
    except (ValueError, RecursionError):  # bad JSON, a 5000-digit integer, deep nesting
        return _error(None, -32700, "Parse error")
    try:
        return handle(server, message)
    except Exception:  # noqa: BLE001 -- see the docstring
        request_id = message.get("id") if isinstance(message, dict) else None
        return _error(request_id, -32603, "Internal error")


def serve(server: StandIn, stdin: Any = None, stdout: Any = None) -> int:
    """Read newline-delimited JSON-RPC from stdin until EOF, or until the client
    stops reading."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    for line in stdin:
        if not line.strip():
            continue
        reply = _answer(server, line)
        if reply is None:
            continue
        try:
            # ASCII output: a lone surrogate echoed in an id cannot be encoded, and
            # U+2028 inside a string must not read as a line end.
            stdout.write(json.dumps(reply, allow_nan=False) + "\n")
            stdout.flush()
        except ValueError:  # an id of NaN or Infinity is not JSON
            stdout.write(json.dumps(_error(None, -32600, "Invalid Request")) + "\n")
            stdout.flush()
        except OSError:  # the client went away
            return _client_gone()
    return 0


def _client_gone() -> int:
    """Leave quietly after a failed write: point fd 1 at the null device so the
    interpreter's own flush at exit does not report the dead pipe."""
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
    except OSError:
        pass
    return 0


def main(program: str, broken: str | None = None) -> int:
    """Serve the stand-in for the CLI server *program* on this process's stdio.
    *broken* is why the CLI was found and cannot start (``None``: it was not found)."""
    if sys.stdin is None or sys.stdout is None:  # started with a stream closed
        return 0
    # Text-mode stdio on Windows would translate "\n" to "\r\n" and decode with
    # the ANSI code page; MCP stdio is UTF-8 with bare newlines. Bytes that are
    # not UTF-8 become a parse error for their line, not the end of the server.
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    try:
        return serve(stand_in(program, broken))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "nx-mcp"))
