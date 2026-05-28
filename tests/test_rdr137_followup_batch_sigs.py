# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-137 followup SIG-10, SIG-13, SIG-14, SIG-17 (epic nexus-43qgm).

Four small-scope SIGNIFICANT fixes bundled together.

SIG-10 (nexus-43qgm.10): _CatalogBackedRegistry.update echoes
"Routing prose to ..." even when register_collection raised and was
swallowed. Operator sees success while the catalog write didn't land.
``update`` returns bool; commands/index.py gates the echo on success.

SIG-13 (nexus-43qgm.13): commands/collection.py post-process repo
lookup fallback checks `collection` and `docs_collection` fields but
misses `code_collection`. `nx collection reindex code__...` on
pre-Phase-1.5a installs silently skips L1 refresh.

SIG-14 (nexus-43qgm.14): context.py synthesizes a non-existent rdr
collection name into the `allowed` set when the catalog has no
rdr__* registered. Latent collision risk; the synthesis achieves
nothing when no rdr topics exist for that collection.

SIG-17 (nexus-43qgm.17): tests/test_no_repo_registry_resurrection.py
_REPOS_JSON_PARSE_ALLOW includes upgrade.py — but upgrade.py routes
through nexus.repos._read_repos_json and does NOT directly parse.
Allowlist entry is superfluous + creates false safety for future
direct-parse additions.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import structlog
from structlog.testing import capture_logs

from nexus.catalog.catalog import Catalog
from nexus.commands.index import _CatalogBackedRegistry


@pytest.fixture(autouse=True)
def _enable_debug_logging():
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
    )
    yield
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
    )


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


class TestSig10UpdateReturnsSuccessFlag:
    def test_update_returns_true_on_success(
        self, cat: Catalog, repo: Path, tmp_path: Path,
    ) -> None:
        adapter = _CatalogBackedRegistry(
            cat=cat, registry_path=tmp_path / "repos.json",
        )
        adapter.add(repo)
        ok = adapter.update(
            repo,
            docs_collection="knowledge__myrepo-1-1__voyage-context-3__v1",
        )
        assert ok is True

    def test_update_returns_false_on_register_failure(
        self, cat: Catalog, repo: Path, tmp_path: Path,
    ) -> None:
        """When cat.register_collection raises (e.g. duplicate-name
        with different metadata), update returns False so the caller
        can avoid the misleading 'Routing prose to ...' echo."""
        adapter = _CatalogBackedRegistry(
            cat=cat, registry_path=tmp_path / "repos.json",
        )
        adapter.add(repo)
        with patch.object(
            cat, "register_collection",
            side_effect=RuntimeError("simulated catalog write failure"),
        ):
            ok = adapter.update(
                repo,
                docs_collection="knowledge__myrepo-1-1__voyage-context-3__v1",
            )
        assert ok is False


class TestSig13CollectionFallbackMatchesCodeCollection:
    def test_fallback_match_includes_code_collection(self) -> None:
        """When a user runs `nx collection reindex code__...` on a
        pre-Phase-1.5a install (collections.owner_id empty in catalog,
        catalog path returns repo_path=None), the legacy registry-walk
        fallback must check code_collection in addition to
        collection/docs_collection. Pre-fix the check missed
        code__* names entirely and silently skipped L1 refresh."""
        # Source-only assertion (the function is buried inside a
        # closure tied to click context; the simplest reliable check
        # is to grep the source for the fixed condition).
        src_path = (
            Path(__file__).resolve().parent.parent
            / "src" / "nexus" / "commands" / "collection.py"
        )
        text = src_path.read_text()
        # Pre-fix code did NOT have this match; post-fix MUST have it.
        assert "info.get(\"code_collection\") == name" in text, (
            "SIG-13: commands/collection.py fallback must check "
            "code_collection field; reindex on code__ collections "
            "would silently skip L1 refresh."
        )


class TestSig14ContextDoesNotSynthesizeMissingRdr:
    def test_no_rdr_in_catalog_omits_rdr_from_allowed(
        self, cat: Catalog, repo: Path, tmp_path: Path,
    ) -> None:
        """When the catalog has the owner + code/docs collections but
        no rdr__*, _repo_collections should NOT synthesize a
        rdr__owner__voyage-context-3__v1 name and add it to the
        allowed set. Pre-fix the synthesis achieved nothing when no
        rdr topics exist + carried latent collision risk."""
        owner = cat.ensure_owner_for_repo(repo)
        owner_id = str(owner).replace(".", "-")
        cat.register_collection(
            "code__myrepo-1-1__voyage-code-3__v1",
            content_type="code", owner_id=owner_id,
            embedding_model="voyage-code-3", model_version="v1",
        )
        cat.register_collection(
            "docs__myrepo-1-1__voyage-context-3__v1",
            content_type="docs", owner_id=owner_id,
            embedding_model="voyage-context-3", model_version="v1",
        )

        from nexus.context import _repo_collections
        colls = _repo_collections(repo)
        assert colls is not None
        # No synthesized rdr__ name when the catalog has none registered.
        rdr_synthesized = [c for c in colls if c.startswith("rdr__")]
        assert not rdr_synthesized, (
            f"context.py synthesized a non-existent rdr collection "
            f"into the allowed set: {rdr_synthesized}"
        )


class TestSig17LintGuardAllowlistDoesNotIncludeUpgrade:
    def test_upgrade_py_not_in_repos_json_parse_allow(self) -> None:
        """SIG-17: upgrade.py routes through nexus.repos._read_repos_json
        and does NOT directly parse repos.json. Including it in the
        lint guard's allowlist creates false safety (future dev adds
        a direct json.loads and assumes pre-authorized)."""
        from tests.test_no_repo_registry_resurrection import (
            _REPOS_JSON_PARSE_ALLOW, SRC,
        )
        # upgrade.py MUST NOT appear in the allowlist.
        assert (SRC / "commands" / "upgrade.py") not in _REPOS_JSON_PARSE_ALLOW
        # repos.py SHOULD remain (it owns _read_repos_json).
        assert (SRC / "repos.py") in _REPOS_JSON_PARSE_ALLOW
