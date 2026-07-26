# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-9n485 — RDR-103 migration probe surfaces tombstoned candidates.

``_migrate_legacy_collections`` (RDR-103 Phase 4) used ``t3_db.collection_exists``
directly to find a migratable legacy candidate. On the service
(HttpVectorClient) path that reads the tombstone-filtered stats view, so a
legacy collection whose every chunk belongs to a trashed document read
exactly like one that never existed — the decision tree silently fell to
"case 4: greenfield" and wrote fresh chunks to a brand-new conformant
collection, orphaning the tombstoned legacy collection forever with no
trace in the logs (bead nexus-9n485's "trash-then-reindex would create a
duplicate fresh collection beside the tombstoned one").

This is an OBSERVABILITY fix, not a decision-tree change. Whether a
tombstoned collection should instead be renamed into the fresh target (and
so implicitly un-orphaned while its documents stay trashed) is its own
design question, deliberately out of scope for this bead — see the
caller-semantics table in the bead. What changes here is that the
tombstoned case is no longer silent: a structlog warning now names the
candidate and explains why it was skipped, so an operator/auditor can find
the orphaned collection instead of it vanishing without a trace. The
decision path itself (which name is returned, whether a rename fires) is
UNCHANGED and continues to be pinned by the existing
``tests/test_collection_name_migration.py`` suite (all real-T3Database, so
it exercises none of this — Chroma has no tombstone concept and never
emits these warnings).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from nexus.catalog.catalog import Catalog
from nexus.db.http_vector_client import HttpVectorClient
from nexus.db.storage_mode import StorageBackend
from nexus.registry import RepoRegistry

pytestmark = pytest.mark.usefixtures("cloud_mode")

STATS_PATH = "/v1/vectors/stats"
COLLECTIONS_PATH = "/v1/vectors/collections"


@pytest.fixture(autouse=True)
def _pin_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real production pairing: HttpVectorClient always runs under the
    SERVICE storage backend. The global unit-suite default pins SQLITE
    (tests/conftest.py's ``_pin_storage_backend_sqlite``), which would
    route ``rename_collection_data_plane`` into the local/Chroma fan-out
    branch that calls ``t3_db.rename_collection(...)`` — a method
    HttpVectorClient does not implement (SERVICE mode never calls it; the
    Java service re-homes the pgvector chunks inside its own atomic
    transaction instead).
    """
    monkeypatch.setattr(
        "nexus.db.storage_mode.storage_backend_for",
        lambda store: StorageBackend.SERVICE,
    )


@pytest.fixture()
def catalog(tmp_path: Path) -> Catalog:
    return Catalog.init(tmp_path / "catalog")


@pytest.fixture()
def repo_with_owner(catalog: Catalog, tmp_path: Path, monkeypatch) -> Path:
    repo = tmp_path / "myproject"
    repo.mkdir()
    catalog.register_owner(
        name="myproject", owner_type="repo", repo_hash="cafef00d",
        repo_root=str(repo),
    )
    monkeypatch.setattr(
        "nexus.repo_identity._repo_identity",
        lambda r: ("myproject", "cafef00d"),
    )
    return repo


@pytest.fixture()
def registry(tmp_path: Path) -> RepoRegistry:
    return RepoRegistry(tmp_path / "repos.json")


def _patch_get(monkeypatch, *, stats_names: set[str], raw_names: set[str]) -> None:
    def fake_get(path: str, *, tenant: str = "default") -> Any:
        if path == STATS_PATH:
            return [
                {"name": n, "dim": 384, "count": 1, "last_write": "x"}
                for n in stats_names
            ]
        if path == COLLECTIONS_PATH:
            return [{"name": n} for n in raw_names]
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr("nexus.db.http_vector_client._get", fake_get)


def test_tombstoned_legacy_candidate_logs_warning_not_silently_greenfield(
    repo_with_owner: Path, catalog: Catalog, registry: RepoRegistry, monkeypatch,
) -> None:
    from nexus.indexer import _legacy_collection_name, _migrate_legacy_collections

    legacy = _legacy_collection_name(repo_with_owner, "code")
    # legacy has physical chunk rows (raw) but zero live chunks (stats) --
    # every document under it is trashed.
    _patch_get(monkeypatch, stats_names=set(), raw_names={legacy})
    t3_db = HttpVectorClient()
    registry.add(repo_with_owner)

    with capture_logs() as cap:
        result = _migrate_legacy_collections(
            repo_with_owner, cat=catalog, t3_db=t3_db, registry=registry,
        )

    # Decision path UNCHANGED: tombstoned legacy is not treated as
    # migratable, so the caller proceeds against the fresh conformant name
    # (case 4 / greenfield) exactly as before this bead's fix.
    assert result["code"] == "code__1-1__voyage-code-3__v1"

    tombstone_events = [
        e for e in cap
        if e.get("event") == "phase4_migration_candidate_tombstoned"
        and e.get("ct") == "code"
    ]
    assert len(tombstone_events) == 1, cap
    assert tombstone_events[0]["candidate"] == legacy


def test_present_legacy_candidate_no_tombstone_warning(
    repo_with_owner: Path, catalog: Catalog, registry: RepoRegistry, monkeypatch,
) -> None:
    """Sanity control: a LIVE legacy candidate proceeds through the
    existing rename path with no tombstone warning at all."""
    from nexus.indexer import _legacy_collection_name, _migrate_legacy_collections

    legacy = _legacy_collection_name(repo_with_owner, "code")
    _patch_get(monkeypatch, stats_names={legacy}, raw_names={legacy})
    t3_db = HttpVectorClient()
    registry.add(repo_with_owner)
    writer = MagicMock()
    writer.rename_collection_cascade = MagicMock(return_value={})

    with capture_logs() as cap:
        _migrate_legacy_collections(
            repo_with_owner, cat=catalog, t3_db=t3_db, registry=registry,
            writer=writer,
        )

    tombstone_events = [e for e in cap if e.get("event") == "phase4_migration_candidate_tombstoned"]
    assert tombstone_events == []
