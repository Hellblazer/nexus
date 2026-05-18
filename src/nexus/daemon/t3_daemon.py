# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-112 P1.5.1 (nexus-s3dm) — T3 daemon lifecycle.

The T3 "daemon" is a managed ``chroma run`` subprocess. chromadb's bundled
HTTP server (``chroma run``) is production-quality (RDR-112 §A1, verified
2026-05-13) and is the RPC layer; this module owns process lifecycle and
the on-disk discovery file at ``~/.config/nexus/t3_addr.<uid>``.

Local-mode only. Cloud mode (NX_LOCAL=0) raises ``T3CloudModeError`` —
chromadb's CloudClient is already HTTP-served, so there is no daemon to
run. Clients in cloud mode connect directly via the CloudClient.

The chroma subprocess is spawned with ``start_new_session=True`` (matches
``start_t1_server`` in ``session.py:459-537``) so SIGTERM at shutdown
reaches the whole process group — chroma's multiprocessing workers and
the resource_tracker child included (nexus-dc57 / nexus-ze2a class).

T1/T3 non-collision invariant (PLAN-AUDIT 2026-05-17): T1 uses
ephemeral tempdirs + ``t1_addr.<claude_pid>``; T3 uses
``nexus.config._default_local_path()`` + ``t3_addr.<uid>``. Both pick
free ports via the OS allocator. Distinct addr-file naming and
distinct chroma --path roots mean two coexisting chroma subprocesses
on the same host do not collide.
"""
from __future__ import annotations

import errno
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

#: chroma listens on loopback only — RDR-113 host-trust invariant. The
#: chroma CLI default is 127.0.0.1; we pass it explicitly for clarity.
_T3_HOST: str = "127.0.0.1"

#: Discovery payload format version. Bump when the shape changes.
_DISCOVERY_FORMAT_VERSION: int = 1

#: How long to wait for the chroma subprocess to begin accepting TCP
#: connections before declaring the start a failure. Local chroma startup
#: is normally <1s on warm caches, ~3s on a cold venv.
_READY_TIMEOUT: float = 10.0

#: After SIGTERM, wait this long before escalating to SIGKILL.
_GRACEFUL_STOP_TIMEOUT: float = 3.0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class T3CloudModeError(RuntimeError):
    """Raised when ``start_t3_daemon`` is invoked in cloud mode.

    In cloud mode chromadb's CloudClient already speaks HTTP to a remote
    service; running ``chroma run`` locally would serve nothing useful.
    Clients in cloud mode bypass the daemon entirely (P1.5.3 / nexus-7yd2
    enforces this on the T3Client factory side).
    """


class T3StartError(RuntimeError):
    """Raised when the chroma subprocess fails to become ready."""


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def t3_discovery_path(config_dir: Path) -> Path:
    """Return the canonical discovery-file path for the T3 daemon.

    Filename is ``t3_addr.<uid>`` — distinct from T2's ``t2_addr.<uid>``
    (T2 daemon owns a separate addr file) and from T1's
    ``t1_addr.<claude_pid>`` (T1 chroma is per-Claude-session and uses a
    different naming key).

    Delegates to ``nexus.daemon.discovery.discovery_path(tier='t3')`` so
    the daemon's WRITE side and the client's READ side derive the path
    from the same single source (nexus-6j2f review S1 fix — was a silent
    drift risk when this module re-derived the path inline).
    """
    from nexus.daemon.discovery import discovery_path as _disc_path
    return _disc_path(config_dir, tier="t3")


def _build_payload(
    *,
    tcp_port: int,
    pid: int,
    local_path: Path,
    daemon_version: str,
) -> dict[str, Any]:
    return {
        "format_version": _DISCOVERY_FORMAT_VERSION,
        "tcp_host": _T3_HOST,
        "tcp_port": tcp_port,
        "pid": pid,
        "daemon_version": daemon_version,
        "start_time": datetime.now(timezone.utc).isoformat(),
        "local_path": str(local_path),
    }


def _write_discovery_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write *path* with 0o600 permissions.

    nexus-6j2f review S1 fix: a prior implementation called
    ``tmp.write_text(...)`` (creating the file with umask-applied mode,
    typically 0o644) and *then* ``os.chmod(0o600)``. In the brief
    window between create and chmod, the tmp file was world-readable
    in a 0o755 ``~/.config/nexus/`` parent directory; on multi-user
    hosts this leaked the PID and TCP port to any local user.

    Fix: create the file at mode 0o600 via ``os.open`` so the file is
    never world-readable on disk. Then write + close + replace.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    body = json.dumps(payload).encode("utf-8")
    # O_CREAT|O_TRUNC|O_WRONLY with mode 0o600 at create time; umask
    # is applied as a SUBSET mask so 0o600 is preserved.
    fd = os.open(
        str(tmp),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        os.write(fd, body)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


def _read_discovery(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("t3_discovery_read_failed", path=str(path), err=str(exc))
        return None


def _pid_is_alive(pid: int) -> bool:
    """Return True iff signalling pid 0 to *pid* succeeds (or hits EPERM —
    process exists but is owned by another uid, treat as alive)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        # ESRCH = no such process; everything else implies the pid exists.
        return exc.errno != errno.ESRCH
    return True


# ---------------------------------------------------------------------------
# Chroma binary location
# ---------------------------------------------------------------------------


def _find_chroma() -> str:
    """Locate the chroma CLI co-installed with this interpreter.

    Mirrors ``nexus.session._find_chroma`` (session.py:442) — chromadb is
    a hard dependency, so the chroma entry-point lives in the same bin
    directory as the Python interpreter. Falls back to a PATH search.
    """
    candidate = Path(sys.executable).parent / "chroma"
    if candidate.is_file():
        return str(candidate)
    import shutil
    found = shutil.which("chroma")
    if not found:
        raise T3StartError(
            "chroma CLI not found alongside Python interpreter or on PATH. "
            "Reinstall nexus to restore the chromadb entry-point."
        )
    return found


# ---------------------------------------------------------------------------
# Lifecycle: start / stop
# ---------------------------------------------------------------------------


def _daemon_version() -> str:
    """Return the conexus package version embedded in discovery payloads."""
    try:
        from importlib.metadata import version
        return version("conexus")
    except Exception:
        return "0.0.0"


def _allocate_free_port() -> int:
    """Bind a free loopback port, then close it. The TOCTOU window between
    close and chroma binding the port is negligible on loopback (same
    rationale as ``start_t1_server`` at session.py:476-484)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((_T3_HOST, 0))
    port: int = sock.getsockname()[1]
    sock.close()
    return port


def _wait_for_ready(
    host: str, port: int, proc: subprocess.Popen[bytes], timeout: float
) -> None:
    """Poll until *host:port* accepts TCP or *proc* exits."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise T3StartError(
                f"chroma run exited with code {proc.returncode} "
                f"before becoming ready on {host}:{port}"
            )
        try:
            conn = socket.create_connection((host, port), timeout=0.5)
            conn.close()
            return
        except OSError:
            time.sleep(0.2)
    proc.kill()
    raise T3StartError(
        f"chroma run on {host}:{port} did not become ready within {timeout:.0f}s"
    )


def start_t3_daemon(*, config_dir: Path, local_path: Path) -> dict[str, Any]:
    """Start the T3 chroma daemon (local mode only). Returns the discovery
    payload that was written to ``t3_discovery_path(config_dir)``.

    Idempotent on a live daemon: if a discovery file exists and its PID is
    still alive, returns the existing payload without spawning a duplicate.

    Raises:
        T3CloudModeError: when ``is_local_mode()`` is False — the cloud
            CloudClient already speaks HTTP; running chroma locally would
            be a no-op.
        T3StartError: when chroma cannot be located or fails to become
            ready within ``_READY_TIMEOUT``.
    """
    from nexus.config import is_local_mode

    if not is_local_mode():
        raise T3CloudModeError(
            "T3 daemon is a no-op in cloud mode. chromadb's CloudClient "
            "is already HTTP-served; there is no local daemon to run. "
            "Set NX_LOCAL=1 to opt into local mode."
        )

    disc_path = t3_discovery_path(config_dir)
    existing = _read_discovery(disc_path)
    if existing is not None:
        existing_pid = existing.get("pid")
        if isinstance(existing_pid, int) and _pid_is_alive(existing_pid):
            _log.info(
                "t3_daemon_already_running",
                pid=existing_pid,
                tcp_port=existing.get("tcp_port"),
            )
            return existing
        # Stale discovery file — log and proceed to spawn a fresh daemon.
        _log.info(
            "t3_daemon_stale_discovery",
            pid=existing_pid,
            path=str(disc_path),
        )

    local_path.mkdir(parents=True, exist_ok=True)
    chroma = _find_chroma()
    port = _allocate_free_port()

    proc = subprocess.Popen(
        [
            chroma, "run",
            "--host", _T3_HOST,
            "--port", str(port),
            "--path", str(local_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # see module docstring
    )

    try:
        _wait_for_ready(_T3_HOST, port, proc, _READY_TIMEOUT)
    except T3StartError:
        # Ready-poll already invoked proc.kill() on timeout; the explicit
        # exit-code branch only raises after proc has terminated. Either
        # way the subprocess is no longer running; clean up the (possibly
        # half-written) discovery file lest a follow-up start trip the
        # stale-detection branch with a confusing payload.
        disc_path.unlink(missing_ok=True)
        raise

    payload = _build_payload(
        tcp_port=port,
        pid=proc.pid,
        local_path=local_path,
        daemon_version=_daemon_version(),
    )
    _write_discovery_atomic(disc_path, payload)
    _log.info(
        "t3_daemon_started",
        pid=proc.pid,
        tcp_port=port,
        local_path=str(local_path),
    )
    return payload


def stop_t3_daemon(*, config_dir: Path) -> int | None:
    """Stop the running T3 daemon by reading the discovery file's PID,
    sending SIGTERM (escalating to SIGKILL after _GRACEFUL_STOP_TIMEOUT),
    and unlinking the discovery file.

    Returns the PID that was signalled (or None when no discovery file
    exists — already stopped). Process-group SIGTERM ensures chroma's
    multiprocessing workers + resource_tracker are signalled too
    (nexus-dc57 / nexus-ze2a class — process group is the correct
    granularity for chroma's subtree).
    """
    from nexus.util.process_group import safe_killpg

    disc_path = t3_discovery_path(config_dir)
    payload = _read_discovery(disc_path)
    if payload is None:
        _log.info("t3_daemon_stop_noop", reason="no_discovery_file")
        return None

    pid = payload.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        _log.warning("t3_daemon_stop_invalid_pid", payload=payload)
        disc_path.unlink(missing_ok=True)
        return None

    if not _pid_is_alive(pid):
        _log.info("t3_daemon_stop_stale_pid", pid=pid)
        disc_path.unlink(missing_ok=True)
        return pid

    # Graceful SIGTERM via process-group → escalate to SIGKILL.
    if safe_killpg(pid, signal.SIGTERM):
        deadline = time.monotonic() + _GRACEFUL_STOP_TIMEOUT
        while time.monotonic() < deadline:
            if not _pid_is_alive(pid):
                break
            time.sleep(0.1)
        if _pid_is_alive(pid):
            safe_killpg(pid, signal.SIGKILL)
            # Reap zombie if it was our child; ignore if not.
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass

    disc_path.unlink(missing_ok=True)
    _log.info("t3_daemon_stopped", pid=pid)
    return pid
