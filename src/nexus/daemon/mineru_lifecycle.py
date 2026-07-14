# SPDX-License-Identifier: AGPL-3.0-or-later
"""MinerU on-demand spawn guard on the RDR-149 election primitive (nexus-1qdb9).

Before this module the MinerU API server had start/stop verbs and pid-file
discovery but NO lifecycle: killed/crashed/never-started, the PDF pipeline
silently degraded to the in-process fallback (OOM-risk on math PDFs) until
a human noticed a doctor warning and ran ``nx mineru start`` — ambient
machine state, the class the gate doctrine bans. Live incident 2026-07-14:
the server was down for hours across an upgrade with only the fallback
carrying math PDFs.

**Scope honesty (critique a29348b4):** this is a single-writer spawn guard
plus policy gates — NOT full RDR-149 lease membership. mineru-api is an
external FastAPI binary that cannot heartbeat a nexus lease itself; real
membership (publish/heartbeat, conformance-TIERS, pid-reuse immunity)
needs a supervisor design and is tracked separately. Liveness here is
pid-based (``os.kill(pid, 0)``), the model the leased tiers left behind.

:func:`ensure_mineru_running` is the choke point the PDF pipeline calls
when it routes a document to MinerU and finds the server unreachable:

- **Policy gates** (:func:`spawn_policy_allows`, shared by EVERY automatic
  spawn trigger — nexus-c7odl): ``pdf.mineru_autostart`` (default True;
  operators who manage the server out-of-band set it False), overridable
  by ``NX_MINERU_AUTOSTART`` (0/false/no/off disable, anything else
  enables); and the remote-intent guard — an explicit non-local
  ``pdf.mineru_server_url`` is operator intent (RDR-148 Gap 1); we never
  spawn a local server to shadow it.
- **Race-free spawn** — the check-then-spawn critical section runs under
  the RDR-149 substrate's election flock (``ServiceRegistry.election``,
  tier ``mineru``) so concurrent PDF ops elect exactly one spawner; the
  pid file is written inside the section, so losers see the claim and
  skip. Health is awaited OUTSIDE the lock (first-start model downloads
  can take minutes; electors must not starve).
- **Shared warm-up budget** (nexus-m45o6) — the spawner stamps a warming
  marker; every subsequent caller's wait is bounded by ``marker_ts +
  wait_healthy_s``, so a >2-minute first-start warm-up costs the BATCH
  at most one budget, not one budget per document.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import structlog

_log = structlog.get_logger(__name__)

MINERU_TIER = "mineru"

#: Spawn-guard election scope (one server per config dir).
_SPAWN_SCOPE = "spawn"

#: How long ensure() waits for a freshly-spawned server to pass /health.
#: First-ever start can download models (~2-3 GB) and blow any sane bound —
#: in that case ensure() returns None (this document falls back) and the
#: server finishes warming for the NEXT one. The budget is SHARED across
#: callers via the warming marker (nexus-m45o6): later documents wait only
#: the remainder, never a fresh 120s each.
_ENSURE_HEALTH_WAIT_S = 120.0

_HEALTH_POLL_S = 2.0

#: NX_MINERU_AUTOSTART values that disable autostart (review H1: mirror the
#: NX_ASPECT_WORKER_AUTOSTART allow-list so a stray "false" in a shell
#: disables rather than silently force-enabling past an explicit config).
_ENV_DISABLE = ("0", "false", "False", "no", "off")


def _healthy(url: str) -> bool:
    try:
        return httpx.get(f"{url}/health", timeout=2).status_code == 200
    except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError):
        return False


def _is_local(url: str) -> bool:
    from urllib.parse import urlparse  # noqa: PLC0415 — stdlib, deferred

    host = urlparse(url).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1")


def _warming_marker_path() -> Path:
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred import

    return nexus_config_dir() / "mineru_warming.json"


def _read_warming_marker() -> dict | None:
    try:
        return json.loads(_warming_marker_path().read_text())
    except (OSError, ValueError):
        return None


def _stamp_warming_marker(pid: int) -> None:
    try:
        _warming_marker_path().write_text(
            json.dumps({"pid": pid, "ts": time.time()}),
        )
    except OSError:
        pass  # marker is an optimization; never let it break the spawn


def _clear_warming_marker() -> None:
    try:
        _warming_marker_path().unlink(missing_ok=True)
    except OSError:
        pass


def spawn_policy_allows(url: str | None = None) -> bool:
    """Policy gate shared by every automatic MinerU spawn trigger.

    nexus-c7odl: the config gate and remote-intent guard must hold at
    EVERY trigger (the on-demand ensure, the crash-restart in
    ``pdf_extractor``), or an operator who set ``mineru_autostart: false``
    still gets local spawns via the ungated paths. ``nx mineru start`` is
    the explicit operator verb and is deliberately NOT gated.
    """
    from nexus.config import get_mineru_server_url, get_pdf_config  # noqa: PLC0415 — deferred import

    env_override = os.environ.get("NX_MINERU_AUTOSTART", "").strip()
    if env_override:
        enabled = env_override not in _ENV_DISABLE
    else:
        enabled = get_pdf_config().mineru_autostart
    if not enabled:
        _log.info("mineru_autostart_disabled")
        return False
    url = url if url is not None else get_mineru_server_url()
    if not _is_local(url):
        # Explicit remote operator intent — never shadow it with a local
        # spawn (RDR-148 Gap 1's precedence, applied to the spawn side).
        _log.info("mineru_remote_url_no_autostart", url=url)
        return False
    return True


def ensure_mineru_running(
    *,
    wait_healthy_s: float = _ENSURE_HEALTH_WAIT_S,
) -> str | None:
    """Return a healthy MinerU server URL, spawning one if permitted.

    ``None`` means "no server available" — the caller degrades exactly as
    it always has (the in-process fallback); this function only ever adds
    the recovery path, never a new failure mode.
    """
    from nexus.config import get_mineru_server_url  # noqa: PLC0415 — deferred: circular-dep avoidance

    url = get_mineru_server_url()
    if _healthy(url):
        _clear_warming_marker()
        return url

    if not spawn_policy_allows(url):
        return None

    proc = None
    try:
        from nexus._mineru_pid import (  # noqa: PLC0415 — deferred import
            is_process_alive,
            read_pid_file,
        )
        from nexus._mineru_spawn import (  # noqa: PLC0415 — deferred import
            _find_free_port,
            spawn_server_process,
        )
        from nexus.config import get_mineru_configured_fixed_port  # noqa: PLC0415 — deferred import
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred import
        from nexus.daemon.service_registry import ServiceRegistry  # noqa: PLC0415 — deferred import

        registry = ServiceRegistry(dir=nexus_config_dir(), tier=MINERU_TIER)
        spawned = False
        with registry.election(_SPAWN_SCOPE):
            # Double-check inside the critical section: a concurrent
            # elector may have spawned while we waited on the flock (its
            # pid file is written before health passes, so liveness — not
            # health — is the in-flight signal).
            info = read_pid_file()
            if info is not None and is_process_alive(info["pid"]):
                pass  # someone's server is up or warming — just wait below
            else:
                port = get_mineru_configured_fixed_port() or _find_free_port()
                proc = spawn_server_process(port)
                if proc is None:
                    _log.warning("mineru_autostart_binary_missing")
                    return None
                spawned = True
                _stamp_warming_marker(proc.pid)
                _log.info(
                    "mineru_autostarted", pid=proc.pid, port=port,
                )
    except Exception:  # noqa: BLE001 — the lifecycle must never break PDF extraction; the fallback path remains
        _log.warning("mineru_autostart_failed", exc_info=True)
        return None

    # Await health OUTSIDE the election (model warm-up can be slow).
    deadline = time.monotonic() + wait_healthy_s
    if not spawned:
        # nexus-m45o6: share the spawner's warm-up budget. Without this,
        # every per-document PDFExtractor re-enters and re-waits a full
        # fresh budget behind a >2-min warm-up — the stall multiplies
        # across the batch instead of being paid once.
        marker = _read_warming_marker()
        if marker is not None:
            current = read_pid_file()
            if current is None or current.get("pid") != marker.get("pid"):
                # Marker from a dead/replaced attempt — it must not cap the
                # wait for a DIFFERENT live server (e.g. an operator's
                # manual `nx mineru start`, which stamps no marker).
                _clear_warming_marker()
                marker = None
        if marker is not None:
            remaining = float(marker.get("ts", 0)) + wait_healthy_s - time.time()
            if remaining <= 0:
                _log.warning(
                    "mineru_warmup_budget_exhausted",
                    marker_age_s=round(time.time() - float(marker.get("ts", 0))),
                    note="server still warming; this document falls back without re-waiting",
                )
                return None
            deadline = min(deadline, time.monotonic() + remaining)

    while time.monotonic() < deadline:
        url = get_mineru_server_url()  # re-resolve: pid file has the port
        if _healthy(url):
            _clear_warming_marker()
            _log.info("mineru_ensure_healthy", url=url, spawned=spawned)
            return url
        if proc is not None and proc.poll() is not None:
            # Review H2: fail FAST when our child dies (e.g. fixed port in
            # use) instead of burning the full budget per document.
            _log.warning(
                "mineru_autostart_process_died",
                returncode=proc.returncode,
                note="check the mineru_server child log; port may be in use",
            )
            from nexus._mineru_pid import _pid_file_path, read_pid_file  # noqa: PLC0415 — deferred import

            info = read_pid_file()
            if info is not None and info.get("pid") == proc.pid:
                _pid_file_path().unlink(missing_ok=True)
            _clear_warming_marker()
            return None
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
