# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""HttpCatalogClient — thin HTTP client over the RDR-152 Java catalog service.

Drop-in replacement for :class:`~nexus.catalog.catalog.Catalog` at the
orchestrator level (NOT at the CatalogStore level).  Activated by setting
``NX_STORAGE_BACKEND_CATALOG=service``.

Config:
    NX_SERVICE_HOST  — service host (default: 127.0.0.1)
    NX_SERVICE_PORT  — service port (required; raises if missing)
    NX_SERVICE_TOKEN — bearer token (required; raises if missing)

Per catalog-git-DECISION (OPTION C, 2026-06-07): Postgres is the SOLE
authority on the catalog write path.  HttpCatalogClient does NOT write
any JSONL or commit git.  Methods like ``rebuild()``, ``defrag()``,
``compact()``, ``sync()``, ``pull()`` that are SQLite/git-only artifacts
raise ``NotImplementedError`` (guard+track; bead nexus-gmiaf.24 covers
the service-side equivalents).
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
        raise RuntimeError(f"NX_SERVICE_PORT must be an integer, got: {port_str!r}") from exc
    if not token:
        raise RuntimeError(
            "NX_SERVICE_TOKEN is required when NX_STORAGE_BACKEND_CATALOG=service."
        )
    return host, port, token


def _to_entry(d: dict) -> CatalogEntry:
    """Convert a server response dict to a CatalogEntry."""
    return CatalogEntry(
        tumbler=Tumbler.parse(d["tumbler"]),
        title=d.get("title", ""),
        author=d.get("author", ""),
        year=d.get("year") or 0,
        content_type=d.get("content_type", ""),
        file_path=d.get("file_path", ""),
        corpus=d.get("corpus", ""),
        physical_collection=d.get("physical_collection", ""),
        chunk_count=d.get("chunk_count", 0) or 0,
        head_hash=d.get("head_hash", ""),
        indexed_at=d.get("indexed_at", ""),
        meta=d.get("meta") or d.get("metadata") or {},
        source_mtime=d.get("source_mtime") or 0.0,
        alias_of=d.get("alias_of", ""),
        source_uri=d.get("source_uri", ""),
    )


class HttpCatalogClient:
    """Catalog orchestrator drop-in backed by the RDR-152 Java HTTP service.

    Implements the full public API of :class:`~nexus.catalog.catalog.Catalog`
    at the ORCHESTRATOR level.  All calls forward to the Java service at
    ``/v1/catalog/*``.

    Args:
        base_url: Optional override for the service base URL.
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
                        "NX_SERVICE_TOKEN is required when NX_STORAGE_BACKEND_CATALOG=service."
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
        return r.json()

    def _post(self, path: str, body: dict | None = None) -> Any:
        r = self._client.post(f"/v1/catalog{path}", json=body or {})
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        if "application/json" in ct and r.content:
            return r.json()
        return None

    def _delete(self, path: str) -> Any:
        r = self._client.delete(f"/v1/catalog{path}")
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        if "application/json" in ct and r.content:
            return r.json()
        return None

    # ══════════════════════════════════════════════════════════════════════════
    # OWNER OPS
    # ══════════════════════════════════════════════════════════════════════════

    def register_owner(
        self,
        *,
        name: str,
        owner_type: str = "repo",
        repo_hash: str | None = None,
        description: str = "",
        repo_root: Path | str | None = None,
        head_hash: str | None = None,
    ) -> Tumbler:
        payload: dict = {"name": name, "owner_type": owner_type}
        if repo_hash:       payload["repo_hash"] = repo_hash
        if description:     payload["description"] = description
        if repo_root:       payload["repo_root"] = str(repo_root)
        if head_hash:       payload["head_hash"] = head_hash
        result = self._post("/owners/register", payload)
        return Tumbler.parse(result["tumbler"])

    def owner_for_repo(self, repo_hash: str) -> Tumbler | None:
        result = self._get("/owners/by_repo_hash", repo_hash=repo_hash)
        if result is None or not result.get("tumbler"):
            return None
        return Tumbler.parse(result["tumbler"])

    def owner_tumblers_by_name(self, name: str) -> list[Tumbler]:
        result = self._get("/owners/by_name", name=name)
        return [Tumbler.parse(r["tumbler"]) for r in result.get("owners", [])]

    def ensure_owner_for_repo(
        self,
        *,
        repo: Path | str,
        name: str | None = None,
        owner_type: str = "repo",
        head_hash: str | None = None,
    ) -> Tumbler:
        payload: dict = {"repo": str(repo), "owner_type": owner_type}
        if name:       payload["name"] = name
        if head_hash:  payload["head_hash"] = head_hash
        result = self._post("/owners/ensure", payload)
        return Tumbler.parse(result["tumbler"])

    def set_owner_head_hash(self, owner: Tumbler | str, head_hash: str) -> None:
        self._post("/owners/set_head_hash", {
            "owner": str(owner), "head_hash": head_hash
        })

    # ══════════════════════════════════════════════════════════════════════════
    # DOCUMENT OPS
    # ══════════════════════════════════════════════════════════════════════════

    def register(
        self,
        *,
        owner: Tumbler | str,
        title: str,
        author: str | None = None,
        year: int | None = None,
        content_type: str = "paper",
        file_path: str | None = None,
        corpus: str | None = None,
        physical_collection: str | None = None,
        chunk_count: int = 0,
        head_hash: str | None = None,
        indexed_at: str | None = None,
        metadata: dict | None = None,
        source_mtime: float | None = None,
        source_uri: str | None = None,
        **kwargs: Any,
    ) -> Tumbler:
        payload: dict = {
            "owner": str(owner),
            "title": title,
            "content_type": content_type,
            "chunk_count": chunk_count,
        }
        if author:             payload["author"] = author
        if year:               payload["year"] = year
        if file_path:          payload["file_path"] = file_path
        if corpus:             payload["corpus"] = corpus
        if physical_collection: payload["physical_collection"] = physical_collection
        if head_hash:          payload["head_hash"] = head_hash
        if indexed_at:         payload["indexed_at"] = indexed_at
        if metadata:           payload["meta"] = metadata
        if source_mtime is not None: payload["source_mtime"] = source_mtime
        if source_uri:         payload["source_uri"] = source_uri
        payload.update(kwargs)
        result = self._post("/register", payload)
        return Tumbler.parse(result["tumbler"])

    def resolve(self, tumbler: Tumbler | str, *, follow_alias: bool = True) -> CatalogEntry | None:
        result = self._get(f"/show/{tumbler}", follow_alias=follow_alias)
        if result is None or not result.get("tumbler"):
            return None
        return _to_entry(result)

    def update(self, tumbler: Tumbler | str, **fields: Any) -> None:
        self._post("/update", {"tumbler": str(tumbler), **fields})

    def delete_document(self, tumbler: Tumbler | str) -> bool:
        result = self._delete(f"/documents/{tumbler}")
        if isinstance(result, dict):
            return bool(result.get("deleted", False))
        return False

    def find(self, query: str, *, content_type: str | None = None) -> list[CatalogEntry]:
        params: dict = {"q": query}
        if content_type:
            params["content_type"] = content_type
        result = self._get("/search", **params)
        return [_to_entry(d) for d in result.get("documents", [])]

    def by_file_path(self, owner: Tumbler | str, file_path: str) -> CatalogEntry | None:
        result = self._get("/by_file_path", owner=str(owner), file_path=file_path)
        if result is None or not result.get("tumbler"):
            return None
        return _to_entry(result)

    def by_source_uri(self, uri: str) -> CatalogEntry | None:
        result = self._get("/by_source_uri", uri=uri)
        if result is None or not result.get("tumbler"):
            return None
        return _to_entry(result)

    def by_owner(self, owner: Tumbler | str) -> list[CatalogEntry]:
        result = self._get("/by_owner", owner=str(owner))
        return [_to_entry(d) for d in result.get("documents", [])]

    def by_content_type(self, content_type: str) -> list[CatalogEntry]:
        result = self._get("/by_content_type", content_type=content_type)
        return [_to_entry(d) for d in result.get("documents", [])]

    def by_corpus(self, corpus: str) -> list[CatalogEntry]:
        result = self._get("/by_corpus", corpus=corpus)
        return [_to_entry(d) for d in result.get("documents", [])]

    def doc_count(self) -> int:
        result = self._get("/stats")
        return int(result.get("doc_count", 0))

    def all_documents(
        self, *, limit: int = 200, offset: int = 0
    ) -> list[CatalogEntry]:
        result = self._get("/documents", limit=limit, offset=offset)
        return [_to_entry(d) for d in result.get("documents", [])]

    def list_by_collection(
        self, physical_collection: str
    ) -> list[CatalogEntry]:
        result = self._get("/by_collection", collection=physical_collection)
        return [_to_entry(d) for d in result.get("documents", [])]

    def by_doc_id(self, doc_id: str) -> CatalogEntry | None:
        return self.resolve(doc_id)

    def lookup_doc_id_by_collection_and_path(
        self, collection: str, file_path: str
    ) -> str | None:
        result = self._get("/lookup_doc", collection=collection, file_path=file_path)
        return result.get("tumbler") if result else None

    def descendants(self, prefix: str) -> list[dict]:
        result = self._get("/descendants", prefix=prefix)
        return result.get("documents", [])

    def set_alias(self, tumbler: Tumbler | str, canonical: Tumbler | str) -> None:
        self._post("/set_alias", {"tumbler": str(tumbler), "canonical": str(canonical)})

    def resolve_alias(self, tumbler: Tumbler | str, *, max_hops: int = 16) -> Tumbler:
        result = self._get(f"/show/{tumbler}", follow_alias=True)
        if result and result.get("tumbler"):
            return Tumbler.parse(result["tumbler"])
        return Tumbler.parse(str(tumbler))

    def resolve_path(self, tumbler: Tumbler | str) -> Path | None:
        result = self._get(f"/show/{tumbler}")
        if result and result.get("file_path"):
            return Path(result["file_path"])
        return None

    def resolve_span(
        self, tumbler: Tumbler | str, span: str
    ) -> dict | None:
        result = self._get("/spans/resolve", tumbler=str(tumbler), span=span)
        return result if result else None

    def resolve_chash(
        self, chash: str, *, collection: str | None = None
    ) -> dict | None:
        params: dict = {"chash": chash}
        if collection:
            params["collection"] = collection
        result = self._get("/spans/chash", **params)
        return result if result else None

    def resolve_chunk(self, tumbler: Tumbler | str) -> dict | None:
        result = self._get(f"/show/{tumbler}")
        return result if result else None

    def resolve_span_text(self, tumbler: Tumbler | str, span: str) -> str | None:
        result = self._get("/spans/text", tumbler=str(tumbler), span=span)
        return result.get("text") if result else None

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
        return bool(result.get("deleted", False) if result else False)

    def links_from(
        self,
        tumbler: Tumbler | str,
        *,
        link_type: str | None = None,
    ) -> list[dict]:
        params: dict = {"tumbler": str(tumbler)}
        if link_type:
            params["link_type"] = link_type
        result = self._get("/links_from", **params)
        return result.get("links", []) if result else []

    def links_to(
        self,
        tumbler: Tumbler | str,
        *,
        link_type: str | None = None,
    ) -> list[dict]:
        params: dict = {"tumbler": str(tumbler)}
        if link_type:
            params["link_type"] = link_type
        result = self._get("/links_to", **params)
        return result.get("links", []) if result else []

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
        payload: dict = {}
        if from_t:    payload["from_tumbler"] = from_t
        if to_t:      payload["to_tumbler"] = to_t
        if link_type: payload["link_type"] = link_type
        if created_by: payload["created_by"] = created_by
        result = self._post("/bulk_unlink", payload)
        return int(result.get("deleted", 0) if result else 0)

    def validate_link(
        self,
        from_t: Tumbler | str,
        to_t: Tumbler | str,
        link_type: str,
    ) -> bool:
        """Check if both tumblers exist (link endpoints valid)."""
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
        params: dict = {
            "tumbler": str(tumbler),
            "direction": direction,
            "depth": depth,
        }
        if link_types:
            params["link_types"] = ",".join(link_types)
        return self._get("/traverse", **params) or {"nodes": [], "edges": []}

    def graph_many(
        self,
        tumblers: list[Tumbler | str],
        *,
        link_types: list[str] | None = None,
        direction: str = "both",
        depth: int = 1,
    ) -> dict:
        payload: dict = {
            "tumblers": [str(t) for t in tumblers],
            "direction": direction,
            "depth": depth,
        }
        if link_types:
            payload["link_types"] = link_types
        return self._post("/traverse_many", payload) or {"nodes": [], "edges": []}

    def link_audit(self, *, t3: Any = None) -> dict:
        return self._get("/link_audit") or {}

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
        self._post("/collections/register", {
            "name": name,
            "content_type": content_type,
            "owner_id": owner_id,
            "embedding_model": embedding_model,
            "model_version": model_version,
            "display_name": display_name,
            "legacy_grandfathered": legacy_grandfathered,
        })

    def delete_collection_projection(self, name: str, *, reason: str = "") -> bool:
        result = self._delete(f"/collections/{name}")
        return bool(result.get("deleted", False) if result else False)

    def list_collections(self) -> list[dict]:
        result = self._get("/collections")
        return result.get("collections", []) if result else []

    def get_collection(self, name: str) -> dict | None:
        result = self._get(f"/collections/{name}")
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
        params = {
            "content_type": content_type,
            "owner_id": owner_id,
            "embedding_model": embedding_model,
        }
        result = self._get("/collections/for_tuple", **params)
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
        result = self._post("/collections/rename", {"old": old, "new": new})
        return int(result.get("updated", 0) if result else 0)

    def update_document_collection(
        self,
        tumbler: Tumbler | str,
        collection: str,
    ) -> None:
        self._post("/documents/update_collection", {
            "tumbler": str(tumbler), "collection": collection
        })

    def update_documents_collection_batch(
        self,
        tumblers: list[Tumbler | str],
        collection: str,
    ) -> int:
        result = self._post("/documents/update_collection_batch", {
            "tumblers": [str(t) for t in tumblers], "collection": collection
        })
        return int(result.get("updated", 0) if result else 0)

    # ══════════════════════════════════════════════════════════════════════════
    # MANIFEST / CHUNKS
    # ══════════════════════════════════════════════════════════════════════════

    def write_manifest(self, doc_id: str, chunks: list[dict]) -> None:
        self._post("/manifest/write", {"doc_id": doc_id, "chunks": chunks})

    def append_manifest_chunks(self, doc_id: str, chunks: list[dict]) -> None:
        self._post("/manifest/append", {"doc_id": doc_id, "chunks": chunks})

    def get_manifest(self, doc_id: str) -> list[Any]:
        result = self._get(f"/manifest/{doc_id}")
        rows = result.get("chunks", []) if result else []
        # Return as _ManifestRow-compatible dicts (the caller iterates them)
        return rows

    def get_chunk_chashes(self, doc_id: str) -> list[str]:
        result = self._get(f"/manifest/{doc_id}/chashes")
        return result.get("chashes", []) if result else []

    def docs_for_chashes(self, chashes: list[str]) -> dict[str, list[str]]:
        result = self._post("/manifest/docs_for_chashes", {"chashes": chashes})
        return result.get("docs", {}) if result else {}

    def chashes_for_collection(self, physical_collection: str) -> set[str]:
        result = self._get("/manifest/chashes_for_collection", collection=physical_collection)
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
        payload: dict = {"doc_id": doc_id, "chunks": chunks}
        if new_collection:   payload["new_collection"] = new_collection
        if new_chunk_count is not None: payload["new_chunk_count"] = new_chunk_count
        self._post("/manifest/atomic_replace", payload)

    def resync_chunk_count_cache(self, doc_id: str) -> None:
        self._post("/manifest/resync", {"doc_id": doc_id})

    # ══════════════════════════════════════════════════════════════════════════
    # STATS
    # ══════════════════════════════════════════════════════════════════════════

    def stats(self) -> dict:
        return self._get("/stats") or {}

    def is_initialized(self) -> bool:
        """Always True when the service is reachable."""
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
            "defrag() is a JSONL compaction operation and has no Postgres equivalent. "
            "Per catalog-git-DECISION OPTION C, JSONL is dropped entirely in service mode."
        )

    def compact(self) -> dict:
        raise NotImplementedError(
            "compact() is a JSONL compaction operation and has no Postgres equivalent. "
            "Per catalog-git-DECISION OPTION C, JSONL is dropped entirely in service mode."
        )

    def sync(self, message: str = "catalog update") -> None:
        raise NotImplementedError(
            "sync() is a git commit operation and has no Postgres equivalent. "
            "Per catalog-git-DECISION OPTION C, git is dropped in service mode."
        )

    def pull(self) -> None:
        raise NotImplementedError(
            "pull() is a git pull operation and has no Postgres equivalent. "
            "Per catalog-git-DECISION OPTION C, git is dropped in service mode."
        )

    def rebuild_if_stale(self) -> None:
        pass  # no-op in service mode: Postgres is always consistent

    def _ensure_consistent(self) -> None:
        pass  # no-op in service mode

    # ══════════════════════════════════════════════════════════════════════════
    # COMPAT SHIMS — satisfy callers that probe catalog attributes
    # ══════════════════════════════════════════════════════════════════════════

    @property
    def catalog_path(self) -> Path | None:
        """Compat: callers that check this return None in service mode."""
        return None

    def jsonl_paths(self) -> tuple:
        return ()

    def mtime_paths(self) -> tuple:
        return ()
