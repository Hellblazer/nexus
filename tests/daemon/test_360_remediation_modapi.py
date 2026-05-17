# SPDX-License-Identifier: AGPL-3.0-or-later
"""Second 360° remediation Bundle C (nexus-qggv + nexus-9n32).

Module-boundary fixes (S360-mod):
- TuplespaceService exposes ``tuple_index()`` and ``registry()`` public
  accessors so the daemon and other sibling code do not reach into
  private attributes via ``getattr``.

Public-API surface fixes (S360-api):
- ``InvalidTimeoutError`` re-exported from ``nexus.tuplespace``.
- ``BindingWatcher`` / ``DataVersionWatcher`` renamed without the
  leading underscore (Serena-driven) so production importers no
  longer violate the underscore-private convention.
- ``nexus.daemon`` package docstring documents the per-submodule
  import contract (option B in the bead).
"""
from __future__ import annotations

from pathlib import Path

import chromadb
import pytest


_REGISTRY_YAML = """
name: tasks/<project>
tier: project
content_type: text
embed_from: content
dimensions:
  status: { type: enum, values: [open, done], required: true }
take:
  enabled: true
  mode: semantic
  floor: 0.0
  margin: 0.0
  default_lease_seconds: 60
read:
  default_floor: 0.0
  default_n: 100
tiers: [project]
retention_seconds: 86400
"""


@pytest.fixture()
def _registry(tmp_path: Path):
    from nexus.tuplespace.registry import Registry

    d = tmp_path / "builtin"
    d.mkdir()
    (d / "tasks.yml").write_text(_REGISTRY_YAML)
    return Registry.load(d)


@pytest.fixture()
def _chroma():
    client = chromadb.EphemeralClient()
    for coll in client.list_collections():
        client.delete_collection(coll.name)
    yield client
    for coll in client.list_collections():
        client.delete_collection(coll.name)


# ---------------------------------------------------------------------------
# nexus-qggv: TuplespaceService public accessors
# ---------------------------------------------------------------------------


class TestTuplespaceServiceAccessors:
    def test_tuple_index_returns_live_index(
        self, tmp_path: Path, _registry, _chroma
    ) -> None:
        from nexus.daemon.tuplespace_service import TuplespaceService
        from nexus.tuplespace.index import TupleIndex

        service = TuplespaceService(
            tuples_db_path=tmp_path / "tuples.db",
            chroma_client=_chroma,
            registry=_registry,
        )
        try:
            idx = service.tuple_index()
            assert isinstance(idx, TupleIndex)
            assert idx is service._index
        finally:
            service.close()

    def test_registry_returns_loaded_registry(
        self, tmp_path: Path, _registry, _chroma
    ) -> None:
        from nexus.daemon.tuplespace_service import TuplespaceService
        from nexus.tuplespace.registry import Registry

        service = TuplespaceService(
            tuples_db_path=tmp_path / "tuples.db",
            chroma_client=_chroma,
            registry=_registry,
        )
        try:
            reg = service.registry()
            assert isinstance(reg, Registry)
            assert reg is _registry
        finally:
            service.close()


# ---------------------------------------------------------------------------
# nexus-9n32 S1: InvalidTimeoutError reachable from package root
# ---------------------------------------------------------------------------


class TestTuplespacePackageExports:
    def test_invalid_timeout_error_importable_from_package(self) -> None:
        from nexus.tuplespace import InvalidTimeoutError
        from nexus.tuplespace.api import (
            InvalidTimeoutError as _from_submodule,
        )
        assert InvalidTimeoutError is _from_submodule
        assert issubclass(InvalidTimeoutError, ValueError)

    def test_invalid_timeout_error_in_dunder_all(self) -> None:
        import nexus.tuplespace as ts

        assert "InvalidTimeoutError" in ts.__all__


# ---------------------------------------------------------------------------
# nexus-9n32 S3: watcher renames remove the underscore-private prefix
# ---------------------------------------------------------------------------


class TestWatcherClassesArePublic:
    def test_binding_watcher_public_name(self) -> None:
        from nexus.cockpit.bindings import BindingWatcher
        assert BindingWatcher.__name__ == "BindingWatcher"

    def test_data_version_watcher_public_name(self) -> None:
        from nexus.tuplespace.watcher import DataVersionWatcher
        assert DataVersionWatcher.__name__ == "DataVersionWatcher"

    def test_old_private_names_no_longer_exported(self) -> None:
        from nexus.cockpit import bindings as cockpit_bindings
        from nexus.tuplespace import watcher as ts_watcher

        assert not hasattr(cockpit_bindings, "_BindingWatcher"), (
            "Old underscore-private _BindingWatcher still exposed; "
            "the rename should remove it."
        )
        assert not hasattr(ts_watcher, "_DataVersionWatcher"), (
            "Old underscore-private _DataVersionWatcher still exposed; "
            "the rename should remove it."
        )


# ---------------------------------------------------------------------------
# nexus-9n32 S2: daemon package documents the per-submodule import contract
# ---------------------------------------------------------------------------


class TestDaemonPackageDocumentsImportContract:
    def test_docstring_lists_canonical_submodule_paths(self) -> None:
        import nexus.daemon as daemon_pkg
        doc = (daemon_pkg.__doc__ or "")
        # Each canonical submodule import path is named in the docstring
        # so a new contributor reading `nexus/daemon/__init__.py` knows
        # the per-submodule contract is intentional, not an oversight.
        assert "nexus.daemon.t2_client" in doc
        assert "nexus.daemon.tuplespace_service" in doc
        assert "BlockingTakeResourceExhausted" in doc
