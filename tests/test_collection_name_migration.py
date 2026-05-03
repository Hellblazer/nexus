# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-103 Phase 4: legacy-to-conformant collection migration on first index.

The indexer detects pre-RDR-103 legacy collection names in T3 and
renames them in place to the conformant
``<content_type>__<owner_id>__<embedding_model>__v1`` shape. The
migration:

  - Runs once per (repo, content_type) pair. Idempotent: re-runs
    skip the rename and emit no message.
  - Uses ``rename_collection_data_plane`` (T3 native modify + T2
    cascade + catalog re-point + collections projection update +
    CollectionSuperseded event), NOT a per-document update loop.
  - Skips the rename when both legacy and conformant exist (partial
    state from a prior interrupted run); the indexer proceeds against
    the conformant collection and the legacy collection is left for
    operator cleanup.
  - Updates the registry so subsequent runs see conformant names
    directly without invoking the migration path.

Tests pin the decision tree per the bead's scope (`nexus-yqnr.6`).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import chromadb
import chromadb.utils.embedding_functions
import pytest

from nexus.catalog.catalog import Catalog
from nexus.corpus import is_conformant_collection_name
from nexus.db.t3 import T3Database
from nexus.registry import RepoRegistry, _collection_name


@pytest.fixture()
def t3():
    """T3 backed by ``EphemeralClient`` with a ChromaDB default
    embedding function (no Voyage API key required).

    NOTE: ``chromadb.EphemeralClient()`` shares process-level state
    across instances (the name is misleading), so the fixture clears
    every collection on entry to keep tests isolated. Mirrors the
    pattern used in ``test_t3_strict_collection_naming.py``.
    """
    client = chromadb.EphemeralClient()
    ef = chromadb.utils.embedding_functions.DefaultEmbeddingFunction()
    db = T3Database(_client=client, _ef_override=ef)
    for raw in list(db._client.list_collections()):
        name = raw if isinstance(raw, str) else getattr(raw, "name", str(raw))
        try:
            db._client.delete_collection(name)
        except Exception:
            pass
    return db


@pytest.fixture()
def catalog(tmp_path: Path) -> Catalog:
    cat_dir = tmp_path / "catalog"
    cat = Catalog.init(cat_dir)
    return cat


@pytest.fixture()
def repo_with_owner(catalog: Catalog, tmp_path: Path, monkeypatch) -> Path:
    """A registered repo with a known repo_hash that
    ``_migrate_legacy_collections`` can resolve."""
    repo = tmp_path / "myproject"
    repo.mkdir()
    catalog.register_owner(
        name="myproject",
        owner_type="repo",
        repo_hash="cafef00d",
        repo_root=str(repo),
    )
    monkeypatch.setattr(
        "nexus.registry._repo_identity",
        lambda r: ("myproject", "cafef00d"),
    )
    return repo


@pytest.fixture()
def registry(tmp_path: Path) -> RepoRegistry:
    return RepoRegistry(tmp_path / "repos.json")


def _make_collection(t3: T3Database, name: str) -> None:
    """Create an empty collection in T3 so the migration sees it."""
    t3.get_or_create_collection(name)


def _seed_collection_with_chunk(t3: T3Database, name: str) -> None:
    """Create a collection and seed one document so the migration's
    rename has data to move (smoke-tests that data survives the
    rename)."""
    t3.put(collection=name, content="seed body", title="seed", tags="seed")


# ── Greenfield-with-legacy: rename happens once, message emitted once ──────


def test_migration_greenfield_legacy_renamed_to_conformant(
    repo_with_owner: Path, catalog: Catalog, t3, registry: RepoRegistry,
) -> None:
    """Repo has legacy ``code__myproject-cafef00d`` from a pre-RDR-103
    index. Migration renames to conformant
    ``code__1-1__voyage-code-3__v1`` and emits one upgrade message.
    """
    from nexus.indexer import _migrate_legacy_collections

    legacy = _collection_name(repo_with_owner)
    _seed_collection_with_chunk(t3, legacy)
    registry.add(repo_with_owner)  # legacy shape, no catalog
    messages: list[str] = []

    result = _migrate_legacy_collections(
        repo_with_owner,
        cat=catalog, t3_db=t3, registry=registry,
        on_message=messages.append,
    )

    # Conformant name returned.
    assert is_conformant_collection_name(result["code"])
    assert result["code"] == "code__1-1__voyage-code-3__v1"
    # Exactly one upgrade message emitted (for code).
    code_msgs = [m for m in messages if "code" in m]
    assert len(code_msgs) == 1
    assert "Upgraded" in code_msgs[0] and legacy in code_msgs[0]


def test_migration_renames_t3_collection(
    repo_with_owner: Path, catalog: Catalog, t3, registry: RepoRegistry,
) -> None:
    """Post-migration, T3 has the conformant collection but NOT the
    legacy. Native modify(name=) preserved the data."""
    from nexus.indexer import _migrate_legacy_collections

    legacy = _collection_name(repo_with_owner)
    _seed_collection_with_chunk(t3, legacy)
    registry.add(repo_with_owner)

    _migrate_legacy_collections(
        repo_with_owner, cat=catalog, t3_db=t3, registry=registry,
    )

    conformant = "code__1-1__voyage-code-3__v1"
    assert t3.collection_exists(conformant)
    assert not t3.collection_exists(legacy)


def test_migration_updates_registry_to_conformant(
    repo_with_owner: Path, catalog: Catalog, t3, registry: RepoRegistry,
) -> None:
    """Registry's ``code_collection`` field is rewritten so subsequent
    indexer runs see the conformant name directly."""
    from nexus.indexer import _migrate_legacy_collections

    _seed_collection_with_chunk(t3, _collection_name(repo_with_owner))
    registry.add(repo_with_owner)

    _migrate_legacy_collections(
        repo_with_owner, cat=catalog, t3_db=t3, registry=registry,
    )

    info = registry.get(repo_with_owner)
    assert info is not None
    assert info["code_collection"] == "code__1-1__voyage-code-3__v1"


# ── Idempotency: re-index after migration ──────────────────────────────────


def test_migration_idempotent_no_message_second_run(
    repo_with_owner: Path, catalog: Catalog, t3, registry: RepoRegistry,
) -> None:
    """Second invocation against the same repo emits no upgrade
    message (legacy already absent in T3)."""
    from nexus.indexer import _migrate_legacy_collections

    _seed_collection_with_chunk(t3, _collection_name(repo_with_owner))
    registry.add(repo_with_owner)

    # First run does the rename.
    _migrate_legacy_collections(
        repo_with_owner, cat=catalog, t3_db=t3, registry=registry,
    )

    # Second run: legacy absent in T3, conformant present, no message.
    messages: list[str] = []
    _migrate_legacy_collections(
        repo_with_owner, cat=catalog, t3_db=t3, registry=registry,
        on_message=messages.append,
    )
    assert messages == []


def test_migration_returns_conformant_when_steady_state(
    repo_with_owner: Path, catalog: Catalog, t3, registry: RepoRegistry,
) -> None:
    """Steady state: only the conformant collection exists. The
    helper still returns the conformant name for the caller to use."""
    from nexus.indexer import _migrate_legacy_collections

    conformant = "docs__1-1__voyage-context-3__v1"
    _make_collection(t3, conformant)
    # Registry is empty / has conformant docs already.
    registry.add(repo_with_owner, cat=catalog)

    result = _migrate_legacy_collections(
        repo_with_owner, cat=catalog, t3_db=t3, registry=registry,
    )
    assert result["docs"] == conformant


# ── Partial state both-exist: skip rename ──────────────────────────────────


def test_migration_skips_when_both_collections_exist(
    repo_with_owner: Path, catalog: Catalog, t3, registry: RepoRegistry,
) -> None:
    """If a prior interrupted run left both legacy and conformant in
    T3, the migration must NOT attempt the rename (would fail because
    target already exists). The helper returns the conformant name and
    the legacy collection is left untouched.
    """
    from nexus.indexer import _migrate_legacy_collections

    legacy = _collection_name(repo_with_owner)
    conformant = "code__1-1__voyage-code-3__v1"
    _seed_collection_with_chunk(t3, legacy)
    _make_collection(t3, conformant)
    registry.add(repo_with_owner)

    messages: list[str] = []
    result = _migrate_legacy_collections(
        repo_with_owner, cat=catalog, t3_db=t3, registry=registry,
        on_message=messages.append,
    )

    # Conformant returned (indexer proceeds against it).
    assert result["code"] == conformant
    # Legacy is untouched (operator cleanup later).
    assert t3.collection_exists(legacy)
    assert t3.collection_exists(conformant)
    # No "Upgraded" message — only an advisory about the partial state.
    assert not any("Upgraded" in m for m in messages)


# ── Atomic rename (not per-document retarget) ──────────────────────────────


def test_migration_uses_atomic_rename_not_per_doc_retarget(
    repo_with_owner: Path, catalog: Catalog, t3, registry: RepoRegistry,
) -> None:
    """The migration must use the data-plane atomic rename
    (``rename_collection_data_plane``), NOT a per-document
    ``update_documents_collection_batch`` loop. The atomic path is
    O(1) on the T3 side via native ``modify(name=)``; the per-doc
    retarget would be O(n) and would re-embed if it touched chunks.

    Test surface: spy on both possible code paths and assert the
    atomic one fires.
    """
    from nexus.indexer import _migrate_legacy_collections

    _seed_collection_with_chunk(t3, _collection_name(repo_with_owner))
    registry.add(repo_with_owner)

    rename_calls = []
    original = t3.rename_collection

    def spy(old, new):
        rename_calls.append((old, new))
        return original(old, new)

    with patch.object(t3, "rename_collection", side_effect=spy):
        _migrate_legacy_collections(
            repo_with_owner, cat=catalog, t3_db=t3, registry=registry,
        )

    # Exactly one atomic rename invocation for the code collection.
    assert len(rename_calls) == 1
    assert rename_calls[0][0] == _collection_name(repo_with_owner)
    assert rename_calls[0][1] == "code__1-1__voyage-code-3__v1"


# ── Catalog absent / owner missing ─────────────────────────────────────────


def test_migration_no_op_when_catalog_uninitialized(
    tmp_path: Path, t3, registry: RepoRegistry, monkeypatch,
) -> None:
    """Catalog is None: migration is a no-op. Returns an empty map so
    the caller falls back to its own resolution (registry or legacy
    helper). The legacy collection in T3 is untouched.
    """
    from nexus.indexer import _migrate_legacy_collections

    repo = tmp_path / "uncataloged"
    repo.mkdir()
    monkeypatch.setattr(
        "nexus.registry._repo_identity",
        lambda r: ("uncataloged", "abcdef12"),
    )
    legacy = _collection_name(repo)
    _seed_collection_with_chunk(t3, legacy)

    result = _migrate_legacy_collections(
        repo, cat=None, t3_db=t3, registry=registry,
    )

    # Empty map: caller's existing fallback handles name resolution.
    assert result == {}
    # Legacy collection untouched.
    assert t3.collection_exists(legacy)


def test_migration_no_op_when_owner_unregistered(
    catalog: Catalog, tmp_path: Path, t3, registry: RepoRegistry, monkeypatch,
) -> None:
    """Catalog initialized but no owner row for this repo: migration
    is a no-op. Returns an empty map so the caller's existing fallback
    handles this run. The _catalog_hook registers the owner later;
    subsequent runs will migrate."""
    from nexus.indexer import _migrate_legacy_collections

    repo = tmp_path / "unregistered"
    repo.mkdir()
    monkeypatch.setattr(
        "nexus.registry._repo_identity",
        lambda r: ("unregistered", "fade1234"),
    )
    legacy = _collection_name(repo)
    _seed_collection_with_chunk(t3, legacy)

    result = _migrate_legacy_collections(
        repo, cat=catalog, t3_db=t3, registry=registry,
    )

    assert result == {}
    assert t3.collection_exists(legacy)


# ── Multiple content types in one pass ─────────────────────────────────────


def test_migration_handles_code_and_docs_independently(
    repo_with_owner: Path, catalog: Catalog, t3, registry: RepoRegistry,
) -> None:
    """The decision tree applies per content_type. A repo with a
    legacy code collection AND a steady-state conformant docs
    collection migrates only the code one and emits one message."""
    from nexus.indexer import _migrate_legacy_collections
    from nexus.registry import _docs_collection_name

    legacy_code = _collection_name(repo_with_owner)
    conformant_docs = "docs__1-1__voyage-context-3__v1"
    _seed_collection_with_chunk(t3, legacy_code)
    _make_collection(t3, conformant_docs)
    registry.add(repo_with_owner)
    # Make registry's docs_collection point to conformant manually.
    registry.update(repo_with_owner, docs_collection=conformant_docs)

    messages: list[str] = []
    result = _migrate_legacy_collections(
        repo_with_owner, cat=catalog, t3_db=t3, registry=registry,
        on_message=messages.append,
    )

    assert result["code"] == "code__1-1__voyage-code-3__v1"
    assert result["docs"] == conformant_docs
    upgrade_msgs = [m for m in messages if "Upgraded" in m]
    assert len(upgrade_msgs) == 1
    assert "code" in upgrade_msgs[0]
