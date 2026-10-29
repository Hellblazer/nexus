# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ``setup`` and ``setup_status`` tools of the real ``nx-mcp`` (RDR-228
Phase 2 Step 3).

The same two tools run in a stand-in server when the machine has the plugin and no
CLI (``conexus/launch/nx_standin.py``). The record asks for one definition on both
servers; they cannot share a file (the stand-in runs with no ``nexus`` package), so
the names, arguments, descriptions and the text the tools answer with are kept
twice and pinned equal by ``tests/mcp/test_setup_tools.py``. Change one copy and
that test names the other.

The tools do none of the setup work. They start ``conexus/launch/nx_setup.py``, the
file ``NX_SETUP_SCRIPT`` names (the launcher sets it when it hands off to this
server), with this server's own Python, and read its answer: one JSON object on
standard output per command (``start --mode M [--autostart] [--slim|--no-slim]``, ``status``,
``not-now``). Nothing here reads or writes the setup directory, its lock, its
status file or its log: a second reader of that layout would be a second
implementation of it. A server that was not started by the launcher has no such
variable, starts nothing, and answers with the terminal commands for the same work.

Consent: nothing is installed without a ``setup`` call, and the model is told in the
tool description, and in the server instructions when no service is found, to ask
the user first and to act only on the answer. Both tools stay out of auto-approval
(``tests/hooks/test_permission_request_hooks.py``).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

from nexus.bounded_subprocess import run_bounded

#: The launcher sets this to the path of ``nx_setup.py`` when it hands off to
#: ``nx-mcp``.
SETUP_SCRIPT_ENV: str = "NX_SETUP_SCRIPT"

#: What ``mode`` accepts. ``cloud`` is the managed service, whose credentials page is
#: ``nx_setup.py``'s (nexus-sv2wx.18).
MODES: tuple[str, ...] = ("local", "cloud", "cli-only", "not-now")

#: ``create_no_window`` for a short child of a server that has no console.
_CREATE_NO_WINDOW = 0x08000000

#: How long one ``nx_setup.py`` command may take. ``start`` returns as soon as the
#: detached process is running; ``status`` reads one file.
_SCRIPT_TIMEOUT_SECONDS: float = 60.0

# -- the sizes of the default install (kept equal to the stand-in's copy) ---------------------
#
# The default install includes the PDF stack (nexus-2k66t.4.9, Sam 2026-10-23: install everything
# by default). The figures are measured on 7.89.0 on macOS and Linux, 2026-10-29 (nexus-2k66t.4.10, T2
# nexus/install-size-measurement-2026-10-24, re-measured on 7.89.0 on 2026-10-29); Windows was not measured. tests/test_install_size_rows.py
# pins every place a user chooses an install.

LOCAL_SIZE_UP_TO: str = "about 0.8 GB to download (1.0 GB on Linux), 2.6 to 2.9 GB on disk"
CLI_SIZE_UP_TO: str = "about 0.5 GB to download (0.6 GB on Linux), 1.9 to 2.2 GB on disk"

SIZES_NOTE: str = (
    "These sizes include PDF support (MinerU and torch). They were measured on 7.89.0 on macOS and Linux; "
    "Windows was not measured. They are not a promise."
)
SLIM_LINE: str = (
    "Slim (setup with slim true) leaves PDF support out: about 0.27 GB less to download on macOS, 0.37 GB on Linux, "
    "and 1.0 GB less on disk on macOS and 1.3 GB on Linux. The first PDF indexed then installs it. Offer slim only if the user asks to leave PDF support out."
)

# -- the tools, as the application sees them ----------------------------------------------

SETUP_TITLE: str = "Set up Nexus"
SETUP_STATUS_TITLE: str = "Nexus setup status"

SETUP_DESCRIPTION: str = (
    "Set up Nexus on this machine: install the Nexus CLI and, if asked, a Nexus service for it to use. "
    "Nexus is missing when no Nexus tool is available in this session, or when Nexus tools fail because no "
    "service is set up. The user can also run /conexus:setup. "
    "ASK THE USER FIRST and act only on the answer. The choices: local (the CLI and a service on this machine, "
    f"up to {LOCAL_SIZE_UP_TO}), cloud (the CLI and a connection to a managed service the user has an "
    f"address, token and tenant for, up to {CLI_SIZE_UP_TO}), cli-only (the CLI alone, up to {CLI_SIZE_UP_TO}; a service can be added later), "
    f"not-now (install nothing and stop asking). {SIZES_NOTE} {SLIM_LINE} For local also ask whether the service should start when the "
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

SLIM_DESCRIPTION: str = (
    "true only if the user asked to leave PDF support (MinerU and torch) out of the install. Omit it otherwise: "
    "omitted installs everything, except that it resumes an unfinished slim run as slim. false installs everything even then, "
    "which is the answer for a user who left PDF support out in an earlier run and now wants it. "
    "A slim install fetches PDF support the first time a PDF is indexed."
)

# -- the words the tools answer with (kept equal to the stand-in's copy) ------------------

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
    slim = bool(view.get("slim"))
    lines = [f"Recorded choice: mode {mode}, autostart {'yes' if autostart else 'no'}{', slim' if slim else ''}."]
    if state in _RESUMABLE:
        again = f"mode {mode}" + (", autostart true" if autostart else "") + (", slim true" if slim else "")
        lines.append(f"To resume, ask the user first; if they agree, call setup again with the same choice ({again}).")
        if slim:
            lines.append("To install PDF support instead, ask the user first; if they agree, call setup with the same mode and slim false.")
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


# -- the usable service -------------------------------------------------------------------


def usable_service() -> bool:
    """The record's definition: whether ``resolve_service_endpoint()`` succeeds.

    That is the function every storage client calls, so "usable" here means "a Nexus
    tool that needs storage would get an endpoint and a credential": ``service_url``
    with a token (environment, config file, the local lease, or a mint credential),
    or the host and port halves, or a live local lease. A saved ``service_url`` with
    no resolvable token raises there, and is not usable here. Any exception counts as
    not usable.

    It reads the environment and the config files, and lease discovery may probe the
    recorded local service's ``/health`` on the loopback address; it contacts nothing
    else and waits for nothing (no wait budget).
    """
    try:
        from nexus.db.service_endpoint import resolve_service_endpoint  # noqa: PLC0415 -- the check runs once per call

        resolve_service_endpoint()
    except Exception:  # noqa: BLE001 -- see the docstring
        return False
    return True


def local_service_provisioned() -> bool:
    """Whether ``nx init`` left a local service on this machine
    (``pg_provision.local_service_files_present``). Any exception counts as no."""
    try:
        from nexus.db.pg_provision import local_service_files_present  # noqa: PLC0415 -- the check runs once per call

        return local_service_files_present()
    except Exception:  # noqa: BLE001 -- see the docstring
        return False


def cli_only_chosen() -> bool:
    """Whether the last setup run finished in mode ``cli-only`` (``health.setup_recorded_cli_only``,
    the reader ``nx doctor`` uses). Any exception counts as no."""
    try:
        from nexus.health import setup_recorded_cli_only  # noqa: PLC0415 -- the check runs once per call

        return setup_recorded_cli_only()
    except Exception:  # noqa: BLE001 -- see the docstring
        return False


def no_service_notice() -> str | None:
    """The instructions sentence for a server that finds no usable service, or
    ``None`` when one is usable. Passive, as the record has it: what is missing, what
    fixes it, and that the user decides. A provisioned local service that is not
    running is said as that, as the setup hook says it (its state 7): the install
    sizes would offer the user a download the fix does not make."""
    if usable_service() or cli_only_chosen():
        return None
    if local_service_provisioned():
        return (
            "Nexus is installed, but its local service is not running on this machine (it was stopped, or the "
            "machine restarted), so the Nexus tools that need storage will fail. The setup tool with mode local, "
            "or the user running /conexus:setup, starts it and downloads nothing, and setup_status says what is "
            "already done, so act only on the user's answer."
        )
    return (
        "Nexus has no usable service on this machine: no service address is configured and no local service "
        "is running, so the Nexus tools that need storage will fail. The setup tool, or the user running "
        "/conexus:setup, sets one up, and setup_status says what is already done. Setup installs software "
        f"(a local service: up to {LOCAL_SIZE_UP_TO}; the CLI alone: up to {CLI_SIZE_UP_TO}) or connects a managed service the user has an address, token and tenant for (no service "
        "download; the user types them into a page that opens in their browser), so act only on the user's answer."
    )


# -- the setup process --------------------------------------------------------------------


class SetupError(Exception):
    """A setup command could not be run or answered nonsense. The message is for the
    model and carries the terminal commands."""


#: The only file name the tools will start.
SETUP_SCRIPT_NAME: str = "nx_setup.py"


def setup_script() -> str | None:
    """The path ``NX_SETUP_SCRIPT`` names, when it is an absolute path to an existing
    file called ``nx_setup.py``. The variable is inherited by every child of the
    launcher, so a value that names anything else starts nothing: this server runs
    the plugin's setup process or none."""
    value = os.environ.get(SETUP_SCRIPT_ENV, "").strip()
    if not value or not os.path.isabs(value) or os.path.basename(value) != SETUP_SCRIPT_NAME:
        return None
    real = os.path.realpath(value)
    # A link named nx_setup.py that resolves to some other file is not the script.
    if os.path.basename(real) != SETUP_SCRIPT_NAME or not os.path.isfile(real):
        return None
    return value


def terminal_commands(mode: str) -> str:
    """What the user runs in a terminal for *mode*, for a server that cannot start
    the setup process."""
    if mode == "local":
        return (
            "Setup cannot be started from this server (it was not started by the conexus plugin's launcher), "
            "so nothing was started. In a terminal the user runs `nx init`, which downloads and starts the local "
            "service and asks whether to start it at login; if the service is already set up and only stopped, "
            "`nx daemon service start` starts it."
        )
    if mode == "cloud":
        return (
            "Setup cannot be started from this server (it was not started by the conexus plugin's launcher), "
            "so nothing was started. In a terminal the user runs `nx config init`, which asks for the service "
            "address, the token and, for a mint credential, the tenant, and saves them."
        )
    if mode == "cli-only":
        return "The Nexus CLI is already installed (this server is part of it). There is nothing to install."
    return (
        "Setup cannot be started from this server (it was not started by the conexus plugin's launcher), "
        "so nothing was recorded. Do not ask the user again in this conversation."
    )


_FALLBACK = " Setup can also be done in a terminal: the user runs `nx init`."


def _call_script(script: str, args: list[str]) -> dict[str, Any]:
    """Run one ``nx_setup.py`` command and return its JSON object."""
    try:
        # contained=False: ``start`` detaches a build that must outlive this call. A
        # contained child lives in a job that is closed when the call returns, which
        # on Windows kills what it started and leaves it no way to break away.
        done = run_bounded(
            [sys.executable, script, *args],
            timeout=_SCRIPT_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL, extra_creationflags=_CREATE_NO_WINDOW,
            contained=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupError(f"The setup process could not be run: {exc}.{_FALLBACK}") from exc
    lines = [line for line in (done.stdout or "").splitlines() if line.strip()]
    if done.returncode != 0 or not lines:
        raise SetupError(f"The setup process failed (exit {done.returncode}) and printed no answer.{_FALLBACK}")
    try:
        view = json.loads(lines[-1])
    except ValueError as exc:
        raise SetupError(f"The setup process printed something other than JSON.{_FALLBACK}") from exc
    if not isinstance(view, dict) or "state" not in view:
        raise SetupError(f"The setup process printed an answer with no state.{_FALLBACK}")
    return view


def _checked(view: dict[str, Any], *, started: bool | None = None) -> str:
    text = render(view, started=started)
    if view.get("state") == "error":
        raise SetupError(text)
    return text


#: setup_status when the last run finished, no service answers, and a local service is
#: provisioned: it was stopped, or the machine restarted with no start at login.
SERVICE_DOWN: str = (
    "No usable Nexus service was found on this machine: the Nexus CLI and a local Nexus service are "
    "installed, but the service is not running, so the Nexus tools that need storage fail.\n"
    "The setup tool with mode local starts it and downloads nothing; with autostart true it also "
    "makes it start at login. Ask the user whether to start it and whether it should start at login, "
    "then call setup with mode local and their answer."
)

NOTHING_MISSING: str = (
    "Nothing is missing: the Nexus CLI is installed and a Nexus service is set up. Setup started nothing."
)


#: Last-run states that mean a run did not finish cleanly. They are reported even
#: when a service answers: the log and the failing rows are what the user needs.
_TROUBLE: tuple[str, ...] = ("doctor-failed", "failed", "interrupted", "unsupported")


def _report_for_usable_service(view: dict[str, Any]) -> str | None:
    """What to say, besides "nothing is missing", when a service answers: a live
    run (the service answers before the last step ends) or a run that went wrong."""
    if view.get("live") and view.get("state") == "running":
        return render(view)
    if view.get("state") in _TROUBLE:
        # setup starts nothing while a service answers, so neither the recorded
        # choice's resume line nor the script's "call setup again" next step may
        # send the model back to it: the log and nx doctor are the way on.
        quiet = {
            **view,
            "mode": None,
            "next": "Setup will not start again while a service answers. Read the log, and run "
            "`nx doctor` in a terminal to see what is wrong.",
        }
        return "A Nexus service answers on this machine, but the last setup run did not finish cleanly.\n" + render(quiet)
    return None


def _slim_args(slim: bool | None) -> list[str]:
    """The script's flag for the tri-state *slim* (nexus-w8ug5): omitted passes nothing, so the script
    inherits an unfinished slim run's choice; false is ``--no-slim``, which does not."""
    return ["--slim"] if slim else ["--no-slim"] if slim is False else []


def run_setup(mode: str, autostart: bool, slim: bool | None = None) -> str:
    """The ``setup`` tool. Raises :class:`SetupError` for a refusal or a failure."""
    if mode not in MODES:
        raise SetupError(mode_refusal(mode))
    script = setup_script()
    if usable_service():
        if script is not None:
            try:
                view = _call_script(script, ["status"])
            except SetupError:
                view = {}
            report = _report_for_usable_service(view)
            if report is not None:
                return report + ("" if view.get("state") == "running" else "\nThis call started nothing: a service already answers.")
        return NOTHING_MISSING
    if script is None:
        return terminal_commands(mode)
    if mode == "not-now":
        view = _call_script(script, ["not-now"])
        if view.get("state") == "error":
            return _checked(view)
        return NOT_NOW_TEXT + (("\n" + render(view)) if view.get("live") else "")
    args = ["start", "--mode", mode] + (["--autostart"] if autostart and mode == "local" else []) + _slim_args(slim)
    view = _call_script(script, args)
    return _checked(view, started=bool(view.get("started")))


def run_status() -> str:
    """The ``setup_status`` tool. Raises :class:`SetupError` when the setup process
    cannot be read and a service is missing."""
    script = setup_script()
    usable = usable_service()
    view: dict[str, Any] | None = None
    if script is not None:
        try:
            view = _call_script(script, ["status"])
        except SetupError:
            if not usable:
                raise
    if usable:
        report = _report_for_usable_service(view) if view is not None else None
        return report if report is not None else NOTHING_MISSING
    if view is None:
        return (
            "No usable Nexus service was found. " + terminal_commands("local")
            + " With a setup process, the setup tool does the same after asking the user."
        )
    if view.get("live") and view.get("state") == "running":
        return render(view)
    rows = view.get("doctor_failing")
    unnamed_doctor_failure = view.get("state") == "doctor-failed" and not (isinstance(rows, list) and rows)
    if (view.get("state") == "done" or unnamed_doctor_failure) and local_service_provisioned():
        # The last run finished, so its "done ... reconnect" is history: what is missing
        # now is a running service (nexus-sv2wx.24, row 2), and setup starts one. A doctor-failed
        # record that names no failing row is the same (nexus-36qyh): a 7.82.0 run on a box with no
        # git recorded none, and the verify step's failure cannot be told from that one tool alone,
        # so it does not send the user to a terminal while the installed service is down. A record
        # that names any row keeps its state below.
        return SERVICE_DOWN + (f"\nLog: {view['log']}" if view.get("log") else "")
    return "No usable Nexus service was found on this machine.\n" + _checked(view)
