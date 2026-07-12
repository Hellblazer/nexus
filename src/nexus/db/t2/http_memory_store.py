# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""HttpMemoryStore — thin HTTP client over the RDR-152 Java memory service.

Drop-in replacement for :class:`~nexus.db.t2.memory_store.MemoryStore`.
Activated by setting ``NX_STORAGE_BACKEND=service`` (or
``NX_STORAGE_BACKEND_MEMORY=service``).

Config:
    NX_SERVICE_HOST  — service host (default: 127.0.0.1)
    NX_SERVICE_PORT  — service port (required; raises if missing)
    NX_SERVICE_TOKEN — bearer token (required; raises if missing)

All methods send ``Authorization: Bearer <token>`` and
``X-Nexus-Tenant: default`` (``DEFAULT_TENANT``) on every request.

Server-side vs client-composed methods:

Server-side (all storage/SQL logic runs on the Java service):
    put, put_or_merge, get, resolve_title, search, list_entries,
    get_projects_with_prefix, search_glob, search_by_tag, get_all,
    delete, expire, merge_memories, flag_stale_memories

    put_or_merge is server-side (POST /v1/memory/put_or_merge):
    the Jaccard scan + conditional merge-or-upsert runs atomically
    in a single Java transaction.  Moving it server-side eliminates
    the TOCTOU window of the former client-composed path and ensures
    that Phase-2 stores inherit the correct pattern.

Client-composed (pure-Python logic over server-side data):
    find_overlapping_memories — Jaccard computation is Python;
        the underlying data is fetched via get_all (server-side).
        Kept client-composed because it is a pure read; no atomicity
        requirement.
"""

from __future__ import annotations

from typing import Any

import httpx

# ── Constants ──────────────────────────────────────────────────────────────────

#: Default tenant matching TenantConstants.DEFAULT_TENANT in the Java service.
DEFAULT_TENANT: str = "default"

#: Stopwords shared with MemoryStore for Jaccard overlap computation.
_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "in", "of", "for", "to", "and", "or", "is", "are", "was",
    "it", "that", "this", "with", "on", "at", "by", "from", "as", "be", "not",
})

#: All-pairs threshold: projects with ≤ this many entries get full-recall Jaccard.
_ALL_PAIRS_MAX_ENTRIES = 1000


# RDR-152 nexus-fjwxh: env-only resolution replaced by the centralized
# resolver (env halves -> ServiceRegistry lease -> fail loud), so the
# T2 service-mode default works wherever the supervisor is running.
# nexus-bikit.4: construction, credential/endpoint refresh-on-401, and the
# HTTP transport itself (_post/_get/_delete) are now inherited wholesale
# from RefreshableHttpStoreMixin — HttpMemoryStore no longer bakes a
# ``self._headers`` dict or a ``httpx.Client(base_url=..., headers=...)``
# at construction time, which is what let a rotated bearer or a
# supervisor-restart port change go silently stale for the life of the
# instance. See ``nx memory get -p nexus -t design-bikit-refreshable-http-store-mixin.md``.
from nexus.db.t2._raw_handle_guard import RawHandleGuardMixin
from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin


# ── HttpMemoryStore ────────────────────────────────────────────────────────────


class HttpMemoryStore(RawHandleGuardMixin, RefreshableHttpStoreMixin):
    """MemoryStore drop-in that delegates to the RDR-152 Java HTTP service.

    Uses a keep-alive :class:`httpx.Client` connection pool via
    :class:`~nexus.db.t2._refreshable_client.RefreshableHttpStoreMixin`,
    which resolves ``NX_SERVICE_HOST``, ``NX_SERVICE_PORT``, and
    ``NX_SERVICE_TOKEN`` (or a managed ``service_url``/``service_token``)
    fresh on construction AND self-heals (re-resolve + retry once) on a
    401 or a connection-refused/reset — see the mixin's own docstring for
    the full resolution order. ``__init__`` is inherited unchanged (this
    class's constructor signature — ``(base_url=None, tenant=DEFAULT_TENANT,
    *, _token=None)`` — matches the mixin's pinned contract exactly, so no
    override is needed).

    Args:
        base_url: Optional override for the service base URL
            (``http://<host>:<port>``).  When supplied without ``_token``,
            only the token half is re-resolved (host/port need not also be
            independently resolvable).
        tenant:   Tenant to stamp on every request (default: ``DEFAULT_TENANT``).
    """

    # ── Write ──────────────────────────────────────────────────────────────────

    def put(
        self,
        project: str,
        title: str,
        content: str,
        tags: str = "",
        ttl: int | None = 30,
        agent: str | None = None,
        session: str | None = None,
    ) -> int:
        """Upsert a memory entry. Returns the row id."""
        payload: dict[str, Any] = {
            "project": project,
            "title": title,
            "content": content,
            "tags": tags or "",
            "ttl": ttl,
        }
        if agent is not None:
            payload["agent"] = agent
        if session is not None:
            payload["session"] = session

        resp = self._post("/v1/memory/put", payload)
        return int(resp["id"])

    def import_entry(
        self,
        project: str,
        title: str,
        content: str,
        timestamp: str,
        tags: str = "",
        ttl: int | None = None,
        agent: str | None = None,
        session: str | None = None,
        access_count: int = 0,
        last_accessed: str | None = None,
    ) -> int:
        """Fidelity-preserving ETL import (bead nexus-gmiaf.8, RDR-152 P1.8).

        Unlike :meth:`put` (which routes through ``/v1/memory/put`` and
        lets the Java service stamp ``timestamp=now()``), this method calls
        ``POST /v1/memory/import`` which writes ``timestamp``,
        ``access_count``, and ``last_accessed`` **verbatim** from the
        source row.  This is the correct path for any ETL that must
        preserve event-time (e.g. the telemetry store .12, where
        ``timestamp`` IS the event-time).

        The Java side uses ``ON CONFLICT (tenant_id, project, title)
        DO UPDATE SET … = EXCLUDED.*`` so re-runs are idempotent and
        content changes in the source propagate on the next run, while
        source ``timestamp`` / ``access_count`` are preserved.

        Args:
            project:       Project namespace.
            title:         Entry title (unique within project).
            content:       Entry body.
            timestamp:     ISO-8601 UTC string, e.g. ``"2026-05-15T08:30:00Z"``.
            tags:          Comma-separated tag string (default ``""``).
            ttl:           Time-to-live in days (``None`` for permanent).
            agent:         Optional agent attribution.
            session:       Optional session id.
            access_count:  Source access count (default 0).
            last_accessed: ISO-8601 UTC string or ``None``
                           (``None`` means never accessed — stored as SQL NULL).

        Returns:
            The Postgres row id (BIGSERIAL, always positive).
        """
        payload: dict[str, Any] = {
            "project":      project,
            "title":        title,
            "content":      content,
            "tags":         tags or "",
            "ttl":          ttl,
            "timestamp":    timestamp,
            "access_count": access_count,
        }
        if agent is not None:
            payload["agent"] = agent
        if session is not None:
            payload["session"] = session
        if last_accessed is not None:
            payload["last_accessed"] = last_accessed

        resp = self._post("/v1/memory/import", payload)
        return int(resp["id"])

    @staticmethod
    def build_import_row(
        project: str,
        title: str,
        content: str,
        timestamp: str,
        tags: str = "",
        ttl: int | None = None,
        agent: str | None = None,
        session: str | None = None,
        access_count: int = 0,
        last_accessed: str | None = None,
    ) -> dict[str, Any]:
        """Build one ``import_entries_batch`` row dict (same field shape as
        :meth:`import_entry`'s payload). Optional fields omitted when ``None``."""
        row: dict[str, Any] = {
            "project": project, "title": title, "content": content,
            "tags": tags or "", "ttl": ttl, "timestamp": timestamp,
            "access_count": access_count,
        }
        if agent is not None:
            row["agent"] = agent
        if session is not None:
            row["session"] = session
        if last_accessed is not None:
            row["last_accessed"] = last_accessed
        return row

    def import_entries_batch(self, rows: list[dict[str, Any]]) -> int:
        """RDR-176 P3 (bead nexus-t9rmg.18): fidelity-preserving BULK import.

        POSTs all *rows* (built via :meth:`build_import_row`) to
        ``/v1/memory/import_batch`` in ONE request — the service lands them in a
        single multi-row INSERT under one tenant transaction. Collapses an
        N-row migration leg from N round-trips to ceil(N/batch). The caller is
        responsible for keeping each batch within the per-write quota (≤300).
        Returns the number of rows imported. An empty list is a no-op.
        """
        if not rows:
            return 0
        resp = self._post("/v1/memory/import_batch", {"rows": rows})
        return int(resp.get("imported", 0))

    # ── Read ───────────────────────────────────────────────────────────────────

    def get(
        self,
        project: str | None = None,
        title: str | None = None,
        id: int | None = None,
    ) -> dict[str, Any] | None:
        """Retrieve a single entry by (project, title) or by numeric ID."""
        if id is not None:
            params: dict[str, Any] = {"id": id}
        elif project is not None and title is not None:
            params = {"project": project, "title": title}
        else:
            raise ValueError("Provide either id or both project and title.")

        # The mixin's _get raises httpx.HTTPStatusError on ANY non-2xx
        # (including 404 — self-heal retry only applies to 401/connection
        # errors, per _is_retryable_endpoint_error, so a genuine 404
        # propagates immediately). get()'s contract is "not found -> None",
        # not an exception, so catch specifically the 404 case here and
        # re-raise anything else untouched.
        try:
            resp = self._get("/v1/memory/get", params=params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        return _normalize(resp)

    def resolve_title(
        self,
        project: str,
        title: str,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """Exact-then-prefix title resolution.

        Returns ``(entry, [])`` on unique match,
        ``(None, candidates)`` on multiple prefix matches,
        ``(None, [])`` when nothing matches.
        """
        data = self._get("/v1/memory/resolve", params={"project": project, "title": title})
        entry = _normalize(data.get("entry")) if data.get("entry") is not None else None
        candidates = [_normalize(c) for c in (data.get("candidates") or [])]
        return entry, candidates

    def search(
        self,
        query: str,
        project: str | None = None,
        access: str = "track",
    ) -> list[dict[str, Any]]:
        """FTS search. Returns rows ordered by relevance.

        Args:
            query:   Search query (sanitized server-side by plainto_tsquery).
            project: Optional project filter.
            access:  Access tracking policy: ``"track"`` (default) increments
                     access_count on returned rows; ``"silent"`` skips it
                     (for internal consolidation scans).
        """
        payload: dict[str, Any] = {"query": query, "access": access}
        if project:
            payload["project"] = project
        resp = self._post("/v1/memory/search", payload)
        if isinstance(resp, list):
            return [_normalize(r) for r in resp]
        return []

    def list_entries(
        self,
        project: str | None = None,
        agent: str | None = None,
    ) -> list[dict[str, Any]]:
        """List entries (summary view) ordered by timestamp descending."""
        params: dict[str, str] = {}
        if project:
            params["project"] = project
        if agent:
            params["agent"] = agent
        resp = self._get("/v1/memory/list", params=params)
        return [_normalize_summary(r) for r in resp]

    def get_projects_with_prefix(self, prefix: str) -> list[dict[str, Any]]:
        """Return distinct project namespaces starting with *prefix*."""
        if not prefix:
            return []
        return self._get("/v1/memory/projects", params={"prefix": prefix})

    def search_glob(self, query: str, project_glob: str) -> list[dict[str, Any]]:
        """FTS search scoped to projects matching a GLOB pattern."""
        resp = self._post("/v1/memory/search_glob", {"query": query, "project_glob": project_glob})
        if isinstance(resp, list):
            return [_normalize(r) for r in resp]
        return []

    def search_by_tag(self, query: str, tag: str) -> list[dict[str, Any]]:
        """FTS search scoped to entries whose tags contain *tag*."""
        resp = self._post("/v1/memory/search_by_tag", {"query": query, "tag": tag})
        if isinstance(resp, list):
            return [_normalize(r) for r in resp]
        return []

    def get_all(self, project: str) -> list[dict[str, Any]]:
        """Return all entries for *project* with full column data."""
        resp = self._get("/v1/memory/all", params={"project": project})
        return [_normalize(r) for r in resp]

    # ── Delete ─────────────────────────────────────────────────────────────────

    def delete(
        self,
        project: str | None = None,
        title: str | None = None,
        id: int | None = None,
    ) -> bool:
        """Delete an entry by (project, title) or by numeric id."""
        if id is not None:
            params: dict[str, Any] = {"id": id}
        elif project is not None and title is not None:
            params = {"project": project, "title": title}
        else:
            raise ValueError("Provide either id or both project and title.")
        resp = self._delete("/v1/memory/delete", params=params)
        return bool(resp.get("deleted", False))

    # ── Housekeeping ───────────────────────────────────────────────────────────

    def expire(self) -> list[int]:
        """Delete TTL-expired memory entries. Returns list of deleted row IDs."""
        resp = self._post("/v1/memory/expire", {})
        return [int(i) for i in resp.get("deleted_ids", [])]

    # ── Consolidation (RDR-061 E6) ─────────────────────────────────────────────

    def find_overlapping_memories(
        self,
        project: str,
        min_similarity: float = 0.7,
        limit: int = 50,
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """CLIENT-COMPOSED: Jaccard overlap computed in Python over server-fetched data.

        Fetches all entries via get_all, then applies the same Jaccard logic
        as MemoryStore.find_overlapping_memories. The actual data retrieval is
        server-side; the overlap computation is client-side.
        """
        entries = self.get_all(project)
        if len(entries) < 2:
            return []

        def _words(text: str) -> set[str]:
            return {
                w.lower() for w in text.split()
                if len(w) > 2 and w.lower() not in _STOPWORDS
            }

        word_sets: list[tuple[dict[str, Any], set[str]]] = [
            (e, _words(e.get("content", ""))) for e in entries
        ]
        word_sets = [(e, w) for e, w in word_sets if w]

        pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []

        # Full-recall all-pairs path for normal-sized projects.
        if len(word_sets) <= _ALL_PAIRS_MAX_ENTRIES:
            for i in range(len(word_sets)):
                e1, w1 = word_sets[i]
                for j in range(i + 1, len(word_sets)):
                    e2, w2 = word_sets[j]
                    jaccard = len(w1 & w2) / len(w1 | w2)
                    if jaccard >= min_similarity:
                        pairs.append((e1, e2))
                        if len(pairs) >= limit:
                            return pairs
            return pairs

        # Large-project: FTS prefilter (bounded recall, O(n^2) protection).
        by_id: dict[Any, set[str]] = {e["id"]: w for e, w in word_sets}
        seen: set[tuple[int, int]] = set()
        for e1, w1 in word_sets:
            words = [
                w for w in e1.get("content", "").split()[:5]
                if w.lower() not in _STOPWORDS and len(w) > 2
            ]
            snippet = " ".join(words[:3])
            if not snippet:
                continue
            try:
                candidates = self.search(snippet, project=project, access="silent")
            except (ValueError, httpx.HTTPError):
                continue
            for e2 in candidates:
                if e2["id"] == e1["id"]:
                    continue
                pair_key = tuple(sorted((e1["id"], e2["id"])))
                if pair_key in seen:
                    continue
                seen.add(pair_key)
                w2 = by_id.get(e2["id"])
                if not w2:
                    continue
                jaccard = len(w1 & w2) / len(w1 | w2)
                if jaccard >= min_similarity:
                    pairs.append((e1, e2))
                    if len(pairs) >= limit:
                        return pairs
        return pairs

    def merge_memories(
        self,
        keep_id: int,
        delete_ids: list[int],
        merged_content: str,
    ) -> None:
        """SERVER-SIDE: atomic UPDATE + DELETE in one transaction on the Java service."""
        if keep_id in delete_ids:
            raise ValueError(
                f"keep_id ({keep_id}) must not be in delete_ids — "
                "would discard the entry meant to be kept"
            )
        # Same shape as get()'s 404-as-None handling above: a 409 here is a
        # semantic "not found" signal (not retryable per
        # _is_retryable_endpoint_error), so catch it specifically and
        # re-raise anything else untouched.
        try:
            self._post(
                "/v1/memory/merge",
                {"keep_id": keep_id, "delete_ids": delete_ids, "merged_content": merged_content},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                raise KeyError(
                    f"keep_id {keep_id} not found — aborted merge to prevent data loss"
                ) from exc
            raise

    def put_or_merge(
        self,
        project: str,
        title: str,
        content: str,
        tags: str = "",
        ttl: int | None = 30,
        agent: str | None = None,
        session: str | None = None,
        min_similarity: float = 0.5,
    ) -> tuple[int, str]:
        """SERVER-SIDE: Jaccard scan + conditional merge or insert in one transaction.

        Delegates to ``POST /v1/memory/put_or_merge``.  The Jaccard overlap
        computation and the conditional UPDATE/INSERT run atomically on the
        Java service, eliminating the TOCTOU window of the former client-composed
        path (get_all → merge_memories).

        Returns ``(row_id, action)`` where ``action`` is ``"inserted"`` or
        ``"merged"``.
        """
        payload: dict[str, Any] = {
            "project": project,
            "title": title,
            "content": content,
            "tags": tags or "",
            "ttl": ttl,
            "min_similarity": min_similarity,
        }
        if agent is not None:
            payload["agent"] = agent
        if session is not None:
            payload["session"] = session
        resp = self._post("/v1/memory/put_or_merge", payload)
        return int(resp["id"]), str(resp["action"])

    def flag_stale_memories(
        self,
        project: str,
        idle_days: int = 30,
    ) -> list[dict[str, Any]]:
        """SERVER-SIDE: SQL date comparison on the Java service."""
        resp = self._get(
            "/v1/memory/flag_stale",
            params={"project": project, "idle_days": str(idle_days)},
        )
        return [_normalize(r) for r in resp]


# ── Normalisation helpers ──────────────────────────────────────────────────────

def _normalize(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Convert a service response row to MemoryStore-compatible dict.

    Normalization rules that match ``dict(zip(_COLUMNS, row))`` from SQLite MemoryStore:

    - ``id``, ``access_count``, ``ttl``: cast to ``int`` (JSON may send as float)
    - ``tags``: guaranteed to be a string by the Java service (``""`` if no tags);
      fallback to ``""`` here for defence-in-depth
    - ``last_accessed``: Java service sends ``""`` when NULL (matching SQLite
      ``DEFAULT ''``); ensure it's always a string (never ``None`` in the dict)
    - ``timestamp``: Java service sends UTC second-precision ISO string
      (``"YYYY-MM-DDTHH:MM:SSZ"``); pass through as-is
    """
    if row is None:
        return None
    # Numeric fields
    if "id" in row and row["id"] is not None:
        row["id"] = int(row["id"])
    if "access_count" in row and row["access_count"] is not None:
        row["access_count"] = int(row["access_count"])
    if "ttl" in row and row["ttl"] is not None:
        row["ttl"] = int(row["ttl"])
    # tags: always a string (Java service guarantees ""; defence-in-depth)
    if row.get("tags") is None:
        row["tags"] = ""
    # last_accessed: always a string; Java sends "" for NULL rows
    if row.get("last_accessed") is None:
        row["last_accessed"] = ""
    return row


def _normalize_summary(row: dict[str, Any]) -> dict[str, Any]:
    """Normalise a list_entries summary row."""
    if "id" in row and row["id"] is not None:
        row["id"] = int(row["id"])
    return row
