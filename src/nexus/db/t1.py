# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
from __future__ import annotations

from uuid import uuid4

from nexus.db.t2 import T2Database

_COLLECTION = "scratch"


class T1Database:
    """T1 in-memory ChromaDB session scratch.

    Uses ``chromadb.EphemeralClient`` + ``DefaultEmbeddingFunction``
    (all-MiniLM-L6-v2, local ONNX — no API calls).

    Each document stores ``session_id`` in its metadata so that, when T1 is
    hosted in the long-running ``nx serve`` process and shared across sessions,
    per-session filtering still works correctly.
    """

    def __init__(self, session_id: str) -> None:
        import chromadb
        from pathlib import Path

        self._session_id = session_id
        # Use a PersistentClient keyed by session_id so data survives across
        # separate CLI invocations within the same session (e.g., multiple
        # `uv run nx scratch …` calls from the same terminal).  The directory
        # is cleaned up by the SessionEnd hook or `nx scratch clear`.
        scratch_dir = Path.home() / ".config" / "nexus" / "scratch" / session_id
        scratch_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(scratch_dir))
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

        Returns results ordered by relevance (closest first).
        Returns an empty list when the collection is empty.
        """
        count = self._col.count()
        if count == 0:
            return []
        actual_n = min(n_results, count)
        results = self._col.query(
            query_texts=[query],
            n_results=actual_n,
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
        count = self._col.count()
        if count == 0:
            return 0
        result = self._col.get(
            where={"session_id": self._session_id},
            include=[],
        )
        ids = result["ids"]
        if ids:
            self._col.delete(ids=ids)
        return len(ids)
