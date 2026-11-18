# SPDX-License-Identifier: AGPL-3.0-or-later
"""The outcome of ``stop_storage_service`` (moved out of ``storage_service_daemon.py``, which is at its
file-size ceiling, when ``unsignalled`` was added, nexus-f3yqd.2.22). ``storage_service_daemon`` re-exports
:class:`StopOutcome`, so callers and tests keep importing it from there."""
from __future__ import annotations

from dataclasses import dataclass

from nexus.daemon.service_registry import GracefulStopSend


@dataclass(frozen=True)
class StopOutcome:
    """Outcome of :func:`stop_storage_service` (nexus-oyo2g).

    Honest-output contract: a lease MISS from ``ServiceRegistry.discover()``
    is a discovery gap, never proof nothing is running (a TTL-expired lease
    on a stalled-but-alive supervisor is indistinguishable from a genuinely
    stopped service at the registry layer — the lz3f2/f9y78 stall
    signature). ``source`` names HOW this outcome was reached so a caller
    (the CLI) can print an honest message instead of a blanket "already
    stopped" whenever nothing was signalled from the lease.

    - ``"lease"``: a fresh lease named a live, signal-able process;
      signalled from it.
    - ``"process_table"``: the lease named nothing signal-able — either it
      was absent entirely, or (``lease_seen=True``) it WAS found but
      carried no usable pid (a malformed or legacy-shaped record) — and
      the OS process table (ground truth) found live processes belonging
      to this config_dir instead.
    - ``"none"``: nothing was signalled from the lease AND the process
      table confirms nothing is running — genuinely already stopped.
    - ``"refused"``: the target runs in another Windows session, so the
      console stop could not be sent (``refused`` names it and both session
      ids). Nothing was signalled or killed.
    - ``"process_table_unavailable"``: nothing was signalled from the
      lease, and the process table itself could not be read (no ``ps``
      and no ``/proc``) — degrades to the pre-nexus-oyo2g behaviour but
      says so, rather than silently claiming the same clean "already
      stopped".

    ``lease_seen`` is True whenever ``ServiceRegistry.discover()`` found a
    record at all, INDEPENDENT of ``source`` — a caller (the CLI) needs
    this to avoid saying "no lease found" when a lease WAS found but had
    no usable pid info (nexus-oyo2g review finding 2: ``source ==
    "process_table"``/``"none"`` alone conflates "lease truly absent" with
    "lease present but unusable", which is a different, honest-output-
    relevant fact).

    ``pids`` always includes every pid this call signalled (lease-named and/
    or tree-swept); ``stubborn`` is the subset still alive after the
    SIGTERM->SIGKILL escalation — a non-empty ``stubborn`` means the caller
    must NOT treat this as a clean stop.

    ``sweep_verified`` is True only when the phase-2/3 process-table sweep
    actually ran (``sweep_matching_processes(...).available``). It is False
    whenever the process table could not be read at all — INCLUDING the
    case where the lease branch already signalled something, so ``source``
    stays ``"lease"`` and the pids list is non-empty. Without this field
    that case is indistinguishable from a fully-verified clean stop: the
    docstring above calls the tree-completion sweep "not optional even on
    the lease-found path", so silently dropping its own verification
    status when it fails to run would contradict that claim (nexus-oyo2g
    review round 2, Significant finding 2). A caller MUST treat
    ``sweep_verified=False`` as "the tree-completion check could not run",
    regardless of what ``source``/``pids`` otherwise say.
    """

    pids: tuple[int, ...]
    stubborn: tuple[int, ...]
    source: str
    lease_seen: bool = False
    sweep_verified: bool = True
    #: Windows only (RDR-224, nexus-f9bgu.17): processes that could not be
    #: reached because they run in another Windows session. NOT signalled and
    #: NOT killed (Sam, 2026-10-05: a cross-session stop fails loud and never
    #: hard-kills). ``source`` is ``"refused"`` when nothing else was signalled.
    #: A refused pid is still running, so it is also in ``stubborn``.
    refused: tuple[GracefulStopSend, ...] = ()
    #: Running pids the stop could not identify, so it did not signal them: flagless
    #: ``daemon service start`` processes it could not place in any directory
    #: (unreadable or malformed environment, nexus-oacd3; ``sweep_verified`` is then
    #: False), and a legacy lease's endpoint pid whose command line could not be read
    #: (nexus-f3yqd). They may still be running, so the outcome is not "already stopped".
    unreadable: tuple[int, ...] = ()
    #: Supervisor pids a FRESH lease names that are not visible from this process
    #: (nexus-dyfsw): a supervisor in another PID namespace sharing the config
    #: directory, still heartbeating. Nothing was signalled and the lease was kept,
    #: so this stop did not bring the service down.
    invisible: tuple[int, ...] = ()
    #: Pids the sweep IDENTIFIED as this directory's stack (an explicit ``--config-dir`` supervisor, the
    #: engine, or a start whose own environment names this directory) and could not signal: both signals
    #: failed with EPERM because another user owns them (nexus-f3yqd.2.22). Still running, never stopped.
    #: Unlike ``unreadable`` they are certain members, so the CLI exits 1 before PostgreSQL for them, as it
    #: does for ``stubborn``; they are not in ``stubborn`` so they are not reported as surviving a SIGKILL.
    unsignalled: tuple[int, ...] = ()

    @property
    def already_stopped(self) -> bool:
        return (
            not self.pids and not self.refused and not self.unreadable
            and not self.invisible and not self.unsignalled
        )

