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

from nexus.session import SESSIONS_DIR as _SESSIONS_DIR  # respects NEXUS_CONFIG_DIR


def _collect_health_data() -> dict[str, Any]:
    """Collect all health card data synchronously (pgHero pattern)."""
    data: dict[str, Any] = {}

    # Health checks from nexus.health
    try:
        from nexus.health import run_health_checks, HealthResult

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
    except Exception:
        data["health_checks"] = []
        data["is_local"] = True
        data["health_ok"] = False

    # T1 sessions
    data["sessions"] = [
        {
            "session_id": s.session_id,
            "host": s.host,
            "port": s.port,
            "pid": s.pid,
            "pid_alive": s.pid_alive,
            "tcp_reachable": s.tcp_reachable,
            "created_at": s.created_at,
        }
        for s in scan_sessions_sync(_SESSIONS_DIR)
    ]
    data["active_sessions"] = sum(1 for s in data["sessions"] if s["pid_alive"])

    # MinerU status
    from nexus.config import nexus_config_dir

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
    cat_db = nexus_config_dir() / "catalog" / ".catalog.db"
    if cat_db.exists():
        mtime = cat_db.stat().st_mtime
        age = time.time() - mtime
        data["catalog"] = {"exists": True, "age_seconds": int(age)}
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


def _collect_aspect_queue_data() -> dict[str, Any]:
    """Return aspect_extraction_queue depth + per-status breakdown.

    Returns ``{"present": False}`` when the T2 database or table is
    missing (pre-RDR-089 install). ``{"present": True, "total": N,
    "by_status": {status: count, ...}, "oldest_pending": iso_str|None,
    "failed_count": N}`` otherwise.
    """
    import sqlite3 as _sqlite3

    from nexus.commands._helpers import default_db_path

    db_path = default_db_path()
    if not db_path.exists():
        return {"present": False}

    try:
        conn = _sqlite3.connect(str(db_path))
    except _sqlite3.Error:
        return {"present": False}

    try:
        has_table = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='aspect_extraction_queue'"
        ).fetchone()
        if not has_table:
            return {"present": False}

        total = conn.execute(
            "SELECT COUNT(*) FROM aspect_extraction_queue"
        ).fetchone()[0]
        by_status_rows = conn.execute(
            "SELECT status, COUNT(*) FROM aspect_extraction_queue "
            "GROUP BY status"
        ).fetchall()
        by_status = {status: int(count) for status, count in by_status_rows}

        oldest = conn.execute(
            "SELECT MIN(enqueued_at) FROM aspect_extraction_queue "
            "WHERE status IN ('pending', 'processing')"
        ).fetchone()
        oldest_pending = oldest[0] if oldest and oldest[0] else None

        failed_count = int(by_status.get("failed", 0))

        return {
            "present": True,
            "total": int(total),
            "by_status": by_status,
            "oldest_pending": oldest_pending,
            "failed_count": failed_count,
        }
    except _sqlite3.Error:
        return {"present": False}
    finally:
        conn.close()


def _age_str(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


@router.get("/health")
async def health_index(request: Request, scope: str = "project"):
    """Panel 2: Sessions & Health — synchronous full render."""
    data = _collect_health_data()
    data["age_str"] = _age_str
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
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "health/_cards.html",
        {"scope": scope, **data},
    )
