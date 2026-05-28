# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-137 followup SIG-6, SIG-8, SIG-11 (epic nexus-43qgm).

Three reader/shim observability fixes:

SIG-6 (nexus-43qgm.6): from_catalog SELECT must use ORDER BY so the
OQ-5 lock (knowledge wins over docs) is deterministic across multi-
collection owners (e.g. post embedding-model upgrade).

SIG-8 (nexus-43qgm.8): _diff_fields suppresses catalog-empty /
registry-has-value (the more dangerous Phase 3 cutover state) along
with the intended partial-record case. Emit a separate
``repos_read_dual_catalog_missing`` event so cutover-progress is
observable.

SIG-11 (nexus-43qgm.11): NEXUS_REPOS_SHIM_WARN env-var only accepts
lowercase ``1``/``true``/``yes`` — rejects ``True``, ``YES``,
``True`` (str(True)), ``on``. Operator setting ``=True`` or ``=YES``
sees no graduation.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest
import structlog
from structlog.testing import capture_logs

from nexus.catalog.catalog import Catalog
from nexus.registry import RepoRegistry
from nexus.repos import _shim_log_level, from_catalog, read_dual


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


class TestSig6FromCatalogDeterministicOrdering:
    def test_two_knowledge_collections_stable_winner(
        self, cat: Catalog, repo: Path,
    ) -> None:
        """Owner with two knowledge__* collections returns the SAME
        docs_collection across 20 calls. Pre-fix no ORDER BY meant
        SQLite chose non-deterministically (insertion order in
        practice but spec-undefined; flips after VACUUM)."""
        owner = cat.ensure_owner_for_repo(repo)
        owner_id = str(owner).replace(".", "-")

        # Register two knowledge collections for the same owner —
        # simulates post embedding-model-upgrade state.
        for name in (
            "knowledge__myrepo-1-1__voyage-context-3__v1",
            "knowledge__myrepo-1-1__voyage-context-3__v2",
        ):
            cat.register_collection(
                name, content_type="knowledge", owner_id=owner_id,
                embedding_model="voyage-context-3", model_version="v1",
            )

        winners = {from_catalog(repo, cat=cat).docs_collection for _ in range(20)}
        assert len(winners) == 1, (
            f"Non-deterministic OQ-5 selection across multiple knowledge "
            f"collections; saw: {winners}"
        )
        # ORDER BY name DESC: v2 wins over v1 (lex-latest model version).
        assert "v2" in next(iter(winners))


class TestSig8DiffFieldsCatalogMissingEvent:
    def test_catalog_empty_field_with_registry_value_emits_event(
        self, cat: Catalog, repo: Path, tmp_path: Path,
    ) -> None:
        """When the catalog has the owner registered but a field is
        empty (e.g. code_collection not yet registered) AND the
        registry has a value for that field, read_dual must emit a
        separate observability event so cutover-progress is visible."""
        # Catalog has the owner but NO collections registered.
        cat.ensure_owner_for_repo(repo)

        # Registry has the legacy info populated.
        reg_path = tmp_path / "repos.json"
        reg = RepoRegistry(reg_path)
        reg.add(repo)
        reg.update(
            repo,
            code_collection="code__myrepo-LEGACY",
            docs_collection="docs__myrepo-LEGACY",
        )

        with capture_logs() as cap:
            rec = read_dual(repo, cat=cat, registry_path=reg_path)

        # Catalog wins (returns the catalog record with empty fields).
        assert rec is not None
        assert rec.code_collection == ""

        # An event must surface that catalog has missing fields the
        # registry could populate. Pre-fix _diff_fields swallowed this
        # silently along with the legitimate partial-record case.
        missing_events = [
            e for e in cap
            if e.get("event") == "repos_read_dual_catalog_missing"
        ]
        assert len(missing_events) == 1
        # The event names the field(s).
        assert "code_collection" in str(missing_events[0])


class TestSig11ShimWarnCaseInsensitive:
    @pytest.mark.parametrize("value", [
        "1", "true", "yes", "on",
        "True", "TRUE", "Yes", "YES", "On", "ON",
    ])
    def test_truthy_values_promote_to_warning(
        self, value: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NEXUS_REPOS_SHIM_WARN", value)
        assert _shim_log_level() == "warning"

    @pytest.mark.parametrize("value", [
        "", "0", "false", "no", "off", "n", "f", "False", "NO", "OFF",
    ])
    def test_falsy_values_stay_at_debug(
        self, value: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NEXUS_REPOS_SHIM_WARN", value)
        assert _shim_log_level() == "debug"
