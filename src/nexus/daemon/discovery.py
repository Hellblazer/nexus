# SPDX-License-Identifier: AGPL-3.0-or-later
"""Client-side discovery for the T2 and T3 storage daemons (RDR-120).

Discovery file paths:
- ``<config_dir>/t2_addr.<uid>`` — T2 daemon (memory.db + plan store + ...)
- ``<config_dir>/t3_addr.<uid>`` — T3 daemon (managed ``chroma run``)

Env-var overrides honoured by ``discovery_resolve``:
- T2: ``NX_T2_SOCK`` (UDS path) then ``NX_T2_ADDR`` (host:port)
- T3: ``NX_T3_ADDR`` (host:port) — TCP-only, chromadb upstream constraint

Precedence (RDR-120 C2 contract): env-var wins when set and non-empty;
file is the fallback when env is unset. An env-var pointing at an
unreachable target surfaces the connect-time error at the client — this
module does not silently fall through from a set-but-unreachable env-var
back to the discovery file.

Validation invariants (PID-liveness, shutdown-marker, format_version
forward-incompat refusal, non-dict shape) are shared across tiers via
``_validate_discovery_payload``.

The daemon writes the file atomically (tmpfile + os.replace) so a
partial read is not possible.

Note: P1.A (nexus-41unl) ships the T3 daemon; the T2 daemon ships in
P3a. The T2 branches of this module are inert until then but the
parametric shape avoids a rewrite when T2 arrives.
"""
from __future__ import annotations

import errno
import json
import os
import time
from pathlib import Path
from typing import Any, Literal, Optional

import structlog

from nexus.config import nexus_config_dir

_log = structlog.get_logger(__name__)

Tier = Literal["t2", "t3"]
_VALID_TIERS: tuple[str, ...] = ("t2", "t3")


class DaemonNotRunningError(RuntimeError):
    """Raised when ``discovery_resolve(tier)`` finds neither env-var nor a
    live discovery file. Message embeds a recovery hint naming the
    correct ``nx daemon <tier> start`` invocation.
    """


def discovery_path(
    config_dir: Optional[Path] = None, *, tier: Tier = "t3"
) -> Path:
    """Return the discovery file path for the given tier and current UID.

    ``config_dir`` is first-positional for backward-compat with existing
    callers; ``tier`` is keyword-only with a T3 default since T3 is the
    only tier the daemon-CLI presently ships.
    """
    if tier not in _VALID_TIERS:
        raise ValueError(
            f"unknown tier {tier!r}; expected one of {_VALID_TIERS}"
        )
    cd = config_dir if config_dir is not None else nexus_config_dir()
    return cd / f"{tier}_addr.{os.getuid()}"


def _validate_discovery_payload(
    payload: Any, path: Path, *, tier: Tier
) -> Optional[dict[str, Any]]:
    """Apply the daemon-discovery validation invariants.

    Returns the payload dict on success, or ``None`` when:
    - payload is not a dict
    - format_version > 1 (forward-incompat refusal)
    - status == 'shutting_down' (shutdown marker)
    - pid is missing / invalid / refers to a dead process

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
        # Live process under a different UID; treat as alive and let
        # the eventual connect surface a clearer error.
        pass
    except OSError as exc:
        # Some Linux kernels surface OSError(errno=ESRCH) instead of
        # ProcessLookupError for an unallocated PID. Treat ESRCH
        # identically; treat every other OSError as "process exists,
        # we can't probe".
        if exc.errno == errno.ESRCH:
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


#: The leased-registry record format version this client understands
#: (mirrors ``service_registry._FORMAT_VERSION``; kept local to avoid a
#: lower-level module importing the substrate).
_LEASE_FORMAT_VERSION: int = 1


def is_lease_record(raw: Any) -> bool:
    """True if *raw* is a RDR-149 leased-registry record (vs a legacy
    pid-payload). A lease record carries both ``endpoint`` and
    ``generation``; a legacy payload carries neither."""
    return (
        isinstance(raw, dict)
        and isinstance(raw.get("endpoint"), dict)
        and "generation" in raw
    )


def normalize_discovery_view(raw: Any) -> dict[str, Any]:
    """Flatten a discovery record (lease OR legacy) to a uniform view with
    top-level ``pid`` / ``uds_path`` / ``tcp_host`` / ``tcp_port`` /
    ``daemon_version``, applying NO liveness or freshness filter.

    Reap paths must inspect even a stale / unreachable predecessor's
    record (that is precisely what they reap), so they cannot go through
    the freshness-filtering ``find_t*_daemon``. This pure normalizer gives
    them the legacy-shaped view they expect from either format.
    """
    if not isinstance(raw, dict):
        return {}
    if not is_lease_record(raw):
        return raw  # legacy payload is already top-level
    endpoint = raw["endpoint"]
    return {
        "pid": endpoint.get("pid"),
        "uds_path": endpoint.get("uds_path"),
        "tcp_host": endpoint.get("tcp_host"),
        "tcp_port": endpoint.get("tcp_port"),
        "daemon_version": raw.get("version"),
        "generation": raw.get("generation"),
        "owner_token": raw.get("owner_token"),
        "status": raw.get("status", "live"),
    }


def _resolve_lease_record(
    raw: dict[str, Any], path: Path, *, tier: str
) -> Optional[dict[str, Any]]:
    """Resolve a RDR-149 lease record to a connection dict, or ``None``.

    Liveness is lease freshness (``now - heartbeat_epoch < ttl``), not pid:
    a dead owner's lease simply ages out, giving pid-reuse immunity. A
    stale, shutdown-marked, or forward-incompatible lease is rejected; an
    expired one is best-effort unlinked so the next lookup is fast. The
    endpoint fields (``uds_path`` / ``tcp_host`` / ``tcp_port``) are lifted
    to the top level so the existing client contract
    (``discovery_resolve`` -> connect) is unchanged; the lease metadata
    (``generation`` / ``owner_token`` / ``version``) rides alongside.

    Tier-parameterized so the T3 migration (RDR-149 P3) reuses it verbatim.
    """
    fmt = raw.get("format_version", _LEASE_FORMAT_VERSION)
    if isinstance(fmt, int) and fmt > _LEASE_FORMAT_VERSION:
        _log.warning(
            f"{tier}_discovery_lease_format_too_new",
            path=str(path),
            format_version=fmt,
        )
        return None
    if raw.get("status", "live") != "live":
        _log.info(f"{tier}_discovery_shutdown_marker_seen", path=str(path))
        return None
    endpoint = raw.get("endpoint")
    heartbeat_epoch = raw.get("heartbeat_epoch")
    ttl = raw.get("ttl")
    if (
        not isinstance(endpoint, dict)
        or not isinstance(heartbeat_epoch, (int, float))
        or not isinstance(ttl, (int, float))
    ):
        _log.warning(f"{tier}_discovery_lease_malformed", path=str(path))
        return None
    # Guard a backward clock step (NTP correction): a suspiciously large
    # negative age is treated as stale rather than perpetually fresh.
    age = time.time() - heartbeat_epoch
    if age >= ttl or age <= -ttl:
        _log.warning(f"{tier}_discovery_lease_expired", path=str(path), age=age)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            _log.warning(
                f"{tier}_discovery_unlink_failed", path=str(path), error=str(exc)
            )
        return None
    result = dict(endpoint)
    result["generation"] = raw.get("generation")
    result["owner_token"] = raw.get("owner_token")
    result["version"] = raw.get("version")
    return result


def find_t2_daemon(config_dir: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """Return the T2 daemon's discovery payload, or ``None`` if absent /
    unreadable / stale.

    RDR-149 P2: the T2 record is now a leased registry record
    (``generation`` + ``owner_token`` + ``endpoint`` + TTL-stamped
    ``heartbeat_epoch``). A record carrying those keys is resolved by lease
    freshness; a legacy payload (top-level pid, no ``endpoint``) falls back
    to the shared pid-liveness validator so an in-flight upgrade window is
    still readable.
    """
    path = discovery_path(config_dir, tier="t2")
    raw = _read_payload(path, tier="t2")
    if raw is None:
        return None
    if is_lease_record(raw):
        return _resolve_lease_record(raw, path, tier="t2")
    return _validate_discovery_payload(raw, path, tier="t2")


def find_t3_daemon(config_dir: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """Return the T3 daemon's discovery payload, or ``None`` if absent /
    unreadable / stale. T3 payloads carry ``tcp_host`` + ``tcp_port`` —
    chromadb's bundled HTTP server is TCP-only.

    RDR-149 P3: T3 now rides the leased registry, heartbeated by the
    long-lived T3 supervisor (which only re-stamps the lease while its
    chroma subprocess is alive, so a fresh lease implies a live chroma).
    A lease record is resolved by freshness; a legacy payload (top-level
    pid, no ``endpoint``) falls back to the pid-liveness validator for the
    in-flight upgrade window.
    """
    path = discovery_path(config_dir, tier="t3")
    raw = _read_payload(path, tier="t3")
    if raw is None:
        return None
    if is_lease_record(raw):
        return _resolve_lease_record(raw, path, tier="t3")
    return _validate_discovery_payload(raw, path, tier="t3")


def _parse_host_port(env_value: str, *, env_name: str) -> tuple[str, int]:
    """Parse a ``host:port`` env value. Raises ``ValueError`` on malformed
    input; the env-var contract is explicit, surface the breakage at the
    resolver boundary rather than at connect time."""
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

    Resolution order (RDR-120 C2 precedence):
        1. Env-var first; when set and non-empty, return immediately.
           - T2: ``NX_T2_SOCK`` (UDS path) → ``NX_T2_ADDR`` (host:port).
           - T3: ``NX_T3_ADDR`` (host:port).
        2. File fallback via ``find_t<tier>_daemon``.
        3. ``DaemonNotRunningError`` with a recovery hint.

    An env-var set to an unreachable target does NOT fall through to the
    discovery file; the connect attempt at the client surfaces the
    error. Silent fallthrough would mask operator misconfiguration.

    The returned dict carries a ``source`` key for diagnostics:
    ``'env:NX_T2_SOCK' | 'env:NX_T2_ADDR' | 'env:NX_T3_ADDR' | 'file'``.
    """
    if tier not in _VALID_TIERS:
        raise ValueError(
            f"unknown tier {tier!r}; expected one of {_VALID_TIERS}"
        )

    if tier == "t2":
        sock = os.environ.get("NX_T2_SOCK", "").strip()
        if sock:
            return {"uds_path": sock, "source": "env:NX_T2_SOCK"}
        addr = os.environ.get("NX_T2_ADDR", "").strip()
        if addr:
            host, port = _parse_host_port(addr, env_name="NX_T2_ADDR")
            return {
                "tcp_host": host,
                "tcp_port": port,
                "source": "env:NX_T2_ADDR",
            }

    if tier == "t3":
        addr = os.environ.get("NX_T3_ADDR", "").strip()
        if addr:
            host, port = _parse_host_port(addr, env_name="NX_T3_ADDR")
            return {
                "tcp_host": host,
                "tcp_port": port,
                "source": "env:NX_T3_ADDR",
            }

    finder = find_t2_daemon if tier == "t2" else find_t3_daemon
    payload = finder(config_dir)
    if payload is not None:
        result = dict(payload)
        result["source"] = "file"
        return result

    raise DaemonNotRunningError(
        f"No {tier} daemon discovery resolved. Tried env-var "
        f"({'NX_T2_SOCK / NX_T2_ADDR' if tier == 't2' else 'NX_T3_ADDR'}) "
        f"and discovery file ({discovery_path(config_dir, tier=tier)}). "
        f"Start with: `nx daemon {tier} start`."
    )
