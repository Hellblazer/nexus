# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nexus.runtime``, the singleton-elimination container (RDR-118).

Phase 1 Step 1 (bead nexus-atf8a). Pins the runtime container's constructor
contract, the ContextVar discovery mechanism, the shared catalog cache keyed
by (path, mode), and the three-chain HookRegistry that replaces the
process-global hook lists. Failure isolation + T2 ``hook_failures`` persistence
contracts are verified against the runtime's HookRegistry directly, not
against the legacy mcp_infra dispatchers (those are migrated to thin shims in
Step 3 / bead nexus-ipyfj, and rewritten end-to-end in Step 4 / bead
nexus-rkkn2).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.catalog import Catalog


# ── Constructor + property surface ───────────────────────────────────────────


def test_runtime_constructor_minimum_kwargs(tmp_path: Path) -> None:
    """Minimum-kwargs path: config_dir alone is enough."""
    from nexus.runtime import NexusRuntime

    rt = NexusRuntime(config_dir=tmp_path / ".config" / "nexus")
    try:
        assert rt.config_dir == tmp_path / ".config" / "nexus"
        assert rt.catalog_path is None
        assert rt.storage_mode == "direct"
        assert rt.dispatch_backend is None
        assert rt.dispatch_qwen_operators is None
        assert rt.dispatch_claude_operators is None
        assert rt.aspect_backend is None
        assert rt.scholarly_paper_version is None
        assert rt.tier_b_dispatcher is None
        assert rt.qwen_agent_supervisor is None
        assert rt.skip_t1 is False
    finally:
        rt.close()


def test_runtime_constructor_all_kwargs_roundtrip(tmp_path: Path) -> None:
    """All 11 env-driven settings are accepted as kwargs and exposed as properties."""
    from nexus.runtime import NexusRuntime

    rt = NexusRuntime(
        config_dir=tmp_path / ".config",
        catalog_path=tmp_path / "catalog",
        storage_mode="daemon",
        dispatch_backend="auto",
        dispatch_qwen_operators="extract,verify",
        dispatch_claude_operators="rank",
        aspect_backend="qwen",
        scholarly_paper_version="v2",
        tier_b_dispatcher="qwen_agent",
        qwen_agent_supervisor="/tmp/supervisor",
        skip_t1=True,
    )
    try:
        assert rt.config_dir == tmp_path / ".config"
        assert rt.catalog_path == tmp_path / "catalog"
        assert rt.storage_mode == "daemon"
        assert rt.dispatch_backend == "auto"
        assert rt.dispatch_qwen_operators == "extract,verify"
        assert rt.dispatch_claude_operators == "rank"
        assert rt.aspect_backend == "qwen"
        assert rt.scholarly_paper_version == "v2"
        assert rt.tier_b_dispatcher == "qwen_agent"
        assert rt.qwen_agent_supervisor == "/tmp/supervisor"
        assert rt.skip_t1 is True
    finally:
        rt.close()


def test_runtime_storage_mode_rejects_unknown_value(tmp_path: Path) -> None:
    """storage_mode literal type is enforced at construction; anything other
    than 'direct'/'daemon' raises before the runtime is built."""
    from nexus.runtime import NexusRuntime

    with pytest.raises(ValueError, match="storage_mode"):
        NexusRuntime(
            config_dir=tmp_path / ".config",
            storage_mode="cloud",  # type: ignore[arg-type]
        )


def test_runtime_paths_coerced_to_path_objects(tmp_path: Path) -> None:
    """String paths are coerced to Path objects at construction."""
    from nexus.runtime import NexusRuntime

    rt = NexusRuntime(
        config_dir=str(tmp_path / ".config"),  # type: ignore[arg-type]
        catalog_path=str(tmp_path / "catalog"),  # type: ignore[arg-type]
    )
    try:
        assert isinstance(rt.config_dir, Path)
        assert isinstance(rt.catalog_path, Path)
    finally:
        rt.close()


# ── ContextVar discovery mechanism ───────────────────────────────────────────


def test_current_runtime_raises_when_unset() -> None:
    """current_runtime() raises RuntimeError with a clear diagnostic when
    no NexusRuntime has been set in the current context."""
    from nexus.runtime import _runtime_var, current_runtime

    # Defensive: belt-and-braces clear the ContextVar in case another test
    # leaked. The conftest fixture (added in this bead) does this in setUp.
    token = _runtime_var.set(None)
    try:
        with pytest.raises(RuntimeError, match="No NexusRuntime"):
            current_runtime()
    finally:
        _runtime_var.reset(token)


def test_use_runtime_sets_contextvar_and_returns_token(tmp_path: Path) -> None:
    """use_runtime(rt) sets the ContextVar to rt and returns a reset token
    so the caller can restore the prior state."""
    from nexus.runtime import (
        NexusRuntime,
        _runtime_var,
        current_runtime,
        use_runtime,
    )

    rt = NexusRuntime(config_dir=tmp_path / ".config")
    try:
        # Confirm initial state is None.
        assert _runtime_var.get() is None
        token = use_runtime(rt)
        try:
            assert current_runtime() is rt
        finally:
            _runtime_var.reset(token)
        # After reset the ContextVar is back to None.
        assert _runtime_var.get() is None
    finally:
        rt.close()


# ── get_catalog with explicit cat_path (open_cached semantics) ───────────────


def test_get_catalog_explicit_path_constructs_and_caches(tmp_path: Path) -> None:
    """runtime.get_catalog(cat_path=...) constructs once and reuses the
    cached instance on subsequent calls with the same (path, mode)."""
    from nexus.runtime import NexusRuntime

    catalog_dir = tmp_path / "cat"
    Catalog.init(catalog_dir)
    rt = NexusRuntime(config_dir=tmp_path / ".config", storage_mode="direct")
    try:
        first = rt.get_catalog(catalog_dir)
        second = rt.get_catalog(catalog_dir)
        assert first is not None
        assert first is second
    finally:
        rt.close()


def test_get_catalog_cache_keyed_by_path_and_mode(tmp_path: Path) -> None:
    """A4 S2 preservation: cache key is (cat_path, mode). Different paths get
    different instances; same path under different modes also do."""
    from nexus.runtime import NexusRuntime

    catalog_a = tmp_path / "cat_a"
    catalog_b = tmp_path / "cat_b"
    Catalog.init(catalog_a)
    Catalog.init(catalog_b)

    rt = NexusRuntime(config_dir=tmp_path / ".config", storage_mode="direct")
    try:
        inst_a = rt.get_catalog(catalog_a)
        inst_b = rt.get_catalog(catalog_b)
        assert inst_a is not inst_b
    finally:
        rt.close()


def test_get_catalog_no_cache_across_runtimes(tmp_path: Path) -> None:
    """Each NexusRuntime owns its own catalog cache. Two runtimes pointed
    at the same path return distinct Catalog instances. No shared
    process-global cache."""
    from nexus.runtime import NexusRuntime

    catalog_dir = tmp_path / "cat"
    Catalog.init(catalog_dir)

    rt1 = NexusRuntime(config_dir=tmp_path / ".config_a")
    rt2 = NexusRuntime(config_dir=tmp_path / ".config_b")
    try:
        inst1 = rt1.get_catalog(catalog_dir)
        inst2 = rt2.get_catalog(catalog_dir)
        assert inst1 is not None
        assert inst2 is not None
        assert inst1 is not inst2
    finally:
        rt1.close()
        rt2.close()


# ── get_catalog with no path (mcp_infra.get_catalog semantics) ───────────────


def test_get_catalog_no_path_returns_none_when_runtime_has_no_catalog_path(
    tmp_path: Path,
) -> None:
    """Without an explicit cat_path and without a catalog_path set on the
    runtime, get_catalog() returns None, preserving the mcp_infra.get_catalog
    'not initialised yet' contract."""
    from nexus.runtime import NexusRuntime

    rt = NexusRuntime(config_dir=tmp_path / ".config")  # no catalog_path
    try:
        assert rt.get_catalog() is None
    finally:
        rt.close()


def test_get_catalog_no_path_returns_none_when_path_uninitialised(
    tmp_path: Path,
) -> None:
    """When self.catalog_path points at a directory that is not a catalog
    repo, get_catalog() returns None rather than constructing an empty one,
    preserving mcp_infra.get_catalog's is_initialized check."""
    from nexus.runtime import NexusRuntime

    rt = NexusRuntime(
        config_dir=tmp_path / ".config",
        catalog_path=tmp_path / "uninitialised",
    )
    try:
        assert rt.get_catalog() is None
    finally:
        rt.close()


def test_get_catalog_no_path_uses_runtime_catalog_path(tmp_path: Path) -> None:
    """get_catalog() derives the path from self.catalog_path and returns
    the cached Catalog when the directory is an initialised catalog repo."""
    from nexus.runtime import NexusRuntime

    catalog_dir = tmp_path / "cat"
    Catalog.init(catalog_dir)
    rt = NexusRuntime(
        config_dir=tmp_path / ".config",
        catalog_path=catalog_dir,
    )
    try:
        first = rt.get_catalog()
        second = rt.get_catalog()
        assert first is not None
        assert first is second
    finally:
        rt.close()


def test_get_catalog_no_path_shares_cache_with_explicit_path(tmp_path: Path) -> None:
    """The (path, mode) cache is unified across the no-arg and explicit-arg
    call paths: requesting the same path either way returns the same Catalog
    instance. This is the dual-singleton collapse RDR-118 ships."""
    from nexus.runtime import NexusRuntime

    catalog_dir = tmp_path / "cat"
    Catalog.init(catalog_dir)
    rt = NexusRuntime(
        config_dir=tmp_path / ".config",
        catalog_path=catalog_dir,
    )
    try:
        via_default = rt.get_catalog()
        via_explicit = rt.get_catalog(catalog_dir)
        assert via_default is via_explicit
    finally:
        rt.close()


# ── fresh_catalog (never cached) ─────────────────────────────────────────────


def test_fresh_catalog_never_caches(tmp_path: Path) -> None:
    """fresh_catalog returns a distinct Catalog instance on every call,
    independent of the runtime's catalog cache."""
    from nexus.runtime import NexusRuntime

    catalog_dir = tmp_path / "cat"
    Catalog.init(catalog_dir)
    rt = NexusRuntime(config_dir=tmp_path / ".config")
    try:
        first = rt.fresh_catalog(catalog_dir)
        second = rt.fresh_catalog(catalog_dir)
        assert first is not None
        assert second is not None
        assert first is not second
        # And neither populates the shared cache.
        cached = rt.get_catalog(catalog_dir)
        assert cached is not first
        assert cached is not second
    finally:
        rt.close()


# ── close() lifecycle ────────────────────────────────────────────────────────


def test_close_drops_catalog_cache(tmp_path: Path) -> None:
    """close() empties the catalog cache. A subsequent reopen on the same
    runtime would build a fresh Catalog. We don't reopen (close is meant
    to be terminal) but we assert the cache is empty so a leaked reference
    cannot resurrect a stale instance."""
    from nexus.runtime import NexusRuntime

    catalog_dir = tmp_path / "cat"
    Catalog.init(catalog_dir)
    rt = NexusRuntime(config_dir=tmp_path / ".config")
    rt.get_catalog(catalog_dir)
    assert rt._cached  # cache populated
    rt.close()
    assert not rt._cached  # cache emptied


def test_close_is_idempotent(tmp_path: Path) -> None:
    """Calling close() twice is a no-op."""
    from nexus.runtime import NexusRuntime

    rt = NexusRuntime(config_dir=tmp_path / ".config")
    rt.close()
    rt.close()  # must not raise


def test_context_manager_protocol(tmp_path: Path) -> None:
    """NexusRuntime supports ``with NexusRuntime(...) as rt:`` for scoped
    construction + teardown."""
    from nexus.runtime import NexusRuntime

    with NexusRuntime(config_dir=tmp_path / ".config") as rt:
        assert isinstance(rt, NexusRuntime)
    assert rt._closed is True


# ── HookRegistry: single chain ───────────────────────────────────────────────


def test_hook_single_register_and_fire(tmp_path: Path) -> None:
    """register_single appends, fire_single invokes every registered hook
    with the (doc_id, collection, content) shape."""
    from nexus.runtime import NexusRuntime

    rt = NexusRuntime(config_dir=tmp_path / ".config")
    try:
        calls: list[tuple] = []
        rt.hooks.register_single(
            lambda doc_id, collection, content: calls.append(
                (doc_id, collection, content)
            )
        )
        rt.hooks.fire_single("doc-1", "test__coll", "the content")
        assert calls == [("doc-1", "test__coll", "the content")]
    finally:
        rt.close()


def test_hook_single_failure_isolated(tmp_path: Path, monkeypatch) -> None:
    """A raising hook does not break the second hook. T2 persistence path is
    monkeypatched out; the persistence contract is exercised separately."""
    import nexus.mcp_infra as _mod

    def _no_t2():
        raise RuntimeError("t2 unavailable")

    monkeypatch.setattr(_mod, "t2_ctx", _no_t2)

    from nexus.runtime import NexusRuntime

    rt = NexusRuntime(config_dir=tmp_path / ".config")
    try:
        survivor_calls: list = []

        def raising(doc_id, collection, content):
            raise RuntimeError("intentional")

        rt.hooks.register_single(raising)
        rt.hooks.register_single(
            lambda doc_id, collection, content: survivor_calls.append(doc_id)
        )
        rt.hooks.fire_single("d1", "c1", "x")
        assert survivor_calls == ["d1"]
    finally:
        rt.close()


# ── HookRegistry: batch chain ────────────────────────────────────────────────


def test_hook_batch_register_and_fire_legacy_shape(tmp_path: Path) -> None:
    """A batch hook without a ``catalog_doc_id`` parameter is invoked with
    the legacy 5-arg shape (no kwarg)."""
    from nexus.runtime import NexusRuntime

    rt = NexusRuntime(config_dir=tmp_path / ".config")
    try:
        seen: list[tuple] = []

        def legacy_hook(doc_ids, collection, contents, embeddings, metadatas):
            seen.append((tuple(doc_ids), collection))

        rt.hooks.register_batch(legacy_hook)
        rt.hooks.fire_batch(
            ["d1", "d2"], "c1", ["t1", "t2"], None, None,
            catalog_doc_id="tumbler-xyz",
        )
        assert seen == [(("d1", "d2"), "c1")]
    finally:
        rt.close()


def test_hook_batch_register_and_fire_phase3_shape(tmp_path: Path) -> None:
    """A batch hook with a ``catalog_doc_id`` parameter receives it as a
    kwarg. Classification is done at registration via inspect.signature."""
    from nexus.runtime import NexusRuntime

    rt = NexusRuntime(config_dir=tmp_path / ".config")
    try:
        seen: list[str] = []

        def phase3_hook(
            doc_ids, collection, contents, embeddings, metadatas,
            *, catalog_doc_id: str = "",
        ):
            seen.append(catalog_doc_id)

        rt.hooks.register_batch(phase3_hook)
        rt.hooks.fire_batch(
            ["d1"], "c1", ["t1"], None, None,
            catalog_doc_id="tumbler-xyz",
        )
        assert seen == ["tumbler-xyz"]
    finally:
        rt.close()


def test_hook_batch_register_and_fire_var_keyword_shape(tmp_path: Path) -> None:
    """A batch hook with **kwargs is classified as catalog_doc_id-aware.
    Python cannot statically classify so passthrough is the conservative
    choice (matches register_post_store_batch_hook semantics)."""
    from nexus.runtime import NexusRuntime

    rt = NexusRuntime(config_dir=tmp_path / ".config")
    try:
        seen: list = []

        def varkwarg_hook(
            doc_ids, collection, contents, embeddings, metadatas, **kw,
        ):
            seen.append(kw.get("catalog_doc_id"))

        rt.hooks.register_batch(varkwarg_hook)
        rt.hooks.fire_batch(
            ["d1"], "c1", ["t1"], None, None,
            catalog_doc_id="tumbler-xyz",
        )
        assert seen == ["tumbler-xyz"]
    finally:
        rt.close()


def test_hook_batch_empty_doc_ids_early_return(tmp_path: Path) -> None:
    """An empty doc_ids list returns before invoking any hook, matching the
    legacy fire_post_store_batch_hooks early-return."""
    from nexus.runtime import NexusRuntime

    rt = NexusRuntime(config_dir=tmp_path / ".config")
    try:
        calls: list = []
        rt.hooks.register_batch(
            lambda doc_ids, collection, contents, embeddings, metadatas: calls.append(1)
        )
        rt.hooks.fire_batch([], "c", [], None, None)
        assert calls == []
    finally:
        rt.close()


def test_hook_batch_failure_isolated(tmp_path: Path, monkeypatch) -> None:
    """A raising batch hook does not block the next hook from firing."""
    import nexus.mcp_infra as _mod

    def _no_t2():
        raise RuntimeError("t2 unavailable")

    monkeypatch.setattr(_mod, "t2_ctx", _no_t2)

    from nexus.runtime import NexusRuntime

    rt = NexusRuntime(config_dir=tmp_path / ".config")
    try:
        survivor_calls: list = []

        def raising(doc_ids, collection, contents, embeddings, metadatas):
            raise RuntimeError("kaboom")

        def survivor(doc_ids, collection, contents, embeddings, metadatas):
            survivor_calls.append(tuple(doc_ids))

        rt.hooks.register_batch(raising)
        rt.hooks.register_batch(survivor)
        rt.hooks.fire_batch(["d1", "d2"], "c", ["t1", "t2"], None, None)
        assert survivor_calls == [("d1", "d2")]
    finally:
        rt.close()


# ── HookRegistry: document chain ─────────────────────────────────────────────


def test_hook_document_register_and_fire_legacy_shape(tmp_path: Path) -> None:
    """A document hook without a ``doc_id`` parameter is invoked with the
    legacy 3-arg shape (no kwarg)."""
    from nexus.runtime import NexusRuntime

    rt = NexusRuntime(config_dir=tmp_path / ".config")
    try:
        seen: list[tuple] = []

        def legacy_hook(source_path, collection, content):
            seen.append((source_path, collection, content))

        rt.hooks.register_document(legacy_hook)
        rt.hooks.fire_document("/tmp/x.md", "knowledge__y", "hello", doc_id="d-1")
        assert seen == [("/tmp/x.md", "knowledge__y", "hello")]
    finally:
        rt.close()


def test_hook_document_register_and_fire_phase4_shape(tmp_path: Path) -> None:
    """A document hook with a ``doc_id`` parameter receives it as a kwarg.
    Classification is done at registration."""
    from nexus.runtime import NexusRuntime

    rt = NexusRuntime(config_dir=tmp_path / ".config")
    try:
        seen: list[str] = []

        def phase4_hook(source_path, collection, content, *, doc_id: str = ""):
            seen.append(doc_id)

        rt.hooks.register_document(phase4_hook)
        rt.hooks.fire_document("/tmp/x.md", "knowledge__y", "hello", doc_id="d-1")
        assert seen == ["d-1"]
    finally:
        rt.close()


def test_hook_document_register_and_fire_var_keyword_shape(tmp_path: Path) -> None:
    """A document hook with **kwargs is classified as doc_id-aware."""
    from nexus.runtime import NexusRuntime

    rt = NexusRuntime(config_dir=tmp_path / ".config")
    try:
        seen: list = []

        def varkwarg_hook(source_path, collection, content, **kw):
            seen.append(kw.get("doc_id"))

        rt.hooks.register_document(varkwarg_hook)
        rt.hooks.fire_document("/tmp/x.md", "c", "hello", doc_id="d-1")
        assert seen == ["d-1"]
    finally:
        rt.close()


def test_hook_document_failure_isolated(tmp_path: Path, monkeypatch) -> None:
    """A raising document hook does not block the next hook from firing."""
    import nexus.mcp_infra as _mod

    def _no_t2():
        raise RuntimeError("t2 unavailable")

    monkeypatch.setattr(_mod, "t2_ctx", _no_t2)

    from nexus.runtime import NexusRuntime

    rt = NexusRuntime(config_dir=tmp_path / ".config")
    try:
        survivor_calls: list = []

        def raising(source_path, collection, content):
            raise RuntimeError("kaboom")

        def survivor(source_path, collection, content):
            survivor_calls.append(source_path)

        rt.hooks.register_document(raising)
        rt.hooks.register_document(survivor)
        rt.hooks.fire_document("/tmp/x.md", "c", "hello")
        assert survivor_calls == ["/tmp/x.md"]
    finally:
        rt.close()


# ── HookRegistry: clear / list helpers ───────────────────────────────────────


def test_hooks_isolation_between_runtimes(tmp_path: Path) -> None:
    """Hooks registered on one runtime do not appear in another. This is
    the per-test-isolation property RDR-118 is buying."""
    from nexus.runtime import NexusRuntime

    rt1 = NexusRuntime(config_dir=tmp_path / ".cfg1")
    rt2 = NexusRuntime(config_dir=tmp_path / ".cfg2")
    try:
        rt1_calls: list = []
        rt1.hooks.register_single(
            lambda doc_id, collection, content: rt1_calls.append(doc_id)
        )
        rt2.hooks.fire_single("d", "c", "x")
        assert rt1_calls == []
    finally:
        rt1.close()
        rt2.close()


# ── install_default_hooks factory stub (filled in Step 3, nexus-ipyfj) ──────


def test_install_default_hooks_is_importable(tmp_path: Path) -> None:
    """The factory symbol is exported from nexus.runtime as a no-op stub in
    Step 1 so Step 3's mcp_infra shim can land without import shuffling.
    Behaviour (registering the three load-bearing batch hooks + the aspect
    extraction document hook) lands in nexus-ipyfj."""
    from nexus.runtime import NexusRuntime, install_default_hooks

    rt = NexusRuntime(config_dir=tmp_path / ".config")
    try:
        # No-op in Phase 1 Step 1; the real registrations land in Step 3.
        install_default_hooks(rt)
    finally:
        rt.close()


# ── Pytest runtime fixture (added in tests/conftest.py by this bead) ────────


def test_runtime_fixture_provides_default_runtime(runtime) -> None:
    """The ``runtime`` fixture constructs a NexusRuntime under tmp_path and
    sets it as the current ContextVar runtime."""
    from nexus.runtime import NexusRuntime, current_runtime

    assert isinstance(runtime, NexusRuntime)
    assert current_runtime() is runtime


def test_runtime_fixture_cleans_up_contextvar(request) -> None:
    """After the runtime fixture's teardown the ContextVar is reset back to
    its prior state. The fixture's contract: opt-in for tests that use it,
    no cross-test leakage."""
    from nexus.runtime import _runtime_var

    # Outside the fixture, ContextVar is None.
    assert _runtime_var.get() is None


# ── Process-default runtime + shim resolver (RDR-118 P1.S2, nexus-2bino) ────
#
# The legacy module-level accessors (``nexus.catalog.open_cached``,
# ``nexus.catalog.open_catalog``, ``nexus.catalog.reset_cache``) become
# thin redirectors that prefer the ContextVar runtime when set and fall
# back to a lazy-constructed process-default runtime otherwise. The
# process-default reads its config from env at first use so the
# existing autouse fixtures (``_isolate_catalog``, ``_isolate_config_dir``,
# ``_pin_storage_mode_direct_for_tests``) continue to drive per-test
# isolation. ``reset_cache()`` tears down the process-default so the
# next access reconstructs it against current env values.


def test_close_releases_t2_client(tmp_path: Path) -> None:
    """close() actively closes the runtime's shared T2Client. Mocks the
    T2 client via daemon-mode injection so we can assert .close() fired."""
    from nexus.runtime import NexusRuntime

    rt = NexusRuntime(config_dir=tmp_path / ".config", storage_mode="daemon")
    closed: list = []

    class _StubClient:
        def close(self) -> None:
            closed.append(True)

    rt._t2_client = _StubClient()
    rt.close()
    assert closed == [True]
    assert rt._t2_client is None


def test_ensure_runtime_for_shim_returns_contextvar_runtime(tmp_path: Path) -> None:
    """When a runtime is in context, the shim resolver returns that runtime
    rather than lazy-constructing a process default."""
    from nexus.runtime import (
        NexusRuntime,
        _close_process_default,
        _ensure_runtime_for_shim,
        _runtime_var,
    )

    _close_process_default()  # ensure clean slate
    rt = NexusRuntime(config_dir=tmp_path / ".config")
    token = _runtime_var.set(rt)
    try:
        assert _ensure_runtime_for_shim() is rt
    finally:
        _runtime_var.reset(token)
        rt.close()


def test_ensure_runtime_for_shim_constructs_process_default_from_env(
    tmp_path: Path, monkeypatch,
) -> None:
    """With no contextvar runtime, the shim resolver lazy-constructs a
    process-default runtime from env. Subsequent calls return the same
    instance until ``_close_process_default`` runs."""
    from nexus.runtime import (
        _close_process_default,
        _ensure_runtime_for_shim,
        _runtime_var,
    )

    _close_process_default()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / ".config"))
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "cat"))
    monkeypatch.setenv("NX_STORAGE_MODE", "direct")
    # Defensive: defeat any leaked contextvar runtime.
    token = _runtime_var.set(None)
    try:
        first = _ensure_runtime_for_shim()
        second = _ensure_runtime_for_shim()
        assert first is second
        assert first.config_dir == tmp_path / ".config"
        assert first.catalog_path == tmp_path / "cat"
        assert first.storage_mode == "direct"
    finally:
        _runtime_var.reset(token)
        _close_process_default()


def test_close_process_default_allows_reconstruction(
    tmp_path: Path, monkeypatch,
) -> None:
    """``_close_process_default`` tears down the process-default; the next
    ``_ensure_runtime_for_shim`` call reads current env. Pinning this so
    tests that flip env between cases see fresh process defaults."""
    from nexus.runtime import (
        _close_process_default,
        _ensure_runtime_for_shim,
        _runtime_var,
    )

    _close_process_default()
    token = _runtime_var.set(None)
    try:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg-a"))
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "cat-a"))
        monkeypatch.setenv("NX_STORAGE_MODE", "direct")
        first = _ensure_runtime_for_shim()
        assert first.catalog_path == tmp_path / "cat-a"

        _close_process_default()

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg-b"))
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "cat-b"))
        second = _ensure_runtime_for_shim()
        assert second is not first
        assert second.catalog_path == tmp_path / "cat-b"
    finally:
        _runtime_var.reset(token)
        _close_process_default()


def test_close_process_default_idempotent() -> None:
    """Calling ``_close_process_default`` twice in a row is a no-op."""
    from nexus.runtime import _close_process_default

    _close_process_default()
    _close_process_default()  # must not raise


# ── nexus.catalog module-level shims (RDR-118 P1.S2) ────────────────────────


def test_catalog_open_cached_shim_uses_runtime(tmp_path: Path, monkeypatch) -> None:
    """``nexus.catalog.open_cached(path)`` resolves through the runtime
    layer rather than a module-global cache. Two calls return the same
    Catalog (cached by ``(path, mode)`` on the resolved runtime)."""
    from nexus.runtime import _close_process_default, _runtime_var

    _close_process_default()
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "cat-shim"))
    monkeypatch.setenv("NX_STORAGE_MODE", "direct")
    token = _runtime_var.set(None)
    try:
        catalog_dir = tmp_path / "cat-shim"
        Catalog.init(catalog_dir)

        from nexus.catalog import open_cached

        first = open_cached(catalog_dir)
        second = open_cached(catalog_dir)
        assert first is second
    finally:
        _runtime_var.reset(token)
        _close_process_default()


def test_catalog_open_catalog_shim_returns_fresh(tmp_path: Path, monkeypatch) -> None:
    """``nexus.catalog.open_catalog(path)`` returns a fresh Catalog on every
    call; not cached. Mirrors the legacy ``open_catalog`` contract."""
    from nexus.runtime import _close_process_default, _runtime_var

    _close_process_default()
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "cat-fresh"))
    monkeypatch.setenv("NX_STORAGE_MODE", "direct")
    token = _runtime_var.set(None)
    try:
        catalog_dir = tmp_path / "cat-fresh"
        Catalog.init(catalog_dir)

        from nexus.catalog import open_catalog

        first = open_catalog(catalog_dir)
        second = open_catalog(catalog_dir)
        assert first is not second
    finally:
        _runtime_var.reset(token)
        _close_process_default()


def test_catalog_reset_cache_drops_runtime_cache(
    tmp_path: Path, monkeypatch,
) -> None:
    """``nexus.catalog.reset_cache()`` tears down the process-default runtime
    so the next ``open_cached`` constructs fresh. Pins the historical
    contract that tests rely on for between-case isolation."""
    from nexus.runtime import _close_process_default, _runtime_var

    _close_process_default()
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "cat-reset"))
    monkeypatch.setenv("NX_STORAGE_MODE", "direct")
    token = _runtime_var.set(None)
    try:
        catalog_dir = tmp_path / "cat-reset"
        Catalog.init(catalog_dir)

        from nexus.catalog import open_cached, reset_cache

        first = open_cached(catalog_dir)
        reset_cache()
        second = open_cached(catalog_dir)
        assert first is not second
    finally:
        _runtime_var.reset(token)
        _close_process_default()


# ── install_default_hooks + mcp_infra shim (RDR-118 P1.S3, nexus-ipyfj) ─────


def test_install_default_hooks_registers_three_batch_hooks(tmp_path: Path) -> None:
    """``install_default_hooks(runtime)`` registers the load-bearing
    chash-dual-write, taxonomy-assign, and manifest-write batch hooks
    on the runtime's HookRegistry."""
    from nexus.mcp_infra import (
        chash_dual_write_batch_hook,
        manifest_write_batch_hook,
        taxonomy_assign_batch_hook,
    )
    from nexus.runtime import NexusRuntime, install_default_hooks

    rt = NexusRuntime(config_dir=tmp_path / ".config")
    try:
        assert rt.hooks._batch == []
        install_default_hooks(rt)
        assert rt.hooks._batch == [
            chash_dual_write_batch_hook,
            taxonomy_assign_batch_hook,
            manifest_write_batch_hook,
        ]
    finally:
        rt.close()


def test_install_default_hooks_is_idempotent(tmp_path: Path) -> None:
    """Calling install_default_hooks a second time on the same runtime
    is a no-op (no duplicate registration)."""
    from nexus.runtime import NexusRuntime, install_default_hooks

    rt = NexusRuntime(config_dir=tmp_path / ".config")
    try:
        install_default_hooks(rt)
        install_default_hooks(rt)
        assert len(rt.hooks._batch) == 3
    finally:
        rt.close()


def test_install_default_hooks_no_auto_register_at_import(
    tmp_path: Path, monkeypatch,
) -> None:
    """R2 regression guard: simply importing nexus.mcp_infra must NOT
    register the load-bearing batch hooks. Only install_default_hooks
    does. Catches the silent-drop class where the hooks live in mcp_infra
    but never attach because the explicit factory call is missing.

    Pre-RDR-118 the three hooks self-registered at module load; that
    coupling is what RDR-118 P1.S3 retires."""
    from nexus.runtime import (
        NexusRuntime,
        _close_process_default,
        _runtime_var,
    )

    _close_process_default()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / ".config"))
    token = _runtime_var.set(None)
    try:
        rt = NexusRuntime(config_dir=tmp_path / ".config")
        try:
            # Re-import mcp_infra to confirm no side effect; the import
            # is idempotent so we are really asserting that nothing fired
            # the second time around either.
            import nexus.mcp_infra  # noqa: F401
            assert rt.hooks._batch == []
        finally:
            rt.close()
    finally:
        _runtime_var.reset(token)
        _close_process_default()


def test_mcp_infra_register_post_store_batch_hook_routes_through_runtime(
    runtime,
) -> None:
    """``mcp_infra.register_post_store_batch_hook(fn)`` appends to the
    runtime's HookRegistry via the shim, not to any module-level list."""
    from nexus.mcp_infra import register_post_store_batch_hook

    def probe(doc_ids, collection, contents, embeddings, metadatas):
        return None

    assert runtime.hooks._batch == []
    register_post_store_batch_hook(probe)
    assert runtime.hooks._batch == [probe]


def test_mcp_infra_fire_post_store_batch_hooks_routes_through_runtime(
    runtime,
) -> None:
    """``mcp_infra.fire_post_store_batch_hooks(...)`` iterates the
    runtime's HookRegistry via the shim and invokes each registered
    hook."""
    from nexus.mcp_infra import (
        fire_post_store_batch_hooks,
        register_post_store_batch_hook,
    )

    seen: list = []

    def hook(doc_ids, collection, contents, embeddings, metadatas):
        seen.append((tuple(doc_ids), collection))

    register_post_store_batch_hook(hook)
    fire_post_store_batch_hooks(
        ["d1", "d2"], "c1", ["t1", "t2"], None, None,
    )
    assert seen == [(("d1", "d2"), "c1")]


def test_mcp_infra_post_store_batch_hooks_proxy_list_ops(runtime) -> None:
    """``mcp_infra._post_store_batch_hooks`` proxies the runtime's
    batch list: iter, len, clear, append, extend, slice-assignment
    all forward to the live runtime storage."""
    import nexus.mcp_infra as _mod
    from nexus.mcp_infra import register_post_store_batch_hook

    def probe_a(doc_ids, collection, contents, embeddings, metadatas):
        return None

    def probe_b(doc_ids, collection, contents, embeddings, metadatas):
        return None

    register_post_store_batch_hook(probe_a)
    register_post_store_batch_hook(probe_b)

    assert len(_mod._post_store_batch_hooks) == 2
    assert list(_mod._post_store_batch_hooks) == [probe_a, probe_b]
    assert probe_a in _mod._post_store_batch_hooks

    # Slice assignment replaces the entire list and re-classifies.
    def probe_c(doc_ids, collection, contents, embeddings, metadatas):
        return None

    _mod._post_store_batch_hooks[:] = [probe_c]
    assert list(_mod._post_store_batch_hooks) == [probe_c]

    _mod._post_store_batch_hooks.clear()
    assert list(_mod._post_store_batch_hooks) == []


def test_mcp_infra_get_catalog_shim_uses_runtime(
    tmp_path: Path, monkeypatch,
) -> None:
    """``mcp_infra.get_catalog()`` resolves through the runtime layer
    when no override is set. Sets up an initialised catalog under
    tmp_path and verifies the shim returns it."""
    from nexus.runtime import (
        _close_process_default,
        _runtime_var,
    )

    catalog_dir = tmp_path / "shim-cat"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    monkeypatch.setenv("NX_STORAGE_MODE", "direct")
    _close_process_default()
    Catalog.init(catalog_dir)
    # Init went through the open_catalog shim which built a
    # process-default; tear it down so the test's get_catalog() call
    # below reconstructs against the env we set above.
    _close_process_default()
    token = _runtime_var.set(None)
    try:
        import nexus.mcp_infra as _mod
        from nexus.mcp_infra import get_catalog

        # Clear any test-patched override.
        _mod._catalog_instance = None

        cat = get_catalog()
        assert cat is not None
        assert isinstance(cat, Catalog)
    finally:
        _runtime_var.reset(token)
        _close_process_default()


def test_mcp_infra_get_catalog_shim_legacy_override_wins(
    tmp_path: Path, monkeypatch,
) -> None:
    """When ``_catalog_instance`` is set (test patch or
    ``inject_catalog``), ``get_catalog`` returns it without going
    through the runtime. Preserves the existing test surface."""
    import nexus.mcp_infra as _mod
    from nexus.mcp_infra import get_catalog

    catalog_dir = tmp_path / "legacy-override"
    Catalog.init(catalog_dir)
    sentinel = Catalog(catalog_dir, catalog_dir / ".catalog.db")
    monkeypatch.setattr(_mod, "_catalog_instance", sentinel)
    monkeypatch.setattr(_mod, "_catalog_mtime", 0.0)

    assert get_catalog() is sentinel


# ── P3.S1: accessor shims (nexus-s43yx) ─────────────────────────────────────


def test_config_nexus_config_dir_returns_runtime_value(runtime) -> None:
    """``nexus.config.nexus_config_dir()`` returns the active runtime's
    ``config_dir`` when a runtime is in context. P3.S1 consolidation:
    the function is a thin redirector to ``_ensure_runtime_for_shim``."""
    from nexus.config import nexus_config_dir

    assert nexus_config_dir() == runtime.config_dir


def test_config_catalog_path_returns_runtime_catalog_path(runtime) -> None:
    """``nexus.config.catalog_path()`` returns the runtime's
    ``catalog_path`` when set on the runtime."""
    from nexus.config import catalog_path

    assert runtime.catalog_path is not None
    assert catalog_path() == runtime.catalog_path


def test_config_default_db_path_derives_from_runtime(runtime) -> None:
    """``nexus.config.default_db_path()`` returns
    ``runtime.config_dir / "memory.db"`` through the shimmed
    ``nexus_config_dir`` call."""
    from nexus.config import default_db_path

    assert default_db_path() == runtime.config_dir / "memory.db"


def test_db_default_storage_mode_returns_runtime_mode(runtime) -> None:
    """``nexus.db.default_storage_mode()`` returns the runtime's
    ``storage_mode``."""
    from nexus.db import default_storage_mode

    assert default_storage_mode() == runtime.storage_mode


def test_db_is_daemon_mode_returns_runtime_mode_flag(tmp_path: Path) -> None:
    """``nexus.db.is_daemon_mode()`` returns True iff the runtime's
    storage_mode is daemon. Cycles through both modes to confirm the
    shim picks up the active runtime each time."""
    from nexus.db import is_daemon_mode
    from nexus.runtime import (
        NexusRuntime,
        _close_process_default,
        _runtime_var,
    )

    _close_process_default()
    rt_direct = NexusRuntime(
        config_dir=tmp_path / ".cfg-direct", storage_mode="direct",
    )
    rt_daemon = NexusRuntime(
        config_dir=tmp_path / ".cfg-daemon", storage_mode="daemon",
    )
    try:
        token = _runtime_var.set(rt_direct)
        try:
            assert is_daemon_mode() is False
        finally:
            _runtime_var.reset(token)

        token = _runtime_var.set(rt_daemon)
        try:
            assert is_daemon_mode() is True
        finally:
            _runtime_var.reset(token)
    finally:
        rt_direct.close()
        rt_daemon.close()
        _close_process_default()


def test_construct_process_default_validates_storage_mode_env(
    tmp_path: Path, monkeypatch,
) -> None:
    """Invalid ``NX_STORAGE_MODE`` env value at process-default
    construction raises ``ValueError``. Mirrors the legacy
    ``default_storage_mode()`` strictness preserved through the
    runtime layer (nexus-8qat)."""
    from nexus.runtime import (
        _close_process_default,
        _ensure_runtime_for_shim,
        _runtime_var,
    )

    _close_process_default()
    monkeypatch.setenv("NX_STORAGE_MODE", "damon")
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / ".cfg"))
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "cat"))
    token = _runtime_var.set(None)
    try:
        with pytest.raises(ValueError, match="NX_STORAGE_MODE.*'damon'"):
            _ensure_runtime_for_shim()
    finally:
        _runtime_var.reset(token)
        _close_process_default()


# ── P2.S1 + P2.S1b: single-doc + document chain migration (nexus-kekrs, nexus-f2ufy) ──


def test_install_default_hooks_registers_aspect_extraction_document_hook(
    tmp_path: Path,
) -> None:
    """RDR-118 P2.S1b: ``install_default_hooks`` registers the
    RDR-089 aspect-extraction enqueue hook on the document chain.
    Pre-P2.S1b the registration self-fired at ``mcp/core.py`` module
    load; the factory is now the single binding point."""
    from nexus.aspect_worker import aspect_extraction_enqueue_hook
    from nexus.runtime import NexusRuntime, install_default_hooks

    rt = NexusRuntime(config_dir=tmp_path / ".config")
    try:
        assert rt.hooks._document == []
        install_default_hooks(rt)
        assert aspect_extraction_enqueue_hook in rt.hooks._document
    finally:
        rt.close()


def test_install_default_hooks_aspect_hook_is_idempotent(tmp_path: Path) -> None:
    """Calling ``install_default_hooks`` twice does not double-register
    the document hook."""
    from nexus.aspect_worker import aspect_extraction_enqueue_hook
    from nexus.runtime import NexusRuntime, install_default_hooks

    rt = NexusRuntime(config_dir=tmp_path / ".config")
    try:
        install_default_hooks(rt)
        install_default_hooks(rt)
        assert rt.hooks._document.count(aspect_extraction_enqueue_hook) == 1
    finally:
        rt.close()


def test_hook_register_document_rejects_async_callable(tmp_path: Path) -> None:
    """RDR-118 P2.S1b: register_document raises on coroutine-returning
    callables. The legacy dispatcher silently dropped async hooks
    (the contract is synchronous-only and load-bearing for RDR-089).
    The new contract surfaces the violation at registration time
    instead of letting the async call return a dropped coroutine."""
    from nexus.runtime import NexusRuntime

    async def async_hook(source_path, collection, content):
        return None

    rt = NexusRuntime(config_dir=tmp_path / ".config")
    try:
        with pytest.raises(TypeError, match="async"):
            rt.hooks.register_document(async_hook)
    finally:
        rt.close()


def test_mcp_infra_register_post_store_hook_routes_through_runtime(
    runtime,
) -> None:
    """``mcp_infra.register_post_store_hook(fn)`` appends to the
    runtime's HookRegistry via the shim, not to any module-level list."""
    from nexus.mcp_infra import register_post_store_hook

    def probe(doc_id, collection, content):
        return None

    assert runtime.hooks._single == []
    register_post_store_hook(probe)
    assert runtime.hooks._single == [probe]


def test_mcp_infra_fire_post_store_hooks_routes_through_runtime(
    runtime,
) -> None:
    """``mcp_infra.fire_post_store_hooks(...)`` iterates the runtime's
    HookRegistry via the shim."""
    from nexus.mcp_infra import (
        fire_post_store_hooks,
        register_post_store_hook,
    )

    seen: list[tuple] = []

    def hook(doc_id, collection, content):
        seen.append((doc_id, collection, content))

    register_post_store_hook(hook)
    fire_post_store_hooks("doc-7", "c1", "hello")
    assert seen == [("doc-7", "c1", "hello")]


def test_mcp_infra_post_store_hooks_proxy_list_ops(runtime) -> None:
    """``mcp_infra._post_store_hooks`` is a proxy over the runtime's
    single-chain list. Iter, len, append, extend, clear forward to
    the live runtime storage so the legacy autouse fixture's
    snapshot / restore keeps working."""
    import nexus.mcp_infra as _mod

    def probe_a(doc_id, collection, content):
        return None

    def probe_b(doc_id, collection, content):
        return None

    _mod._post_store_hooks.append(probe_a)
    _mod._post_store_hooks.append(probe_b)

    assert len(_mod._post_store_hooks) == 2
    assert list(_mod._post_store_hooks) == [probe_a, probe_b]
    assert probe_a in _mod._post_store_hooks

    _mod._post_store_hooks.clear()
    assert list(_mod._post_store_hooks) == []


def test_mcp_infra_register_post_document_hook_routes_through_runtime(
    runtime,
) -> None:
    """``mcp_infra.register_post_document_hook(fn)`` appends to the
    runtime's document chain via the shim."""
    from nexus.mcp_infra import register_post_document_hook

    def probe(source_path, collection, content):
        return None

    assert runtime.hooks._document == []
    register_post_document_hook(probe)
    assert runtime.hooks._document == [probe]


def test_mcp_infra_fire_post_document_hooks_routes_through_runtime(
    runtime,
) -> None:
    """``mcp_infra.fire_post_document_hooks(...)`` iterates the
    runtime's HookRegistry via the shim with the doc_id passed
    through to ``doc_id``-aware hooks."""
    from nexus.mcp_infra import (
        fire_post_document_hooks,
        register_post_document_hook,
    )

    seen: list = []

    def hook(source_path, collection, content, *, doc_id: str = ""):
        seen.append((source_path, doc_id))

    register_post_document_hook(hook)
    fire_post_document_hooks(
        "/tmp/file.md", "knowledge__x", "body", doc_id="d-1",
    )
    assert seen == [("/tmp/file.md", "d-1")]


def test_mcp_infra_post_document_hooks_proxy_list_ops(runtime) -> None:
    """``mcp_infra._post_document_hooks`` and
    ``_post_document_hooks_with_doc_id`` are proxies over the runtime's
    document chain storage."""
    import nexus.mcp_infra as _mod

    def probe_legacy(source_path, collection, content):
        return None

    def probe_with_doc_id(source_path, collection, content, *, doc_id: str = ""):
        return None

    _mod._post_document_hooks.append(probe_legacy)
    _mod._post_document_hooks.append(probe_with_doc_id)

    assert len(_mod._post_document_hooks) == 2
    assert list(_mod._post_document_hooks) == [probe_legacy, probe_with_doc_id]
    assert id(probe_with_doc_id) in _mod._post_document_hooks_with_doc_id
    assert id(probe_legacy) not in _mod._post_document_hooks_with_doc_id

    _mod._post_document_hooks.clear()
    assert list(_mod._post_document_hooks) == []
    assert list(_mod._post_document_hooks_with_doc_id) == []


def test_catalog_open_cached_prefers_contextvar_runtime(
    tmp_path: Path, monkeypatch,
) -> None:
    """When a runtime is in context, ``nexus.catalog.open_cached`` resolves
    via that runtime (not the process-default). Ensures the new
    ``runtime`` fixture wins over any lazy fallback."""
    from nexus.runtime import (
        NexusRuntime,
        _close_process_default,
        _runtime_var,
    )

    _close_process_default()
    # Pollute env to a different path so the process-default would diverge
    # if the shim resolved through it.
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "wrong"))
    monkeypatch.setenv("NX_STORAGE_MODE", "direct")
    catalog_dir = tmp_path / "right"
    Catalog.init(catalog_dir)
    rt = NexusRuntime(
        config_dir=tmp_path / ".config",
        catalog_path=catalog_dir,
        storage_mode="direct",
    )
    token = _runtime_var.set(rt)
    try:
        from nexus.catalog import open_cached

        cat = open_cached(catalog_dir)
        # The Catalog must come from the context runtime's cache, not from
        # the process default.
        assert (catalog_dir, "direct") in rt._cached
        assert rt._cached[(catalog_dir, "direct")] is cat
    finally:
        _runtime_var.reset(token)
        rt.close()
        _close_process_default()
