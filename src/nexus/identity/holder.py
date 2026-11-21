# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The client-side holder check (RDR-253 "A holder that no longer exists").

The engine cannot read a laptop's files, so the question "does the process that holds this seat still exist" is
the client's. A holder is ``{host, pid, started_at, proc_start}``: the host and pid of a Claude process, the
``startedAt`` its registry file records (milliseconds; an in-place ``/restart`` keeps the pid and the OS start time
but writes a new one) and the process's OS start time as a UTC instant to the second, ``2026-11-17T08:00:00Z``.

On the holder's own host the check is, in order:

1. a registry file ``<claude config>/sessions/<pid>.json`` naming that pid with that ``startedAt``; none, or one
   with another ``startedAt``, means the holder exited, crashed, was replaced by ``/restart`` or the machine
   rebooted: **gone**;
2. a pid probe (:func:`nexus.daemon.service_registry.pid_alive`, the one liveness probe); not running: **gone**;
3. the running process's OS start time against the binding's ``proc_start``, both as UTC instants: different means
   the pid was reused after a crash: **gone**. The registry's ``procStart`` string is UTC while ``ps`` prints local
   time, so the probe runs ``ps`` under ``TZ=UTC`` (Windows reads the process creation FILETIME); a probe that
   cannot read a start time answers **unknown**, never gone, and the caller treats unknown as running.

The registry legs (1) read one registry directory, ``CLAUDE_CONFIG_DIR/sessions``. A Claude under another config
directory writes its file elsewhere, so with an identity directory the registry is consulted only for a holder whose token
file names THIS directory; any other holder (or one with no token file) is judged by the pid probe and the start-time
compare alone, which do not depend on a directory: gone if the pid is dead or was reused, else unknown, and unknown is
never reported gone.

A holder on another host is judged by its lease alone and the check says **other_host**. "This host" is the machine's stable key, or a
pre-key hostname the machine can prove was its own (:func:`nexus.identity.hostname.holder_is_local`). A crashed process leaves
its registry file behind, so file presence alone never decides: the probe does.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nexus.identity import hostname as _hostname
from nexus.identity.hostname import describe_host, holder_is_local

GONE = "gone"
RUNNING = "running"
UNKNOWN = "unknown"
OTHER_HOST = "other_host"

#: ``entrypoint`` values of an interactive Claude session a person types into (RDR "Take-over"): a ``claude -p``
#: child's entrypoint is ``sdk-cli`` though its ``kind`` is ``interactive``.
INTERACTIVE_ENTRYPOINTS = frozenset({"cli", "claude-desktop"})
DESKTOP_ENTRYPOINT = "claude-desktop"


def this_host() -> str:
    """The name bindings carry for this machine: a key stable across network changes (:mod:`nexus.identity.hostname`)."""
    return _hostname.this_host()


def claude_sessions_dir() -> Path:
    """Claude Code's per-process session registry, ``<config>/sessions``; ``CLAUDE_CONFIG_DIR`` is honoured the
    way Claude Code honours it."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(override).expanduser() if override else Path.home() / ".claude") / "sessions"


# ── instants ────────────────────────────────────────────────────────────────


def format_instant(moment: datetime) -> str:
    """A UTC instant to the second, ``2026-11-17T08:00:00Z``: the one form every writer of ``proc_start`` uses (the
    engine compares it as a string)."""
    return moment.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalise_instant(text: object) -> str | None:
    """A process start as :func:`format_instant` spells it, from ``ps``'s ``lstart`` (``Tue Nov 17 08:00:00 2026``,
    which the registry's ``procStart`` also is, in UTC) or from an ISO instant; None for anything else. The two
    inputs are read as UTC: callers run ``ps`` under ``TZ=UTC``, and an ISO string without an offset is UTC here."""
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    try:
        return format_instant(datetime.strptime(raw, "%a %b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc))
    except ValueError:
        pass
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return format_instant(moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc))


def epoch_seconds(instant: str) -> int:
    """The Unix seconds of a :func:`format_instant` string (the pending file's name carries them)."""
    return int(datetime.strptime(instant, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


# ── the process table ───────────────────────────────────────────────────────


def os_start_time(pid: int, *, platform: str | None = None, run: Callable[..., Any] | None = None,
                  win_api: Any = None) -> str | None:
    """The OS start time of *pid* as a UTC instant, or None when it cannot be read (no such process, a failed ``ps``).
    POSIX: ``ps -o lstart=`` under ``TZ=UTC`` and ``LC_ALL=C``: ``lstart`` is local time, so a probe that forgot the
    variable would compare a local-time start against a UTC ``proc_start`` and find every holder gone. Windows: the
    process creation FILETIME (``nexus._install.winproc_core``), floored to the second, which is a fixed instant."""
    if (platform if platform is not None else sys.platform) == "win32":
        from nexus._install import winproc_core  # noqa: PLC0415 — Windows only

        created = winproc_core.process_creation_unix(pid, win_api if win_api is not None else winproc_core.ctypes_win_info_api())
        return None if created is None else format_instant(datetime.fromtimestamp(int(created), tz=timezone.utc))
    if run is None:
        from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred: hook import cost

        run = run_bounded
    try:
        from nexus.util.nx_argv import argv_off_cwd  # noqa: PLC0415 — stdlib-only; a planted ps in the cwd must not win on Windows

        out = run(argv_off_cwd(["ps", "-o", "lstart=", "-p", str(pid)]), timeout=2, stderr=subprocess.DEVNULL, check=True,
                  env={**os.environ, "TZ": "UTC", "LC_ALL": "C"}).stdout
    except Exception:  # noqa: BLE001 — no such pid, no ps, timeout: all "cannot read"
        return None
    return normalise_instant(str(out).strip())


class Processes:
    """What the holder check asks of the machine. The default reads the real process table; a test passes a
    stand-in with the same methods (``tests/identity/fakes.py``)."""

    def __init__(self, *, sessions_dir: Path | None = None, host: str | None = None, platform: str | None = None,
                 win_api: Any = None) -> None:
        self.sessions_dir = sessions_dir if sessions_dir is not None else claude_sessions_dir()
        self._host = host
        self.platform = platform if platform is not None else sys.platform
        self.win_api = win_api

    @property
    def host(self) -> str:
        """The name bindings carry for this machine: the one a caller gave, else :func:`this_host` read NOW, so a key probe
        that failed when this object was built (and is retried by :mod:`nexus.identity.hostname`) heals for a long-lived
        holder of it, the MCP lease."""
        return self._host if self._host is not None else this_host()

    def alive(self, pid: int) -> bool:
        from nexus.daemon.service_registry import pid_alive  # noqa: PLC0415 — the one liveness probe (test_pid_alive_single_probe_lint); deferred, stdlib-heavy module

        return pid_alive(pid, platform=self.platform)

    def os_start(self, pid: int) -> str | None:
        return os_start_time(pid, platform=self.platform, win_api=self.win_api)


# ── the registry ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RegistryEntry:
    """The fields of ``<claude config>/sessions/<pid>.json`` this package reads."""

    pid: int
    started_at: int | None
    session_id: str | None
    proc_start: str | None
    name: str | None
    entrypoint: str | None
    kind: str | None
    tmux: str | None

    @property
    def interactive(self) -> bool:
        return self.entrypoint in INTERACTIVE_ENTRYPOINTS

    @property
    def desktop(self) -> bool:
        return self.entrypoint == DESKTOP_ENTRYPOINT


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def read_registry_entry(pid: int, sessions_dir: Path | None = None) -> RegistryEntry | None:
    """The registry entry of *pid*, or None when there is no readable file for it. Never raises."""
    base = sessions_dir if sessions_dir is not None else claude_sessions_dir()
    try:
        data = json.loads((base / f"{pid}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or _int(data.get("pid")) not in (None, pid):
        return None
    return RegistryEntry(
        pid=pid, started_at=_int(data.get("startedAt")), session_id=_text(data.get("sessionId")),
        proc_start=normalise_instant(data.get("procStart")), name=_text(data.get("name")),
        entrypoint=_text(data.get("entrypoint")), kind=_text(data.get("kind")), tmux=_text(data.get("tmux")),
    )


# ── the check ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Verdict:
    """``state`` is one of :data:`GONE`, :data:`RUNNING`, :data:`UNKNOWN`, :data:`OTHER_HOST`; *reason* says why in
    words a log line or a hook notice can carry."""

    state: str
    reason: str

    @property
    def gone(self) -> bool:
        return self.state == GONE


def holder_process(holder: Mapping[str, Any]) -> dict[str, Any]:
    """The ``holder_gone`` evidence naming *holder*: the four fields the engine compares, nothing else."""
    return {"host": holder.get("host"), "pid": holder.get("pid"), "started_at": holder.get("started_at"),
            "proc_start": holder.get("proc_start")}


def check_holder(holder: Mapping[str, Any], *, procs: Processes | Any, host: str | None = None,
                 identity_dir: Path | None = None) -> Verdict:
    """Whether the process *holder* names still exists (module docstring). *holder* is a binding's
    ``{host, pid, started_at, proc_start}``.

    The registry legs read ONE registry directory, ``procs.sessions_dir``: a Claude running under another
    ``CLAUDE_CONFIG_DIR`` writes its registry file somewhere else, so an absent file there proves nothing and a file with
    its pid there may be a stale leftover of another process. With *identity_dir* the holder's token file says which
    directory the holder used, and this is applied BEFORE the registry is read: a holder whose token file does not name
    this reader's directory (or that has no token file) is judged by the two legs that do not depend on a directory,
    the pid probe and the OS start time against ``proc_start``. A dead or reused pid is gone; anything else is
    unknown. A holder that was restarted in place (same pid and start time, a new ``startedAt``) cannot be told
    from a running one without its registry file, so it stays unknown."""
    here = host if host is not None else procs.host
    if not holder_is_local(holder, host=here, identity_dir=identity_dir):
        return Verdict(OTHER_HOST, f"held on {describe_host(holder.get('host'))}, not on {describe_host(here)}")
    pid = _int(holder.get("pid"))
    if pid is None:
        return Verdict(UNKNOWN, "the binding names no pid")
    if identity_dir is not None:
        from nexus.identity.token_file import read_token_file, token_path  # noqa: PLC0415 — token_file imports holder lazily

        started_at = _int(holder.get("started_at"))
        record = read_token_file(token_path(identity_dir, pid, started_at)) if started_at is not None else None
        if record is None or record.registry_dir != str(procs.sessions_dir):
            where = "no token file records" if record is None else f"its token file records {record.registry_dir}, not"
            note = f"{where} {procs.sessions_dir} as its registry directory"
            if not procs.alive(pid):
                return Verdict(GONE, f"pid {pid} is not running ({note})")
            recorded = normalise_instant(holder.get("proc_start"))
            started = procs.os_start(pid) if recorded is not None else None
            if started is not None and started != recorded:
                return Verdict(GONE, f"pid {pid} started {started}, not {recorded} (the pid was reused; {note})")
            return Verdict(UNKNOWN, f"pid {pid} is running or cannot be judged without its registry file: {note}")
    entry = read_registry_entry(pid, procs.sessions_dir)
    if entry is None:
        return Verdict(GONE, f"no registry file for pid {pid}")
    if entry.started_at != holder.get("started_at"):
        return Verdict(GONE, f"pid {pid} now has startedAt {entry.started_at}, not {holder.get('started_at')} (restarted)")
    if not procs.alive(pid):
        return Verdict(GONE, f"pid {pid} is not running")
    recorded = normalise_instant(holder.get("proc_start"))
    if recorded is None:
        return Verdict(RUNNING, f"pid {pid} is running (the binding recorded no start time)")
    started = procs.os_start(pid)
    if started is None:
        return Verdict(UNKNOWN, f"pid {pid} is running but its start time cannot be read here")
    if started != recorded:
        return Verdict(GONE, f"pid {pid} started {started}, not {recorded} (the pid was reused)")
    return Verdict(RUNNING, f"pid {pid} is running")
