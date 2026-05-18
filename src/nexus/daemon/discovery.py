# SPDX-License-Identifier: AGPL-3.0-or-later
"""Client-side discovery helper for the T2 and T3 daemons.

RDR-112 P1.5.2 (nexus-n8xg): generalised from T2-only to tier-parametric
across ``t2`` and ``t3``. The validation invariants (PID-liveness,
shutdown-marker, format_version-too-new, non-dict-shape) are shared
across tiers via ``_validate_discovery_payload``.

Discovery file paths:
- ``<config_dir>/t2_addr.<uid>`` — T2 daemon (memory.db + tuples.db)
- ``<config_dir>/t3_addr.<uid>`` — T3 daemon (chroma run subprocess)

Env-var overrides honoured by ``discovery_resolve``:
- T2: ``NX_T2_SOCK`` (UDS path) then ``NX_T2_ADDR`` (host:port)
- T3: ``NX_T3_ADDR`` (host:port) — TCP-only per RDR-112 §Approach §2

The daemon writes the file atomically (tmpfile + os.replace) so a
partial read is not possible.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal, Optional

import structlog

from nexus.config import nexus_config_dir

_log = structlog.get_logger(__name__)

Tier = Literal["t2", "t3"]
_VALID_TIERS: tuple[str, ...] = ("t2", "t3")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DaemonNotRunningError(RuntimeError):
    """Raised when ``discovery_resolve(tier)`` finds neither env-var nor a
    live discovery file. Message includes a recovery hint naming the
    correct ``nx daemon <tier> start`` invocation.
    """


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def discovery_path(
    config_dir: Optional[Path] = None, *, tier: Tier = "t2"
) -> Path:
    """Return the discovery file path for the given tier and current UID.

    Signature note: ``config_dir`` is first-positional to preserve the
    pre-existing callers (``discovery_path(tmp_path)`` etc.). ``tier`` is
    keyword-only with a default of ``"t2"`` so legacy callers continue
    to receive the T2 path.
    """
    if tier not in _VALID_TIERS:
        raise ValueError(
            f"unknown tier {tier!r}; expected one of {_VALID_TIERS}"
        )
    cd = config_dir if config_dir is not None else nexus_config_dir()
    return cd / f"{tier}_addr.{os.getuid()}"


# ---------------------------------------------------------------------------
# Shared payload validation (PID-liveness, shutdown-marker, format check)
# ---------------------------------------------------------------------------


def _validate_discovery_payload(
    payload: Any, path: Path, *, tier: Tier
) -> Optional[dict[str, Any]]:
    """Apply the daemon-discovery validation invariants.

    Returns the payload dict on success, or ``None`` when:
    - payload is not a dict (nexus-26b7 dim-5 N1)
    - format_version > 1 (nexus-26b7 dim-13 N-4 — forward-incompat refusal)
    - status == 'shutting_down' (nexus-2kld.2 HR-2 — shutdown marker)
    - pid is missing / invalid / refers to a dead process (nexus-j6dj)

    Side-effect: a stale-PID file is best-effort unlinked so the next
    check is fast. Logs are tagged with the tier.
    """
    if not isinstance(payload, dict):
        _log.warning(
            f"{tier}_discovery_unexpected_shape",
            path=str(path),
            type=type(payload).__name__,
        )
        return None

    discovery_format = payload.get("format_version", 1)
    if isinstance(discovery_format, int) and discovery_format > 1:
        _log.warning(
            f"{tier}_discovery_format_too_new",
            path=str(path),
            format_version=discovery_format,
        )
        return None

    if payload.get("status") == "shutting_down":
        _log.info(
            f"{tier}_discovery_shutdown_marker_seen",
            path=str(path),
            shutdown_at=payload.get("shutdown_at"),
        )
        return None

    pid = payload.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        _log.warning(
            f"{tier}_discovery_invalid_pid", path=str(path), pid=repr(pid)
        )
        return None

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        _log.warning(f"{tier}_discovery_stale_pid", path=str(path), pid=pid)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            _log.warning(
                f"{tier}_discovery_unlink_failed",
                path=str(path),
                error=str(exc),
            )
        return None
    except PermissionError:
        # Live process under a different UID — treat as alive and let
        # the eventual connect surface a clearer error than we can here.
        pass
    except OSError as exc:
        # nexus-6j2f review S2 fix: on some Linux kernels, ``os.kill(pid, 0)``
        # surfaces ``OSError(errno=ESRCH)`` instead of ``ProcessLookupError``
        # for an unallocated PID. Treat ESRCH identically to
        # ProcessLookupError; treat every other OSError (EINVAL, EFAULT, etc.)
        # as "process exists, we can't probe" — matches the ``_pid_is_alive``
        # behavior in t3_daemon.py:147-151.
        import errno as _errno
        if exc.errno == _errno.ESRCH:
            _log.warning(f"{tier}_discovery_stale_pid", path=str(path), pid=pid)
            try:
                path.unlink(missing_ok=True)
            except OSError as unlink_exc:
                _log.warning(
                    f"{tier}_discovery_unlink_failed",
                    path=str(path),
                    error=str(unlink_exc),
                )
            return None
        # Unprobable but exists: treat as alive.

    return payload


def _read_payload(path: Path, *, tier: Tier) -> Optional[Any]:
    """Read + parse the discovery file. Returns the raw JSON or None."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning(f"{tier}_discovery_read_failed", path=str(path), error=str(exc))
        return None


# ---------------------------------------------------------------------------
# Per-tier find_*_daemon
# ---------------------------------------------------------------------------


def find_t2_daemon(config_dir: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """Return the T2 daemon's discovery payload, or ``None`` if absent /
    unreadable / stale.

    See ``_validate_discovery_payload`` for the validation invariants.
    Public signature preserved for backward-compat with existing callers
    in ``nexus.mcp.core``, ``nexus.cockpit.hook_bridge``,
    ``nexus.commands.cockpit``, and the test suite.
    """
    path = discovery_path(config_dir, tier="t2")
    raw = _read_payload(path, tier="t2")
    if raw is None:
        return None
    return _validate_discovery_payload(raw, path, tier="t2")


def find_t3_daemon(config_dir: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """Return the T3 daemon's discovery payload, or ``None`` if absent /
    unreadable / stale. Mirrors ``find_t2_daemon`` exactly but reads the
    T3 discovery file. T3 payloads carry ``tcp_host`` + ``tcp_port`` (no
    ``uds_path`` — chromadb 1.4.4's bundled HTTP server is TCP-only,
    RDR-112 §Approach §2).
    """
    path = discovery_path(config_dir, tier="t3")
    raw = _read_payload(path, tier="t3")
    if raw is None:
        return None
    return _validate_discovery_payload(raw, path, tier="t3")


# ---------------------------------------------------------------------------
# Unified resolver — env-first, file-fallback
# ---------------------------------------------------------------------------


def _parse_host_port(env_value: str, *, env_name: str) -> tuple[str, int]:
    """Parse a ``host:port`` env value. Raises ``ValueError`` on malformed
    input — the env-var contract is explicit; surface the breakage at the
    resolver boundary, not at connect time."""
    if ":" not in env_value:
        raise ValueError(
            f"{env_name}={env_value!r} is malformed; expected 'host:port'."
        )
    host, _, port_str = env_value.rpartition(":")
    try:
        port = int(port_str)
    except ValueError as exc:
        raise ValueError(
            f"{env_name}={env_value!r} has non-integer port {port_str!r}."
        ) from exc
    return host, port


def discovery_resolve(
    tier: Tier, *, config_dir: Optional[Path] = None
) -> dict[str, Any]:
    """Resolve the daemon-connection target for ``tier``.

    Resolution order:
        1. Honour env-var first:
           - T2: ``NX_T2_SOCK`` (UDS path) → ``NX_T2_ADDR`` (host:port).
           - T3: ``NX_T3_ADDR`` (host:port).
        2. Fall back to the discovery file via ``find_t<tier>_daemon``.
        3. Raise ``DaemonNotRunningError`` with a recovery hint.

    The returned dict always carries a ``source`` key indicating the
    resolution path: ``'env:NX_T2_SOCK' | 'env:NX_T2_ADDR' | 'env:NX_T3_ADDR'``
    or ``'file'``. Callers do not need to inspect ``source``; it exists
    for diagnostics and the daemon-mode-hint test surface.

    Args:
        tier: ``'t2'`` or ``'t3'``.
        config_dir: Optional override for the file-fallback path.

    Returns:
        The discovery payload merged with a ``source`` annotation.

    Raises:
        ValueError: malformed env-var (e.g. NX_T3_ADDR missing ':port').
        DaemonNotRunningError: neither env-var nor a live discovery file
            resolves; recovery hint embedded in the message.
    """
    if tier not in _VALID_TIERS:
        raise ValueError(
            f"unknown tier {tier!r}; expected one of {_VALID_TIERS}"
        )

    # --- T2: NX_T2_SOCK first (UDS), then NX_T2_ADDR (TCP) ---
    if tier == "t2":
        sock = os.environ.get("NX_T2_SOCK", "").strip()
        if sock:
            return {
                "uds_path": sock,
                "source": "env:NX_T2_SOCK",
            }
        addr = os.environ.get("NX_T2_ADDR", "").strip()
        if addr:
            host, port = _parse_host_port(addr, env_name="NX_T2_ADDR")
            return {
                "tcp_host": host,
                "tcp_port": port,
                "source": "env:NX_T2_ADDR",
            }

    # --- T3: NX_T3_ADDR (TCP only — chromadb upstream constraint) ---
    if tier == "t3":
        addr = os.environ.get("NX_T3_ADDR", "").strip()
        if addr:
            host, port = _parse_host_port(addr, env_name="NX_T3_ADDR")
            return {
                "tcp_host": host,
                "tcp_port": port,
                "source": "env:NX_T3_ADDR",
            }

    # --- File fallback ---
    finder = find_t2_daemon if tier == "t2" else find_t3_daemon
    payload = finder(config_dir)
    if payload is not None:
        # Annotate the source for diagnostics; do not mutate the caller's
        # view of the on-disk payload (copy first).
        result = dict(payload)
        result["source"] = "file"
        return result

    raise DaemonNotRunningError(
        f"No {tier} daemon discovery resolved. Tried env-var "
        f"({'NX_T2_SOCK / NX_T2_ADDR' if tier == 't2' else 'NX_T3_ADDR'}) "
        f"and discovery file ({discovery_path(config_dir, tier=tier)}). "
        f"Start with: `nx daemon {tier} start`."
    )
