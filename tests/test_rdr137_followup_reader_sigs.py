# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-137 followup SIG-6, SIG-8, SIG-11 (epic nexus-43qgm).

Three reader/shim observability fixes:

SIG-6 (nexus-43qgm.6): from_catalog SELECT must use ORDER BY so the
OQ-5 lock (knowledge wins over docs) is deterministic across multi-
collection owners (e.g. post embedding-model upgrade). Narrowed by
nexus-l52ms (2026-09-23): a knowledge__* collection is a docs-slot
candidate at all only when marked with
nexus.corpus.KNOWLEDGE_CORPUS_OPT_IN_MARKER; the ordering determinism
this section proves now applies among marked candidates (see
TestSig6FromCatalogDeterministicOrdering's two tests: marked
collections resolve deterministically to the lex-latest one, unmarked
collections resolve deterministically to "" every time).

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

from tests._catalog_fixture_ops import ActiveCatalog
import structlog
from structlog.testing import capture_logs

from nexus.corpus import _write_intent_embedding_model
from nexus.registry import RepoRegistry
from nexus.repos import _shim_log_level, from_catalog, read_dual

# RDR-204 Phase 1 follow-up (nexus-f5wwx): the engine's ``/collections/upsert``
# handler unconditionally seeds the tenant's embedding_profile for the
# request's content_type from the ENGINE's own configured embedder (bead
# nexus-ft04v.6/.8) on the FIRST touch of that content_type — a hardcoded
# "voyage-context-3" literal here 422s against the box's real (bge) profile.
#
# RDR-204 Phase 3 item 3 (nexus-ft04v.26): _write_intent_embedding_model, not
# effective_embedding_model_for_writes -- see test_catalog_backfill_collections.py's
# identical comment for why (the latter now makes a real network call, unsafe
# at module collection time).
_KNOWLEDGE_MODEL = _write_intent_embedding_model("knowledge")


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
def cat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ActiveCatalog:
    cfg = tmp_path / "config"
    cat_dir = cfg / "catalog"
    cat_dir.mkdir(parents=True)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(cat_dir))
    # nexus-aqbrk: return the ACTIVE catalog. The code under test resolves
    # through the factories, so a local-only handle left the service
    # catalog empty. (The local Catalog.init that used to run here died
    # with the local catalog in the terminal nexus-i711w deletion.)
    return ActiveCatalog()


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
        practice but spec-undefined; flips after VACUUM).

        nexus-l52ms (2026-09-23): this test predates the durable
        opt-in marker requirement -- from_catalog no longer admits ANY
        knowledge__* collection to the docs slot unless its
        display_name carries KNOWLEDGE_CORPUS_OPT_IN_MARKER (the
        l52ms ship-blocker fixup; see repos.py's from_catalog
        docstring and test_repos_reader.py's
        test_coincidental_untagged_knowledge_collection_still_never_wins).
        That is an intentional behaviour change, not a regression: an
        unmarked knowledge collection sharing the repo's owner id by
        coincidence must never win, regardless of how many exist or
        how they sort. Both collections here now carry the marker --
        the scenario this test actually exists for (two GENERATIONS
        of the SAME opted-in knowledge corpus after an embedding-model
        upgrade), which is the only case where "which one wins" is a
        real question at all. The ORDER BY name DESC determinism
        SIG-6 was written to prove is still live among marked
        candidates."""
        owner = cat.ensure_owner_for_repo(repo)
        owner_id = str(owner).replace(".", "-")

        # Register two knowledge collections for the same owner, BOTH
        # carrying the opt-in marker — simulates post embedding-model-
        # upgrade state for a repo that deliberately opted into
        # --corpus knowledge.
        for name in (
            f"knowledge__myrepo-1-1__{_KNOWLEDGE_MODEL}__v1",
            f"knowledge__myrepo-1-1__{_KNOWLEDGE_MODEL}__v2",
        ):
            cat.register_collection(
                name, content_type="knowledge", owner_id=owner_id,
                embedding_model=_KNOWLEDGE_MODEL, model_version="v1",
                display_name="nx-corpus-knowledge-opt-in",
            )

        winners = {from_catalog(repo, cat=cat).docs_collection for _ in range(20)}
        assert len(winners) == 1, (
            f"Non-deterministic OQ-5 selection across multiple knowledge "
            f"collections; saw: {winners}"
        )
        # ORDER BY name DESC: v2 wins over v1 (lex-latest model version).
        assert "v2" in next(iter(winners))

    def test_two_unmarked_knowledge_collections_neither_wins(
        self, cat: Catalog, repo: Path,
    ) -> None:
        """Companion to the above: the SAME two-collection, post-
        upgrade shape, but with NEITHER collection carrying the opt-in
        marker (the l52ms coincidental-owner-id incident, just with
        two candidates instead of one). Selection is still
        deterministic -- it deterministically returns empty, every
        time, not a coin-flip between the two unmarked rows."""
        owner = cat.ensure_owner_for_repo(repo)
        owner_id = str(owner).replace(".", "-")

        for name in (
            f"knowledge__myrepo-1-1__{_KNOWLEDGE_MODEL}__v1",
            f"knowledge__myrepo-1-1__{_KNOWLEDGE_MODEL}__v2",
        ):
            cat.register_collection(
                name, content_type="knowledge", owner_id=owner_id,
                embedding_model=_KNOWLEDGE_MODEL, model_version="v1",
            )

        winners = {from_catalog(repo, cat=cat).docs_collection for _ in range(20)}
        assert winners == {""}, (
            f"an unmarked knowledge collection must never win the docs slot, "
            f"whatever its name or how many exist; saw: {winners}"
        )


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
