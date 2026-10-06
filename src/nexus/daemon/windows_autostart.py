# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Windows autostart: a per-user Task Scheduler logon task and its launcher.

RDR-224 Phase 3 Step 3 (nexus-f9bgu.23). The macOS and Linux units exec
``nx daemon service start --foreground`` and let launchd/systemd watch that
process. Windows cannot do the same, for two reasons measured on a real box
(T2 ``nexus_rdr/224-f9bgu23-autostart``):

1. The stop channel needs the supervisor to own a hidden console in its own
   process group (``CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW``, nexus-f9bgu.17).
   A task that runs ``nx ... --foreground`` makes the TASK's process the
   supervisor, and nothing on that path sets those flags, so ``CTRL_BREAK``
   cannot reach it.
2. The Task Scheduler setting "restart on failure" does NOT restart a task
   whose process exits non-zero. Measured on Windows 11 build 26200 with exit
   codes 1, 3, 255, ``0x80070005``, ``0xC0000005`` and ``0xFFFFFFFF``, started
   by ``schtasks /Run`` and by a time trigger: no restart for any of them. It
   restarts only when the task fails to LAUNCH (an action whose executable is
   missing was restarted at the configured interval). So the launcher cannot
   hand "the supervisor died" to the Task Scheduler by exiting non-zero.

The shape that satisfies both: the task runs this module under ``pythonw``
(no window). The module is a small launcher that spawns the supervisor with
the stop-channel flags, WAITS for it, and applies launchd's
``KeepAlive/SuccessfulExit=false`` rule itself: exit 0 (a clean stop, or a
stand-down because another supervisor owns the lease) ends the launcher and
the task; any other exit is respawned after a throttle. The task's own
``RestartOnFailure`` stays configured and covers what it really covers, a
launch failure of the launcher.

Task Scheduler runs the task process in a job object with
``KILL_ON_JOB_CLOSE | SILENT_BREAKAWAY_OK`` (0x3000, measured). Children of the
launcher break away silently: the supervisor, its engine and PostgreSQL all
survive ``schtasks /End`` and the launcher's own exit (measured, with and
without ``CREATE_BREAKAWAY_FROM_JOB``).

Pure library code apart from :func:`main`; every platform seam is injectable so
both the render and the launcher loop run on every host.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path, PureWindowsPath

import structlog

_log = structlog.get_logger(__name__)

#: The task's name, at the root of the Task Scheduler library (a folder would
#: leave an empty folder behind after ``schtasks /Delete``, which cannot remove
#: one). One per user: the logged-on user's tasks are that user's own.
TASK_NAME = "NexusStorageService"

#: The kept copy of the task definition. Content-compared with the render by
#: the drift checks, exactly as the launchd plist and systemd unit are.
TASK_FILENAME = "nexus-service-task.xml"

#: ``python -m`` target of the launcher the task runs.
LAUNCHER_MODULE = "nexus.daemon.windows_autostart"

#: Seconds the launcher waits before it respawns a supervisor that exited
#: non-zero. launchd's ``ThrottleInterval`` for the same unit is 30.
RESTART_THROTTLE_S = 30.0

#: Task Scheduler's own restart settings. They cover a launch failure only (see
#: the module docstring); 999 is the schema's ceiling for the count and one
#: minute is its floor for the interval.
TASK_RESTART_INTERVAL = "PT1M"
TASK_RESTART_COUNT = 999

_TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def _xml_text(value: str) -> str:
    """Escape *value* for XML text and attribute-free element content, ASCII only.

    Non-ASCII characters become numeric character references, so the rendered
    document is pure ASCII: ``Path.read_text()`` and ``Path.write_text()`` agree
    on it under any locale, and the file needs no encoding declaration (a
    ``UTF-8`` declaration is REFUSED by ``schtasks /Create /XML`` with "unable to
    switch the encoding", measured; an undeclared UTF-8/ASCII file is accepted).
    """
    escaped = (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return escaped.encode("ascii", "xmlcharrefreplace").decode("ascii")


def launcher_arguments(config_dir: str) -> str:
    """The command-line arguments of the launcher the task runs (no executable)."""
    return subprocess.list2cmdline(["-m", LAUNCHER_MODULE, "--config-dir", config_dir])


def task_xml(*, sid: str, pythonw: str, config_dir: str) -> str:
    """The task definition ``schtasks /Create /XML`` imports.

    Logon trigger for exactly this user (*sid*), and a principal with
    ``InteractiveToken`` (the XML spelling of ``/IT``: runs only while the user
    is logged on, in their own session, with no stored password) and
    ``LeastPrivilege`` (no elevation, so no administrator is needed to register
    it; registered and run from a Medium-integrity token on the measuring box).

    ``DisallowStartIfOnBatteries`` and ``StopIfGoingOnBatteries`` are false
    because the ``schtasks`` defaults are true: a laptop on battery would never
    start the service or would kill it. ``ExecutionTimeLimit`` is ``PT0S`` (no
    limit; the default is 72 hours). ``Hidden`` stays false: it hides the task
    from the Task Scheduler UI, not a window, and a user must be able to find
    the task to disable it. The window is hidden by ``pythonw`` here and by
    ``CREATE_NO_WINDOW`` on the supervisor. ``IgnoreNew`` keeps a second logon
    event or ``schtasks /Run`` from starting a second launcher.
    """
    sid_x = _xml_text(sid)
    return (
        '<?xml version="1.0"?>\n'
        f'<Task version="1.4" xmlns="{_TASK_NS}">\n'
        "  <RegistrationInfo>\n"
        "    <Description>Nexus storage service: starts the engine and PostgreSQL "
        "supervisor at logon (nx daemon service install --autostart)</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        f"      <UserId>{sid_x}</UserId>\n"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        f"      <UserId>{sid_x}</UserId>\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>false</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <Priority>7</Priority>\n"
        "    <RestartOnFailure>\n"
        f"      <Interval>{TASK_RESTART_INTERVAL}</Interval>\n"
        f"      <Count>{TASK_RESTART_COUNT}</Count>\n"
        "    </RestartOnFailure>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{_xml_text(pythonw)}</Command>\n"
        f"      <Arguments>{_xml_text(launcher_arguments(config_dir))}</Arguments>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


_ENABLED_RE = re.compile(r"<Settings>.*?<Enabled>\s*(true|false)\s*</Enabled>", re.DOTALL | re.IGNORECASE)


def task_enabled(query_xml: str) -> bool:
    """Whether the ``schtasks /Query /XML`` output says the task is enabled.

    ``schtasks`` normalises the document it stores (element order, an expanded
    ``UserId``), so the kept file is the drift reference, not this output. The
    schema default for a missing ``Enabled`` is true.
    """
    match = _ENABLED_RE.search(query_xml)
    return True if match is None else match.group(1).lower() == "true"


def pythonw_for(python_exe: str, *, exists: Callable[[str], bool] = os.path.exists) -> str:
    """The windowless interpreter next to *python_exe*, else *python_exe*.

    *exists* is a test seam.
    """
    path = PureWindowsPath(python_exe)
    if path.name.lower() == "pythonw.exe":
        return str(path)
    candidate = str(path.with_name("pythonw.exe"))
    return candidate if exists(candidate) else str(path)


def console_python_for(python_exe: str) -> str:
    """The console interpreter next to *python_exe* (``pythonw.exe`` -> ``python.exe``).

    The supervisor needs a console subsystem process: ``CREATE_NO_WINDOW`` gives
    it a hidden console, and that console is what ``nx daemon service stop``
    attaches to. A ``pythonw`` supervisor has no console and could not be reached.
    """
    path = PureWindowsPath(python_exe)
    if path.name.lower() == "pythonw.exe":
        return str(path.with_name("python.exe"))
    return str(path)


def _supervise_once(
    config_dir: Path,
    *,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    platform: str | None = None,
) -> int:
    """Spawn the supervisor with the stop-channel flags and wait for its exit code.

    *popen* and *platform* are test seams; production passes neither.
    """
    from nexus.commands.daemon import (  # noqa: PLC0415 — deferred: heavy import, runs once per spawn in a long-lived launcher
        _supervisor_argv,
        _supervisor_popen_kwargs,
    )
    from nexus.logging_setup import open_child_log_or_devnull  # noqa: PLC0415 — deferred, same reason

    argv = _supervisor_argv(
        config_dir, nx_bin=[console_python_for(sys.executable), "-m", "nexus.cli"]
    )
    spawn_log = open_child_log_or_devnull("storage_service.crash", config_dir)
    try:
        proc = popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=spawn_log,
            stderr=spawn_log,
            **_supervisor_popen_kwargs(platform),
        )
    finally:
        if not isinstance(spawn_log, int):
            spawn_log.close()
    _log.info("windows_autostart_supervisor_spawned", pid=proc.pid, argv=argv)
    return proc.wait()


def _stop_requested_since(config_dir: Path) -> Callable[[float], bool]:
    """The default stop check: a marker for this user's storage service that is
    not older than the given time. A check that cannot run is "no stop"."""

    def check(since: float) -> bool:
        from nexus.daemon.service_registry import (  # noqa: PLC0415 — deferred: the launcher's heavy imports load once, at the first check
            service_identity,
            stop_requested_since,
        )

        return stop_requested_since(config_dir, "storage_service", service_identity(), since)

    return check


def run_launcher(
    config_dir: Path,
    *,
    supervise: Callable[[Path], int] = _supervise_once,
    sleep: Callable[[float], None] = time.sleep,
    throttle_s: float = RESTART_THROTTLE_S,
    clock: Callable[[], float] = time.time,
    stop_requested: Callable[[float], bool] | None = None,
) -> int:
    """Run the supervisor until it exits 0, respawning it after any other exit.

    A deliberate stop wins over the respawn (RDR-224, nexus-f9bgu.33): ``nx daemon
    service stop`` writes a stop marker before it signals, and the launcher neither
    spawns nor respawns while the marker is not older than its last spawn. That
    covers the three paths where a stop is not an exit 0: the hard-kill fallback
    (exit 1), a break that lands before the supervisor's handler is installed
    (exit ``0xC000013A``), and a stop that arrives while the launcher sleeps its
    throttle. A marker older than this launcher's own start is a previous
    session's stop and is ignored. *clock* and *stop_requested* are test seams;
    a stop check that raises counts as no stop.

    launchd's ``KeepAlive/SuccessfulExit=false`` rule, applied here because the
    Task Scheduler cannot apply it (module docstring). Exit 0 is the supervisor's
    "deliberate stand-down" code: a clean ``nx daemon service stop``, or another
    supervisor already owning the lease. Everything else (a crash, exit 2
    before ``nx init`` has provisioned credentials, exit 3 when the engine
    died, exit 4 when PostgreSQL did, a hard kill) is respawned after
    *throttle_s*, never given up on, as launchd and the systemd unit never give
    up. A spawn that raises ``OSError`` is treated the same way, so a missing
    interpreter at logon heals when it appears.
    """
    check = stop_requested if stop_requested is not None else _stop_requested_since(config_dir)

    def stop_wanted(since: float) -> bool:
        try:
            return check(since)
        except Exception as exc:  # noqa: BLE001 — a check that cannot run is "no stop", never a dead launcher
            _log.warning("windows_autostart_stop_check_failed", error=str(exc))
            return False

    last_spawn = clock()
    while True:
        if stop_wanted(last_spawn):
            _log.info("windows_autostart_stop_marker_honoured", where="before_spawn")
            return 0
        last_spawn = clock()
        try:
            code = supervise(config_dir)
        except OSError as exc:
            _log.error("windows_autostart_spawn_failed", error=str(exc))
        else:
            _log.info("windows_autostart_supervisor_exited", exit_code=code)
            if code == 0:
                return 0
            if stop_wanted(last_spawn):
                _log.info("windows_autostart_stop_marker_honoured", where="after_exit", exit_code=code)
                return 0
        sleep(throttle_s)


def _redirect_missing_stdio(config_dir: Path) -> None:
    """Under ``pythonw`` ``sys.stdout`` and ``sys.stderr`` are ``None``; point them at a file.

    Anything that writes to them (a stray ``print``, a logging handler that
    probes ``isatty()``) would otherwise raise. The file is the launcher's crash
    channel, the same role ``storage_service.crash.log`` plays for the supervisor.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    logs_dir = config_dir / "logs"
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        stream = open(logs_dir / "windows_autostart.crash.log", "a", buffering=1)  # noqa: SIM115 — lives as long as the process
    except OSError:
        stream = open(os.devnull, "w")  # noqa: SIM115 — lives as long as the process
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of ``python -m nexus.daemon.windows_autostart --config-dir <dir>``."""
    parser = argparse.ArgumentParser(prog=LAUNCHER_MODULE)
    parser.add_argument("--config-dir", required=True)
    args = parser.parse_args(argv)
    config_dir = Path(args.config_dir).resolve()
    _redirect_missing_stdio(config_dir)

    from nexus.logging_setup import configure_logging  # noqa: PLC0415 — deferred: heavy import

    configure_logging("windows_autostart", config_dir=config_dir)
    _log.info("windows_autostart_launcher_started", pid=os.getpid(), config_dir=str(config_dir))
    return run_launcher(config_dir)


if __name__ == "__main__":
    sys.exit(main())
