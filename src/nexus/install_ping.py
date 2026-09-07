"""Anonymous daily install ping (nexus-h5olw).

The only signal that counts local-mode installs: once every 24 hours the MCP
server, on a background thread, POSTs a random install id plus the client
version, install mode, OS, arch, and Python minor to the managed service's
unauthenticated ``/v1/install-ping`` route. Nothing else is sent: no tenant,
no hostname, no paths, no collection names.

Opt-out, default on. Any of these disables it:

* ``NX_NO_TELEMETRY=1`` (any non-empty value other than ``0``)
* ``telemetry.enabled: false`` in ``~/.config/nexus/config.yml``
  (``nx telemetry off``)

Every failure is swallowed at debug level. The ping never blocks startup,
never retries, and never raises into the caller; a ping the network drops is
simply a day uncounted.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

import structlog

from nexus import config as _config
from nexus.db.managed_endpoint import DEFAULT_MANAGED_SERVICE_URL

_log = structlog.get_logger(__name__)

#: Env opt-out. ``0`` and empty mean "not set"; anything else disables.
NO_TELEMETRY_ENV = "NX_NO_TELEMETRY"
#: Test / private-deployment override for the ping target.
PING_URL_ENV = "NX_INSTALL_PING_URL"
#: Route on the managed service (unauthenticated; see the engine's InstallPingHandler).
PING_PATH = "/v1/install-ping"
#: Minimum interval between pings.
PING_INTERVAL_S = 24 * 60 * 60
#: Wall-clock budget for the whole request.
PING_TIMEOUT_S = 2.0
#: Files under the nexus config dir.
INSTALL_ID_FILENAME = "install_id"
LAST_PING_FILENAME = "install_ping.last"


def telemetry_enabled() -> bool:
    """Opt-out check: env first, then the config key."""
    env = os.environ.get(NO_TELEMETRY_ENV, "").strip()
    if env and env != "0":
        return False
    return _config_enabled()


def telemetry_status() -> dict[str, Any]:
    """For ``nx telemetry status``: the decision and where it came from."""
    env = os.environ.get(NO_TELEMETRY_ENV, "").strip()
    if env and env != "0":
        return {"enabled": False, "source": f"env {NO_TELEMETRY_ENV}={env}"}
    if not _config_enabled():
        return {"enabled": False, "source": "config telemetry.enabled"}
    return {"enabled": True, "source": "default"}


def _config_enabled() -> bool:
    try:
        section = _config.load_config().get("telemetry")
    except Exception:  # noqa: BLE001 — a broken config never turns the ping on or off loudly
        return True
    if not isinstance(section, dict):
        return True
    value = section.get("enabled", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", "off"}
    return bool(value)


def install_id(config_dir: Path | None = None) -> str:
    """The random per-install UUID, minted on first use and kept in the config dir."""
    path = (config_dir or _config.nexus_config_dir()) / INSTALL_ID_FILENAME
    try:
        existing = path.read_text().strip()
        uuid.UUID(existing)
        return existing
    except (OSError, ValueError):
        pass
    fresh = str(uuid.uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fresh + "\n")
    return fresh


def last_ping_at(config_dir: Path | None = None) -> float | None:
    path = (config_dir or _config.nexus_config_dir()) / LAST_PING_FILENAME
    try:
        return float(path.read_text().strip())
    except (OSError, ValueError):
        return None


def due(now: float, config_dir: Path | None = None) -> bool:
    last = last_ping_at(config_dir)
    return last is None or now - last >= PING_INTERVAL_S


def _mark_pinged(now: float, config_dir: Path | None) -> None:
    path = (config_dir or _config.nexus_config_dir()) / LAST_PING_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{now:.0f}\n")


def install_mode() -> str:
    """``cloud`` when a managed service_url is configured, else ``local``.

    Mirrors ``nx init``'s own dispatch (``get_credential("service_url")``),
    not ``is_local_mode()``, which is service_url-blind.
    """
    return "cloud" if _config.get_credential("service_url") else "local"


def client_version() -> str:
    from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415 — startup cost

    try:
        return version("conexus")
    except PackageNotFoundError:
        return "0.0.0"


def build_payload(config_dir: Path | None = None) -> dict[str, str]:
    """Exactly the six fields the engine accepts. Nothing else is ever added here."""
    return {
        "install_id": install_id(config_dir),
        "client_version": client_version(),
        "mode": install_mode(),
        "os": sys.platform,
        "arch": platform.machine() or "unknown",
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
    }


def ping_url() -> str:
    override = os.environ.get(PING_URL_ENV, "").strip()
    return override or DEFAULT_MANAGED_SERVICE_URL.rstrip("/") + PING_PATH


def send_ping(payload: dict[str, str], url: str | None = None, timeout: float = PING_TIMEOUT_S) -> bool:
    """One POST. True on any 2xx. Never raises."""
    target = url or ping_url()
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        target, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": f"conexus/{payload.get('client_version', '?')}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — https to the managed service
            ok = 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError) as e:
        _log.debug("install_ping_failed", url=target, error=str(e))
        return False
    _log.debug("install_ping_sent", url=target, ok=ok)
    return ok


def ping_if_due(now: float | None = None, config_dir: Path | None = None) -> bool:
    """The whole decision: opt-out, 24h throttle, send, mark. Returns True if sent."""
    if not telemetry_enabled():
        return False
    ts = time.time() if now is None else now
    if not due(ts, config_dir):
        return False
    try:
        sent = send_ping(build_payload(config_dir))
    except Exception as e:  # noqa: BLE001 — a telemetry beacon never surfaces
        _log.debug("install_ping_error", error=str(e))
        return False
    if sent:
        _mark_pinged(ts, config_dir)
    return sent


def ping_in_background(target: Callable[[], Any] = ping_if_due) -> threading.Thread | None:
    """Fire :func:`ping_if_due` on a daemon thread. Returns the thread, or None when opted out."""
    if not telemetry_enabled():
        return None
    t = threading.Thread(target=target, name="nx-install-ping", daemon=True)
    t.start()
    return t
