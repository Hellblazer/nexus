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
    put, get, resolve_title, search, list_entries,
    get_projects_with_prefix, search_glob, search_by_tag, get_all,
    delete, expire, merge_memories, flag_stale_memories

Client-composed (Jaccard/Python logic over server-side get_all results):
    find_overlapping_memories — Jaccard computation is Python;
        the underlying data is fetched via get_all (server-side).
    put_or_merge — Jaccard scan over get_all + conditional put.

Both client-composed methods call server-side endpoints for data;
they do NOT contain any SQL or storage logic themselves.
"""

from __future__ import annotations

import math
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog

_log = structlog.get_logger(__name__)

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


def _resolve_config() -> tuple[str, int, str]:
    """Return (host, port, token) from environment.

    Raises:
        RuntimeError: if NX_SERVICE_PORT or NX_SERVICE_TOKEN are not set.
    """
    host = os.environ.get("NX_SERVICE_HOST", "127.0.0.1")
    port_str = os.environ.get("NX_SERVICE_PORT", "")
    token = os.environ.get("NX_SERVICE_TOKEN", "")

    if not port_str:
        raise RuntimeError(
            "NX_SERVICE_PORT is required when NX_STORAGE_BACKEND=service. "
            "Set it to the port where the nexus-service is listening."
        )
    try:
        port = int(port_str)
    except ValueError as exc:
        raise RuntimeError(f"NX_SERVICE_PORT must be an integer, got: {port_str!r}") from exc

    if not token:
        raise RuntimeError(
            "NX_SERVICE_TOKEN is required when NX_STORAGE_BACKEND=service. "
            "Set it to the bearer token configured in the nexus-service."
        )

    return host, port, token


# ── HttpMemoryStore ────────────────────────────────────────────────────────────


class HttpMemoryStore:
    """MemoryStore drop-in that delegates to the RDR-152 Java HTTP service.

    Uses a keep-alive :class:`httpx.Client` connection pool.  Reads
    ``NX_SERVICE_HOST``, ``NX_SERVICE_PORT``, and ``NX_SERVICE_TOKEN``
    from the environment at construction time.

    Args:
        base_url: Optional override for the service base URL
            (``http://<host>:<port>``).  When supplied, ``host``/``port``
            env-vars are ignored; the token env-var is still required.
        tenant:   Tenant to stamp on every request (default: ``DEFAULT_TENANT``).
    """

    def __init__(
        self,
        base_url: str | None = None,
        tenant: str = DEFAULT_TENANT,
        *,
        _token: str | None = None,
    ) -> None:
        if base_url is not None:
            if _token is None:
                _token = os.environ.get("NX_SERVICE_TOKEN", "")
                if not _token:
                    raise RuntimeError(
                        "NX_SERVICE_TOKEN is required when NX_STORAGE_BACKEND=service."
                    )
            self._base_url = base_url.rstrip("/")
        else:
            host, port, token = _resolve_config()
            self._base_url = f"http://{host}:{port}"
            _token = token

        self._tenant = tenant
        self._headers = {
            "Authorization": f"Bearer {_token}",
            "X-Nexus-Tenant": tenant,
            "Content-Type": "application/json",
        }
        # Keep-alive connection pool (S0.3 requirement).
        self._client = httpx.Client(
            base_url=self._base_url,
            headers=self._headers,
            timeout=30.0,
        )
        _log.info("http_memory_store.init", base_url=self._base_url, tenant=tenant)

    def close(self) -> None:
        """Close the keep-alive connection pool (idempotent)."""
        self._client.close()
        _log.debug("http_memory_store.closed")

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

    # ── Read ───────────────────────────────────────────────────────────────────

    def get(
        self,
        project: str | None = None,
        title: str | None = None,
        id: int | None = None,
    ) -> dict[str, Any] | None:
        """Retrieve a single entry by (project, title) or by numeric ID."""
        if id is not None:
            resp = self._client.get("/v1/memory/get", params={"id": id})
        elif project is not None and title is not None:
            resp = self._client.get("/v1/memory/get", params={"project": project, "title": title})
        else:
            raise ValueError("Provide either id or both project and title.")

        if resp.status_code == 404:
            return None
        self._raise_for_status(resp, "get")
        return _normalize(resp.json())

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
        resp = self._client.get("/v1/memory/resolve", params={"project": project, "title": title})
        self._raise_for_status(resp, "resolve_title")
        data = resp.json()
        entry = _normalize(data.get("entry")) if data.get("entry") is not None else None
        candidates = [_normalize(c) for c in (data.get("candidates") or [])]
        return entry, candidates

    def search(
        self,
        query: str,
        project: str | None = None,
        access: str = "track",  # noqa: ARG002 — access tracking is server-managed
    ) -> list[dict[str, Any]]:
        """FTS search. Returns rows ordered by relevance."""
        payload: dict[str, Any] = {"query": query}
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
        resp = self._client.get("/v1/memory/list", params=params)
        self._raise_for_status(resp, "list_entries")
        return [_normalize_summary(r) for r in resp.json()]

    def get_projects_with_prefix(self, prefix: str) -> list[dict[str, Any]]:
        """Return distinct project namespaces starting with *prefix*."""
        if not prefix:
            return []
        resp = self._client.get("/v1/memory/projects", params={"prefix": prefix})
        self._raise_for_status(resp, "get_projects_with_prefix")
        return resp.json()

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
        resp = self._client.get("/v1/memory/all", params={"project": project})
        self._raise_for_status(resp, "get_all")
        return [_normalize(r) for r in resp.json()]

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
        resp = self._client.delete("/v1/memory/delete", params=params)
        self._raise_for_status(resp, "delete")
        return bool(resp.json().get("deleted", False))

    # ── Housekeeping ───────────────────────────────────────────────────────────

    def expire(self) -> list[int]:
        """Delete TTL-expired memory entries. Returns list of deleted row IDs."""
        resp = self._client.post("/v1/memory/expire", json={})
        self._raise_for_status(resp, "expire")
        return [int(i) for i in resp.json().get("deleted_ids", [])]

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
        resp = self._client.post(
            "/v1/memory/merge",
            json={"keep_id": keep_id, "delete_ids": delete_ids, "merged_content": merged_content},
        )
        if resp.status_code == 409:
            raise KeyError(
                f"keep_id {keep_id} not found — aborted merge to prevent data loss"
            )
        self._raise_for_status(resp, "merge_memories")

    def _content_words(self, text: str) -> set[str]:
        """Lowercased word set for Jaccard overlap."""
        return {
            w.lower()
            for w in text.split()
            if len(w) > 2 and w.lower() not in _STOPWORDS
        }

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
        """CLIENT-COMPOSED: Jaccard scan over server-side get_all + conditional put.

        Returns ``(row_id, action)`` where ``action`` is ``"inserted"`` or
        ``"merged"``.

        The Jaccard similarity check runs client-side on data fetched via
        get_all. The merge write (UPDATE) goes to the server via merge_memories
        (server-side atomic). Pure inserts use put (server-side upsert).
        """
        new_words = self._content_words(content)
        if new_words:
            best_id: int | None = None
            best_jaccard = 0.0
            best_content = ""
            for entry in self.get_all(project):
                if entry.get("title") == title:
                    continue
                existing_words = self._content_words(entry.get("content", ""))
                if not existing_words:
                    continue
                jaccard = len(new_words & existing_words) / len(new_words | existing_words)
                if jaccard > best_jaccard:
                    best_jaccard = jaccard
                    best_id = entry["id"]
                    best_content = entry.get("content", "")
            if best_id is not None and best_jaccard >= min_similarity:
                timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                merged = (
                    f"{best_content}\n\n"
                    f"<!-- merged from {title!r} @ {timestamp} "
                    f"(jaccard={best_jaccard:.2f}) -->\n{content}"
                )
                self.merge_memories(best_id, [], merged)
                return best_id, "merged"
        row_id = self.put(
            project, title, content,
            tags=tags, ttl=ttl, agent=agent, session=session,
        )
        return row_id, "inserted"

    def flag_stale_memories(
        self,
        project: str,
        idle_days: int = 30,
    ) -> list[dict[str, Any]]:
        """SERVER-SIDE: SQL date comparison on the Java service."""
        resp = self._client.get(
            "/v1/memory/flag_stale",
            params={"project": project, "idle_days": str(idle_days)},
        )
        self._raise_for_status(resp, "flag_stale_memories")
        return [_normalize(r) for r in resp.json()]

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        """POST JSON payload, raise on error, return parsed JSON."""
        resp = self._client.post(path, json=payload)
        self._raise_for_status(resp, path)
        return resp.json()

    def _raise_for_status(self, resp: httpx.Response, op: str) -> None:
        """Raise a descriptive exception on non-2xx responses."""
        if resp.is_success:
            return
        try:
            detail = resp.json().get("error", resp.text)
        except Exception:
            detail = resp.text
        raise httpx.HTTPStatusError(
            f"HttpMemoryStore.{op} failed: HTTP {resp.status_code}: {detail}",
            request=resp.request,
            response=resp,
        )


# ── Normalisation helpers ──────────────────────────────────────────────────────

def _normalize(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Convert a service response row to MemoryStore-compatible dict.

    The service returns ``null`` for missing optional fields;
    Python callers expect ``None`` for nullable columns and ``""`` for
    ``last_accessed`` (legacy SQLite convention — but we keep it as ``None``
    here since Python callers should handle both via the ``or ""`` pattern).
    """
    if row is None:
        return None
    # Normalise: ensure numeric id is an int
    if "id" in row and row["id"] is not None:
        row["id"] = int(row["id"])
    if "access_count" in row and row["access_count"] is not None:
        row["access_count"] = int(row["access_count"])
    if "ttl" in row and row["ttl"] is not None:
        row["ttl"] = int(row["ttl"])
    # last_accessed: service returns ISO string or null; MemoryStore returns "" on null.
    if row.get("last_accessed") is None:
        row["last_accessed"] = ""
    return row


def _normalize_summary(row: dict[str, Any]) -> dict[str, Any]:
    """Normalise a list_entries summary row."""
    if "id" in row and row["id"] is not None:
        row["id"] = int(row["id"])
    return row
