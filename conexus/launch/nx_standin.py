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
  not installed and which commands install it, and the server offers two tools,
  ``setup`` and ``setup_status``. The tools' descriptions say what is missing and
  that ``/conexus:setup`` or the ``setup`` tool fixes it, because a Claude Desktop
  conversation is passed a server's tools and not its ``instructions``.
* In place of ``nx-mcp-catalog``: no tools and no instructions, so the notice is
  not given twice.

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
server inside one connection. The launcher decides at each start from one fact,
whether the program resolves, so the next start after an install runs the real
server.

Transport: MCP stdio, newline-delimited JSON-RPC 2.0 on stdin/stdout. It writes
nothing to stderr: Claude Code records every stderr line of a stdio server as an
``error`` entry in its MCP log. Standard library only, and no ``nexus`` import.
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


def manual_commands(platform: str | None = None) -> tuple[str, ...]:
    """The first install for this machine, as README.md and the install page
    give it. On Linux the first command names the CPU build of torch; without
    it uv installs the GPU build on x86-64, 4.3 GB that Nexus never uses."""
    platform = sys.platform if platform is None else platform
    first = "uv tool install conexus --python 3.12"
    if platform.startswith("linux"):
        first += " --torch-backend cpu"
    return (first, "nx self install", "nx init")


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


def instructions(platform: str | None = None) -> str:
    """The server instructions. One sentence, as the record asks."""
    inline = ", then ".join(f"`{command}`" for command in manual_commands(platform))
    return (
        "Nexus is not installed on this machine, so this server has no Nexus tools; to install "
        f"it the user runs {inline} in a terminal and then reconnects this server "
        f"(install guide: {INSTALL_PAGE})."
    )


def setup_text(platform: str | None = None) -> str:
    """What the tools answer when the setup process (``nx_setup.py``) is missing
    from the plugin: the commands, for the user to run."""
    return (
        f"{NOT_INSTALLED}\n\n"
        "The plugin's setup process is missing, so this tool installed nothing. "
        "Show the user the commands below and let them decide. "
        "They are for the user to run in a terminal, in order; do not run them yourself "
        "unless the user asks you to.\n\n"
        + "".join(f"    {command}\n" for command in manual_commands(platform))
        + "\nThe first installs the CLI, the second moves it to its permanent place, and the "
        "third downloads and starts the local service and asks whether to start it at login. "
        f"Together they download more than a gigabyte and take several minutes.{platform_note(platform)} Afterwards the "
        "user reconnects the `nexus` server (in Claude Code: /mcp, the server, Reconnect) or "
        "starts a new session.\n\n"
        f"Commands for each operating system, and troubleshooting: {INSTALL_PAGE}"
    )


# -- the two tools: names, arguments and descriptions ------------------------------------------
#
# These constants, STAGE_WAITS, the refusal and not-now texts, and ``render`` are kept
# equal to ``nexus.mcp.setup_tools`` by tests/mcp/test_setup_tools.py. Edit both.

#: What ``mode`` accepts. A managed service is not offered until its credentials
#: page exists, so no text lists it.
MODES: tuple[str, ...] = ("local", "cli-only", "not-now")

SETUP_TITLE: str = "Set up Nexus"
SETUP_STATUS_TITLE: str = "Nexus setup status"

SETUP_DESCRIPTION: str = (
    "Set up Nexus on this machine: install the Nexus CLI and, if asked, a Nexus service for it to use. "
    "Nexus is missing when no Nexus tool is available in this session, or when Nexus tools fail because no "
    "service is set up. The user can also run /conexus:setup. "
    "ASK THE USER FIRST and act only on the answer. The choices: local (the CLI and a service on this machine, "
    "up to about 1.3 GB to download), cli-only (the CLI alone, up to about 0.7 GB to download; a service can be added later), "
    "not-now (install nothing and stop asking). For local also ask whether the service should start when the "
    "user logs in; autostart is false unless the user says yes. Setup takes several minutes. "
    "This call returns at once with the current stage and the work continues in the background: follow it "
    "with setup_status, and do not call setup again while it runs. When nothing is missing, this starts nothing."
)

SETUP_STATUS_DESCRIPTION: str = (
    "Report how far Nexus setup has got, or that nothing is missing. Nexus is missing when no Nexus tool is "
    "available in this session, or when Nexus tools fail because no service is set up; the setup tool, or the "
    "user running /conexus:setup, fixes it. Read-only. The answer gives the stage, the log path and the next "
    "step. A full setup takes 2 to 8 minutes: the CLI build usually 1 to 3 minutes, the local service 2 to 5. "
    "Check again no sooner than the interval the answer names, and not in a loop: each call can ask the user "
    "for approval. When the answer says setup is done, the user reconnects the nexus server or starts a new "
    "session before the Nexus tools appear."
)

MODE_DESCRIPTION: str = (
    "What the user chose: local, cli-only or not-now. local installs the CLI and a service on this machine; "
    "cli-only installs the CLI alone; not-now installs nothing."
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
    "verify": (30, "under a minute"),
}
_DEFAULT_WAIT: tuple[int, str] = (30, "a minute or so")

CLOUD_REFUSAL: str = (
    "Setting up a managed service is not offered by this build yet. Choose local, cli-only or not-now."
)
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
    if mode == "cloud":
        return CLOUD_REFUSAL
    return f"Unknown mode {mode!r}. Choose local, cli-only or not-now."


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


def _call_script(args: list[str]) -> dict[str, Any]:
    """Run one ``nx_setup.py`` command and return its JSON object."""
    script = setup_script()
    if script is None or not sys.executable:
        raise SetupError(setup_text())
    try:
        done = subprocess.run(  # noqa: S603 -- our own interpreter and the plugin's script
            [sys.executable, script, *args],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_SCRIPT_TIMEOUT_SECONDS, check=False,
            creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupError(f"The setup process could not be run: {exc}.\n\n{setup_text()}") from exc
    lines = [line for line in (done.stdout or "").splitlines() if line.strip()]
    if done.returncode != 0 or not lines:
        raise SetupError(f"The setup process failed (exit {done.returncode}) and printed no answer.\n\n{setup_text()}")
    try:
        view = json.loads(lines[-1])
    except ValueError as exc:
        raise SetupError(f"The setup process printed something other than JSON.\n\n{setup_text()}") from exc
    if not isinstance(view, dict) or "state" not in view:
        raise SetupError(f"The setup process printed an answer with no state.\n\n{setup_text()}")
    return view


def _checked(view: dict[str, Any], *, started: bool | None = None) -> str:
    text = render(view, started=started)
    if view.get("state") == "error":
        raise SetupError(text)
    return text


def run_setup(arguments: Any) -> str:
    """The ``setup`` tool. Raises :class:`SetupError` for a refusal or a failure."""
    if not isinstance(arguments, dict):
        raise SetupError("setup takes an object: mode, and for local optionally autostart.")
    mode = arguments.get("mode")
    if not isinstance(mode, str):
        raise SetupError("setup needs a mode. Choose local, cli-only or not-now.")
    autostart = arguments.get("autostart", False)
    if not isinstance(autostart, bool):
        raise SetupError("autostart must be true or false.")
    if mode not in MODES:
        raise SetupError(mode_refusal(mode))
    if mode == "not-now":
        view = _call_script(["not-now"])
        if view.get("state") == "error":
            return _checked(view)
        return NOT_NOW_TEXT + (("\n" + render(view)) if view.get("live") else "")
    view = _call_script(["start", "--mode", mode] + (["--autostart"] if autostart and mode == "local" else []))
    return _checked(view, started=bool(view.get("started")))


def run_status() -> str:
    """The ``setup_status`` tool."""
    view = _call_script(["status"])
    text = _checked(view)
    if view.get("state") == "none":
        return f"{NOT_INSTALLED}\n{text}"
    return text


class StandIn:
    """What one stand-in server offers: its name, instructions and tools."""

    def __init__(self, name: str, instructions: str, tools: tuple[dict[str, Any], ...]) -> None:
        self.name = name
        self.instructions = instructions
        self.tools = tools


#: The stand-in for each CLI server program.
STAND_INS: dict[str, StandIn] = {
    "nx-mcp": StandIn("nexus-not-installed", instructions(), SETUP_TOOLS),
    "nx-mcp-catalog": StandIn("nexus-catalog-not-installed", "", ()),
}


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
                text = run_setup(arguments if arguments is not None else {}) if name == "setup" else run_status()
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


def main(program: str) -> int:
    """Serve the stand-in for the CLI server *program* on this process's stdio."""
    if sys.stdin is None or sys.stdout is None:  # started with a stream closed
        return 0
    # Text-mode stdio on Windows would translate "\n" to "\r\n" and decode with
    # the ANSI code page; MCP stdio is UTF-8 with bare newlines. Bytes that are
    # not UTF-8 become a parse error for their line, not the end of the server.
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    try:
        return serve(STAND_INS[program])
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "nx-mcp"))
