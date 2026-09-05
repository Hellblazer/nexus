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

Pass 3 (T2 [24547]): ``local_embed_activity`` alone was null for every cloud
install (the majority posture) — the engine now also reports an additive
``embedder_activity`` map (one entry per Voyage/CCE embedder, keyed by model
token), and :func:`format_engine_activity_line` falls back to it when
``local_embed_activity`` is null, so the doctor line shows real activity on
cloud too.
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


def _format_activity_fields(activity: dict) -> list[str]:
    """Shared field-formatting for one activity dict (the ``local_embed_activity``
    shape, or one value out of ``embedder_activity``) — both carry the identical
    field set."""
    active = activity.get("active", False)
    chunks_done = activity.get("chunks_done_total", "?")
    rate = activity.get("last_chunks_per_sec", "?")
    age_ms = activity.get("last_activity_age_ms", -1)
    queue_depth = activity.get("queue_depth", -1)
    thread_width = activity.get("thread_width", -1)

    parts = [
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
    return parts


def _pick_busiest_embedder(embedder_activity: dict) -> tuple[str, dict] | None:
    """Pick the most-recently-active entry (smallest ``last_activity_age_ms``)
    out of ``embedder_activity`` — an honest "which embedder should I report on"
    choice when more than one is tracked (cloud mode: voyage-code-3,
    voyage-context-3, voyage-3 can all be present). Entries with no valid age
    sort last; ties keep dict order (Java's LinkedHashMap preserves insertion
    order across the wire)."""
    if not embedder_activity:
        return None

    def _age(item: tuple[str, dict]) -> float:
        age = item[1].get("last_activity_age_ms", -1)
        return age if isinstance(age, (int, float)) and age >= 0 else float("inf")

    return min(embedder_activity.items(), key=_age)


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
    if activity is not None:
        parts = [f"Engine activity: mode={mode}", *_format_activity_fields(activity)]
        return ", ".join(parts)

    # Pass 3 (T2 [24547]): local_embed_activity is null in cloud/voyage mode by
    # design (no local Bge768Embedder to read) -- fall back to embedder_activity,
    # the majority-posture fix, before reporting "nothing tracked".
    embedder_activity = status.get("embedder_activity") or {}
    busiest = _pick_busiest_embedder(embedder_activity)
    if busiest is not None:
        name, entry = busiest
        others = len(embedder_activity) - 1
        parts = [f"Engine activity: mode={mode}", f"embedder={name}", *_format_activity_fields(entry)]
        line = ", ".join(parts)
        if others > 0:
            line += f" (+{others} other embedder(s) tracked)"
        return line

    if mode == "voyage":
        return f"Engine activity: mode={mode} (no embedder activity reported)"
    return f"Engine activity: mode={mode}, no local embed activity reported"
