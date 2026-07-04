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
  GET   /v1/catalog/docs/collection-counts
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
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx
import structlog

from nexus.catalog.catalog import CatalogEntry, CatalogLink, Tumbler
from nexus.catalog.catalog_spans import parse_chash_span
from nexus.catalog.catalog_writes import ManifestRow
from nexus.catalog.collection_name import CollectionName, owner_segment_for_tumbler

_log = structlog.get_logger(__name__)

DEFAULT_TENANT: str = "default"


def _manifest_row_from_dict(d: dict) -> ManifestRow:
    """Build a typed ``ManifestRow`` from a wire dict (return-type parity, RDR-168).

    The Java ``/manifest/get`` rows carry an extra ``doc_id`` key the dataclass does not
    model; only the seven schema fields are mapped.
    """
    return ManifestRow(
        position=int(d.get("position", 0)),
        # Defensive [:32]: the catalog chash is the 32-char natural ID; keep the read
        # path consistent with the write path and every other chash site.
        chash=(d.get("chash") or "")[:32],
        chunk_index=d.get("chunk_index"),
        line_start=d.get("line_start"),
        line_end=d.get("line_end"),
        char_start=d.get("char_start"),
        char_end=d.get("char_end"),
    )


def _link_from_dict(d: dict) -> CatalogLink:
    """Build a typed ``CatalogLink`` from a wire dict (return-type parity, RDR-168).

    Local ``Catalog.links_from`` / ``links_to`` / ``link_query`` return
    ``list[CatalogLink]`` and consumers do attribute access (``lnk.to_tumbler`` —
    e.g. the indexer rename-detection housekeeping); the wire returns dicts, so a raw
    list[dict] crashes them in service mode (nexus-njrcn.3 follow-up / critic finding).
    """
    def _tum(v: object) -> Tumbler:
        return Tumbler.parse(v) if isinstance(v, str) else v  # type: ignore[return-value]

    return CatalogLink(
        from_tumbler=_tum(d.get("from_tumbler", "")),
        to_tumbler=_tum(d.get("to_tumbler", "")),
        link_type=d.get("link_type", "") or "",
        from_span=d.get("from_span", "") or "",
        to_span=d.get("to_span", "") or "",
        created_by=d.get("created_by", "") or "",
        created_at=d.get("created_at", "") or "",
        meta=d.get("metadata") or d.get("meta") or {},
    )


# RDR-152 nexus-fjwxh: delegate to the centralized resolver (was an inline
# copy of the env->lease->fail-loud logic now shared across all clients).
from nexus.db.service_endpoint import resolve_service_endpoint as _resolve_endpoint

# nexus-gui8a: the service rejects POST /v1/catalog/manifest/get_many bodies
# carrying more than 1000 doc_ids with HTTP 400 (bisected: OK at 1000, 400 at
# 1001). ``get_manifests`` pages at this size and merges the per-page results.
_MANIFEST_GET_MANY_PAGE = 1000


def _coerce_legacy_grandfathered(d: dict) -> dict:
    """Coerce a collection row's ``legacy_grandfathered`` to ``bool`` (nexus-u26b4).

    ``CatalogRepository.collRow()``'s ``legacy_grandfathered`` column is a boxed
    Integer on the wire (serializes as a JSON number, 0/1); local
    ``Catalog.list_collections()`` (via ``_row_to_collection_dict``) already casts
    it to a real Python ``bool``. Mirrors that cast here so raw dict-returning
    callers (``get_collection``/``list_collections``/``collections_by_owner``) get
    the same type local gives, not just ``is_legacy_collection()`` which papered
    over the divergence at its own call site with a local ``bool(...)`` cast.
    """
    d = dict(d)
    if "legacy_grandfathered" in d:
        d["legacy_grandfathered"] = bool(d["legacy_grandfathered"])
    return d


def _to_entry(d: dict) -> CatalogEntry:
    """Convert a server response dict to a CatalogEntry.

    All CatalogEntry fields are non-optional; coerce None / missing values
    to the same empty-string / 0 / {} defaults the SQLite Catalog uses.
    The Java catalog document payload (CatalogRepository.docRowFromRecord)
    carries the RDR-101 bib_* columns; surface them on the entry (nexus-rzqto).
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
        bib_year=d.get("bib_year") or 0,
        bib_authors=d.get("bib_authors") or "",
        bib_venue=d.get("bib_venue") or "",
        bib_citation_count=d.get("bib_citation_count") or 0,
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
                        "NX_SERVICE_TOKEN is required when an explicit "
                        "base_url is passed (NX_STORAGE_BACKEND_CATALOG="
                        "service): with a caller-chosen URL the supervisor "
                        "lease token may not match — export the token, or "
                        "omit base_url to auto-discover both halves from "
                        "the lease ('nx daemon service start')."
                    )
            self._base_url = base_url.rstrip("/")
        else:
            self._base_url, token = _resolve_endpoint()
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

    @property
    def _db(self) -> None:  # type: ignore[return]
        """Guard: raises a clear error when commands/ code tries to use the raw SQLite handle.

        All 46 ``commands/`` sites that call ``cat._db`` are tracked in bead
        nexus-xnz0o (RDR-152: migrate commands/ catalog._db consumers).  Until
        migrated, flipping ``NX_STORAGE_BACKEND_CATALOG=service`` in a session
        that runs those commands will hit this property and get an actionable
        message instead of a bare ``AttributeError``.

        Bead nexus-xnz0o is a HARD BLOCKER of Phase-4 catalog deletion
        (nexus-gmiaf.24).
        """
        raise RuntimeError(
            "catalog._db is unavailable in service mode "
            "(NX_STORAGE_BACKEND_CATALOG=service).  "
            "This command path is not yet ported to the public catalog API — "
            "tracked in bead nexus-xnz0o.  "
            "Run with NX_STORAGE_BACKEND_CATALOG unset to use SQLite mode."
        )

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

    def delete_collection(self, name: str) -> dict[str, int]:
        """RDR-164 P2: atomically delete a collection + all its in-Postgres
        derived state via the service's single transactional deleteCollection.

        Returns the per-table deleted-row count map (``chunks_384``,
        ``chash_index``, ``topic_assignments``, ``topics``,
        ``taxonomy_centroids_*``, ``document_aspects``, ``document_highlights``,
        ``aspect_extraction_queue``, ``catalog_documents``,
        ``catalog_collections``). ``pipeline.db`` and the local-mode cascade
        stay client-side (see ``purge_collection_cascade``).
        """
        result = self._post("/collections/delete", {"name": name})
        return (result or {}).get("deleted", {}) or {}

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
        repo: Path | str,
        *,
        repo_name: str = "",
        description: str = "",
        name: str | None = None,
        owner_type: str = "repo",
        head_hash: str | None = None,
        tumbler_prefix: str | None = None,
    ) -> Tumbler:
        """Ensure an owner row exists for ``repo`` and return its Tumbler.

        If ``tumbler_prefix`` is provided (e.g. during ETL migration) it is used
        directly.  Otherwise the server assigns a prefix; we query it back.

        nexus-0cy4b: mirror the canonical ``Catalog.ensure_owner_for_repo`` — key
        the owner on ``repo_hash`` (from ``_repo_identity_with_main`` so it is
        stable across worktrees) and send it. The server dedups by repo_hash and
        assigns the prefix; without repo_hash the server would allocate a fresh
        prefix per call and collide on the (name, owner_type) unique constraint.
        """
        from nexus.repo_identity import _repo_identity_with_main  # noqa: PLC0415 — circular-dep avoidance (nexus.repo_identity)

        derived_name, repo_hash, main_repo = _repo_identity_with_main(Path(repo))
        # repo_name (canonical param) takes precedence, then name (benign extra), then derived
        effective_name = repo_name or name or derived_name
        # Idempotent fast path: an owner already exists for this repo
        # (owner_for_repo returns None on 404).
        if owner_type == "repo" and repo_hash:
            existing = self.owner_for_repo(repo_hash)
            if existing is not None:
                return existing
        payload: dict = {
            "name": effective_name,
            "owner_type": owner_type,
            "repo_root": str(main_repo),
            "repo_hash": repo_hash,
        }
        if description:    payload["description"] = description
        if tumbler_prefix: payload["tumbler_prefix"] = tumbler_prefix
        if head_hash:      payload["head_hash"] = head_hash
        result = self._post("/owners/upsert", payload)
        if isinstance(result, dict) and result.get("tumbler_prefix"):
            return Tumbler.parse(result["tumbler_prefix"])
        if tumbler_prefix:
            return Tumbler.parse(tumbler_prefix)
        # Query back — prefer the exact repo_hash lookup over name (ambiguous).
        if owner_type == "repo" and repo_hash:
            existing = self.owner_for_repo(repo_hash)
            if existing is not None:
                return existing
        owners = self._get("/owners/by_name", name=effective_name)
        rows = owners.get("owners", []) if owners else []
        if rows:
            return Tumbler.parse(rows[0]["tumbler_prefix"])
        raise RuntimeError(
            f"ensure_owner_for_repo: server did not return prefix for {repo!r}"
        )

    def set_owner_head_hash(self, owner: Tumbler | str, head_hash: str) -> int:
        """Persist *head_hash* on the owner row. Returns rowcount.

        Return-shape parity with local ``Catalog.set_owner_head_hash``
        (nexus-h8rf6.3 audit): the server already returns the exact rowcount
        (``{"updated": N}``, ``CatalogHandler.handleOwnerHeadHash``); the
        pre-fix client discarded it and returned ``None``, which meant
        ``indexer.py``'s ``if rowcount == 0: _log.warning(...)``
        (concurrent-owner-deletion detector) could never fire in service
        mode — a lost write went unobserved.
        """
        result = self._post("/owners/head_hash", {
            "tumbler_prefix": str(owner),
            "head_hash": head_hash,
        })
        return int(result.get("updated", 0) if result else 0)

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

    def curator_owner_tumbler_by_name(self, name: str) -> "Tumbler | None":
        """Return the tumbler of the *curator*-type owner with this name, or None.

        The ``(name, owner_type)`` constraint is UNIQUE so at most one curator
        owner per name exists.  Returns ``None`` when no curator owner is found.
        Used by doc_indexer / pipeline_stages curator lookups that previously
        issued ``SELECT … WHERE name=? AND owner_type='curator'`` directly.

        Implementation note — client-side ``owner_type`` filtering:
        The service endpoint ``GET /owners/by_name?name=<name>`` returns ALL
        owners across all ``owner_type`` values that match the given name (repo,
        curator, …).  This method filters the response list to the first entry
        where ``owner_type == "curator"``.  The filtering is therefore done on the
        client, not pushed into the query.  This is safe because the server enforces
        a ``UNIQUE(tenant_id, name, owner_type)`` constraint — there can be at most
        one curator owner per name per tenant — so the client-side filter is
        functionally equivalent to a server-side ``WHERE owner_type = 'curator'``
        predicate and produces the same result without an extra round-trip.
        """
        result = self._get("/owners/by_name", name=name)
        owners = result.get("owners", []) if result else []
        for o in owners:
            if o.get("owner_type") == "curator" and o.get("tumbler_prefix"):
                return Tumbler.parse(o["tumbler_prefix"])
        return None

    def get_owner_by_prefix(self, tumbler_prefix: str) -> dict | None:
        """Return full owner dict for the given tumbler_prefix, or None.

        Backs repos.py head_hash lookup that previously issued
        ``SELECT head_hash FROM owners WHERE tumbler_prefix=?`` directly.
        Returns None when the server responds 404 (prefix not found).
        """
        try:
            result = self._get("/owners/show", tumbler_prefix=tumbler_prefix)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        return result if result and result.get("tumbler_prefix") else None

    def list_owners_by_type(self, owner_type: str) -> list[dict]:
        """Return all owners of the given type.

        Backs repos.py:list_repos_dual which previously queried
        ``SELECT repo_root FROM owners WHERE owner_type='repo'`` directly.
        Uses POST /v1/catalog/owners/by_type endpoint (nexus-qnp5s).
        """
        result = self._post("/owners/by_type", {"owner_type": owner_type})
        return result.get("owners", []) if result else []

    def chunk_counts_for_docs(self, doc_ids: list[str]) -> dict[str, int]:
        """Batch-fetch chunk_count for a set of document tumblers.

        Returns ``{tumbler: chunk_count}`` for docs that have a chunk_count.
        Backs scoring.py hot-path which previously issued a batch
        ``SELECT tumbler, chunk_count FROM documents WHERE tumbler IN (?)``
        directly (nexus-qnp5s).
        """
        if not doc_ids:
            return {}
        result = self._post("/docs/chunk-counts", {"doc_ids": doc_ids})
        return {k: int(v) for k, v in (result or {}).items() if v is not None}

    def manifest_backfill(self) -> int:
        """Stamp manifest collection from the owning doc where NULL; return
        the number of rows stamped (RDR-159 P-1b).

        Wraps the ``nexus.manifest_backfill()`` stored function via the
        service (RDR-152: no direct Python PG connection). MUST be called
        before :meth:`manifest_orphans` — pre-backfill NULL-collection rows
        would otherwise read as false orphans.
        """
        result = self._post("/manifest/backfill", {})
        return int((result or {}).get("stamped", 0))

    #: dims the ``chunks_<dim>`` tables (and the stored function) accept.
    _MANIFEST_DIMS = (384, 768, 1024)

    def manifest_orphans(self, dim: int, *, limit: int = 100) -> dict:
        """Manifest rows with no chunk row in ``chunks_<dim>`` (RDR-159 P-1b).

        Returns ``{"dim": d, "count": n, "orphans": [...]}`` where ``count``
        is the exact orphan count (the non-vacuous migration-validation
        signal — zero is clean) and ``orphans`` is a diagnostic sample capped
        at ``limit`` (must be > 0; the count is the gate, not the sample
        length). count and sample are computed server-side in one transaction
        so they agree.

        The result is tenant-scoped: the stored function is SECURITY INVOKER
        and the service counts under the request tenant's RLS GUC. ``dim``
        must be one of 384/768/1024. Call :meth:`manifest_backfill` FIRST —
        pre-backfill (NULL-collection) rows are excluded by the function, so
        an orphan check on an un-backfilled manifest reads a false-clean zero.
        """
        if dim not in self._MANIFEST_DIMS:
            raise ValueError(
                f"dim must be one of {self._MANIFEST_DIMS}, got {dim!r}"
            )
        if limit <= 0:
            raise ValueError(f"limit must be > 0, got {limit!r}")
        result = self._get("/manifest/orphans", dim=dim, limit=limit)
        result = result or {}
        return {
            "dim": int(result.get("dim", dim)),
            "count": int(result.get("count", 0)),
            "orphans": result.get("orphans", []),
        }

    def relation_counts(self, relations: list[str]) -> dict[str, int]:
        """Tenant-scoped row counts for migration-verify relations.

        RDR-159 P-1a (nexus-0wz93): backs ``nexus.migration`` count
        verification. The service whitelists the relation names server-side
        (the fixed migration-verify set) and counts each under the request
        tenant's RLS GUC — so this is a safe replacement for the legacy
        admin-psql shell-out (RDR-152 bars a direct Python PG connection).

        Returns ``{relation: count}`` for the relations the service could
        count; a relation the service does not whitelist is simply absent
        (the caller treats a missing relation as INDETERMINATE, never a
        pass).
        """
        if not relations:
            return {}
        result = self._post(
            "/verify/relation-counts", {"relations": relations},
        )
        counts = (result or {}).get("counts", {})
        return {k: int(v) for k, v in counts.items() if v is not None}

    def links_from_batch(self, tumblers: list[str]) -> dict[str, list[dict]]:
        """Batch-fetch outbound links for a set of tumblers.

        Returns ``{tumbler: [{"from_tumbler": ..., "link_type": ...}, ...]}``.
        Backs scoring.py hot-path which previously issued a batch
        ``SELECT from_tumbler, link_type FROM links WHERE from_tumbler IN (?)``
        directly (nexus-qnp5s).
        """
        if not tumblers:
            return {}
        result = self._post("/links/from-batch", {"tumblers": tumblers})
        return result if result else {}

    def collections_by_owner(self, owner_id: str) -> list[dict]:
        """Return collections registered for the given owner_id.

        Backs repos.py which previously queried
        ``SELECT name, content_type FROM collections WHERE owner_id=?`` directly.
        Filters the full list_collections() result client-side (collection list
        is small; avoids a dedicated server endpoint).
        """
        all_colls = self.list_collections()
        return [c for c in all_colls if c.get("owner_id") == owner_id]

    def list_owners(self) -> list[dict]:
        """Return all owners for this tenant.

        Backs commands/catalog.py owners_cmd which previously queried
        ``SELECT tumbler_prefix, name, owner_type, repo_hash, description FROM owners``
        directly.  Uses GET /v1/catalog/owners/list (nexus-xnz0o).
        Returns list of dicts with keys: tumbler_prefix, name, owner_type,
        repo_hash, description, repo_root, head_hash.
        """
        result = self._get("/owners/list")
        return result.get("owners", []) if result else []

    def distinct_doc_collections(self) -> list[str]:
        """Return distinct physical_collection values from documents (non-empty).

        Backs commands/catalog.py backfill_collections_cmd and
        _run_collections_drift which previously issued
        ``SELECT DISTINCT physical_collection FROM documents WHERE physical_collection != ''``
        directly.  Uses GET /v1/catalog/docs/distinct-collections (nexus-xnz0o).
        """
        result = self._get("/docs/distinct-collections")
        return result.get("collections", []) if result else []

    def owners_with_roots(self) -> dict[str, str]:
        """Return {tumbler_prefix: repo_root} for owners with non-empty repo_root.

        Backs commands/catalog.py prune_stale_cmd and commands/t3.py which
        previously issued ``SELECT tumbler_prefix, repo_root FROM owners
        WHERE repo_root != ''`` directly.  Uses GET /v1/catalog/owners/all-with-roots
        (nexus-xnz0o).
        """
        result = self._get("/owners/all-with-roots")
        owners = result.get("owners", []) if result else []
        return {o["tumbler_prefix"]: o["repo_root"] for o in owners
                if o.get("tumbler_prefix") and o.get("repo_root")}

    def orphaned_docs(self) -> list[dict]:
        """Return documents with no incoming AND no outgoing links.

        Backs commands/catalog.py orphans_cmd which previously issued
        a ``LEFT JOIN links`` query directly.
        Uses GET /v1/catalog/docs/orphaned (nexus-xnz0o).
        Returns list of dicts with tumbler, title, content_type, file_path.
        """
        result = self._get("/docs/orphaned")
        return result.get("documents", []) if result else []

    def docs_with_absolute_paths(self) -> list[dict]:
        """Return documents whose file_path begins with '/'.

        Backs commands/doctor.py fix-paths which previously issued
        ``SELECT tumbler, file_path, physical_collection FROM documents
        WHERE file_path LIKE '/%'`` directly.
        Uses GET /v1/catalog/docs/absolute-paths (nexus-xnz0o).
        Returns list of dicts with tumbler, file_path, physical_collection.
        """
        result = self._get("/docs/absolute-paths")
        return result.get("documents", []) if result else []

    def get_collection_owner_root(self, name: str) -> tuple[str, str]:
        """Return (owner_id, repo_root) for a collection name.

        Backs commands/collection.py which previously chained
        ``SELECT owner_id FROM collections WHERE name=?`` then
        ``SELECT repo_root FROM owners WHERE tumbler_prefix=?`` directly.
        Uses GET /v1/catalog/collections/owner-root?name=X (nexus-xnz0o).
        Returns ("", "") when either lookup fails.
        """
        try:
            result = self._get("/collections/owner-root", name=name)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return "", ""
            raise
        if not result:
            return "", ""
        return result.get("owner_id", ""), result.get("repo_root", "")

    def collection_doc_counts(self) -> dict[str, int]:
        """Return {physical_collection: doc_count} for all non-empty collections.

        Backs commands/catalog.py _check_collection_health which needs per-collection
        doc counts to identify T3 orphans.
        Uses GET /v1/catalog/docs/collection-counts (nexus-xnz0o).
        """
        result = self._get("/docs/collection-counts")
        return {k: int(v) for k, v in result.get("counts", {}).items()}

    def coverage_by_content_type(self, owner_prefix: str = "") -> list[dict]:
        """Return per-content-type link coverage.

        For each distinct content_type in documents (optionally scoped to
        owner_prefix), return {content_type, total, linked} where:
          - total  = COUNT(*) documents of that type
          - linked = COUNT(DISTINCT tumbler) documents with at least one link
                     in either direction (from_tumbler OR to_tumbler)

        Uses GET /v1/catalog/coverage?owner_prefix=<opt> (nexus-3cwnx).
        Mirrors Catalog.coverage_by_content_type().
        """
        params: dict = {}
        if owner_prefix:
            params["owner_prefix"] = owner_prefix
        result = self._get("/coverage", **params)
        rows = result.get("coverage", []) if result else []
        return [
            {
                "content_type": str(r.get("content_type", "")),
                "total":  int(r.get("total", 0)),
                "linked": int(r.get("linked", 0)),
            }
            for r in rows
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
        # nexus-h9f1w / GH #1350: the service /list ignores file_path when owner
        # is set and returns the FULL owner list, so a brand-new file's query
        # yields docs[0] (an unrelated doc). Trusting docs[0] mis-attributed the
        # new file's chunks to that doc, overwriting its manifest (silent data
        # corruption). Filter by exact file_path client-side; correct regardless
        # of the server-side bug (Fix B tracked separately).
        result = self._get("/list", owner=str(owner), file_path=file_path)
        docs = result.get("documents", []) if result else []
        match = [d for d in docs if d.get("file_path") == file_path]
        return _to_entry(match[0]) if match else None

    def by_source_uri(self, uri: str) -> CatalogEntry | None:
        # nexus-h9f1w / GH #1350: same docs[0]-trust class as by_file_path.
        # Defense-in-depth exact-match filter (the server currently filters
        # source_uri correctly; this guards against an owner-list-leak-style
        # regression and keeps the sibling lookups consistent).
        result = self._get("/list", source_uri=uri)
        docs = result.get("documents", []) if result else []
        match = [d for d in docs if d.get("source_uri") == uri]
        return _to_entry(match[0]) if match else None

    def find_by_file_path(self, file_path: str) -> CatalogEntry | None:
        """Return the first document matching file_path (no owner filter).

        Backs dt.py stamp command which looks up by file_path without a
        known owner (nexus-xnz0o).  Uses GET /list?file_path=X (owner-agnostic
        form supported by the Java /list endpoint).
        """
        # nexus-h9f1w / GH #1350: exact-match guard, same class as by_file_path.
        # The owner-agnostic /list routes to documentsByFilePath (exact eq) so
        # this is a no-op today; kept for consistency + regression defense. When
        # a path is shared across owners the server returns >1 match and this
        # preserves the documented "first match" contract.
        result = self._get("/list", file_path=file_path)
        docs = result.get("documents", []) if result else []
        match = [d for d in docs if d.get("file_path") == file_path]
        return _to_entry(match[0]) if match else None

    def by_owner(self, owner: Tumbler | str) -> list[CatalogEntry]:
        return self._docs_from(self._get("/list", owner=str(owner)))

    def by_content_type(self, content_type: str) -> list[CatalogEntry]:
        return self._docs_from(self._get("/list", content_type=content_type))

    def by_corpus(self, corpus: str) -> list[CatalogEntry]:
        return self._docs_from(self._get("/list", corpus=corpus))

    def all_documents(
        self, limit: int = 0, *, content_type: str = "", offset: int = 0,
    ) -> list[CatalogEntry]:
        if limit > 0:
            params: dict = {"limit": limit, "offset": offset}
            if content_type:
                params["content_type"] = content_type
            return self._docs_from(self._get("/list", **params))
        # limit == 0 means UNBOUNDED (canonical semantics).
        if content_type:
            # The service's content_type branch (CatalogHandler.handleList ->
            # documentsByContentType) ignores limit/offset and returns ALL matching rows
            # in one shot. A pagination loop would re-fetch the same full set every page
            # and never terminate, so issue a single unbounded request. (Service-side
            # content_type+limit interaction is a CA-4 / P4 item: nexus-pwclh.)
            return self._docs_from(self._get("/list", content_type=content_type))
        # Unfiltered: the service respects limit/offset (listDocuments), so paginate
        # exhaustively rather than silently capping — a hardcoded cap would truncate
        # large catalogs with no error.
        page = 1000
        out: list[CatalogEntry] = []
        cur = offset
        while True:
            batch = self._docs_from(self._get("/list", limit=page, offset=cur))
            out.extend(batch)
            if len(batch) < page:
                break
            cur += page
        return out

    def list_by_collection(
        self, physical_collection: str, *, limit: int | None = None,
    ) -> list[CatalogEntry]:
        params: dict = {"collection": physical_collection}
        if limit is not None:
            params["limit"] = limit
        return self._docs_from(self._get("/list", **params))

    def by_doc_id(self, doc_id: str) -> CatalogEntry | None:
        return self.resolve(doc_id)

    def resolve_many(self, doc_ids: list[str]) -> "dict[str, CatalogEntry]":
        """Batch-resolve multiple doc_ids to CatalogEntry objects.

        nexus-7lm3q: replaces the N per-doc ``by_doc_id()`` loop in
        ``_attach_display_paths`` (search_engine.py) so a single search
        with M distinct docs pays ONE catalog round-trip instead of M.
        Mirrors ``POST /v1/catalog/resolve_many`` → Java
        ``CatalogHandler.handleResolveMany`` /
        ``CatalogRepository.resolveMany``.

        Returns a dict keyed by doc_id; each value is a CatalogEntry.
        Missing or unresolvable doc_ids are absent from the result.

        nexus-gui8a: paged at ``_MANIFEST_GET_MANY_PAGE`` per POST — the
        service enforces the same ``MAX_BATCH_DOC_IDS = 1000`` cap on
        ``/resolve_many`` as on ``/manifest/get_many``.
        """
        if not doc_ids:
            return {}
        entries: dict[str, CatalogEntry] = {}
        for start in range(0, len(doc_ids), _MANIFEST_GET_MANY_PAGE):
            batch = doc_ids[start : start + _MANIFEST_GET_MANY_PAGE]
            result = self._post("/resolve_many", {"doc_ids": batch})
            if not result:
                continue
            for doc_id, raw in result.get("entries", {}).items():
                if raw and raw.get("tumbler"):
                    entries[doc_id] = _to_entry(raw)
        return entries

    def lookup_doc_id_by_collection_and_path(
        self, collection: str, source_path: str
    ) -> str:
        """Return the tumbler/legacy-doc_id for a (collection, path) probe.

        Return-shape parity with local ``Catalog.lookup_doc_id_by_collection_and_path``
        (nexus-h8rf6.3 audit): local's documented contract is ``""`` (never
        ``None``) on no-match, so the client normalizes to match — existing
        callers already treat both as falsy, but a non-Optional ``str``
        matches the documented caller contract exactly.
        """
        try:
            result = self._get(
                "/resolve",
                file_path=source_path,  # wire key is file_path; canonical param is source_path
                collection=collection,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return ""
            raise
        docs = result.get("documents", []) if result else []
        return docs[0]["tumbler"] if docs else ""

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

    def resolve_span(
        self,
        span: str,
        physical_collection: str,
        t3: "Any" = None,
    ) -> dict | None:
        """Resolve a ``chash:`` span to chunk text + metadata (nexus-njrcn.4).

        Non-``chash:`` spans (line-range, chunk:char) are out of scope for
        service mode — return ``None`` so callers fall back gracefully.
        ``t3`` is a local-mode artefact; accepted for conformance, ignored.
        """
        if not span.startswith("chash:"):
            return None
        try:
            hex_chash, char_range = parse_chash_span(span)
        except ValueError:
            return None
        try:
            result = self._get(
                "/resolve_span",
                span_chash=hex_chash[:32],
                collection=physical_collection,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        if not result:
            return None
        text: str = result.get("chunk_text", "")
        if char_range:
            text = text[char_range[0]:char_range[1]]
        out: dict = {
            "chunk_text": text,
            "metadata":   result.get("metadata", {}),
            "chunk_hash": hex_chash,
        }
        if char_range:
            out["char_range"] = char_range
        return out

    def resolve_chash(
        self,
        chash: str,
        t3: "Any" = None,
        chash_index: "Any" = None,
        *,
        prefer_collection: str | None = None,
    ) -> dict | None:
        """Globally resolve a chash to chunk text + collection + doc_id (nexus-njrcn.4).

        ``t3`` and ``chash_index`` are local-mode artefacts; accepted for
        conformance, ignored. The service resolves via its own internal index.
        """
        try:
            hex_chash, char_range = parse_chash_span(chash)
        except ValueError:
            return None
        params: dict[str, Any] = {"chash": hex_chash[:32]}
        if prefer_collection:
            params["prefer_collection"] = prefer_collection
        try:
            result = self._get("/resolve_chash", **params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        if not result:
            return None
        chunk_text: str = result.get("chunk_text", "")
        if char_range:
            chunk_text = chunk_text[char_range[0]:char_range[1]]
        out: dict = {
            # Canonical contract (catalog_spans._build_ref): chash/chunk_hash are the
            # FULL parsed hex, NOT the 32-char wire key the service stores/echoes — a
            # downstream consumer comparing against a 64-char citation hex must match.
            "chash":               hex_chash,
            "chunk_hash":          hex_chash,
            "physical_collection": result.get("physical_collection", ""),
            "doc_id":              result.get("doc_id", ""),
            "chunk_text":          chunk_text,
            "metadata":            result.get("metadata", {}),
        }
        if char_range:
            out["char_range"] = char_range
        return out

    def resolve_chunk(self, tumbler: Tumbler | str) -> dict | None:
        """Resolve a 4-segment chunk tumbler to its document + chunk metadata.

        Mirrors the local ``Catalog.resolve_chunk`` contract exactly
        (catalog_docs.py): chunks are implicit addresses, not their own
        catalog rows. Returns ``None`` immediately (no wire round-trip) when
        ``tumbler`` is not a chunk address (``tumbler.chunk is None``) — the
        same short-circuit the local side takes. Otherwise calls
        ``GET /resolve_chunk`` (nexus-gc2ze).
        """
        t = Tumbler.parse(tumbler) if isinstance(tumbler, str) else tumbler
        if t.chunk is None:
            return None
        try:
            result = self._get("/resolve_chunk", tumbler=str(t))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        if not result:
            return None
        return {
            "document_tumbler": result.get("document_tumbler", ""),
            "chunk_index": result.get("chunk_index", 0),
            "physical_collection": result.get("physical_collection", ""),
            "title": result.get("title", ""),
            "content_type": result.get("content_type", ""),
        }

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
        created_by: str,
        *,
        from_span: str = "",
        to_span: str = "",
        allow_dangling: bool = False,
        **meta: object,
    ) -> bool:
        payload: dict = {
            "from_tumbler": str(from_t),
            "to_tumbler": str(to_t),
            "link_type": link_type,
            "created_by": created_by,
            "from_span": from_span,
            "to_span": to_span,
            "allow_dangling": allow_dangling,
        }
        if meta:
            payload["metadata"] = dict(meta)
        result = self._post("/link", payload)
        return bool(result.get("created") if result else False)  # True=created, False=merged (njrcn.3)

    def link_if_absent(
        self,
        from_t: Tumbler | str,
        to_t: Tumbler | str,
        link_type: str,
        created_by: str,
        *,
        from_span: str = "",
        to_span: str = "",
        allow_dangling: bool = False,
        **meta: object,
    ) -> bool:
        # THE LOAD-BEARING CASE (RDR-168 Phase 3): all params explicitly wired to the
        # service. Previously **kwargs swallowed them silently (data loss). Each param
        # now serializes onto the wire payload; no accept-and-drop.
        #
        # Idempotency parity with canonical _LinkOps.link_if_absent (INSERT-OR-SKIP):
        # the service's POST /link is an UPSERT (ON CONFLICT DO UPDATE), which would
        # overwrite created_by / from_span / to_span / meta on an existing link — silent
        # mutation on every re-index. Canonical instead SKIPS when the row exists and
        # never touches its fields. Pre-flight the existence check and skip the write so
        # the "if absent" contract holds. (TOCTOU is acceptable: the canonical
        # cross-process path is not atomic either, and the common case is a re-index
        # where the row already exists and we correctly no-op.)
        existing = self.link_query(
            from_t=str(from_t), to_t=str(to_t), link_type=link_type, limit=1
        )
        if existing:
            return False  # row present — no overwrite, matching canonical skip
        payload: dict = {
            "from_tumbler": str(from_t),
            "to_tumbler": str(to_t),
            "link_type": link_type,
            "created_by": created_by,
            "from_span": from_span,
            "to_span": to_span,
            "allow_dangling": allow_dangling,
        }
        if meta:
            payload["metadata"] = dict(meta)
        result = self._post("/link", payload)
        return bool(result.get("created") if result else False)  # True=created, False=merged (njrcn.3)

    def unlink(
        self,
        from_t: Tumbler | str,
        to_t: Tumbler | str,
        link_type: str,
    ) -> int:
        """Delete one or all link types between *from_t* and *to_t*.

        Return-shape parity with local ``Catalog.unlink`` (nexus-h8rf6.3 audit):
        the server already returns the exact rowcount (``{"deleted": N}``,
        ``CatalogHandler.handleUnlink``); the pre-fix client discarded it in
        favour of a bool, so ``commands/catalog_cmds/links.py``'s
        ``click.echo(f"Removed {removed} link(s)")`` and ``mcp/catalog.py``'s
        ``{"removed": removed}`` response printed/returned ``True``/``False``
        instead of a count.
        """
        result = self._post("/unlink", {
            "from_tumbler": str(from_t),
            "to_tumbler": str(to_t),
            "link_type": link_type,
        })
        return int(result.get("deleted", 0) if result else 0)

    def links_from(
        self,
        tumbler: Tumbler | str,
        link_type: str = "",
        link_types: list[str] | None = None,
    ) -> list[CatalogLink]:
        params: dict = {"tumbler": str(tumbler), "direction": "out"}
        # njrcn.5: forward link_types to the server-side IN filter (no client-side
        # over-fetch). link_types takes precedence; else the single link_type.
        if link_types:
            params["link_types"] = ",".join(link_types)
        elif link_type:
            params["link_type"] = link_type
        result = self._get("/links", **params)
        return [_link_from_dict(r) for r in (result.get("links_from", []) if result else [])]

    def links_to(
        self,
        tumbler: Tumbler | str,
        link_type: str = "",
        link_types: list[str] | None = None,
    ) -> list[CatalogLink]:
        params: dict = {"tumbler": str(tumbler), "direction": "in"}
        if link_types:
            params["link_types"] = ",".join(link_types)
        elif link_type:
            params["link_type"] = link_type
        result = self._get("/links", **params)
        return [_link_from_dict(r) for r in (result.get("links_to", []) if result else [])]

    def link_query(
        self,
        *,
        from_t: str | None = None,
        to_t: str | None = None,
        link_type: str | None = None,
        created_by: str | None = None,
        created_at_before: str | None = None,
        direction: str | None = None,
        tumbler: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[CatalogLink]:
        params: dict = {"limit": limit, "offset": offset}
        if from_t:            params["from_tumbler"] = from_t
        if to_t:              params["to_tumbler"] = to_t
        if link_type:         params["link_type"] = link_type
        if created_by:        params["created_by"] = created_by
        if created_at_before: params["created_at_before"] = created_at_before
        if direction:         params["direction"] = direction
        if tumbler:           params["tumbler"] = tumbler
        result = self._get("/link_query", **params)
        return [_link_from_dict(r) for r in (result.get("links", []) if result else [])]

    def bulk_unlink(
        self,
        from_t: str = "",
        to_t: str = "",
        link_type: str = "",
        created_by: str = "",
        created_at_before: str = "",
        dry_run: bool = False,
    ) -> int:
        """Bulk delete links — all fields are optional filters.

        Canonical parity: requires at least one filter (unless ``dry_run``), and
        ``dry_run=True`` returns the count that *would* be deleted (no deletion).
        """
        has_filter = any((from_t, to_t, link_type, created_by, created_at_before))
        if not has_filter and not dry_run:
            raise ValueError(
                "bulk_unlink requires at least one filter (or dry_run=True)"
            )
        if dry_run:
            # The service has no server-side dry_run yet (Phase 4 follow-up nexus-pwclh).
            # Compute the real would-delete count via link_query — matching canonical
            # semantics — rather than silently returning 0 (a misleading preview).
            page = 1000
            cur = 0
            total = 0
            while True:
                batch = self.link_query(
                    from_t=from_t or None,
                    to_t=to_t or None,
                    link_type=link_type or None,
                    created_by=created_by or None,
                    created_at_before=created_at_before or None,
                    limit=page,
                    offset=cur,
                )
                total += len(batch)
                if len(batch) < page:
                    break
                cur += page
            return total
        payload: dict = {}
        if from_t:            payload["from_tumbler"] = from_t
        if to_t:              payload["to_tumbler"] = to_t
        if link_type:         payload["link_type"] = link_type
        if created_by:        payload["created_by"] = created_by
        if created_at_before: payload["created_at_before"] = created_at_before
        result = self._post("/unlink", payload)
        return int(result.get("deleted", 0) if result else 0)

    def validate_link(
        self,
        from_t: Tumbler | str,
        to_t: Tumbler | str,
        link_type: str,
    ) -> list[str]:
        """Return a list of validation errors (empty = link is valid).

        Return-type parity (nexus-u26b4): mirrors local
        ``_LinkOps.validate_link``'s errors-list contract exactly (was a
        ``bool``), reusing existing wire routes — ``self.resolve()`` for
        the existence checks and ``self.link_query()`` for the duplicate
        check — rather than a new server endpoint.
        """
        errors: list[str] = []
        if self.resolve(from_t) is None:
            errors.append(f"from_tumbler {from_t} not found in documents")
        if self.resolve(to_t) is None:
            errors.append(f"to_tumbler {to_t} not found in documents")
        existing = self.link_query(
            from_t=str(from_t), to_t=str(to_t), link_type=link_type, limit=1,
        )
        if existing:
            errors.append(
                f"duplicate: link ({from_t}, {to_t}, {link_type!r}) "
                f"already exists"
            )
        return errors

    def _traverse(self, payload: dict) -> dict:
        """POST /v1/catalog/traverse and convert wire dicts to typed objects.

        Return-type parity (nexus-u26b4, the h8rf6.3 incident class): local
        ``Catalog.graph()``/``graph_many()`` return
        ``{"nodes": list[CatalogEntry], "edges": list[CatalogLink]}`` (matching
        the return-type-parity pattern used elsewhere in this module, e.g.
        ``links_from``/``get_manifest``); this previously returned the RAW wire
        dict unconverted, so consumers doing attribute access (``node.tumbler``,
        ``edge.link_type`` — e.g. ``mcp/core.py``'s traverse tool,
        ``commands/catalog_cmds/links.py``'s ``links`` command) silently
        degraded or crashed in service mode.
        """
        result = self._post("/traverse", payload) or {"nodes": [], "edges": []}
        return {
            "nodes": [_to_entry(n) for n in result.get("nodes", [])],
            "edges": [_link_from_dict(e) for e in result.get("edges", [])],
        }

    def graph(
        self,
        tumbler: Tumbler | str,
        depth: int = 1,
        direction: str = "both",
        link_type: str = "",
        link_types: list[str] | None = None,
        include_heuristic: bool = False,
    ) -> dict:
        """BFS traversal from a single seed — POST /v1/catalog/traverse."""
        payload: dict = {
            "seeds": [str(tumbler)],
            "direction": direction,
            "depth": depth,
        }
        # Merge link_type (scalar) into link_types list for the wire
        effective_link_types = list(link_types) if link_types else []
        if link_type and link_type not in effective_link_types:
            effective_link_types.insert(0, link_type)
        if effective_link_types:
            payload["link_types"] = effective_link_types
        # include_heuristic: forwarded to service for future support; currently informational
        if include_heuristic:
            payload["include_heuristic"] = True
        return self._traverse(payload)

    def graph_many(
        self,
        seeds: list[Tumbler | str],
        depth: int = 1,
        direction: str = "both",
        link_type: str = "",
        link_types: list[str] | None = None,
        include_heuristic: bool = False,
    ) -> dict:
        """BFS traversal from multiple seeds — POST /v1/catalog/traverse."""
        payload: dict = {
            "seeds": [str(t) for t in seeds],
            "direction": direction,
            "depth": depth,
        }
        # Merge link_type (scalar) into link_types list for the wire
        effective_link_types = list(link_types) if link_types else []
        if link_type and link_type not in effective_link_types:
            effective_link_types.insert(0, link_type)
        if effective_link_types:
            payload["link_types"] = effective_link_types
        # include_heuristic: forwarded to service for future support; currently informational
        if include_heuristic:
            payload["include_heuristic"] = True
        return self._traverse(payload)

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
        return [
            _coerce_legacy_grandfathered(c)
            for c in (result.get("collections", []) if result else [])
        ]

    def get_collection(self, name: str) -> dict | None:
        try:
            result = self._get("/collections/get", name=name)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        if not result or not result.get("name"):
            return None
        return _coerce_legacy_grandfathered(result)

    def is_legacy_collection(self, name: str) -> bool:
        coll = self.get_collection(name)
        return bool(coll.get("legacy_grandfathered", False)) if coll else False

    def collection_for(
        self,
        content_type: str,
        owner: Tumbler | str,
        embedding_model: str,
        *,
        bump: bool = False,
    ) -> CollectionName:
        # Mirror canonical _DocumentOps.collection_for: the catalog RENDERS the name; a
        # NEW tuple lands at v1 (never a 404/None), an existing tuple returns vN, and
        # bump returns vN+1. The service /collections/for_tuple is lookup-only (404 on a
        # new tuple), so we use it purely as the version oracle and render the name with
        # the SAME canonical helpers (owner_segment_for_tumbler + CollectionName) — no
        # client-side reimplementation, so local and service modes cannot diverge on the
        # physical name. (nexus-njrcn.2; the prior lookup-only behaviour 404'd the very
        # first index of a repo — the CA-4 empty-catalog cause.)
        owner_id = owner_segment_for_tumbler(owner)
        if not owner_id:
            raise ValueError(
                f"collection_for: cannot derive owner_id segment from owner {owner!r}"
            )
        existing_version = 0
        try:
            result = self._get(
                "/collections/for_tuple",
                content_type=content_type,
                owner_id=owner_id,  # canonical owner SEGMENT, not the raw tumbler
                embedding_model=embedding_model,
            )
            if result and result.get("name"):
                existing_version = CollectionName.parse(result["name"]).model_version
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            # 404 == tuple not yet registered == new tuple → v1 (canonical semantics).
        if existing_version == 0:
            new_version = 1
        elif bump:
            new_version = existing_version + 1
        else:
            new_version = existing_version
        return CollectionName(
            content_type=content_type,
            owner_id=owner_id,
            embedding_model=embedding_model,
            model_version=new_version,
        )

    def collection_for_repo(
        self,
        repo: Path,
        content_type: str,
        *,
        bump: bool = False,
    ) -> CollectionName:
        # Mirrors canonical _DocumentOps.collection_for_repo:
        # look up the owner by repo_hash, then resolve the embedding model.
        from nexus.repo_identity import _repo_identity  # noqa: PLC0415 — circular-dep avoidance
        from nexus.corpus import effective_embedding_model_for_writes  # noqa: PLC0415 — circular-dep avoidance

        _, repo_hash = _repo_identity(repo)
        owner = self.owner_for_repo(repo_hash)
        if owner is None:
            raise LookupError(
                f"collection_for_repo: no owner registered for repo_hash {repo_hash!r} "
                f"(repo {repo!s}). Call ensure_owner_for_repo() first."
            )
        embedding_model = effective_embedding_model_for_writes(content_type)
        return self.collection_for(content_type, owner, embedding_model, bump=bump)

    def supersede_collection(
        self,
        old_name: str,
        new_name: str,
        *,
        reason: str = "",
        superseded_at: str | None = None,
    ) -> None:
        # Wire keys: name (old_name), superseded_by (new_name); reason is informational.
        payload: dict = {"name": old_name, "superseded_by": new_name}
        if reason:
            payload["reason"] = reason
        if superseded_at:
            payload["superseded_at"] = superseded_at
        self._post("/collections/supersede", payload)

    def rename_collection(self, old: str, new: str, *, cross_model: bool = False) -> int:
        # RDR-164 P3: the consolidated endpoint returns {"renamed": {per-table counts}}.
        # The int contract reports the re-homed catalog_documents count.
        renamed = self.rename_collection_cascade(old, new, cross_model=cross_model)
        return int(renamed.get("catalog_documents", 0))

    def rename_collection_cascade(
        self, old: str, new: str, *, cross_model: bool = False
    ) -> dict[str, int]:
        """RDR-164 P3: atomically re-home a collection X->Y and all its in-Postgres
        derived state via the service's single transactional renameCollection.

        Returns the per-table re-home count map (``catalog_collections_inserted``,
        ``chunks_384/768/1024``, ``chash_index``, ``topic_assignments``, ``topics``,
        ``taxonomy_meta``, ``taxonomy_centroids_*``, ``document_aspects``,
        ``document_highlights``, ``aspect_extraction_queue``, ``catalog_documents``,
        ``search_telemetry``, ``hook_failures``, ``catalog_collections_deleted``).
        The cross-model COPY branch (target already registered) returns only
        ``catalog_documents``. ``pipeline.db`` and the local-mode fan-out stay
        client-side (see ``rename_collection_data_plane``).

        ``cross_model`` (nexus-gaou3): pass ``True`` ONLY for a deliberate RDR-162
        cross-model repoint where ``new`` is already a populated target. With the
        default ``False`` the service rejects an existing ``new`` with 409 (a plain
        rename onto an existing collection is a collision, not a silent COPY).
        """
        body: dict[str, Any] = {"old_name": old, "new_name": new}
        if cross_model:
            body["cross_model"] = True
        result = self._post("/collections/rename", body)
        renamed = (result or {}).get("renamed", {}) or {}
        return {k: int(v) for k, v in renamed.items()}

    def update_document_collection(
        self, tumbler: str, new_collection: str,
    ) -> bool:
        result = self._post("/update", {
            "tumbler": str(tumbler), "physical_collection": new_collection,
        })
        return bool(result.get("updated", 0) > 0 if result else False)

    def update_documents_collection_batch(
        self,
        pairs: list[tuple[str, str]],
    ) -> int:
        # No server-side batch endpoint yet (guard+track bead nexus-gmiaf.24);
        # iterate single updates. Each pair is (tumbler, new_collection).
        for tumbler, new_collection in pairs:
            self.update_document_collection(tumbler, new_collection)
        return len(pairs)

    # ══════════════════════════════════════════════════════════════════════════
    # MANIFEST / CHUNKS
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _manifest_rows(chunks: list[dict]) -> list[dict]:
        """Normalize manifest rows for the wire: chash to the 32-char natural ID.

        The catalog chash is ``chunk_text_hash[:32]`` (RDR-108 D1 — the Chroma natural
        ID). The local Catalog truncates at the write layer (catalog_writes.py); callers
        pass the full 64-char ``chunk_text_hash`` (e.g. the manifest post-store hook), so
        the service client MUST truncate too or the Postgres
        ``catalog_document_chunks_chash_len_check`` (length == 32) rejects the insert
        (RDR-168 nexus-njrcn.6 layer 2).
        """
        out: list[dict] = []
        for c in chunks:
            row = dict(c)
            row["chash"] = (row.get("chash") or "")[:32]
            out.append(row)
        return out

    def write_manifest(self, doc_id: str, chunks: list[dict]) -> None:
        """Replace manifest for doc_id (atomic delete + insert)."""
        self._post("/manifest/write", {"doc_id": doc_id, "rows": self._manifest_rows(chunks)})

    def append_manifest_chunks(self, doc_id: str, chunks: list[dict]) -> None:
        self._post("/manifest/append", {"doc_id": doc_id, "rows": self._manifest_rows(chunks)})

    def get_manifest(self, doc_id: str) -> list[ManifestRow]:
        """Return ordered manifest rows — typed like local Catalog.get_manifest.

        Return-type parity (RDR-168): local returns list[ManifestRow] and consumers do
        attribute access (``row.chash``); the wire returns dicts, so reconstruct the
        dataclass here or service-mode housekeeping (e.g. _prune_misclassified) breaks
        with ``'dict' object has no attribute 'chash'``.
        """
        result = self._get("/manifest/get", doc_id=doc_id)
        rows = result.get("rows", []) if result else []
        return [_manifest_row_from_dict(r) for r in rows]

    def get_manifests(self, doc_ids: list[str]) -> dict[str, list[ManifestRow]]:
        """Batch-fetch manifests for multiple doc_ids in one round-trip.

        nexus-7lm3q: replaces the N per-doc ``get_manifest()`` loop in
        ``_attach_doc_ids_from_catalog`` (search_engine.py) so a single
        search with M distinct docs pays ONE catalog round-trip instead
        of M. Mirrors ``POST /v1/catalog/manifest/get_many`` → Java
        ``CatalogHandler.handleManifestGetMany`` /
        ``CatalogRepository.getManifestMany``.

        Returns a dict keyed by doc_id; each value is the ordered list
        of manifest rows (same shape as ``get_manifest()``). Missing
        doc_ids are absent from the result (not keyed to empty list).

        nexus-gui8a: doc_ids are paged at ``_MANIFEST_GET_MANY_PAGE`` per
        POST — the service 400s on bodies with more than 1000 doc_ids, so
        an un-paged call from ``build_staleness_cache`` (4500+ ids on medium
        repos) silently built an empty cache and degraded every
        ``nx index repo`` to a full re-index.

        A page failure propagates (whole call fails loud). Deliberate:
        every caller already handles the exception in its own safe
        direction — build_staleness_cache degrades to full re-index,
        embed_migrate blocks its destructive re-index, and catalog
        doctor must see a hard error rather than a silent partial that
        reads as data corruption.
        """
        if not doc_ids:
            return {}
        merged: dict[str, list[ManifestRow]] = {}
        for start in range(0, len(doc_ids), _MANIFEST_GET_MANY_PAGE):
            batch = doc_ids[start : start + _MANIFEST_GET_MANY_PAGE]
            result = self._post("/manifest/get_many", {"doc_ids": batch})
            manifests = result.get("manifests", {}) if result else {}
            for did, rows in manifests.items():
                merged[did] = [_manifest_row_from_dict(r) for r in rows]
        return merged

    def get_chunk_chashes(self, doc_id: str) -> list[str]:
        """Return chashes for all chunks of doc_id.

        The server's /manifest/chashes endpoint queries by collection, not doc_id.
        We resolve the document's physical_collection first, then return its
        chashes.  This is a best-effort approximation; for a strict per-doc list
        use get_manifest() + extract chash from each row.
        """
        rows = self.get_manifest(doc_id)
        return [row.chash for row in rows if row.chash]

    def docs_for_chashes(self, chashes: list[str]) -> dict[str, list[str]]:
        """Reverse-lookup: chash -> [doc_id, ...] — dict-shape parity with local
        ``Catalog.docs_for_chashes`` (nexus-h8rf6.3).

        ``CatalogRepository.docsForChashes()`` runs a single SELECT DISTINCT on
        doc_id across ALL provided chashes and the handler wraps it as a FLAT
        ``{"tumblers": [tumbler, ...]}`` — it does not group by chash. Returning
        that flat list directly (the pre-fix behaviour) crashed every consumer
        that does ``by_chash.items()`` (``indexer_utils.build_staleness_cache``,
        ``search_engine._attach_doc_ids_from_catalog``, ``mcp/core.py``,
        ``db/embed_migrate.py``, ``commands/collection.py``,
        ``commands/catalog_cmds/doctor.py``) with ``AttributeError: 'list' object
        has no attribute 'items'`` — silently swallowed to a warning, degrading
        every service-mode ``nx index repo`` to a full re-chunk + re-embed.

        Reconstructed here CLIENT-SIDE (no engine change): the flat tumbler list
        names every doc that contains ANY of the requested chashes, so a second
        batched round-trip via :meth:`get_manifests` fetches each of those docs'
        manifest rows and the chash -> doc_id edges are rebuilt by intersecting
        each row's chash against the requested set. Two round-trips total,
        regardless of how many chashes/docs are involved (no N-per-chash calls).

        Mirrors the local implementation's input-form-preserving contract:
        matching is normalized to the 32-char chash prefix (``chash[:32]``,
        RDR-108 D1 natural-id form — matching ``_manifest_row_from_dict``'s
        defensive truncation), but the RETURNED keys preserve whatever form
        (32- or 64-char) the caller passed in ``chashes``. Chashes with no
        manifest entries are omitted from the result, same as local.
        """
        if not chashes:
            return {}
        prefix_to_inputs: dict[str, list[str]] = defaultdict(list)
        for c in chashes:
            if c:
                prefix_to_inputs[c[:32]].append(c)
        if not prefix_to_inputs:
            return {}
        # nexus-h8rf6.12: the FIRST round-trip must send the 32-char
        # prefixes, not the raw ``chashes`` input. ``CatalogRepository
        # .docsForChashes`` (service/src/main/java/dev/nexus/service/db/
        # CatalogRepository.java:1130-1136) does an EXACT-match
        # ``F_CHK_CHASH.in(chashes)`` against ``catalog_document_chunks
        # .chash`` — a 32-char RDR-108 D1 natural-id column — with no
        # server-side normalization, unlike local ``Catalog
        # .docs_for_chashes`` (catalog_writes.py:1145-1147) which
        # normalizes BOTH sides via SQL ``substr(chash,1,32)``. Any
        # caller passing full 64-char ``chunk_text_hash`` values (e.g.
        # ``indexer_utils.build_staleness_cache``, which reads chunk
        # metadata written as ``hashlib.sha256(...).hexdigest()`` —
        # code_indexer.py:396, doc_indexer.py:1048/1136,
        # prose_indexer.py:102/166) got zero matches on every call: the
        # server never raised, it legitimately found no rows, so the
        # h8rf6.3 shape-conformance test (built on manufactured
        # pre-normalized 32-char fixtures) passed while every live
        # service-mode ``nx index repo`` staleness-cache build returned
        # empty. Sending the already-deduped ``prefix_to_inputs`` keys
        # mirrors the local contract exactly (also gets the dedup for
        # free: mixed 32-/64-char input colliding to the same prefix is
        # now one wire entry, not N).
        result = self._post(
            "/manifest/docs_for_chashes", {"chashes": list(prefix_to_inputs.keys())}
        )
        # Handler returns {"tumblers": [tumbler_string, ...]} — flat, not per-chash.
        tumblers = result.get("tumblers", []) if result else []
        if not tumblers:
            return {}
        manifests = self.get_manifests(tumblers)  # {doc_id: [ManifestRow, ...]}
        wanted_prefixes = set(prefix_to_inputs.keys())
        prefix_to_docs: dict[str, list[str]] = defaultdict(list)
        for doc_id, rows in manifests.items():
            for row in rows:
                if row.chash in wanted_prefixes and doc_id not in prefix_to_docs[row.chash]:
                    prefix_to_docs[row.chash].append(doc_id)
        out: dict[str, list[str]] = {}
        for prefix, doc_ids in prefix_to_docs.items():
            for input_form in prefix_to_inputs[prefix]:
                out[input_form] = list(doc_ids)
        return out

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
        self._post("/manifest/write", {"doc_id": doc_id, "rows": self._manifest_rows(chunks)})
        if new_collection or new_chunk_count is not None:
            updates: dict = {}
            if new_collection:              updates["physical_collection"] = new_collection
            if new_chunk_count is not None: updates["chunk_count"] = new_chunk_count
            self._post("/update", {"tumbler": doc_id, **updates})

    def resync_chunk_count_cache(self, doc_id: str) -> None:
        """Recompute ``documents.chunk_count`` from the true manifest row count.

        Calls ``POST /v1/catalog/manifest/resync`` which runs
        ``COUNT(catalog_document_chunks WHERE doc_id=?)`` server-side and
        updates ``documents.chunk_count`` atomically.  Mirrors the local-SQLite
        path in ``catalog_writes.py`` (nexus-zq79).

        Bug nexus-0jq9u: the previous implementation was a literal no-op whose
        docstring falsely claimed Postgres tracks chunk_count automatically.  The
        real recompute (``CatalogRepository.resyncChunkCount``) was wired to no
        HTTP endpoint, leaving service mode with no reconciliation path at all.
        """
        self._post("/manifest/resync", {"doc_id": doc_id})

    # ══════════════════════════════════════════════════════════════════════════
    # STATS / HEALTH
    # ══════════════════════════════════════════════════════════════════════════

    def collection_health_meta(self, collection: str) -> dict:
        """Return ``{last_indexed, orphan_count, stale_source_ratio}`` for *collection*.

        nexus-dsu5z: public service-mode method replacing the guarded
        ``hasattr(cat, '_db')`` path in ``collection_health._default_catalog_stats_fn``.
        Routes to ``GET /v1/catalog/collections/health?collection=<name>``.

        Returns ``{"last_indexed": None, "orphan_count": 0, "stale_source_ratio": None}``
        when the service responds with an empty/absent payload or 404 (collection
        unknown). Non-404 errors (auth, bad-request, 5xx) propagate — they signal
        misconfiguration that must not be masked as a healthy-empty result.

        Return-shape parity (nexus-u26b4): ``stale_source_ratio`` is carried by
        both the wire response (``CatalogRepository.collectionHealthMeta``, the
        catalog-011 PG view) and local ``Catalog.collection_health_meta()`` — this
        method previously reconstructed only ``{last_indexed, orphan_count}`` and
        silently dropped it, leaving ``collection_health.py``'s report always
        rendering the ``—`` placeholder for service-mode collections.
        """
        try:
            result = self._get("/collections/health", collection=collection)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return {
                    "last_indexed": None, "orphan_count": 0,
                    "stale_source_ratio": None,
                }
            raise
        if not result:
            return {
                "last_indexed": None, "orphan_count": 0,
                "stale_source_ratio": None,
            }
        return {
            "last_indexed": result.get("last_indexed"),
            "orphan_count": int(result.get("orphan_count") or 0),
            "stale_source_ratio": result.get("stale_source_ratio"),
        }

    def stats(self) -> dict:
        return self._get("/stats") or {}

    def is_initialized(self, catalog_path: Path | None = None) -> bool:
        """True when the service responds to /stats.

        catalog_path is a local-mode filesystem path with no service-mode meaning;
        accepted for signature conformance, ignored here. Service reachability == initialized.
        """
        try:
            self._get("/stats")
            return True
        except Exception:  # noqa: BLE001 — probe: any failure to reach /stats means not-initialized
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
