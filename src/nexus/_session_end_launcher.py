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
to return control to Claude Code: ~17ms.

RDR-094 Phase C swap: the launcher dispatches to
``hooks.session_end_flush`` (storage-only path, fork-safe). nx-mcp
owns chroma teardown via its FastMCP lifespan + signal handler +
atexit chain (Phase 4, unconditional as of 4.13.0); the watchdog
sidecar is the safety net if all three of those paths fail. The
hook does T1 flush + T2 expire only, which is fork-safe.

**Pre-fork budget invariant** (historically "never import nexus.*
before ``os.fork()``"): the parent must pay near-zero cost before
forking off the daemon. Phase 1C (nexus-a52i) relaxed the letter of
the import ban for the LOCAL-mode tier summary — a fast, bounded
sqlite read — but the spirit is binding: nothing slow or network-bound
may run pre-fork. The SERVICE-mode summary therefore prints AFTER the
fork dispatch, from the parent, via a pinned-endpoint single-attempt
read (nexus-ov13k review — the retrying transport's 20-50s worst case
pre-fork would reproduce the exact "Hook cancelled" race this module
exists to prevent). Heavy imports for the cleanup itself happen only
inside ``_run_session_end_synchronously`` in the grandchild.

Shell invocation (wired into ``conexus/hooks/hooks.json``)::

    nx-session-end-launcher

On platforms without ``os.fork`` (Windows), falls through to the
synchronous path so cleanup still happens, at the cost of the hook
blocking until done.
"""
from __future__ import annotations

import os
import sys


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
    """
    try:
        from nexus import hooks  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
        hooks.session_end_flush()
    except Exception:  # noqa: BLE001 — boundary catch of undocumented third-party exceptions; non-fatal
        # Fully detached; nothing upstream can observe us. Swallow.
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


def _print_tier_status_summary() -> None:
    """Print a one-line tier-write summary to stderr BEFORE the fork.

    Phase 1C of the tier-discipline restoration initiative
    (nexus-a52i). Closes the visibility loop: every session that
    persists findings now sees its own contribution count at close.

    Best-effort: any failure is swallowed so launcher startup never
    breaks. Suppressed entirely when there are zero writes (no point
    printing for a transactional session that didn't intend to
    persist anything).

    Resolution: delegates to
    :func:`nexus.session.resolve_active_session_id`. Short-circuits
    when no session is bound -- a per-session summary makes no sense
    without a session, and querying ``WHERE session_id = "unknown"``
    would leak rows from unrelated invocations into the user-facing
    summary.

    Issue #594 / nexus-9e9a: this site shares the resolution chain
    with the T1 chunk store and the tier-write audit log, so the
    three surfaces never disagree on attribution.
    """
    try:
        import sqlite3  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
        from pathlib import Path  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

        from nexus.config import default_db_path  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
        from nexus.session import resolve_active_session_id  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

        session_id = resolve_active_session_id()
        if not session_id:
            return

        from nexus.db.storage_mode import StorageBackend, storage_backend_for  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
        if storage_backend_for("telemetry") == StorageBackend.SERVICE:
            # nexus-ov13k: service-mode summaries print POST-fork (see
            # _print_service_tier_summary + main()) — never from this
            # pre-fork path. Both reviewers independently flagged that any
            # network wait here (mixin worst case 20-50s: gateway backoff +
            # lease-wait, unbounded by the client timeout kwarg) would sit
            # AHEAD of the cleanup dispatch and reintroduce the exact
            # pre-fork SIGTERM race this module exists to prevent.
            return

        db_path = default_db_path()
        if not Path(db_path).exists():
            return
        conn = sqlite3.connect(str(db_path))  # epsilon-allow: session-end best-effort observation — must not block on daemon availability; read-only tier_writes check
        try:
            has_table = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='tier_writes'"
            ).fetchone()
            if not has_table:
                return
            rows = conn.execute(
                "SELECT tier, COUNT(*) FROM tier_writes "
                "WHERE session_id = ? GROUP BY tier",
                (session_id,),
            ).fetchall()
        finally:
            conn.close()
        by_tier = {tier: n for tier, n in rows}
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
    except Exception:  # noqa: BLE001 — boundary catch of undocumented third-party exceptions; non-fatal
        # Telemetry must never break session close.
        pass


def _print_service_tier_summary() -> None:
    """Service-mode twin of :func:`_print_tier_status_summary` — POST-fork.

    nexus-ov13k: service mode is the RDR-152 DEFAULT and records tier_writes
    in the engine, so the sqlite reader saw an empty table and the
    zero-writes suppression silently killed the Phase-1C summary for every
    service-mode session (third consumer of the wyu1g blindness class;
    tier-status and doctor fixed via nexus-59wjj).

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
        from nexus.db.storage_mode import StorageBackend, storage_backend_for  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
        if storage_backend_for("telemetry") != StorageBackend.SERVICE:
            return
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
    _print_tier_status_summary()
    if not hasattr(os, "fork"):
        # Windows etc — no fork, run synchronously.
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
