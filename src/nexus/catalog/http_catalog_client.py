# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""HttpCatalogClient — thin HTTP client over the RDR-152 Java catalog service.

Drop-in replacement for :class:`~nexus.catalog.catalog.Catalog` at the
orchestrator level (NOT at the CatalogStore level).  Activated by setting
``NX_STORAGE_BACKEND_CATALOG=service``.

Config:
    NX_SERVICE_HOST  — service host (default: 127.0.0.1)
    NX_SERVICE_PORT  — required; raises if missing
    NX_SERVICE_TOKEN — bearer token; required; raises if missing

Route alignment with CatalogHandler (bead nexus-gmiaf.18).  Every route
below maps to an exact ``case`` in the Java handler's switch:

  POST  /v1/catalog/doc/register         server-side tumbler assignment
  GET   /v1/catalog/show?tumbler=X       get document
  GET   /v1/catalog/list?...             paginated list / filtered list
  GET   /v1/catalog/search?q=X          FTS search
  POST  /v1/catalog/update              update document fields
  POST  /v1/catalog/delete              delete by {tumbler}  (also DELETE)
  GET   /v1/catalog/resolve?...         resolve by file_path/source_uri/title
  GET   /v1/catalog/stats               per-tenant statistics
  POST  /v1/catalog/link               upsert link
  POST  /v1/catalog/unlink             delete link(s)
  GET   /v1/catalog/links?tumbler=X&direction=out|in|both  neighbors
  GET   /v1/catalog/link_query?...      paginated link query
  POST  /v1/catalog/traverse            BFS graph traversal {seeds, depth, ...}
  POST  /v1/catalog/manifest/write      replace manifest {doc_id, rows}
  POST  /v1/catalog/manifest/append     append chunks {doc_id, rows}
  GET   /v1/catalog/manifest/get?doc_id=X
  POST  /v1/catalog/manifest/purge      {doc_id}
  GET   /v1/catalog/manifest/chashes?collection=X
  POST  /v1/catalog/manifest/docs_for_chashes  {chashes}
  POST  /v1/catalog/owners/upsert       upsert owner
  GET   /v1/catalog/owners/list
  GET   /v1/catalog/owners/by_repo?repo_hash=X
  GET   /v1/catalog/owners/by_name?name=X
  POST  /v1/catalog/owners/head_hash    {tumbler_prefix, head_hash}
  POST  /v1/catalog/collections/upsert
  GET   /v1/catalog/collections/list
  GET   /v1/catalog/collections/get?name=X
  POST  /v1/catalog/collections/supersede
  POST  /v1/catalog/collections/rename  {old_name, new_name}
  GET   /v1/catalog/collections/for_tuple?content_type=X&owner_id=X&embedding_model=X
  POST  /v1/catalog/import/owner|document|link|chunk|collection  ETL

Per catalog-git-DECISION OPTION C (2026-06-07): Postgres is the SOLE authority
on the catalog write path.  Methods like rebuild(), defrag(), compact(), sync(),
pull() that are SQLite/git-only artifacts raise NotImplementedError (guard+track;
bead nexus-gmiaf.24 tracks the service-side equivalents).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
import structlog

from nexus.catalog.catalog import CatalogEntry, Tumbler

_log = structlog.get_logger(__name__)

DEFAULT_TENANT: str = "default"


def _resolve_config() -> tuple[str, int, str]:
    """Return (host, port, token) from environment."""
    host = os.environ.get("NX_SERVICE_HOST", "127.0.0.1")
    port_str = os.environ.get("NX_SERVICE_PORT", "")
    token = os.environ.get("NX_SERVICE_TOKEN", "")
    if not port_str:
        raise RuntimeError(
            "NX_SERVICE_PORT is required when NX_STORAGE_BACKEND_CATALOG=service. "
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
            "NX_SERVICE_TOKEN is required when NX_STORAGE_BACKEND_CATALOG=service."
        )
    return host, port, token


def _to_entry(d: dict) -> CatalogEntry:
    """Convert a server response dict to a CatalogEntry.

    All CatalogEntry fields are non-optional; coerce None / missing values
    to the same empty-string / 0 / {} defaults the SQLite Catalog uses.
    CatalogEntry has no bib fields — those live only in T3 metadata.
    """
    return CatalogEntry(
        tumbler=Tumbler.parse(d["tumbler"]),
        title=d.get("title") or "",
        author=d.get("author") or "",
        year=d.get("year") or 0,
        content_type=d.get("content_type") or "",
        file_path=d.get("file_path") or "",
        corpus=d.get("corpus") or "",
        physical_collection=d.get("physical_collection") or "",
        chunk_count=d.get("chunk_count") or 0,
        head_hash=d.get("head_hash") or "",
        indexed_at=d.get("indexed_at") or "",
        meta=d.get("meta") or d.get("metadata") or {},
        source_mtime=d.get("source_mtime") or 0.0,
        alias_of=d.get("alias_of") or "",
        source_uri=d.get("source_uri") or "",
    )


class HttpCatalogClient:
    """Catalog orchestrator drop-in backed by the RDR-152 Java HTTP service.

    Implements the full public API of :class:`~nexus.catalog.catalog.Catalog`
    at the ORCHESTRATOR level.  All calls forward to the Java service at
    ``/v1/catalog/*``.

    Args:
        base_url: Optional override (e.g. ``"http://127.0.0.1:8765"``).
        tenant:   Tenant header stamped on every request.
        _token:   Token override (used with base_url; read from env otherwise).
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
                        "NX_SERVICE_TOKEN is required when "
                        "NX_STORAGE_BACKEND_CATALOG=service."
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
        self._client = httpx.Client(
            base_url=self._base_url,
            headers=self._headers,
            timeout=30.0,
        )
        _log.info("http_catalog_client.init", base_url=self._base_url, tenant=tenant)

    def close(self) -> None:
        """Close the keep-alive connection pool (idempotent)."""
        self._client.close()
        _log.debug("http_catalog_client.closed")

    def __enter__(self) -> "HttpCatalogClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _get(self, path: str, **params: Any) -> Any:
        filtered = {k: v for k, v in params.items() if v is not None and v != ""}
        r = self._client.get(f"/v1/catalog{path}", params=filtered)
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        if "application/json" in ct and r.content:
            return r.json()
        return None

    def _post(self, path: str, body: dict | None = None) -> Any:
        r = self._client.post(f"/v1/catalog{path}", json=body or {})
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        if "application/json" in ct and r.content:
            return r.json()
        return None

    def _docs_from(self, result: Any) -> list[CatalogEntry]:
        if not result:
            return []
        return [_to_entry(d) for d in result.get("documents", []) if d.get("tumbler")]

    # ══════════════════════════════════════════════════════════════════════════
    # OWNERS
    # ══════════════════════════════════════════════════════════════════════════

    def register_owner(
        self,
        name: str,
        owner_type: str = "repo",
        *,
        repo_hash: str | None = None,
        description: str = "",
        repo_root: Path | str | None = None,
        head_hash: str | None = None,
        tumbler_prefix: str | None = None,
    ) -> Tumbler:
        """Upsert an owner and return a Tumbler for its prefix.

        If ``tumbler_prefix`` is given it is used directly (ETL / migration path).
        Otherwise the server assigns a prefix via its next_seq logic; we query
        it back via /owners/by_name after the upsert.
        """
        payload: dict = {
            "name": name,
            "owner_type": owner_type,
        }
        if tumbler_prefix: payload["tumbler_prefix"] = tumbler_prefix
        if repo_hash:      payload["repo_hash"] = repo_hash
        if description:    payload["description"] = description
        if repo_root:      payload["repo_root"] = str(repo_root)
        if head_hash:      payload["head_hash"] = head_hash
        result = self._post("/owners/upsert", payload)
        # If server echoes tumbler_prefix in response (future enhancement), use it
        if isinstance(result, dict) and result.get("tumbler_prefix"):
            return Tumbler.parse(result["tumbler_prefix"])
        # Otherwise fall back to explicit prefix or query by name
        if tumbler_prefix:
            return Tumbler.parse(tumbler_prefix)
        # Query back the server-assigned prefix
        owners = self._get("/owners/by_name", name=name)
        rows = owners.get("owners", []) if owners else []
        if rows:
            return Tumbler.parse(rows[0]["tumbler_prefix"])
        raise RuntimeError(
            f"register_owner: server did not return or store prefix for owner {name!r}"
        )

    def ensure_owner_for_repo(
        self,
        *,
        repo: Path | str,
        name: str | None = None,
        owner_type: str = "repo",
        head_hash: str | None = None,
        tumbler_prefix: str | None = None,
    ) -> Tumbler:
        """Ensure an owner row exists for ``repo`` and return its Tumbler.

        If ``tumbler_prefix`` is provided (e.g. during ETL migration) it is used
        directly.  Otherwise the server assigns a prefix; we query it back.
        """
        effective_name = name or Path(repo).name
        payload: dict = {
            "name": effective_name,
            "owner_type": owner_type,
            "repo_root": str(repo),
        }
        if tumbler_prefix: payload["tumbler_prefix"] = tumbler_prefix
        if head_hash:      payload["head_hash"] = head_hash
        result = self._post("/owners/upsert", payload)
        if isinstance(result, dict) and result.get("tumbler_prefix"):
            return Tumbler.parse(result["tumbler_prefix"])
        if tumbler_prefix:
            return Tumbler.parse(tumbler_prefix)
        # Query back
        owners = self._get("/owners/by_name", name=effective_name)
        rows = owners.get("owners", []) if owners else []
        if rows:
            return Tumbler.parse(rows[0]["tumbler_prefix"])
        raise RuntimeError(
            f"ensure_owner_for_repo: server did not return prefix for {repo!r}"
        )

    def set_owner_head_hash(self, owner: Tumbler | str, head_hash: str) -> None:
        self._post("/owners/head_hash", {
            "tumbler_prefix": str(owner),
            "head_hash": head_hash,
        })

    def owner_for_repo(self, repo_hash: str) -> Tumbler | None:
        try:
            result = self._get("/owners/by_repo", repo_hash=repo_hash)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        if result and result.get("tumbler_prefix"):
            return Tumbler.parse(result["tumbler_prefix"])
        return None

    def owner_tumblers_by_name(self, name: str) -> list[Tumbler]:
        result = self._get("/owners/by_name", name=name)
        owners = result.get("owners", []) if result else []
        return [
            Tumbler.parse(o["tumbler_prefix"])
            for o in owners
            if o.get("tumbler_prefix")
        ]

    # ══════════════════════════════════════════════════════════════════════════
    # DOCUMENTS
    # ══════════════════════════════════════════════════════════════════════════

    def register(
        self,
        owner: Tumbler | str,
        title: str,
        *,
        content_type: str = "",
        file_path: str = "",
        corpus: str = "",
        physical_collection: str = "",
        chunk_count: int = 0,
        head_hash: str = "",
        author: str = "",
        year: int = 0,
        meta: dict | None = None,
        source_mtime: float = 0.0,
        source_uri: str = "",
        **kwargs: Any,
    ) -> Tumbler:
        """Register a document; returns the server-assigned tumbler.

        Signature matches :meth:`nexus.catalog.catalog.Catalog.register`
        exactly — positional ``owner`` + ``title``, then keyword-only.
        No bib fields: CatalogEntry / Catalog.register() have none.

        Uses POST /v1/catalog/doc/register for server-side atomic tumbler
        assignment via catalog_owners.next_seq (SELECT FOR UPDATE).
        """
        payload: dict = {
            "owner_prefix": str(owner),
            "title": title,
            "content_type": content_type,
            "file_path": file_path,
            "corpus": corpus,
            "physical_collection": physical_collection,
            "chunk_count": chunk_count,
            "author": author,
            "year": year,
            "source_mtime": source_mtime,
            "source_uri": source_uri,
        }
        if head_hash: payload["head_hash"] = head_hash
        if meta:      payload["meta"] = meta
        payload.update(kwargs)
        result = self._post("/doc/register", payload)
        return Tumbler.parse(result["tumbler"])

    def resolve(
        self, tumbler: Tumbler | str, *, follow_alias: bool = True
    ) -> CatalogEntry | None:
        try:
            result = self._get("/show", tumbler=str(tumbler))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        if not result or not result.get("tumbler"):
            return None
        return _to_entry(result)

    def update(self, tumbler: Tumbler | str, **fields: Any) -> None:
        self._post("/update", {"tumbler": str(tumbler), **fields})

    def delete_document(self, tumbler: Tumbler | str) -> bool:
        result = self._post("/delete", {"tumbler": str(tumbler)})
        return bool(result.get("deleted", 0) > 0 if result else False)

    def find(
        self, query: str, *, content_type: str | None = None
    ) -> list[CatalogEntry]:
        params: dict = {"q": query}
        if content_type:
            params["content_type"] = content_type
        return self._docs_from(self._get("/search", **params))

    def by_file_path(
        self, owner: Tumbler | str, file_path: str
    ) -> CatalogEntry | None:
        result = self._get("/list", owner=str(owner), file_path=file_path)
        docs = result.get("documents", []) if result else []
        return _to_entry(docs[0]) if docs else None

    def by_source_uri(self, uri: str) -> CatalogEntry | None:
        result = self._get("/list", source_uri=uri)
        docs = result.get("documents", []) if result else []
        return _to_entry(docs[0]) if docs else None

    def by_owner(self, owner: Tumbler | str) -> list[CatalogEntry]:
        return self._docs_from(self._get("/list", owner=str(owner)))

    def by_content_type(self, content_type: str) -> list[CatalogEntry]:
        return self._docs_from(self._get("/list", content_type=content_type))

    def by_corpus(self, corpus: str) -> list[CatalogEntry]:
        return self._docs_from(self._get("/list", corpus=corpus))

    def all_documents(
        self, *, limit: int = 200, offset: int = 0
    ) -> list[CatalogEntry]:
        return self._docs_from(
            self._get("/list", limit=limit, offset=offset)
        )

    def list_by_collection(self, physical_collection: str) -> list[CatalogEntry]:
        return self._docs_from(
            self._get("/list", collection=physical_collection)
        )

    def by_doc_id(self, doc_id: str) -> CatalogEntry | None:
        return self.resolve(doc_id)

    def lookup_doc_id_by_collection_and_path(
        self, collection: str, file_path: str
    ) -> str | None:
        try:
            result = self._get(
                "/resolve",
                file_path=file_path,
                collection=collection,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        docs = result.get("documents", []) if result else []
        return docs[0]["tumbler"] if docs else None

    def doc_count(self) -> int:
        return int(self.stats().get("doc_count", 0))

    def descendants(self, prefix: str) -> list[dict]:
        # Server has no dedicated descendants route; pull all and filter
        result = self._get("/list", limit=500)
        docs = result.get("documents", []) if result else []
        return [d for d in docs if d.get("tumbler", "").startswith(prefix + ".")]

    def set_alias(self, tumbler: Tumbler | str, canonical: Tumbler | str) -> None:
        self._post("/update", {"tumbler": str(tumbler), "alias_of": str(canonical)})

    def resolve_alias(self, tumbler: Tumbler | str, *, max_hops: int = 16) -> Tumbler:
        entry = self.resolve(tumbler, follow_alias=True)
        if entry:
            return entry.tumbler
        return Tumbler.parse(str(tumbler))

    def resolve_path(self, tumbler: Tumbler | str) -> Path | None:
        entry = self.resolve(tumbler)
        return Path(entry.file_path) if entry and entry.file_path else None

    def resolve_span(self, tumbler: Tumbler | str, span: str) -> dict | None:
        return None  # not supported in initial service-mode implementation

    def resolve_chash(self, chash: str, *, collection: str | None = None) -> dict | None:
        return None  # not supported in initial service-mode implementation

    def resolve_chunk(self, tumbler: Tumbler | str) -> dict | None:
        result = self.resolve(tumbler)
        return result.__dict__ if result else None

    def resolve_span_text(self, tumbler: Tumbler | str, span: str) -> str | None:
        return None  # not supported in initial service-mode implementation

    # ══════════════════════════════════════════════════════════════════════════
    # LINKS
    # ══════════════════════════════════════════════════════════════════════════

    def link(
        self,
        from_t: Tumbler | str,
        to_t: Tumbler | str,
        link_type: str,
        *,
        from_span: str = "",
        to_span: str = "",
        created_by: str = "user",
        metadata: dict | None = None,
    ) -> dict:
        payload: dict = {
            "from_tumbler": str(from_t),
            "to_tumbler": str(to_t),
            "link_type": link_type,
            "from_span": from_span,
            "to_span": to_span,
            "created_by": created_by,
        }
        if metadata:
            payload["metadata"] = metadata
        return self._post("/link", payload) or {}

    def link_if_absent(
        self,
        from_t: Tumbler | str,
        to_t: Tumbler | str,
        link_type: str,
        **kwargs: Any,
    ) -> dict:
        return self.link(from_t, to_t, link_type, **kwargs)

    def unlink(
        self,
        from_t: Tumbler | str,
        to_t: Tumbler | str,
        link_type: str,
    ) -> bool:
        result = self._post("/unlink", {
            "from_tumbler": str(from_t),
            "to_tumbler": str(to_t),
            "link_type": link_type,
        })
        return bool(result.get("deleted", 0) > 0 if result else False)

    def links_from(
        self,
        tumbler: Tumbler | str,
        *,
        link_type: str | None = None,
    ) -> list[dict]:
        params: dict = {"tumbler": str(tumbler), "direction": "out"}
        if link_type:
            params["link_type"] = link_type
        result = self._get("/links", **params)
        return result.get("links_from", []) if result else []

    def links_to(
        self,
        tumbler: Tumbler | str,
        *,
        link_type: str | None = None,
    ) -> list[dict]:
        params: dict = {"tumbler": str(tumbler), "direction": "in"}
        if link_type:
            params["link_type"] = link_type
        result = self._get("/links", **params)
        return result.get("links_to", []) if result else []

    def link_query(
        self,
        *,
        from_t: str | None = None,
        to_t: str | None = None,
        link_type: str | None = None,
        created_by: str | None = None,
        created_at_before: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict]:
        params: dict = {"limit": limit, "offset": offset}
        if from_t:            params["from_tumbler"] = from_t
        if to_t:              params["to_tumbler"] = to_t
        if link_type:         params["link_type"] = link_type
        if created_by:        params["created_by"] = created_by
        if created_at_before: params["created_at_before"] = created_at_before
        result = self._get("/link_query", **params)
        return result.get("links", []) if result else []

    def bulk_unlink(
        self,
        *,
        from_t: str = "",
        to_t: str = "",
        link_type: str = "",
        created_by: str = "",
    ) -> int:
        """Bulk delete links — all fields are optional filters."""
        payload: dict = {}
        if from_t:    payload["from_tumbler"] = from_t
        if to_t:      payload["to_tumbler"] = to_t
        if link_type: payload["link_type"] = link_type
        if created_by: payload["created_by"] = created_by
        result = self._post("/unlink", payload)
        return int(result.get("deleted", 0) if result else 0)

    def validate_link(
        self,
        from_t: Tumbler | str,
        to_t: Tumbler | str,
        link_type: str,
    ) -> bool:
        return (
            self.resolve(from_t) is not None and
            self.resolve(to_t) is not None
        )

    def graph(
        self,
        tumbler: Tumbler | str,
        *,
        link_types: list[str] | None = None,
        direction: str = "both",
        depth: int = 1,
    ) -> dict:
        """BFS traversal from a single seed — POST /v1/catalog/traverse."""
        payload: dict = {
            "seeds": [str(tumbler)],
            "direction": direction,
            "depth": depth,
        }
        if link_types:
            payload["link_types"] = link_types
        return self._post("/traverse", payload) or {"nodes": [], "edges": []}

    def graph_many(
        self,
        tumblers: list[Tumbler | str],
        *,
        link_types: list[str] | None = None,
        direction: str = "both",
        depth: int = 1,
    ) -> dict:
        """BFS traversal from multiple seeds — POST /v1/catalog/traverse."""
        payload: dict = {
            "seeds": [str(t) for t in tumblers],
            "direction": direction,
            "depth": depth,
        }
        if link_types:
            payload["link_types"] = link_types
        return self._post("/traverse", payload) or {"nodes": [], "edges": []}

    def link_audit(self, *, t3: Any = None) -> dict:
        return {}  # not supported in initial service-mode implementation

    # ══════════════════════════════════════════════════════════════════════════
    # COLLECTIONS
    # ══════════════════════════════════════════════════════════════════════════

    def register_collection(
        self,
        name: str,
        *,
        content_type: str = "",
        owner_id: str = "",
        embedding_model: str = "",
        model_version: str = "v1",
        display_name: str = "",
        legacy_grandfathered: bool = False,
    ) -> None:
        self._post("/collections/upsert", {
            "name": name,
            "content_type": content_type,
            "owner_id": owner_id,
            "embedding_model": embedding_model,
            "model_version": model_version,
            "display_name": display_name,
            "legacy_grandfathered": legacy_grandfathered,
        })

    def delete_collection_projection(self, name: str, *, reason: str = "") -> bool:
        # No hard-delete route in initial service; guard+track for bead nexus-gmiaf.24
        _log.warning(
            "http_catalog_client.delete_collection_not_supported",
            name=name,
            hint="hard delete not implemented; supersede_collection() can mark inactive",
        )
        return False

    def list_collections(self) -> list[dict]:
        result = self._get("/collections/list")
        return result.get("collections", []) if result else []

    def get_collection(self, name: str) -> dict | None:
        try:
            result = self._get("/collections/get", name=name)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        return result if result and result.get("name") else None

    def is_legacy_collection(self, name: str) -> bool:
        coll = self.get_collection(name)
        return bool(coll.get("legacy_grandfathered", False)) if coll else False

    def collection_for(
        self,
        *,
        content_type: str,
        owner_id: str,
        embedding_model: str,
    ) -> dict | None:
        try:
            result = self._get(
                "/collections/for_tuple",
                content_type=content_type,
                owner_id=owner_id,
                embedding_model=embedding_model,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        return result if result and result.get("name") else None

    def collection_for_repo(
        self,
        *,
        owner: Tumbler | str,
        content_type: str,
        embedding_model: str,
    ) -> dict | None:
        return self.collection_for(
            content_type=content_type,
            owner_id=str(owner),
            embedding_model=embedding_model,
        )

    def supersede_collection(
        self,
        name: str,
        *,
        superseded_by: str,
        superseded_at: str | None = None,
    ) -> int:
        payload: dict = {"name": name, "superseded_by": superseded_by}
        if superseded_at:
            payload["superseded_at"] = superseded_at
        result = self._post("/collections/supersede", payload)
        return int(result.get("updated", 0) if result else 0)

    def rename_collection(self, old: str, new: str) -> int:
        result = self._post("/collections/rename", {"old_name": old, "new_name": new})
        return int(result.get("updated", 0) if result else 0)

    def update_document_collection(
        self, tumbler: Tumbler | str, collection: str
    ) -> None:
        self._post("/update", {
            "tumbler": str(tumbler), "physical_collection": collection
        })

    def update_documents_collection_batch(
        self,
        tumblers: list[Tumbler | str],
        collection: str,
    ) -> int:
        # No server-side batch endpoint yet (guard+track bead nexus-gmiaf.24);
        # iterate single updates.
        for t in tumblers:
            self.update_document_collection(t, collection)
        return len(tumblers)

    # ══════════════════════════════════════════════════════════════════════════
    # MANIFEST / CHUNKS
    # ══════════════════════════════════════════════════════════════════════════

    def write_manifest(self, doc_id: str, chunks: list[dict]) -> None:
        """Replace manifest for doc_id (atomic delete + insert)."""
        self._post("/manifest/write", {"doc_id": doc_id, "rows": chunks})

    def append_manifest_chunks(self, doc_id: str, chunks: list[dict]) -> None:
        self._post("/manifest/append", {"doc_id": doc_id, "rows": chunks})

    def get_manifest(self, doc_id: str) -> list[Any]:
        result = self._get("/manifest/get", doc_id=doc_id)
        return result.get("rows", []) if result else []

    def get_chunk_chashes(self, doc_id: str) -> list[str]:
        """Return chashes for all chunks of doc_id.

        The server's /manifest/chashes endpoint queries by collection, not doc_id.
        We resolve the document's physical_collection first, then return its
        chashes.  This is a best-effort approximation; for a strict per-doc list
        use get_manifest() + extract chash from each row.
        """
        rows = self.get_manifest(doc_id)
        return [row["chash"] for row in rows if row.get("chash")]

    def docs_for_chashes(self, chashes: list[str]) -> list[str]:
        """Return the list of document tumblers that contain any of the given chashes.

        CatalogRepository.docsForChashes() runs a SELECT DISTINCT on doc_id across
        all provided chashes.  The handler wraps it as {"tumblers": [tumbler, ...]}.
        This is a flat list, NOT a per-chash mapping.
        """
        result = self._post("/manifest/docs_for_chashes", {"chashes": chashes})
        # Handler returns {"tumblers": [tumbler_string, ...]}
        return result.get("tumblers", []) if result else []

    def chashes_for_collection(self, physical_collection: str) -> set[str]:
        result = self._get("/manifest/chashes", collection=physical_collection)
        return set(result.get("chashes", [])) if result else set()

    def purge_manifest_for_doc(self, doc_id: str) -> None:
        self._post("/manifest/purge", {"doc_id": doc_id})

    def atomic_manifest_replace(
        self,
        doc_id: str,
        chunks: list[dict],
        *,
        new_collection: str | None = None,
        new_chunk_count: int | None = None,
    ) -> None:
        # /manifest/write performs the atomic delete+insert already
        self._post("/manifest/write", {"doc_id": doc_id, "rows": chunks})
        if new_collection or new_chunk_count is not None:
            updates: dict = {}
            if new_collection:              updates["physical_collection"] = new_collection
            if new_chunk_count is not None: updates["chunk_count"] = new_chunk_count
            self._post("/update", {"tumbler": doc_id, **updates})

    def resync_chunk_count_cache(self, doc_id: str) -> None:
        """No-op in service mode: Postgres tracks chunk_count automatically."""
        pass

    # ══════════════════════════════════════════════════════════════════════════
    # STATS / HEALTH
    # ══════════════════════════════════════════════════════════════════════════

    def stats(self) -> dict:
        return self._get("/stats") or {}

    def is_initialized(self) -> bool:
        """True when the service responds to /stats."""
        try:
            self._get("/stats")
            return True
        except Exception:
            return False

    # ══════════════════════════════════════════════════════════════════════════
    # GUARDED — SQLite/git-only operations (catalog-git-DECISION OPTION C)
    # ══════════════════════════════════════════════════════════════════════════

    def rebuild(self) -> None:
        raise NotImplementedError(
            "rebuild() is a SQLite-projection rebuild and has no Postgres equivalent. "
            "bead nexus-gmiaf.24 tracks the service-side equivalent."
        )

    def defrag(self) -> dict:
        raise NotImplementedError(
            "defrag() is a JSONL compaction operation; dropped under catalog-git-DECISION "
            "OPTION C.  Postgres is the sole authority in service mode."
        )

    def compact(self) -> dict:
        raise NotImplementedError(
            "compact() is a JSONL compaction operation; dropped under catalog-git-DECISION "
            "OPTION C.  Postgres is the sole authority in service mode."
        )

    def sync(self, message: str = "catalog update") -> None:
        raise NotImplementedError(
            "sync() is a git commit operation; dropped under catalog-git-DECISION OPTION C."
        )

    def pull(self) -> None:
        raise NotImplementedError(
            "pull() is a git pull operation; dropped under catalog-git-DECISION OPTION C."
        )

    def rebuild_if_stale(self) -> None:
        pass  # no-op in service mode: Postgres is always consistent

    def _ensure_consistent(self) -> None:
        pass  # no-op in service mode

    # ══════════════════════════════════════════════════════════════════════════
    # COMPAT SHIMS
    # ══════════════════════════════════════════════════════════════════════════

    @property
    def catalog_path(self) -> Path | None:
        return None

    def jsonl_paths(self) -> tuple:
        return ()

    def mtime_paths(self) -> tuple:
        return ()
