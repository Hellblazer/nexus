# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
from __future__ import annotations

import warnings
from collections.abc import Callable
from typing import TypeVar
from uuid import uuid4

from nexus.db.t2 import T2Database
from nexus.session import SESSIONS_DIR, find_ancestor_session

_T = TypeVar("_T")

_COLLECTION = "scratch"


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
        Falls back to EphemeralClient when no record is found.
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
            self._client = chromadb.EphemeralClient()
            # session_id intentionally preserved from original construction.

        self._col = self._client.get_or_create_collection(_COLLECTION)

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
        }
        self._exec(lambda: self._col.add(ids=[doc_id], documents=[content], metadatas=[meta]))
        return doc_id

    # ── Read ──────────────────────────────────────────────────────────────────

    def get(self, id: str) -> dict | None:
        """Return the document dict for *id*, or None if not found."""
        result = self._exec(lambda: self._col.get(ids=[id], include=["documents", "metadatas"]))
        if not result["ids"]:
            return None
        return self._to_row(result["ids"][0], result["documents"][0], result["metadatas"][0])

    def search(self, query: str, n_results: int = 10) -> list[dict]:
        """Semantic search using the local ONNX embedding model.

        Results are scoped to this session via ``session_id`` metadata filter.
        Returns results ordered by relevance (closest first).
        Returns an empty list when the session has no entries.
        """
        session_filter = {"session_id": self._session_id}

        def _do() -> list[dict]:
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

        return self._exec(_do)

    def list_entries(self) -> list[dict]:
        """Return all entries belonging to this session."""
        result = self._exec(lambda: self._col.get(
            where={"session_id": self._session_id},
            include=["documents", "metadatas"],
        ))
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

    def promote(self, id: str, project: str, title: str, t2: T2Database) -> None:
        """Copy T1 entry *id* to T2 immediately (manual promote)."""
        entry = self.get(id)
        if entry is None:
            raise KeyError(f"No scratch entry: {id!r}")
        t2.put(project=project, title=title, content=entry["content"], tags=entry.get("tags", ""))


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
        """Remove all session entries. Returns the count deleted."""
        def _do() -> int:
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

        return self._exec(_do)
