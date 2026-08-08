# SPDX-License-Identifier: AGPL-3.0-or-later
"""Panel 2: Sessions & Health — system health dashboard."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from nexus.console.watchers import scan_sessions_sync

router = APIRouter(tags=["health"])

from nexus.config import nexus_config_dir as _nexus_config_dir


def _collect_health_data() -> dict[str, Any]:
    """Collect all health card data synchronously (pgHero pattern)."""
    data: dict[str, Any] = {}

    # Health checks from nexus.health
    try:
        from nexus.health import run_health_checks, HealthResult  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import

        results, is_local = run_health_checks()
        checks = []
        for r in results:
            checks.append({
                "label": r.label,
                "ok": r.ok,
                "detail": r.detail,
                "fix_suggestions": r.fix_suggestions,
            })
        data["health_checks"] = checks
        data["is_local"] = is_local
        data["health_ok"] = all(not (r.fatal and not r.ok) for r in results)
    except Exception:  # noqa: BLE001 — best-effort health probe; degrades to unhealthy default for the route
        data["health_checks"] = []
        data["is_local"] = True
        data["health_ok"] = False

    # T1 sessions (nexus-8zfwv: lease-based, no host/port/pid -- T1 is one
    # shared nexus-service now, not a per-session chroma). expires_in_seconds
    # is computed here (not in the template, which has no clock access) so
    # the template can reuse the same age_str()-shaped formatter.
    now = time.time()
    data["sessions"] = [
        {
            "session_id": s.session_id,
            "fresh": s.fresh,
            "expires_in_seconds": max(0, int(s.expires_at - now)),
        }
        for s in scan_sessions_sync(_nexus_config_dir())
    ]
    data["active_sessions"] = sum(1 for s in data["sessions"] if s["fresh"])

    # MinerU status
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import

    mineru_pid_path = nexus_config_dir() / "mineru.pid"
    if mineru_pid_path.exists():
        try:
            info = json.loads(mineru_pid_path.read_text())
            pid = info.get("pid", 0)
            try:
                os.kill(pid, 0)
                data["mineru"] = {"running": True, "port": info.get("port"), "pid": pid}
            except OSError:
                data["mineru"] = {"running": False, "stale_pid": True}
        except (json.JSONDecodeError, OSError):
            data["mineru"] = {"running": False}
    else:
        data["mineru"] = {"running": False}

    # Catalog status
    # nexus-k0luu: age_seconds is the mtime of the LOCAL .catalog.db cache. On a
    # migrated box that file is frozen, so the age grows without bound and reads
    # as "the catalog has not been touched in weeks" when the authoritative
    # catalog is in PG and current. Marked local-only rather than deleted (it is
    # still a real signal pre-migration) so a reader cannot mistake it for the
    # live catalog's age.
    cat_db = nexus_config_dir() / "catalog" / ".catalog.db"
    if cat_db.exists():
        # nexus-i711w: the catalog is service-backed in every mode; a
        # surviving local .catalog.db is always a frozen migration source.
        mtime = cat_db.stat().st_mtime
        age = time.time() - mtime
        data["catalog"] = {
            "exists": True,
            "age_seconds": int(age),
            "scope": "local-cache-frozen",
        }
    else:
        data["catalog"] = {"exists": False}

    # Index log
    index_log = nexus_config_dir() / "index.log"
    if index_log.exists():
        mtime = index_log.stat().st_mtime
        age = time.time() - mtime
        size_mb = index_log.stat().st_size / (1024 * 1024)
        data["index_log"] = {"exists": True, "age_seconds": int(age), "size_mb": round(size_mb, 1)}
    else:
        data["index_log"] = {"exists": False}

    # Dolt server log
    dolt_log = nexus_config_dir() / "dolt-server.log"
    if dolt_log.exists():
        mtime = dolt_log.stat().st_mtime
        age = time.time() - mtime
        data["dolt_server"] = {"exists": True, "age_seconds": int(age)}
    else:
        data["dolt_server"] = {"exists": False}

    # Aspect-extraction queue (RDR-089 worker depth, nexus-qf48). Mirrors
    # the doctor.py --check-aspect-queue surface (nexus-1pfq) so the
    # console exposes the same observability without invoking the CLI.
    data["aspect_queue"] = _collect_aspect_queue_data()

    return data


def _collect_aspect_queue_data_service() -> dict[str, Any]:
    """Aspect-queue depth from the LIVE PG queue over HTTP (nexus-k0luu).

    ``{"present": True, "backend": "service", "total": N, "failed_count": N}``.

    Two deliberate shape differences from the sqlite collector, both because the
    service list endpoint does not carry the data — declared rather than filled
    with zeros, since a zero here is exactly the false-clean being fixed:
      * ``by_status`` is omitted (no per-status aggregate endpoint).
      * ``oldest_pending`` is None (enqueued_at is not in the projection).

    On a transport error this returns ``{"present": True, "unavailable": True}``
    — NOT ``{"present": False}`` and NOT a zero count. "I could not reach the
    queue" and "the queue is empty" must not render identically.
    """
    import httpx  # noqa: PLC0415 — deliberate deferred import: branch-local / startup-cost avoidance

    from nexus.db.t2.http_aspect_queue import HttpAspectQueue  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import

    try:
        q = HttpAspectQueue()
        pending = q.pending_count()
        failed = len(q.list_failed())
    except (httpx.HTTPError, RuntimeError) as exc:
        return {"present": True, "backend": "service", "unavailable": True,
                "error": str(exc)[:200]}
    return {
        "present": True,
        "backend": "service",
        "total": pending + failed,
        "pending": pending,
        "failed_count": failed,
        "oldest_pending": None,
    }


def _collect_aspect_queue_data() -> dict[str, Any]:
    """Return aspect_extraction_queue depth + per-status breakdown.

    nexus-k0luu: reads the SERVICE queue (PG) — the console used to read
    the frozen SQLite queue on a migrated box and report it as current
    (false-clean, second surface of the doctor fix). The SQLite reader
    leg died with the =sqlite opt-out (RDR-158 P3, nexus-7bomn); the
    resolver call below is validation only, so a stranded =sqlite export
    hard-errors on this route rather than being silently ignored (the
    service collector constructs HttpAspectQueue directly, bypassing
    T2Database's validation seam).
    """
    from nexus.db.storage_mode import storage_backend_for  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import

    storage_backend_for("aspect_queue")
    return _collect_aspect_queue_data_service()


def _age_str(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def _in_str(seconds: int) -> str:
    """Format a forward-looking duration (T1 lease expiry), mirroring
    ``_age_str``'s backward-looking one."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


@router.get("/health")
async def health_index(request: Request, scope: str = "project"):
    """Panel 2: Sessions & Health — synchronous full render."""
    data = _collect_health_data()
    data["age_str"] = _age_str
    data["in_str"] = _in_str
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "health/index.html",
        {"scope": scope, "active_panel": "health", **data},
    )


@router.get("/health/refresh")
async def health_refresh(request: Request, scope: str = "project"):
    """Manual refresh — returns HTMX partial."""
    data = _collect_health_data()
    data["age_str"] = _age_str
    data["in_str"] = _in_str
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "health/_cards.html",
        {"scope": scope, **data},
    )
