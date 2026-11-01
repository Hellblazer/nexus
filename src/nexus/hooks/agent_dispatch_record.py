# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Record one agent dispatch's T1 put count in the engine (nexus-mtheb.4).

The SubagentStop nudge (:mod:`nexus.hooks.subagent_stop_t1_nudge`) calls
:func:`spawn` at every real agent stop. The hook must not wait on the
network: the record goes out from a detached child process
(``python -m nexus.hooks.agent_dispatch_record <json>``) and the hook
returns at once. The child pins the endpoint from one fast resolve and makes
one bounded request through ``record_agent_dispatch_once``, the pattern
``_session_end_launcher`` uses for its tier-write summary, so it never
touches the gateway retry ladder. Its data-token step can mint (up to a
minute on a cold mint, longer waiting on the mint lock), so a watchdog ends
the child after ``_CHILD_LIFETIME_S`` whatever it is doing: no row then, as
with any other failure.

Best effort end to end: a missing endpoint, an engine without the route
(404), a slow engine or any other failure leaves no row and no output. The
engine upsert keeps the larger count per (session, agent), so the re-stop
after a nudge, which sends the final count, wins over the first stop's zero.

``NX_DISPATCH_COUNTER=off`` (or ``0``) turns the spawn off; tests that run
the hook verb in a subprocess set it so no child reaches a real service.

Stdlib only at module scope: the nudge imports this module on every real
agent stop.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

__all__ = ["OFF_ENV", "main", "run_child", "spawn"]

#: Set to ``off`` or ``0`` to start no recorder child.
OFF_ENV: str = "NX_DISPATCH_COUNTER"

#: The one request's hard timeout, in seconds.
_REQUEST_TIMEOUT_S: float = 2.0

#: The child's whole life, imports and token step included, in seconds.
_CHILD_LIFETIME_S: float = 8.0

# Windows creation flags, as _session_end_launcher sets them for its detached
# child (CREATE_NO_WINDOW, not DETACHED_PROCESS: see that module).
_CREATE_NO_WINDOW: int = 0x08000000
_CREATE_NEW_PROCESS_GROUP: int = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB: int = 0x01000000


def _disabled() -> bool:
    return os.environ.get(OFF_ENV, "").strip().lower() in {"off", "0"}


def spawn(session_id: str, agent_id: str, agent_type: str, t1_puts: int) -> bool:
    """Start the recorder child and return at once; ``True`` when one started.

    Never raises and never waits. Nothing is spawned without a session id
    and an agent id, the row's identity.
    """
    if _disabled() or not session_id or not agent_id:
        return False
    try:
        import subprocess  # noqa: PLC0415 — deferred: the nudge's import cost stays stdlib-light
        import warnings  # noqa: PLC0415 — deferred with subprocess

        from nexus.util.nx_argv import argv_off_cwd  # noqa: PLC0415 — stdlib-only helper, deferred with subprocess
        from nexus.util.stable_cwd import detached_spawn_kwargs  # noqa: PLC0415 — stdlib-only helper, deferred with subprocess

        payload = json.dumps({
            "session_id": session_id,
            "agent_id": agent_id,
            "agent_type": agent_type or None,
            "t1_puts": int(t1_puts),
        })
        argv = [sys.executable, "-m", "nexus.hooks.agent_dispatch_record", payload]
        common: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
            **detached_spawn_kwargs(),
        }
        if os.name == "nt":
            base = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
            attempts: list[dict[str, Any]] = [{"creationflags": base | _CREATE_BREAKAWAY_FROM_JOB}, {"creationflags": base}]
        else:
            attempts = [{"start_new_session": True}]
        for extra in attempts:
            try:
                child = subprocess.Popen(argv_off_cwd(argv), **common, **extra)
            except (OSError, ValueError):
                continue
            # Never waited on; drop the handle without Popen.__del__'s
            # "still running" ResourceWarning on the hook's stderr.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                del child
            return True
    except Exception:  # noqa: BLE001 — a counter must never cost the hook anything
        return False
    return False


def _record(payload: dict) -> None:
    from nexus.db.data_token import get_data_token_manager  # noqa: PLC0415 — child process only
    from nexus.db.service_endpoint import resolve_service_endpoint  # noqa: PLC0415 — child process only
    from nexus.db.t2._refreshable_client import DEFAULT_TENANT  # noqa: PLC0415 — child process only
    from nexus.db.t2.http_telemetry_store import HttpTelemetryStore  # noqa: PLC0415 — child process only

    # One fast resolve with both halves pinned, so the store's own
    # evidence-gated resolution and its lease wait never run (the
    # _session_end_launcher summary pattern).
    base_url, token = resolve_service_endpoint()
    data_token = get_data_token_manager().bearer_for(base_url, DEFAULT_TENANT)
    if data_token is not None:
        token = data_token
    store = HttpTelemetryStore(base_url=base_url, _token=token)
    try:
        store.record_agent_dispatch_once(
            session_id=str(payload["session_id"]),
            agent_id=str(payload["agent_id"]),
            t1_puts=int(payload["t1_puts"]),
            agent_type=payload.get("agent_type") or None,
            timeout=_REQUEST_TIMEOUT_S,
        )
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 — best-effort close
            pass


def main(argv: list[str] | None = None) -> int:
    """Child entry: record the dispatch in ``argv[0]`` (JSON). Always 0."""
    args = sys.argv[1:] if argv is None else argv
    try:
        payload = json.loads(args[0])
        if isinstance(payload, dict):
            _record(payload)
    except Exception:  # noqa: BLE001 — best effort: no row, no output, exit 0
        pass
    return 0


def _arm_watchdog(seconds: float) -> None:
    """End this process after *seconds*, exit 0, whatever it is doing."""
    import threading  # noqa: PLC0415 — child process only

    timer = threading.Timer(seconds, os._exit, args=(0,))
    timer.daemon = True
    timer.start()


def run_child(argv: list[str] | None = None) -> int:
    """The ``python -m`` entry: arm the watchdog, then :func:`main`."""
    _arm_watchdog(_CHILD_LIFETIME_S)
    return main(argv)


if __name__ == "__main__":
    sys.exit(run_child())
