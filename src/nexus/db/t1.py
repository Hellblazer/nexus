# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
from __future__ import annotations

import os
from collections.abc import Callable
from typing import TypeVar
from uuid import uuid4

import structlog

_log = structlog.get_logger(__name__)

from datetime import UTC, datetime

from nexus.db.t2 import T2Database


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
from nexus.session import (
    _t1_isolated_env,
    find_immediate_claude_pid,
    read_claude_session_id,
    read_t1_addr_for,
)

_T = TypeVar("_T")

_COLLECTION = "scratch"


# Common English stopwords, shared with MemoryStore._STOPWORDS for
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


class T1ServerNotFoundError(RuntimeError):
    """Raised when ``T1Database()`` cannot resolve a live T1 server.

    GH #567: pre-fix the constructor silently fell back to a per-process
    ``EphemeralClient`` whenever discovery failed. CLI ``nx scratch put``
    writes landed in that store and vanished at process exit; the next
    ``nx scratch list`` invocation spawned a fresh ``EphemeralClient``
    and saw nothing.

    Opt-in paths (no exception raised):
      - ``T1Database(client=...)`` for explicit client injection in
        tests and the MCP server lifespan.
      - ``NX_T1_ISOLATED=1`` (or its deprecated ``NEXUS_SKIP_T1=1``
        alias) for stateless one-shot subprocesses; constructs an
        ``EphemeralClient``.
    """


class T1Database:
    """T1 ChromaDB session scratch, shared across all agents in a session tree.

    RDR-105 P4: a single hybrid-discovery path. The constructor's
    ``_init_new_discovery`` is a four-branch fail-loud gate, Path A
    (env), Path B (addr file), Path C (explicit isolation), or Path
    D (raise). No legacy session-record resolver. Reconnect after a
    connectivity loss is unsupported; callers must construct a fresh
    ``T1Database`` to re-resolve.

    Pass ``client=`` to inject a custom client in tests; this bypasses
    the gate entirely.
    """

    @staticmethod
    def _resolve_session_id(arg: str | None) -> str:
        """Resolve the session_id used as the per-entry metadata filter.

        Chain: explicit ctor arg > ``NX_SESSION_ID`` env >
        ``read_claude_session_id()`` (~/.config/nexus/current_session)
        > new ``uuid4()``.

        The ``read_claude_session_id()`` step is the load-bearing
        fallback for cross-process T1 visibility (nexus-h8ge): the MCP
        server, a Bash-tool sibling, and a hook subprocess all find
        the same Claude session via the on-disk pointer and converge
        on its UUID, so each side's session_id metadata filter sees
        the others' entries. Pre-fix this step was missing in every
        construction branch and every shell ``nx scratch`` invocation
        orphaned its writes under a fresh per-process UUID,
        breaking every nx-plugin hook that read T1 from the shell
        (subagent-start, post_compact, pre_close_verification,
        divergence-language-guard).
        """
        return (
            arg
            or os.environ.get("NX_SESSION_ID", "").strip()
            or read_claude_session_id()
            or str(uuid4())
        )

    def _init_new_discovery(self, chromadb, session_id: str | None) -> None:
        """RDR-105 P2 (nexus-mj2o): four-branch fail-loud constructor.

        Branch order (opt-in outranks discovery):

        Path C (operator opt-in, highest priority)
            ``NX_T1_ISOLATED=1`` (or its legacy ``NEXUS_SKIP_T1=1``
            alias) -> ``EphemeralClient``. The only place
            ``EphemeralClient`` may be constructed in this code path.
            nexus-svpq / GH #593: this branch is consulted FIRST so an
            explicit operator opt-in to ephemeral semantics is not
            silently overridden by env-pair or addr-file auto-discovery
            inside an active Claude session.
        Path A (env-pair discovery)
            ``NX_T1_HOST`` + ``NX_T1_PORT`` in env -> ``HttpClient``.
            Used by MCP-dispatched subprocesses (``claude -p`` shared).
        Path B (addr-file discovery)
            ``find_immediate_claude_pid`` resolves to a live addr file
            at ``~/.config/nexus/t1_addr.<pid>`` -> ``HttpClient``.
            Used by Claude-Code-spawned siblings (Bash tool, hooks).
        Path D (failure)
            None of the above -> raise :class:`T1ServerNotFoundError`.

        The flag-on path does NOT fall through to the legacy resolver.
        Per the RDR §'Phase 2 flag-isolation contract', flag-on and
        flag-off paths are mutually exclusive per process.
        """
        if _t1_isolated_env():
            self._client = chromadb.EphemeralClient()
            self._session_id = self._resolve_session_id(session_id)
            return

        host_env = os.environ.get("NX_T1_HOST", "").strip()
        port_env = os.environ.get("NX_T1_PORT", "").strip()
        if host_env and port_env:
            try:
                port_int = int(port_env)
            except ValueError as exc:
                raise T1ServerNotFoundError(
                    f"NX_T1_HOST is set but NX_T1_PORT={port_env!r} is "
                    "not a valid integer."
                ) from exc
            self._client = chromadb.HttpClient(host=host_env, port=port_int)
            self._session_id = self._resolve_session_id(session_id)
            return

        claude_pid = find_immediate_claude_pid()
        if claude_pid > 0:
            addr = read_t1_addr_for(claude_pid)
            if addr is not None:
                host, port = addr
                self._client = chromadb.HttpClient(host=host, port=port)
                self._session_id = self._resolve_session_id(session_id)
                return

        raise T1ServerNotFoundError(
            "T1 not configured for this process. Either inherit "
            "NX_T1_HOST and NX_T1_PORT from a parent MCP server "
            "(MCP-dispatched subprocess), run as a sibling of a "
            "top-level MCP server so the address file is "
            "discoverable via the PPID walk to the immediate "
            "claude ancestor, or set NX_T1_ISOLATED=1 to opt in "
            "to an in-process ephemeral T1.\n"
            "\n"
            "If you launched Claude Code via 'exec -a' or a custom "
            "wrapper, ensure the process name starts with 'claude'."
        )

    def __init__(self, session_id: str | None = None, client=None) -> None:
        import chromadb

        self._dead: bool = False

        if client is not None:
            # Test-injection / MCP-server path: caller supplies a client
            # explicitly (EphemeralClient, mock, or its own HttpClient).
            # Used by the FastMCP lifespan to install a server-lifetime
            # EphemeralClient as the MCP-tool-side T1 store.
            self._client = client
            self._session_id = self._resolve_session_id(session_id)
        else:
            # RDR-105 P4 (nexus-jnx7): the four-branch fail-loud gate is
            # the only resolution path. The legacy session-record
            # resolver chain was deleted along with the multi-writer
            # coordination machinery that produced the GH #567 / #572 /
            # #574 / #575 / #576 / #579 bug class.
            self._init_new_discovery(chromadb, session_id)

        self._col = self._client.get_or_create_collection(_COLLECTION)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _reconnect(self) -> None:
        """Surface a connection loss as :class:`T1ServerNotFoundError`.

        RDR-105 P4 (nexus-jnx7): there is no in-place reconnect under
        the hybrid-discovery architecture. The legacy resolver chain
        consulted ``SESSIONS_DIR`` and the multi-writer record files,
        both of which are gone. Re-resolving via the addr file or env
        path requires constructing a fresh ``T1Database`` so the
        four-branch fail-loud gate fires; doing it inside an existing
        instance would mask the connectivity loss as a silent retry.

        Sets ``_dead=True`` so subsequent ``_exec`` calls re-raise
        immediately rather than looping.
        """
        if self._dead:
            return
        self._dead = True
        _log.warning(
            "t1_reconnect_unsupported",
            session_id=self._session_id,
        )
        raise T1ServerNotFoundError(
            "T1 connection lost. Construct a new T1Database to "
            "re-resolve via the four-branch hybrid-discovery gate "
            "(env -> addr file -> isolation -> raise)."
        )

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
        agent: str = "",
    ) -> str:
        """Store *content* in T1. Returns the new document ID.

        If *persist* is True the entry is pre-flagged for SessionEnd flush.
        Auto-destination (when no explicit project/title): ``scratch_sessions``
        / ``{session_id}_{doc_id}``.

        ``agent`` (Phase 1B follow-up nexus-9clx) attributes the write to
        a subagent role for the tier-discipline observability loop. Empty
        string falls back to ``NX_AGENT`` env, then empty. Stored on
        chroma metadata; ``nx tier-status`` slices by agent via the
        ``tier_writes`` T2 mirror.
        """
        import os as _os
        doc_id = str(uuid4())
        if persist:
            flush_project = flush_project or "scratch_sessions"
            flush_title = flush_title or f"{self._session_id}_{doc_id}"
        if not agent:
            agent = _os.environ.get("NX_AGENT", "")
        meta = {
            "session_id": self._session_id,
            "tags": tags,
            "flagged": persist,
            "flush_project": flush_project,
            "flush_title": flush_title,
            "access_count": 0,
            "last_accessed": "",
            "agent": agent,
        }
        self._exec(lambda: self._col.add(ids=[doc_id], documents=[content], metadatas=[meta]))
        return doc_id

    # ── Read ──────────────────────────────────────────────────────────────────

    def _resolve_id(self, id: str) -> tuple[str | None, list[str]]:
        """Exact-then-prefix id resolution scoped to this session.

        Mirrors :meth:`MemoryStore.resolve_title` (nexus-e59o):

        * Exact session-owned match found: returns ``(id, [])``.
        * No exact match, exactly one session-owned id whose value
          starts with *id*: returns ``(full_id, [])``.
        * Multiple session-owned prefix candidates: returns
          ``(None, [candidate_ids])`` so the caller can list them.
        * Nothing matches: returns ``(None, [])``.

        Used by :meth:`get` and :meth:`delete` so the operator can paste
        back the 8-char prefix that ``scratch list`` displays
        (nexus-zpw6) instead of being forced to type the full UUID.
        Ownership check: the prefix scan is constrained to the current
        ``session_id`` so a sibling session's id never leaks into the
        candidate list.
        """
        # Exact lookup against the FULL collection — ChromaDB's
        # ``ids=[id]`` filter is the cheapest path. A non-empty hit
        # short-circuits before the prefix scan, but we still verify
        # ownership downstream in get/delete (this method does NOT do
        # the access-count side effect; callers do).
        try:
            exact = self._exec(
                lambda: self._col.get(ids=[id], include=["metadatas"])
            )
        except Exception:
            exact = {"ids": []}
        if exact["ids"]:
            owned = (
                (exact["metadatas"][0] or {}).get("session_id")
                == self._session_id
            )
            if owned:
                return id, []

        # Prefix fallback: enumerate this session's ids and filter.
        # Cheap because list_entries is already paginated to 300/page
        # and a session typically holds dozens of entries, not
        # thousands. The ergonomic case (8-char prefix unique) is
        # the common one; the ambiguous case surfaces a candidate list.
        try:
            session_ids = [e["id"] for e in self.list_entries()]
        except Exception:
            return None, []
        candidates = [sid for sid in session_ids if sid.startswith(id)]
        if len(candidates) == 1:
            return candidates[0], []
        return None, candidates

    def get(self, id: str) -> dict | None:
        """Return the document dict for *id*, or None if not found.

        nexus-zpw6: ``id`` may be the full UUID (legacy / strict) OR a
        unique session-owned prefix matching the 8-char form
        ``scratch list`` displays. Ambiguous prefixes return None and
        log the candidates so the MCP layer can surface them; this
        method never picks silently.
        """
        resolved, ambiguous = self._resolve_id(id)
        if resolved is None:
            if ambiguous:
                _log.warning(
                    "t1_get_ambiguous_prefix",
                    requested_id=id,
                    candidates=ambiguous,
                    session_id=self._session_id,
                )
            return None
        result = self._exec(lambda: self._col.get(ids=[resolved], include=["documents", "metadatas"]))
        if not result["ids"]:
            # Bug 1 (nexus-bug-report-2026-04-22): users hit Not-found on
            # ids freshly returned by put(). The path is unreproducible in
            # isolation; this log captures the state the next time it fires
            # so the cause can be diagnosed (stale singleton after a silent
            # _reconnect, dual MCP servers writing to different chroma
            # collections, etc.).
            _log.warning(
                "t1_get_miss",
                requested_id=id,
                session_id=self._session_id,
                client_type=type(self._client).__name__,
                dead=self._dead,
            )
            return None
        # Update access tracking (F-3: preserve existing metadata)
        existing = result["metadatas"][0] or {}
        updated_meta = {
            **existing,
            "access_count": existing.get("access_count", 0) + 1,
            "last_accessed": _now_iso(),
        }
        try:
            self._exec(lambda: self._col.update(ids=[resolved], metadatas=[updated_meta]))
        except Exception:
            _log.warning("t1_access_count_update_failed", id=resolved)
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
        """Delete a scratch entry by its full ID OR unique session-owned prefix.

        nexus-zpw6: ``id`` may be the full UUID OR a unique session-
        owned prefix (matches the 8-char form ``scratch list``
        displays). Ambiguous prefixes return False without deleting
        and log the candidate ids so the MCP layer can surface them
        instead of silently picking. Verifies session ownership
        before deleting; entries belonging to other sessions return
        False without deleting.
        """
        resolved, ambiguous = self._resolve_id(id)
        if resolved is None:
            if ambiguous:
                _log.warning(
                    "t1_delete_ambiguous_prefix",
                    requested_id=id,
                    candidates=ambiguous,
                    session_id=self._session_id,
                )
            return False

        def _do() -> bool:
            result = self._col.get(ids=[resolved], include=["metadatas"])
            if not result["ids"]:
                return False
            if result["metadatas"][0].get("session_id") != self._session_id:
                return False
            self._col.delete(ids=[resolved])
            return True

        return self._exec(_do)

    def resolve_prefix_candidates(self, id: str) -> list[str]:
        """Return session-owned ids matching *id* as exact or prefix.

        Public companion to :meth:`_resolve_id` — exposes the
        ambiguous-candidate list so MCP / CLI wrappers can surface a
        clean disambiguation message rather than just "not found".
        Empty list when nothing matches; one-element list when a
        unique resolution exists; multi-element when ambiguous.
        """
        resolved, ambiguous = self._resolve_id(id)
        if resolved is not None:
            return [resolved]
        return ambiguous

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
