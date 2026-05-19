# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runtime container for nexus state.

Eliminates the global singletons in nexus.catalog and nexus.mcp_infra
(RDR-118 Phase 1 Step 1, nexus-atf8a).

Threads previously process-global state (catalog cache, T2 client pool,
post-store hook lists, env-driven config) through one explicit container
constructed per CLI invocation or per test. ContextVar discovery lets
module-level shim functions resolve the live runtime without rewriting
every call site at once. Steps 2-3 (nexus-2bino, nexus-ipyfj) thread the
runtime into ``nexus.catalog`` and ``nexus.mcp_infra``; Phase 2-4 retire
the remaining module-globals and autouse fixtures.

Two A4 preservations gate later phases:

* S1: ``get_catalog()`` (no arg) preserves the mtime-refresh path from
  ``mcp_infra.get_catalog`` so cross-process JSONL writes invalidate the
  cached instance under direct mode. Phase 2 Step 0 (nexus-u1f64) decides
  whether to absorb this into daemon mode or retire it.
* S2: the catalog cache key is ``(cat_path, mode)``. Tests that flip
  ``storage_mode`` must not see the prior-mode instance.
"""
from __future__ import annotations

import inspect
import threading
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Callable, Literal

import structlog

from nexus.catalog import Catalog


__all__ = [
    "NexusRuntime",
    "HookRegistry",
    "current_runtime",
    "use_runtime",
    "install_default_hooks",
]


_BOOL_TRUE: tuple[str, ...] = ("1", "true", "yes", "on")


_STORAGE_MODES: tuple[str, ...] = ("direct", "daemon")
StorageMode = Literal["direct", "daemon"]


def _max_jsonl_mtime(cat: Catalog) -> float:
    """Return max mtime across every catalog file written by a mutator.

    Mirrors ``nexus.mcp_infra._max_jsonl_mtime`` verbatim. Inlined rather
    than imported to keep the runtime module free of a back-reference
    into ``mcp_infra`` (one of the two singleton hosts this module
    replaces).
    """
    mtime = 0.0
    for path in cat.mtime_paths():
        try:
            if path.exists():
                mtime = max(mtime, path.stat().st_mtime)
        except OSError:
            pass
    return mtime


# ── HookRegistry ─────────────────────────────────────────────────────────────


class HookRegistry:
    """Three-chain post-store hook registry.

    Replaces the six module-level mutables in ``nexus.mcp_infra``
    (``_post_store_hooks``, ``_post_store_batch_hooks``,
    ``_post_store_batch_hooks_with_catalog_doc_id``,
    ``_post_document_hooks``, ``_post_document_hooks_with_doc_id``) and
    their dispatchers. Per-hook failure isolation + T2 ``hook_failures``
    persistence semantics are preserved verbatim: the failure-recording
    helpers live as module-level functions in ``nexus.mcp_infra`` and are
    invoked here via lazy import so existing tests that monkeypatch
    ``nexus.mcp_infra.t2_ctx`` keep working unchanged.
    """

    def __init__(self) -> None:
        self._single: list[Callable[..., None]] = []
        self._batch: list[Callable[..., None]] = []
        self._batch_with_catalog_doc_id: set[int] = set()
        self._document: list[Callable[..., None]] = []
        self._document_with_doc_id: set[int] = set()

    def clear(self) -> None:
        """Drop every registration in all three chains."""
        self._single.clear()
        self._batch.clear()
        self._batch_with_catalog_doc_id.clear()
        self._document.clear()
        self._document_with_doc_id.clear()

    # ── Single-doc chain ─────────────────────────────────────────────────────

    def register_single(self, fn: Callable[[str, str, str], None]) -> None:
        """Register a ``fn(doc_id, collection, content)`` callable to fire
        once per MCP ``store_put``. Mirrors ``register_post_store_hook``."""
        self._single.append(fn)

    def fire_single(self, doc_id: str, collection: str, content: str) -> None:
        """Invoke every single-doc hook. Per-hook exceptions are caught,
        logged, and persisted to T2 ``hook_failures``; never propagated.
        Verbatim of ``mcp_infra.fire_post_store_hooks``."""
        log = structlog.get_logger()
        for hook in self._single:
            try:
                hook(doc_id, collection, content)
            except Exception as exc:
                hook_name = getattr(hook, "__name__", "?")
                log.warning(
                    "post_store_hook_failed", hook=hook_name, exc_info=True,
                )
                _record_hook_failure(
                    doc_id=doc_id, collection=collection,
                    hook_name=hook_name, error=str(exc),
                )

    # ── Batch chain ──────────────────────────────────────────────────────────

    def register_batch(self, fn: Callable[..., None]) -> None:
        """Register a batch hook (CLI ingest). Classifies whether the
        callable accepts ``catalog_doc_id`` at registration via
        ``inspect.signature`` (RDR-108 Phase 3 contract). Mirrors
        ``register_post_store_batch_hook``."""
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
            structlog.get_logger().debug(
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
        """Invoke every batch hook with the recorded call shape. Empty
        ``doc_ids`` returns early before any hook fires. Per-hook
        exceptions captured + persisted, never raised. Verbatim of
        ``mcp_infra.fire_post_store_batch_hooks``."""
        if not doc_ids:
            return
        log = structlog.get_logger()
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
                log.warning(
                    "post_store_batch_hook_failed",
                    hook=hook_name, exc_info=True,
                )
                _record_batch_hook_failure(
                    doc_ids=doc_ids, collection=collection,
                    hook_name=hook_name, error=str(exc),
                )

    # ── Document-grain chain ─────────────────────────────────────────────────

    def register_document(self, fn: Callable[..., None]) -> None:
        """Register a synchronous ``fn(source_path, collection, content)``
        callable. Async callables are silently unsupported (the
        synchronous-only contract is load-bearing for RDR-089 aspect
        extraction). Classifies whether the callable accepts ``doc_id``
        at registration. Mirrors ``register_post_document_hook``."""
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
            structlog.get_logger().debug(
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
        """Invoke every document hook. Synchronous dispatch, no
        ``asyncio.to_thread``. Per-hook exceptions captured + persisted,
        never raised. Verbatim of ``mcp_infra.fire_post_document_hooks``."""
        log = structlog.get_logger()
        for hook in self._document:
            try:
                if id(hook) in self._document_with_doc_id:
                    hook(source_path, collection, content, doc_id=doc_id)
                else:
                    hook(source_path, collection, content)
            except Exception as exc:
                hook_name = getattr(hook, "__name__", "?")
                log.warning(
                    "post_document_hook_failed",
                    hook=hook_name,
                    source_path=source_path,
                    collection=collection,
                    exc_info=True,
                )
                _record_document_hook_failure(
                    source_path=source_path, collection=collection,
                    hook_name=hook_name, error=str(exc),
                )


def _record_hook_failure(
    *, doc_id: str, collection: str, hook_name: str, error: str,
) -> None:
    """Delegate to ``mcp_infra._record_hook_failure``. Lazy import keeps
    the runtime module free of a back-reference at import time and
    preserves the existing monkeypatch-on-``mcp_infra.t2_ctx`` test
    contract for hook-failure persistence assertions."""
    from nexus.mcp_infra import _record_hook_failure as _impl
    _impl(
        doc_id=doc_id, collection=collection,
        hook_name=hook_name, error=error,
    )


def _record_batch_hook_failure(
    *, doc_ids: list[str], collection: str, hook_name: str, error: str,
) -> None:
    """Delegate to ``mcp_infra._record_batch_hook_failure`` (lazy import)."""
    from nexus.mcp_infra import _record_batch_hook_failure as _impl
    _impl(
        doc_ids=doc_ids, collection=collection,
        hook_name=hook_name, error=error,
    )


def _record_document_hook_failure(
    *, source_path: str, collection: str, hook_name: str, error: str,
) -> None:
    """Delegate to ``mcp_infra._record_document_hook_failure`` (lazy import)."""
    from nexus.mcp_infra import _record_document_hook_failure as _impl
    _impl(
        source_path=source_path, collection=collection,
        hook_name=hook_name, error=error,
    )


# ── NexusRuntime ─────────────────────────────────────────────────────────────


class NexusRuntime:
    """Per-invocation container for previously-global nexus state.

    Constructed once at CLI entry or once per test. Holds the catalog
    cache (keyed by ``(cat_path, mode)``, the A4 S2 preservation), an
    optional T2Client socket pool for daemon mode, the three-chain hook
    registry, and explicit values for the eleven env-driven settings
    that scattered ``os.environ.get(...)`` calls used to read at every
    call site. CLI entry points construct from env + flags; tests
    construct directly with explicit kwargs.

    Lifecycle:

    * Construct with at least ``config_dir``; other kwargs default.
    * ``close()`` drops the catalog cache and closes the T2Client.
    * Context manager protocol calls ``close()`` on exit.
    """

    def __init__(
        self,
        *,
        config_dir: Path | str,
        catalog_path: Path | str | None = None,
        storage_mode: StorageMode = "direct",
        dispatch_backend: str | None = None,
        dispatch_qwen_operators: str | None = None,
        dispatch_claude_operators: str | None = None,
        aspect_backend: str | None = None,
        scholarly_paper_version: str | None = None,
        tier_b_dispatcher: str | None = None,
        qwen_agent_supervisor: str | None = None,
        skip_t1: bool = False,
    ) -> None:
        if storage_mode not in _STORAGE_MODES:
            raise ValueError(
                f"storage_mode must be one of {_STORAGE_MODES!r}; "
                f"got {storage_mode!r}"
            )
        self._config_dir: Path = Path(config_dir)
        self._catalog_path: Path | None = (
            Path(catalog_path) if catalog_path is not None else None
        )
        self._storage_mode: StorageMode = storage_mode
        self._dispatch_backend = dispatch_backend
        self._dispatch_qwen_operators = dispatch_qwen_operators
        self._dispatch_claude_operators = dispatch_claude_operators
        self._aspect_backend = aspect_backend
        self._scholarly_paper_version = scholarly_paper_version
        self._tier_b_dispatcher = tier_b_dispatcher
        self._qwen_agent_supervisor = qwen_agent_supervisor
        self._skip_t1 = skip_t1

        self._cached: dict[tuple[Path, str], Catalog] = {}
        self._cache_lock = threading.Lock()
        self._t2_client: Any = None
        self._t2_client_lock = threading.Lock()
        self._catalog_mtime: float = 0.0
        self._catalog_mtime_lock = threading.Lock()
        self._hooks = HookRegistry()
        self._closed = False

    # ── Read-only properties ─────────────────────────────────────────────────

    @property
    def config_dir(self) -> Path:
        return self._config_dir

    @property
    def catalog_path(self) -> Path | None:
        return self._catalog_path

    @property
    def storage_mode(self) -> StorageMode:
        return self._storage_mode

    @property
    def dispatch_backend(self) -> str | None:
        return self._dispatch_backend

    @property
    def dispatch_qwen_operators(self) -> str | None:
        return self._dispatch_qwen_operators

    @property
    def dispatch_claude_operators(self) -> str | None:
        return self._dispatch_claude_operators

    @property
    def aspect_backend(self) -> str | None:
        return self._aspect_backend

    @property
    def scholarly_paper_version(self) -> str | None:
        return self._scholarly_paper_version

    @property
    def tier_b_dispatcher(self) -> str | None:
        return self._tier_b_dispatcher

    @property
    def qwen_agent_supervisor(self) -> str | None:
        return self._qwen_agent_supervisor

    @property
    def skip_t1(self) -> bool:
        return self._skip_t1

    @property
    def hooks(self) -> HookRegistry:
        return self._hooks

    # ── Catalog accessors ────────────────────────────────────────────────────

    def get_catalog(self, cat_path: Path | str | None = None) -> Catalog | None:
        """Return a Catalog for *cat_path* or, when omitted, for
        ``self.catalog_path``.

        Explicit ``cat_path`` (replaces ``nexus.catalog.open_cached``):
        cached by ``(cat_path, storage_mode)``; constructs once and
        reuses on every subsequent call. Always returns a Catalog;
        callers that need ``is_initialized`` semantics check the
        directory themselves.

        Omitted ``cat_path`` (replaces ``nexus.mcp_infra.get_catalog``):
        derives the path from ``self.catalog_path``. Returns ``None``
        when the runtime has no catalog path set or when the directory
        is not an initialised catalog repo. Applies the A4 S1 preserved
        mtime-refresh path so cross-process JSONL writes trigger
        ``_ensure_consistent`` on the cached instance.

        Raises ``RuntimeError`` after the runtime has been closed.
        """
        if self._closed:
            raise RuntimeError("NexusRuntime is closed")
        if cat_path is None:
            return self._get_default_catalog()
        return self._get_cached_catalog(Path(cat_path))

    def fresh_catalog(self, cat_path: Path | str) -> Catalog:
        """Return a brand-new (un-cached) Catalog for *cat_path*. Replaces
        ``nexus.catalog.open_catalog``. Useful for write-heavy paths
        where reuse would leak state across operations."""
        if self._closed:
            raise RuntimeError("NexusRuntime is closed")
        return self._construct_catalog(Path(cat_path))

    def _get_default_catalog(self) -> Catalog | None:
        """No-arg ``get_catalog`` path (mcp_infra.get_catalog semantics)."""
        if self._catalog_path is None:
            return None
        if not Catalog.is_initialized(self._catalog_path):
            return None
        cat = self._get_cached_catalog(self._catalog_path)
        # A4 S1 preservation: cross-process JSONL writes (a git pull from
        # another process, an event-sourced write made by a peer) advance
        # the catalog files' mtime; the cached instance must re-run
        # ``_ensure_consistent`` so its projector state matches what's on
        # disk. Phase 2 Step 0 (nexus-u1f64) decides whether to absorb or
        # retire this under daemon mode.
        try:
            current_mtime = _max_jsonl_mtime(cat)
            if current_mtime > self._catalog_mtime:
                with self._catalog_mtime_lock:
                    if current_mtime > self._catalog_mtime:
                        self._catalog_mtime = current_mtime
                        cat._ensure_consistent()
        except OSError:
            pass
        return cat

    def _get_cached_catalog(self, cat_path: Path) -> Catalog:
        """Cached fetch by ``(cat_path, storage_mode)``. The (path, mode)
        keying is the A4 S2 preservation: a test sequence that flips
        ``storage_mode`` between cases would otherwise return the
        prior-mode instance and read against the wrong backend."""
        key = (cat_path, self._storage_mode)
        inst = self._cached.get(key)
        if inst is not None:
            return inst
        with self._cache_lock:
            inst = self._cached.get(key)
            if inst is not None:
                return inst
            inst = self._construct_catalog(cat_path)
            # Baseline the mtime when we just built the default-path
            # catalog so the subsequent mtime-refresh check doesn't
            # immediately re-run ``_ensure_consistent`` on first access.
            if cat_path == self._catalog_path:
                try:
                    self._catalog_mtime = _max_jsonl_mtime(inst)
                except OSError:
                    pass
            self._cached[key] = inst
            return inst

    def _construct_catalog(self, cat_path: Path) -> Catalog:
        """Build a Catalog respecting ``self.storage_mode``. In daemon
        mode the Catalog wraps an ``ExecuteProxy`` over the runtime's
        shared T2Client; in direct mode it constructs a plain
        ``CatalogDB`` over ``.catalog.db``. Mirrors
        ``nexus.catalog.open_catalog`` but with mode resolved from the
        runtime instead of ``nexus.db.is_daemon_mode``.

        The ``Catalog`` class is looked up lazily through
        ``nexus.catalog`` on every call so existing tests that
        ``patch.object(nexus.catalog, "Catalog", ...)`` still intercept
        the construction here. The legacy ``open_catalog`` used the
        module-level ``Catalog`` name in ``nexus.catalog``; this lazy
        lookup keeps the same patch surface working through the shim.
        """
        from nexus.catalog import Catalog as _Catalog

        if self._storage_mode == "daemon":
            from nexus.catalog.catalog_proxy import ExecuteProxy
            t2 = self._get_t2_client()
            return _Catalog(cat_path, cat_path / ".catalog.db", db=ExecuteProxy(t2))
        return _Catalog(cat_path, cat_path / ".catalog.db")

    def _get_t2_client(self) -> Any:
        """Lazy-construct the runtime's shared T2Client for daemon mode.
        Replaces the legacy module-level ``nexus.catalog._get_t2_client``
        singleton with a per-runtime instance. Released by ``close()``."""
        if self._t2_client is not None:
            return self._t2_client
        with self._t2_client_lock:
            if self._t2_client is not None:
                return self._t2_client
            from nexus.mcp_infra import t2_ctx
            self._t2_client = t2_ctx()
            return self._t2_client

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Drop the catalog cache and close the shared T2Client (daemon
        mode). Idempotent: calling twice is a no-op. After close the
        catalog accessors raise ``RuntimeError`` rather than silently
        return stale handles. The hook registry is left intact because
        registrations live on the runtime instance, not on the cache."""
        if self._closed:
            return
        with self._cache_lock:
            self._cached.clear()
            with self._t2_client_lock:
                client = self._t2_client
                self._t2_client = None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        self._closed = True

    def __enter__(self) -> "NexusRuntime":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ── ContextVar discovery ─────────────────────────────────────────────────────

_runtime_var: ContextVar[NexusRuntime | None] = ContextVar(
    "nexus_runtime", default=None,
)


def current_runtime() -> NexusRuntime:
    """Return the NexusRuntime active in the current context.

    Raises ``RuntimeError`` with a diagnostic message when no runtime
    has been set, surfacing the wiring gap loudly rather than silently
    falling back to module-globals (which is the failure mode RDR-118
    exists to retire).
    """
    rt = _runtime_var.get()
    if rt is None:
        raise RuntimeError(
            "No NexusRuntime in current context. CLI entry points must "
            "construct one via NexusRuntime(...) + use_runtime(rt); tests "
            "must use the `runtime` pytest fixture or call "
            "use_runtime(...) explicitly."
        )
    return rt


def use_runtime(runtime: NexusRuntime) -> Token:
    """Install *runtime* as the current ContextVar runtime and return
    the reset token. Callers MUST pass the token to ``_runtime_var.reset``
    in a ``finally`` block to keep the per-context binding scoped."""
    return _runtime_var.set(runtime)


# ── Process-default runtime + shim resolver (RDR-118 P1.S2) ─────────────────
#
# Legacy module-level accessors (``nexus.catalog.open_cached``, etc.) become
# thin redirectors that resolve through ``_ensure_runtime_for_shim()``. With
# a runtime in the current context the shim uses it directly; without one a
# process-default runtime is lazy-constructed from environment variables so
# the existing autouse fixtures (``_isolate_catalog``,
# ``_isolate_config_dir``, ``_pin_storage_mode_direct_for_tests``) continue
# to drive per-test isolation. ``_close_process_default()`` tears the
# default down so a subsequent access reconstructs it against the current
# env. Tests call this via ``nexus.catalog.reset_cache()`` at the per-test
# boundaries; Phase 4 retires the fallback entirely.

_process_default: NexusRuntime | None = None
_process_default_lock = threading.Lock()


def _ensure_runtime_for_shim() -> NexusRuntime:
    """Return the active runtime for a legacy module-level shim caller.

    Prefers the ContextVar runtime when set; otherwise lazy-constructs a
    process-default from environment variables. The process-default is
    cached across calls until ``_close_process_default`` runs, mirroring
    the existing module-level singleton lifecycle that the shim layer
    replaces.

    Auto-installs the load-bearing default hooks on the process-default
    only (the "production-like" pathway: lazy construction matches
    pre-RDR-118 module-load auto-registration semantics). Explicit
    ``NexusRuntime(...)`` construction yields a clean registry so the
    new ``runtime`` fixture and CLI entry points retain control over
    when hooks attach.
    """
    rt = _runtime_var.get()
    if rt is not None and not rt._closed:
        return rt
    global _process_default
    if _process_default is not None and not _process_default._closed:
        return _process_default
    with _process_default_lock:
        if _process_default is not None and not _process_default._closed:
            return _process_default
        _process_default = _construct_process_default_from_env()
        install_default_hooks(_process_default)
        return _process_default


def _close_process_default() -> None:
    """Tear down the process-default runtime if one exists. Idempotent.
    Called by ``nexus.catalog.reset_cache()`` and by tests that flip env
    values between cases so the next access reads current env."""
    global _process_default
    with _process_default_lock:
        rt = _process_default
        _process_default = None
    if rt is not None and not rt._closed:
        try:
            rt.close()
        except Exception:  # pragma: no cover
            pass


def _construct_process_default_from_env() -> NexusRuntime:
    """Read the eleven previously-env-driven settings and build a runtime.

    Source of truth for env names: this function is the one place where
    the env-as-config pattern lives during Phases 1-3. Phase 3
    (``nexus-s43yx``) consolidates all env reads into a single CLI-entry
    construction; this helper retires alongside the autouse env-isolation
    fixtures."""
    import os
    from nexus.config import catalog_path as _catalog_path
    from nexus.config import nexus_config_dir

    config_dir = nexus_config_dir()
    cp_raw = os.environ.get("NEXUS_CATALOG_PATH", "").strip()
    catalog_path = Path(cp_raw) if cp_raw else _catalog_path()
    storage_mode_raw = os.environ.get("NX_STORAGE_MODE", "").strip() or "direct"
    storage_mode: StorageMode = (
        "daemon" if storage_mode_raw == "daemon" else "direct"
    )
    skip_t1_env = (
        os.environ.get("NX_T1_ISOLATED", "")
        or os.environ.get("NEXUS_SKIP_T1", "")
    ).strip().lower()
    return NexusRuntime(
        config_dir=config_dir,
        catalog_path=catalog_path,
        storage_mode=storage_mode,
        dispatch_backend=os.environ.get("NEXUS_DISPATCH_BACKEND") or None,
        dispatch_qwen_operators=os.environ.get(
            "NEXUS_DISPATCH_QWEN_OPERATORS",
        ) or None,
        dispatch_claude_operators=os.environ.get(
            "NEXUS_DISPATCH_CLAUDE_OPERATORS",
        ) or None,
        aspect_backend=os.environ.get("NEXUS_ASPECT_BACKEND") or None,
        scholarly_paper_version=os.environ.get(
            "NEXUS_SCHOLARLY_PAPER_VERSION",
        ) or None,
        tier_b_dispatcher=os.environ.get("NEXUS_TIER_B_DISPATCHER") or None,
        qwen_agent_supervisor=os.environ.get("QWEN_AGENT_SUPERVISOR") or None,
        skip_t1=skip_t1_env in _BOOL_TRUE,
    )


# ── Default-hooks factory (filled in Step 3, nexus-ipyfj) ────────────────────


def install_default_hooks(runtime: NexusRuntime) -> None:
    """Register the load-bearing default hooks on *runtime*.

    RDR-118 P1.S3 (nexus-ipyfj). Moves the three module-load
    self-registrations from ``nexus.mcp_infra:983-985`` into an explicit
    factory called by CLI / MCP entry points after ``NexusRuntime``
    construction. The hooks are load-bearing for catalog correctness
    across both CLI ingest paths and MCP ``store_put``: dropping any
    of them silently leaves chash_index, taxonomy_assignments, or the
    catalog manifest stale (see RDR-108 Phase 3 / nexus-bdag).

    Idempotent: calling more than once on the same runtime is a no-op
    via duplicate-registration detection on each hook's identity. Tests
    that need the default registry call this factory explicitly; tests
    that need a clean registry skip it.

    The RDR-089 aspect-extraction document hook stays in
    ``nexus.mcp.core`` for Phase 1 and migrates to this factory in
    Phase 2 (bead ``nexus-f2ufy``).
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
        if hook not in runtime.hooks._batch:
            runtime.hooks.register_batch(hook)
