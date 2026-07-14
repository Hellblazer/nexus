# SPDX-License-Identifier: AGPL-3.0-or-later
"""MinerU on-demand lifecycle — RDR-149 substrate membership (nexus-1qdb9).

Before this module the MinerU API server had start/stop verbs and pid-file
discovery but NO lifecycle: killed/crashed/never-started, the PDF pipeline
silently degraded to the in-process fallback (OOM-risk on math PDFs) until
a human noticed a doctor warning and ran ``nx mineru start`` — ambient
machine state, the class the gate doctrine bans. Live incident 2026-07-14:
the server was down for hours across an upgrade with only the fallback
carrying math PDFs.

:func:`ensure_mineru_running` is the choke point the PDF pipeline calls
when it routes a document to MinerU and finds the server unreachable:

- **Config gate** — ``pdf.mineru_autostart`` (default True). Operators
  who manage the server out-of-band set it False.
- **Remote-intent guard** — an explicit non-local ``pdf.mineru_server_url``
  is operator intent (RDR-148 Gap 1); we never spawn a local server to
  shadow it.
- **Race-free spawn** — the check-then-spawn critical section runs under
  the RDR-149 substrate's election flock (``ServiceRegistry.election``,
  tier ``mineru``) so concurrent PDF ops elect exactly one spawner; the
  pid file is written inside the section, so losers see the claim and
  skip. Health is awaited OUTSIDE the lock (first-start model downloads
  can take minutes; electors must not starve).
"""
from __future__ import annotations

import time

import httpx
import structlog

_log = structlog.get_logger(__name__)

MINERU_TIER = "mineru"

#: Spawn-guard election scope (one server per config dir).
_SPAWN_SCOPE = "spawn"

#: How long ensure() waits for a freshly-spawned server to pass /health.
#: First-ever start can download models (~2-3 GB) and blow any sane bound —
#: in that case ensure() returns None (this document falls back) and the
#: server finishes warming for the NEXT one.
_ENSURE_HEALTH_WAIT_S = 120.0

_HEALTH_POLL_S = 2.0


def _healthy(url: str) -> bool:
    try:
        return httpx.get(f"{url}/health", timeout=2).status_code == 200
    except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError):
        return False


def _is_local(url: str) -> bool:
    from urllib.parse import urlparse  # noqa: PLC0415 — stdlib, deferred

    host = urlparse(url).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1")


def ensure_mineru_running(
    *,
    wait_healthy_s: float = _ENSURE_HEALTH_WAIT_S,
) -> str | None:
    """Return a healthy MinerU server URL, spawning one if permitted.

    ``None`` means "no server available" — the caller degrades exactly as
    it always has (the in-process fallback); this function only ever adds
    the recovery path, never a new failure mode.
    """
    from nexus.config import get_mineru_server_url, get_pdf_config  # noqa: PLC0415 — deferred: circular-dep avoidance

    url = get_mineru_server_url()
    if _healthy(url):
        return url

    # Env override > config (NX_MINERU_AUTOSTART=0/1). The test suite pins
    # 0 suite-wide (tests/conftest.py) — an unpatched unit test must NEVER
    # spawn a real server (it did, 2026-07-14: four strays from one run).
    import os  # noqa: PLC0415 — stdlib, deferred

    env_override = os.environ.get("NX_MINERU_AUTOSTART", "")
    if env_override == "0" or (
        not env_override and not get_pdf_config().mineru_autostart
    ):
        _log.info("mineru_autostart_disabled", url=url)
        return None
    if not _is_local(url):
        # Explicit remote operator intent — never shadow it with a local
        # spawn (RDR-148 Gap 1's precedence, applied to the spawn side).
        _log.info("mineru_remote_url_no_autostart", url=url)
        return None

    try:
        from nexus.commands.mineru import (  # noqa: PLC0415 — deferred: the spawn core lives beside its helpers
            _is_process_alive,
            _read_pid_file,
            spawn_server_process,
        )
        from nexus.config import get_mineru_configured_fixed_port  # noqa: PLC0415 — deferred import
        from nexus.daemon.service_registry import ServiceRegistry  # noqa: PLC0415 — deferred import
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred import

        registry = ServiceRegistry(dir=nexus_config_dir(), tier=MINERU_TIER)
        spawned = False
        with registry.election(_SPAWN_SCOPE):
            # Double-check inside the critical section: a concurrent
            # elector may have spawned while we waited on the flock (its
            # pid file is written before health passes, so liveness — not
            # health — is the in-flight signal).
            info = _read_pid_file()
            if info is not None and _is_process_alive(info["pid"]):
                pass  # someone's server is up or warming — just wait below
            else:
                from nexus.commands.mineru import _find_free_port  # noqa: PLC0415 — deferred import

                port = get_mineru_configured_fixed_port() or _find_free_port()
                proc = spawn_server_process(port)
                if proc is None:
                    _log.warning("mineru_autostart_binary_missing")
                    return None
                spawned = True
                _log.info(
                    "mineru_autostarted", pid=proc.pid, port=port,
                )
    except Exception:  # noqa: BLE001 — the lifecycle must never break PDF extraction; the fallback path remains
        _log.warning("mineru_autostart_failed", exc_info=True)
        return None

    # Await health OUTSIDE the election (model warm-up can be slow).
    deadline = time.monotonic() + wait_healthy_s
    while time.monotonic() < deadline:
        url = get_mineru_server_url()  # re-resolve: pid file has the port
        if _healthy(url):
            _log.info("mineru_ensure_healthy", url=url, spawned=spawned)
            return url
        time.sleep(_HEALTH_POLL_S)
    _log.warning(
        "mineru_ensure_health_timeout",
        waited_s=wait_healthy_s, spawned=spawned,
        note=(
            "server may still be warming (first start downloads models); "
            "this document falls back, the next one re-checks"
        ),
    )
    return None
