# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The SessionStart hook's identity leg (RDR-253 Step 3, nexus-hczdo.1.4).

:func:`bind_at_session_start` gathers what the process knows about itself (its Claude pid, OS start time, registry
file, launch name), applies the policy table (:class:`nexus.identity.binder.SessionBinder`), runs the host sweep and
the local file cleanup, and returns what the hook should say and what the T1 handoff marker should carry. It never
raises: a SessionStart hook must not fail a session over identity, so an engine that cannot be reached, or a bug
here, becomes one line saying the session is unbound.

Called from :func:`nexus.hooks.session_start` as a call site only; the logic lives in :mod:`nexus.identity`.
Imported lazily by its caller: a hook pays its import cost on every invocation.

Not bound here, by design:

* a process that carries ``NX_IDENTITY_TOKEN`` (a delegated child): the dispatcher bound it;
* a process that inherited ``NX_SESSION_ID`` (a nested subprocess reusing its parent's session): the session id is
  not this process's, and binding it would re-point the parent's id at the child (the same rule that keeps the
  ``current_session`` file alone);
* a process with no Claude ancestor (``nx hook session-start`` typed in a shell).

On Windows the holder check cannot read a process start time (:mod:`nexus.identity.holder`), so the hook binds and
sweeps but never sends a ``holder_gone`` it could not prove, and the in-place ``/restart`` rebind, which needs the
process's own start time, does not run.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nexus.identity.binder import HANDOFF_SOURCES, NX_SEAT_ENV, BindOutcome, LaunchFacts, SessionBinder
from nexus.identity.holder import Processes, epoch_seconds, this_host

#: What the hook says when the Claude registry file never appeared (the MCP server then binds from the pending file).
_PENDING_NOTICE = ("identity: not bound yet. The Claude registry file did not appear within 5 seconds; "
                   "the MCP server will bind this session when it does.")


@dataclass
class IdentityResult:
    outcome: BindOutcome | None = None
    notices: list[str] = field(default_factory=list)
    sweep: Any = None

    def text(self) -> str:
        return "\n".join(self.notices)


def launch_name_from_args(args: str) -> str | None:
    """The ``-n`` / ``--name`` value of a Claude Code command line, for a hook that runs before the registry file
    holds the name. None when the line names none."""
    tokens = args.split()
    for i, token in enumerate(tokens):
        if token in ("-n", "--name") and i + 1 < len(tokens):
            return tokens[i + 1]
        if token.startswith("--name="):
            return token.split("=", 1)[1] or None
    return None


def _claude_args(pid: int) -> str:
    """The command line of *pid* from the process table (``nexus.session`` reads it with a Windows branch)."""
    try:
        from nexus.session import _list_processes  # noqa: PLC0415 — deferred: hook import cost

        return next((command for p, _parent, command in _list_processes() if p == pid), "")
    except Exception:  # noqa: BLE001 — best effort: the registry file is the primary source
        return ""


def bind_at_session_start(
    session_id: str, source: str | None, *, env: Mapping[str, str] | None = None, claude_pid: int | None = None,
    procs: Processes | Any | None = None, client: Any = None, config_dir: Path | None = None,
    clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
) -> IdentityResult:
    """Run the identity leg of SessionStart. Every argument after *source* is an injection seam for the tests; the
    hook passes none of them."""
    result = IdentityResult()
    try:
        _run(result, session_id, source or "", env if env is not None else os.environ, claude_pid, procs, client,
             config_dir, clock, sleep)
    except Exception as exc:  # noqa: BLE001 — a hook must never fail a session over identity
        _log().warning("identity_bind_failed", error=f"{type(exc).__name__}: {exc}")
        result.notices.append("identity: not bound (the identity step failed; see the nx log)")
    return result


def _log():
    import structlog  # noqa: PLC0415 — deferred: hook import cost

    return structlog.get_logger("nexus.hooks.identity_bind")


def _run(result: IdentityResult, session_id: str, source: str, env: Mapping[str, str], claude_pid: int | None,
         procs: Processes | Any | None, client: Any, config_dir: Path | None, clock: Callable[[], float],
         sleep: Callable[[float], None]) -> None:
    from nexus.identity.bind_client import IdentityClient, IdentityUnavailableError  # noqa: PLC0415
    from nexus.identity.pending import Pending, write_pending  # noqa: PLC0415
    from nexus.identity.sweep import clean_local_files, sweep_host  # noqa: PLC0415
    from nexus.identity.token_file import identity_dir  # noqa: PLC0415

    if (env.get("NX_IDENTITY_TOKEN") or "").strip() or (env.get("NX_SESSION_ID") or "").strip():
        return
    if claude_pid is None:
        from nexus.session import _command_name_of, find_immediate_claude_pid  # noqa: PLC0415

        claude_pid = find_immediate_claude_pid()
        if claude_pid <= 0 or not _command_name_of(claude_pid).lower().startswith("claude"):
            return
    if config_dir is None:
        from nexus import config as _nx_config  # noqa: PLC0415 — a module attribute so a patched config dir reaches it

        config_dir = _nx_config.nexus_config_dir()
    procs = procs if procs is not None else Processes()
    host = this_host() if getattr(procs, "host", None) is None else procs.host
    directory = identity_dir(config_dir)
    from nexus.identity.holder import normalise_instant  # noqa: PLC0415

    proc_start = normalise_instant(procs.os_start(claude_pid))
    try:
        from nexus.db.client_identity import client_version  # noqa: PLC0415

        version = client_version()
    except Exception:  # noqa: BLE001
        version = None
    facts = LaunchFacts(source=source, session_id=session_id, host=host, claude_pid=claude_pid, proc_start=proc_start,
                        client_version=version, seat=(env.get(NX_SEAT_ENV) or "").strip() or None,
                        pane=(env.get("TMUX_PANE") or "").strip() or None)
    try:
        engine = client if client is not None else IdentityClient.from_config(timeout=5.0)
    except IdentityUnavailableError as exc:
        result.notices.append(f"identity: not bound, no engine to bind against ({exc})")
        return
    binder = SessionBinder(client=engine, procs=procs, identity_dir=directory, clock=clock, sleep=sleep)

    if source != "compact" and not binder.await_registry(facts):
        facts.name = facts.name or launch_name_from_args(_claude_args(claude_pid))
        if proc_start is not None:
            write_pending(directory, Pending(session_id=session_id, source=source, seat=facts.seat, name=facts.name,
                                             os_start=proc_start, claude_pid=claude_pid, pane=facts.pane),
                          epoch_seconds(proc_start))
            result.notices.append(_PENDING_NOTICE)
        else:
            result.notices.append("identity: not bound; neither the Claude registry file nor the process start time is readable")
        clean_local_files(directory, procs, host=host)
        return
    if source == "compact":
        from nexus.identity.holder import read_registry_entry  # noqa: PLC0415

        entry = read_registry_entry(claude_pid, procs.sessions_dir)
        if entry is not None:
            facts.with_registry(entry)

    outcome = binder.bind(facts, env=env)
    result.outcome = outcome
    result.notices.extend(outcome.notices)
    if outcome.action in ("unbound",):
        return
    try:
        report = sweep_host(engine, procs, host=host, own=(claude_pid, facts.started_at), identity_dir=directory)
        clean_local_files(directory, procs, host=host, own_token=outcome.token_file, report=report)
        result.sweep = report
    except Exception as exc:  # noqa: BLE001 — the sweep is housekeeping; the bind above already stands
        _log().warning("identity_sweep_failed", error=f"{type(exc).__name__}: {exc}")


def handoff_fields(result: IdentityResult, source: str | None) -> dict[str, Any]:
    """The extra fields the T1 handoff marker carries for *result*: identity, generation and the token file's
    path, on ``clear``, ``resume`` and ``fork`` when the hook bound or kept an identity; else nothing."""
    outcome = result.outcome
    if source not in HANDOFF_SOURCES or outcome is None or not outcome.handoff:
        return {}
    return {"identity": outcome.identity, "generation": outcome.generation, "token_file": str(outcome.token_file)}
