# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the per-store backend-selection flag (RDR-152 bead nexus-gmiaf.4).

Resolution precedence (narrowest wins):
  1. Per-store env var  NX_STORAGE_MODE_<STORE>=service|sqlite
  2. Global env var     NX_STORAGE_MODE=service|sqlite
  3. Config file        nexus.storage_mode.<store> or nexus.storage_mode.default
  4. Hard default       'sqlite'

All invalid values raise StorageModeFlagError immediately (no silent fallback).
"""
from __future__ import annotations

import pytest

from nexus.db.storage_mode import (
    VALID_STORE_NAMES,
    StorageBackend,
    StorageModeFlagError,
    storage_mode_for,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all NX_STORAGE_MODE* env vars so tests start from a clean slate."""
    monkeypatch.delenv("NX_STORAGE_MODE", raising=False)
    for store in VALID_STORE_NAMES:
        monkeypatch.delenv(f"NX_STORAGE_MODE_{store.upper()}", raising=False)


# ── default: all stores resolve to 'sqlite' ──────────────────────────────────


@pytest.mark.parametrize("store", VALID_STORE_NAMES)
def test_default_is_sqlite_for_every_store(
    store: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    assert storage_mode_for(store) == StorageBackend.SQLITE


def test_default_returns_sqlite_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    result = storage_mode_for("memory")
    assert result == "sqlite"


# ── per-store env override ────────────────────────────────────────────────────


def test_per_store_env_sets_service_for_that_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_MODE_MEMORY", "service")
    assert storage_mode_for("memory") == StorageBackend.SERVICE


def test_per_store_env_does_not_affect_other_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_MODE_MEMORY", "service")
    for store in VALID_STORE_NAMES:
        if store != "memory":
            assert storage_mode_for(store) == StorageBackend.SQLITE, store


def test_per_store_env_sqlite_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_MODE_PLANS", "sqlite")
    assert storage_mode_for("plans") == StorageBackend.SQLITE


def test_per_store_env_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_MODE_MEMORY", "SERVICE")
    assert storage_mode_for("memory") == StorageBackend.SERVICE


# ── global env override ───────────────────────────────────────────────────────


def test_global_env_flips_all_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_MODE", "service")
    for store in VALID_STORE_NAMES:
        assert storage_mode_for(store) == StorageBackend.SERVICE, store


def test_global_env_sqlite_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_MODE", "sqlite")
    for store in VALID_STORE_NAMES:
        assert storage_mode_for(store) == StorageBackend.SQLITE, store


# ── precedence: per-store env beats global env ────────────────────────────────


def test_per_store_env_beats_global_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-store SQLite override wins even when global is service."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_MODE", "service")
    monkeypatch.setenv("NX_STORAGE_MODE_MEMORY", "sqlite")
    assert storage_mode_for("memory") == StorageBackend.SQLITE
    # Other stores still inherit the global
    for store in VALID_STORE_NAMES:
        if store != "memory":
            assert storage_mode_for(store) == StorageBackend.SERVICE, store


def test_per_store_service_beats_global_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-store service override wins even when global is sqlite."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_MODE", "sqlite")
    monkeypatch.setenv("NX_STORAGE_MODE_PLANS", "service")
    assert storage_mode_for("plans") == StorageBackend.SERVICE
    for store in VALID_STORE_NAMES:
        if store != "plans":
            assert storage_mode_for(store) == StorageBackend.SQLITE, store


# ── error cases ───────────────────────────────────────────────────────────────


def test_invalid_global_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_MODE", "direct")
    with pytest.raises(StorageModeFlagError, match="NX_STORAGE_MODE"):
        storage_mode_for("memory")


def test_invalid_per_store_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_MODE_MEMORY", "direct")
    with pytest.raises(StorageModeFlagError, match="NX_STORAGE_MODE_MEMORY"):
        storage_mode_for("memory")


def test_unknown_store_name_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    with pytest.raises(StorageModeFlagError, match="unknown store"):
        storage_mode_for("bogus_store")


def test_empty_global_env_is_treated_as_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty string env var must not be treated as invalid — it means 'unset'."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_MODE", "")
    assert storage_mode_for("memory") == StorageBackend.SQLITE


def test_empty_per_store_env_is_treated_as_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_MODE_MEMORY", "")
    assert storage_mode_for("memory") == StorageBackend.SQLITE


# ── store name normalization ──────────────────────────────────────────────────


def test_store_name_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Callers may pass 'MEMORY' or 'Memory'; both resolve correctly."""
    _clear_env(monkeypatch)
    assert storage_mode_for("MEMORY") == StorageBackend.SQLITE
    assert storage_mode_for("Memory") == StorageBackend.SQLITE


# ── seam: T2Database constructor still instantiates sqlite stores ─────────────


def test_t2database_all_sqlite_seam_unchanged(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With all stores on sqlite (the default), T2Database construction succeeds
    and store attributes are the same concrete classes as before .4.

    This is the 'seam is wired but behavior unchanged' contract test.
    """
    _clear_env(monkeypatch)
    from pathlib import Path

    import nexus.db.t2 as t2_mod

    orig = t2_mod._DEFAULT_RUN_MIGRATIONS
    t2_mod._DEFAULT_RUN_MIGRATIONS = True
    try:
        db_path = tmp_path / "test_seam.db"  # type: ignore[operator]
        from nexus.db.t2 import T2Database
        from nexus.db.t2.memory_store import MemoryStore
        from nexus.db.t2.plan_library import PlanLibrary

        db = T2Database(Path(db_path))
        try:
            assert isinstance(db.memory, MemoryStore)
            assert isinstance(db.plans, PlanLibrary)
        finally:
            db.close()
    finally:
        t2_mod._DEFAULT_RUN_MIGRATIONS = orig
