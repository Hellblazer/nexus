# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fork-first SessionEnd daemonizer (nexus-2u7o, RDR-094 Phase C).

The 4.10.3 double-fork path in ``nexus.commands.hook.session_end_detach_cmd``
waits for Click to parse argv and for ``nexus.hooks`` + friends to import
before calling ``os.fork()``. Cold-start cost on a reference install is
~2 seconds, and Claude Code's shutdown SIGTERM to the hook's process
group arrives faster than that on some machines, so the first fork
never runs and ``Hook cancelled`` is logged instead of the graceful
cleanup.

This module flips the order: the ``__main__`` block uses only ``os``
and ``sys`` from the standard library (both are preloaded by the
interpreter, so no import cost), forks, ``setsid``s, forks again, and
redirects stdio to ``/dev/null`` -- all before touching a single nexus
module. Then in the fully detached grandchild it imports
``nexus.hooks`` and runs ``session_end_flush()``. Wall-clock cost
to return control to Claude Code: ~17ms for the fork itself; measured
2026-08-28 (nexus-33fhv) the parent path totals ~150ms with a reachable
engine (~120ms with none), the balance being the POST-fork tier-summary
read and capability census below, which never delay the cleanup
dispatch. The detached grandchild's own work is ~1ms against a local
engine (one T2 expire round-trip; the T1 routing resolve is file reads).

RDR-094 Phase C swap: the launcher dispatches to
``hooks.session_end_flush`` (storage-only path, fork-safe). nx-mcp
owns chroma teardown via its FastMCP lifespan + signal handler +
atexit chain (Phase 4, unconditional as of 4.13.0); the watchdog
sidecar is the safety net if all three of those paths fail. The
hook does T1 flush + T2 expire only, which is fork-safe.

**Pre-fork budget invariant** (historically "never import nexus.*
before ``os.fork()``"): the parent must pay near-zero cost before
forking off the daemon. Nothing slow or network-bound may run
pre-fork — the tier summary therefore prints AFTER the fork dispatch,
from the parent, via a pinned-endpoint single-attempt read
(nexus-ov13k review — the retrying transport's 20-50s worst case
pre-fork would reproduce the exact "Hook cancelled" race this module
exists to prevent; the Phase 1C pre-fork sqlite summary that once
relaxed the import ban died with the =sqlite opt-out, RDR-158 P3).
Heavy imports for the cleanup itself happen only inside
``_run_session_end_synchronously`` in the grandchild.

Shell invocation (wired into ``conexus/hooks/hooks.json``)::

    nx-session-end-launcher

On platforms without ``os.fork`` (Windows), :func:`_spawn_detached_cleanup`
starts the cleanup as a detached child process and returns, which is the
Windows equivalent of the double-fork (nexus-34f7r). Running the cleanup
inline there, as this module used to, spent the hook's whole budget on it,
and Claude Code on Windows reported "SessionEnd hook
[nx-session-end-launcher] failed: Hook cancelled". Only if the detached
spawn fails does the cleanup run synchronously, so it is never skipped.
"""
from __future__ import annotations

import os
import sys

#: The cleanup child's entry. A ``-c`` string rather than ``-m``, because
#: this module's ``__main__`` block is the whole launcher, and the child must
#: run only the cleanup.
_DETACHED_CHILD_CODE: str = (
    "from nexus._session_end_launcher import _run_session_end_synchronously; "
    "_run_session_end_synchronously()"
)

#: Windows CreateProcess flags, spelled out because ``subprocess`` defines
#: them only on Windows and is not imported here at module scope: POSIX pays
#: the pre-fork budget above, and ``subprocess`` costs ~5ms of it (measured
#: 2026-09-24). DETACHED_PROCESS gives the child no console, so nothing
#: flashes on screen and nothing ties it to the hook's console.
#: CREATE_NEW_PROCESS_GROUP keeps a console CTRL event aimed at the hook
#: from reaching it. CREATE_BREAKAWAY_FROM_JOB takes it out of any job
#: object the hook runs in, so closing that job does not kill it; a job
#: that forbids breakaway refuses the spawn, and the spawn is retried
#: without the flag.
_DETACHED_PROCESS: int = 0x00000008
_CREATE_NEW_PROCESS_GROUP: int = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB: int = 0x01000000


def _run_session_end_synchronously() -> None:
    """Import nexus.hooks and call session_end_flush; swallow exceptions.

    Runs in the fully detached grandchild, so exceptions are no longer
    observable by Claude Code -- they must not escape and crash the
    daemon. Logging goes through the structlog pipeline nexus.hooks
    already configures (RotatingFileHandler under ~/.config/nexus/logs).

    RDR-094 Phase C: dispatches to ``session_end_flush`` (storage-only)
    rather than ``session_end``. Chroma teardown is owned by the MCP
    server's lifespan/atexit/signal handlers (Phase 4, unconditional
    as of 4.13.0); calling stop_t1_server here would race those paths
    and was the documented source of double-stop failures.

    nexus-h33x8.3: also records this session's capability census (nexus-
    gjv9b PART 1: to the ``capability_census`` engine table now, not a
    JSONL log), in its own failure-isolated step -- a census bug
    must never prevent (or be prevented by) the storage flush above.
    This is the ONLY place the census writer runs: the grandchild's
    stdio is already redirected to /dev/null by the time this function
    is reached (see :func:`_daemonize_and_run`), so the write is
    PROVABLY INVISIBLE on screen and does not need to race the
    pre-fork budget invariant that gates :func:`_print_service_tier_summary`
    below -- see ``nexus._session_end_census``'s module docstring for the
    full reasoning on why no visible line was added.
    """
    try:
        from nexus import hooks  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
        hooks.session_end_flush()
    except Exception:  # noqa: BLE001 — boundary catch of undocumented third-party exceptions; non-fatal
        # Fully detached; nothing upstream can observe us. Swallow.
        pass
    _write_capability_census()
    _sweep_local_garbage()


def _sweep_local_garbage() -> None:
    """Reap the config directory's litter at session close (nexus-fjwk7):
    ``t1_mint_<session>.lock`` files older than a day with no live lease,
    rotated logs, operator dumps. The same sweep ``nx doctor --fix`` runs,
    so a box no one doctors stops accumulating hundreds of zero-byte
    locks. Failure-isolated like the census above."""
    try:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
        from nexus.garbage import sweep_local_garbage  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

        sweep_local_garbage(nexus_config_dir())
    except Exception as exc:  # noqa: BLE001 — boundary catch; session close must never break on a sweep
        try:
            import structlog  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

            structlog.get_logger(__name__).debug(
                "session_end_garbage_sweep_failed", error=str(exc),
            )
        except Exception:  # noqa: BLE001 — even the debug log is best-effort
            pass


def _write_capability_census() -> None:
    """Best-effort per-session capability census append (nexus-h33x8.3).

    Wraps ``nexus._session_end_census.write_session_capability_census``
    so a census bug can never break SessionEnd cleanup; unlike the flush
    above, failures here are logged via structlog (debug level) rather
    than bare-swallowed, since an environment whose census silently
    fails forever must stay diagnosable from the logs (same discipline
    as :func:`_print_service_tier_summary`).
    """
    try:
        from nexus._session_end_census import write_session_capability_census  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
        write_session_capability_census()
    except Exception as exc:  # noqa: BLE001 — boundary catch; session close must never break on census
        try:
            import structlog  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
            structlog.get_logger(__name__).debug(
                "session_end_capability_census_failed",
                error=str(exc),
            )
        except Exception:  # noqa: BLE001 — even the debug log is best-effort
            pass


def _daemonize_and_run() -> None:
    """Daemonize via the canonical double-fork + setsid, then run cleanup.

    Contract: returns control to the caller (Claude Code's hook runner)
    in the parent in single-digit milliseconds. The grandchild runs the
    actual cleanup and exits via ``os._exit(0)``.
    """
    # First fork: let the original parent return to the shell /
    # Claude Code immediately.
    try:
        first_pid = os.fork()
    except OSError:
        # No fork available for some reason; fall through to synchronous.
        _run_session_end_synchronously()
        return
    if first_pid > 0:
        return  # Original process — return to Click caller which then exits.

    # Child: create a new session to leave Claude Code's process group
    # so a pgrp-wide SIGTERM from Claude Code doesn't reap us.
    try:
        os.setsid()
    except OSError:
        pass

    # Second fork: ensure the grandchild is not a session leader, so it
    # can never reacquire a controlling terminal (canonical daemon
    # recipe).
    try:
        second_pid = os.fork()
    except OSError:
        _run_session_end_synchronously()
        os._exit(0)
    if second_pid > 0:
        os._exit(0)

    # Grandchild: redirect stdio to /dev/null. Claude Code may close the
    # original hook fds during shutdown; leaving them open would let a
    # write at shutdown kill us with SIGPIPE.
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            try:
                os.dup2(devnull, fd)
            except OSError:
                pass
        if devnull > 2:
            os.close(devnull)
    except OSError:
        pass

    _run_session_end_synchronously()
    os._exit(0)


def _spawn_detached_cleanup() -> bool:
    """Start the cleanup as a detached child and return at once (nexus-34f7r).

    The no-fork counterpart of :func:`_daemonize_and_run`. Returns ``True``
    when a child was started. Returns ``False`` when both spawn attempts
    failed, and the caller then runs the cleanup inline so it still happens.
    The child's stdio is the null device, as the grandchild's is on POSIX,
    because Claude Code may close the hook's handles while it is exiting.

    WHAT IS NOT MEASURED: whether the child outlives Claude Code's own exit
    on Windows. That depends on the job object Claude Code runs hooks in,
    which this code cannot see; the breakaway attempt is the best it can do
    without knowing. The qwentescence check on nexus-34f7r settles it.
    """
    import subprocess  # noqa: PLC0415 — deferred: off the POSIX pre-fork path (module docstring)
    import warnings  # noqa: PLC0415 — deferred with subprocess, for the same reason

    argv = [sys.executable, "-c", _DETACHED_CHILD_CODE]
    base = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
    for flags in (base | _CREATE_BREAKAWAY_FROM_JOB, base):
        try:
            child = subprocess.Popen(
                argv,
                creationflags=flags,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except (OSError, ValueError):
            continue
        # The child is deliberately never waited on. Dropping the handle
        # here makes Popen.__del__ print "subprocess N is still running" as
        # a ResourceWarning on the hook's stderr, so drop it quietly.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            del child
        return True
    return False


def _print_service_tier_summary() -> None:
    """Print the Phase-1C tier-write summary from the engine — POST-fork.

    nexus-ov13k: the engine records tier_writes, so the pre-fork sqlite
    reader this replaced saw an empty table and the zero-writes suppression
    silently killed the summary for every service-mode session (third
    consumer of the wyu1g blindness class; tier-status and doctor fixed via
    nexus-59wjj). The sqlite twin died with the =sqlite opt-out (RDR-158
    P3, nexus-7bomn).

    Runs in the PARENT after :func:`_daemonize_and_run` has already forked
    the cleanup child, so no network wait can ever delay the cleanup
    dispatch (review Critical: the mixin's retrying transport has a 20-50s
    worst case that the client timeout kwarg does not bound). Uses the
    single-attempt ``query_tier_writes_once`` — one raw request, hard 2s
    timeout, no gateway backoff, no lease-wait. Failure is silent on stderr
    (session close must not noise-fail) but leaves a structured debug event
    (review Significant: an environment whose summaries fail forever must be
    diagnosable from the logs).
    """
    try:
        from nexus.session import resolve_active_session_id  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
        session_id = resolve_active_session_id()
        if not session_id:
            return
        # Round-2 critique: pin BOTH endpoint halves from a single fast
        # resolve (wait_budget 0) so the mixin's evidence-gated construction
        # retry (12s lease-wait on a supervisor-mid-restart box) can never
        # fire here — a missed summary is acceptable; blowing the SessionEnd
        # hook timeout is not.
        from nexus.db.service_endpoint import resolve_service_endpoint  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
        from nexus.db.t2.http_telemetry_store import HttpTelemetryStore  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

        base_url, token = resolve_service_endpoint()
        # code-review Sig#1 (nexus-wrwb7 fix pass, nexus-ssqk9 relay): this
        # store is constructed with BOTH base_url and _token pinned (to skip
        # the mixin's own evidence-gated resolution entirely -- see the
        # round-2 critique above), which also sets _token_pinned=True, so
        # RefreshableHttpStoreMixin._apply_data_token_override() would
        # silently no-op forever, and this SessionEnd summary would never
        # adopt a self-minted data token even when mint_token is
        # configured. Apply the SAME override here, BEFORE construction,
        # so the store still gets pinned (zero wait risk) with the RIGHT
        # token from the start -- never touching _token_pinned's skip-if-
        # pinned contract at all.
        from nexus.db.data_token import get_data_token_manager  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
        from nexus.db.t2._refreshable_client import DEFAULT_TENANT  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

        data_token = get_data_token_manager().bearer_for(base_url, DEFAULT_TENANT)
        if data_token is not None:
            token = data_token
        store = HttpTelemetryStore(base_url=base_url, _token=token)
        try:
            svc_rows = store.query_tier_writes_once(
                session_id=session_id, timeout=2.0,
            )
        finally:
            try:
                store.close()
            except Exception:  # noqa: BLE001 — best-effort close; never mask the read outcome
                pass
        by_tier: dict[str, int] = {}
        for _tool, tier, _agent, _project, n in svc_rows:
            by_tier[tier] = by_tier.get(tier, 0) + n
        total = sum(by_tier.values())
        if total == 0:
            return
        parts = [
            f"{tier}={by_tier.get(tier, 0)}"
            for tier in ("T1", "T2", "T3", "plan")
            if by_tier.get(tier, 0)
        ]
        sys.stderr.write(
            f"nx tier writes (session {session_id[:8]}): "
            f"total={total} {' '.join(parts)}\n"
        )
        sys.stderr.flush()
    except Exception as exc:  # noqa: BLE001 — boundary catch; session close must never break on telemetry
        try:
            import structlog  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
            structlog.get_logger(__name__).debug(
                "session_end_tier_summary_service_unavailable",
                error=str(exc),
            )
        except Exception:  # noqa: BLE001 — even the debug log is best-effort
            pass


def main() -> None:
    # The pre-fork SQLite tier summary died with the =sqlite opt-out
    # (RDR-158 P3, nexus-7bomn): the service twin below prints POST-fork so
    # a slow/hung service read can never delay the cleanup dispatch — the
    # exact pre-fork SIGTERM race the old ordering had to guard against.
    if not hasattr(os, "fork"):
        # Windows: a detached child is the no-fork double-fork. Inline only
        # when the spawn itself failed, so cleanup is never skipped.
        if not _spawn_detached_cleanup():
            _run_session_end_synchronously()
        _print_service_tier_summary()
        return
    _daemonize_and_run()
    # POST-fork (parent side): the cleanup child is already dispatched, so a
    # slow/hung service read can no longer delay it (nexus-ov13k review).
    _print_service_tier_summary()


if __name__ == "__main__":
    main()
    sys.exit(0)
