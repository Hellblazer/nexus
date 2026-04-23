# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""T1 ``plans__session`` semantic cache — RDR-078 P1 (SC-2).

:class:`PlanSessionCache` wraps a ChromaDB client (HTTP via the T1
session server in production; ``EphemeralClient`` in tests) and owns a
``plans__session`` collection. At :func:`nexus.hooks.session_start` the
cache is fully rebuilt from every row in :class:`~nexus.db.t2.
plan_library.PlanLibrary` that :meth:`~nexus.db.t2.plan_library.
PlanLibrary.list_active_plans` returns — meaning ``outcome='success'``
plus the byte-identical TTL predicate used elsewhere in the library.

The collection is embedded with the same local ONNX MiniLM function
used by :mod:`nexus.db.t1` so the cache works with zero API keys and
no network. Every collection metadata field is primitive; the schema
is flat and small enough that :mod:`nexus.metadata_schema` is not
needed here (the collection is T1, not T3).

Per RDR-078 §Phase 1 "Write visibility": a ``plan_save`` inside the
session calls :meth:`upsert` so the new row is queryable by
:func:`~nexus.plans.matcher.plan_match` immediately, without waiting
for the next SessionStart.
"""
from __future__ import annotations

from typing import Any

import structlog

from nexus.db.local_ef import LocalEmbeddingFunction
from nexus.db.t2.plan_library import PlanLibrary

__all__ = ["PlanSessionCache", "PLANS_COLLECTION", "_synthesize_match_text"]

_log = structlog.get_logger(__name__)

#: Collection name for T1 plan cache. Static — every session uses the
#: same name; the ``EphemeralClient`` (or HTTP server) provides the
#: per-session isolation.
PLANS_COLLECTION: str = "plans__session"


class PlanSessionCache:
    """Cosine-search cache for plan descriptions in the T1 session.

    See module docstring for invariants. The ``session_id`` is recorded
    on every row so co-tenant sessions stay isolated when multiple
    sessions share an HTTP server (production). With
    ``EphemeralClient`` (tests) the client itself is per-instance so
    the filter is a no-op safety net.
    """

    def __init__(self, *, client: Any, session_id: str) -> None:
        self._client = client
        self._session_id = session_id
        self._available = True
        try:
            self._col = client.get_or_create_collection(
                PLANS_COLLECTION,
                embedding_function=LocalEmbeddingFunction(),
            )
        except Exception as exc:
            _log.warning(
                "plan_session_cache_unavailable",
                error=str(exc), error_type=type(exc).__name__,
            )
            self._col = None
            self._available = False

    # ── Protocol: PlanCache ────────────────────────────────────────────

    @property
    def is_available(self) -> bool:
        return self._available and self._col is not None

    def query(self, intent: str, n: int) -> list[tuple[int, float]]:
        """Return ``(plan_id, distance)`` pairs ordered closest-first."""
        if not self.is_available or not intent:
            return []
        try:
            result = self._col.query(
                query_texts=[intent],
                n_results=min(n, 300),
                where={"session_id": self._session_id},
                include=["metadatas", "distances"],
            )
        except Exception:
            _log.warning("plan_session_cache_query_failed", exc_info=True)
            return []

        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        out: list[tuple[int, float]] = []
        for meta, distance in zip(metadatas, distances):
            plan_id = meta.get("plan_id") if meta else None
            if plan_id is None:
                continue
            d = float(distance)
            # Cosine distance is mathematically in [0.0, 2.0] but ChromaDB
            # can return tiny negatives (~1e-7) from FP rounding on exact
            # matches. Clamp so downstream consumers see the documented range.
            # Warn loudly on meaningful out-of-range values (> ~FP noise) so
            # Chroma misconfiguration (e.g. wrong distance metric) becomes
            # observable rather than silently zeroed.
            if d > 2.0 + 1e-3 or d < -1e-3:
                _log.warning(
                    "plan_session_cache_distance_out_of_range",
                    distance=d,
                    plan_id=int(plan_id),
                )
            clamped = max(0.0, min(2.0, d))
            out.append((int(plan_id), clamped))
        return out

    # ── Population + mutation ─────────────────────────────────────────

    def populate(
        self,
        library: PlanLibrary,
        *,
        project: str = "",
    ) -> int:
        """Rebuild the cache from every active plan in *library*.

        Drops prior state first so a SessionStart on a restarted process
        never sees ghost rows from a previous run. Returns the number
        of rows loaded. Rows whose description is empty are skipped —
        they'd embed degenerate and pollute cosine results.
        """
        if not self.is_available:
            return 0

        # Full rebuild: drop anything we've seen before. ``include``
        # omitted so we get just the ids (ids come back unconditionally).
        try:
            existing = self._col.get(where={"session_id": self._session_id})
            ids = existing.get("ids") or []
            if ids:
                self._col.delete(ids=ids)
        except Exception:
            _log.warning("plan_session_cache_reset_failed", exc_info=True)

        rows = library.list_active_plans(project=project)
        loaded = 0
        for row in rows:
            if self._upsert_row(row):
                loaded += 1
        return loaded

    def upsert(self, row: dict[str, Any]) -> bool:
        """Upsert a single plan row. Returns True when the row landed."""
        if not self.is_available:
            return False
        return self._upsert_row(row)

    def remove(self, plan_id: int) -> bool:
        """Drop a stale cache entry for *plan_id*.

        Called from ``plan_match`` when ``library.get_plan(plan_id)``
        returns ``None`` — the plan was deleted from T2 but the T1
        embedding remains (search review I-4). Best-effort; returns
        False when the cache is unavailable or the delete fails.
        """
        if not self.is_available:
            return False
        doc_id = f"{self._session_id}:{int(plan_id)}"
        try:
            self._col.delete(ids=[doc_id])
            return True
        except Exception:
            _log.warning(
                "plan_session_cache_remove_failed",
                plan_id=plan_id, exc_info=True,
            )
            return False

    def _upsert_row(self, row: dict[str, Any]) -> bool:
        match_text = _synthesize_match_text(row)
        if not match_text:
            return False
        plan_id = row.get("id")
        if plan_id is None:
            return False
        doc_id = f"{self._session_id}:{int(plan_id)}"
        meta = {
            "session_id": self._session_id,
            "plan_id": int(plan_id),
            "project": row.get("project") or "",
            "tags": row.get("tags") or "",
            "verb": row.get("verb") or "",
            "scope": row.get("scope") or "",
        }
        try:
            self._col.upsert(
                ids=[doc_id], documents=[match_text], metadatas=[meta],
            )
        except Exception:
            _log.warning("plan_session_cache_upsert_failed",
                         plan_id=plan_id, exc_info=True)
            return False
        return True


def _synthesize_match_text(row: dict[str, Any]) -> str:
    """Hybrid description + dimensional suffix for T1 embedding. RDR-092 Phase 1.

    Shape: ``"<description>. <verb> <name> scope <scope>"`` when the
    row carries verb AND name (the identity signal the cosine lane
    needs to differentiate otherwise-similar descriptions). Scope is
    optional and only appended when present.

    When verb or name is missing, falls back to the raw description
    — legacy NULL-dimension rows still embed cleanly rather than
    losing all signal to an empty suffix. R10 validates the hybrid
    form: zero verb-accuracy regression vs raw-description, plus the
    dimensional suffix gives the matcher a reliable verb hook.
    """
    description = (row.get("query") or "").strip()
    verb = (row.get("verb") or "").strip()
    name = (row.get("name") or "").strip()
    scope = (row.get("scope") or "").strip()

    if not verb or not name:
        return description

    suffix = f"{verb} {name}"
    if scope:
        suffix += f" scope {scope}"
    if description:
        # Collapse an existing trailing '.' so descriptions that already
        # end with punctuation do not produce '..' in the match text.
        core = description.rstrip(".").rstrip()
        return f"{core}. {suffix}"
    return suffix
