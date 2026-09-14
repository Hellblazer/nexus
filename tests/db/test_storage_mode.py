# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the storage-backend env guard (RDR-158 P3, nexus-7bomn).

Since P3 removed the ``=sqlite`` opt-out there is exactly one backend:
``storage_backend_for`` always returns SERVICE or raises. These tests pin

  * the hard default (service, every store);
  * ``=service`` still resolving at both env layers, any case;
  * ``=sqlite`` HARD-ERRORING at both env layers with the
    stranded-install redirect (never resolving, never silently ignored);
  * precedence (narrowest wins) surviving the collapse: a per-store
    ``=service`` shields exactly that store from a global ``=sqlite``;
  * the generic invalid-value error and the unknown-store error;
  * the VALID_STORE_NAMES / T2Database drift guard.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

from nexus.db.storage_mode import (
    T2_FACADE_STORES,
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


# ── default: all stores resolve to 'service' ─────────────────────────────────


@pytest.mark.parametrize("store", sorted(VALID_STORE_NAMES))
def test_default_is_service_for_every_store(
    store: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    assert storage_backend_for(store) == StorageBackend.SERVICE


def test_default_returns_service_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    result = storage_backend_for("memory")
    assert result == "service"


# ── =service still resolves (both layers, case-insensitive) ──────────────────


def test_per_store_env_service_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND_MEMORY", "service")
    assert storage_backend_for("memory") == StorageBackend.SERVICE


def test_global_env_service_resolves_all_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
    for store in VALID_STORE_NAMES:
        assert storage_backend_for(store) == StorageBackend.SERVICE, store


def test_env_value_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND_MEMORY", "SERVICE")
    assert storage_backend_for("memory") == StorageBackend.SERVICE


# ── =sqlite is a HARD ERROR with the stranded-install redirect ───────────────
#
# RDR-158 P3 (nexus-7bomn): the opt-out is removed, and it must fail LOUD —
# silently resolving to the engine would hand an operator who explicitly
# asked for the old SQLite baseline a green run testing the wrong substrate
# (the silent-fallback class the project bans).


def test_global_sqlite_hard_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")
    for store in VALID_STORE_NAMES:
        with pytest.raises(StorageModeFlagError, match="retired SQLite storage backend"):
            storage_backend_for(store)


def test_per_store_sqlite_hard_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND_PLANS", "sqlite")
    with pytest.raises(StorageModeFlagError, match="NX_STORAGE_BACKEND_PLANS"):
        storage_backend_for("plans")
    # Other stores are untouched by the per-store var.
    for store in VALID_STORE_NAMES:
        if store != "plans":
            assert storage_backend_for(store) == StorageBackend.SERVICE, store


def test_sqlite_error_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND", "SQLite")
    with pytest.raises(StorageModeFlagError, match="retired SQLite storage backend"):
        storage_backend_for("memory")


def test_sqlite_error_carries_the_stranded_install_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The message must carry the two-hop redirect, not just a refusal.

    Pins the load-bearing content: the env key (so the operator can find
    the setting), the migration verb (``nx upgrade`` — the ladder verb a
    user can find in ``--help``, per the RDR-185 verb demotion), the
    round-trip through the last migration-capable release, and the
    ``nx doctor`` detector pointer.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")
    with pytest.raises(StorageModeFlagError) as excinfo:
        storage_backend_for("memory")
    msg = str(excinfo.value)
    assert "NX_STORAGE_BACKEND" in msg
    assert "nx upgrade" in msg
    assert "migration-capable" in msg
    assert "nx doctor" in msg


def test_per_store_sqlite_error_names_the_per_store_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The redirect must name the variable that actually tripped it —
    an operator told about NX_STORAGE_BACKEND will not find the
    per-store override they exported."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND_MEMORY", "sqlite")
    with pytest.raises(StorageModeFlagError) as excinfo:
        storage_backend_for("memory")
    assert "NX_STORAGE_BACKEND_MEMORY=sqlite" in str(excinfo.value)


# ── precedence: narrowest wins, unchanged by the collapse ────────────────────


def test_per_store_service_shields_from_global_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-store =service wins before the global layer is even read, so the
    shielded store resolves while every other store still hard-errors."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("NX_STORAGE_BACKEND_PLANS", "service")
    assert storage_backend_for("plans") == StorageBackend.SERVICE
    for store in VALID_STORE_NAMES:
        if store != "plans":
            with pytest.raises(StorageModeFlagError):
                storage_backend_for(store)


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
    assert storage_backend_for("memory") == StorageBackend.SERVICE


def test_empty_per_store_env_is_treated_as_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND_MEMORY", "")
    assert storage_backend_for("memory") == StorageBackend.SERVICE


def test_nx_storage_mode_daemon_does_not_collide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy NX_STORAGE_MODE=daemon (RDR-120) must NOT affect this resolver."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
    assert storage_backend_for("memory") == StorageBackend.SERVICE


# ── store name normalization ──────────────────────────────────────────────────


def test_store_name_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Callers may pass 'MEMORY' or 'Memory'; both resolve correctly."""
    _clear_env(monkeypatch)
    assert storage_backend_for("MEMORY") == StorageBackend.SERVICE
    assert storage_backend_for("Memory") == StorageBackend.SERVICE


# ── the enum has exactly one member ──────────────────────────────────────────


def test_storage_backend_enum_has_no_sqlite_member() -> None:
    """The opt-out backend is GONE, not merely unreachable: nothing may
    reconstruct a SQLITE member to branch on (RDR-158 P3)."""
    assert [m.name for m in StorageBackend] == ["SERVICE"]


# ── VALID_STORE_NAMES drift guards ───────────────────────────────────────────


def test_t2_facade_stores_is_valid_names_minus_catalog_and_t1() -> None:
    """T2_FACADE_STORES (what T2Database validates at construction) must be
    exactly VALID_STORE_NAMES minus the two documented exclusions, so a new
    store added to one set cannot silently miss the other."""
    assert set(T2_FACADE_STORES) == VALID_STORE_NAMES - {"catalog", "t1"}


_T2_INIT_PATH = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "src" / "nexus" / "db" / "t2" / "__init__.py"
)
_HTTP_CALL_NAME = re.compile(r"^_*Http")


def _t2database_constructed_domain_store_names() -> frozenset[str]:
    """AST-derive the set of domain-store attribute names
    ``T2Database.__init__`` actually constructs: every ``self.<name>:
    <Type> = <Type>(...)`` annotated assignment inside ``__init__`` whose
    call target's name matches ``Http*`` (after stripping any leading
    underscores from an aliased import, e.g. ``_HttpTaxonomyStore``).
    This deliberately excludes ``self._path`` (no ``Http*`` call) and
    ``self.RENAME_LOCK`` (``threading.RLock()``, not ``Http*``) -- the
    two other annotated ``self.`` assignments in the same method that are
    not domain stores.

    ``self._taxonomy`` is mapped to the public name ``taxonomy``: the
    store is constructed onto the private ``_taxonomy`` attribute, but
    every caller -- and ``T2_FACADE_STORES`` itself -- names it
    ``taxonomy`` (the property it is read through).

    Pure AST walk, no import of ``nexus.db.t2`` and no ``T2Database``
    instantiation (which would require a live engine endpoint) --
    complements ``test_valid_store_names_covers_t2database_attributes``,
    which checks one direction (constructed subset of named) via a real
    instantiation; this walk checks both directions against
    ``T2_FACADE_STORES`` specifically, statically.
    """
    tree = ast.parse(_T2_INIT_PATH.read_text(), filename=str(_T2_INIT_PATH))
    class_node = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "T2Database"
    )
    init_node = next(
        n for n in class_node.body
        if isinstance(n, ast.FunctionDef) and n.name == "__init__"
    )

    names: set[str] = set()
    for node in ast.walk(init_node):
        if not isinstance(node, ast.AnnAssign):
            continue
        target = node.target
        if not (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
        ):
            continue
        if not isinstance(node.value, ast.Call) or not isinstance(node.value.func, ast.Name):
            continue
        if not _HTTP_CALL_NAME.match(node.value.func.id):
            continue
        attr = target.attr
        names.add("taxonomy" if attr == "_taxonomy" else attr)
    return frozenset(names)


def test_t2_facade_stores_matches_t2database_ast_construction() -> None:
    """T2_FACADE_STORES must equal exactly the set of domain-store
    attributes T2Database.__init__ actually constructs, so a store added
    to one without the other (as ``tuples`` -- HttpTupleStore, RDR-205 --
    went missing from several fan-out tests' hand-typed store lists for a
    while) goes red here instead of silently drifting."""
    constructed = _t2database_constructed_domain_store_names()
    assert constructed == set(T2_FACADE_STORES), (
        f"T2Database.__init__ constructs {sorted(constructed)} but "
        f"T2_FACADE_STORES is {sorted(T2_FACADE_STORES)} -- a domain "
        f"store was added to (or removed from) one without the other"
    )


def test_valid_store_names_covers_t2database_attributes(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """VALID_STORE_NAMES must cover every eagerly-constructed domain-store
    attribute on T2Database, so that adding a new store to T2Database without
    updating VALID_STORE_NAMES causes this test to fail.

    Asymmetry handled:
    - ``catalog`` is the engine catalog (not a T2Database attribute); it IS
      in VALID_STORE_NAMES by explicit contract.
    - ``t1`` is forward-declared (not a T2Database attribute); it IS in
      VALID_STORE_NAMES by explicit contract.
    - ``RENAME_LOCK`` and ``_path``-style privates are not stores --
      excluded by naming convention (upper-case or leading _).
    """
    from pathlib import Path

    from nexus.db.t2 import T2Database

    # RDR-158 P4 Stage 4 (nexus-i711w): the auto-migrate plumbing is deleted;
    # construction never migrates, so no default-flip is needed here.
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


# ── the catalog factory is a validation point ────────────────────────────────


def test_catalog_factory_hard_errors_on_catalog_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RDR-158 Stage 5 (sweeper open question e): with the local catalog
    deleted, nothing else resolves ``storage_backend_for("catalog")`` — the
    factory is the seam that keeps a stranded
    ``NX_STORAGE_BACKEND_CATALOG=sqlite`` export fail-loud instead of
    silently ignored."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "sqlite")
    from nexus.catalog.factory import make_catalog_reader, make_catalog_writer

    with pytest.raises(StorageModeFlagError, match="NX_STORAGE_BACKEND_CATALOG"):
        make_catalog_reader()
    with pytest.raises(StorageModeFlagError, match="NX_STORAGE_BACKEND_CATALOG"):
        make_catalog_writer()


# ── T2Database construction is a validation point ────────────────────────────


def test_t2database_construction_hard_errors_on_global_sqlite(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stranded shell export must fail at the facade, not be silently
    ignored: with the per-site selectors collapsed, T2Database.__init__ is
    the T2 entry seam that still reads the env (nexus-7bomn)."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")
    from pathlib import Path

    from nexus.db.t2 import T2Database

    with pytest.raises(StorageModeFlagError, match="retired SQLite storage backend"):
        T2Database(Path(tmp_path) / "hard_error.db", run_migrations=False)  # type: ignore[arg-type]


def test_t2database_construction_hard_errors_on_per_store_sqlite(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-store opt-outs must trip the same guard — validating only the
    global var would let NX_STORAGE_BACKEND_PLANS=sqlite rot silently."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", "sqlite")
    from pathlib import Path

    from nexus.db.t2 import T2Database

    with pytest.raises(
        StorageModeFlagError, match="NX_STORAGE_BACKEND_DOCUMENT_ASPECTS"
    ):
        T2Database(Path(tmp_path) / "hard_error2.db", run_migrations=False)  # type: ignore[arg-type]


def test_t2database_service_backend_uses_http_memory_store(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default construction path yields the HttpMemoryStore — the only
    memory store since the SQLite MemoryStore deletion (nexus-i711w A3)."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("NX_SERVICE_PORT", "19999")
    monkeypatch.setenv("NX_SERVICE_TOKEN", "test-token-for-seam")
    from pathlib import Path

    from nexus.db.t2.http_memory_store import HttpMemoryStore

    db = None
    try:
        from nexus.db.t2 import T2Database

        db = T2Database(Path(tmp_path) / "seam_service.db")  # type: ignore[arg-type]
        assert isinstance(db.memory, HttpMemoryStore), (
            f"Expected HttpMemoryStore, got {type(db.memory).__name__}"
        )
    finally:
        if db is not None:
            try:
                db.memory.close()
            except Exception:
                pass
