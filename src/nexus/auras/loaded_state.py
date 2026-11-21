# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The loaded-aura state file (RDR-252 "Two additions to the aura surface"; bead nexus-igea9.7 part A, items 6, 7).

The MCP server writes the identity it is bound to and the set of auras it loaded, and why each was or was not
loaded, to ``<config>/auras/loaded-<claude pid>.<startedAt>.json``, keyed like RDR-253's token files by its Claude
process's pid and the ``startedAt`` that process's registry file records. It rewrites the file at load and on each
identity change its heartbeat sees, and removes it on exit (:func:`remove_own`, which leaves a successor's file).

``nx aura list`` reads the file of its nearest Claude ancestor (:func:`view`) and removes a file whose holder
RDR-253's holder check finds gone: no registry file with that pid and ``startedAt``, or a pid whose OS start time
differs, which covers an in-place ``/restart`` (:func:`clean_gone`). A host the file was not written on, and a
holder the check cannot judge, are left alone. With no Claude ancestor the files are listed and none is claimed.

``identity`` is the identity the server holds now; ``loaded_identity`` is the one the loaded auras were evaluated for.
They differ after a cross-seat ``/resume`` or a take-over: loading happens at server start, so the loaded set and
``self.identity`` stay the old identity's until the MCP server reconnects, and :func:`view` says so.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import structlog

_log = structlog.get_logger(__name__)

#: The directory under the config dir that holds the files.
STATE_DIR_NAME: str = "auras"

_NAME = re.compile(r"^loaded-(?P<pid>[0-9]+)\.(?P<started_at>[0-9]+)\.json$")


@dataclass(frozen=True)
class AuraState:
    """One aura as the load stage decided it: loaded or not, why (a :mod:`nexus.auras.session` reason), and the
    group named in its ``[load]`` table, if any."""

    loaded: bool
    reason: str
    group: str | None = None


@dataclass(frozen=True)
class LoadedState:
    """What the file holds. ``pid``, ``started_at``, ``proc_start`` and ``host`` name the holder, a Claude process
    (the four fields RDR-253's holder check compares); ``server_pid`` is the MCP server that wrote it. The load
    stage builds one with only ``identity``, ``loaded_identity`` and ``auras``; the writer fills in the rest."""

    identity: str | None = None
    loaded_identity: str | None = None
    auras: dict[str, AuraState] = field(default_factory=dict)
    server_pid: int = 0
    host: str = ""
    pid: int = 0
    started_at: int = 0
    proc_start: str | None = None
    session_id: str = ""
    written_at: float = 0.0


def state_dir(config_dir: Path) -> Path:
    return Path(config_dir) / STATE_DIR_NAME


def state_path(directory: Path, pid: int, started_at: int) -> Path:
    return Path(directory) / f"loaded-{pid}.{started_at}.json"


def parse_state_name(path: Path) -> tuple[int, int] | None:
    """``(claude pid, startedAt)`` from a state file's name, or None for any other name."""
    m = _NAME.match(Path(path).name)
    return (int(m["pid"]), int(m["started_at"])) if m else None


def state_files(directory: Path) -> list[Path]:
    try:
        return sorted(p for p in Path(directory).iterdir() if parse_state_name(p) is not None)
    except OSError:
        return []


def write_state(path: Path, state: LoadedState) -> None:
    """Publish *state* at *path*: a temp file created 0600, then one atomic rename, so a reader never sees a torn file."""
    from nexus._winsec import make_user_dir, open_private  # noqa: PLC0415 -- deferred: the server and CLI pay it once

    path = Path(path)
    make_user_dir(path.parent)
    stamped = replace(state, written_at=state.written_at or time.time())
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        fd = open_private(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
        try:
            pending = memoryview(json.dumps(asdict(stamped)).encode("utf-8"))
            while pending:  # os.write may take fewer bytes than it is given
                pending = pending[os.write(fd, pending):]
        finally:
            os.close(fd)
        os.replace(str(tmp), str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def read_state(path: Path) -> LoadedState | None:
    """The file at *path*, or None when it is absent, unreadable or not a state file. Never raises."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    pid, started_at, auras = _int(data.get("pid")), _int(data.get("started_at")), data.get("auras")
    if pid is None or started_at is None or not isinstance(auras, dict):
        return None
    parsed: dict[str, AuraState] = {}
    for name, row in auras.items():
        if not isinstance(name, str) or not isinstance(row, dict) or not isinstance(row.get("loaded"), bool):
            return None
        parsed[name] = AuraState(row["loaded"], _text(row.get("reason")) or "", _text(row.get("group")))
    written = data.get("written_at")
    return LoadedState(
        identity=_text(data.get("identity")), loaded_identity=_text(data.get("loaded_identity")), auras=parsed,
        server_pid=_int(data.get("server_pid")) or 0, host=_text(data.get("host")) or "", pid=pid,
        started_at=started_at, proc_start=_text(data.get("proc_start")), session_id=_text(data.get("session_id")) or "",
        written_at=float(written) if isinstance(written, (int, float)) and not isinstance(written, bool) else 0.0,
    )


def delete_state(path: Path) -> None:
    with contextlib.suppress(OSError):
        Path(path).unlink()


def remove_own(path: Path, *, server_pid: int) -> bool:
    """Delete the file at *path* when this server (*server_pid*) wrote it: a server that exits after a ``/mcp``
    reconnect must not remove its successor's file. True when a file was removed."""
    state = read_state(path)
    if state is None or state.server_pid != server_pid:
        return False
    delete_state(path)
    return True


def clean_gone(directory: Path, procs: Any, *, identity_dir: Path | None = None) -> list[Path]:
    """Remove every state file whose holder the holder check finds gone; return what was removed. *procs* is
    :class:`nexus.identity.holder.Processes` or a stand-in. A file written on another host, and a holder the check
    cannot judge, stay. *identity_dir* is the token-file directory (``token_file.identity_dir(config dir)``), passed as
    ``sweep.py`` and ``binder.py`` pass it: the registry the check reads is one directory, so a holder whose token file
    names another one is judged by its pid and start time alone, never by a registry file that is not here."""
    from nexus.identity.holder import check_holder, read_registry_entry  # noqa: PLC0415 -- deferred: the identity package is a leaf
    from nexus.identity.hostname import is_key  # noqa: PLC0415

    removed: list[Path] = []
    for path in state_files(directory):
        named = parse_state_name(path)
        state = read_state(path)
        holder: dict[str, Any]
        if state is not None:
            holder = {"host": state.host, "pid": state.pid, "started_at": state.started_at, "proc_start": state.proc_start}
            if state.host != procs.host and not is_key(state.host):
                # A file written under a hostname this machine has since lost (before the host key existed), by a process
                # with no token file: this machine's own registry file for that pid and startedAt, live or a crash's
                # leftover, proves the process was local (nexus.identity.hostname). A clean exit leaves none: that file stays.
                entry = read_registry_entry(state.pid, procs.sessions_dir)
                if entry is not None and entry.started_at == state.started_at:
                    holder["host"] = procs.host
        elif named is not None:  # a torn or foreign file under a state file's name: judged by its name alone
            holder = {"host": procs.host, "pid": named[0], "started_at": named[1], "proc_start": None}
        else:
            continue
        try:
            verdict = check_holder(holder, procs=procs, identity_dir=identity_dir)
        except Exception as exc:  # noqa: BLE001 -- a check that cannot run leaves the file
            _log.debug("aura_state_holder_check_failed", path=str(path), error=str(exc))
            continue
        if verdict.gone:
            delete_state(path)
            removed.append(path)
    return removed


@dataclass(frozen=True)
class LoadedView:
    """What ``nx aura list`` shows of the load: ``state`` is the file of the nearest Claude ancestor (None when there
    is none, or it has no file), ``claimed`` is whether there was an ancestor to claim one, ``listed`` is every
    remaining file (the whole set, for a caller with no ancestor), ``mismatch`` is the text to print when the loaded
    auras were built for another identity than the one this process now resolves to, and ``removed`` the files
    dropped because their holder is gone."""

    state: LoadedState | None
    claimed: bool
    listed: tuple[LoadedState, ...]
    mismatch: str | None
    removed: tuple[Path, ...]


def identity_mismatch(state: LoadedState, current: str | None) -> str | None:
    """The mismatch text, or None when the identity the auras were loaded for is the one *current* names (or
    *current* is unknown: a resolver that cannot say reports nothing)."""
    if current is None or state.loaded_identity == current:
        return None
    if state.loaded_identity is None:
        return (
            f"this session's MCP server started without an identity and now resolves to {current}: "
            "reconnect the MCP server (/mcp) to load the group-gated auras"
        )
    return (
        f"the auras were loaded for identity {state.loaded_identity}, and this process now resolves to {current}: "
        "reconnect the MCP server (/mcp) to load for the new identity"
    )


def view(
    directory: Path, *, claude_pid: int | None, procs: Any, current_identity: str | None, identity_dir: Path | None = None,
) -> LoadedView:
    """The state files for ``nx aura list`` (module docstring). Removes the files of holders that are gone first."""
    removed = clean_gone(directory, procs, identity_dir=identity_dir)
    listed = tuple(s for s in (read_state(p) for p in state_files(directory)) if s is not None)
    if claude_pid is None:
        return LoadedView(None, False, listed, None, tuple(removed))
    own = [s for s in listed if s.pid == claude_pid]
    state = max(own, key=lambda s: s.started_at) if own else None
    mismatch = identity_mismatch(state, current_identity) if state is not None else None
    return LoadedView(state, True, listed, mismatch, tuple(removed))
