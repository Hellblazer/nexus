# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""HttpAspectQueue — thin HTTP client over the RDR-152 Java aspects/queue service.

Drop-in replacement for :class:`~nexus.db.t2.aspect_extraction_queue.AspectExtractionQueue`.
Activated when ``NX_STORAGE_BACKEND_ASPECT_QUEUE=service``.

Config:
    NX_SERVICE_HOST  — service host (default: 127.0.0.1)
    NX_SERVICE_PORT  — service port (required; raises if missing)
    NX_SERVICE_TOKEN — bearer token (required; raises if missing)

Interface parity (bead nexus-gmiaf.15, RDR-152 P2.5):
    enqueue, claim_next, claim_batch, mark_done, mark_failed,
    mark_retry, reclaim_stale, pending_count, is_drained,
    list_pending, rename_collection, close

Queue claim strategy: the Java service uses
``SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1`` — eliminates WAL contention
vs the SQLite CAS loop and is the primary motivation for this migration.

The ``rename_lock`` constructor parameter accepted by AspectExtractionQueue
is accepted but ignored here (no Python-side threading; the Java service owns
all serialization).
"""
from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import structlog

from nexus.collection_errors import SupersededCollectionWriteError
from nexus.db.t2.records import QueueRow

_log = structlog.get_logger(__name__)

DEFAULT_TENANT: str = "default"

# ── RDR-235: the producer behind NX_ASPECT_QUEUE ─────────────────────────────

#: Selects where :meth:`HttpAspectQueue.enqueue` and ``enqueue_many`` write.
ASPECT_QUEUE_ENV: str = "NX_ASPECT_QUEUE"
ASPECT_QUEUE_MODES: tuple[str, str] = ("table", "tuples")

#: The tuple-space subspace, key and the engine release that carries the
#: ``aspects`` template (RDR-235 E3). One queue per tenant; the collection is a
#: field of the task body.
ASPECTS_SUBSPACE: str = "aspects"
ASPECTS_KEYS: dict[str, str] = {"queue": "aspects"}
ASPECTS_ENGINE_RELEASE: str = "engine-service-v0.1.169"

#: Concurrent ``out`` calls one ``enqueue_many`` makes. The tuple space has no
#: batch ``out`` (RDR-235 gate S2); a flush holds at most 16 (onnx-local) or 64
#: (Voyage) documents, so this is at most 8 and 2 waves. Each call is one
#: ``out`` on its own id over the shared keep-alive pool.
ENQUEUE_FANOUT_WORKERS: int = 8

_warned_unknown_modes: set[str] = set()


def aspect_queue_mode() -> str:
    """``"table"`` (default) or ``"tuples"``, read from ``NX_ASPECT_QUEUE`` at
    call time. An unset or blank value is ``table``. Any other value logs one
    warning per process and means ``table``: the table is the path every
    released engine supports, so a typo falls back to the working queue and
    says so, instead of failing every ingest hook."""
    raw = os.environ.get(ASPECT_QUEUE_ENV, "").strip().lower()
    if raw in ("", "table"):
        return "table"
    if raw == "tuples":
        return "tuples"
    if raw not in _warned_unknown_modes:
        _warned_unknown_modes.add(raw)
        _log.warning(
            "aspect_queue_mode_unknown_falling_back_to_table",
            env=ASPECT_QUEUE_ENV, value=raw, allowed=list(ASPECT_QUEUE_MODES),
        )
    return "table"


class AspectQueueEngineUnsupportedError(RuntimeError):
    """``NX_ASPECT_QUEUE=tuples`` against an engine with no ``aspects``
    template (it predates ``engine-service-v0.1.169``)."""


_warned_no_aspects_template: list[bool] = []


def aspects_census(store: Any, *, timeout: float | None = None) -> Any:
    """The ``aspects`` subspace's census from *store* (an ``HttpTupleStore``),
    or ``None`` when the engine has no ``aspects`` template.

    The readers of the old table (queue-depth hook, drain, requeue-failed)
    count this side beside the table's (RDR-235). An engine that predates
    ``engine-service-v0.1.169`` has no such template, and every reader must go
    on working there exactly as it did: ``None`` means "no tuple side", the
    caller reads it as zero, and one log line per process says so. Any other
    failure propagates; a caller decides what an unreadable side means.

    *timeout* caps the one request (seconds); ``None`` sends it unchanged."""
    from nexus.db.t2.http_tuple_store import UnknownSubspaceError  # noqa: PLC0415 — deferred: table mode never loads it

    try:
        if timeout is None:
            return store.subspace_stats(ASPECTS_SUBSPACE)
        return store.subspace_stats(ASPECTS_SUBSPACE, timeout=timeout)
    except UnknownSubspaceError:
        if not _warned_no_aspects_template:
            _warned_no_aspects_template.append(True)
            _log.info(
                "aspects_tuple_template_absent_reading_table_only",
                needs=ASPECTS_ENGINE_RELEASE,
            )
        return None


def _identity_body(collection: str, source_path: str, doc_id: str, content_hash: str) -> str:
    """The task body (RDR-235 E2): the queue row's identity, never text. The
    worker re-reads the document from T3 chunks or the file."""
    return json.dumps(
        {
            "collection": collection,
            "source_path": source_path,
            "doc_id": doc_id,
            "content_hash": content_hash,
        },
        ensure_ascii=False, separators=(",", ":"),
    )


# RDR-152 nexus-fjwxh: env-only resolution replaced by the centralized
# resolver (env halves -> ServiceRegistry lease -> fail loud), so the
# T2 service-mode default works wherever the supervisor is running.
# nexus-f2qvx.2: construction, credential/endpoint refresh-on-401, and the
# HTTP transport itself (_post/_get/_delete) are now inherited wholesale
# from RefreshableHttpStoreMixin — HttpAspectQueue no longer bakes a
# ``self._headers`` dict or a ``httpx.Client(base_url=..., headers=...)``
# at construction time, which is what let a rotated bearer or a
# supervisor-restart port change go silently stale for the life of the
# instance. See ``nx memory get -p nexus -t design-bikit-refreshable-http-store-mixin.md``.
# This store is the one with a public ``timeout`` kwarg on its own
# constructor (the mixin's ``_DEFAULT_TIMEOUT_S`` docstring flags it by
# name) — threaded through to ``super().__init__(..., timeout=timeout)``
# below, which required the mixin's own ``__init__`` to grow a matching
# optional ``timeout`` kwarg (additive; every other adopter keeps the
# 30.0s default unchanged).
from nexus.db.t2._raw_handle_guard import RawHandleGuardMixin
from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin


def _body_to_queue_row(body: dict[str, Any]) -> QueueRow:
    return QueueRow(
        collection=body.get("collection", ""),
        source_path=body.get("source_path", ""),
        content_hash=body.get("content_hash", ""),
        content=body.get("content", ""),
        retry_count=int(body.get("retry_count", 0)),
        doc_id=body.get("doc_id", "") or "",
    )


class HttpAspectQueue(RawHandleGuardMixin, RefreshableHttpStoreMixin):
    """AspectExtractionQueue drop-in that delegates to the RDR-152 Java HTTP service.

    Uses a keep-alive :class:`httpx.Client` connection pool via
    :class:`~nexus.db.t2._refreshable_client.RefreshableHttpStoreMixin`,
    which resolves ``NX_SERVICE_HOST``, ``NX_SERVICE_PORT``, and
    ``NX_SERVICE_TOKEN`` (or a managed ``service_url``/``service_token``)
    fresh on construction AND self-heals (re-resolve + retry once) on a
    401 or a connection-refused/reset — see the mixin's own docstring for
    the full resolution order.

    The ``rename_lock`` parameter is accepted to match AspectExtractionQueue's
    constructor signature (T2Database injects it). It is ignored — no
    Python-side threading guards are needed when the Java service owns all
    queue state. ``timeout`` is this store's own public constructor kwarg
    (pre-dating the mixin); it is threaded through to the mixin's
    ``__init__`` unchanged so callers that pin a non-default HTTP timeout
    keep working identically post-adoption.

    Args:
        base_url:    Optional override for the service base URL.
        tenant:      Tenant to stamp on every request (default: ``DEFAULT_TENANT``).
        rename_lock: Accepted for constructor parity with AspectExtractionQueue;
                     NOT used (no-op).
        timeout:     HTTP client timeout in seconds (default: 30.0).
        client:      Optional pre-constructed ``httpx.Client`` (nexus-m20mf
                     P3, additive) -- when supplied, this store shares that
                     pool instead of opening its own, and ``close()`` on
                     this store becomes a no-op for the client (the
                     injecting caller owns closing it).
    """

    def __init__(
        self,
        base_url: str | None = None,
        tenant: str = DEFAULT_TENANT,
        *,
        rename_lock: "threading.RLock | None" = None,
        _token: str | None = None,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(base_url, tenant, _token=_token, timeout=timeout, client=client)
        # RDR-235: the construction arguments the tuples-mode store is built
        # from (lazily, on first use), so it resolves its endpoint and token
        # and self-heals exactly as this store does. Table mode never builds it.
        self._ctor_base_url = base_url
        self._ctor_token = _token
        self._tuple_store: Any = None
        self._tuple_store_lock = threading.Lock()
        # rename_lock accepted for constructor parity but ignored over HTTP
        # (verbatim behavior preserved from the pre-mixin constructor).
        self.rename_lock: threading.RLock = (
            rename_lock if rename_lock is not None else threading.RLock()
        )

    # ── Internal helpers ───────────────────────────────────────────────────────
    #
    # These stay LOCAL overrides (not a straight inherit) because every method
    # in this class calls self._post/self._get with a SHORT path suffix
    # (e.g. "/enqueue") — the "/v1/aspects/queue" prefix is store-specific
    # routing, not part of the mixin's shared contract. Every actual HTTP
    # round-trip still goes through the inherited, self-healing
    # super()._post/_get (RefreshableHttpStoreMixin._send), never
    # self._client directly.

    def _post(
        self, path: str, body: dict[str, Any], *, idempotent: bool = True, mutates: bool = True
    ) -> Any:
        # nexus-a2qhz: mutates forwarded unchanged (every call site in this
        # class is a write, default True; forwarding it anyway closes the
        # latent TypeError trap a future read-via-POST addition would
        # otherwise hit -- the class of regression caught in
        # HttpTokenStore.list_tokens during review).
        return super()._post(
            f"/v1/aspects/queue{path}", body, idempotent=idempotent, mutates=mutates
        )

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        idempotent: bool = True,
        timeout: float | None = None,
    ) -> Any:
        q = {k: str(v) for k, v in (params or {}).items() if v is not None}
        return super()._get(f"/v1/aspects/queue{path}", q, idempotent=idempotent, timeout=timeout)

    # ── Public API — mirrors AspectExtractionQueue ────────────────────────────

    def _signal_wake(self) -> None:
        """Best-effort same-box wake touch after a successful enqueue
        (nexus-59611 stage 2). ``signal_aspect_wake`` already swallows its
        own failures (an unwritable config dir, races); nothing here can
        fail the enqueue that just succeeded.
        """
        from nexus.aspect_worker import signal_aspect_wake  # noqa: PLC0415 — deferred to avoid circular import (aspect_worker <-> db.t2)

        signal_aspect_wake()

    def enqueue(
        self,
        collection: str,
        source_path: str,
        content_hash: str = "",
        content: str = "",
        *,
        doc_id: str = "",
    ) -> None:
        """Persist a new pending row for (collection, source_path).

        INSERT OR REPLACE semantics: re-enqueue at the same key resets
        status='pending' and retry_count=0.
        """
        if not collection:
            raise ValueError("collection must not be empty")
        if not source_path:
            raise ValueError("source_path must not be empty")
        if aspect_queue_mode() == "tuples":
            # RDR-235 E2: the identity goes out as a replace; ``content`` is
            # dropped (the worker re-reads the document).
            self._enqueue_tuple(collection, source_path, content_hash, doc_id)
            self._signal_wake()
            return
        # RDR-204 Phase 1 (nexus-f5wwx): the engine no longer
        # auto-registers a collection on first write — ensure it here,
        # once per process per collection (cached), and retry once if
        # the engine's per-tenant boot sweep (bead .3) reaped a
        # registered-but-chunkless row between registration and this
        # write.
        from nexus.corpus import write_with_registration_retry  # noqa: PLC0415 — circular-dep avoidance (corpus)
        write_with_registration_retry(collection, lambda: self._post("/enqueue", {
            "collection": collection,
            "source_path": source_path,
            "content_hash": content_hash,
            "content": content,
            "doc_id": doc_id,
        }), registrar=self._catalog_registrar)
        self._signal_wake()

    def enqueue_many(self, rows: list[dict]) -> int:
        """Batch-enqueue N documents in ONE round trip (nexus-nj4ch,
        nexus-duoak follow-up: replaces the indexer's per-document
        ``enqueue()`` call under the ChunkBatcher's flush-grain hook,
        measured at ~34.7s across ~250 real inserts in this repo's own
        shakeout).

        Each dict carries the same fields as :meth:`enqueue`'s keyword
        arguments (``collection``, ``source_path``, and optionally
        ``content_hash``/``content``/``doc_id``). Returns the count of
        rows actually enqueued (malformed rows are skipped server-side,
        not counted).

        The whole batch shares ONE Postgres transaction server-side
        (``ctx.batch(...)`` inside one ``tenantScope.withTenant``), so a
        single row's constraint violation (e.g. a ``doc_id`` FK to a
        catalog document that hasn't landed yet) fails the WHOLE batch,
        not just that row — unlike the per-row isolation
        ``updateDocumentsMany``/``register_many`` get from their looser
        constraints. A batch the engine refuses because a collection it names is
        retired (:class:`~nexus.collection_errors.SupersededCollectionWriteError`)
        is raised as is: the refusal is deterministic, so no per-row replay.
        On any other batch failure this falls back to per-row
        :meth:`enqueue` calls, isolating the genuinely bad row from its
        batch-mates (mirrors :meth:`nexus.catalog.http_catalog_client.
        HttpCatalogClient.register_many`'s page-failure fallback).
        """
        if not rows:
            return 0
        if aspect_queue_mode() == "tuples":
            return self._enqueue_many_tuples(rows)
        try:
            result = self._post("/enqueue_many", {"rows": rows})
            enqueued = int((result or {}).get("enqueued", 0))
            if enqueued:
                self._signal_wake()
            return enqueued
        except SupersededCollectionWriteError:
            # nexus-i15oc: the engine refused the batch because a collection it names is
            # retired (409 collection_superseded). That is deterministic, not one bad
            # row: the per-row replay below would repeat the same refusal N times, each
            # with a traceback. Raise it to the caller as it is; the one caller
            # (indexer flush hook) is best-effort and logs any exception.
            raise
        except Exception:  # noqa: BLE001 — batch unrecoverable (e.g. one row's constraint violation); per-row isolation fallback
            _log.warning(
                "aspect_enqueue_many_failed_falling_back_per_row",
                row_count=len(rows),
                exc_info=True,
            )
            enqueued = 0
            for row in rows:
                collection = row.get("collection", "")
                source_path = row.get("source_path", "")
                if not collection or not source_path:
                    continue
                try:
                    self.enqueue(
                        collection, source_path,
                        content_hash=row.get("content_hash", ""),
                        content=row.get("content", ""),
                        doc_id=row.get("doc_id", ""),
                    )
                    enqueued += 1
                except Exception:  # noqa: BLE001 — per-row fallback failure isolation (mirrors register_many's per-doc try/except upstream)
                    _log.warning(
                        "aspect_enqueue_fallback_row_failed",
                        collection=collection,
                        source_path=source_path,
                        exc_info=True,
                    )
            return enqueued

    # ── RDR-235: the tuples-mode producer ─────────────────────────────────────

    def _tuples(self) -> Any:
        """The ``HttpTupleStore`` tuples mode writes through, built on first use
        over this store's own keep-alive pool."""
        with self._tuple_store_lock:
            if self._tuple_store is None:
                from nexus.db.t2.http_tuple_store import HttpTupleStore  # noqa: PLC0415 — deferred: table mode never builds it

                self._tuple_store = HttpTupleStore(
                    self._ctor_base_url, self._tenant,
                    _token=self._ctor_token, client=self._client,
                )
            return self._tuple_store

    def _out_task(self, collection: str, source_path: str, content_hash: str, doc_id: str) -> str:
        """One ``out`` ``mode=replace`` of the identity body; returns the outcome
        (``created``, ``replaced`` or ``dirtied``). nonce = ``doc_id`` (a tumbler,
        unique per document across collections), so a re-enqueue replaces the
        task and never adds a second. Raises ``TooLargeError`` before any send
        when the body is over the 4,096-byte cap (a very long ``source_path``)."""
        from nexus.db.t2.http_tuple_store import UnknownSubspaceError  # noqa: PLC0415 — deferred: table mode never loads it

        body = _identity_body(collection, source_path, doc_id, content_hash)
        try:
            _id, outcome = self._tuples().out_replace(
                ASPECTS_SUBSPACE, dict(ASPECTS_KEYS), None, body, nonce=doc_id,
            )
        except UnknownSubspaceError as exc:
            raise AspectQueueEngineUnsupportedError(
                f"{ASPECT_QUEUE_ENV}=tuples needs the 'aspects' tuple template, which the "
                f"engine does not have; it predates {ASPECTS_ENGINE_RELEASE}. Upgrade the "
                f"engine, or unset {ASPECT_QUEUE_ENV} to use the table queue."
            ) from exc
        return outcome

    def _enqueue_tuple(
        self, collection: str, source_path: str, content_hash: str, doc_id: str,
    ) -> None:
        if not doc_id:
            raise ValueError(
                f"doc_id must not be empty when {ASPECT_QUEUE_ENV}=tuples: it is the task's identity"
            )
        # The registered-and-not-retired check (O4). A retired name raises
        # SupersededCollectionWriteError here, before anything is sent to the
        # tuple space; the tuple engine has no collection_superseded check.
        from nexus.corpus import ensure_collection_registered  # noqa: PLC0415 — circular-dep avoidance (corpus)

        ensure_collection_registered(collection, registrar=self._catalog_registrar)
        self._out_task(collection, source_path, content_hash, doc_id)

    def _enqueue_many_tuples(self, rows: list[dict]) -> int:
        """The batch form for tuples mode: a bounded fan-out of replaces.

        The tuple space has no batch ``out`` (RDR-235 gate S2), so each document
        is one ``out``, at most :data:`ENQUEUE_FANOUT_WORKERS` at once. Every
        ``out`` is independent and idempotent, so a batch that stops half way
        leaves valid tasks behind and a re-run lands on the same ids.

        * A row with no ``collection``, ``source_path`` or ``doc_id`` is skipped
          and counted in one warning (the table form skips them server-side).
        * A row whose body is over the cap (``TooLargeError``, refused before
          send) is skipped with a warning naming its ``doc_id``; its batch-mates
          still land.
        * Anything else stops the batch: ``MaxLiveRowsExceeded`` (the tenant is
          at the template's ceiling), a retired collection, a missing template
          or a transport failure is not one bad row, and replaying the rest
          would hit the same wall N times. The remaining rows are not sent, an
          error is logged with the counts, and the typed error is raised to the
          caller. Rows that already landed stay.

        Returns the number of tasks written (created, replaced or dirtied).
        """
        from nexus.corpus import ensure_collection_registered  # noqa: PLC0415 — circular-dep avoidance (corpus)
        from nexus.db.t2.http_tuple_store import TooLargeError  # noqa: PLC0415 — deferred: table mode never loads it

        work: list[tuple[str, str, str, str]] = []
        malformed = 0
        for row in rows:
            collection = row.get("collection", "")
            source_path = row.get("source_path", "")
            doc_id = row.get("doc_id", "")
            if not collection or not source_path or not doc_id:
                malformed += 1
                continue
            work.append((collection, source_path, row.get("content_hash", ""), doc_id))
        if malformed:
            _log.warning(
                "aspect_enqueue_many_rows_skipped_malformed",
                skipped=malformed, row_count=len(rows),
            )
        if not work:
            return 0
        # O4: refuse a retired name before any out. One check per collection
        # (cached per process; a retired name raises SupersededCollectionWriteError).
        for collection in dict.fromkeys(w[0] for w in work):
            ensure_collection_registered(collection, registrar=self._catalog_registrar)

        stop = threading.Event()

        def one(item: tuple[str, str, str, str]) -> tuple[str, BaseException | None]:
            if stop.is_set():
                return "unsent", None
            try:
                self._out_task(*item)
                return "ok", None
            except TooLargeError as exc:
                return "too_large", exc
            except BaseException as exc:  # noqa: BLE001 — surfaced below as the batch's fatal error
                stop.set()
                return "fatal", exc

        if len(work) == 1:
            results = [one(work[0])]
        else:
            with ThreadPoolExecutor(
                max_workers=min(ENQUEUE_FANOUT_WORKERS, len(work)),
                thread_name_prefix="aspect-enqueue",
            ) as pool:
                results = list(pool.map(one, work))

        enqueued = 0
        fatal: BaseException | None = None
        for item, (status, exc) in zip(work, results):
            if status == "ok":
                enqueued += 1
            elif status == "too_large":
                _log.warning(
                    "aspect_enqueue_row_skipped_too_large",
                    collection=item[0], source_path=item[1][:200], doc_id=item[3],
                    error=str(exc)[:300],
                )
            elif status == "fatal" and fatal is None:
                fatal = exc
        if enqueued:
            self._signal_wake()
        if fatal is not None:
            _log.error(
                "aspect_enqueue_many_aborted",
                collections=sorted({w[0] for w in work}),
                row_count=len(rows), enqueued=enqueued,
                unsent=sum(1 for st, _ in results if st == "unsent"),
                error_type=type(fatal).__name__, error=str(fatal)[:300],
            )
            raise fatal
        return enqueued

    def close(self) -> None:
        store, self._tuple_store = self._tuple_store, None
        if store is not None:
            store.close()
        super().close()

    def claim_next(self) -> QueueRow | None:
        """Atomically claim the oldest pending row via SELECT ... FOR UPDATE SKIP LOCKED.

        Returns the claimed row as a QueueRow, or None when no pending row exists.
        The Java service uses FOR UPDATE SKIP LOCKED — no CAS retry loop needed.
        """
        # nexus-tjvgf: claiming is NOT retry-safe — a lost response
        # after a successful server-side claim orphans the row
        # in_progress until reclaim_stale. Single attempt; the worker
        # loop owns recovery. Exception (nexus-ig3qe): a definitive 401
        # re-mints and retries once inside the mixin — the auth layer
        # rejected the request before the claim SQL ran, so no row was
        # claimed and no orphan is possible.
        r = self._post("/claim_next", {}, idempotent=False)
        if not r or r.get("claimed") is False or not r.get("row"):
            return None
        return _body_to_queue_row(r["row"])

    def claim_batch(self, limit: int) -> list[QueueRow]:
        """Claim up to *limit* pending rows in FIFO order."""
        if limit <= 0:
            return []
        # nexus-tjvgf: see claim_next — single attempt (with the mixin's
        # nexus-ig3qe 401-remint carve-out; the long-running worker's data
        # token expires on TTL and this is its claim path).
        r = self._post("/claim_batch", {"limit": limit}, idempotent=False)
        # nexus-575kd: the Java service sends a BARE JSON ARRAY here
        # (AspectHandler.handleQueueClaimBatch -> writeValueAsString(rows)),
        # unlike claim_next which is enveloped. Accept the array directly;
        # tolerate a future {"rows":[...]} envelope defensively. The prior
        # ``r.get("rows", [])`` raised AttributeError on the list every poll,
        # so the service-mode worker claimed nothing and document_aspects never
        # populated (all service-mode aspect extraction was dead).
        rows = r if isinstance(r, list) else r.get("rows", [])
        return [_body_to_queue_row(row) for row in rows]

    def mark_done(
        self,
        collection: str = "",
        source_path: str = "",
        *,
        doc_id: str = "",
    ) -> int:
        """DELETE the row at (doc_id) or (collection, source_path) — success path.

        Returns deleted row count.
        """
        r = self._post("/mark_done", {
            "collection": collection,
            "source_path": source_path,
            "doc_id": doc_id,
        })
        return int(r.get("deleted", 0))

    def mark_failed(
        self,
        collection: str,
        source_path: str,
        error: str,
    ) -> None:
        """Mark the row as 'failed' (terminal until re-enqueued)."""
        self._post("/mark_failed", {
            "collection": collection,
            "source_path": source_path,
            "error": error[:2000],
        })

    def mark_retry(self, collection: str, source_path: str,
                   interval_seconds: int = 0) -> None:
        """Reset the row to 'pending', increment retry_count, and back it off.

        ``interval_seconds`` is the worker-chosen backoff; the service stamps
        ``next_retry_at = now() + interval_seconds`` server-side (RDR-163 P1,
        nexus-ztpt6). Default 0 = ready immediately.
        """
        # nexus-tjvgf: retry_count = retry_count + 1 server-side — a
        # retried request double-increments the budget toward premature
        # terminal mark_failed. Single attempt (the mixin's nexus-ig3qe
        # 401-remint carve-out applies: a 401'd request never reached the
        # increment).
        self._post("/mark_retry", {
            "collection": collection,
            "source_path": source_path,
            "interval_seconds": interval_seconds,
        }, idempotent=False)

    def reclaim_stale(
        self, timeout_seconds: int = 300, *, count_attempt: bool = True,
    ) -> int:
        """Reclaim in_progress rows older than timeout.

        ``count_attempt=True`` (default, the age-based sweep): each row goes
        back to 'pending' with retry_count + 1, or to 'failed' when it was
        already at the retry cap (the engine's reclaimStale counts a reclaim as
        an attempt, nexus-j67ix.2).

        ``count_attempt=False`` (the daemon's graceful-stop sweep, nexus-j67ix.3):
        every stale row goes back to 'pending' with retry_count and last_error
        unchanged, and is never failed, because the rows a stopping worker
        abandons did nothing wrong. An engine without this change ignores the key:
        every released engine through 0.1.165 never counted a reclaim, and a
        develop build that carries only nexus-j67ix.2 counts.

        Returns the number of rows reclaimed, whichever way they went.

        ``idempotent=False`` (nexus-ll31n sibling): an age-predicate sweep
        over a discovered population, the same shape as ``mark_retry``
        just above -- a retry after a lost response re-evaluates the same
        predicate against rows already reclaimed by attempt 1 and reports
        fewer (often zero), misreporting a completed reclaim as having
        done nothing (RDR-173's shutdown-observability warning line
        reads this count).
        """
        r = self._post(
            "/reclaim_stale",
            {"timeout_seconds": timeout_seconds, "count_attempt": count_attempt},
            idempotent=False,
        )
        return int(r.get("reclaimed", 0))

    def pending_count(self, *, timeout: float | None = None) -> int:
        """Return the number of rows currently in 'pending' status.

        ``timeout`` (nexus-m20mf P3 fold-in): optional per-request
        override -- lets a caller sharing this store's client across a
        longer-timeout run (e.g. an ``index_repository()`` shared pool)
        still cap THIS specific call strictly, without needing its own
        dedicated short-timeout client. ``None`` (default): no override,
        unchanged from before this kwarg existed.
        """
        r = self._get("/pending_count", timeout=timeout)
        return int(r.get("count", 0))

    def is_drained(self) -> bool:
        """Return True iff no actionable rows remain (count of non-failed rows is 0)."""
        r = self._get("/is_drained")
        return bool(r.get("drained", False))

    def list_pending(self, limit: int | None = None) -> list[QueueRow]:
        """Return pending rows in claim order (FIFO by enqueued_at)."""
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        rows: list[dict] = self._get("/list_pending", params)
        return [_body_to_queue_row(r) for r in rows]

    def list_failed(self, collection: str | None = None) -> list[QueueRow]:
        """Return terminal-failed rows, optionally scoped to one collection."""
        params: dict[str, Any] = {}
        if collection:
            params["collection"] = collection
        rows: list[dict] = self._get("/list_failed", params)
        return [_body_to_queue_row(r) for r in rows]

    def rename_collection(self, *, old: str, new: str) -> int:
        """Re-point every row's collection from *old* to *new*.

        RDR-204 Phase 1 follow-up (nexus-f5wwx): the engine no longer
        auto-registers the rename DESTINATION — ``new`` must be a
        registered ``catalog_collections`` row before the fan-out.
        """
        from nexus.corpus import write_with_registration_retry  # noqa: PLC0415 — circular-dep avoidance (corpus)
        r = write_with_registration_retry(
            new,
            lambda: self._post("/rename_collection", {"old": old, "new": new}),
            registrar=self._catalog_registrar,
        )
        return int(r.get("updated", 0))

    # ── ETL import ────────────────────────────────────────────────────────────

    def import_queue_row(self, body: dict[str, Any]) -> int:
        """Fidelity-preserving import for ETL. Returns 1 on success."""
        r = self._post("/import", body)
        return int(r.get("imported", 0))

    def import_queue_batch(self, rows: list[dict[str, Any]]) -> int:
        """RDR-176 P3 (bead nexus-t9rmg.18): GUC-once bulk queue import — POST all
        *rows* to /v1/aspects/queue/import in ONE request."""
        if not rows:
            return 0
        r = self._post("/import", {"rows": rows})
        return int(r.get("imported", 0))
