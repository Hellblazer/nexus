# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-137 Phase 2a: catalog-backed reader with DEBUG shim (nexus-tts0d.4).

Single ``RepoRegistry``-shaped read API backed by the catalog so
consumers can swap in one import-change PR each (Phase 3).

Test coverage shape:
- Pure unit: ``from_catalog`` returns the right fields without
  touching ``repos.json`` at all.
- ``--corpus knowledge`` prefix inference (OQ-5): when the catalog
  has a ``knowledge__*`` collection registered to the owner, the
  reader returns that as the canonical docs slot.
- Empty-catalog fallback to ``RepoRegistry`` (the dual-read shim).
- Shim disagreement logger fires at DEBUG when the two sources
  diverge.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import structlog
from structlog.testing import capture_logs

from nexus.catalog.catalog import Catalog
from nexus.registry import RepoRegistry
from nexus.repos import RepoRecord, from_catalog, from_registry, read_dual


@pytest.fixture(autouse=True)
def _enable_debug_logging():
    """Temporarily allow DEBUG-level structlog events so capture_logs() sees them."""
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    yield
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))


@pytest.fixture
def cat(tmp_path: Path) -> Catalog:
    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir()
    Catalog.init(cat_dir)
    return Catalog(cat_dir, cat_dir / ".catalog.db")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Create a git repo so ``_repo_identity_with_main`` resolves cleanly."""
    import subprocess
    r = tmp_path / "myrepo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    return r


def _seed_owner_with_collections(
    cat: Catalog,
    repo: Path,
    *,
    code_coll: str | None = None,
    docs_coll: str | None = None,
) -> str:
    """Register an owner + the collections it owns. Returns tumbler prefix.

    register_collection is invoked with owner_id so the auto-migration
    in CatalogStore.__init__ (Phase 1.5a from nexus-tts0d.1) is not
    relied on for backfill mid-test.
    """
    owner = cat.ensure_owner_for_repo(repo)
    owner_id = str(owner).replace(".", "-")
    if code_coll:
        cat.register_collection(
            code_coll,
            content_type="code",
            owner_id=owner_id,
            embedding_model="voyage-code-3",
            model_version="1",
        )
    if docs_coll:
        # Infer content_type from prefix for the test seed.
        ct = docs_coll.split("__", 1)[0]
        cat.register_collection(
            docs_coll,
            content_type=ct,
            owner_id=owner_id,
            embedding_model="voyage-context-3",
            model_version="1",
        )
    return str(owner)


class TestFromCatalog:
    def test_returns_correct_fields_for_registered_repo(
        self, cat: Catalog, repo: Path,
    ) -> None:
        owner = _seed_owner_with_collections(
            cat, repo,
            code_coll="code__myrepo-1-2__voyage-code-3__v1",
            docs_coll="docs__myrepo-1-2__voyage-context-3__v1",
        )
        rec = from_catalog(repo, cat=cat)
        assert isinstance(rec, RepoRecord)
        assert rec.name == "myrepo"
        assert rec.code_collection == "code__myrepo-1-2__voyage-code-3__v1"
        assert rec.docs_collection == "docs__myrepo-1-2__voyage-context-3__v1"
        # Back-compat alias.
        assert rec.collection == rec.code_collection

    def test_returns_none_when_repo_not_in_catalog(
        self, cat: Catalog, repo: Path,
    ) -> None:
        assert from_catalog(repo, cat=cat) is None

    def test_corpus_knowledge_prefix_inference_oq5(
        self, cat: Catalog, repo: Path,
    ) -> None:
        """OQ-5 lock: when a repo's docs slot was indexed via
        ``--corpus knowledge``, the catalog has a ``knowledge__*``
        collection for the owner; the reader returns that as the
        canonical docs_collection without any registry lookup.
        """
        _seed_owner_with_collections(
            cat, repo,
            code_coll="code__myrepo-1-2__voyage-code-3__v1",
            docs_coll="knowledge__myrepo-1-2__voyage-context-3__v1",
        )
        rec = from_catalog(repo, cat=cat)
        assert rec is not None
        assert rec.docs_collection.startswith("knowledge__")

    def test_prefers_knowledge_over_docs_when_both_exist(
        self, cat: Catalog, repo: Path,
    ) -> None:
        """Mixed history: the user once indexed without --corpus, then
        re-indexed with --corpus knowledge. Both collections exist for
        the owner. The reader returns knowledge as the canonical docs
        slot (the user's most recent intent)."""
        owner_str = _seed_owner_with_collections(
            cat, repo,
            code_coll="code__myrepo-1-2__voyage-code-3__v1",
        )
        owner_id = owner_str.replace(".", "-")
        cat.register_collection(
            "docs__myrepo-1-2__voyage-context-3__v1",
            content_type="docs", owner_id=owner_id,
            embedding_model="voyage-context-3", model_version="1",
        )
        cat.register_collection(
            "knowledge__myrepo-1-2__voyage-context-3__v1",
            content_type="knowledge", owner_id=owner_id,
            embedding_model="voyage-context-3", model_version="1",
        )
        rec = from_catalog(repo, cat=cat)
        assert rec is not None
        assert rec.docs_collection.startswith("knowledge__")


class TestFromRegistry:
    def test_round_trips_the_legacy_shape(
        self, tmp_path: Path, repo: Path,
    ) -> None:
        reg = RepoRegistry(tmp_path / "repos.json")
        reg.add(repo)
        rec = from_registry(repo, registry_path=tmp_path / "repos.json")
        assert rec is not None
        assert rec.name == "myrepo"
        # Legacy registry fields populated.
        assert rec.collection
        assert rec.code_collection == rec.collection
        assert rec.docs_collection
        assert rec.status == "registered"


class TestReadDualShim:
    def test_catalog_wins_when_present(
        self, cat: Catalog, repo: Path, tmp_path: Path,
    ) -> None:
        """Catalog has the repo; registry is empty → catalog answer
        returned, no fallback fire."""
        _seed_owner_with_collections(
            cat, repo,
            code_coll="code__myrepo-1-2__voyage-code-3__v1",
            docs_coll="docs__myrepo-1-2__voyage-context-3__v1",
        )
        rec = read_dual(
            repo, cat=cat, registry_path=tmp_path / "missing.json",
        )
        assert rec is not None
        assert rec.code_collection.startswith("code__myrepo-1-2__")

    def test_registry_fallback_fires_when_catalog_empty(
        self, cat: Catalog, repo: Path, tmp_path: Path,
    ) -> None:
        """Catalog has no owner for the repo → falls back to registry
        and emits a DEBUG event."""
        reg = RepoRegistry(tmp_path / "repos.json")
        reg.add(repo)
        with capture_logs() as cap:
            rec = read_dual(
                repo, cat=cat, registry_path=tmp_path / "repos.json",
            )
        assert rec is not None
        assert rec.name == "myrepo"
        assert any(
            entry.get("event") == "repos_read_dual_fallback"
            and entry.get("fallback_branch") == "registry"
            for entry in cap
        )

    def test_disagreement_logged_when_catalog_and_registry_differ(
        self, cat: Catalog, repo: Path, tmp_path: Path,
    ) -> None:
        """Catalog and registry both have the repo but disagree on the
        code_collection — emit the disagreement log line at DEBUG."""
        _seed_owner_with_collections(
            cat, repo,
            code_coll="code__myrepo-1-2__voyage-code-3__v1",
            docs_coll="docs__myrepo-1-2__voyage-context-3__v1",
        )
        # Registry says a DIFFERENT code_collection (legacy hash-based
        # name) — the kind of drift RDR-137 was filed to eliminate.
        reg_path = tmp_path / "repos.json"
        reg_path.write_text(json.dumps({
            "repos": {
                str(repo): {
                    "name": "myrepo",
                    "collection": "code__myrepo-FAKE",
                    "code_collection": "code__myrepo-FAKE",
                    "docs_collection": "docs__myrepo-FAKE",
                    "head_hash": "",
                    "status": "registered",
                },
            },
        }))
        with capture_logs() as cap:
            rec = read_dual(
                repo, cat=cat, registry_path=reg_path,
            )
        # Catalog wins.
        assert rec is not None
        assert rec.code_collection == "code__myrepo-1-2__voyage-code-3__v1"
        # Disagreement event fired and names the divergent fields.
        disagree_events = [
            e for e in cap
            if e.get("event") == "repos_read_dual_disagreement"
        ]
        assert len(disagree_events) == 1
        diffs = disagree_events[0]["disagreements"]
        assert "code_collection" in diffs
        assert diffs["code_collection"]["catalog"] == (
            "code__myrepo-1-2__voyage-code-3__v1"
        )
        assert diffs["code_collection"]["registry"] == "code__myrepo-FAKE"
