# SPDX-License-Identifier: AGPL-3.0-or-later
"""Three-chain post-store hook registry (RDR-118 successor / scrap follow-up).

Replaces the six module-level mutables that used to live in
``nexus.mcp_infra`` (``_post_store_hooks``, ``_post_store_batch_hooks``,
``_post_store_batch_hooks_with_catalog_doc_id``, ``_post_document_hooks``,
``_post_document_hooks_with_doc_id``) plus their dispatchers
(``register_post_store_hook``, ``fire_post_store_hooks``, and the two
parallel pairs). Entry points construct one ``HookRegistry``, call
:func:`install_default_hooks` to attach the load-bearing default
consumers, and thread the instance through the indexing / storage
pipeline. Tests construct their own ``HookRegistry`` per test.

Three chains, three shapes:

* **single** (RDR-070) — ``fn(doc_id, collection, content)`` per
  document. Currently empty by default — registered ad-hoc.
* **batch** (RDR-095) —
  ``fn(doc_ids, collection, contents, embeddings, metadatas, *,
  catalog_doc_id="")`` per batch. Default consumers: chash dual-write,
  taxonomy assign, manifest write.
* **document** (RDR-089) —
  ``fn(source_path, collection, content, *, doc_id="")`` per source
  document. Default consumer: aspect-extraction enqueue.

Per-hook failure isolation + T2 ``hook_failures`` persistence semantics
are preserved verbatim from the legacy dispatchers. The
``_record_*_hook_failure`` helpers live here (moved from ``mcp_infra``)
and use the same ``t2_ctx()`` accessor so existing tests that
monkeypatch ``nexus.mcp_infra.t2_ctx`` keep working unchanged.
"""
from __future__ import annotations

import inspect
import json
from typing import Any, Callable

import structlog


__all__ = [
    "HookRegistry",
    "install_default_hooks",
]


_log = structlog.get_logger(__name__)


# ── HookRegistry ─────────────────────────────────────────────────────────────


class HookRegistry:
    """Three-chain post-store hook registry. Constructor-injected.

    Entry points (CLI commands, MCP tools, tests) instantiate one
    registry per logical invocation, call :func:`install_default_hooks`
    to attach the load-bearing batch + document hooks, and pass the
    instance down through the indexing pipeline. The pipeline calls
    :meth:`fire_single`, :meth:`fire_batch`, and :meth:`fire_document`
    on the threaded instance instead of on module-level globals.

    Per-hook failure isolation: a single hook raising does not block
    other hooks from firing. Failures are logged at WARNING and
    persisted to T2 ``hook_failures`` for triage (``nx taxonomy
    status`` reads from there).

    Contract tightening from the legacy mcp_infra dispatcher
    (RDR-118 P2.S1b carryover): :meth:`register_document` raises
    ``TypeError`` on coroutine-returning callables. The legacy
    dispatcher accepted async hooks and silently dropped the returned
    coroutine at fire time (audit F1 silent-failure mode); registration
    surfaces the contract violation where the diagnostic points at the
    buggy caller.
    """

    def __init__(self) -> None:
        self._single: list[Callable[..., None]] = []
        self._batch: list[Callable[..., None]] = []
        self._batch_with_catalog_doc_id: set[int] = set()
        self._document: list[Callable[..., None]] = []
        self._document_with_doc_id: set[int] = set()

    def clear(self) -> None:
        """Drop every registration in all three chains. Useful for tests
        that need to assert specific hooks in isolation against an
        otherwise pre-populated registry."""
        self._single.clear()
        self._batch.clear()
        self._batch_with_catalog_doc_id.clear()
        self._document.clear()
        self._document_with_doc_id.clear()

    # ── Single-doc chain ─────────────────────────────────────────────────────

    def register_single(self, fn: Callable[[str, str, str], None]) -> None:
        """Register a ``fn(doc_id, collection, content)`` callable to
        fire once per document. Mirrors the legacy
        ``register_post_store_hook``."""
        self._single.append(fn)

    def fire_single(self, doc_id: str, collection: str, content: str) -> None:
        """Invoke every single-doc hook. Per-hook exceptions are caught,
        logged at WARNING, and persisted to T2 ``hook_failures``; never
        propagated to the caller."""
        for hook in self._single:
            try:
                hook(doc_id, collection, content)
            except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash caller
                hook_name = getattr(hook, "__name__", "?")
                _log.warning(
                    "post_store_hook_failed",
                    hook=hook_name,
                    exc_info=True,
                )
                _record_hook_failure(
                    doc_id=doc_id,
                    collection=collection,
                    hook_name=hook_name,
                    error=str(exc),
                )

    # ── Batch chain ──────────────────────────────────────────────────────────

    def register_batch(self, fn: Callable[..., None]) -> None:
        """Register a batch hook. Classifies whether the callable
        accepts ``catalog_doc_id`` at registration time so the dispatch
        in :meth:`fire_batch` picks the right call shape per hook
        (RDR-108 Phase 3 dual-shape contract)."""
        self._batch.append(fn)
        try:
            sig = inspect.signature(fn)
            params = sig.parameters
            if "catalog_doc_id" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in params.values()
            ):
                self._batch_with_catalog_doc_id.add(id(fn))
        except (TypeError, ValueError):
            # Builtin/C-extension callable with no introspectable
            # signature. Treat as legacy shape so the dispatcher does not
            # blow up on first call.
            _log.debug(
                "post_store_batch_hook_signature_unintrospectable",
                hook=getattr(fn, "__name__", repr(fn)),
            )

    def fire_batch(
        self,
        doc_ids: list[str],
        collection: str,
        contents: list[str],
        embeddings: list[list[float]] | None = None,
        metadatas: list[dict] | None = None,
        *,
        catalog_doc_id: str = "",
        grain: str = "all",
    ) -> None:
        """Invoke every batch hook with the recorded call shape.

        Empty ``doc_ids`` returns early — no hooks fire on empty batches
        (matches the legacy dispatcher's semantics; chash dual-write,
        taxonomy assign, and manifest write all early-return on empty
        inputs anyway).

        Per-hook exceptions are caught, logged at WARNING, and
        persisted to T2 ``hook_failures`` with ``chain='batch'``; never
        propagated to the caller.

        *catalog_doc_id* (RDR-108 Phase 3) — catalog ``Document.tumbler``
        for this batch's document. Required by ``manifest_write_batch_hook``
        post-Phase-3; the manifest hook can no longer derive it from
        chunk metadata.

        *grain* (nexus-duoak.7) — the duoak-2C batched indexer fires the
        chain twice at different aggregation levels: once per FILE (with
        that file's catalog_doc_id) and once per upload FLUSH (all files'
        chunks, no single doc identity). A hook declares its level via a
        ``batch_grain`` attribute (``"file"`` default, ``"flush"`` for
        file-agnostic consumers like taxonomy/chash whose per-call cost
        is round-trip-dominated). ``grain="all"`` (default) fires every
        hook regardless — every pre-existing caller (MCP store_put,
        legacy per-file indexing) is behaviorally unchanged.
        """
        if not doc_ids:
            return
        for hook in self._batch:
            if grain != "all" and getattr(hook, "batch_grain", "file") != grain:
                continue
            try:
                if id(hook) in self._batch_with_catalog_doc_id:
                    hook(
                        doc_ids, collection, contents, embeddings, metadatas,
                        catalog_doc_id=catalog_doc_id,
                    )
                else:
                    hook(doc_ids, collection, contents, embeddings, metadatas)
            except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash caller
                hook_name = getattr(hook, "__name__", "?")
                _log.warning(
                    "post_store_batch_hook_failed",
                    hook=hook_name,
                    exc_info=True,
                )
                _record_batch_hook_failure(
                    doc_ids=doc_ids,
                    collection=collection,
                    hook_name=hook_name,
                    error=str(exc),
                )

    # ── Document-grain chain ─────────────────────────────────────────────────

    def register_document(self, fn: Callable[..., None]) -> None:
        """Register a synchronous ``fn(source_path, collection, content)``
        callable.

        The synchronous-only contract is load-bearing for RDR-089
        aspect extraction; coroutine-returning callables would be
        silently dropped by the dispatcher. Registration raises
        ``TypeError`` on coroutine functions so the contract violation
        surfaces where the diagnostic points at the buggy caller.
        """
        if inspect.iscoroutinefunction(fn):
            raise TypeError(
                f"register_document(fn={getattr(fn, '__name__', repr(fn))}): "
                "async callables are not supported. The dispatcher fires "
                "synchronously and would drop the returned coroutine. "
                "Hooks that need async work must run their own event loop "
                "internally."
            )
        self._document.append(fn)
        try:
            sig = inspect.signature(fn)
            params = sig.parameters
            if "doc_id" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in params.values()
            ):
                self._document_with_doc_id.add(id(fn))
        except (TypeError, ValueError):
            _log.debug(
                "post_document_hook_signature_unintrospectable",
                hook=getattr(fn, "__name__", repr(fn)),
            )

    def fire_document(
        self,
        source_path: str,
        collection: str,
        content: str,
        *,
        doc_id: str = "",
    ) -> None:
        """Invoke every document hook. Synchronous dispatch — no
        ``asyncio.to_thread``, no ``await``. Per-hook exceptions caught,
        logged, and persisted to T2 ``hook_failures`` with
        ``chain='document'``; never propagated."""
        for hook in self._document:
            try:
                if id(hook) in self._document_with_doc_id:
                    hook(source_path, collection, content, doc_id=doc_id)
                else:
                    hook(source_path, collection, content)
            except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash caller
                hook_name = getattr(hook, "__name__", "?")
                _log.warning(
                    "post_document_hook_failed",
                    hook=hook_name,
                    source_path=source_path,
                    collection=collection,
                    exc_info=True,
                )
                _record_document_hook_failure(
                    source_path=source_path,
                    collection=collection,
                    hook_name=hook_name,
                    error=str(exc),
                )

    # ── Combined fire helper ─────────────────────────────────────────────────

    def fire_store_chains(
        self,
        doc_ids: list[str],
        collection: str,
        contents: list[str],
        *,
        source_paths: list[str] | None = None,
        embeddings: list[list[float]] | None = None,
        metadatas: list[dict] | None = None,
        catalog_doc_id: str = "",
    ) -> None:
        """Fire all three post-store hook chains for a batch of just-stored
        docs. Single, batch, and document-grain chains run in that order.
        Errors caught per-hook and persisted; nothing propagated.

        Used by MCP ``store_put`` and the CLI store-path entry points
        (``nx store put``, ``nx memory promote``, ``nx store import``).
        Bulk ``nx index *`` paths still call the three fire methods
        directly to preserve the existing per-batch shape.

        INVARIANT (nexus-w8lg1): ``catalog_doc_id`` is scoped to the
        WHOLE call — the document chain broadcasts it to every item.
        One call therefore covers ONE catalog document's chunks (or
        passes ``""``). Callers with a multi-document batch must group
        by document first (see ``exporter._fire_store_chains_grouped_by_doc``);
        passing a nonempty ``catalog_doc_id`` across mixed documents
        would mis-attribute every document's aspect-queue row.
        """
        n = len(doc_ids)
        if len(contents) != n:
            raise ValueError(
                f"contents length {len(contents)} != doc_ids length {n}"
            )
        if source_paths is None:
            source_paths = list(doc_ids)
        elif len(source_paths) != n:
            raise ValueError(
                f"source_paths length {len(source_paths)} != "
                f"doc_ids length {n}"
            )

        for doc_id, content in zip(doc_ids, contents):
            self.fire_single(doc_id, collection, content)

        self.fire_batch(
            doc_ids, collection, contents,
            embeddings=embeddings, metadatas=metadatas,
            catalog_doc_id=catalog_doc_id,
        )

        # nexus-w8lg1 (6.3.0 live shakeout finding #1): the document chain
        # carries the CATALOG doc_id (tumbler), never the T3 chunk id.
        # Passing ``did`` here shipped chunk_text_hash[:32] to the aspect
        # enqueue, violating the engine's composite FK
        # aspect_extraction_queue(tenant_id, doc_id) ->
        # catalog_documents(tenant_id, tumbler) — typed 409, aspects
        # silently lost on every CLI store put. "" persists as NULL,
        # which the nullable FK accepts. ``doc_ids`` is intentionally
        # unused here — chunk ids belong to the single/batch chains only.
        for sp, content in zip(source_paths, contents):
            self.fire_document(sp, collection, content, doc_id=catalog_doc_id)


# ── Default-hooks factory ────────────────────────────────────────────────────


def install_default_hooks(registry: HookRegistry) -> None:
    """Register the load-bearing default consumers on *registry*.

    Three batch hooks + one document hook were previously self-registered
    at module load in ``nexus.mcp_infra`` (the batch trio) and
    ``nexus.mcp.core`` (the aspect-extraction enqueue). Without these
    consumers the catalog manifest, chash index, taxonomy assignments,
    and aspect-extraction queue all silently fall out of sync with
    every storage event.

    Idempotent: re-registering the same callable on the same registry
    is a no-op (duplicate-registration detection by identity).
    """
    from nexus.mcp_infra import (  # noqa: PLC0415 — deferred to avoid circular import
        chash_dual_write_batch_hook,
        manifest_write_batch_hook,
        taxonomy_assign_batch_hook,
    )

    for hook in (
        chash_dual_write_batch_hook,
        taxonomy_assign_batch_hook,
        manifest_write_batch_hook,
    ):
        if hook not in registry._batch:
            registry.register_batch(hook)

    from nexus.aspect_worker import aspect_extraction_enqueue_hook  # noqa: PLC0415 — deferred to avoid circular import
    if aspect_extraction_enqueue_hook not in registry._document:
        registry.register_document(aspect_extraction_enqueue_hook)


# ── Failure-record helpers (moved from mcp_infra) ────────────────────────────


#: nexus-9613q.3: warn-once guard so a failed hook_failures persist (e.g. a
#: service 5xx) is VISIBLE rather than silently swallowed at DEBUG. Keyed on
#: ``(chain, hook_name)`` — NOT ``chain`` alone — so a transient failure of one
#: hook does not permanently silence every other hook of the same chain for the
#: process lifetime (nexus-9613q review M1).
_hook_failure_drop_warned: set[tuple[str, str]] = set()


def _persist_hook_failure(
    *,
    doc_id: str,
    collection: str,
    hook_name: str,
    error: str,
    chain: str,
    batch_doc_ids: str | None = None,
    is_batch: bool = False,
) -> None:
    """Persist one ``hook_failures`` row via the telemetry STORE.

    nexus-9613q.3: routes through ``db.telemetry.record_hook_failure(...)`` so
    the write works on both the SQLite and service backends. The prior code
    reached ``t2.taxonomy.conn`` directly, which a service-backed store lacks,
    silently dropping every row in service mode (the silent-loss class
    nexus-pyzk7 closed for tier_writes). Best-effort: recording an
    already-failing hook must never mask the original hook exception, but a
    persist failure is now WARNED ONCE per chain instead of swallowed at DEBUG.
    The store owns the column-set migration, so there is no per-caller
    INSERT fallback ladder anymore.
    """
    from nexus.mcp_infra import t2_ctx  # noqa: PLC0415 — deferred to avoid circular import

    try:
        with t2_ctx() as t2:
            t2.telemetry.record_hook_failure(
                doc_id=doc_id,
                collection=collection,
                hook_name=hook_name,
                error=error[:2000],
                chain=chain,
                batch_doc_ids=batch_doc_ids,
                is_batch=is_batch,
            )
    except Exception:  # noqa: BLE001 — best-effort: failure logged, must not crash caller
        key = (chain, hook_name)
        if key not in _hook_failure_drop_warned:
            _hook_failure_drop_warned.add(key)
            _log.warning(
                "hook_failure_persist_dropped",
                chain=chain,
                hook=hook_name,
                collection=collection,
                exc_info=True,
            )


def _record_hook_failure(
    *,
    doc_id: str,
    collection: str,
    hook_name: str,
    error: str,
) -> None:
    """Persist a single-doc post-store hook failure (``chain='single'``)."""
    _persist_hook_failure(
        doc_id=doc_id, collection=collection, hook_name=hook_name,
        error=error, chain="single",
    )


def _record_batch_hook_failure(
    *,
    doc_ids: list[str],
    collection: str,
    hook_name: str,
    error: str,
) -> None:
    """Persist a batch-shape post-store hook failure to T2 ``hook_failures``.

    Writes the JSON-encoded doc_id list to ``batch_doc_ids`` and sets
    ``is_batch=1``; stores a representative scalar (first doc_id) in
    the legacy ``doc_id`` column so existing scalar readers continue to
    render something meaningful (RDR-095 schema migration adds the two
    new columns in 4.14.1).

    """
    _persist_hook_failure(
        doc_id=doc_ids[0] if doc_ids else "",
        collection=collection, hook_name=hook_name, error=error,
        chain="batch", batch_doc_ids=json.dumps(doc_ids), is_batch=True,
    )


def _record_document_hook_failure(
    *,
    source_path: str,
    collection: str,
    hook_name: str,
    error: str,
) -> None:
    """Persist a document-grain hook failure to T2 ``hook_failures``.

    Stores ``source_path`` in the legacy ``doc_id`` column (the column
    carries 'subject of failure' regardless of chain shape) and sets
    ``chain='document'`` so readers can render the row appropriately.

    """
    _persist_hook_failure(
        doc_id=source_path, collection=collection, hook_name=hook_name,
        error=error, chain="document",
    )


# ── Serializing proxy for concurrent indexing (nexus-cfc72) ──────────────────


class LockedHookRegistry:
    """Serialize the fire methods of a :class:`HookRegistry` under one lock.

    The nexus-cfc72 bounded file-level indexing concurrency runs 2+ file
    pipelines at once; the hook chains they fire (manifest writes, chash
    ledger, taxonomy assign, aspect enqueue) were written for the
    sequential loop and are not audited for interleaving. Hooks are
    milliseconds against the multi-second embed/upsert work, so
    serializing them costs ~nothing and removes the question. Everything
    else (registration, attribute access) delegates to the wrapped
    registry unchanged.
    """

    def __init__(self, registry: HookRegistry) -> None:
        import threading  # noqa: PLC0415 — only needed by this concurrency proxy

        self._registry = registry
        self._lock = threading.Lock()

    def fire_single(self, *args: Any, **kwargs: Any) -> None:
        with self._lock:
            self._registry.fire_single(*args, **kwargs)

    def fire_batch(self, *args: Any, **kwargs: Any) -> None:
        with self._lock:
            self._registry.fire_batch(*args, **kwargs)

    def fire_document(self, *args: Any, **kwargs: Any) -> None:
        with self._lock:
            self._registry.fire_document(*args, **kwargs)

    def fire_store_chains(self, *args: Any, **kwargs: Any) -> None:
        with self._lock:
            self._registry.fire_store_chains(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._registry, name)
