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
  catalog_doc_id="")`` per batch. Default consumers: taxonomy assign,
  manifest write.
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
import time
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
        self._batch_with_manifest_complete: set[int] = set()
        self._document: list[Callable[..., None]] = []
        self._document_with_doc_id: set[int] = set()

    def clear(self) -> None:
        """Drop every registration in all three chains. Useful for tests
        that need to assert specific hooks in isolation against an
        otherwise pre-populated registry."""
        self._single.clear()
        self._batch.clear()
        self._batch_with_catalog_doc_id.clear()
        self._batch_with_manifest_complete.clear()
        self._document.clear()
        self._document_with_doc_id.clear()

    # ── Single-doc chain ─────────────────────────────────────────────────────

    def register_single(self, fn: Callable[[str, str, str], None]) -> None:
        """Register a ``fn(doc_id, collection, content)`` callable to
        fire once per document. Mirrors the legacy
        ``register_post_store_hook``."""
        self._single.append(fn)

    def has_single_hooks(self) -> bool:
        """Would :meth:`fire_single` invoke any hook? (nexus-itpdc)"""
        return bool(self._single)

    def has_document_hooks(self) -> bool:
        """Would :meth:`fire_document` invoke any hook? (nexus-itpdc)"""
        return bool(self._document)

    def fire_single(
        self,
        doc_id: str,
        collection: str,
        content: str,
        *,
        invoke: Callable[[Callable, tuple, dict], Any] | None = None,
    ) -> None:
        """Invoke every single-doc hook. Per-hook exceptions are caught,
        logged at WARNING, and persisted to T2 ``hook_failures``; never
        propagated to the caller.

        *invoke* (nexus-eslkl) — optional interception seam. When supplied,
        each hook is called as ``invoke(hook, (doc_id, collection, content),
        {})`` instead of directly; :class:`LockedHookRegistry` uses this to
        wrap each hook's own invocation in a per-hook lock without this
        class needing to know anything about locking. ``None`` (default)
        preserves the exact prior direct-call behavior.
        """
        for hook in self._single:
            try:
                call_args = (doc_id, collection, content)
                if invoke is not None:
                    invoke(hook, call_args, {})
                else:
                    hook(*call_args)
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
            has_var_kw = any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in params.values()
            )
            if "catalog_doc_id" in params or has_var_kw:
                self._batch_with_catalog_doc_id.add(id(fn))
            # nexus-5xn3k.4 (RUNFENCE): hooks declaring ``manifest_complete``
            # receive the producer's per-doc completeness assertion — same
            # registration-time classification as catalog_doc_id above.
            if "manifest_complete" in params or has_var_kw:
                self._batch_with_manifest_complete.add(id(fn))
        except (TypeError, ValueError):
            # Builtin/C-extension callable with no introspectable
            # signature. Treat as legacy shape so the dispatcher does not
            # blow up on first call.
            _log.debug(
                "post_store_batch_hook_signature_unintrospectable",
                hook=getattr(fn, "__name__", repr(fn)),
            )

    def has_batch_hooks(self, grain: str = "all") -> bool:
        """Would :meth:`fire_batch` invoke ANY hook at this *grain*?

        Mirrors :meth:`fire_batch`'s grain filter exactly (same
        ``getattr(hook, "batch_grain", "file")`` default, same ``"all"``
        wildcard). Lets a caller skip work that a zero-match dispatch
        would make pointless — notably :class:`LockedHookRegistry`,
        which must not serialize a call that fires nothing (nexus-itpdc).
        """
        if grain == "all":
            return bool(self._batch)
        return any(
            getattr(hook, "batch_grain", "file") == grain
            for hook in self._batch
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
        manifest_complete: dict[str, str] | None = None,
        hook_timings: dict[str, float] | None = None,
        invoke: Callable[[Callable, tuple, dict], Any] | None = None,
    ) -> None:
        """Invoke every batch hook with the recorded call shape.

        Empty ``doc_ids`` returns early — no hooks fire on empty batches
        (matches the legacy dispatcher's semantics; taxonomy assign
        and manifest write both early-return on empty inputs anyway).

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

        *manifest_complete* (nexus-5xn3k.4, RUNFENCE) — ``{doc_id:
        content_hash}`` for documents the PRODUCER asserts are WHOLLY
        contained in this batch (file-atomic). The manifest hook uses it
        to ride ``write_manifest_many``'s fail-closed completion stamp in
        the same round trip. A producer that fires a document across
        multiple batches (the streaming PDF pipeline) must NOT claim it
        here — a partial batch stamped 'complete' rebuilds the exact
        silent-truncation bug the fence exists to close; multi-batch
        documents use the explicit ``/index-run/complete`` call instead.
        Threaded only to hooks that declare the parameter.

        *hook_timings* (nexus-lde88 G4) — when supplied, per-hook wall
        seconds are ACCUMULATED into ``hook_timings[hook.__name__]``
        (``+=``, not overwrite — a caller reusing one dict across several
        ``fire_batch`` calls gets a running total). Optional: ``None``
        (default) skips timing entirely, no ``time.monotonic()`` calls
        paid on the hot path unless a caller asks. This is how the
        flush-grain bucket (taxonomy + manifest, both ``batch_grain =
        "flush"``) can be split back out by hook name instead of staying
        one merged total.

        *invoke* (nexus-eslkl) — optional interception seam, same contract
        as :meth:`fire_single`'s. Called as ``invoke(hook, call_args,
        kwargs)`` in place of ``hook(*call_args, **kwargs)`` when supplied.
        This is what lets :class:`LockedHookRegistry` serialize each
        registered hook under its OWN lock (rather than one lock around the
        whole dispatch) without duplicating this method's kwarg-shape and
        per-hook-failure-isolation logic.
        """
        if not doc_ids:
            return
        for hook in self._batch:
            if grain != "all" and getattr(hook, "batch_grain", "file") != grain:
                continue
            hook_name = getattr(hook, "__name__", "?")
            _t0 = time.monotonic() if hook_timings is not None else 0.0
            try:
                call_args = (doc_ids, collection, contents, embeddings, metadatas)
                kwargs: dict = {}
                if id(hook) in self._batch_with_catalog_doc_id:
                    kwargs["catalog_doc_id"] = catalog_doc_id
                if (manifest_complete is not None
                        and id(hook) in self._batch_with_manifest_complete):
                    kwargs["manifest_complete"] = manifest_complete
                if invoke is not None:
                    invoke(hook, call_args, kwargs)
                else:
                    hook(*call_args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash caller
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
            finally:
                if hook_timings is not None:
                    hook_timings[hook_name] = (
                        hook_timings.get(hook_name, 0.0) + (time.monotonic() - _t0)
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
        invoke: Callable[[Callable, tuple, dict], Any] | None = None,
    ) -> None:
        """Invoke every document hook. Synchronous dispatch — no
        ``asyncio.to_thread``, no ``await``. Per-hook exceptions caught,
        logged, and persisted to T2 ``hook_failures`` with
        ``chain='document'``; never propagated.

        *invoke* (nexus-eslkl) — optional interception seam, same contract
        as :meth:`fire_single`'s.
        """
        for hook in self._document:
            try:
                call_args = (source_path, collection, content)
                kwargs = {"doc_id": doc_id} if id(hook) in self._document_with_doc_id else {}
                if invoke is not None:
                    invoke(hook, call_args, kwargs)
                else:
                    hook(*call_args, **kwargs)
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
        manifest_complete: dict[str, str] | None = None,
        invoke: Callable[[Callable, tuple, dict], Any] | None = None,
    ) -> None:
        """Fire all three post-store hook chains for a batch of just-stored
        docs. Single, batch, and document-grain chains run in that order.
        Errors caught per-hook and persisted; nothing propagated.

        *invoke* (nexus-eslkl) — threaded through unchanged to all three
        internal fire_* calls; see :meth:`fire_single`'s docstring.

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

        *manifest_complete* (nexus-cotmr / nexus-tafjk): threaded straight
        through to the internal :meth:`fire_batch` call — see that
        method's own docstring for the ``{doc_id: content_hash}`` contract
        and the file-atomicity precondition. Callers that fence their own
        ``_fence_begin`` before the vector put (single-chunk, file-atomic
        producers — CLI ``nx store put`` / ``nx memory promote``, mirroring
        MCP ``store_put``'s F2 pattern) pass this so the completion stamp
        rides the SAME round trip the manifest hook already pays for, no
        extra call. Callers that do not fence (``nx store import``'s
        multi-chunk-per-doc grouped batches; the ``collection.py`` re-embed
        path, which passes no ``catalog_doc_id`` at all) simply omit it —
        default ``None`` preserves their exact prior behavior.
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
            self.fire_single(doc_id, collection, content, invoke=invoke)

        # Conditional kwarg (never passed as an explicit ``None``):
        # ``self.fire_batch`` is a virtual dispatch — a caller-installed
        # ``HookRegistry`` subclass with a narrower, pre-existing
        # ``fire_batch`` override (test doubles; nothing in production
        # subclasses this) may not declare ``manifest_complete`` in its
        # signature at all. Passing the kwarg unconditionally (even as
        # ``None``) would raise ``TypeError`` on every such override,
        # not just the ones this feature actually targets. Mirrors
        # ``fire_batch``'s own internal per-hook conditional-kwarg
        # dispatch one level down.
        batch_kwargs: dict = {}
        if manifest_complete is not None:
            batch_kwargs["manifest_complete"] = manifest_complete
        self.fire_batch(
            doc_ids, collection, contents,
            embeddings=embeddings, metadatas=metadatas,
            catalog_doc_id=catalog_doc_id,
            invoke=invoke,
            **batch_kwargs,
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
            self.fire_document(sp, collection, content, doc_id=catalog_doc_id, invoke=invoke)


# ── Default-hooks factory ────────────────────────────────────────────────────


def install_default_hooks(registry: HookRegistry) -> None:
    """Register the load-bearing default consumers on *registry*.

    Two batch hooks + one document hook were previously self-registered
    at module load in ``nexus.mcp_infra`` (the batch pair) and
    ``nexus.mcp.core`` (the aspect-extraction enqueue). Without these
    consumers the catalog manifest, taxonomy assignments, and
    aspect-extraction queue all silently fall out of sync with every
    storage event. (The chash dual-write consumer was retired by RDR-187:
    the chunks tables are the chash store.)

    Idempotent: re-registering the same callable on the same registry
    is a no-op (duplicate-registration detection by identity).
    """
    from nexus.mcp_infra import (  # noqa: PLC0415 — deferred to avoid circular import
        manifest_write_batch_hook,
        taxonomy_assign_batch_hook,
    )

    # RDR-187 (nexus-piwya.4): the chash dual-write hook is retired — the
    # chunks tables are the chash store; nothing to dual-write.
    for hook in (
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


def record_catalog_hook_failure(
    *,
    source_path: str,
    collection: str,
    hook_name: str,
    error: str,
) -> None:
    """Public entry point for the CATALOG post-store hooks (nexus-ou4tb).

    The catalog registration hooks live outside this module (``indexer``,
    ``catalog.store_hook``, ``pipeline_stages``) but fail in exactly the shape
    :func:`_record_document_hook_failure` records, and their failures were
    previously logged at DEBUG and nowhere else — so a document could land in
    T3 and never be registered in the catalog, with no doc_id, no manifest and
    no links, and nobody told. Only a rebuild recovers it.

    Best-effort by construction: recording a failure must never turn a
    degraded-but-working index run into a crash.
    """
    _record_document_hook_failure(
        source_path=source_path, collection=collection,
        hook_name=hook_name, error=error,
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
    """Serialize the fire methods of a :class:`HookRegistry` under PER-HOOK
    locks (nexus-eslkl narrowing of the nexus-cfc72 single process-wide
    mutex this class used to hold).

    The nexus-cfc72 bounded file-level indexing concurrency runs 2+ flush
    workers at once; the hook chains they fire (manifest write, taxonomy
    assign, aspect enqueue) were written for the sequential loop and are
    not all safe under interleaving. The ORIGINAL fix serialized every
    hook's fire behind ONE lock — correct, but coarser than the hazard: it
    also serialized hooks that do not interleave with each other at all
    (measured: ~90s of a 104s wait was queueing behind a DIFFERENT hook's
    round trip, not the same one — see the nexus-eslkl design memo, T2
    ``nexus/design-eslkl-hook-lock-narrowing``).

    Per-hook locking (keyed on the hook callable's identity) means two
    DIFFERENT registered hooks never wait on each other, while concurrent
    fires of the SAME hook still fully serialize — exactly the granularity
    the interleaving audit found necessary: cross-hook interleaving is
    either provably safe (taxonomy: server-side idempotent, transactional
    ``assignFromChashes``) or the single remaining live hazard (manifest:
    ``_sweep_superseded_vectors``'s client-side read-modify-write over
    globally-shared chashes, tracked open at nexus-11gh6 / nexus-wxjr6 —
    the engine-side fold narrows but does not yet close the TOCTOU window,
    so the manifest hook keeps its lock).

    A hook opts OUT of serialization entirely by declaring
    ``hook.serialize = False`` at the module level, next to its
    ``batch_grain`` declaration — the same idiom, so the safety claim sits
    beside the evidence for it. Declaring nothing defaults to LOCKED: a
    new hook is safe by construction, never silently unserialized. Do NOT
    set this on a hook whose safety argument is not written down at its
    declaration site.

    CORRECTED (nexus-eslkl, was stale on ``resolve_index_concurrency``):
    the prior "this lock is what makes a diverging T2 memory backend safe"
    rationale is dead code-provably — ``storage_backend_for()`` has
    resolved to exactly one backend (``StorageBackend.SERVICE``) since
    RDR-158 P3, so no supported configuration produces a divergent T2
    backend, and every T2 write from every hook goes through
    ``mcp_infra._service_t2_lock`` regardless of this class. The real
    remaining constraint this class protects is the manifest hook's
    client-side sweep sequence, not the T2 backend question.

    Two consequences are load-bearing (unchanged from the coarse-lock
    version, now applied per-hook rather than per-dispatch):

    * A fire that would invoke ZERO matching hooks touches no lock at
      all — the dispatch loop in :meth:`HookRegistry.fire_batch` /
      ``fire_single`` / ``fire_document`` simply has nothing to iterate,
      so :meth:`_invoke` is never called. :meth:`fire_batch` still
      pre-checks the grain via :meth:`HookRegistry.has_batch_hooks`
      OUTSIDE any lock so a zero-match dispatch skips even the delegate
      call (nexus-itpdc); with per-hook locks this is a minor efficiency
      nicety rather than a correctness requirement, since an empty match
      set can never acquire a lock regardless.
    * Concurrent fires of the SAME hook still fully serialize via that
      hook's own lock — :attr:`lock_wait_seconds` aggregates the wait
      across every hook's lock (not just one), so the existing
      ``index_chunk_batch_stats`` consumer (``indexer.py``, reads this
      attribute via ``getattr(hooks, "lock_wait_seconds", 0.0)``) keeps
      working unchanged.
    """

    def __init__(self, registry: HookRegistry) -> None:
        import threading  # noqa: PLC0415 — only needed by this concurrency proxy

        self._registry = registry
        # One lock per registered hook, keyed on the hook callable's
        # identity. Lazily created (a hook may be registered after this
        # wrapper is constructed — install_default_hooks(locked) passes
        # register_* straight through via __getattr__), guarded by
        # _locks_meta_lock so two threads racing to create the FIRST lock
        # for the same hook cannot install two different Lock objects.
        self._hook_locks: dict[int, threading.Lock] = {}
        self._locks_meta_lock = threading.Lock()
        # Seconds spent WAITING to acquire a hook's lock, summed across
        # every contending thread AND every hook (so it can exceed wall
        # clock, like the other per-grain hook totals). Mutated under
        # _stats_lock rather than under whichever hook's lock happened to
        # be acquired, since two DIFFERENT hooks' locks can be held by two
        # different threads at once now — unlike the single-lock version,
        # where the lock being measured was also the lock protecting the
        # measurement.
        self._stats_lock = threading.Lock()
        self.lock_wait_seconds = 0.0

    def _lock_for(self, hook: Callable) -> "threading.Lock":
        import threading  # noqa: PLC0415 — only needed by this concurrency proxy

        key = id(hook)
        lock = self._hook_locks.get(key)
        if lock is not None:
            return lock
        with self._locks_meta_lock:
            lock = self._hook_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._hook_locks[key] = lock
            return lock

    def _invoke(self, hook: Callable, args: tuple, kwargs: dict) -> Any:
        """The ``invoke`` seam :class:`HookRegistry`'s fire_* methods call
        instead of ``hook(*args, **kwargs)`` directly. Opted-out hooks
        (``hook.serialize is False``) run completely unlocked; every other
        hook serializes against concurrent fires of ITSELF only."""
        if getattr(hook, "serialize", True) is False:
            return hook(*args, **kwargs)
        lock = self._lock_for(hook)
        _t0 = time.monotonic()
        lock.acquire()
        try:
            with self._stats_lock:
                self.lock_wait_seconds += time.monotonic() - _t0
            return hook(*args, **kwargs)
        finally:
            lock.release()

    def fire_single(self, *args: Any, **kwargs: Any) -> None:
        # Zero-match fast path — and the one that matters MOST by call
        # count: the batched indexer fires this once per CHUNK (~2000 per
        # run), and the default hook set registers no single-grain hook
        # at all.
        if not self._registry.has_single_hooks():
            return
        self._registry.fire_single(*args, invoke=self._invoke, **kwargs)

    def fire_batch(self, *args: Any, **kwargs: Any) -> None:
        # Zero-match fast path (nexus-itpdc): a grain with no registered
        # hook fires nothing, so there is nothing to lock or dispatch.
        if not self._registry.has_batch_hooks(kwargs.get("grain", "all")):
            return
        self._registry.fire_batch(*args, invoke=self._invoke, **kwargs)

    def fire_document(self, *args: Any, **kwargs: Any) -> None:
        if not self._registry.has_document_hooks():
            return
        self._registry.fire_document(*args, invoke=self._invoke, **kwargs)

    def fire_store_chains(self, *args: Any, **kwargs: Any) -> None:
        self._registry.fire_store_chains(*args, invoke=self._invoke, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._registry, name)
