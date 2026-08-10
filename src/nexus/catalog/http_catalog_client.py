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
  POST  /v1/catalog/purge-trash         reclaim tombstoned rows {older_than_days, dry_run} (nexus-3ck2g)
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

nexus-f2qvx.3 (mixin-adoption sweep, batch C — one of the two OUTLIERS):
construction, credential/endpoint refresh-on-401, and the HTTP transport
itself are now inherited from
:class:`~nexus.db.t2._refreshable_client.RefreshableHttpStoreMixin` —
``HttpCatalogClient`` no longer bakes a ``self._headers`` dict or a
``httpx.Client(base_url=..., headers=...)`` at construction time. Unlike
every other adopted store, this class already had its OWN internal
``_get``/``_post`` wrapper methods (used by ~100+ call sites throughout this
file) with a DIFFERENT public signature than the mixin's (``_get(path,
**params)`` filtering falsy values + a ``/v1/catalog`` path prefix, vs the
mixin's ``_get(path, params=None)``). Rather than rename every call site,
this class's own ``_get``/``_post`` are kept with their existing signature
and now internally delegate to ``super()._get``/``super()._post`` (the
mixin's self-healing transport) after prefixing the path — so every one of
those ~100+ call sites gets self-heal for free with zero changes to the call
sites themselves. ``HttpCatalogClient`` was NOT on ``RawHandleGuardMixin``
before this adoption and stays that way (it guards raw-handle access via its
own ``_db`` property instead) — adding ``RawHandleGuardMixin`` here would be
new scope, not a mechanical mixin swap.
"""
from __future__ import annotations

import contextlib
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx
import structlog

from nexus.catalog.tumbler import Tumbler
from nexus.catalog.types import CatalogEntry, CatalogLink
from nexus.catalog.catalog_spans import parse_chash_span
from nexus.catalog.types import ManifestRow, _CROSS_PROJECT_OVERRIDE_ENV
from nexus.catalog.collection_name import CollectionName, owner_segment_for_tumbler
from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin
from nexus.errors import CombinedWriteEmbedTimeoutError, IndexRunVerifyRefused

_log = structlog.get_logger(__name__)

DEFAULT_TENANT: str = "default"


def _manifest_row_from_dict(d: dict) -> ManifestRow:
    """Build a typed ``ManifestRow`` from a wire dict (return-type parity, RDR-168).

    The Java ``/manifest/get`` rows carry an extra ``doc_id`` key the dataclass does not
    model; only the seven schema fields are mapped.
    """
    return ManifestRow(
        position=int(d.get("position", 0)),
        # RDR-180: the wire value is authoritative — full 64-hex for rekeyed
        # rows, 32-hex for not-yet-rekeyed legacy rows. Never truncate.
        chash=d.get("chash") or "",
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
#: Page size for register_many POSTs — matches the server's MAX_BATCH_DOC_IDS
#: cap (nexus-9dvqy). ~24 columns/row keeps a full page under PostgreSQL's
#: 32767 bind-parameter ceiling.
_REGISTER_MANY_PAGE = 1000

# nexus-y9t08: the combined write (write_manifest_many's ``chunks=``
# branch) makes the engine run a SYNCHRONOUS server-side embed inside the
# request/transaction — the exact same shape ``HttpVectorClient.upsert_chunks``
# (its predecessor for this workload) passes ``timeout=600`` for
# (``src/nexus/db/http_vector_client.py:1413``, docstring reasoning at
# :837-843: "a 300-chunk CCE (voyage-context-3) upsert batch routinely
# exceeds 120s server-side (embed is synchronous in the request)" — and
# note that docstring's own closing point, that the raise is deliberately
# NOT global because a slow search should still fail fast, which is the
# same reasoning driving the per-request scoping below). The v7.5.0 combined
# write REGRESSED this: HttpCatalogClient's ``__init__`` builds on
# RefreshableHttpStoreMixin without an explicit ``timeout``, so it silently
# inherited the mixin's ``_DEFAULT_TIMEOUT_S`` (30.0s) — a control-plane
# default sized for ordinary catalog calls (registration, linking,
# listing), not a synchronous embed.
#
# SCOPING DECISION (bead nexus-y9t08, option (a) over (b)): this constant
# is applied as a PER-REQUEST override on the chunk-carrying POST only
# (see write_manifest_many's ``page_carries_chunks`` branch below), never
# as a client-wide bump on ``HttpCatalogClient``. A blanket 600s default on
# every catalog call would make a genuinely hung ordinary control-plane
# request (one with no embed at all) take 10 minutes to surface instead of
# 30 seconds — its own defect, and unjustified for the ~100+ other call
# sites through this client that do no embedding.
#
# RETRY DECISION — CORRECTED (nexus-y9t08 round 2; the original text
# below this line, shipped in 127f2dbc, made a FALSE claim about the old
# path and has been struck):
#
# The 127f2dbc version of this comment claimed "the 600s budget matches
# the OLD path's own accepted behavior, under which a ReadTimeout on this
# workload was rare enough in practice to never surface as a retry-storm
# complaint." That is FALSE, verified directly against the predecessor's
# own source: ``HttpVectorClient``'s retry classifier
# (``src/nexus/db/http_vector_client.py:779-784``) catches ONLY
# ``(urllib.error.URLError, ConnectionRefusedError, ConnectionResetError)``
# — its own comment states explicitly "TimeoutError is intentionally NOT
# in this retry classifier (it is not an auto-restart signature); it
# propagates straight to the _get/_post handler". The old path FAILS ONCE
# on a timeout, by deliberate design — it does not retry it "rarely
# enough to not matter"; it never retries it at all.
#
# The retained retry-on-ReadTimeout in 127f2dbc was therefore a NEW
# amplifier introduced in 7.5.0, not a restoration of prior behavior: up
# to 8 POSTs total across the mixin's self-heal layer (one retry) and the
# outer ``nexus.retry._manifest_write_with_retry`` wrapper (up to 4
# attempts), each starting a fresh — and, since the engine has no
# cancellation path, UNCANCELLED — server-side embed on a timeout, firing
# hardest exactly when the engine is already under the memory pressure
# that caused the original incident (nexus-33hpq, nexus-b32rx).
#
# FIX: a ``httpx.ReadTimeout`` on THIS call (the chunk-carrying POST) is
# no longer retried, matching the old path's actual behavior. See
# ``write_manifest_many``'s ``page_carries_chunks`` branch below —
# ``retry_read_timeout=False`` closes the inner self-heal retry
# (``RefreshableHttpStoreMixin._send``), and catching+converting the
# ``httpx.ReadTimeout`` into :class:`~nexus.errors.CombinedWriteEmbedTimeoutError`
# closes the outer ``_manifest_write_with_retry`` retry too (that
# classifier — ``nexus.retry._is_connectivity_error`` — inspects
# exception type/chain only, so a bare re-raised ``httpx.ReadTimeout``
# would still be retried there even after the inner layer stopped).
# Every OTHER exception this call can raise
# (``ConnectionResetError``/``httpx.ConnectError``/etc. — genuine
# connectivity blips, GH #1371) is UNCHANGED at both layers; this
# narrowing applies to ReadTimeout on this ONE call only, never to
# ``nexus.retry``'s general classifier or any other catalog call.
#
# Deeper mitigation (bounded embed concurrency / engine-side admission
# control / cancellation) remains Phase 2 (engine-tag-cut) scope per T2
# ``nexus/assess-local-index-cluster-2026-08-10`` items 6-7 — explicitly
# NOT this fix.
_COMBINED_WRITE_EMBED_TIMEOUT_S = 600.0

#: Page size for update_many POSTs — same MAX_BATCH_DOC_IDS cap as
#: register_many (nexus-xedhp).
_UPDATE_MANY_PAGE = 1000
#: Page size for /manifest/docs_for_chashes POSTs — mirrors the engine's
#: CatalogHandler.MAX_BATCH_DOC_IDS cap on handleDocsForChashes (nexus-uu4b9):
#: the endpoint had NO client-side cap, so an unbounded chash list (a
#: superseded-vector sweep or GC pass over a large collection) went straight
#: into a jOOQ IN toward the 32767 bind-parameter limit and now gets a hard
#: HTTP 400 instead. Paged and unioned client-side, same shape as the other
#: *_MANY_PAGE constants above.
_DOCS_FOR_CHASHES_PAGE = 1000


def _engine_error_detail(exc: httpx.HTTPStatusError) -> str:
    """Best-effort human text from an engine error response.

    Engine handlers reply ``{"error": "<message>"}``; fall back to the raw body
    (then the status line) so a refusal never surfaces as an empty reason.
    """
    with contextlib.suppress(Exception):
        body = exc.response.json()
        if isinstance(body, dict) and body.get("error"):
            return str(body["error"])
    with contextlib.suppress(Exception):
        if text := exc.response.text.strip():
            return text
    return f"HTTP {exc.response.status_code}"


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
        # nexus-9l2lg: the remaining 4 of 8 bib_* columns — the wire dict
        # already carries these (CatalogRepository.docRowFromRecord), this
        # was a pure mapping gap.
        bib_semantic_scholar_id=d.get("bib_semantic_scholar_id") or "",
        bib_openalex_id=d.get("bib_openalex_id") or "",
        bib_doi=d.get("bib_doi") or "",
        bib_enriched_at=d.get("bib_enriched_at") or "",
        # nexus-5xn3k.3 (RUNFENCE): index_state is deliberately NOT coerced
        # with `or ""` — None/absent (a pre-fence engine, or a legacy row
        # that predates catalog-020) must hydrate as None ("unknown"),
        # never as the empty string or any other value that could be
        # mistaken for a real state. The other three fence fields mirror
        # the engine's NOT NULL DEFAULT '' columns, same treatment as
        # alias_of/source_uri above.
        index_state=d.get("index_state"),
        index_content_hash=d.get("index_content_hash") or "",
        index_run_id=d.get("index_run_id") or "",
        index_started_at=d.get("index_started_at") or "",
        # nexus-vw594 F3 (root cause of nexus-biq4x): "index_state" in d
        # distinguishes "key absent" (pre-fence engine — d.get() would
        # also return None here) from "key present, value null" (a
        # fence-aware engine with nothing stamped yet). d.get() alone
        # cannot tell these apart; this dict-membership check is the fix.
        index_state_reported="index_state" in d,
    )


class HttpCatalogClient(RefreshableHttpStoreMixin):
    """Catalog orchestrator drop-in backed by the RDR-152 Java HTTP service.

    Implements the full public API of :class:`~nexus.catalog.catalog.Catalog`
    at the ORCHESTRATOR level.  All calls forward to the Java service at
    ``/v1/catalog/*``.

    Construction, credential/endpoint refresh-on-401, and the underlying
    keep-alive ``httpx.Client`` are inherited from
    :class:`~nexus.db.t2._refreshable_client.RefreshableHttpStoreMixin`
    (nexus-f2qvx.3) — see that class's docstring for the full resolution
    order (env halves -> managed ``service_url``/``service_token`` ->
    supervisor lease -> fail loud) and self-heal contract (re-resolve +
    retry once on a 401 or connection-refused/reset).

    Args:
        base_url: Optional override (e.g. ``"http://127.0.0.1:8765"``).
        tenant:   Tenant header stamped on every request.
        _token:   Token override (used with base_url; resolved config-first
                  otherwise — see the mixin's ``_resolve_token_only``).
    """

    def __init__(
        self,
        base_url: str | None = None,
        tenant: str = DEFAULT_TENANT,
        *,
        _token: str | None = None,
    ) -> None:
        super().__init__(base_url=base_url, tenant=tenant, _token=_token)
        #: nexus-5i864: per-instance owner cache for resolve_path (see
        #: ``_owner_for_resolve_path``). Maps owner tumbler_prefix ->
        #: owner dict. HITS ONLY — misses are never cached, because this
        #: client is a process-lifetime singleton and a pinned miss would
        #: silently zero the auto-linker for any owner registered later
        #: by another process.
        self._resolve_path_owner_cache: dict[str, dict] = {}
        _log.debug("http_catalog_client.init", base_url=self._base_url, tenant=tenant)

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

        RAISES AttributeError, NEVER RuntimeError (nexus-xj744). ``hasattr()``
        only swallows ``AttributeError``; anything else propagates. A
        ``RuntimeError`` here means a caller writing the sanctioned
        ``hasattr(cat, "_db")`` / ``has_raw_access(cat)`` probe would CRASH in
        service mode instead of getting ``False`` and taking the service branch
        — the guard idiom that exists to make such checks safe would become the
        thing that breaks them. ``db/t2/_raw_handle_guard.py`` states this
        contract explicitly; this property was the one place violating it.
        """
        raise AttributeError(
            "catalog._db does not exist: the local SQLite catalog was "
            "deleted (RDR-158 P4, nexus-i711w) and the engine's HTTP "
            "catalog is the only substrate.  "
            "This command path is not yet ported to the public catalog API — "
            "tracked in bead nexus-xnz0o."
        )

    # ── Internal helpers ───────────────────────────────────────────────────────
    #
    # These keep THIS class's own established signature (``_get(path,
    # **params)`` filtering falsy values; ``_post(path, body=None)``), used
    # unchanged by the ~100+ call sites below — nexus-f2qvx.3 routes the
    # actual wire request through the inherited
    # ``RefreshableHttpStoreMixin._get``/``._post`` (self-healing transport)
    # rather than touching ``self._client`` directly, so every call site
    # gets self-heal for free with no signature changes.

    def _get(self, path: str, **params: Any) -> Any:
        filtered = {k: v for k, v in params.items() if v is not None and v != ""}
        return super()._get(f"/v1/catalog{path}", params=filtered)

    def _post(
        self,
        path: str,
        body: dict | None = None,
        *,
        timeout: float | None = None,
        retry_read_timeout: bool = True,
    ) -> Any:
        """``timeout`` (nexus-y9t08): optional per-call override forwarded
        to the mixin's ``_post`` — see that method's docstring. ``None``
        (the default) means every existing call site is byte-identical to
        before this kwarg existed; only ``write_manifest_many``'s
        chunk-carrying (combined-write) branch passes a non-default value.

        ``retry_read_timeout`` (nexus-y9t08 CRITICAL 1 correction):
        forwarded to the mixin's ``_post`` unchanged — default ``True``
        means every existing call site keeps retrying a ``ReadTimeout``
        exactly as before this kwarg existed. Only
        ``write_manifest_many``'s chunk-carrying branch passes ``False``.
        """
        return super()._post(
            f"/v1/catalog{path}", body or {}, timeout=timeout, retry_read_timeout=retry_read_timeout,
        )

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
        ``catalog_collections``). The streaming pipeline buffer is swept via
        its own engine endpoint (``/v1/pipeline/delete_collection``, RDR-186
        .16); the local-mode cascade stays client-side (see
        ``purge_collection_cascade``).
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
        # nexus-5i864: an owner upsert can change owner_type/repo_root —
        # drop the resolve_path owner cache so later resolutions re-read.
        # AFTER the POST: a raising upsert changed nothing to invalidate.
        self._resolve_path_owner_cache.clear()
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
        # nexus-5i864: see register_owner — invalidate after a successful upsert.
        self._resolve_path_owner_cache.clear()
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

    def list_owners_by_type(self, owner_type: str, *, include_deactivated: bool = False) -> list[dict]:
        """Return owners of the given type.

        Backs repos.py:list_repos_dual_with_catalog_roots which previously
        queried ``SELECT repo_root FROM owners WHERE owner_type='repo'``
        directly. Uses POST /v1/catalog/owners/by_type endpoint (nexus-qnp5s).

        ``include_deactivated`` (nexus-cw262): default False excludes
        deactivated owners -- this is the exact read path the 7kl32 census
        and doctor's git-hooks dead-owner attribution both use, so the
        default-exclusion here is what makes deactivation actually stop the
        re-surfacing debris. Pass True for the audit escape hatch.
        """
        result = self._post("/owners/by_type", {
            "owner_type": owner_type,
            "include_deactivated": include_deactivated,
        })
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
        if "count" not in result:
            # nexus-znwc2: `count` feeds the migration P3 validation gate
            # (manifest_check) — a stripped field defaulting to 0 would be a
            # vacuous PASS. Fail closed, matching relation_counts' model.
            raise RuntimeError(
                "manifest/orphans response carried no `count` field — cannot "
                "verify orphan state; refusing a false-clean zero "
                f"(response keys: {sorted(result)})"
            )
        return {
            "dim": int(result.get("dim", dim)),
            "count": int(result["count"]),
            "orphans": result.get("orphans", []),
        }

    def chash_conformance_report(self, dim: int) -> dict:
        """Per-table chash width-conformance report for *dim* (RDR-180, bead
        nexus-du2dw): the engine-route counterpart to the LOCAL-ONLY
        ``nexus_diag`` psql probe (``nexus.db.diag_connection`` — shells a
        local psql at 127.0.0.1, nexus-y3wuu), for managed/cloud installs
        with no direct substrate access.

        Returns ``{"dim": d, "tables": [{"table_name", "total",
        "non_conformant", "sample_chashes"}, ...]}``. One row per covered
        table: ``chunks_<dim>`` and ``catalog_document_chunks`` (filtered to
        *dim*'s model-token collections — same IN-list routing caveat as
        :meth:`manifest_orphans`: a collection whose model token is outside
        every dim's IN-list is invisible here). ``non_conformant`` counts
        ``octet_length(chash) <> 32`` (the era-safe RDR-180 predicate,
        ``nexus.db.chash_tables``); ``sample_chashes`` is hex-encoded,
        capped at 20 by the server-side function.

        Tenant-scoped (the stored function is SECURITY INVOKER, counting
        under the request tenant's RLS GUC) — deliberately NOT the
        cross-tenant BYPASSRLS view the local install-binary gate reads.
        This gives a managed-mode tenant visibility into their OWN data's
        conformance; it is not a substitute for the local gate's whole-store
        decision. ``dim`` must be one of 384/768/1024.
        """
        if dim not in self._MANIFEST_DIMS:
            raise ValueError(
                f"dim must be one of {self._MANIFEST_DIMS}, got {dim!r}"
            )
        result = self._get("/chash/conformance", dim=dim)
        result = result or {}
        if "tables" not in result:
            # Same fail-closed contract as manifest_orphans' `count` field —
            # a stripped `tables` key defaulting to [] would read as a false
            # clean-store zero rather than a broken response.
            raise RuntimeError(
                "chash/conformance response carried no `tables` field — "
                "cannot verify chash conformance; refusing a false-clean "
                f"empty report (response keys: {sorted(result)})"
            )
        return {
            "dim": int(result.get("dim", dim)),
            "tables": result.get("tables", []),
        }

    # ══════════════════════════════════════════════════════════════════════
    # INDEX RUN FENCE (RUNFENCE, nexus-5xn3k.3) — design memo §3.3/§3.4
    #
    # Engine-floor tolerance (memo §6, the bead's explicit ship-order
    # ruling): this client ships WITHOUT waiting on REQUIRED_ENGINE_VERSION.
    # A pre-fence engine (built before catalog-020/021 landed) 404s every
    # route in this section — CatalogHandler's switch has no matching
    # `case`, so it falls to the generic `default -> 404`. The floor bump
    # rides the NEXT engine tag per AGENTS.md, never a tag cut for this
    # arc alone.
    #
    # begin/complete/fail are advisory WRITES with no meaningful fallback
    # value a caller could act on, so a 404 here is swallowed (logged at
    # WARNING, method returns normally) rather than raised — an old engine
    # simply never records intent/completion, and indexing must proceed
    # unaffected. manifest_verify/manifest_verify_all are READS whose
    # result other callers (the doctor sweep, `nx catalog verify`) need to
    # tell apart from a real "verified clean" 200, so they raise like any
    # other read method; the ONE caller that must fail open on an
    # unreachable verify (_manifest_is_fully_present, doc_indexer.py) owns
    # that fail-open+WARNING contract itself.
    # ══════════════════════════════════════════════════════════════════════

    def begin_index_run_many(
        self, docs: list[dict], collection: str,
    ) -> dict:
        """POST /v1/catalog/index-run/begin-many — batch ``index_state=
        'indexing'`` stamp for N documents in ONE round trip (nexus-vw594
        F1). *docs* is ``[{"doc_id": ..., "content_hash": ..., "run_id":
        ...}, ...]`` — same idempotent, NOT-a-lock semantics as
        :meth:`begin_index_run` (memo §3.5 T0), batched so the ChunkBatcher
        flush-grain repo path pays ONE round trip per FLUSH instead of one
        per FILE (the ``indexer.py`` ``:3631`` cost objection this closes).

        Returns ``{}`` on a 404 (pre-fence engine, engine-floor-tolerated:
        logged at WARNING) — same sentinel-vs-empty-success distinction as
        :meth:`begin_index_run`'s single-doc 404 handling. Any other
        transport failure propagates; the caller (:func:`nexus.doc_indexer.
        _fence_begin_many`) wraps this in its own advisory fail-open catch.
        """
        try:
            return self._post("/index-run/begin-many", {
                "docs": docs, "collection": collection,
            }) or {}
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                _log.warning("index_run_begin_many_engine_floor", doc_count=len(docs))
                return {}
            raise

    def begin_index_run(
        self, doc_id: str, content_hash: str, run_id: str, collection: str,
    ) -> None:
        """POST /v1/catalog/index-run/begin — stamp ``index_state='indexing'``
        BEFORE the first chunk upsert (memo §3.5 T0: the fence is committed
        before the first byte of content and cleared only after the last).

        Idempotent; NOT a lock (nexus-lcmbp non-goal, memo §5) — a retry or a
        second concurrent run simply re-stamps the same shape.
        """
        try:
            self._post("/index-run/begin", {
                "doc_id": doc_id, "content_hash": content_hash,
                "run_id": run_id, "collection": collection,
            })
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                _log.warning("index_run_begin_engine_floor", doc_id=doc_id)
                return
            raise

    def complete_index_run(
        self, doc_id: str, content_hash: str, chunk_count: int,
    ) -> dict | None:
        """POST /v1/catalog/index-run/complete — the load-bearing FAIL-CLOSED
        verify-then-stamp (memo §3.3). Returns
        ``{referenced, present, missing, flagged}`` on success (200).

        Raises :class:`~nexus.errors.IndexRunVerifyRefused` on the engine's
        409 refusal (``missing > 0`` or ``referenced != chunk_count`` — the
        document is not actually whole in T3, so ``index_state`` was left
        untouched rather than stamped ``'complete'``).

        Returns ``None`` — NOT ``{}`` — on a 404 (pre-fence engine,
        engine-floor-tolerated: logged at WARNING). The two are
        deliberately distinguishable: ``None`` means "this call never
        reached a fence-aware engine at all" (the route doesn't exist),
        while ``{}`` would mean "the engine answered with an empty body" —
        a real, if degenerate, 200 response. A caller bracketing an index
        run (nexus-5xn3k.4) MUST be able to tell those apart: the first
        means the fence recorded nothing and there is nothing to act on;
        conflating it with an empty success dict risks reading absent
        fields as zero counts instead of "unknown."
        """
        try:
            result = self._post("/index-run/complete", {
                "doc_id": doc_id, "content_hash": content_hash,
                "chunk_count": chunk_count,
            })
            return result or {}
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status == 404:
                _log.warning("index_run_complete_engine_floor", doc_id=doc_id)
                return None
            if status == 409:
                body: dict = {}
                with contextlib.suppress(Exception):
                    body = exc.response.json() or {}
                raw_referenced = body.get("referenced")
                raw_present = body.get("present")
                raw_missing = body.get("missing")
                raw_chunk_count = body.get("chunk_count")
                raise IndexRunVerifyRefused(
                    doc_id=str(body.get("doc_id", doc_id)),
                    referenced=int(raw_referenced) if raw_referenced is not None else 0,
                    present=int(raw_present) if raw_present is not None else 0,
                    missing=int(raw_missing) if raw_missing is not None else 0,
                    # nexus-5xn3k.3 review: `or chunk_count` is a falsy-0 trap
                    # — a genuine server-reported chunk_count=0 would fall
                    # through to the caller's claimed value instead of the
                    # engine's actual (zero) count. Explicit presence check.
                    chunk_count=int(raw_chunk_count) if raw_chunk_count is not None else chunk_count,
                    server_detail=body.get("error") or _engine_error_detail(exc),
                ) from exc
            raise

    def fail_index_run(self, doc_id: str, error: str) -> None:
        """POST /v1/catalog/index-run/fail — stamps ``index_state='failed'``.

        A 404 (pre-fence engine) is engine-floor-tolerated: logged at
        WARNING, returns normally.
        """
        try:
            self._post("/index-run/fail", {"doc_id": doc_id, "error": error})
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                _log.warning("index_run_fail_engine_floor", doc_id=doc_id)
                return
            raise

    def manifest_verify(self, doc_id: str) -> dict:
        """GET /v1/catalog/manifest/verify?doc_id=X — one-document
        ``{referenced, present, missing}`` (memo §3.2's SQL primitive,
        narrowed to a single document).

        This is a READ: unlike begin/complete/fail above, a 404 or any
        other transport failure PROPAGATES — callers that need to
        distinguish "the engine doesn't support this route yet" from "the
        engine says the document is clean" (the doctor sweep, `nx catalog
        verify`) must be able to tell the two apart. The ONE caller that
        must fail open on ANY failure here
        (:func:`nexus.doc_indexer._manifest_is_fully_present`) implements
        that fail-open+WARNING contract itself, around this call.

        CALLER TRAP: an empty *doc_id* is silently DROPPED by ``_get``'s
        falsy-param filter (``{k: v for k, v in params.items() if v is not
        None and v != ""}``) — the request goes out with NO ``doc_id`` query
        param at all, which the engine's ``doc_id.isBlank()`` guard answers
        with a 400, not a per-document result. This method does not guard
        against it; callers must pass a non-empty *doc_id* (the only current
        caller, ``_manifest_is_fully_present``, already short-circuits on an
        empty ``doc_id`` before ever reaching here).
        """
        result = self._get("/manifest/verify", doc_id=doc_id)
        return result or {}

    def manifest_verify_all(self) -> dict:
        """GET /v1/catalog/manifest/verify_all — every live document in the
        tenant, grouped by collection: ``{"collections": [...], "count": n}``
        (nexus-ac4id part 2 — the doctor sweep primitive; supersedes the
        client-side per-collection T3 paging that check used to do).

        A READ; propagates on failure like :meth:`manifest_verify` — the
        doctor's own non-vacuity discipline (``checked == 0`` renders
        SKIPPED, never a false-clean pass) needs to see a 404 as a real
        error, not a silently-empty "0 collections checked."
        """
        result = self._get("/manifest/verify_all")
        return result or {}

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

    def list_owners(self, *, include_deactivated: bool = False) -> list[dict]:
        """Return owners for this tenant.

        Backs commands/catalog.py owners_cmd which previously queried
        ``SELECT tumbler_prefix, name, owner_type, repo_hash, description FROM owners``
        directly.  Uses GET /v1/catalog/owners/list (nexus-xnz0o).
        Returns list of dicts with keys: tumbler_prefix, name, owner_type,
        repo_hash, description, repo_root, head_hash.

        ``include_deactivated`` (nexus-cw262): default False excludes owners
        the 7kl32 GC mutation arm has deactivated -- that exclusion is the
        entire point of the deactivate route (dead owners stop appearing in
        doctor's git-hooks walk and the census). Pass True for the audit
        escape hatch; the returned dicts then also carry ``deactivated_at``
        (ISO timestamp string, or ``None`` for still-active rows).
        """
        # nexus-cw262: `_get` drops any param whose value is falsy/empty
        # (`params.items() if v is not None and v != ""`), and a bare Python
        # `False` would otherwise round-trip as the query string "False" --
        # not the lowercase "true" CatalogHandler.handleOwnerList checks for.
        # Omitting the param entirely on the (far more common) False path
        # keeps every existing plain `nx catalog owners` call unaffected.
        params = {"include_deactivated": "true"} if include_deactivated else {}
        result = self._get("/owners/list", **params)
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

    def orphaned_links(self) -> list[dict]:
        """Return catalog_links rows whose from_tumbler or to_tumbler
        resolves to no document (nexus-ysrwi, GH #1419 issue 7).

        ``catalog_links`` carries a PK and a UNIQUE constraint but NO
        foreign key to ``catalog_documents`` (catalog-001-baseline.xml), so
        a link survives when its referenced document is hard-deleted. The
        only production hard-delete of document rows is
        ``CatalogRepository.deleteCollectionTxn``'s per-collection
        ``catalog_documents`` delete (called from :meth:`delete_collection`)
        — it has no corresponding ``catalog_links`` cleanup step, so a
        cross-collection link whose endpoint's collection gets deleted
        dangles forever. Closing that write-time gap needs an engine change
        (a new step in that transaction) — this method is the DETECTION
        half only.

        Uses GET /v1/catalog/links/orphaned (v0.1.55,
        ``CatalogRepository.orphanedLinks`` / ``CatalogHandler
        .handleLinksOrphaned``). Each dict carries ``id``, ``from_tumbler``,
        ``to_tumbler``, ``link_type``, ``created_by``, and ``side``
        (``"from"``/``"to"``/``"both"``) naming which endpoint is missing.
        """
        result = self._get("/links/orphaned")
        # Shape-guard the response. A truthy NON-dict (a bare JSON array, say)
        # would make .get() raise AttributeError, which is NOT in doctor.py's
        # `except (httpx.HTTPError, RuntimeError)` -- so the caller would crash
        # with a raw traceback instead of the UNKNOWN/exit(2) contract that this
        # check and its three same-day siblings exist to guarantee. Raise the
        # type the caller already handles, and say what arrived.
        if result and not isinstance(result, dict):
            raise RuntimeError(
                f"/links/orphaned returned {type(result).__name__}, expected an "
                f"object with a 'links' key -- the engine's wire shape changed"
            )
        return (result or {}).get("links", [])

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
        with_created: bool = False,
        **kwargs: Any,
    ) -> Tumbler | tuple[Tumbler, bool]:
        """Register a document; returns the server-assigned tumbler.

        Signature matches :meth:`nexus.catalog.catalog.Catalog.register`
        exactly — positional ``owner`` + ``title``, then keyword-only.
        No bib fields: CatalogEntry / Catalog.register() have none.

        Uses POST /v1/catalog/doc/register for server-side atomic tumbler
        assignment via catalog_owners.next_seq (SELECT FOR UPDATE).

        nexus-e7cys: the engine enforces the nexus-3e4s cross-project
        containment guard on an explicit ``source_uri`` (rejecting one that
        resolves outside a "repo" owner's ``repo_root``). The LOCAL arm's
        escape hatch was the ``NEXUS_CATALOG_ALLOW_CROSS_PROJECT=1``
        environment variable read directly by the process doing the
        registering; the engine has no access to the CLIENT's environment,
        so this method reads it here and forwards ``allow_cross_project``
        on the wire — the honest way an env-var escape hatch survives a
        client/server split. An explicit ``allow_cross_project`` kwarg
        always wins over the env-var default.

        nexus-vfef0: pass ``with_created=True`` to additionally receive the
        engine's per-call ``created`` signal, returning ``(tumbler,
        created)`` instead of a bare ``Tumbler``. ``created`` is ``True``
        only when THIS call's INSERT minted the row — ``False`` for an
        idempotency-leg hit (the (owner, source_uri)/(owner, file_path)
        already existed) or a concurrent first-put race loser (a
        simultaneous caller's INSERT won the race; the engine's ON-CONFLICT
        backstop handed this call back the WINNER's tumbler instead of
        minting a duplicate). Older engines that predate this signal omit
        the wire field entirely; its absence is treated as ``created=True``
        — the historical assumption every caller ignoring this parameter
        already makes. Default ``False`` keeps the return type/behavior
        exactly as before for every other call site; this kwarg exists so
        ``with_created=True`` can ride the SAME whitelisted ``register``
        RPC name through :class:`nexus.catalog.factory._ServiceCatalogWriter`
        rather than requiring a second, unwhitelisted method name.
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
        if os.environ.get(_CROSS_PROJECT_OVERRIDE_ENV) == "1":
            payload["allow_cross_project"] = True
        payload.update(kwargs)
        result = self._post("/doc/register", payload)
        tumbler = Tumbler.parse(result["tumbler"])
        if with_created:
            return tumbler, bool(result.get("created", True))
        return tumbler

    def register_many(
        self, owner: Tumbler | str, docs: list[dict]
    ) -> list[Tumbler]:
        """Batch-register documents; returns tumblers aligned 1:1 with *docs*.

        nexus-9dvqy (duoak.11 sink #2): replaces the per-file ``register()``
        loop in the indexer's catalog hook. The single-doc path pays one
        ``SELECT next_seq FOR UPDATE`` WAN round-trip per file — 2,133 serial
        calls (333s) on a --force index, all queued on the same owner lock.
        ``POST /doc/register_many`` claims the whole contiguous tumbler block
        under one lock and inserts every new row in one multi-row INSERT.

        Each doc dict carries the same keyword fields as :meth:`register`
        (``title``, ``content_type``, ``file_path``, ``physical_collection``,
        ``head_hash``, ``meta``, ``source_mtime``, ``source_uri`` …). Docs are
        paged at :data:`_REGISTER_MANY_PAGE` to stay under the server's
        MAX_BATCH_DOC_IDS cap. A page that fails as a whole falls back to
        per-doc :meth:`register`, preserving the per-file failure isolation the
        caller relies on (a single bad doc must not sink the rest of the run).
        """
        if not docs:
            return []
        # nexus-e7cys: same env-var-to-wire-field forwarding as register()
        # above; an explicit per-doc allow_cross_project always wins.
        if os.environ.get(_CROSS_PROJECT_OVERRIDE_ENV) == "1":
            docs = [{"allow_cross_project": True, **d} for d in docs]
        out: list[Tumbler] = []
        for start in range(0, len(docs), _REGISTER_MANY_PAGE):
            page = docs[start : start + _REGISTER_MANY_PAGE]
            try:
                result = self._post(
                    "/doc/register_many",
                    {"owner_prefix": str(owner), "docs": page},
                )
                tumblers = (result or {}).get("tumblers", [])
                if len(tumblers) != len(page):
                    raise ValueError(
                        f"register_many returned {len(tumblers)} tumblers "
                        f"for {len(page)} docs"
                    )
                out.extend(Tumbler.parse(t) for t in tumblers)
            except Exception:  # noqa: BLE001 — page failed; fall back to resilient per-doc register
                _log.warning(
                    "register_many_page_failed_falling_back_per_doc",
                    owner=str(owner),
                    page_size=len(page),
                    exc_info=True,
                )
                for d in page:
                    title = d.get("title", "")
                    rest = {k: v for k, v in d.items() if k != "title"}
                    out.append(self.register(owner, title, **rest))
        return out

    def resolve(
        self, tumbler: Tumbler | str, *, follow_alias: bool = True
    ) -> CatalogEntry | None:
        try:
            # nexus-fguo5: this declared follow_alias but never sent it —
            # the engine's handleShow (nexus-ekaxn) defaults follow_alias to
            # FALSE for wire back-compat, so every client call silently got
            # the identity (non-alias-following) lookup regardless of what
            # was passed here.
            result = self._get(
                "/show", tumbler=str(tumbler), follow_alias=follow_alias
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        if not result or not result.get("tumbler"):
            return None
        return _to_entry(result)

    def update(self, tumbler: Tumbler | str, **fields: Any) -> None:
        self._post("/update", {"tumbler": str(tumbler), **fields})

    def update_many(self, updates: list[dict]) -> list[int]:
        """Batch-update N documents' fields; returns per-entry update counts
        aligned 1:1 with *updates* (nexus-xedhp, duoak.11 follow-up).

        Each dict carries a ``"tumbler"`` key plus the same keyword fields
        as :meth:`update` (``head_hash``, ``physical_collection``, ``meta``,
        ``source_mtime``, ...). Replaces the per-changed-doc ``update()``
        loop in the indexer's catalog hook Pass 1 — a HEAD bump (any new git
        commit) flips every indexed doc's stored ``head_hash`` to "changed",
        so a warm re-index without this batched path pays one serial WAN
        round trip per file (measured: 175.5s / 1718 files, ~102ms/file).

        Paged at :data:`_UPDATE_MANY_PAGE` to stay under the server's
        MAX_BATCH_DOC_IDS cap. A page that fails as a whole falls back to
        per-doc :meth:`update`, preserving the per-file failure isolation
        the caller relies on (mirrors :meth:`register_many`).
        """
        if not updates:
            return []
        out: list[int] = []
        for start in range(0, len(updates), _UPDATE_MANY_PAGE):
            page = updates[start : start + _UPDATE_MANY_PAGE]
            try:
                result = self._post("/update_many", {"updates": page})
                counts = (result or {}).get("updated", [])
                if len(counts) != len(page):
                    raise ValueError(
                        f"update_many returned {len(counts)} counts "
                        f"for {len(page)} updates"
                    )
                out.extend(int(c) for c in counts)
            except Exception:  # noqa: BLE001 — page failed; fall back to resilient per-doc update
                _log.warning(
                    "update_many_page_failed_falling_back_per_doc",
                    page_size=len(page),
                    exc_info=True,
                )
                for d in page:
                    tumbler = d.get("tumbler", "")
                    rest = {k: v for k, v in d.items() if k != "tumbler"}
                    try:
                        self.update(tumbler, **rest)
                        out.append(1)
                    except Exception:  # noqa: BLE001 — per-doc fallback failure isolation (mirrors register_many's per-doc try/except upstream in the indexer)
                        out.append(0)
        return out

    def delete_document(self, tumbler: Tumbler | str) -> bool:
        result = self._post("/delete", {"tumbler": str(tumbler)})
        return bool(result.get("deleted", 0) > 0 if result else False)

    def deactivate_owner(self, tumbler_prefix: str) -> bool:
        """Soft-delete an owner (nexus-cw262: the 7kl32 dead-owner GC
        mutation arm's engine call). Reversible -- see :meth:`reactivate_owner`
        and CatalogRepository.upsertOwner's automatic clear-on-reregister.
        Idempotent: a second call on an already-deactivated (or never-
        existent) prefix returns False, mirroring :meth:`delete_document`'s
        idempotent-zero contract.
        """
        result = self._post("/owners/deactivate", {"tumbler_prefix": str(tumbler_prefix)})
        return bool(result.get("deactivated", 0) > 0 if result else False)

    def reactivate_owner(self, tumbler_prefix: str) -> bool:
        """Clear an owner's deactivated flag (nexus-cw262). The explicit
        manual-correction path; a live re-registration through
        :meth:`register_owner` / ``ensure_owner_for_repo`` already does this
        automatically. Idempotent: already-active returns False.
        """
        result = self._post("/owners/reactivate", {"tumbler_prefix": str(tumbler_prefix)})
        return bool(result.get("reactivated", 0) > 0 if result else False)

    def delete_many(self, tumblers: list[Tumbler | str]) -> set[str]:
        """Batch-tombstone N documents; returns the subset of *tumblers*
        (as strings) that were actually tombstoned (nexus-xedhp: completes
        the update_many/register_many/delete_many batch trio).

        Already-deleted or non-existent tumblers are silently excluded from
        the result, same idempotent semantics as :meth:`delete_document`.
        Paged at :data:`_UPDATE_MANY_PAGE` to stay under the server's
        MAX_BATCH_DOC_IDS cap. A page that fails as a whole falls back to
        per-doc :meth:`delete_document`, preserving per-file failure
        isolation (mirrors :meth:`register_many`).
        """
        if not tumblers:
            return set()
        out: set[str] = set()
        str_tumblers = [str(t) for t in tumblers]
        for start in range(0, len(str_tumblers), _UPDATE_MANY_PAGE):
            page = str_tumblers[start : start + _UPDATE_MANY_PAGE]
            try:
                result = self._post("/delete_many", {"tumblers": page})
                out.update((result or {}).get("deleted", []))
            except Exception:  # noqa: BLE001 — page failed; fall back to resilient per-doc delete
                _log.warning(
                    "delete_many_page_failed_falling_back_per_doc",
                    page_size=len(page),
                    exc_info=True,
                )
                for t in page:
                    try:
                        if self.delete_document(t):
                            out.add(t)
                    except Exception:  # noqa: BLE001 — per-doc fallback failure isolation (mirrors register_many's per-doc try/except upstream)
                        pass
        return out

    def purge_trash(self, older_than_days: int = 30, *, dry_run: bool = True) -> dict:
        """POST /v1/catalog/purge-trash — reclaim tombstoned catalog rows and
        their manifest-orphaned ``chunks_<dim>`` rows via the engine's
        ``nexus.purge_trash(interval)`` sweep (nexus-3ck2g).

        Delete tombstones the catalog row but deliberately leaves the
        ``document_chunks`` manifest and T3 chunk rows in place (preserving
        the manual-restore path, nexus-xavu7); this is the caller that
        physically reclaims them.

        CAVEAT (nexus-8j1zx fix round): the reclaim this method performs is
        NOT age-gated the way "once the retention window has passed" might
        suggest. The underlying ``nexus.purge_trash`` chunk sweep (Steps
        1-3) runs on EVERY currently-tombstoned document on the very next
        ``dry_run=False`` call, regardless of how recently it was
        tombstoned — only the catalog row's physical delete (Step 4, the
        ``documents_purged`` count) honors ``older_than_days``. So the
        manual-restore path (nexus-xavu7) stays open only until the NEXT
        ``dry_run=False`` call anywhere in the tenant, not until this
        document individually ages past the threshold.

        Wire contract (LOCKED, design of record T1 scratch 2fbc12df): POST
        body ``{"older_than_days": int, "dry_run": bool}``; the engine's
        JSON response — ``documents_purged`` (age-gated) plus per-dim
        ``chunks_<dim>_stranded`` counts (age-INDEPENDENT, see the CAVEAT
        above), plus the echoed ``dry_run`` flag — is returned to the
        caller verbatim as a ``dict``; this client does not interpret or
        reshape it. ``dry_run=True`` (the default) computes the same
        counts as a preview WITHOUT deleting anything; ``dry_run=False``
        performs the real sweep, scoped to the request tenant.

        ``dry_run=False`` responses from a nexus-ff85q-or-newer engine carry
        one ADDITIONAL key, ``documents_eligible``: the age-gated population
        the engine measured in the same transaction as the purge.
        ``documents_purged < documents_eligible`` means the purge took a
        strict subset of its own reported population — the CLI verb treats
        that as an error (see ``catalog_cmds.purge_trash``). Older engines
        omit the key entirely; verbatim passthrough means callers must treat
        it as optional, never assume it.

        A pre-nexus-3ck2g engine has no matching route for this call and
        answers 404 — this method does NOT swallow that (unlike the
        RUNFENCE advisory-write methods above); it propagates the raw
        ``httpx.HTTPStatusError`` like any other read/write call in this
        class, so the CLI verb can tell "engine too old" apart from "engine
        answered."
        """
        return self._post("/purge-trash", {
            "older_than_days": older_than_days,
            "dry_run": dry_run,
        }) or {}

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
            # nexus-xoimv: the service's content_type branch (CatalogHandler.handleList ->
            # documentsByContentType) now HONORS an explicit limit/offset — the accept-
            # and-drop behaviour nexus-pwclh documented here is fixed. But this call
            # deliberately sends neither: omitting them is the documented "absent from
            # the query string" contract, which stays UNBOUNDED on every filter branch
            # (same as before xoimv, now by design instead of by omission) — so a single
            # request still returns ALL matching rows in one shot and a pagination loop
            # here would just re-fetch the same full set every page and never terminate.
            return self._docs_from(self._get("/list", content_type=content_type))
        # Unfiltered: the service always respects limit/offset here (listDocuments
        # defaults limit to 200 whether or not the caller sends one — unlike the filter
        # branches above, which stay unbounded when limit is absent, per nexus-xoimv), so
        # paginate exhaustively rather than silently capping — a hardcoded cap would
        # truncate large catalogs with no error.
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
        """TUMBLER-only lookup, collapsed into :meth:`resolve` (nexus-5axey).

        Despite the name, ``doc_id`` here MUST be a tumbler — the settled
        wji11 contract is that tumbler is the only document identity, so a
        chash cannot resolve through this method (it will simply mismatch,
        same as any other malformed tumbler string). Callers with a
        content-chash (T3 chunk natural id, e.g. a store_put's ``meta.doc_id``)
        want :func:`nexus.catalog.store_hook.resolve_knowledge_doc_for_chash`
        (backed by :meth:`docs_for_chashes`) instead — the class of bug this
        distinction exists to prevent (dedup/delete-path/reap silently
        mismatching every chash they were passed) is nexus-5axey.

        Kept as a thin alias rather than removed: ``commands/collection.py``
        and ``search_engine.py`` still call it with a genuine tumbler.
        """
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
        """Resolve *tumbler* to its canonical (alias-followed) target.

        nexus-fguo5: this was an accidental identity function — it always
        called ``resolve(follow_alias=True)``, but ``resolve()`` never wired
        ``follow_alias`` onto the wire, so the server's default-FALSE
        ``/show`` handler ignored it and echoed the input tumbler back
        unresolved. Now that ``resolve()`` actually sends the param, the
        engine's ``alias_of`` chain-walk (``CatalogRepository
        .resolveAliasTarget``, capped at its own 16-hop limit) runs
        server-side and the returned entry's ``tumbler`` is the resolved
        target. ``max_hops`` is accepted for signature parity with the
        (never-shipped) local arm; the actual hop cap lives server-side.
        """
        entry = self.resolve(tumbler, follow_alias=True)
        if entry:
            return entry.tumbler
        return Tumbler.parse(str(tumbler))

    def resolve_path(self, tumbler: Tumbler | str) -> Path | None:
        """Return the absolute filesystem path for a document, or ``None``.

        nexus-5i864: ports the deleted local ``Catalog.resolve_path``
        resolution order (catalog_docs.py, RDR-137 P3.6 form) into the
        service client. The pre-fix client returned the STORED
        ``file_path`` verbatim — for the relative paths the catalog hook
        always writes, that re-anchored on the caller's CWD (the
        nexus-3e4s class) and silently zeroed / mis-fed the auto-linker.

        Resolution order:
          1. resolve(tumbler); no entry or empty file_path -> None
          2. owner lookup by ``tumbler.owner_address()`` (cached per
             client instance — the link generator calls this in loops)
          3. owner missing or ``owner_type == "curator"`` -> None
             (curator-owned PDFs / standalone docs are not resolvable)
          4. absolute ``file_path`` -> returned as-is
          5. non-empty ``owner.repo_root`` -> ``repo_root / file_path``
          6. else DEBUG ``catalog_resolve_path_legacy_owner_missing_repo_root``
             and None (legacy owner healed by re-running ``nx index repo``)
        """
        tum = tumbler if isinstance(tumbler, Tumbler) else Tumbler.parse(tumbler)
        entry = self.resolve(tum)
        if not entry or not entry.file_path:
            return None

        owner_prefix = str(tum.owner_address())
        owner = self._owner_for_resolve_path(owner_prefix)
        if owner is None:
            return None
        if owner.get("owner_type") == "curator":
            return None

        fp = Path(entry.file_path)
        if fp.is_absolute():
            return fp

        repo_root = owner.get("repo_root") or ""
        if repo_root:
            return Path(repo_root) / entry.file_path

        _log.debug(
            "catalog_resolve_path_legacy_owner_missing_repo_root",
            tumbler=str(tum),
            owner_prefix=owner_prefix,
            repo_hash=owner.get("repo_hash", ""),
            file_path=entry.file_path,
            hint="re-run 'nx index repo' on the source repo to backfill repo_root",
        )
        return None

    def _owner_for_resolve_path(self, owner_prefix: str) -> dict | None:
        """Cached owner lookup for :meth:`resolve_path` (nexus-5i864).

        ``resolve_path`` is driven in loops by the link generator
        (link_generator.py) where consecutive tumblers overwhelmingly
        share an owner; caching per client instance turns N owner
        round-trips into one per distinct owner.

        POSITIVE RESULTS ONLY — a miss is never cached. This client is a
        PROCESS-LIFETIME SINGLETON (catalog/factory.py
        ``_get_shared_service_catalog_client``, nexus-53x7s), so caching
        a ``None`` would pin "this owner does not exist" for the life of
        the process: an owner registered afterwards by ANOTHER process
        (a separate ``nx index repo`` run, a daemon) would never be
        observed here, and ``resolve_path`` would keep returning None —
        silently zeroing the auto-linker for that repo indefinitely.
        That is the very failure shape nexus-5i864 exists to remove, so
        the miss path re-queries every call. A found owner is safe to
        cache: ``owner_type`` / ``repo_root`` change only on
        re-registration, which clears this cache (:meth:`register_owner`,
        :meth:`ensure_owner_for_repo`).
        """
        cache = self._resolve_path_owner_cache
        cached = cache.get(owner_prefix)
        if cached is not None:
            return cached
        owner = self.get_owner_by_prefix(owner_prefix)
        if owner is not None:
            cache[owner_prefix] = owner
        return owner

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

        CAVEAT (review 2026-07-19, pre-existing): the returned
        ``chunk_hash`` echoes the CALLER's requested width — a legacy
        32-hex span that resolves via the engine's alias route is NOT
        rewritten to the canonical 64-hex identity here (unlike
        ``catalog_spans.resolve_chash_globally``, which rewrites). Both
        current callers only null-check the result; a future caller that
        trusts ``chunk_hash`` as canonical must rewrite it itself or go
        through ``resolve_chash``.
        """
        if not span.startswith("chash:"):
            return None
        try:
            hex_chash, char_range = parse_chash_span(span)
        except ValueError:
            return None
        try:
            # RDR-180: pass the citation's own width through — 64-hex resolves
            # directly; a legacy 32-hex reference rides the engine's alias
            # route. Truncating a canonical citation to 32 would force EVERY
            # resolution through the legacy path and dangle for post-cutover
            # content (which has no alias row).
            result = self._get(
                "/resolve_span",
                span_chash=hex_chash,
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

        On a miss, retries once through the alias-aware route (nexus-84tr4).
        ``/resolve_chash`` has no alias fallback — that lives on
        ``/v1/chash/lookup`` — so this used to MISS on a legacy 32-hex ref
        while ``resolve_chash_globally`` RESOLVED the same ref, two functions
        disagreeing about the same identifier space. Harmless on a fully
        rekeyed store, where every caller reads 64-hex manifest chashes, but a
        user pasting a legacy citation into an ``nx doc`` path, or any
        un-rekeyed or partially-rekeyed store, got a silent MISS.

        The retry costs one extra request only on a miss, and only when the
        alias route reports a different canonical identity. The returned
        ``chash``/``chunk_hash`` is then the CANONICAL hex, not the legacy ref
        that was passed in, so a caller comparing against a 64-char citation
        matches — the same rewrite the citation resolver performs.
        """
        try:
            hex_chash, char_range = parse_chash_span(chash)
        except ValueError:
            return None
        # RDR-180: full-width pass-through (see resolve_span's width note).
        params: dict[str, Any] = {"chash": hex_chash}
        if prefer_collection:
            params["prefer_collection"] = prefer_collection
        result = self._resolve_chash_once(params)
        if not result:
            canonical = self._canonical_chash(hex_chash)
            if canonical and canonical != hex_chash:
                params["chash"] = canonical
                result = self._resolve_chash_once(params)
                if result:
                    hex_chash = canonical
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

    def _resolve_chash_once(self, params: dict[str, Any]) -> Any:
        """One ``/resolve_chash`` GET; 404 is a miss, other errors propagate."""
        try:
            return self._get("/resolve_chash", **params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    def _canonical_chash(self, hex_chash: str) -> str | None:
        """Alias-resolve *hex_chash* to its canonical identity (nexus-84tr4).

        ``/v1/chash/lookup`` echoes the canonical 64-hex it resolved —
        identity-mapped for canonical input, alias-chained for a legacy 32-hex
        ref. Best-effort by design: this runs only after the primary lookup
        already missed, so a failure here returns exactly the ``None`` the
        caller was going to get anyway. It cannot mask a real error, because
        a non-404 from the primary path propagates before we get here.
        """
        try:
            data = super()._get("/v1/chash/lookup", params={"chash": hex_chash})
        except Exception as exc:  # noqa: BLE001 — supplementary lookup; primary miss already stands
            _log.debug("resolve_chash_alias_lookup_failed", chash=hex_chash, error=str(exc))
            return None
        canonical = (data or {}).get("chash")
        return canonical if isinstance(canonical, str) and canonical else None

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
        """Resolve a span to text content — service-mode parity (nexus-p8nd5).

        Faithful mirror of :meth:`nexus.catalog.catalog.Catalog.resolve_span_text`
        (the canonical contract, ``catalog_protocol``): ``None`` when the span
        is genuinely unresolvable (unknown tumbler, missing chunk); RAISES
        :class:`~nexus.db.http_vector_client.VectorServiceError` when the
        vector service is DEGRADED (nexus-ib6uy — unreachable is never
        collapsed into not-found; the CLI boundaries render the distinction).
        Composition over the existing service surfaces: entry via
        :meth:`resolve`, chunk reads via the service store routes (the shared
        resolver's T3 reads are client-shape-agnostic since the p8nd5 seam
        fix), manifest via :meth:`get_manifest` for chunk:char spans.
        """
        if not span:
            return None
        entry = self.resolve(tumbler)
        if entry is None:
            return None
        from nexus.catalog import catalog_spans  # noqa: PLC0415 — circular-dep avoidance

        return catalog_spans.resolve_span_text_for_entry(
            entry, span, catalog=self,
        )

    # ══════════════════════════════════════════════════════════════════════════
    # LINKS
    # ══════════════════════════════════════════════════════════════════════════

    def _post_link(self, payload: dict) -> bool:
        """POST /link, translating a dangling-endpoint refusal into ValueError.

        nexus-9ssih, CLIENT HALF — deliberately landed AHEAD of its engine half.
        The engine will reject a link whose endpoint does not resolve to a live
        document with ``400 {"code": "dangling_endpoint", ...}``. The canonical
        local ``link_if_absent`` raised ``ValueError`` for exactly that case,
        and ``auto_linker.auto_link`` counts ``skipped_missing_endpoint`` inside
        an ``except ValueError``. Without this translation the refusal would
        surface as ``httpx.HTTPStatusError``, which nothing on the client
        catches — every install would take an uncaught exception on its next
        index pass, since a stale link-context reference occurs on every one.

        FORWARD-COMPATIBLE BY CONSTRUCTION, which is why it can ship first:
        against an engine that never emits that code this branch is unreachable,
        and ``allow_dangling`` on the payload is ignored by an engine that does
        not read it. So this is safe on today's engine and correct on the next.

        Discriminates on ``code``, never on the bare 400: a malformed-body 400
        must keep raising what it raises today.
        """
        try:
            result = self._post("/link", payload)
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 400:
                code = ""
                with contextlib.suppress(Exception):
                    body = exc.response.json()
                    if isinstance(body, dict):
                        code = str(body.get("code", ""))
                if code == "dangling_endpoint":
                    raise ValueError(
                        f"dangling link endpoint: {payload.get('from_tumbler')} -> "
                        f"{payload.get('to_tumbler')} ({payload.get('link_type')}) — "
                        "an endpoint does not resolve to a live catalog document. "
                        "Pass allow_dangling=True to write the edge anyway."
                    ) from exc
            raise
        return bool(result.get("created") if result else False)  # True=created, False=merged (njrcn.3)

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
        return self._post_link(payload)

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
        return self._post_link(payload)

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
        # nexus-ybj1b: the service HONOURS this now (CatalogHandler.handleTraverse
        # -> CatalogRepository.graphBFS). Sending it only when True is correct:
        # absent means false server-side, and false is the default that EXCLUDES
        # implements-heuristic. It was previously "informational" — the server
        # ignored it entirely, so the default silently included the heuristic
        # flood and the opt-in did nothing.
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
        # nexus-ybj1b: the service HONOURS this now (CatalogHandler.handleTraverse
        # -> CatalogRepository.graphBFS). Sending it only when True is correct:
        # absent means false server-side, and false is the default that EXCLUDES
        # implements-heuristic. It was previously "informational" — the server
        # ignored it entirely, so the default silently included the heuristic
        # flood and the opt-in did nothing.
        if include_heuristic:
            payload["include_heuristic"] = True
        return self._traverse(payload)

    def link_audit(self, *, t3: Any = None) -> dict:
        """Audit the link graph: totals, per-type/creator breakdown, orphans, dupes.

        nexus-ai41v: this used to ``return {}`` — "not supported in initial
        service-mode implementation". Service mode is now EVERY mode, and the
        verb ships on two surfaces (``nx catalog link-audit`` and the MCP
        ``catalog_link_audit`` tool), so an empty dict was not a graceful
        degradation: ``--json`` printed ``{}``, which reads as a CLEAN audit.
        That is the silent-false-clean shape this project has been burned by
        before (nexus-ou4tb folded unreadable collections in as "no ghosts").

        Everything here is computed from reads the engine already serves — the
        paginated ``/link_query`` with no filters, and ``/links/orphaned``
        (nexus-ysrwi) — so no engine change is required.

        ``t3`` is accepted and ignored: the local implementation used it for a
        stale-chash cross-check against T3, which this substrate answers from
        the manifest instead. It stays in the signature for call-site parity.
        """
        by_type: dict[str, int] = defaultdict(int)
        by_creator: dict[str, int] = defaultdict(int)
        seen: dict[tuple[str, str, str], int] = defaultdict(int)
        total = 0

        page = 200
        offset = 0
        while True:
            batch = self.link_query(limit=page, offset=offset)
            if not batch:
                break
            for lk in batch:
                total += 1
                by_type[lk.link_type] += 1
                by_creator[lk.created_by or ""] += 1
                seen[(str(lk.from_tumbler), str(lk.to_tumbler), lk.link_type)] += 1
            if len(batch) < page:
                break
            offset += page

        duplicates = {k: n for k, n in seen.items() if n > 1}
        result = self._get("/links/orphaned")
        orphaned = (result.get("links", []) if result else [])

        return {
            "total": total,
            "by_type": dict(by_type),
            "by_creator": dict(by_creator),
            "duplicate_count": len(duplicates),
            "duplicates": [
                {"from_tumbler": f, "to_tumbler": t, "link_type": lt, "count": n}
                for (f, t, lt), n in sorted(duplicates.items())
            ],
            "orphaned_count": len(orphaned),
            "orphaned": orphaned,
        }

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
        legacy_grandfathered: bool | None = None,
    ) -> None:
        """Register (upsert) a collection row.

        ``legacy_grandfathered`` DEFAULTS TO DERIVED, not to False (nexus-cecqy,
        scope-corrected 2026-07-29). The retired local catalog derived the flag
        by regex; the engine infers nothing (``upsertCollection`` binds the
        caller's value verbatim) and this client used to default the kwarg
        ``False`` — so every *bare* ``register_collection(name)`` call site left
        non-conformant names UN-flagged. Those bare calls are BY CONSTRUCTION
        the non-conformant branch of an ``if is_conformant_collection_name(...)``
        fork, which is exactly where the flag has to be True:

            src/nexus/indexer.py:784                         (post-rename registration)
            src/nexus/commands/catalog_cmds/collections.py:138 (backfill loop)
            src/nexus/commands/catalog_cmds/collections.py:349 (rename --allow-legacy)

        The defect was invisible on the conformant branch because a conformant
        name wants False and the old default WAS False — correct by accident,
        which is what made this read as an ``--allow-legacy``-only bug when it
        reached the ordinary ``nx index`` and backfill paths too.

        Deriving here rather than at the call sites puts the inference on the
        same side of the boundary as the implementation it replaced, and covers
        callers that do not yet exist. Pass the kwarg explicitly to force it.
        """
        from nexus.corpus import (  # noqa: PLC0415  — deferred: nexus.corpus imports back into catalog
            is_conformant_collection_name,
        )

        if legacy_grandfathered is None:
            legacy_grandfathered = not is_conformant_collection_name(name)
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
    ) -> int:
        """Mark *old_name* superseded by *new_name*; return rows actually updated.

        Raises ``ValueError`` when the engine refuses on a precondition
        (nexus-g8z8n): *old_name* unregistered, *new_name* unregistered (which
        would leave a dangling ``superseded_by`` pointer), or *old_name* already
        superseded to a DIFFERENT target. Re-asserting the SAME target is
        idempotent and returns 1 without moving the recorded instant.

        nexus-cecqy: returns the engine's rowcount instead of None. Zero used to
        be the routine outcome — the rename DELETEd the old row out from under
        this call — and discarding it is what let the CLI announce a supersede
        that had not happened. The rename now retires the row as a tombstone
        instead, so zero is once again the exceptional case.
        """
        # Wire keys: name (old_name), superseded_by (new_name); reason is informational.
        payload: dict = {"name": old_name, "superseded_by": new_name}
        if reason:
            payload["reason"] = reason
        if superseded_at:
            payload["superseded_at"] = superseded_at
        try:
            result = self._post("/collections/supersede", payload)
        except httpx.HTTPStatusError as exc:
            # 404 = guard 1 or 3 (an endpoint names nothing), 409 = guard 2 (a
            # second supersession would rewrite the chain). Every one of these
            # used to be a 200 {"updated": 0} the client threw away, so a typo on
            # an explicit action silently did nothing — the exact shape the
            # no-silent-fallbacks directive exists to prevent.
            if exc.response is not None and exc.response.status_code in (404, 409):
                raise ValueError(
                    f"supersede_collection({old_name!r} -> {new_name!r}) refused: "
                    f"{_engine_error_detail(exc)}"
                ) from exc
            raise
        return int(result.get("updated", 0)) if isinstance(result, dict) else 0

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
        ``catalog_documents``. The streaming pipeline buffer lives engine-side
        (``nexus.pdf_pipeline``, RDR-186 .16); the local-mode fan-out stays
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
        """Normalize manifest rows for the wire: chash passes through FULL width.

        RDR-180 (nexus-p78a0 rehearsal catch): the catalog chash IS the full
        64-hex ``chunk_text_hash`` — the local Catalog stores it verbatim
        (catalog_writes.py write_manifest, "store the full chash") and the
        engine's converted ``catalog_document_chunks`` enforces
        ``octet_length(chash) = 32`` BYTES (rdr180-001), which a truncated
        32-hex value (16 bytes) VIOLATES on every new write. The length==32
        TEXT check this truncation once served (RDR-168 njrcn.6 layer 2)
        was retired by the same changeset.
        """
        out: list[dict] = []
        for c in chunks:
            row = dict(c)
            row["chash"] = row.get("chash") or ""
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

        nexus-b9puj: same union-guard chain as nexus-ocf52
        (``docs_for_chashes``), one hop deeper — a page FAILURE (exception)
        already fails loud, but a silently TRUNCATED page would not, since
        the fan-out consumers (search_engine, ``docs_for_chashes`` itself)
        can't otherwise tell "no rows for this doc" apart from "a hop
        stripped rows off the response". The engine emits ``count``
        unconditionally (floor >= v0.1.61); the client reconciles
        ``len(manifests) == count`` per page before merging it in.
        """
        if not doc_ids:
            return {}
        merged: dict[str, list[ManifestRow]] = {}
        for start in range(0, len(doc_ids), _MANIFEST_GET_MANY_PAGE):
            batch = doc_ids[start : start + _MANIFEST_GET_MANY_PAGE]
            result = self._post("/manifest/get_many", {"doc_ids": batch})
            result = result if isinstance(result, dict) else {}
            manifests = result.get("manifests", {}) if result else {}
            manifests = manifests if isinstance(manifests, dict) else {}
            if "count" not in result:
                _log.error(
                    "manifest_get_many_count_missing",
                    requested=len(batch),
                    received=len(manifests),
                    response_keys=sorted(result),
                )
                raise RuntimeError(
                    "manifest/get_many response carried no `count` field — "
                    "the engine emits it unconditionally (floor >= v0.1.61), "
                    "so something interposed on the read path; refusing to "
                    "merge an unverifiable page "
                    f"(response keys: {sorted(result)})"
                )
            count = int(result["count"])
            if len(manifests) != count:
                _log.error(
                    "manifest_get_many_count_mismatch",
                    requested=len(batch),
                    received=len(manifests),
                    count=count,
                )
                raise RuntimeError(
                    f"manifest/get_many truncated: page carries "
                    f"{len(manifests)} doc_ids but count says {count} — "
                    "refusing to merge a partial page"
                )
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

        Mirrors the local implementation's contract (RDR-180, nexus-p78a0
        rehearsal catch): EXACT full-width matching — the [:32] prefix
        grouping (and the engine-side 32-char column it matched) died with
        the truncation era; local ``docs_for_chashes`` already matches the
        caller's form verbatim. The RETURNED keys preserve whatever form
        the caller passed in ``chashes``; chashes with no manifest entries
        are omitted from the result, same as local. (History: nexus-h8rf6.12
        established that the wire values must match the engine's EXACT-match
        ``F_CHK_CHASH.in(chashes)`` semantics — that now means full width,
        the same width the manifest stores.)

        nexus-ocf52: the round-1 response is count-reconciled BEFORE any
        early-out — this is the superseded-vector sweep's union guard (a
        chash is hard-deleted from T3 iff no tumbler here references it), so
        a partially-delivered ``tumblers`` list would silently destroy a
        live shared row. A missing ``count`` field (floor >= v0.1.61, which
        emits it unconditionally) means a field-stripping hop interposed —
        refuse rather than hand the sweep an unverifiable reference set. The
        guard runs per PAGE, before any tumblers accumulate into the return
        value, so a stripped-field EMPTY response fails loud rather than
        slipping through indistinguishable from a genuine "nothing found".

        nexus-uu4b9: paged at :data:`_DOCS_FOR_CHASHES_PAGE` — the engine
        caps this endpoint at the same ``MAX_BATCH_DOC_IDS`` = 1000 as the
        other batch endpoints (it previously had no cap at all). Each page
        is reconciled independently; the resulting tumblers are unioned
        (deduplicated, no particular order — every consumer treats this as
        a set of doc ids that reference the chash).
        """
        if not chashes:
            return {}
        prefix_to_inputs: dict[str, list[str]] = defaultdict(list)
        for c in chashes:
            if c:
                prefix_to_inputs[c].append(c)
        if not prefix_to_inputs:
            return {}
        unique_chashes = list(prefix_to_inputs.keys())
        tumblers: set[str] = set()
        for start in range(0, len(unique_chashes), _DOCS_FOR_CHASHES_PAGE):
            batch = unique_chashes[start : start + _DOCS_FOR_CHASHES_PAGE]
            result = self._post("/manifest/docs_for_chashes", {"chashes": batch})
            result = result if isinstance(result, dict) else {}
            # Handler returns {"tumblers": [tumbler_string, ...], "count": N}
            # — flat, not per-chash.
            batch_tumblers = result.get("tumblers", []) if result else []
            batch_tumblers = (
                batch_tumblers if isinstance(batch_tumblers, list) else []
            )
            if "count" not in result:
                _log.error(
                    "manifest_docs_for_chashes_count_missing",
                    requested=len(batch),
                    received=len(batch_tumblers),
                    response_keys=sorted(result),
                )
                raise RuntimeError(
                    "manifest/docs_for_chashes response carried no `count` "
                    "field — the engine emits it unconditionally (floor >= "
                    "v0.1.61), so something interposed on the read path; "
                    "refusing to hand the superseded-vector sweep an "
                    f"unverifiable reference set (response keys: {sorted(result)})"
                )
            count = int(result["count"])
            if len(batch_tumblers) != count:
                _log.error(
                    "manifest_docs_for_chashes_count_mismatch",
                    requested=len(batch),
                    received=len(batch_tumblers),
                    count=count,
                )
                raise RuntimeError(
                    f"manifest/docs_for_chashes truncated: page carries "
                    f"{len(batch_tumblers)} tumblers but count says {count} "
                    "— a partial reference set could make the "
                    "superseded-vector sweep delete a still-referenced "
                    "chunk; refusing"
                )
            tumblers.update(batch_tumblers)
        if not tumblers:
            return {}
        manifests = self.get_manifests(list(tumblers))  # {doc_id: [ManifestRow, ...]}
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
        """Referenced-chash alive-set for a collection, count-reconciled.

        nexus-ir6eh: this list is the indexer GC's alive-set — chunks
        absent from it are classified orphan and DELETED, so a
        PARTIALLY-truncated list silently destroys live data (a fully
        missing list is guarded downstream by the indexer's
        ``manifest_empty_skipping_gc``). The engine emits ``count``
        alongside ``chashes`` since v0.1.55 (CatalogHandler.
        handleManifestChashes; the release floor guarantees it), so the
        client reconciles ``len(chashes) == count`` BEFORE returning and
        fails loud on any deviation — a missing ``count`` field means a
        field-stripping hop interposed (the nexus-znwc2 class) and is
        itself a contract violation, never treated as optional.
        """
        result = self._get("/manifest/chashes", collection=physical_collection)
        result = result if isinstance(result, dict) else {}
        chashes = result.get("chashes") or []
        if "count" not in result:
            _log.error(
                "manifest_chashes_count_missing",
                collection=physical_collection,
                received=len(chashes),
                response_keys=sorted(result),
            )
            raise RuntimeError(
                f"manifest/chashes response for {physical_collection!r} "
                "carried no `count` field — the engine emits it "
                "unconditionally (floor >= v0.1.55), so something "
                "interposed on the read path; refusing to hand GC an "
                "unverifiable alive-set "
                f"(response keys: {sorted(result)})"
            )
        count = int(result["count"])
        if len(chashes) != count:
            _log.error(
                "manifest_chashes_count_mismatch",
                collection=physical_collection,
                received=len(chashes),
                count=count,
            )
            raise RuntimeError(
                f"manifest/chashes truncated for {physical_collection!r}: "
                f"list carries {len(chashes)} chashes but count says "
                f"{count} — orphan classification off this list would "
                "delete live chunks; refusing"
            )
        return set(chashes)

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

    def write_manifest_many(
        self, docs: "list[tuple[str, list[dict]]]",
        complete: dict[str, str] | None = None,
        *,
        sweep: bool = False,
        chunks: "list[dict] | None" = None,
        collection: str | None = None,
        force_re_embed: bool = False,
    ) -> dict:
        """Atomic per-doc manifest REPLACE for many docs in one POST.

        nexus-u2kwq: the flush-grain manifest hook writes complete files
        (file-atomic batching guarantees position 0), and the per-doc
        ``atomic_manifest_replace`` cost 2 POSTs per file (~205s of a
        177-file wall). ``/manifest/write_many`` (engine v0.1.24+) does
        DELETE+INSERT+chunk_count per doc in one server-side per-doc
        transaction; docs page at 1000 (MAX_BATCH parity). Callers must
        catch HTTP 404 and fall back per-doc for older engines.

        *complete* (nexus-5xn3k.4, RUNFENCE) — optional ``{doc_id:
        content_hash}``: for each entry the engine (v0.1.62+, nexus-5xn3k.2)
        runs the SAME fail-closed verify ``/index-run/complete`` runs
        (missing==0 AND referenced==chunk_count) inside the per-doc write
        transaction and stamps ``index_state='complete'`` — no extra round
        trip on the hot flush-grain path. A failed verify does NOT fail the
        write (the rows are correct; over-work-never-under-work): the doc
        lands in ``complete_refused`` instead, and the CALLER MUST parse it
        — a refused doc is not fully indexed. Entries are sent with the
        page containing their doc; a pre-fence engine ignores the unknown
        key, so refusals simply never appear (fence unstamped, 'unknown').

        *sweep* (nexus-eslkl) folds the superseded-vector sweep into each
        doc's own transaction server-side. Response gains ``swept``/
        ``sweep_skipped``/``sweep_detail`` (zero/empty when ``sweep`` is
        False — same request-gated shape the engine already implements).

        *chunks*/*collection*/*force_re_embed* (nexus-wxjr6, the CLIENT
        half of nexus-kl2z6's combined write; design memo T2
        design-kl2z6-combined-write §1.1) — optional top-level ``chunks``
        (``[{chash, text, metadata}, ...]``) folds the chunk VECTOR upload
        into the SAME per-doc transaction as the manifest write.
        *collection* is REQUIRED whenever *chunks* is provided (raises
        ``ValueError`` locally — same shape the engine 400s on, checked
        client-side first so a caller gets an immediate, specific error
        instead of a round trip). *chunks* is sent on the FIRST page
        only — it is a FLUSH-wide payload (the caller's whole upload
        batch), not a per-page one, and a flush's doc count is bounded by
        its own chunk cap (<=300), always far under
        ``_MANIFEST_GET_MANY_PAGE`` (1000), so the real caller (the
        ChunkBatcher flush path) never pages. *force_re_embed* mirrors
        ``/v1/vectors/upsert-chunks``' escape from the RDR-181
        existence-partition.

        ACK-ECHO (design memo §5.2): a request that sent *chunks* and gets
        back a response with NO ``chunks_written`` key means the engine
        silently dropped the unknown field — the route already exists on
        every pre-kl2z6 engine (only the field is new), so no 404 signals
        this the way ``chashes_many``-style capability probes do. This
        follows the ``upsert-chunks`` ack-mismatch RAISE precedent
        (:meth:`nexus.db.http_vector_client.HttpVectorClient.upsert_chunks`,
        ``http_vector_client.py:1348-1354`: ``raise RuntimeError(f"upsert-chunks
        ack mismatch for {collection!r}: sent ...")``), explicitly NOT the
        ``complete_refused_count_mismatch`` WARN-and-conservative-fallback
        precedent (``mcp_infra.py``'s ``_manifest_write_loop``, ~:1690-1697)
        — that shape is correct for a RECONCILABLE count inside an
        otherwise-successful response, but ``chunks_written`` absence
        signals undetectable silent content loss, which is never
        reconcilable after the fact. Raises ``RuntimeError``, never a
        silent proceed.

        Returns ``{"failed_doc_ids": [...], "complete_refused": [{doc_id,
        referenced, missing, chunk_count}, ...], "complete_refused_count":
        int, "swept": int, "sweep_skipped": int, "sweep_detail": [...]}``
        plus ``chunks_written`` when *chunks* was provided — the scalar
        counts are carried separately so a truncated list is detectable
        (bead-amendment MUST, same discipline ``complete_refused_count``
        already established).
        """
        if chunks is not None and not collection:
            raise ValueError(
                "write_manifest_many: 'collection' is required when 'chunks' "
                "is provided (design memo §1.1 — the engine 400s on this "
                "same combination; checked here so the caller gets an "
                "immediate, specific error)"
            )
        failed: list[str] = []
        refused: list[dict] = []
        refused_count = 0
        swept_total = 0
        sweep_skipped_total = 0
        sweep_detail: list[dict] = []
        chunks_written: int | None = None
        for page_num, start in enumerate(range(0, len(docs), _MANIFEST_GET_MANY_PAGE)):
            page = docs[start : start + _MANIFEST_GET_MANY_PAGE]
            body: dict = {"docs": [
                {"doc_id": d, "rows": self._manifest_rows(page_chunks)}
                for d, page_chunks in page
            ]}
            if complete:
                page_complete = {
                    d: complete[d] for d, _ in page if d in complete
                }
                if page_complete:
                    body["complete"] = page_complete
            if sweep:
                body["sweep"] = True
            page_carries_chunks = chunks is not None and page_num == 0
            if page_carries_chunks:
                body["collection"] = collection
                body["chunks"] = chunks
                if force_re_embed:
                    body["force_re_embed"] = True
            if page_carries_chunks:
                # nexus-y9t08: this page's POST triggers a synchronous
                # server-side embed — give it the embed-appropriate
                # budget, not the 30s control-plane default. See the
                # constant's docstring above for the (a)-vs-(b) scoping
                # decision and the CORRECTED retry decision.
                #
                # retry_read_timeout=False: a ReadTimeout here must fail
                # ONCE, not retry — matches the predecessor path's
                # deliberate exclusion of timeouts (see the constant's
                # docstring). Caught below and converted to
                # CombinedWriteEmbedTimeoutError so the OUTER
                # nexus.retry._manifest_write_with_retry wrapper (around
                # this whole call, from indexer.py's flush path) also does
                # not retry — that classifier is type/chain-based only, so
                # a bare re-raised httpx.ReadTimeout (which IS-A
                # httpx.TransportError) would still be retried there even
                # after this inner self-heal opt-out. The deferred-raise
                # below (store the exception, raise it OUTSIDE the except
                # block) deliberately avoids implicit __context__ chaining
                # back to the original ReadTimeout — a chained context
                # would still classify as connectivity at the outer layer
                # despite the new top-level type (verified: `raise ... from
                # None` clears __cause__ but NOT __context__).
                _pending_timeout: httpx.ReadTimeout | None = None
                try:
                    result = self._post(
                        "/manifest/write_many", body,
                        timeout=_COMBINED_WRITE_EMBED_TIMEOUT_S,
                        retry_read_timeout=False,
                    )
                except httpx.ReadTimeout as _rt:
                    _pending_timeout = _rt
                if _pending_timeout is not None:
                    raise CombinedWriteEmbedTimeoutError(
                        collection=collection or "",
                        chunk_count=len(chunks) if chunks else 0,
                        original=str(_pending_timeout),
                    )
            else:
                result = self._post("/manifest/write_many", body)
            result = result if isinstance(result, dict) else {}
            if page_carries_chunks:
                # nexus-wxjr6 ack-echo (design memo §5.2) — see docstring.
                if "chunks_written" not in result:
                    raise RuntimeError(
                        f"write_many ack mismatch for {collection!r}: sent "
                        f"{len(chunks)} chunks but the response carried no "
                        "'chunks_written' key — the engine may not "
                        "understand the combined write (pre-nexus-kl2z6); "
                        "refusing to treat the chunk content as durably "
                        "written"
                    )
                chunks_written = int(result.get("chunks_written") or 0)
            failed.extend(result.get("failed_doc_ids", []))
            refused.extend(result.get("complete_refused") or [])
            refused_count += int(result.get("complete_refused_count") or 0)
            swept_total += int(result.get("swept") or 0)
            sweep_skipped_total += int(result.get("sweep_skipped") or 0)
            sweep_detail.extend(result.get("sweep_detail") or [])
            # nexus-fhhwf: the engine (v0.1.33+) returns a structured
            # per-doc reason alongside the bare id list — surface it so a
            # failed doc is diagnosable from the client log instead of
            # requiring three deploy-gate iterations (the v0.1.24 lesson).
            for f in result.get("failed") or []:
                _log.warning(
                    "manifest_write_many_doc_failed",
                    doc_id=f.get("doc_id", ""),
                    reason=f.get("reason", ""),
                    sqlstate=f.get("sqlstate", ""),
                )
        out: dict = {
            "failed_doc_ids": failed,
            "complete_refused": refused,
            "complete_refused_count": refused_count,
            "swept": swept_total,
            "sweep_skipped": sweep_skipped_total,
            "sweep_detail": sweep_detail,
        }
        if chunks is not None:
            out["chunks_written"] = chunks_written or 0
        return out

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
