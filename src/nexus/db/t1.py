# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
from __future__ import annotations

import warnings
from collections.abc import Callable
from typing import TypeVar
from uuid import uuid4

import structlog

_log = structlog.get_logger(__name__)

from datetime import UTC, datetime

from nexus.db.t2 import T2Database


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
from nexus.session import SESSIONS_DIR, find_ancestor_session

_T = TypeVar("_T")

_COLLECTION = "scratch"

# Common English stopwords — shared with MemoryStore._STOPWORDS for
# consistency across the two overlap-detection helpers.
_PROMOTE_STOPWORDS = frozenset(
    {
        "the", "a", "an", "in", "of", "for", "to", "and", "or", "is", "are", "was",
        "it", "that", "this", "with", "on", "at", "by", "from", "as", "be", "not",
    }
)

# Jaccard threshold for promote() overlap confirmation. Matches the
# default used by MemoryStore.find_overlapping_memories (RDR-061 E6) but
# is slightly more permissive (0.5 vs 0.7) because the promote path
# flags advisorily — the row is still written — while consolidation
# uses the higher bar for destructive merges.
_PROMOTE_OVERLAP_JACCARD = 0.5


def _promote_content_words(text: str) -> set[str]:
    """Return the set of non-stopword content tokens (length > 2) in *text*.

    Lowercased for case-insensitive comparison. Shared between the FTS5
    candidate query builder and the Jaccard confirmation step.
    """
    return {
        w.lower() for w in text.split()
        if len(w) > 2 and w.lower() not in _PROMOTE_STOPWORDS
    }


def _find_promote_overlap_candidates(
    content: str,
    project: str,
    t2: T2Database,
) -> list[dict]:
    """Return existing T2 entries under *project* that overlap with *content*.

    Two-phase match mirroring ``MemoryStore.find_overlapping_memories``:

    1. Pull the first 3 content tokens (non-stopword, length > 2) and use
       them as the FTS5 MATCH query for candidate retrieval. Keeping the
       query small is critical: FTS5 defaults to implicit AND, so using
       the full content would require every token to appear in the
       candidate (making detection impossible for similar-but-not-identical
       content).
    2. Compute Jaccard similarity on the full non-stopword word sets and
       keep only candidates at or above ``_PROMOTE_OVERLAP_JACCARD``.

    Returns an empty list when no candidates exceed the threshold, or
    when the content has fewer than 3 usable tokens.
    """
    words = [
        w for w in content.split()
        if len(w) > 2 and w.lower() not in _PROMOTE_STOPWORDS
    ]
    if len(words) < 3:
        # Not enough content to compute a meaningful similarity — trust
        # the caller's intent and report no overlap. Very short scratch
        # entries (< 3 non-stopword tokens) don't benefit from overlap
        # detection anyway.
        return []
    snippet = " ".join(words[:3])
    try:
        candidates = t2.memory.search(snippet, project=project, access="silent")
    except ValueError:
        return []
    if not candidates:
        return []
    w_new = _promote_content_words(content)
    if not w_new:
        return []
    hits: list[tuple[float, dict]] = []
    for cand in candidates:
        w_cand = _promote_content_words(cand.get("content", ""))
        if not w_cand:
            continue
        jaccard = len(w_new & w_cand) / len(w_new | w_cand)
        if jaccard >= _PROMOTE_OVERLAP_JACCARD:
            hits.append((jaccard, cand))
    hits.sort(key=lambda x: -x[0])
    return [cand for _, cand in hits]


class T1Database:
    """T1 ChromaDB session scratch — shared across all agents in a session tree.

    On construction, walks the PPID chain to find the session's ChromaDB HTTP
    server address.  All agents that share a common ancestor Claude Code process
    connect to the same server and see each other's entries (scoped by
    ``session_id`` metadata filter).

    Falls back to a local ``EphemeralClient`` (with a warning) when no server
    record is found — this preserves T1 functionality in restricted environments
    where the server could not start or ``ps`` is unavailable.

    If the parent session ends (stopping the ChromaDB server) while a child
    agent is still running, the first subsequent T1 operation will catch the
    connectivity error and transparently reconnect — either to a freshly
    detected server record or to a new local EphemeralClient.  Only one
    reconnect attempt is made; ``_dead`` is set afterwards to prevent loops.

    Pass ``client=`` explicitly to inject a custom client in tests.
    """

    def __init__(self, session_id: str | None = None, client=None) -> None:
        import chromadb

        self._dead: bool = False

        if client is not None:
            # Test-injection path: use provided client as-is.
            self._client = client
            self._session_id = session_id or str(uuid4())
        else:
            record = find_ancestor_session(SESSIONS_DIR)
            if record is not None:
                self._client = chromadb.HttpClient(
                    host=record["server_host"],
                    port=record["server_port"],
                )
                self._session_id = record["session_id"]
            else:
                warnings.warn(
                    "No T1 server found; falling back to local EphemeralClient. "
                    "Cross-agent scratch sharing is unavailable for this session.",
                    stacklevel=2,
                )
                from nexus.session import read_claude_session_id
                self._client = chromadb.EphemeralClient()
                self._session_id = session_id or read_claude_session_id() or str(uuid4())

        self._col = self._client.get_or_create_collection(_COLLECTION)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _reconnect(self) -> None:
        """Re-resolve the T1 server connection after a connectivity failure.

        Walks the PPID chain for a (possibly restarted) server record.
        Falls back to EphemeralClient when no record is found — but this
        loses all prior scratch entries including flagged ones (nexus-uhch).
        Sets ``_dead=True`` immediately to prevent cascading reconnect loops.
        """
        import chromadb

        if self._dead:
            return
        self._dead = True  # set before any I/O to prevent loops on re-entry

        record = find_ancestor_session(SESSIONS_DIR)
        if record is not None:
            self._client = chromadb.HttpClient(
                host=record["server_host"],
                port=record["server_port"],
            )
            self._session_id = record["session_id"]
        else:
            warnings.warn(
                "T1 reconnect falling back to EphemeralClient — "
                "all prior scratch entries (including flagged ones) are lost.",
                stacklevel=2,
            )
            _log.warning("t1_reconnect_ephemeral_fallback_data_lost",
                         session_id=self._session_id)
            self._client = chromadb.EphemeralClient()
            # session_id intentionally preserved from original construction.

        self._col = self._client.get_or_create_collection(_COLLECTION)

    @property
    def session_id(self) -> str:
        """Public accessor for the T1 session identifier."""
        return self._session_id

    def _exec(self, op: Callable[[], _T]) -> _T:
        """Execute a ChromaDB operation, reconnecting once on connection error."""
        try:
            return op()
        except Exception as exc:
            if self._dead:
                raise
            name = type(exc).__name__.lower()
            msg = str(exc).lower()
            if "connection" in name or "connect" in msg or "refused" in msg:
                self._reconnect()
                return op()
            raise

    def _to_row(self, doc_id: str, document: str, metadata: dict) -> dict:
        return {"id": doc_id, "content": document, **metadata}

    # ── Write ─────────────────────────────────────────────────────────────────

    def put(
        self,
        content: str,
        tags: str = "",
        persist: bool = False,
        flush_project: str = "",
        flush_title: str = "",
    ) -> str:
        """Store *content* in T1. Returns the new document ID.

        If *persist* is True the entry is pre-flagged for SessionEnd flush.
        Auto-destination (when no explicit project/title): ``scratch_sessions``
        / ``{session_id}_{doc_id}``.
        """
        doc_id = str(uuid4())
        if persist:
            flush_project = flush_project or "scratch_sessions"
            flush_title = flush_title or f"{self._session_id}_{doc_id}"
        meta = {
            "session_id": self._session_id,
            "tags": tags,
            "flagged": persist,
            "flush_project": flush_project,
            "flush_title": flush_title,
            "access_count": 0,
            "last_accessed": "",
        }
        self._exec(lambda: self._col.add(ids=[doc_id], documents=[content], metadatas=[meta]))
        return doc_id

    # ── Read ──────────────────────────────────────────────────────────────────

    def get(self, id: str) -> dict | None:
        """Return the document dict for *id*, or None if not found."""
        result = self._exec(lambda: self._col.get(ids=[id], include=["documents", "metadatas"]))
        if not result["ids"]:
            return None
        # Update access tracking (F-3: preserve existing metadata)
        existing = result["metadatas"][0] or {}
        updated_meta = {
            **existing,
            "access_count": existing.get("access_count", 0) + 1,
            "last_accessed": _now_iso(),
        }
        try:
            self._exec(lambda: self._col.update(ids=[id], metadatas=[updated_meta]))
        except Exception:
            _log.warning("t1_access_count_update_failed", id=id)
        return self._to_row(result["ids"][0], result["documents"][0], updated_meta)

    def search(self, query: str, n_results: int = 10) -> list[dict]:
        """Semantic search using the local ONNX embedding model.

        Results are scoped to this session via ``session_id`` metadata filter.
        Returns results ordered by relevance (closest first).
        Returns an empty list when the session has no entries.
        """
        session_filter = {"session_id": self._session_id}

        def _query() -> list[dict]:
            # Count session-scoped documents to avoid n_results > matching count error.
            session_docs = self._col.get(where=session_filter, include=[])
            session_count = len(session_docs["ids"])
            if session_count == 0:
                return []
            actual_n = min(n_results, session_count)
            results = self._col.query(
                query_texts=[query],
                n_results=actual_n,
                where=session_filter,
                include=["documents", "metadatas", "distances"],
            )
            return [
                {"id": did, "content": doc, "distance": dist, **meta}
                for did, doc, meta, dist in zip(
                    results["ids"][0],
                    results["documents"][0],
                    results["metadatas"][0],
                    results["distances"][0],
                )
            ]

        # Phase 1: fetch results via _exec (reconnect-safe)
        rows = self._exec(_query)

        # Phase 2: update access_count across all returned rows in a single
        # batched ``col.update`` (search review S-3). The previous per-row
        # loop issued N serial HTTP round-trips to the T1 ChromaDB server,
        # adding measurable latency to every ``nx scratch search`` call
        # that routed through the session HTTP server.
        now = _now_iso()
        ids_to_update: list[str] = []
        metas_to_update: list[dict] = []
        for row in rows:
            existing_meta = {
                k: v for k, v in row.items()
                if k not in ("id", "content", "distance")
            }
            ids_to_update.append(row["id"])
            metas_to_update.append({
                **existing_meta,
                "access_count": existing_meta.get("access_count", 0) + 1,
                "last_accessed": now,
            })

        if ids_to_update:
            try:
                self._exec(
                    lambda ids=ids_to_update, metas=metas_to_update:
                    self._col.update(ids=ids, metadatas=metas)
                )
            except Exception:
                _log.warning(
                    "t1_access_count_batch_update_failed",
                    count=len(ids_to_update),
                )
        return rows

    def list_entries(self) -> list[dict]:
        """Return all entries belonging to this session.

        Paginates to avoid ChromaDB default limit truncation (nexus-885n).
        """
        all_ids: list[str] = []
        all_docs: list[str] = []
        all_metas: list[dict] = []
        offset = 0

        def _page() -> dict:
            return self._col.get(
                where={"session_id": self._session_id},
                include=["documents", "metadatas"],
                limit=300,
                offset=offset,
            )

        while True:
            result = self._exec(_page)
            all_ids.extend(result["ids"])
            all_docs.extend(result["documents"])
            all_metas.extend(result["metadatas"])
            if len(result["ids"]) < 300:
                break
            offset += 300

        return [
            self._to_row(did, doc, meta)
            for did, doc, meta in zip(all_ids, all_docs, all_metas)
        ]

    def flagged_entries(self) -> list[dict]:
        """Return all entries marked for SessionEnd flush."""
        return [e for e in self.list_entries() if e.get("flagged")]

    # ── Flag / unflag ─────────────────────────────────────────────────────────

    def flag(self, id: str, project: str = "", title: str = "") -> None:
        """Mark *id* for SessionEnd flush to T2.

        Auto-destination when *project*/*title* are omitted:
        ``scratch_sessions`` / ``{session_id}_{id}``.
        """
        def _do() -> None:
            existing = self._col.get(ids=[id], include=["metadatas"])
            if not existing["ids"]:
                raise KeyError(f"No scratch entry: {id!r}")
            meta = dict(existing["metadatas"][0])
            meta["flagged"] = True
            meta["flush_project"] = project or "scratch_sessions"
            meta["flush_title"] = title or f"{self._session_id}_{id}"
            self._col.update(ids=[id], metadatas=[meta])

        self._exec(_do)

    def unflag(self, id: str) -> None:
        """Remove the flush-on-SessionEnd marking from *id*."""
        def _do() -> None:
            existing = self._col.get(ids=[id], include=["metadatas"])
            if not existing["ids"]:
                raise KeyError(f"No scratch entry: {id!r}")
            meta = dict(existing["metadatas"][0])
            meta["flagged"] = False
            meta["flush_project"] = ""
            meta["flush_title"] = ""
            self._col.update(ids=[id], metadatas=[meta])

        self._exec(_do)

    # ── Promote ───────────────────────────────────────────────────────────────

    def promote(self, id: str, project: str, title: str, t2: T2Database) -> "PromotionReport":
        """Copy T1 entry *id* to T2 immediately. Returns a PromotionReport.

        Overlap detection (RDR-057): pulls the first few non-stopword content
        tokens from the scratch entry and FTS5-searches T2 for any existing
        entry under the same project that matches them. If candidates come
        back, confirms with Jaccard similarity (≥ 0.5) on the non-stopword
        word sets before reporting ``overlap_detected``.

        Why not MATCH the full snippet: FTS5 MATCH uses implicit AND, so a
        full-content query requires every token in the new entry to also
        appear in the existing entry. By construction, similar-but-not-
        identical content always has at least one new token, making the
        full-snippet approach unable to detect the common case (v3.8.0
        shakeout finding). Using a small token prefix for candidate
        retrieval plus Jaccard for precision matches the pattern already
        used by ``find_overlapping_memories`` (memory_store.py).
        """
        from nexus.types import PromotionReport

        # Fetch without incrementing access_count (promote is a write-path, not a read)
        result = self._exec(lambda: self._col.get(ids=[id], include=["documents", "metadatas"]))
        if not result["ids"]:
            raise KeyError(f"No scratch entry: {id!r}")
        entry = self._to_row(result["ids"][0], result["documents"][0], result["metadatas"][0])

        matches = _find_promote_overlap_candidates(entry["content"], project, t2)
        if matches:
            best = matches[0]
            # merged=False: T2.put() writes the new entry as a separate row.
            # The agent must explicitly merge if that's the intent.
            report = PromotionReport(
                action="overlap_detected",
                existing_title=best["title"],
                merged=False,
            )
        else:
            report = PromotionReport(action="new")
        t2.put(project=project, title=title, content=entry["content"], tags=entry.get("tags", ""))
        return report


    def delete(self, id: str) -> bool:
        """Delete a scratch entry by its full ID.

        Verifies session ownership before deleting — entries belonging to
        other sessions return False without deleting.  Returns False when the
        entry does not exist or the session does not own it; True on success.
        """
        def _do() -> bool:
            result = self._col.get(ids=[id], include=["metadatas"])
            if not result["ids"]:
                return False
            if result["metadatas"][0].get("session_id") != self._session_id:
                return False
            self._col.delete(ids=[id])
            return True

        return self._exec(_do)

    # ── Clear ─────────────────────────────────────────────────────────────────

    def clear(self) -> int:
        """Remove all session entries. Returns the count deleted.

        Paginates to avoid ChromaDB default limit truncation (nexus-885n).
        """
        def _do() -> int:
            all_ids: list[str] = []
            offset = 0
            while True:
                result = self._col.get(
                    where={"session_id": self._session_id},
                    include=[],
                    limit=300,
                    offset=offset,
                )
                all_ids.extend(result["ids"])
                if len(result["ids"]) < 300:
                    break
                offset += 300
            if all_ids:
                self._col.delete(ids=all_ids)
            return len(all_ids)

        return self._exec(_do)
