# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""HttpAspectQueue — the producer side of the engine's ``aspects`` tuple queue.

One task per document, written by ``out`` ``mode=replace`` on the identity
body (RDR-235): :meth:`HttpAspectQueue.enqueue` and :meth:`enqueue_many`, plus
the census the diagnostics read (:meth:`HttpAspectQueue.tuple_counts`,
:func:`aspects_census`). The consumer is the aspect worker
(``nexus.aspect_worker_tuples``); the ``aspect_extraction_queue`` table and its
``/v1/aspects/queue/*`` routes are retired (nexus-hbnml, RDR-235).

Config:
    NX_SERVICE_HOST  — service host (default: 127.0.0.1)
    NX_SERVICE_PORT  — service port (required; raises if missing)
    NX_SERVICE_TOKEN — bearer token (required; raises if missing)

The ``rename_lock`` constructor parameter is accepted for constructor parity
with the other T2 stores and ignored (no Python-side threading; the engine owns
all serialization).
"""
from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

_log = structlog.get_logger(__name__)

DEFAULT_TENANT: str = "default"

# ── RDR-235: the producer behind NX_ASPECT_QUEUE ─────────────────────────────

#: The variable that once selected the producer. ``tuples`` (or unset) is the only
#: producer. ``table`` is retired (nexus-bpikw, table dropped by nexus-hbnml): the
#: producer paths warn once and use tuples (:func:`producer_queue_mode`); the
#: operator surface refuses it (:func:`aspect_queue_mode`,
#: :class:`AspectQueueTableModeRetiredError`).
ASPECT_QUEUE_ENV: str = "NX_ASPECT_QUEUE"
ASPECT_QUEUE_MODES: tuple[str, ...] = ("tuples",)

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
_warned_table_retired: list[bool] = []


class AspectQueueTableModeRetiredError(RuntimeError):
    """``NX_ASPECT_QUEUE=table`` was asked for after the table producer was retired
    (nexus-bpikw, RDR-235). The tuple queue is the only producer."""


def _env_mode() -> str:
    """The variable as written: ``"tuples"`` (unset, blank or ``tuples``), ``"table"``,
    or ``"tuples"`` for any other value after one warning per process (a typo such as
    ``tuple`` must not fail every ingest hook). Read at call time."""
    raw = os.environ.get(ASPECT_QUEUE_ENV, "").strip().lower()
    if raw in ("", "tuples"):
        return "tuples"
    if raw == "table":
        return "table"
    if raw not in _warned_unknown_modes:
        _warned_unknown_modes.add(raw)
        _log.warning(
            "aspect_queue_mode_unknown_falling_back_to_tuples",
            env=ASPECT_QUEUE_ENV, value=raw, allowed=list(ASPECT_QUEUE_MODES),
        )
    return "tuples"


def aspect_queue_mode() -> str:
    """The STRICT reader, for the operator surface (``nx aspects requeue-failed``):
    ``"tuples"``, or
    :class:`AspectQueueTableModeRetiredError` for ``NX_ASPECT_QUEUE=table``. An
    operator who typed the retired value is told so and nothing runs, instead of a
    quiet reroute.

    The producer paths do not call this: they use :func:`producer_queue_mode`, which
    never drops a document over a stale value. The worker calls neither."""
    if _env_mode() == "table":
        raise AspectQueueTableModeRetiredError(
            f"{ASPECT_QUEUE_ENV}=table is retired: the aspect queue table and its producer "
            "were removed (nexus-bpikw, nexus-hbnml, RDR-235) and the engine's 'aspects' tuple "
            f"queue is the only queue. Unset {ASPECT_QUEUE_ENV} (or set it to 'tuples')."
        )
    return "tuples"


def producer_queue_mode() -> str:
    """The PRODUCER reader (``HttpAspectQueue.enqueue`` and ``enqueue_many``, reached
    from the indexer's flush and the MCP and single-document hooks): always
    ``"tuples"``. A stale ``NX_ASPECT_QUEUE=table`` (a shell profile, an MCP server
    config) logs one warning per process saying to unset it and uses tuples; a
    refusal here would drop the documents of every ingest until someone found the
    variable (nexus-bpikw design decision D1)."""
    if _env_mode() == "table" and not _warned_table_retired:
        _warned_table_retired.append(True)
        _log.warning(
            "aspect_queue_table_mode_retired_using_tuples",
            env=ASPECT_QUEUE_ENV, bead="nexus-bpikw",
            action=f"unset {ASPECT_QUEUE_ENV}; the aspects tuple queue is the only producer",
        )
    return "tuples"


@dataclass(frozen=True)
class AspectTupleCounts:
    """The ``aspects`` subspace census, reduced to what the diagnostics need
    (RDR-235 F18). ``available`` is pending work, ``dead`` is the failed count,
    and ``claimed`` is in flight: it is NOT pending and NOT failed. A task
    waiting out a retry delay is counted in ``available`` (the census does not
    render ``available_at``), so ``available`` is an upper bound on what is
    claimable this second."""

    available: int
    claimed: int
    dead: int


#: The one-line remedy printed beside any row the engine kept from the retired queue table
#: (nexus-hbnml). The runbook section has the owner-role SQL recipe.
RETIRED_TABLE_REMEDY: str = (
    "rows the engine could not move to the aspects queue are stranded in nexus.aspect_extraction_queue: "
    "`nx enrich aspects <collection>` re-extracts documents that lack aspects, and deleting the rows "
    "(as the table owner) lets the next engine start drop the table; "
    "https://github.com/Hellblazer/nexus/blob/main/docs/migration-runbook.md#the-retired-aspect-queue-tuples-rung-and-the-aspect-queue-table"
)


@dataclass(frozen=True)
class RetiredQueueTable:
    """What the engine says about the retired ``nexus.aspect_extraction_queue`` table
    (``GET /v1/status``, ``aspect_queue_table_present`` / ``aspect_queue_rows_retained``).

    ``state`` is one of: ``gone`` (no table), ``empty`` (present, no rows: the next engine
    start drops it), ``retained`` (``rows`` > 0 stranded rows), ``uncounted`` (present and
    its rows could not be counted) or ``unreported`` (the status could not be read, or the
    engine predates the field or has not run its start-up drain yet). Only ``gone`` and
    ``empty`` are clean: a caller must never render the other three as a zero."""

    state: str
    rows: int | None = None
    reasons: tuple[tuple[str, int], ...] = ()

    @property
    def clean(self) -> bool:
        return self.state in ("gone", "empty")


def retired_queue_table(status: Any) -> RetiredQueueTable:
    """Read the retired-table fields out of an engine ``/v1/status`` body (``None`` when it
    could not be fetched)."""
    if not isinstance(status, dict):
        return RetiredQueueTable("unreported")
    present = status.get("aspect_queue_table_present")
    rows = status.get("aspect_queue_rows_retained")
    if not isinstance(present, bool) or isinstance(rows, bool) or not isinstance(rows, int):
        return RetiredQueueTable("unreported")
    if not present:
        return RetiredQueueTable("gone", 0)
    if rows < 0:
        return RetiredQueueTable("uncounted", None)
    raw = status.get("aspect_queue_rows_retained_reasons")
    reasons = tuple(sorted((str(k), int(v)) for k, v in raw.items())) if isinstance(raw, dict) else ()
    if rows == 0:
        return RetiredQueueTable("empty", 0)
    return RetiredQueueTable("retained", rows, reasons)


class AspectQueueEngineUnsupportedError(RuntimeError):
    """The engine has no ``aspects`` tuple template (it predates
    ``engine-service-v0.1.169``)."""


_warned_no_aspects_template: list[bool] = []


def aspects_census(store: Any, *, timeout: float | None = None, single_shot: bool = False) -> Any:
    """The ``aspects`` subspace's census from *store* (an ``HttpTupleStore``),
    or ``None`` when the engine has no ``aspects`` template.

    The readers (queue-depth hook, drain, requeue-failed, doctor) count this
    side. An engine that predates ``engine-service-v0.1.169`` has no such
    template: ``None`` means "no tuple side", the caller reads it as zero, and
    one log line per process says so. Any other
    failure propagates; a caller decides what an unreadable side means.

    *timeout* caps the one request (seconds); ``None`` sends it unchanged. *single_shot*
    (nexus-g6ic6) sends it once: no ``ReadTimeout`` resend, no gateway backoff."""
    from nexus.db.t2.http_tuple_store import UnknownSubspaceError  # noqa: PLC0415 — deferred: keeps this module's import light

    try:
        if timeout is None and not single_shot:
            return store.subspace_stats(ASPECTS_SUBSPACE)
        kwargs: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        if single_shot:
            kwargs["single_shot"] = True
        return store.subspace_stats(ASPECTS_SUBSPACE, **kwargs)
    except UnknownSubspaceError:
        if not _warned_no_aspects_template:
            _warned_no_aspects_template.append(True)
            _log.info(
                "aspects_tuple_template_absent",
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


class HttpAspectQueue(RawHandleGuardMixin, RefreshableHttpStoreMixin):
    """The producer of the engine's ``aspects`` tuple queue.

    Uses a keep-alive :class:`httpx.Client` connection pool via
    :class:`~nexus.db.t2._refreshable_client.RefreshableHttpStoreMixin`,
    which resolves ``NX_SERVICE_HOST``, ``NX_SERVICE_PORT``, and
    ``NX_SERVICE_TOKEN`` (or a managed ``service_url``/``service_token``)
    fresh on construction AND self-heals (re-resolve + retry once) on a
    401 or a connection-refused/reset — see the mixin's own docstring for
    the full resolution order.

    The ``rename_lock`` parameter is accepted for constructor parity (T2Database
    injects it). It is ignored — no Python-side threading guards are needed when
    the engine owns all queue state. ``timeout`` is this store's own public constructor kwarg
    (pre-dating the mixin); it is threaded through to the mixin's
    ``__init__`` unchanged so callers that pin a non-default HTTP timeout
    keep working identically post-adoption.

    Args:
        base_url:    Optional override for the service base URL.
        tenant:      Tenant to stamp on every request (default: ``DEFAULT_TENANT``).
        rename_lock: Accepted for constructor parity; NOT used (no-op).
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
        # RDR-235: the construction arguments the tuple store is built from
        # (lazily, on first use), so it resolves its endpoint and token and
        # self-heals exactly as this store does.
        self._ctor_base_url = base_url
        self._ctor_token = _token
        self._tuple_store: Any = None
        self._tuple_store_lock = threading.Lock()
        # rename_lock accepted for constructor parity but ignored over HTTP
        # (verbatim behavior preserved from the pre-mixin constructor).
        self.rename_lock: threading.RLock = (
            rename_lock if rename_lock is not None else threading.RLock()
        )

    # ── Public API ────────────────────────────────────────────────────────────

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
        """Queue a document for aspect extraction.

        Writes one ``aspects`` task, replaced by ``doc_id`` (RDR-235): a re-enqueue
        replaces the task and never adds a second.
        """
        if not collection:
            raise ValueError("collection must not be empty")
        if not source_path:
            raise ValueError("source_path must not be empty")
        producer_queue_mode()  # warns once about a stale NX_ASPECT_QUEUE=table
        # RDR-235 E2: the identity goes out as a replace; ``content`` is
        # dropped (the worker re-reads the document).
        self._enqueue_tuple(collection, source_path, content_hash, doc_id)
        self._signal_wake()

    def enqueue_many(self, rows: list[dict]) -> int:
        """Batch-enqueue N documents: the bounded tuple fan-out
        (:meth:`_enqueue_many_tuples`), one ``out`` per document.

        Each dict carries the same fields as :meth:`enqueue`'s keyword
        arguments (``collection``, ``source_path``, ``doc_id`` and optionally
        ``content_hash``). Returns the count of tasks written; a malformed row
        is skipped and counted in one warning. A batch the engine refuses
        because a collection it names is retired
        (:class:`~nexus.collection_errors.SupersededCollectionWriteError`) is
        raised as is: the refusal is deterministic.
        """
        if not rows:
            return 0
        producer_queue_mode()  # warns once about a stale NX_ASPECT_QUEUE=table
        return self._enqueue_many_tuples(rows)

    # ── RDR-235: the tuple producer ───────────────────────────────────────────

    def _tuples(self) -> Any:
        """The ``HttpTupleStore`` the producer writes through, built on first use
        over this store's own keep-alive pool."""
        with self._tuple_store_lock:
            if self._tuple_store is None:
                from nexus.db.t2.http_tuple_store import HttpTupleStore  # noqa: PLC0415 — deferred: keeps this module's import light

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
        from nexus.db.t2.http_tuple_store import UnknownSubspaceError  # noqa: PLC0415 — deferred: keeps this module's import light

        body = _identity_body(collection, source_path, doc_id, content_hash)
        try:
            _id, outcome = self._tuples().out_replace(
                ASPECTS_SUBSPACE, dict(ASPECTS_KEYS), None, body, nonce=doc_id,
            )
        except UnknownSubspaceError as exc:
            raise AspectQueueEngineUnsupportedError(
                f"the 'aspects' tuple template is missing from the engine, which "
                f"predates {ASPECTS_ENGINE_RELEASE}. Upgrade the engine."
            ) from exc
        return outcome

    def _enqueue_tuple(
        self, collection: str, source_path: str, content_hash: str, doc_id: str,
    ) -> None:
        if not doc_id:
            raise ValueError(
                "doc_id must not be empty: it is the task's identity"
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
        from nexus.db.t2.http_tuple_store import TooLargeError  # noqa: PLC0415 — deferred: keeps this module's import light

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

    def tuple_counts(self) -> AspectTupleCounts:
        """The tuple queue's side of the depth, for ``nx doctor`` and the console
        (RDR-235 F18). Zeros when the ``aspects`` subspace does not exist yet
        (an engine without the template, or a tenant that never enqueued in
        ``tuples`` mode): nothing is queued there, so zero is true. Any other
        failure propagates: the caller must report that side UNKNOWN, never
        zero. The absent-template rule lives in :func:`aspects_census`, the
        one reader every table-side consumer shares."""
        c = aspects_census(self._tuples())
        if c is None:
            return AspectTupleCounts(0, 0, 0)
        return AspectTupleCounts(c.available, c.claimed, c.dead)

    def close(self) -> None:
        store, self._tuple_store = self._tuple_store, None
        if store is not None:
            store.close()
        super().close()
