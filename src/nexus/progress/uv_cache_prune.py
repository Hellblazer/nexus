# SPDX-License-Identifier: AGPL-3.0-or-later
"""``uv cache prune`` that never holds an install hostage (nexus-mjmjl).

``nx self install`` prunes uv's package cache after a flip (nexus-xn84f), and
``nx self gc --prune-uv-cache`` does it on its own. ``uv cache prune`` takes
uv's cache lock exclusively and, by default, waits for every other uv process
that is reading the cache to finish. Any ``uv run`` or ``uvx`` left running
(another session's tools, a test run) is enough, so the prune sat silent until
its 600 s bound ran out, after the install itself had finished.

Now the prune waits at most :data:`LOCK_WAIT_S` for the lock. When the cache
is busy it starts the same prune in the background, detached, with a lock
wait of :data:`BACKGROUND_LOCK_WAIT_S` and its output in a log file (one at a
time: a live one is not started again), and says so: it is cleanup, and the
install it follows is already complete.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import IO, Any

from nexus.util.nx_argv import argv_off_cwd

#: Seconds the foreground prune waits for uv's cache lock (``UV_LOCK_TIMEOUT``).
LOCK_WAIT_S: int = 15
#: Seconds the background prune waits for the lock: it finishes once the uv
#: process holding the cache ends, or gives up after this.
BACKGROUND_LOCK_WAIT_S: int = 6 * 3600
#: The foreground prune's own bound once it holds the lock.
PRUNE_TIMEOUT_S: int = 600

def cache_busy(stderr: str) -> bool:
    """True when uv refused because another uv process holds the cache.

    uv 0.13 prints "Cache is currently in-use, waiting for other uv processes
    to finish" and then "Timeout (<n>s) when waiting for lock on ..."."""
    text = stderr.lower()
    return "in-use" in text or ("timeout" in text and "lock" in text)


def log_path(config_dir: Path) -> Path:
    """``<config>/logs/uv-cache-prune.log``."""
    return config_dir / "logs" / "uv-cache-prune.log"


def pid_path(config_dir: Path) -> Path:
    """The background prune's pid, so a second install does not start another."""
    return config_dir / "logs" / "uv-cache-prune.pid"


def _pid_alive(pid: int) -> bool:
    try:
        from nexus.daemon.service_registry import pid_alive  # noqa: PLC0415 — deferred, the one liveness probe

        return bool(pid_alive(pid))
    except Exception:  # noqa: BLE001 — unknown reads as not running
        return False


def background_running(config_dir: Path, *, alive: Callable[[int], bool] = _pid_alive) -> bool:
    """A background prune this module started is still waiting or running."""
    try:
        pid = int(pid_path(config_dir).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return alive(pid)


def prune(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]],
    spawn: Callable[[Sequence[str], Mapping[str, str], IO[bytes]], Any] | None,
    config_dir: Path,
    environ: Mapping[str, str] | None = None,
    alive: Callable[[int], bool] = _pid_alive,
) -> str:
    """Prune uv's cache; return one line for the operator. Never raises.

    *run* is :func:`nexus.bounded_subprocess.run_bounded`; *spawn* starts the
    detached background prune (``None``: do not, and say how to run it later).
    """
    env = dict(os.environ if environ is None else environ)
    env["UV_LOCK_TIMEOUT"] = str(LOCK_WAIT_S)
    try:
        r = run(argv_off_cwd(["uv", "cache", "prune"]), timeout=PRUNE_TIMEOUT_S, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"uv cache prune skipped: {exc}"
    if r.returncode == 0:
        tail = ((r.stderr or "").strip() or (r.stdout or "").strip()).splitlines()
        return "uv cache prune: " + (tail[-1] if tail else "done")
    stderr = (r.stderr or "").strip()
    if not cache_busy(stderr):
        return f"uv cache prune failed (rc={r.returncode}): {stderr[:200]}"
    later = "`nx self gc --prune-uv-cache`"
    if spawn is None:
        return (
            "uv cache prune skipped: another uv process is using uv's cache "
            f"(waited {LOCK_WAIT_S} s). Run {later} once it has ended."
        )
    path = log_path(config_dir)
    if background_running(config_dir, alive=alive):
        return (
            f"uv cache prune: another uv process is using uv's cache (waited {LOCK_WAIT_S} s); a background "
            f"prune started earlier is already waiting for it. Log: {path}"
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        bg_env = dict(env)
        bg_env["UV_LOCK_TIMEOUT"] = str(BACKGROUND_LOCK_WAIT_S)
        with path.open("ab") as fh:
            child = spawn(argv_off_cwd(["uv", "cache", "prune"]), bg_env, fh)
        pid = getattr(child, "pid", None)
        if isinstance(pid, int):
            pid_path(config_dir).write_text(str(pid), encoding="utf-8")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return (
            "uv cache prune skipped: another uv process is using uv's cache, and the "
            f"background prune could not start ({exc}). Run {later} once it has ended."
        )
    return (
        f"uv cache prune: another uv process is using uv's cache (waited {LOCK_WAIT_S} s), so the "
        f"prune continues in the background: it runs once that process ends, or gives up after "
        f"{BACKGROUND_LOCK_WAIT_S // 3600} h. Log: {path}"
    )


def spawn_detached(argv: Sequence[str], env: Mapping[str, str], log: IO[bytes]) -> subprocess.Popen[bytes]:
    """Start *argv* outside this process's session (POSIX) or process group and
    console (Windows), in a stable directory, stdout and stderr to *log*. The
    head is resolved off the working directory (a planted ``uv.exe`` in the
    user's clone must not run). Bytes mode: nothing is decoded here."""
    from nexus.util.stable_cwd import detached_spawn_kwargs  # noqa: PLC0415 — stdlib-only helper, deferred

    placed = detached_spawn_kwargs(env)
    cwd = placed["cwd"]
    child_env = dict(placed.get("env", env))
    if os.name != "nt":
        return subprocess.Popen(  # noqa: S603 — fixed argv, resolved head
            argv_off_cwd(list(argv)), stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, cwd=cwd, env=child_env,
            start_new_session=True,
        )
    # CREATE_BREAKAWAY_FROM_JOB so a job the caller runs in (an installer, a CI
    # shell) does not end the prune with it; a job that forbids breakaway
    # refuses the flag, and then it starts without (as the session-end
    # launcher does).
    create_no_window, new_group, breakaway = 0x08000000, 0x00000200, 0x01000000
    try:
        return subprocess.Popen(  # noqa: S603 — fixed argv, resolved head
            argv_off_cwd(list(argv)), stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, cwd=cwd, env=child_env,
            creationflags=create_no_window | new_group | breakaway,
        )
    except OSError:
        return subprocess.Popen(  # noqa: S603 — fixed argv, resolved head
            argv_off_cwd(list(argv)), stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, cwd=cwd, env=child_env,
            creationflags=create_no_window | new_group,
        )
