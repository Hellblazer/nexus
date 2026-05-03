# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-103 Phase 3a invariant: greenfield indexing produces conformant
collection names.

Tests cover the seam where the indexer asks for a collection name:

  - ``RepoRegistry.add(repo, cat=cat)`` populates ``code_collection``
    and ``docs_collection`` with conformant names when a catalog with
    a registered owner is supplied.
  - ``RepoRegistry.add(repo)`` (no catalog) preserves the legacy shape
    so callers that have not yet wired in the catalog do not break.
  - The indexer's ``_repo_collection_or_legacy`` returns conformant
    names when the catalog is initialized with a registered owner.

These are the unit-level invariants. Full-pipeline e2e coverage lives
in ``tests/test_indexer_e2e.py``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog
from nexus.corpus import is_conformant_collection_name
from nexus.registry import RepoRegistry


@pytest.fixture()
def catalog(tmp_path):
    """Production-shaped catalog: SQLite at ``<cat_dir>/.catalog.db`` so
    that ``_repo_collection_or_legacy`` (which constructs a fresh Catalog
    at the same convention) sees the same backing store as the test.

    Also creates the ``.git/`` and ``documents.jsonl`` markers that
    ``Catalog.is_initialized`` checks so the helper's gate succeeds.
    """
    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir()
    (cat_dir / ".git").mkdir()
    (cat_dir / "documents.jsonl").touch()
    return Catalog(catalog_dir=cat_dir, db_path=cat_dir / ".catalog.db")


@pytest.fixture()
def repo_with_owner(catalog, tmp_path, monkeypatch):
    """Greenfield repo with a registered catalog owner under a known hash."""
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


# ── RepoRegistry.add: catalog-aware path ────────────────────────────────

def test_repo_registry_add_with_catalog_emits_conformant(
    repo_with_owner: Path, catalog: Catalog, tmp_path: Path,
) -> None:
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(repo_with_owner, cat=catalog)
    info = reg.get(repo_with_owner)
    assert info is not None
    assert is_conformant_collection_name(info["code_collection"])
    assert is_conformant_collection_name(info["docs_collection"])


def test_repo_registry_add_with_catalog_uses_canonical_models(
    repo_with_owner: Path, catalog: Catalog, tmp_path: Path,
) -> None:
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(repo_with_owner, cat=catalog)
    info = reg.get(repo_with_owner)
    assert "voyage-code-3" in info["code_collection"]
    assert "voyage-context-3" in info["docs_collection"]


def test_repo_registry_add_with_catalog_lands_at_v1(
    repo_with_owner: Path, catalog: Catalog, tmp_path: Path,
) -> None:
    """First-time registration in a fresh catalog lands at ``v1``."""
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(repo_with_owner, cat=catalog)
    info = reg.get(repo_with_owner)
    assert info["code_collection"].endswith("__v1")
    assert info["docs_collection"].endswith("__v1")


# ── RepoRegistry.add: cat=None (legacy fallback) ────────────────────────

def test_repo_registry_add_without_catalog_preserves_legacy(tmp_path: Path) -> None:
    """When ``cat=None``, the legacy ``code__<basename>-<hash8>`` /
    ``docs__<basename>-<hash8>`` shape is preserved. This keeps callers
    that have not yet wired in the catalog working through the
    transitional Phase 3a window."""
    repo = tmp_path / "legacyrepo"
    repo.mkdir()
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(repo)  # no cat
    info = reg.get(repo)
    assert info is not None
    assert info["code_collection"].startswith("code__")
    assert info["docs_collection"].startswith("docs__")
    # Legacy 2-segment shape; not conformant per RDR-103.
    assert not is_conformant_collection_name(info["code_collection"])
    assert not is_conformant_collection_name(info["docs_collection"])


def test_repo_registry_add_catalog_without_owner_falls_back(
    catalog: Catalog, tmp_path: Path, monkeypatch,
) -> None:
    """When a catalog is supplied but no owner is registered for the
    repo, ``add`` falls back to the legacy helper instead of raising.
    This handles the order-of-operations case in
    ``commands/index.py:_index_repo`` where ``reg.add`` runs BEFORE
    ``_catalog_hook`` registers the owner.
    """
    repo = tmp_path / "no_owner_repo"
    repo.mkdir()
    monkeypatch.setattr(
        "nexus.registry._repo_identity",
        lambda r: ("no_owner_repo", "deadbeef"),
    )
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(repo, cat=catalog)
    info = reg.get(repo)
    assert info is not None
    # Legacy fallback fires; not conformant.
    assert not is_conformant_collection_name(info["code_collection"])


# ── _repo_collection_or_legacy ─────────────────────────────────────────

def test_repo_collection_or_legacy_uses_catalog_when_initialized(
    repo_with_owner: Path, catalog: Catalog, monkeypatch, tmp_path: Path,
) -> None:
    """The indexer-side helper returns the conformant name when a
    catalog is initialized at the configured path and the owner exists."""
    cat_dir = tmp_path / "catalog"
    monkeypatch.setattr("nexus.config.catalog_path", lambda: cat_dir)

    from nexus.indexer import _repo_collection_or_legacy

    name = _repo_collection_or_legacy(repo_with_owner, "code")
    assert is_conformant_collection_name(name)


def test_repo_collection_or_legacy_falls_back_when_catalog_absent(
    tmp_path: Path, monkeypatch,
) -> None:
    """When no catalog is initialized at the configured path, the
    helper falls back to the legacy registry function."""
    repo = tmp_path / "isolated"
    repo.mkdir()
    monkeypatch.setattr("nexus.config.catalog_path", lambda: tmp_path / "no_such_catalog")
    monkeypatch.setattr(
        "nexus.registry._repo_identity",
        lambda r: ("isolated", "abcdef12"),
    )

    from nexus.indexer import _repo_collection_or_legacy

    name = _repo_collection_or_legacy(repo, "docs")
    assert name == "docs__isolated-abcdef12"
    assert not is_conformant_collection_name(name)
