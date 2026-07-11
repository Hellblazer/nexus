# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
from __future__ import annotations

import fcntl
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar
from uuid import uuid4

if TYPE_CHECKING:
    from nexus.daemon.t2_client import T2Client

import structlog

_log = structlog.get_logger(__name__)

from datetime import UTC, datetime

from nexus.db.t2 import T2Database


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
from nexus.session import (
    _t1_isolated_env,
    resolve_active_session_id,
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

        Delegates to :func:`nexus.session.resolve_active_session_id` and
        substitutes ``"unknown"`` when the chain returns ``None``.

        The per-entry session_id is the metadata filter key on every
        chunk; it must never be empty. ``"unknown"`` is the canonical
        sentinel: when no session is bound, the audit log
        (``mcp/core._record_tier_write``) and the T1 chunk store agree
        on attribution and operators can grep for "unknown" to find
        rows that did not bind to a Claude session.

        Pre-issue-#594 this method open-coded the chain and used
        ``uuid4()`` as the fallback, which made T1 writes impossible to
        correlate with the audit log when the on-disk pointer was
        missing -- the exact failure mode that PR #590 was supposed to
        close. Issue #594 / nexus-9e9a unifies the chain and the
        fallback so the three drift-prone sites
        (T1 / tier-write / launcher) have one source of truth.
        """
        return resolve_active_session_id(arg) or "unknown"

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
        Path B (session-id lease discovery)
            ``resolve_active_session_id`` resolves a session-id whose
            live lease at ``~/.config/nexus/t1_addr.<session_id>``
            yields ``(host, port)`` -> ``HttpClient`` (RDR-149 P4).
            Both the writer (MCP lifespan) and the reader compute the
            session-id identically from ``current_session``; liveness is
            lease freshness (TTL), not pid, so a dead owner's lease ages
            out (pid-reuse immunity). Used by Claude-Code-spawned
            siblings (Bash tool, hooks) once a session-id resolves. In the
            cold-start sliver before the SessionStart hook writes
            ``current_session`` (and with no ``NX_SESSION_ID`` in env), a
            bare Bash sibling resolves no session-id and falls back to
            matching the owner's transient lease by its own immediate Claude
            ancestor pid (``discover_t1_by_claude_ancestor``, nexus-0x16i);
            a sibling of a different session does not match and fails loud
            (Path D). MCP-dispatched subprocesses are unaffected (Path A).
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

        from nexus.session import _nexus_config_dir_at_import  # noqa: PLC0415 — circular-dep avoidance (nexus.session imports from db)

        config_dir = _nexus_config_dir_at_import()
        resolved_session = resolve_active_session_id(session_id)
        if resolved_session:
            from nexus.daemon.t1_lease import discover_t1_lease  # noqa: PLC0415 — circular-dep avoidance (daemon package imports from db)

            addr = discover_t1_lease(resolved_session, config_dir=config_dir)
            if addr is not None:
                host, port = addr
                self._client = chromadb.HttpClient(host=host, port=port)
                self._session_id = self._resolve_session_id(session_id)
                return

        # Ancestor-pid fallback when the session-id path missed. Two cases
        # (nexus-0x16i cold start AND nexus-gff3g session-id divergence): the
        # sibling may resolve no session-id (before SessionStart writes
        # current_session) OR a session-id that simply has no live lease
        # (because the owner's MCP keyed on a divergent NX_SESSION_ID). Either
        # way, target the owner's own lease — transient or session-keyed — by
        # the sibling's immediate Claude ancestor pid (RF-6: both sides resolve
        # it identically). Ancestor-pid-targeted + TTL-bounded, so no
        # cross-session mis-bind. ``resolved_session`` being non-empty does NOT
        # mean this path is unreachable: a non-empty-but-unleased id falls here.
        from nexus.daemon.t1_lease import discover_t1_by_claude_ancestor  # noqa: PLC0415 — circular-dep avoidance (daemon package imports from db)
        from nexus.session import find_immediate_claude_pid  # noqa: PLC0415 — circular-dep avoidance (nexus.session imports from db)

        addr = discover_t1_by_claude_ancestor(
            find_immediate_claude_pid(), config_dir=config_dir
        )
        if addr is not None:
            host, port = addr
            self._client = chromadb.HttpClient(host=host, port=port)
            self._session_id = self._resolve_session_id(session_id)
            return

        raise T1ServerNotFoundError(
            "T1 not configured for this process. Either inherit "
            "NX_T1_HOST and NX_T1_PORT from a parent MCP server "
            "(MCP-dispatched subprocess), run as a sibling of a "
            "top-level MCP server so a live session-id lease "
            "(~/.config/nexus/t1_addr.<session_id>) is discoverable, "
            "or set NX_T1_ISOLATED=1 to opt in to an in-process "
            "ephemeral T1.\n"
            "\n"
            "If no session-id resolves, ensure the SessionStart hook "
            "has written ~/.config/nexus/current_session, or pass "
            "NX_SESSION_ID explicitly."
        )

    def __init__(self, session_id: str | None = None, client=None) -> None:
        import chromadb  # noqa: PLC0415 — optional/heavy dep deferred (chromadb)

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
            "T1 connection lost. Use /clear or restart the MCP server "
            "to re-resolve the T1 endpoint via the four-branch discovery gate "
            "(env -> lease -> isolation -> raise). "
            "In-place reconnect is unsupported by design (RDR-105 P4 nexus-jnx7)."
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
        import os as _os  # noqa: PLC0415 — deliberate function-local import (branch-local env read)
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
        except Exception:  # noqa: BLE001 — boundary catch of undocumented chromadb get() failures; fall back to the prefix scan
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
        except Exception:  # noqa: BLE001 — boundary catch of undocumented chromadb enumeration failures; resolution falls back to not-found
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
        except Exception:  # noqa: BLE001 — best-effort access-count telemetry must not crash the caller; surfaced via log.warning
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
            except Exception:  # noqa: BLE001 — best-effort batch access-count telemetry must not crash the caller; surfaced via log.warning
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

    def promote(
        self, id: str, project: str, title: str, t2: T2Database | T2Client,
    ) -> "PromotionReport":
        """Copy T1 entry *id* to T2 immediately. Returns a PromotionReport.

        *t2* may be a direct ``T2Database`` or a daemon-backed ``T2Client``
        (RDR-128 P3): ``nx scratch promote`` routes through
        ``mcp_infra.t2_index_write``, so the overlap-detection ``memory.search``
        read and the ``put`` write both go over the daemon RPC when one is
        reachable. Both types expose the ``.put`` / ``.memory`` surface this
        method uses.

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
        from nexus.types import PromotionReport  # noqa: PLC0415 — circular-dep avoidance (nexus.types imports from db)

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


# ── CLI-dedicated session (nexus-rn3wo.1) ──────────────────────────────────────
#
# Design history (LOCKED — see T2 nexus/design-t1-service-local-cutover-
# 2026-07-11.md for the full decision trail; do not relitigate without new
# evidence):
#
#   1. First draft: mint a FRESH random uuid4() session per bare-CLI
#      invocation. Safe (never collides) but gives zero cross-invocation
#      continuity -- every ``nx scratch`` call was its own island.
#   2. nx_plan_audit flagged this CRITICAL-adjacent and suggested deriving
#      the session id from resolve_active_session_id() (the same chain the
#      MCP server uses). Tested empirically and found ACTIVELY DANGEROUS:
#      resolve_active_session_id() resolves to the SAME id a live MCP
#      server for that Claude session has already minted a T1 token for.
#      HttpTokenStore.start_session() is ON CONFLICT DO UPDATE -- it
#      ROTATES. A bare CLI process deriving from resolve_active_session_id()
#      would rotate the live MCP's token out from under it.
#   3. FINAL (this module): a CLI-DEDICATED, PERSISTED session id --
#      generated once, cached to a local file, reused by every subsequent
#      bare-CLI invocation. NEVER derived from resolve_active_session_id(),
#      NX_SESSION_ID, or current_session -- a separate, purpose-built
#      identity namespace exclusively for bare-CLI T1 access, so it can
#      never collide with anything an MCP server would independently
#      compute. Gives real continuity across separate ``nx scratch`` calls
#      AND is collision-safe.
#   4. Second nx_plan_audit pass: PASS_WITH_CHANGES, one MEDIUM gap -- the
#      dedicated-id approach fixes CLI-vs-MCP collision but not CLI-vs-CLI
#      races (two concurrent bare ``nx scratch`` processes could each
#      re-mint the same dedicated id, rotating each other's token, causing
#      the loser's next request to 401). Fix: self-heal -- on a 401 from
#      HttpScratchStore when using the CLI-dedicated session, re-mint once
#      and retry the failed operation before propagating the error.

#: Cache-file name for the CLI-dedicated T1 session id. Lives directly under
#: ``nexus_config_dir()``, alongside the other per-install identity files
#: (``current_session``, ``t1_addr.<session_id>``). This file is read/written
#: ONLY by the no-inherited-session branch of :func:`get_t1_database`; the
#: inherited-live-MCP-session branch never touches it.
_CLI_DEDICATED_SESSION_FILENAME = "t1_cli_dedicated_session"


def _cli_dedicated_session_id(config_dir: Path) -> str:
    """Return the persisted CLI-dedicated T1 session id, minting one on first use.

    Race-safe first creation: two bare-CLI processes racing to create the
    cache file for the FIRST time converge on the SAME id rather than each
    generating a different one and silently picking one. Uses the same
    ``fcntl.flock`` election + temp-file/``os.replace`` atomic-publish
    pattern as :mod:`nexus.daemon.service_registry` -- a blocking exclusive
    lock serializes the read-or-create critical section, and the publish
    itself can never be observed torn.
    """
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = config_dir / _CLI_DEDICATED_SESSION_FILENAME
    lock_path = config_dir / f"{_CLI_DEDICATED_SESSION_FILENAME}.lock"

    lock_fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        # Not a daemon-lifecycle election (RDR-149's ServiceRegistry._elect):
        # no TTL, no heartbeat, no owner_token, no generation fencing. This is
        # a one-shot idempotent "read-or-create a permanent identity file"
        # mutex; the id, once written, never changes. Routing it through
        # ServiceRegistry would misuse a leased-liveness primitive for a value
        # with no liveness concept.
        fcntl.flock(lock_fd, fcntl.LOCK_EX)  # lifecycle-gate-allow: one-shot idempotent file-create mutex, not a lifecycle election
        try:
            try:
                existing = path.read_text().strip()
            except OSError:
                existing = ""
            if existing:
                return existing

            new_id = str(uuid4())
            tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
            fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            try:
                os.write(fd, new_id.encode("utf-8"))
            finally:
                os.close(fd)
            os.replace(str(tmp), str(path))
            return new_id
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


# ── Live-MCP-session lease (nexus-c8yvj) ────────────────────────────────────
#
# session_end_flush() (nexus/hooks.py) runs as a SEPARATE OS process from
# the live nx-mcp server -- a detached grandchild launched by
# nx-session-end-launcher, not a child of the MCP process -- so it never
# inherits the MCP's in-process os.environ["NX_T1_SESSION"] /
# ["NX_T1_SESSION_ID"] mutation (mcp/core.py's _t1_chroma_lifespan Branch
# 0). Before the CLI-dedicated-session work above, this was harmless: T1
# defaulted to the Chroma-backed T1Database, whose session_id resolved via
# resolve_active_session_id() -- the SAME on-disk chain any sibling process
# (including the hook) could compute, so hook and MCP always agreed on the
# session partition. Once T1 hard-defaults to SERVICE, a detached process
# with no inherited env falls into the CLI-dedicated branch below --
# resolving a PERSISTED id shared by every bare-CLI invocation on the
# machine, completely disjoint from the live MCP session. flagged_entries()
# then silently reads the WRONG (always-empty) session: no exception, just
# "Flushed 0" on every SessionEnd, forever.
#
# Fix: the live MCP publishes its minted (session_id, session_token) to a
# lease file the instant it mints (mirrors the existing
# t1_addr.<session_id> Chroma-lease pattern) and removes it on teardown.
# get_t1_database() checks for a live lease BEFORE falling into the
# CLI-dedicated path -- never by re-deriving/re-minting via
# resolve_active_session_id() (that chain resolves the SAME id a live MCP
# already minted a token for; re-minting ROTATES it via
# HttpTokenStore.start_session's ON CONFLICT DO UPDATE, the exact hazard
# the CLI-dedicated design above was built to avoid). This mechanism only
# ever READS a lease the MCP already published; it never mints.
_T1_SESSION_LEASE_PREFIX = "t1_session_lease."


def _t1_session_lease_path(session_id: str, config_dir: Path) -> Path:
    return config_dir / f"{_T1_SESSION_LEASE_PREFIX}{session_id}"


def publish_t1_session_lease(session_id: str, session_token: str, config_dir: Path) -> None:
    """Publish a live MCP session's minted T1 token to a lease file.

    Called by :mod:`nexus.mcp.core`'s ``_t1_chroma_lifespan`` Branch 0
    immediately after a successful mint. Atomic temp-file + ``os.replace``
    publish (same pattern as :func:`_cli_dedicated_session_id`'s cache-file
    write) so a concurrent reader never observes a torn write. Mode
    ``0o600`` -- the token is a secret. Best-effort by convention at the
    call site: a failure here is a lost convenience lease for a detached
    hook process, not a correctness-critical failure for the live MCP
    session itself, so callers should log-and-continue rather than fail
    session startup on a publish error.
    """
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _t1_session_lease_path(session_id, config_dir)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, session_token.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


def read_t1_session_lease(session_id: str, config_dir: Path) -> str | None:
    """Read a published T1 session lease token, or ``None`` if absent/unreadable.

    No liveness probe on the token itself: a stale lease (the live MCP
    crashed without running its teardown, e.g. SIGKILL/OOM) is caught
    downstream by the service's own 401 on the now-invalid token, which
    surfaces as a plain ``RuntimeError`` from ``HttpScratchStore`` -- the
    SAME best-effort "T1 unavailable, logged, flush skipped" outcome
    ``hooks.session_end_flush`` already documents and tolerates for the
    pre-existing narrow stdin-EOF race. This function intentionally adds no
    orphan sweep / TTL of its own -- it is a plain read.
    """
    path = _t1_session_lease_path(session_id, config_dir)
    try:
        token = path.read_text().strip()
    except OSError:
        return None
    return token or None


def clear_t1_session_lease(session_id: str, config_dir: Path) -> None:
    """Remove a published T1 session lease file (live-MCP teardown cleanup).

    Best-effort / idempotent: a missing file is not an error (double-clear,
    or a lease that was never published because no resolvable session id
    existed for this process -- see the ``no_resolvable_session`` branch in
    ``mcp.core._t1_chroma_lifespan``). Removing the lease promptly on
    teardown ensures a stale lease is never read by a later, unrelated
    process once this session has genuinely ended.
    """
    path = _t1_session_lease_path(session_id, config_dir)
    try:
        path.unlink()
    except OSError:
        pass


class _CliDedicatedScratchStore:
    """T1Database-shaped wrapper around ``HttpScratchStore`` for the
    CLI-dedicated session path.

    Self-heals a stale/rotated token (nx_plan_audit MEDIUM finding, design
    history item 4 above): on a 401 (``SESSION_UNAUTHORIZED_MARKER``) from
    the wrapped store, re-mints the dedicated session's token once and
    retries the failed call before propagating. Exactly one retry -- a
    second failure on the same call propagates immediately rather than
    looping.
    """

    def __init__(self, dedicated_id: str, store) -> None:
        self._dedicated_id = dedicated_id
        self._store = store

    def _remint(self) -> None:
        from nexus.db.http_scratch_store import HttpScratchStore  # noqa: PLC0415 — deferred import (rare/branch-local path)
        from nexus.db.t2.http_token_store import HttpTokenStore  # noqa: PLC0415 — deferred import (rare/branch-local path)

        try:
            with HttpTokenStore() as token_store:
                minted = token_store.start_session(self._dedicated_id)
        except Exception as exc:  # noqa: BLE001 — clean-error boundary (nexus-c8yvj finding 2): HttpTokenStore.start_session raises httpx.HTTPStatusError on a non-2xx (e.g. bad NX_SERVICE_TOKEN), which is NOT a RuntimeError and would otherwise bypass _call's `except RuntimeError` and commands/scratch.py's _clean_service_errors, surfacing as a raw traceback instead of a clean message
            raise RuntimeError(
                f"T1 CLI-dedicated session re-mint failed for session {self._dedicated_id!r}: {exc}"
            ) from exc
        self._store = HttpScratchStore(
            session_id=self._dedicated_id,
            _session_token=minted["session_token"],
        )

    def _call(self, name: str, *args, **kwargs):
        from nexus.db.http_scratch_store import SESSION_UNAUTHORIZED_MARKER  # noqa: PLC0415 — deferred import (rare/branch-local path)

        try:
            return getattr(self._store, name)(*args, **kwargs)
        except RuntimeError as exc:
            if SESSION_UNAUTHORIZED_MARKER not in str(exc):
                raise
            _log.warning(
                "t1_cli_dedicated_session_selfheal",
                session_id=self._dedicated_id,
                op=name,
            )
            self._remint()
            # Exactly one retry: a second failure propagates to the caller.
            return getattr(self._store, name)(*args, **kwargs)

    @property
    def session_id(self) -> str:
        return self._store.session_id

    def put(self, *args, **kwargs):
        return self._call("put", *args, **kwargs)

    def get(self, *args, **kwargs):
        return self._call("get", *args, **kwargs)

    def search(self, *args, **kwargs):
        return self._call("search", *args, **kwargs)

    def list_entries(self, *args, **kwargs):
        return self._call("list_entries", *args, **kwargs)

    def flagged_entries(self, *args, **kwargs):
        return self._call("flagged_entries", *args, **kwargs)

    def flag(self, *args, **kwargs):
        return self._call("flag", *args, **kwargs)

    def unflag(self, *args, **kwargs):
        return self._call("unflag", *args, **kwargs)

    def promote(self, *args, **kwargs):
        return self._call("promote", *args, **kwargs)

    def delete(self, *args, **kwargs):
        return self._call("delete", *args, **kwargs)

    def clear(self, *args, **kwargs):
        return self._call("clear", *args, **kwargs)

    def resolve_prefix_candidates(self, *args, **kwargs):
        return self._call("resolve_prefix_candidates", *args, **kwargs)


# ── Factory ───────────────────────────────────────────────────────────────────


def get_t1_database(
    session_id: str | None = None,
    client=None,
) -> "T1Database":
    """Return the authoritative T1 scratch store for this process.

    RDR-152 bead nexus-gmiaf.13 routing seam, extended by nexus-rn3wo.1 and
    nexus-c8yvj:

    * ``NX_STORAGE_BACKEND_T1=service`` (or global ``NX_STORAGE_BACKEND=service``,
      now the hard default -- see :mod:`nexus.db.storage_mode`):
        - Inherited live MCP session (``NX_T1_SESSION`` / ``NX_T1_SESSION_ID``
          set in env) → :class:`~nexus.db.http_scratch_store.HttpScratchStore`
          directly, unchanged from before.
        - No inherited session, but ``resolve_active_session_id()`` resolves
          a real id AND that live MCP session published a lease (nexus-c8yvj:
          :mod:`nexus.mcp.core`'s ``_t1_chroma_lifespan`` Branch 0 writes one
          right after its own mint) → :class:`~nexus.db.http_scratch_store.HttpScratchStore`
          bound to that SAME session/token, read via :func:`read_t1_session_lease`.
          Never mints/rotates -- this is how a detached process (the
          SessionEnd hook) reaches the live MCP session's T1 data.
        - No inherited session and no lease → mints a CLI-dedicated,
          persisted session id (see :func:`_cli_dedicated_session_id`) and
          returns a :class:`_CliDedicatedScratchStore` (self-healing wrapper).
    * Explicit ``sqlite`` opt-out (or ``NX_T1_ISOLATED=1`` / ``NEXUS_SKIP_T1=1``)
      → :class:`T1Database` (ChromaDB path, unchanged).

    The ``session_id`` and ``client`` arguments are forwarded to ``T1Database``
    on the Chroma path; they are ignored on the service path (session_id is
    sourced from ``NX_T1_SESSION``/``NX_T1_SESSION_ID`` env, a published
    lease for a resolvable session id, or the CLI-dedicated cache file, in
    that order).

    Returns a ``T1Database``-shaped object: callers use ``put``, ``get``,
    ``search``, ``list_entries``, ``flagged_entries``, ``flag``, ``unflag``,
    ``promote``, ``delete``, ``clear``, ``resolve_prefix_candidates``, and
    the ``session_id`` property.  All methods are available on every path.
    """
    from nexus.db.storage_mode import StorageBackend, storage_backend_for  # noqa: PLC0415 — deliberate function-local import (factory-time backend selection)

    # nexus-h8rf6 (shakeout finding 13): explicit isolation WINS over backend
    # routing. NX_T1_ISOLATED=1 is the documented escape hatch every T1 error
    # message recommends ("in-process ephemeral scratch"); pre-fix it was only
    # honored inside T1Database's Chroma-path constructor, which the SERVICE
    # branch below never reaches — dead code in exactly the installs that
    # need it (a bare CLI in service mode cannot safely mint a session token).
    if os.environ.get("NX_T1_ISOLATED") == "1" or os.environ.get("NEXUS_SKIP_T1") == "1":
        return T1Database(session_id=session_id, client=client)

    if storage_backend_for("t1") == StorageBackend.SERVICE:
        from nexus.db.http_scratch_store import HttpScratchStore  # noqa: PLC0415 — rare/branch-local import (SERVICE backend path only)

        has_inherited_session = bool(
            os.environ.get("NX_T1_SESSION", "").strip()
        ) or bool(os.environ.get("NX_T1_SESSION_ID", "").strip())
        if has_inherited_session:
            return HttpScratchStore()  # type: ignore[return-value]

        from nexus.config import nexus_config_dir  # noqa: PLC0415 — rare/branch-local import (CLI-dedicated / lease path only)

        config_dir = nexus_config_dir()

        # nexus-c8yvj: a DETACHED process (no inherited NX_T1_SESSION/
        # NX_T1_SESSION_ID) -- most notably the SessionEnd hook, which runs
        # as a separate OS process launched by nx-session-end-launcher and
        # so never sees the live MCP's in-process env mutation -- may still
        # be able to resolve the IDENTITY of a live MCP session for this
        # Claude session via the on-disk current_session pointer. If that
        # live session ALSO published a lease (mcp.core._t1_chroma_lifespan
        # Branch 0, right after its own mint), use its token DIRECTLY.
        # NEVER re-mint against a resolved session id here -- see the
        # design history above for why that rotates the live MCP's token
        # out from under it. No lease found means either no live MCP
        # session exists for this shell, or it never resolved a session id
        # itself -- either way, fall through to the CLI-dedicated path
        # below exactly as before this fix.
        from nexus.session import resolve_active_session_id  # noqa: PLC0415 — rare/branch-local import (lease-lookup only; NEVER used to mint)

        candidate_id = resolve_active_session_id()
        if candidate_id and candidate_id != "unknown":
            leased_token = read_t1_session_lease(candidate_id, config_dir)
            if leased_token:
                return HttpScratchStore(  # type: ignore[return-value]
                    session_id=candidate_id, _session_token=leased_token
                )

        # nexus-rn3wo.1: bare CLI, no inherited live MCP session, and no
        # published lease for a resolvable session id either. Mint (or
        # reuse) the CLI-dedicated persisted session id and self-heal on a
        # rotated-token 401 from a racing sibling bare-CLI invocation.
        from nexus.db.t2.http_token_store import HttpTokenStore  # noqa: PLC0415 — rare/branch-local import (CLI-dedicated path only)

        dedicated_id = _cli_dedicated_session_id(config_dir)
        try:
            with HttpTokenStore() as token_store:
                minted = token_store.start_session(dedicated_id)
        except Exception as exc:  # noqa: BLE001 — clean-error boundary (nexus-c8yvj finding 2): HttpTokenStore.start_session raises httpx.HTTPStatusError on a non-2xx (e.g. bad NX_SERVICE_TOKEN), which is NOT a RuntimeError and would otherwise bypass commands/scratch.py's _clean_service_errors, surfacing as a raw traceback instead of a clean message
            raise RuntimeError(
                f"T1 CLI-dedicated session mint failed for session {dedicated_id!r}: {exc}"
            ) from exc
        store = HttpScratchStore(
            session_id=dedicated_id, _session_token=minted["session_token"]
        )
        return _CliDedicatedScratchStore(dedicated_id, store)  # type: ignore[return-value]

    return T1Database(session_id=session_id, client=client)
