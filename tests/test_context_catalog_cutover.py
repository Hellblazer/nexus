# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-137 Phase 3.5: context.py catalog cutover regression (nexus-tts0d.10).

The original nexus-9iw41 bug: ``_repo_collections`` read
``~/.config/nexus/repos.json`` directly and returned a phantom
``docs__1-2188`` collection name registered to the nexus repo,
while the catalog and chroma both had the real ``docs__1-1``. The
SessionStart Knowledge Map injection then loaded topic labels from
the phantom (empty) collection and produced duplicate / wrong
labels.

The cutover threads the catalog-backed reader (``nexus.repos.read_dual``,
shipped by ``nexus-tts0d.4``) through ``_repo_collections`` so the
catalog answer wins whenever both sources have data. This file is
the regression contract: seed the catalog with the truth, seed
``repos.json`` with the phantom, assert the catalog value is what
``_repo_collections`` returns.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest
import structlog

from nexus.catalog.catalog import Catalog
from nexus.context import _repo_collections
from nexus.registry import RepoRegistry


@pytest.fixture(autouse=True)
def _enable_debug_logging():
    """Bump structlog so the shim's DEBUG fallback / disagreement events
    fire under the test."""
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    yield
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "nexus"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    return r


@pytest.fixture
def cat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Catalog:
    """A catalog rooted at tmp_path with both config-dir + catalog-path env
    overrides wired.

    The conftest-level ``_isolate_catalog`` fixture sets ``NEXUS_CATALOG_PATH``
    to a non-existent ``<tmp>/test-catalog`` so production hooks don't write
    to the user's real catalog. We need ``catalog_path()`` to resolve to
    OUR seeded catalog directory instead, so override both env vars: this
    fixture's ``cat`` is what ``_repo_collections`` opens via
    ``nexus.config.catalog_path()``.
    """
    cfg = tmp_path / "config"
    cat_dir = cfg / "catalog"
    cat_dir.mkdir(parents=True)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(cat_dir))
    Catalog.init(cat_dir)
    return Catalog(cat_dir, cat_dir / ".catalog.db")


def _register_owner_with_docs(cat: Catalog, repo: Path, docs_name: str) -> None:
    """Mint an owner + register the docs collection against it."""
    owner = cat.ensure_owner_for_repo(repo)
    owner_id = str(owner).replace(".", "-")
    cat.register_collection(
        docs_name,
        content_type="docs",
        owner_id=owner_id,
        embedding_model="voyage-context-3",
        model_version="1",
    )


class TestPhantomCollectionRegression:
    def test_catalog_wins_over_registry_phantom(
        self, cat: Catalog, repo: Path, tmp_path: Path,
    ) -> None:
        """nexus-9iw41 reproduction: catalog has the real docs__1-1,
        registry has the phantom docs__1-2188. The reader must surface
        the catalog's value."""
        # Seed catalog with the truth.
        _register_owner_with_docs(cat, repo, "docs__1-1__voyage-context-3__v1")
        # Seed registry with the phantom (the bug scenario).
        cfg = Path(__import__("os").environ["NEXUS_CONFIG_DIR"])
        reg_path = cfg / "repos.json"
        reg_path.parent.mkdir(parents=True, exist_ok=True)
        reg = RepoRegistry(reg_path)
        reg.add(repo)
        # Hand-corrupt the registry entry to mimic the production drift
        # that produced nexus-9iw41: docs_collection points at a
        # phantom name the catalog disagrees with.
        reg.update(
            repo,
            collection="code__nexus-1-2188__voyage-code-3__v1",
            code_collection="code__nexus-1-2188__voyage-code-3__v1",
            docs_collection="docs__1-2188",
        )

        colls = _repo_collections(repo)
        assert colls is not None
        # The catalog's docs collection wins.
        assert "docs__1-1__voyage-context-3__v1" in colls
        # The phantom does NOT appear.
        assert "docs__1-2188" not in colls

    def test_unregistered_repo_returns_none(
        self, cat: Catalog, repo: Path,
    ) -> None:
        """No catalog owner, no registry entry → None (signals
        ``inject everything`` upstream)."""
        assert _repo_collections(repo) is None

    def test_registry_fallback_when_catalog_empty(
        self, cat: Catalog, repo: Path,
    ) -> None:
        """A repo only known to the registry (pre-catalog install) still
        produces a result via the shim's fallback branch."""
        cfg = Path(__import__("os").environ["NEXUS_CONFIG_DIR"])
        reg_path = cfg / "repos.json"
        reg = RepoRegistry(reg_path)
        reg.add(repo)
        colls = _repo_collections(repo)
        assert colls is not None
        # At least the code_collection (which RepoRegistry.add seeds)
        # should appear.
        assert any(c.startswith("code__") for c in colls)
