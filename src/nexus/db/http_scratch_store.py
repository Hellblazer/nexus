# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""HttpScratchStore — thin HTTP client for the RDR-152 Java T1 scratch service.

Drop-in replacement for :class:`~nexus.db.t1.T1Database`.

Activated when ``NX_STORAGE_BACKEND_T1=service`` (or global
``NX_STORAGE_BACKEND=service``).  Requires ``NX_T1_SESSION`` to be set; that
env var replaces the old ``NX_T1_HOST`` / ``NX_T1_PORT`` Chroma discovery
mechanism (RDR-152 bead nexus-gmiaf.13).

Config
------
NX_SERVICE_HOST  — service host (default: 127.0.0.1)
NX_SERVICE_PORT  — service port (required when using service backend)
NX_SERVICE_TOKEN — bearer token (required)
NX_T1_SESSION    — session identifier (required; replaces NX_T1_HOST/PORT)
NX_NEXUS_TENANT  — tenant to stamp on every request (default: "default")

SEARCH BEHAVIOR CHANGE
----------------------
``T1Database.search()`` was **semantic** (ChromaDB ONNX cosine similarity).
``HttpScratchStore.search()`` is **FTS** (Postgres tsvector, OR-query:
``plainto_tsquery('english', q)`` for prose stemming and
``plainto_tsquery('simple', q)`` for exact identifier/tag matching).

This is an intentional upgrade (see 152-FTS-tokenizer-DECISION in T2 project
memory ``rdr``).  Short exact-identifier queries still work via the ``simple``
branch.  Ranking is ts_rank (BM25-like), not cosine; results are still
ordered best-first.

The ``promote()`` method is NOT implemented here.  Promote copies a T1 entry
into T2 memory and requires a T2Database/T2Client reference.  It is kept on
``T1Database`` (Chroma path) and will be ported to the Postgres path in a
follow-on bead once the T2 memory store migration (nexus-gmiaf.7) is stable
enough to pass the T2 reference through.  Callers that reach ``promote()``
on this class get a ``NotImplementedError`` with a clear message.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import structlog

_log = structlog.get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

#: Default tenant matching TenantConstants.DEFAULT_TENANT in the Java service.
DEFAULT_TENANT: str = "default"

#: Env var carrying the session identifier (replaces NX_T1_HOST/PORT).
_SESSION_ENV: str = "NX_T1_SESSION"

#: Header the Java service uses to record the session for observability.
_HEADER_T1_SESSION: str = "X-Nexus-T1-Session"


def _resolve_config() -> tuple[str, int, str]:
    """Return (host, port, token) from environment.

    Raises RuntimeError if NX_SERVICE_PORT or NX_SERVICE_TOKEN are not set.
    """
    host = os.environ.get("NX_SERVICE_HOST", "127.0.0.1")
    port_str = os.environ.get("NX_SERVICE_PORT", "")
    token = os.environ.get("NX_SERVICE_TOKEN", "")

    if not port_str:
        raise RuntimeError(
            "NX_SERVICE_PORT is required when NX_STORAGE_BACKEND_T1=service. "
            "Set it to the port where the nexus-service is listening."
        )
    try:
        port = int(port_str)
    except ValueError as exc:
        raise RuntimeError(
            f"NX_SERVICE_PORT must be an integer, got: {port_str!r}"
        ) from exc

    if not token:
        raise RuntimeError(
            "NX_SERVICE_TOKEN is required when NX_STORAGE_BACKEND_T1=service."
        )

    return host, port, token


def _resolve_session() -> str:
    """Return session identifier from NX_T1_SESSION (required).

    Raises RuntimeError when the env var is absent or blank.
    """
    session = os.environ.get(_SESSION_ENV, "").strip()
    if not session:
        raise RuntimeError(
            f"{_SESSION_ENV} is required when NX_STORAGE_BACKEND_T1=service. "
            "Set it to the session identifier shared across all sibling agents."
        )
    return session


# ── HttpScratchStore ───────────────────────────────────────────────────────────


class HttpScratchStore:
    """T1Database drop-in that delegates to the RDR-152 Java HTTP service.

    SEARCH BEHAVIOR CHANGE: ``search()`` uses FTS (Postgres tsvector) rather
    than vector/cosine (ChromaDB ONNX).  See module docstring for details.

    ``promote()`` is not implemented; see module docstring.

    Uses a keep-alive :class:`httpx.Client` connection pool.

    Args:
        base_url:   Optional URL override (``http://<host>:<port>``).
                    When supplied, host/port env-vars are ignored; token
                    env-var is still required.
        tenant:     Tenant to stamp on every request (default: ``DEFAULT_TENANT``).
        session_id: Optional session identifier override.  When ``None``,
                    resolved from ``NX_T1_SESSION`` env var.
    """

    def __init__(
        self,
        base_url: str | None = None,
        tenant: str = DEFAULT_TENANT,
        *,
        session_id: str | None = None,
        _token: str | None = None,
    ) -> None:
        if base_url is not None:
            if _token is None:
                _token = os.environ.get("NX_SERVICE_TOKEN", "")
                if not _token:
                    raise RuntimeError(
                        "NX_SERVICE_TOKEN is required when NX_STORAGE_BACKEND_T1=service."
                    )
            self._base_url = base_url.rstrip("/")
        else:
            host, port, token = _resolve_config()
            self._base_url = f"http://{host}:{port}"
            _token = token

        self._tenant = tenant
        self._session_id: str = session_id if session_id else _resolve_session()

        self._headers = {
            "Authorization": f"Bearer {_token}",
            "X-Nexus-Tenant": tenant,
            _HEADER_T1_SESSION: self._session_id,
            "Content-Type": "application/json",
        }
        self._client = httpx.Client(
            base_url=self._base_url,
            headers=self._headers,
            timeout=30.0,
        )
        _log.info(
            "http_scratch_store.init",
            base_url=self._base_url,
            tenant=tenant,
            session_id=self._session_id,
        )

    # ── Session ────────────────────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        """The session identifier used to scope all scratch entries."""
        return self._session_id

    def close(self) -> None:
        """Close the keep-alive connection pool (idempotent)."""
        self._client.close()
        _log.debug("http_scratch_store.closed")

    def close_session(self) -> int:
        """Delete all scratch entries for this session. Returns count deleted.

        Called from MCP lifespan on exit for promptness.  The service also
        runs a periodic TTL sweep (default 24 h) as a crash-safety backstop.
        Idempotent: double-close returns 0, not an error.
        """
        resp = self._post("/v1/t1/session/close", {"session_id": self._session_id})
        return int(resp.get("deleted", 0))

    # ── Write ──────────────────────────────────────────────────────────────────

    def put(
        self,
        content: str,
        tags: str = "",
        persist: bool = False,
        flush_project: str = "",
        flush_title: str = "",
        agent: str = "",
    ) -> str:
        """Store *content* in T1 scratch. Returns the entry UUID.

        Interface matches :meth:`T1Database.put` exactly.

        If *persist* is ``True`` the entry is pre-flagged for SessionEnd
        flush.  Auto-destination when no explicit project/title:
        ``scratch_sessions`` / ``{session_id}_{id}``.
        """
        import uuid as _uuid_mod
        doc_id = str(_uuid_mod.uuid4())
        if persist:
            flush_project = flush_project or "scratch_sessions"
            flush_title = flush_title or f"{self._session_id}_{doc_id}"
        if not agent:
            agent = os.environ.get("NX_AGENT", "")

        payload: dict[str, Any] = {
            "id": doc_id,
            "session_id": self._session_id,
            "content": content,
            "tags": tags,
            "agent": agent or None,
            "flagged": persist,
            "flush_project": flush_project or None,
            "flush_title": flush_title or None,
        }
        resp = self._post("/v1/t1/put", payload)
        return str(resp["id"])

    # ── Read ───────────────────────────────────────────────────────────────────

    def get(self, id: str) -> dict | None:
        """Return the entry dict for *id*, or None if not found / wrong session.

        *id* may be the full UUID or a unique session-owned prefix (uses
        resolve_prefix_candidates internally when a full-UUID miss occurs).

        BEHAVIOR CHANGE: semantics shift from ChromaDB cosine to column-filter.
        The entry is scoped to ``(tenant, session_id)`` via Postgres RLS +
        WHERE; cross-session access returns None.
        """
        # Try exact id first
        resp_data = self._post_raw("/v1/t1/get", {"id": id, "session_id": self._session_id})
        if resp_data.get("found") is False:
            # Prefix fallback: find full id
            candidates = self.resolve_prefix_candidates(id)
            if len(candidates) == 1:
                resp_data = self._post_raw(
                    "/v1/t1/get", {"id": candidates[0], "session_id": self._session_id}
                )
            elif candidates:
                _log.warning(
                    "t1_http_get_ambiguous_prefix",
                    requested_id=id,
                    candidates=candidates,
                    session_id=self._session_id,
                )
                return None
            else:
                return None
        if resp_data.get("found") is False:
            return None
        return resp_data if resp_data else None

    def search(self, query: str, n_results: int = 10) -> list[dict]:
        """FTS search over content + tags, scoped to this session.

        BEHAVIOR CHANGE: was vector/cosine (ChromaDB ONNX); now FTS
        (Postgres tsvector, OR: English stemmer + simple identifier config).
        Results are ordered by ts_rank descending (best first).
        """
        resp = self._post(
            "/v1/t1/search",
            {"query": query, "session_id": self._session_id, "limit": n_results},
        )
        return resp.get("results", [])

    def list_entries(self) -> list[dict]:
        """Return all entries for this session (ordered ts desc)."""
        resp = self._post("/v1/t1/list", {"session_id": self._session_id})
        return resp.get("entries", [])

    def flagged_entries(self) -> list[dict]:
        """Return all flagged entries for this session."""
        resp = self._post("/v1/t1/flagged", {"session_id": self._session_id})
        return resp.get("entries", [])

    # ── Flag / unflag ──────────────────────────────────────────────────────────

    def flag(self, id: str, project: str = "", title: str = "") -> None:
        """Mark *id* for SessionEnd flush to T2.

        Auto-destination when *project*/*title* omitted:
        ``scratch_sessions`` / ``{session_id}_{id}``.

        Raises KeyError when the entry is not found.
        """
        flush_project = project or "scratch_sessions"
        flush_title = title or f"{self._session_id}_{id}"
        resp = self._post(
            "/v1/t1/flag",
            {
                "id": id,
                "session_id": self._session_id,
                "flush_project": flush_project,
                "flush_title": flush_title,
            },
        )
        if not resp.get("ok"):
            raise KeyError(f"No scratch entry: {id!r}")

    def unflag(self, id: str) -> None:
        """Remove the flush-on-SessionEnd marking from *id*.

        Raises KeyError when the entry is not found.
        """
        resp = self._post(
            "/v1/t1/unflag",
            {"id": id, "session_id": self._session_id},
        )
        if not resp.get("ok"):
            raise KeyError(f"No scratch entry: {id!r}")

    # ── Promote ────────────────────────────────────────────────────────────────

    def promote(self, id: str, project: str, title: str, t2: object) -> object:  # noqa: ARG002
        """NOT IMPLEMENTED for the Postgres T1 backend.

        Promote copies a T1 entry into T2 memory and requires a T2Database
        or T2Client reference with Jaccard overlap detection.  The logic is
        kept on the ChromaDB T1Database for now and will be ported to the
        service path once the T2 memory migration (nexus-gmiaf.7) is stable
        enough to pass the reference through the service layer.
        """
        raise NotImplementedError(
            "HttpScratchStore.promote() is not implemented. "
            "Use T1Database (Chroma path) for promote() calls, or wait for "
            "the service-path port in a future bead."
        )

    # ── Delete / clear ─────────────────────────────────────────────────────────

    def delete(self, id: str) -> bool:
        """Delete a scratch entry by full UUID or unique session-owned prefix.

        Returns True when deleted, False when not found or not in this session.
        """
        # Prefix resolution for ergonomics (mirrors T1Database.delete)
        resolved = id
        if "-" not in id:
            # Looks like a short prefix; attempt resolution
            candidates = self.resolve_prefix_candidates(id)
            if len(candidates) == 1:
                resolved = candidates[0]
            elif candidates:
                _log.warning(
                    "t1_http_delete_ambiguous_prefix",
                    requested_id=id,
                    candidates=candidates,
                    session_id=self._session_id,
                )
                return False
            else:
                return False

        resp = self._post(
            "/v1/t1/delete",
            {"id": resolved, "session_id": self._session_id},
        )
        return bool(resp.get("deleted", False))

    def clear(self) -> int:
        """Remove all session entries. Returns the count deleted.

        Implemented via session-close + count.  Note: this ALSO invalidates
        the current session on the service side (all entries gone).  Callers
        that need to continue using the scratch store after clear() should
        create a fresh ``HttpScratchStore`` with a new session_id.
        """
        return self.close_session()

    def resolve_prefix_candidates(self, id: str) -> list[str]:
        """Return session-owned ids matching *id* as exact or prefix.

        Empty list when nothing matches; one-element list when a unique
        resolution exists; multi-element when ambiguous.
        """
        resp = self._post(
            "/v1/t1/resolve_prefix",
            {"prefix": id, "session_id": self._session_id},
        )
        return resp.get("ids", [])

    # ── HTTP helpers ───────────────────────────────────────────────────────────

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST *payload* to *path* and return the parsed JSON body.

        Raises RuntimeError on non-2xx responses.
        """
        try:
            resp = self._client.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"HttpScratchStore: network error on {path}: {exc}") from exc
        if not resp.is_success:
            raise RuntimeError(
                f"HttpScratchStore: {path} returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()

    def _post_raw(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST *payload* to *path* and return parsed JSON without raising on 404-class."""
        try:
            resp = self._client.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"HttpScratchStore: network error on {path}: {exc}") from exc
        if resp.status_code == 404:
            return {"found": False}
        if not resp.is_success:
            raise RuntimeError(
                f"HttpScratchStore: {path} returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()
