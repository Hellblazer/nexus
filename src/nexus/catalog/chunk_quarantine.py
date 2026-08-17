# SPDX-License-Identifier: AGPL-3.0-or-later
"""Chunk quarantine — soft delete for the orphan GC (nexus-xukbj).

Instead of hard-deleting orphan chunks (or refusing over-floor sweeps with
a recurring warning — the nexus-mr89x nag), the GC MOVES orphans to a
sibling collection named ``quarantine__<owner>__<model>__v<n>``. The
``quarantine`` prefix is in NO search corpus, so quarantined chunks are
excluded from every retrieval surface by construction — no filters, no
metadata-update primitive, no schema change.

Lifecycle per GC pass (wired in ``indexer._prune_deleted_files``):

1. **Restore** — quarantined chashes that are referenced by the manifest
   again (a heal re-referenced them, or content returned) copy back to the
   origin collection and leave quarantine. Chash-keyed upsert = idempotent.
2. **Quarantine** — this pass's orphans move over with their embeddings
   (no re-embed), stamped ``quarantined_at`` + ``origin_collection`` at add
   time. NO safety floor here: the move is recoverable, so mass supersede
   churn from a big ``git pull`` proceeds silently instead of warning
   forever (the nexus-mr89x refusal nag this module retires).
3. **Expire** — quarantine rows older than ``NX_GC_QUARANTINE_DAYS``
   (default 14) hard-delete. The mr89x safety floor applies HERE only: a
   mass hard-delete surviving a full grace window means a manifest defect
   persisted for weeks — the one case that should still be loud.

First concrete piece of the RDR-156 soft-delete theme (nexus-70r3c).
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

QUARANTINE_PREFIX = "quarantine"

#: Days a quarantined chunk survives before the expiry pass hard-deletes it.
QUARANTINE_DAYS_DEFAULT = 14

_WRITE_BATCH = 300  # ChromaCloud MAX_RECORDS_PER_WRITE; safe everywhere


def quarantine_collection_name(origin: str) -> str:
    """``code__nexus-1-1__voyage-code-3__v1`` -> its quarantine sibling
    ``quarantine-code__nexus-1-1__voyage-code-3__v1``.

    The origin's content-type stays IN the prefix segment (review 4cb743be
    C3: dropping it collided every same-owner/model origin onto one
    sibling, cross-contaminating the expiry floor), so each origin owns a
    distinct sibling. The ``quarantine-*`` prefix matches no search corpus,
    which is what keeps quarantined chunks out of every retrieval surface.
    These names are deliberately outside the strict conformance enum —
    creation passes ``strict=False`` (system-internal collections).
    """
    parts = origin.split("__", 1)
    if len(parts) == 2:
        return f"{QUARANTINE_PREFIX}-{parts[0]}__{parts[1]}"
    return f"{QUARANTINE_PREFIX}-x__{origin}"


def quarantine_days() -> int:
    raw = os.environ.get("NX_GC_QUARANTINE_DAYS", "")
    if not raw:
        return QUARANTINE_DAYS_DEFAULT
    try:
        val = int(raw)
    except ValueError:
        val = -1
    if val < 0:
        _log.warning(
            "gc_quarantine_days_invalid",
            raw=raw, using=QUARANTINE_DAYS_DEFAULT,
        )
        return QUARANTINE_DAYS_DEFAULT
    return val


def now_stamp() -> str:
    """The GC quarantine timestamp format: ``2026-08-10T12:00:00Z``.

    Shared by the client-side stamp (:func:`quarantine_orphans`'s ``now``)
    and the RDR-191 server-side path (:func:`quarantine_orphans_serverside`
    passes this same shape through as ``quarantined_at``/compares against
    it as ``cutoff`` in :func:`expire_quarantine_serverside`) so both
    remain lexicographically comparable — see the catalog-023 changelog
    header for why that matters.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── RDR-191 Phase 1: server-side prune (catalog-023) ────────────────────────
#
# These three ``*_serverside`` wrappers try the engine's anti-join GC route
# (``HttpVectorClient.gc_quarantine_orphans`` / ``gc_restore_rereferenced`` /
# ``gc_expire_quarantine``) and return ``None`` when ``db`` has no such method
# — local/in-memory mode, where the InMemoryVectorClient unit-test double has
# no server to route to. That branch is permanent: it is about CAPABILITY, not
# engine version, so no floor bump ever retires it.
#
# The 404 branch that used to sit alongside it is GONE (retired at
# REQUIRED_ENGINE_VERSION (0, 1, 70), the tag that ships catalog-023's
# routes, by ``TestGcServersideFallbackDoesNotOutliveItsRoute`` in
# ``tests/test_engine_version.py``). It is unreachable at this floor in both
# directions, verified before deleting: a local box converges its engine to
# ``REQUIRED_ENGINE_VERSION`` on any ordinary ``nx`` command
# (``upgrade_finish.converge_engine``, unattended), and a cloud client
# refuses a below-identity managed engine outright (GH #1402) rather than
# reaching a route call. A VectorServiceError from these calls is now a real
# failure and propagates, which is the point.
#
# Callers (``indexer._prune_deleted_files``) fall back to the client-side
# fetch-diff-copy-delete path on ``None``. That client-side implementation
# stays — retiring it is RDR-191 Phase 5, a separate and much larger change.
#
# A ``None`` return is a ROUTE-UNAVAILABLE signal, never "nothing to do" —
# the caller must not mistake it for a zero-orphan result.

def quarantine_orphans_serverside(
    db: Any, collection_name: str, quarantine_name: str,
    quarantined_at: str, sample_limit: int = 20,
) -> tuple[int, list[dict]] | None:
    """Try the server-side anti-join move. ``(moved, sample)`` or ``None``
    if the route is unavailable (caller falls back to :func:`quarantine_orphans`)."""
    fn = getattr(db, "gc_quarantine_orphans", None)
    if fn is None:
        return None
    result = fn(collection_name, quarantine_name, quarantined_at, sample_limit)
    return int(result.get("moved", 0)), list(result.get("sample") or [])


def restore_rereferenced_serverside(db: Any, quarantine_name: str, origin_name: str) -> int | None:
    """Try the server-side re-reference restore. Restored count, or ``None``
    if the route is unavailable (caller falls back to :func:`restore_rereferenced`)."""
    fn = getattr(db, "gc_restore_rereferenced", None)
    if fn is None:
        return None
    return fn(quarantine_name, origin_name)


def expire_quarantine_serverside(
    db: Any, quarantine_name: str, origin_name: str, cutoff: str,
    *, floor_fraction: float, floor_min_chunks: int, force: bool = False,
) -> tuple[int, int] | None:
    """Try the server-side grace-window expiry. ``(expired, refused)`` or
    ``None`` if the route is unavailable (caller falls back to
    :func:`expire_quarantine`)."""
    fn = getattr(db, "gc_expire_quarantine", None)
    if fn is None:
        return None
    result = fn(quarantine_name, origin_name, cutoff, floor_fraction, floor_min_chunks, force)
    return int(result.get("expired", 0)), int(result.get("refused", 0))

# _fetch_full, _upsert_full, quarantine_orphans, restore_rereferenced,
# and expire_quarantine (the client-side fetch-diff-copy-delete quarantine
# lifecycle) are DELETED here (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15)
# — their one caller, indexer._prune_deleted_files' client-side fallback
# sweep, is retired alongside them (see that function's own docstring for
# the full rationale: the manifest-chunk FK, catalog-029, makes the
# completeness apparatus this lifecycle existed to prove correct
# unreachable by construction). The *_serverside siblings above
# (quarantine_orphans_serverside / restore_rereferenced_serverside /
# expire_quarantine_serverside, RDR-191 Phase 1, catalog-023) are the ONLY
# quarantine lifecycle left — every currently supported deployment has
# their routes (REQUIRED_ENGINE_VERSION is far past catalog-023).
