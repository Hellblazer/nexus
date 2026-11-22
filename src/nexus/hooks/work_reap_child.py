# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""SessionStart's reap of the session's own work rows (RDR-253 Step 6, nexus-hczdo: the work-row lifecycle).

At every SessionStart, after the identity leg has bound the session, :func:`spawn` starts a detached child that closes the
identity's running rows whose work has ended (:func:`nexus.identity.reap.reap`): an agent that was in flight across a
restart, a lab run killed without its end, a worktree that was removed. The hook returns at once; the child makes up to
one engine call per row under :data:`DEADLINE_S` and logs what it closed with a count, so a non-zero count names an
end-of-life path that did not write.

Stdlib only at module scope: SessionStart imports this module on every start.
"""
from __future__ import annotations

import os
import subprocess
import sys
import warnings
from typing import Any

#: The child's whole life, in seconds: enough for ~40 rows at the engine's usual latency; what it does not reach waits for
#: the next start or the restart ready check.
DEADLINE_S: float = 30.0

#: Set to 1 to start no child (tests, and an operator who wants the rows left as they are).
DISABLE_ENV = "NX_WORK_REAP_DISABLED"

_CREATE_NO_WINDOW: int = 0x08000000
_CREATE_NEW_PROCESS_GROUP: int = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB: int = 0x01000000


def spawn(session_id: str) -> bool:
    """Start the reap child for *session_id* and return at once; True when one started. Never raises, never waits."""
    if not session_id or session_id == "unknown" or os.environ.get(DISABLE_ENV, "") == "1":
        return False
    try:
        from nexus.util.nx_argv import argv_off_cwd  # noqa: PLC0415 — stdlib-only helper, deferred to the spawn
        from nexus.util.stable_cwd import detached_spawn_kwargs  # noqa: PLC0415 — stdlib-only helper, deferred to the spawn

        argv = [sys.executable, "-m", "nexus.hooks.work_reap_child", session_id]
        common: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                                  "stderr": subprocess.DEVNULL, "close_fds": True, **detached_spawn_kwargs()}
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
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                del child
            return True
    except Exception:  # noqa: BLE001 — a reap never costs the session start anything
        return False
    return False


def reap_session(session_id: str, env: Any = None) -> list[dict[str, str]]:
    """Reap the rows of the identity *session_id* belongs to. *env* defaults to the real :class:`IdentityEnv`."""
    from nexus.identity.ops import IdentityEnv  # noqa: PLC0415 — child process only
    from nexus.identity.reap import reap  # noqa: PLC0415 — child process only

    env = env if env is not None else IdentityEnv.real()
    identity = str(env.client.session(session_id)["identity"])
    return reap(env, identity, deadline_s=DEADLINE_S - 5.0)   # the alarm (POSIX) is the hard stop; this bound holds on Windows


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        return 2
    import signal  # noqa: PLC0415 — child process only

    if hasattr(signal, "alarm"):  # POSIX: the child never outlives its deadline; Windows relies on the engine timeouts
        signal.alarm(int(DEADLINE_S))
    try:
        from nexus._hook_runtime._io import configure_hook_logging  # noqa: PLC0415 — child process only

        configure_hook_logging()
    except Exception:  # noqa: BLE001 — no log file, still reap
        pass
    try:
        reap_session(argv[0])
    except Exception as exc:  # noqa: BLE001 — a reap that cannot run leaves the rows for the next reader
        try:
            import structlog  # noqa: PLC0415

            structlog.get_logger(__name__).warning("identity_work_reap_unavailable", error=f"{type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
