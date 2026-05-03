# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the RDR SessionStart hook.

RDR-103 Phase 3b: ``rdr_hook.py`` resolves the indexed collection name
through the catalog (via ``Catalog.collection_for_repo``) instead of
constructing the legacy 2-segment ``rdr__{repo_name}`` shape inline.
The test surface pins:

  - The hook helper returns the conformant ``CollectionName.render()``
    when the catalog is initialized and the repo has a registered owner.
  - The helper falls back to the legacy 2-segment name when the catalog
    is absent (so the hook continues to function on operator
    workstations that have not yet run ``nx catalog setup``). Phase 5
    will tighten this fallback.
  - The helper returns the legacy shape when the catalog is initialized
    but the repo has no owner row (the case before the first
    ``nx index repo`` populates the owner via ``_catalog_hook``).
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_PATH = REPO_ROOT / "nx" / "hooks" / "scripts" / "rdr_hook.py"


@pytest.fixture()
def rdr_hook_module():
    """Import ``rdr_hook.py`` as a module so we can call its helpers
    directly. The script is not on the import path by default — load
    it via spec_from_file_location."""
    spec = importlib.util.spec_from_file_location("rdr_hook_under_test", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def catalog_with_owner(tmp_path, monkeypatch):
    """Set up a catalog at tmp_path/catalog with one registered owner."""
    from nexus.catalog.catalog import Catalog

    cat_dir = tmp_path / "catalog"
    cat = Catalog.init(cat_dir)
    cat.register_owner(
        name="myproject",
        owner_type="repo",
        repo_hash="cafef00d",
        repo_root=str(tmp_path / "myproject"),
    )
    monkeypatch.setattr("nexus.config.catalog_path", lambda: cat_dir)
    monkeypatch.setattr(
        "nexus.registry._repo_identity",
        lambda r: ("myproject", "cafef00d"),
    )
    return cat_dir


def test_resolve_rdr_collection_uses_catalog_when_initialized(
    rdr_hook_module, catalog_with_owner, tmp_path,
):
    repo = tmp_path / "myproject"
    repo.mkdir()
    name = rdr_hook_module._resolve_rdr_collection(repo)
    # Owner 1.1 to owner_segment 1-1; canonical model voyage-context-3;
    # new tuple lands at v1.
    assert name == "rdr__1-1__voyage-context-3__v1"


def test_resolve_rdr_collection_falls_back_when_catalog_absent(
    rdr_hook_module, tmp_path, monkeypatch,
):
    """No catalog at the configured path: helper falls back to the
    legacy 2-segment shape. This keeps the SessionStart hook functional
    on operator workstations that have not initialized the catalog.
    Phase 5 tightens this.
    """
    repo = tmp_path / "isolated"
    repo.mkdir()
    monkeypatch.setattr(
        "nexus.config.catalog_path",
        lambda: tmp_path / "no_such_catalog",
    )
    name = rdr_hook_module._resolve_rdr_collection(repo)
    assert name == "rdr__isolated"


def test_resolve_rdr_collection_falls_back_when_owner_unregistered(
    rdr_hook_module, tmp_path, monkeypatch,
):
    """Catalog initialized but no owner registered for this repo:
    helper falls back to legacy. Mirrors the order-of-operations
    fallback in indexer.py's ``_repo_collection_or_legacy``.
    """
    from nexus.catalog.catalog import Catalog

    cat_dir = tmp_path / "catalog"
    Catalog.init(cat_dir)
    monkeypatch.setattr("nexus.config.catalog_path", lambda: cat_dir)

    repo = tmp_path / "fresh"
    repo.mkdir()
    monkeypatch.setattr(
        "nexus.registry._repo_identity",
        lambda r: ("fresh", "deadbeef"),
    )
    name = rdr_hook_module._resolve_rdr_collection(repo)
    assert name == "rdr__fresh"
