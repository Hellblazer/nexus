# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
from __future__ import annotations

from uuid import uuid4

from nexus.db.t2 import T2Database

_COLLECTION = "scratch"


class T1Database:
    """T1 ChromaDB session scratch.

    Uses a per-process ``chromadb.EphemeralClient`` + ``DefaultEmbeddingFunction``
    (all-MiniLM-L6-v2, local ONNX — no API calls).

    EphemeralClient holds data in-memory only; nothing is written to disk.
    Per-session isolation is provided by ``session_id`` metadata filtering in
    ``search``, ``list_entries``, ``clear``, and ``flagged_entries``.

    Note: crash-recovery of orphaned entries from previous sessions is out of scope
    per spec — T1 is the scratch tier and must be zero-persistence.
    """

    def __init__(self, session_id: str, client=None) -> None:
        import chromadb

        self._session_id = session_id
        self._client = client if client is not None else chromadb.EphemeralClient()
        self._col = self._client.get_or_create_collection(_COLLECTION)

    # ── Internal helpers ──────────────────────────────────────────────────────

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
        self._col.add(
            ids=[doc_id],
            documents=[content],
            metadatas=[
                {
                    "session_id": self._session_id,
                    "tags": tags,
                    "flagged": persist,
                    "flush_project": flush_project,
                    "flush_title": flush_title,
                }
            ],
        )
        return doc_id

    # ── Read ──────────────────────────────────────────────────────────────────

    def get(self, id: str) -> dict | None:
        """Return the document dict for *id*, or None if not found."""
        result = self._col.get(ids=[id], include=["documents", "metadatas"])
        if not result["ids"]:
            return None
        return self._to_row(result["ids"][0], result["documents"][0], result["metadatas"][0])

    def search(self, query: str, n_results: int = 10) -> list[dict]:
        """Semantic search using the local ONNX embedding model.

        Results are scoped to this session via ``session_id`` metadata filter.
        Returns results ordered by relevance (closest first).
        Returns an empty list when the session has no entries.
        """
        # Count session-scoped documents to avoid n_results > matching count error.
        session_filter = {"session_id": self._session_id}
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

    def list_entries(self) -> list[dict]:
        """Return all entries belonging to this session."""
        count = self._col.count()
        if count == 0:
            return []
        result = self._col.get(
            where={"session_id": self._session_id},
            include=["documents", "metadatas"],
        )
        return [
            self._to_row(did, doc, meta)
            for did, doc, meta in zip(
                result["ids"], result["documents"], result["metadatas"]
            )
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
        existing = self._col.get(ids=[id], include=["metadatas"])
        if not existing["ids"]:
            raise KeyError(f"No scratch entry: {id!r}")
        meta = dict(existing["metadatas"][0])
        meta["flagged"] = True
        meta["flush_project"] = project or "scratch_sessions"
        meta["flush_title"] = title or f"{self._session_id}_{id}"
        self._col.update(ids=[id], metadatas=[meta])

    def unflag(self, id: str) -> None:
        """Remove the flush-on-SessionEnd marking from *id*."""
        existing = self._col.get(ids=[id], include=["metadatas"])
        if not existing["ids"]:
            raise KeyError(f"No scratch entry: {id!r}")
        meta = dict(existing["metadatas"][0])
        meta["flagged"] = False
        meta["flush_project"] = ""
        meta["flush_title"] = ""
        self._col.update(ids=[id], metadatas=[meta])

    # ── Promote ───────────────────────────────────────────────────────────────

    def promote(self, id: str, project: str, title: str, t2: T2Database) -> None:
        """Copy T1 entry *id* to T2 immediately (manual promote)."""
        entry = self.get(id)
        if entry is None:
            raise KeyError(f"No scratch entry: {id!r}")
        t2.put(project=project, title=title, content=entry["content"], tags=entry.get("tags", ""))

    # ── Clear ─────────────────────────────────────────────────────────────────

    def clear(self) -> int:
        """Remove all session entries. Returns the count deleted."""
        # Query by session_id directly — the former total-count early-exit was
        # incorrect: if the collection has entries from OTHER sessions (count > 0)
        # but THIS session has none, the early return would still proceed to the
        # get() call unnecessarily.  Removing it is cleaner and correct.
        result = self._col.get(
            where={"session_id": self._session_id},
            include=[],
        )
        ids = result["ids"]
        if ids:
            self._col.delete(ids=ids)
        return len(ids)
