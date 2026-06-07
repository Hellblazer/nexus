# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the per-store backend-selection flag (RDR-152 bead nexus-gmiaf.4).

Resolution precedence (narrowest wins):
  1. Per-store env var  NX_STORAGE_BACKEND_<STORE>=service|sqlite
  2. Global env var     NX_STORAGE_BACKEND=service|sqlite
  3. Hard default       'sqlite'

A config-file layer is reserved for Phase 2+ and is NOT implemented here.

All invalid values raise StorageModeFlagError immediately (no silent fallback).
"""
from __future__ import annotations

import pytest

from nexus.db.storage_mode import (
    VALID_STORE_NAMES,
    StorageBackend,
    StorageModeFlagError,
    storage_backend_for,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all NX_STORAGE_BACKEND* env vars so tests start from a clean slate."""
    monkeypatch.delenv("NX_STORAGE_BACKEND", raising=False)
    for store in VALID_STORE_NAMES:
        monkeypatch.delenv(f"NX_STORAGE_BACKEND_{store.upper()}", raising=False)


# ── default: all stores resolve to 'sqlite' ──────────────────────────────────


@pytest.mark.parametrize("store", VALID_STORE_NAMES)
def test_default_is_sqlite_for_every_store(
    store: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    assert storage_backend_for(store) == StorageBackend.SQLITE


def test_default_returns_sqlite_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    result = storage_backend_for("memory")
    assert result == "sqlite"


# ── per-store env override ────────────────────────────────────────────────────


def test_per_store_env_sets_service_for_that_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND_MEMORY", "service")
    assert storage_backend_for("memory") == StorageBackend.SERVICE


def test_per_store_env_does_not_affect_other_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND_MEMORY", "service")
    for store in VALID_STORE_NAMES:
        if store != "memory":
            assert storage_backend_for(store) == StorageBackend.SQLITE, store


def test_per_store_env_sqlite_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND_PLANS", "sqlite")
    assert storage_backend_for("plans") == StorageBackend.SQLITE


def test_per_store_env_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND_MEMORY", "SERVICE")
    assert storage_backend_for("memory") == StorageBackend.SERVICE


# ── global env override ───────────────────────────────────────────────────────


def test_global_env_flips_all_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
    for store in VALID_STORE_NAMES:
        assert storage_backend_for(store) == StorageBackend.SERVICE, store


def test_global_env_sqlite_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")
    for store in VALID_STORE_NAMES:
        assert storage_backend_for(store) == StorageBackend.SQLITE, store


# ── precedence: per-store env beats global env ────────────────────────────────


def test_per_store_env_beats_global_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-store SQLite override wins even when global is service."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
    monkeypatch.setenv("NX_STORAGE_BACKEND_MEMORY", "sqlite")
    assert storage_backend_for("memory") == StorageBackend.SQLITE
    # Other stores still inherit the global
    for store in VALID_STORE_NAMES:
        if store != "memory":
            assert storage_backend_for(store) == StorageBackend.SERVICE, store


def test_per_store_service_beats_global_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-store service override wins even when global is sqlite."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("NX_STORAGE_BACKEND_PLANS", "service")
    assert storage_backend_for("plans") == StorageBackend.SERVICE
    for store in VALID_STORE_NAMES:
        if store != "plans":
            assert storage_backend_for(store) == StorageBackend.SQLITE, store


# ── error cases ───────────────────────────────────────────────────────────────


def test_invalid_global_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND", "direct")
    with pytest.raises(StorageModeFlagError, match="NX_STORAGE_BACKEND"):
        storage_backend_for("memory")


def test_invalid_per_store_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND_MEMORY", "direct")
    with pytest.raises(StorageModeFlagError, match="NX_STORAGE_BACKEND_MEMORY"):
        storage_backend_for("memory")


def test_unknown_store_name_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    with pytest.raises(StorageModeFlagError, match="unknown store"):
        storage_backend_for("bogus_store")


def test_empty_global_env_is_treated_as_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty string env var must not be treated as invalid -- it means 'unset'."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND", "")
    assert storage_backend_for("memory") == StorageBackend.SQLITE


def test_empty_per_store_env_is_treated_as_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND_MEMORY", "")
    assert storage_backend_for("memory") == StorageBackend.SQLITE


def test_nx_storage_mode_daemon_does_not_collide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy NX_STORAGE_MODE=daemon (RDR-120) must NOT affect the new resolver.

    An operator with NX_STORAGE_MODE=daemon set should see no change in
    behaviour -- the new resolver reads NX_STORAGE_BACKEND only.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
    assert storage_backend_for("memory") == StorageBackend.SQLITE


# ── store name normalization ──────────────────────────────────────────────────


def test_store_name_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Callers may pass 'MEMORY' or 'Memory'; both resolve correctly."""
    _clear_env(monkeypatch)
    assert storage_backend_for("MEMORY") == StorageBackend.SQLITE
    assert storage_backend_for("Memory") == StorageBackend.SQLITE


# ── VALID_STORE_NAMES drift guard ────────────────────────────────────────────


def test_valid_store_names_covers_t2database_attributes(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """VALID_STORE_NAMES must cover every eagerly-constructed domain-store
    attribute on T2Database, so that adding a new store to T2Database without
    updating VALID_STORE_NAMES causes this test to fail.

    Asymmetry handled:
    - ``catalog`` is lazily constructed via a property (not in __dict__ after
      __init__); it IS in VALID_STORE_NAMES by explicit contract.
    - ``t1`` is forward-declared (not a T2Database attribute); it IS in
      VALID_STORE_NAMES by explicit contract.
    - ``RENAME_LOCK`` and ``_path``, ``_catalog``, ``_catalog_db_path_override``
      are not stores -- excluded by naming convention (upper-case or leading _).
    """
    from pathlib import Path

    import nexus.db.t2 as t2_mod

    # Temporarily enable auto-migrate so construction works without a running daemon.
    orig = t2_mod._DEFAULT_RUN_MIGRATIONS
    t2_mod._DEFAULT_RUN_MIGRATIONS = True
    try:
        from nexus.db.t2 import T2Database

        db = T2Database(Path(tmp_path) / "drift_guard.db")  # type: ignore[arg-type]
        try:
            # Collect eagerly-constructed public store attributes: lower-case,
            # not starting with '_', not ALL_CAPS (RENAME_LOCK), not a method/property.
            import inspect

            store_attrs = {
                name
                for name, val in vars(db).items()
                if (
                    not name.startswith("_")
                    and name != name.upper()  # exclude ALL_CAPS like RENAME_LOCK
                    and not inspect.ismethod(val)
                    and not inspect.isfunction(val)
                )
            }
            # Every eager store attribute must be in VALID_STORE_NAMES.
            missing = store_attrs - VALID_STORE_NAMES
            assert not missing, (
                f"T2Database has domain-store attribute(s) not in VALID_STORE_NAMES: "
                f"{sorted(missing)}.  Add them to nexus.db.storage_mode.VALID_STORE_NAMES."
            )
        finally:
            db.close()
    finally:
        t2_mod._DEFAULT_RUN_MIGRATIONS = orig


# ── seam: memory branch is live, default behavior unchanged ──────────────────


def test_t2database_sqlite_seam_constructs_memory_store(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default (sqlite) path: T2Database construction succeeds and db.memory is
    the concrete MemoryStore.  Confirms the seam is live and routes correctly.
    """
    _clear_env(monkeypatch)
    from pathlib import Path

    import nexus.db.t2 as t2_mod

    orig = t2_mod._DEFAULT_RUN_MIGRATIONS
    t2_mod._DEFAULT_RUN_MIGRATIONS = True
    try:
        from nexus.db.t2 import T2Database
        from nexus.db.t2.memory_store import MemoryStore

        db = T2Database(Path(tmp_path) / "seam_sqlite.db")  # type: ignore[arg-type]
        try:
            assert isinstance(db.memory, MemoryStore)
        finally:
            db.close()
    finally:
        t2_mod._DEFAULT_RUN_MIGRATIONS = orig


def test_t2database_service_backend_raises_not_implemented(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NX_STORAGE_BACKEND_MEMORY=service -> T2Database construction raises
    NotImplementedError (the .7 wiring point; not yet implemented).
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND_MEMORY", "service")
    from pathlib import Path

    import nexus.db.t2 as t2_mod

    orig = t2_mod._DEFAULT_RUN_MIGRATIONS
    t2_mod._DEFAULT_RUN_MIGRATIONS = True
    try:
        from nexus.db.t2 import T2Database

        with pytest.raises(NotImplementedError, match="nexus-gmiaf.7"):
            T2Database(Path(tmp_path) / "seam_service.db")  # type: ignore[arg-type]
    finally:
        t2_mod._DEFAULT_RUN_MIGRATIONS = orig
