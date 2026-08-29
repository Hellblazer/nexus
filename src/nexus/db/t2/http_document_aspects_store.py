# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""HttpDocumentAspectsStore — thin HTTP client over the RDR-152 Java aspects service.

Drop-in replacement for :class:`~nexus.db.t2.document_aspects.DocumentAspects`.
Activated when ``NX_STORAGE_BACKEND_DOCUMENT_ASPECTS=service``.

Config:
    NX_SERVICE_HOST  — service host (default: 127.0.0.1)
    NX_SERVICE_PORT  — service port (required; raises if missing)
    NX_SERVICE_TOKEN — bearer token (required; raises if missing)

Interface parity (bead nexus-gmiaf.15, RDR-152 P2.5):
    upsert, get, get_by_doc_id, list_by_collection, delete,
    delete_orphans, rename_collection, list_by_extractor_version,
    set_salient_sentences, set_salient_sentences_by_key,
    get_salient_sentences, close
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
import structlog

from nexus.db.limits import MAX_QUERY_RESULTS
from nexus.db.t2.records import AspectRecord, _safe_json_dict, _safe_json_list

_log = structlog.get_logger(__name__)

DEFAULT_TENANT: str = "default"


# RDR-152 nexus-fjwxh: env-only resolution replaced by the centralized
# resolver (env halves -> ServiceRegistry lease -> fail loud), so the
# T2 service-mode default works wherever the supervisor is running.
# nexus-f2qvx.2: construction, credential/endpoint refresh-on-401, and the
# HTTP transport itself (_post/_get/_delete) are now inherited wholesale
# from RefreshableHttpStoreMixin — HttpDocumentAspectsStore no longer bakes
# a ``self._headers`` dict or a ``httpx.Client(base_url=..., headers=...)``
# at construction time, which is what let a rotated bearer or a
# supervisor-restart port change go silently stale for the life of the
# instance. See ``nx memory get -p nexus -t design-bikit-refreshable-http-store-mixin.md``.
from nexus.db.t2._raw_handle_guard import RawHandleGuardMixin
from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin


def _record_to_body(record: AspectRecord) -> dict[str, Any]:
    """Serialize AspectRecord to JSON body for HTTP POST."""
    return {
        "collection": record.collection,
        "source_path": record.source_path,
        "problem_formulation": record.problem_formulation,
        "proposed_method": record.proposed_method,
        "experimental_datasets": list(record.experimental_datasets),
        "experimental_baselines": list(record.experimental_baselines),
        "experimental_results": record.experimental_results,
        "extras": dict(record.extras),
        "confidence": record.confidence,
        "extracted_at": record.extracted_at,
        "model_version": record.model_version,
        "extractor_name": record.extractor_name,
        "source_uri": record.source_uri,
        "doc_id": record.doc_id,
        "salient_sentences": list(record.salient_sentences),
    }


def _body_to_record(body: dict[str, Any]) -> AspectRecord:
    """Deserialize HTTP response dict into an AspectRecord."""
    def _json_list(v: Any) -> list:
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            return _safe_json_list(v)
        return []

    def _json_dict(v: Any) -> dict:
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            return _safe_json_dict(v)
        return {}

    return AspectRecord(
        collection=body.get("collection", ""),
        source_path=body.get("source_path", ""),
        problem_formulation=body.get("problem_formulation"),
        proposed_method=body.get("proposed_method"),
        experimental_datasets=_json_list(body.get("experimental_datasets", [])),
        experimental_baselines=_json_list(body.get("experimental_baselines", [])),
        experimental_results=body.get("experimental_results"),
        extras=_json_dict(body.get("extras", {})),
        confidence=body.get("confidence"),
        extracted_at=body.get("extracted_at", ""),
        model_version=body.get("model_version", ""),
        extractor_name=body.get("extractor_name", ""),
        source_uri=body.get("source_uri"),
        doc_id=body.get("doc_id", ""),
        salient_sentences=_json_list(body.get("salient_sentences", [])),
    )


class HttpDocumentAspectsStore(RawHandleGuardMixin, RefreshableHttpStoreMixin):
    """DocumentAspects drop-in that delegates to the RDR-152 Java HTTP service.

    Uses a keep-alive :class:`httpx.Client` connection pool via
    :class:`~nexus.db.t2._refreshable_client.RefreshableHttpStoreMixin`,
    which resolves ``NX_SERVICE_HOST``, ``NX_SERVICE_PORT``, and
    ``NX_SERVICE_TOKEN`` (or a managed ``service_url``/``service_token``)
    fresh on construction AND self-heals (re-resolve + retry once) on a
    401 or a connection-refused/reset — see the mixin's own docstring for
    the full resolution order. ``__init__`` is inherited unchanged (this
    class's constructor signature matches the mixin's pinned contract
    exactly, so no override is needed).

    Promotion log (nexus-70x7y, 2026-07-25): ``list_promotions`` IS
    forwarded, over ``GET /v1/aspects/promotion/list``. Its former sibling
    ``promote_extras_field`` is retired rather than forwarded — runtime
    schema DDL has no valid implementation under the
    ALL-DDL-through-Liquibase directive, so field promotion is now a
    changeset that adds the column, backfills it, and appends to
    ``aspect_promotion_log`` together (recipe in
    ``src/nexus/aspect_promotion.py``).

    Args:
        base_url: Optional override for the service base URL.
        tenant:   Tenant to stamp on every request (default: ``DEFAULT_TENANT``).
    """

    def _has_doc_id_pk(self) -> bool:
        """The PG schema always has doc_id as its primary key.

        The SQLite counterpart introspects ``PRAGMA table_info`` to detect
        whether the schema has been migrated; on the service backend the PG
        DDL always defines ``doc_id TEXT PRIMARY KEY``, so this is
        unconditionally ``True``.
        """
        return True

    # ── Internal helpers ───────────────────────────────────────────────────────
    #
    # These stay LOCAL overrides (not a straight inherit) because every method
    # in this class calls self._post/self._get with a SHORT path suffix
    # (e.g. "/upsert") — the "/v1/aspects" prefix is store-specific routing,
    # not part of the mixin's shared contract. Every actual HTTP round-trip
    # still goes through the inherited, self-healing super()._post/_get
    # (RefreshableHttpStoreMixin._send), never self._client directly.

    def _post(self, path: str, body: dict[str, Any], *, idempotent: bool = True) -> Any:
        return super()._post(f"/v1/aspects{path}", body, idempotent=idempotent)

    def _get(self, path: str, params: dict[str, Any] | None = None, *, idempotent: bool = True) -> Any:
        q = {k: str(v) for k, v in (params or {}).items() if v is not None}
        return super()._get(f"/v1/aspects{path}", q, idempotent=idempotent)

    # ── Public API — mirrors DocumentAspects ──────────────────────────────────

    def upsert(self, record: AspectRecord) -> bool:
        """Persist *record* — complete overwrite if the key already exists.

        Returns True when the row was written; False when rejected by the
        confidence gate (confidence < 0.3).
        """
        if not record.extracted_at:
            raise ValueError("extracted_at must not be empty")
        if not record.model_version:
            raise ValueError("model_version must not be empty")
        if not record.extractor_name:
            raise ValueError("extractor_name must not be empty")
        if not record.doc_id:
            # nexus-x1de2 (52): the engine stores a blank doc_id as NULL
            # (fk-001) — a row no catalog document can claim. The schema
            # permits it and the enqueue contract allows an empty doc_id,
            # so this is not refused, but it is never silent: every
            # producer that knows the identity stamps it (with_doc_id),
            # and a write that still lacks one is the signal the
            # attribution gap has reopened.
            _log.warning(
                "document_aspects_upsert_unattributed",
                collection=record.collection,
                source_path=record.source_path,
                extractor_name=record.extractor_name,
            )
        r = self._post("/upsert", _record_to_body(record))
        return bool(r.get("written", False))

    def get(self, collection: str, source_path: str) -> AspectRecord | None:
        """Return the row matching (collection, source_path), or None."""
        try:
            body = self._get("/get", {
                "collection": collection,
                "source_path": source_path,
            })
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        if not body:
            return None
        return _body_to_record(body)

    def get_by_doc_id(self, doc_id: str) -> AspectRecord | None:
        """Return the row matching doc_id, or None."""
        try:
            body = self._get("/get_by_doc_id", {"doc_id": doc_id})
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        if not body:
            return None
        return _body_to_record(body)

    def list_by_collection(
        self,
        collection: str,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AspectRecord]:
        """Return all rows in *collection*, paginated."""
        params: dict[str, Any] = {"collection": collection, "offset": offset}
        if limit is not None:
            params["limit"] = limit
        rows: list[dict] = self._get("/list_by_collection", params)
        return [_body_to_record(r) for r in rows]

    def list_without_catalog_document(
        self, limit: int | None = None, offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Aspect rows no live catalog document claims (nexus-mlu3k, read-only).

        ``GET /v1/aspects/list_without_catalog_document``: aspects-004's
        attribution predicate, negated engine-side — ``doc_id IS NULL`` and no
        same-tenant, non-alias, non-tombstoned catalog document with a
        byte-equal ``source_uri``. Each row is a plain dict (``collection``,
        ``source_path``, ``source_uri``, ``extracted_at``, ``model_version``,
        ``extractor_name``, ``confidence``, ``tombstoned_match``) rather than
        an :class:`AspectRecord`, because the census cares about identity and
        era, not the extracted fields. Ordered by ``source_uri`` then
        ``collection``. ONE PAGE: the engine clamps *limit* to
        ``AspectRepository.MAX_PAGE_ROWS`` (300; ``None``/0 = one full page,
        never everything), so walk with *offset* or use
        :meth:`iter_without_catalog_document`. An engine without the route
        404s — the caller reports that, never an empty census.
        """
        params: dict[str, Any] = {"offset": offset}
        if limit is not None:
            params["limit"] = limit
        rows: list[dict[str, Any]] = self._get("/list_without_catalog_document", params)
        return list(rows or [])

    def iter_without_catalog_document(
        self, max_rows: int | None = None, page_size: int = MAX_QUERY_RESULTS,
    ) -> Iterator[dict[str, Any]]:
        """Page through :meth:`list_without_catalog_document` until a short page.

        *max_rows* caps the total (``None`` = all); *page_size* is the per-call
        ``limit`` (defaults to the ``MAX_QUERY_RESULTS`` ceiling the engine
        clamps to, so a full page is never silently a truncated one).
        """
        if page_size <= 0:
            raise ValueError(f"page_size must be positive, got {page_size}")
        offset = 0
        yielded = 0
        while max_rows is None or yielded < max_rows:
            want = page_size if max_rows is None else min(page_size, max_rows - yielded)
            page = self.list_without_catalog_document(limit=want, offset=offset)
            yield from page
            yielded += len(page)
            offset += len(page)
            if len(page) < want:
                return

    def delete(self, collection: str, source_path: str) -> int:
        """Drop the row at (collection, source_path). Returns deleted count."""
        r = self._post("/delete", {
            "collection": collection,
            "source_path": source_path,
        })
        return int(r.get("deleted", 0))

    def delete_orphans(
        self,
        catalog_db_path: Path | None,
        *,
        dry_run: bool = True,
    ) -> tuple[int, int]:
        """Orphan deletion is SQLite-specific (ATTACH DATABASE) — REFUSES here.

        Raises :class:`NotImplementedError`. It used to return ``(0, 0)`` with
        only a structlog note, on the reasoning that a noop is safer than a
        delete on a backend that cannot confirm orphan status. The delete half
        of that reasoning still holds; the RETURN half did not.

        ``(0, 0)`` is indistinguishable from "examined every row, found nothing
        wrong". ``nx aspects gc`` consumed it and printed "examined 0 row(s)...
        deleted 0 orphan(s)" — a clean bill of health for a check that never
        ran (nexus-ingey). A caller cannot tell the two apart, so the value is
        withheld rather than made ambiguous: no silent fallbacks for
        data-correctness questions.

        What is true on this backend, and why the refusal is not a coverage
        loss for the main class: rows orphaned by a document delete cannot
        accumulate, because fk-001-catalog-cross-store binds
        ``document_aspects (tenant_id, doc_id)`` to
        ``catalog_documents (tenant_id, tumbler)`` ``ON DELETE CASCADE``.

        What is NOT covered, and must not be claimed clean: the
        ``source_uri``-keyed sweep this method performs on SQLite. That
        changeset deliberately leaves ``source_uri`` unconstrained ("a URI path
        string, not a catalog tumbler reference").
        """
        _log.warning(
            "http_document_aspects_store.delete_orphans_refused",
            catalog_db_path=str(catalog_db_path),
            dry_run=dry_run,
            reason="orphan deletion requires catalog ATTACH which is SQLite-specific",
        )
        raise NotImplementedError(
            "delete_orphans is not supported on the service backend: the sweep "
            "ATTACHes the catalog SQLite cache, which is unavailable over HTTP. "
            "Document-delete orphans cannot accumulate here (document_aspects."
            "doc_id is FK-bound to catalog_documents ON DELETE CASCADE), but the "
            "source_uri-keyed class this method sweeps is deliberately "
            "unconstrained and is NOT certified clean by this refusal. "
            "Track: nexus-ingey."
        )

    def rename_collection(self, *, old: str, new: str) -> int:
        """Re-point every row's collection from *old* to *new*."""
        r = self._post("/rename_collection", {"old": old, "new": new})
        return int(r.get("updated", 0))

    def list_by_extractor_version(
        self,
        extractor_name: str,
        max_version: str,
    ) -> list[AspectRecord]:
        """Return rows whose extractor_name matches and model_version < max_version.

        Wire param is ``extractor_name`` (AspectHandler.java:244). The client
        sent ``extractor`` from inception — every real-engine call 400'd —
        and the fake-server wire tests pinned the client's own wrong shape;
        found by the nexus-i711w A3 port running against the real engine.
        """
        rows: list[dict] = self._get("/list_by_extractor_version", {
            "extractor_name": extractor_name,
            "max_version": max_version,
        })
        return [_body_to_record(r) for r in rows]

    def list_promotions(self) -> list[dict]:
        """Return the ``aspect_promotion_log`` history, oldest first.

        Mirrors ``DocumentAspects.list_promotions``. The engine reads
        ``nexus.aspect_promotion_log`` tenant-scoped and already returns the
        SQLite key shape verbatim (``field_name``, ``sql_type``,
        ``column_added``, ``rows_backfilled``, ``rows_pruned``, ``pruned``,
        ``promoted_at``), so no per-key translation is needed here.
        """
        rows = self._get("/promotion/list")
        return list(rows) if isinstance(rows, list) else []

    def set_salient_sentences(self, doc_id: str, sentences: list[str]) -> bool:
        """Write salient_sentences for doc_id. Returns True on update."""
        r = self._post("/salient_sentences/set", {
            "doc_id": doc_id,
            "sentences": sentences,
        })
        return bool(r.get("updated", False))

    def set_salient_sentences_by_key(
        self,
        collection: str,
        source_path: str,
        sentences: list[str],
    ) -> bool:
        """Pre-PK-migration fallback: target rows by (collection, source_path)."""
        r = self._post("/salient_sentences/set_by_key", {
            "collection": collection,
            "source_path": source_path,
            "sentences": sentences,
        })
        return bool(r.get("updated", False))

    def get_salient_sentences(self, doc_id: str) -> list[str]:
        """Return the salient sentences for doc_id, or []."""
        try:
            r = self._get("/salient_sentences/get", {"doc_id": doc_id})
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return []
            raise
        sentences = r.get("sentences", [])
        if not isinstance(sentences, list):
            return []
        return [str(s) for s in sentences if s]

    # ── ETL import ────────────────────────────────────────────────────────────

    def import_aspect(self, body: dict[str, Any]) -> int:
        """Fidelity-preserving import for ETL. Returns 1 on success."""
        r = self._post("/import", body)
        return int(r.get("imported", 0))

    def import_aspects_batch(self, rows: list[dict[str, Any]]) -> int:
        """RDR-176 P3 (bead nexus-t9rmg.18): GUC-once bulk aspect import — POST
        all *rows* to /v1/aspects/import in ONE request. Returns rows written
        (sub-confidence rows count 0). Empty list is a no-op."""
        if not rows:
            return 0
        r = self._post("/import", {"rows": rows})
        return int(r.get("imported", 0))

    def import_promotion_batch(self, rows: list[dict[str, Any]]) -> int:
        """RDR-176 P3: GUC-once bulk aspect-promotion import — POST all *rows* to
        /v1/aspects/promotion/import in ONE request."""
        if not rows:
            return 0
        r = self._post("/promotion/import", {"rows": rows})
        return int(r.get("imported", 0))

    # ── RDR-089 SQL fast-path operator queries (bead nexus-l9hd8) ─────────────

    def operator_filter(
        self,
        source_uris: list[str],
        field: str,
        predicate: str,
    ) -> list[str]:
        """Filter: return source_uris matching field ILIKE predicate.

        Mirrors ``aspect_sql._query_filter`` semantics over the service
        backend. Batching is handled server-side.

        Args:
            source_uris: candidate URIs (already derived from idents via uri_for)
            field:       aspect column name or ``extras.key``
            predicate:   SQL LIKE/ILIKE pattern (e.g. ``"%paxos%"``)

        Returns:
            subset of source_uris whose aspect row matches the predicate
        """
        if not source_uris:
            return []
        r = self._post("/operator-query", {
            "op": "filter",
            "field": field,
            "predicate": predicate,
            "source_uris": source_uris,
        })
        result: list[str] = r.get("matched_uris", [])
        _log.debug(
            "http_aspects.operator_filter",
            field=field,
            input_count=len(source_uris),
            matched_count=len(result),
        )
        return result

    def operator_groupby(
        self,
        source_uris: list[str],
        field: str,
    ) -> dict[str, str | None]:
        """GroupBy: return {source_uri: key_value} for each URI with an aspect row.

        Mirrors ``aspect_sql._query_groupby`` semantics. URIs without aspect
        rows are absent from the result dict; the Python caller maps absent
        entries to ``"unassigned"``.

        Args:
            source_uris: candidate URIs
            field:       aspect column name or ``extras.key``

        Returns:
            dict mapping source_uri to its field value (str or None)
        """
        if not source_uris:
            return {}
        r = self._post("/operator-query", {
            "op": "groupby",
            "field": field,
            "source_uris": source_uris,
        })
        groups: list[dict] = r.get("uri_groups", [])
        result: dict[str, str | None] = {}
        for entry in groups:
            uri = entry.get("source_uri")
            val = entry.get("key_value")
            if uri is not None:
                result[uri] = val
        _log.debug(
            "http_aspects.operator_groupby",
            field=field,
            input_count=len(source_uris),
            group_count=len(result),
        )
        return result

    def operator_confidence_aggregate(
        self,
        source_uris: list[str],
        reducer_kind: str,
    ) -> float | None:
        """Confidence aggregate: AVG / MIN / MAX confidence across source_uris.

        Mirrors ``aspect_sql._query_confidence_aggregate`` semantics.

        Args:
            source_uris:  candidate URIs
            reducer_kind: one of ``avg_confidence``, ``min_confidence``,
                          ``max_confidence``

        Returns:
            the aggregate value as a float, or None when no rows matched
        """
        if not source_uris:
            return None
        r = self._post("/operator-query", {
            "op": "confidence_aggregate",
            "reducer_kind": reducer_kind,
            "source_uris": source_uris,
        })
        value = r.get("value")
        if value is None:
            return None
        _log.debug(
            "http_aspects.operator_confidence_aggregate",
            reducer_kind=reducer_kind,
            input_count=len(source_uris),
            value=value,
        )
        return float(value)
