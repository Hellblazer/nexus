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
import sqlite3
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
            except Exception as exc:
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
        """
        if not doc_ids:
            return
        for hook in self._batch:
            try:
                if id(hook) in self._batch_with_catalog_doc_id:
                    hook(
                        doc_ids, collection, contents, embeddings, metadatas,
                        catalog_doc_id=catalog_doc_id,
                    )
                else:
                    hook(doc_ids, collection, contents, embeddings, metadatas)
            except Exception as exc:
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
            except Exception as exc:
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

        for did, sp, content in zip(doc_ids, source_paths, contents):
            self.fire_document(sp, collection, content, doc_id=did)


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
    from nexus.mcp_infra import (
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

    from nexus.aspect_worker import aspect_extraction_enqueue_hook
    if aspect_extraction_enqueue_hook not in registry._document:
        registry.register_document(aspect_extraction_enqueue_hook)


# ── Failure-record helpers (moved from mcp_infra) ────────────────────────────


def _record_hook_failure(
    *,
    doc_id: str,
    collection: str,
    hook_name: str,
    error: str,
) -> None:
    """Persist a single-doc post-store hook failure to T2 ``hook_failures``.

    Writes ``chain='single'`` (RDR-089 4.14.2 schema) when the column is
    present, falling back to a chain-less insert on pre-4.14.2 DBs.
    Secondary best-effort path: if the T2 write itself fails, swallow
    the second failure (the primary warning already reached structlog)
    rather than mask the original hook exception.
    """
    from nexus.mcp_infra import t2_ctx

    truncated = error[:2000]
    try:
        with t2_ctx() as t2:
            conn = t2.taxonomy.conn
            with t2.taxonomy._lock:
                try:
                    conn.execute(
                        "INSERT INTO hook_failures "
                        "(doc_id, collection, hook_name, error, chain) "
                        "VALUES (?, ?, ?, ?, 'single')",
                        (doc_id, collection, hook_name, truncated),
                    )
                except sqlite3.OperationalError as exc:
                    msg = str(exc)
                    if "no column named" not in msg and "no such column" not in msg:
                        raise
                    conn.execute(
                        "INSERT INTO hook_failures "
                        "(doc_id, collection, hook_name, error) VALUES (?, ?, ?, ?)",
                        (doc_id, collection, hook_name, truncated),
                    )
                conn.commit()
    except Exception:
        _log.debug(
            "hook_failure_persist_failed",
            hook=hook_name,
            collection=collection,
            exc_info=True,
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

    Falls back to scalar-only insert if the new columns don't exist
    yet (mixed-version operator scenario).
    """
    from nexus.mcp_infra import t2_ctx

    representative = doc_ids[0] if doc_ids else ""
    payload = json.dumps(doc_ids)
    truncated = error[:2000]
    try:
        with t2_ctx() as t2:
            conn = t2.taxonomy.conn
            with t2.taxonomy._lock:
                try:
                    conn.execute(
                        "INSERT INTO hook_failures "
                        "(doc_id, collection, hook_name, error, "
                        " batch_doc_ids, is_batch, chain) "
                        "VALUES (?, ?, ?, ?, ?, 1, 'batch')",
                        (representative, collection, hook_name, truncated, payload),
                    )
                except sqlite3.OperationalError as exc:
                    msg = str(exc)
                    if "no column named" not in msg and "no such column" not in msg:
                        raise
                    try:
                        conn.execute(
                            "INSERT INTO hook_failures "
                            "(doc_id, collection, hook_name, error, "
                            " batch_doc_ids, is_batch) VALUES (?, ?, ?, ?, ?, 1)",
                            (representative, collection, hook_name, truncated, payload),
                        )
                    except sqlite3.OperationalError as exc2:
                        msg2 = str(exc2)
                        if "no column named" not in msg2 and "no such column" not in msg2:
                            raise
                        conn.execute(
                            "INSERT INTO hook_failures "
                            "(doc_id, collection, hook_name, error) "
                            "VALUES (?, ?, ?, ?)",
                            (representative, collection, hook_name, truncated),
                        )
                conn.commit()
    except Exception:
        _log.debug(
            "batch_hook_failure_persist_failed",
            hook=hook_name,
            collection=collection,
            exc_info=True,
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

    Two-tier fallback (vs. three-tier in :func:`_record_batch_hook_failure`)
    is correct: the document chain is new in 4.14.2, so there is no
    intermediate 4.14.1-shape schema to handle.
    """
    from nexus.mcp_infra import t2_ctx

    truncated = error[:2000]
    try:
        with t2_ctx() as t2:
            conn = t2.taxonomy.conn
            with t2.taxonomy._lock:
                try:
                    conn.execute(
                        "INSERT INTO hook_failures "
                        "(doc_id, collection, hook_name, error, chain) "
                        "VALUES (?, ?, ?, ?, 'document')",
                        (source_path, collection, hook_name, truncated),
                    )
                except sqlite3.OperationalError as exc:
                    msg = str(exc)
                    if "no column named" not in msg and "no such column" not in msg:
                        raise
                    conn.execute(
                        "INSERT INTO hook_failures "
                        "(doc_id, collection, hook_name, error) "
                        "VALUES (?, ?, ?, ?)",
                        (source_path, collection, hook_name, truncated),
                    )
                conn.commit()
    except Exception:
        _log.debug(
            "document_hook_failure_persist_failed",
            hook=hook_name,
            collection=collection,
            exc_info=True,
        )
