# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Bead nexus-s71lr, deliverable 2/3 — client for the engine's ``GET /v1/status``
live embed-activity counters.

The wire-level twin of the ``event=bge768_embed_progress`` / ``event=embed_progress``
log lines: lets ``nx doctor`` (and any other caller) answer "is the engine still
embedding, or has it hung?" by polling one unauthenticated endpoint instead of
tailing logs. Additive route — see ``docs/wire-contract-pending.md``'s
nexus-s71lr entry.

Deliberately fail-closed, never raise: a transport failure, a missing route
(pre-nexus-s71lr engine), or a malformed body all resolve to ``None`` — the
same "unprobeable" contract ``doctor.py``'s existing ``GET /version`` dry-run
gate already uses (see ``_run_trim_telemetry``'s ``serving = None`` fallback).
"""
from __future__ import annotations

import httpx
import structlog

_log = structlog.get_logger(__name__)


def fetch_engine_status(*, timeout: float = 5.0) -> dict | None:
    """Fetch the engine's ``GET /v1/status`` body, or ``None`` on any failure
    (unresolvable endpoint, transport error, non-200, malformed JSON) — never
    raises, mirroring the existing ``GET /version`` probe idiom in
    ``commands/doctor.py``.
    """
    try:
        from nexus.db.service_endpoint import (  # noqa: PLC0415 — deferred to keep CLI startup fast
            resolve_service_endpoint_with_evidence_gate,
        )
        base_url, _token = resolve_service_endpoint_with_evidence_gate()
    except Exception as exc:  # noqa: BLE001 — fail-closed: an unresolvable endpoint is "unprobeable", not a crash
        _log.debug("engine_status_endpoint_unresolvable", error=str(exc))
        return None

    try:
        resp = httpx.get(f"{base_url.rstrip('/')}/v1/status", timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:  # noqa: BLE001 — fail-closed: a transport blip or a pre-nexus-s71lr engine (404) is "unprobeable"
        _log.debug("engine_status_probe_failed", error=str(exc))
        return None

    if not isinstance(body, dict):
        return None
    return body


def format_engine_activity_line(status: dict | None) -> str:
    """Render ``status`` (the ``fetch_engine_status()`` return, or ``None``)
    as a single human-readable line for ``nx doctor`` / a shakedown
    transcript's periodic poll. Never raises — a malformed/partial body
    degrades to the "unknown" line rather than a traceback.
    """
    if status is None:
        return "Engine activity: UNKNOWN (status endpoint unreachable — pre-nexus-s71lr engine, or service down)"

    mode = status.get("embedding_mode", "unknown")
    activity = status.get("local_embed_activity")
    if activity is None:
        if mode == "voyage":
            return f"Engine activity: mode={mode} (cloud-mode counters are not tracked wire-side; see engine logs)"
        return f"Engine activity: mode={mode}, no local embed activity reported"

    active = activity.get("active", False)
    chunks_done = activity.get("chunks_done_total", "?")
    rate = activity.get("last_chunks_per_sec", "?")
    age_ms = activity.get("last_activity_age_ms", -1)
    queue_depth = activity.get("queue_depth", -1)
    thread_width = activity.get("thread_width", -1)

    parts = [
        f"Engine activity: mode={mode}",
        "active" if active else "idle",
        f"chunks_done={chunks_done}",
        f"rate={rate}/s",
    ]
    if age_ms is not None and age_ms >= 0:
        parts.append(f"last_activity={age_ms}ms ago")
    if queue_depth is not None and queue_depth >= 0:
        parts.append(f"queue_depth={queue_depth}")
    if thread_width is not None and thread_width >= 0:
        parts.append(f"thread_width={thread_width}")
    return ", ".join(parts)
