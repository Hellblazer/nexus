# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-137 Phase 4 close gate (nexus-tts0d.17, OQ-5).

End-to-end fixture test that exercises ``nx index repo . --corpus
knowledge`` through the catalog-backed writer (PR for
``nexus-tts0d.16``) and reader (PR for ``nexus-tts0d.4``) without
``repos.json`` ever being created.

The OQ-5 lock states the ``--corpus knowledge`` opt-in must
materialise as a ``knowledge__*`` collection registered to the owner
in the catalog. Subsequent reads see the ``knowledge__*`` collection
and prefer it over ``docs__*`` in the canonical docs slot.

This test simulates the writer + reader cycle without invoking the
real T3 indexer (no Voyage credentials needed): exercises the
``_CatalogBackedRegistry.update(docs_collection=...)`` path that
mints the ``knowledge__*`` row, then reads back via
``nexus.repos.read_dual``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog
from nexus.commands.index import _CatalogBackedRegistry
from nexus.repos import read_dual


@pytest.fixture
def cat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Catalog:
    cfg = tmp_path / "config"
    cat_dir = cfg / "catalog"
    cat_dir.mkdir(parents=True)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(cat_dir))
    Catalog.init(cat_dir)
    return Catalog(cat_dir, cat_dir / ".catalog.db")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "myrepo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    return r


class TestCorpusKnowledgeWriteThenRead:
    def test_corpus_knowledge_registers_knowledge_collection_in_catalog(
        self, cat: Catalog, repo: Path, tmp_path: Path,
    ) -> None:
        """The --corpus knowledge update path mints a knowledge__
        collection registered to the repo owner in the catalog."""
        adapter = _CatalogBackedRegistry(
            cat=cat, registry_path=tmp_path / "repos.json",
        )
        adapter.add(repo)  # ensures owner
        # Simulate the --corpus knowledge rewrite that commands/index.py
        # used to do via reg.update(path, docs_collection=...).
        adapter.update(
            repo,
            docs_collection="knowledge__myrepo-1-1__voyage-context-3__v1",
        )

        # Catalog now has the knowledge collection registered.
        rows = cat._db.execute(
            "SELECT name FROM collections "
            "WHERE name LIKE 'knowledge__%'"
        ).fetchall()
        assert any(
            r[0] == "knowledge__myrepo-1-1__voyage-context-3__v1"
            for r in rows
        ), [r[0] for r in rows]

    def test_subsequent_read_returns_knowledge_in_docs_slot_oq5(
        self, cat: Catalog, repo: Path, tmp_path: Path,
    ) -> None:
        """OQ-5 lock end-to-end: after the writer plants a knowledge__
        collection for the repo, the reader returns it as the
        canonical docs_collection."""
        adapter = _CatalogBackedRegistry(
            cat=cat, registry_path=tmp_path / "repos.json",
        )
        adapter.add(repo)
        adapter.update(
            repo,
            docs_collection="knowledge__myrepo-1-1__voyage-context-3__v1",
        )

        rec = read_dual(
            repo, cat=cat, registry_path=tmp_path / "repos.json",
        )
        assert rec is not None
        assert rec.docs_collection.startswith("knowledge__")

    def test_repos_json_not_created_by_corpus_knowledge_flow(
        self, cat: Catalog, repo: Path, tmp_path: Path,
    ) -> None:
        """Phase 4 success criterion: the install surface is clean
        post-cutover. No repos.json touched by the writer."""
        reg_path = tmp_path / "repos.json"
        assert not reg_path.exists()

        adapter = _CatalogBackedRegistry(cat=cat, registry_path=reg_path)
        adapter.add(repo)
        adapter.update(
            repo,
            docs_collection="knowledge__myrepo-1-1__voyage-context-3__v1",
        )
        adapter.update(repo, head_hash="abc123def456")  # no-op
        adapter.update(repo, status="ready")  # no-op

        # repos.json was never created.
        assert not reg_path.exists()
