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

import nexus.corpus as nexus_corpus
from tests._catalog_fixture_ops import ActiveCatalog

from nexus.commands.index import _CatalogBackedRegistry
from nexus.repos import read_dual

# RDR-204 Phase 1 follow-up (nexus-f5wwx, mirrored from test_repos_reader.py):
# the engine pins an install-scoped embedding profile per (tenant,
# content_type) on first write -- a hardcoded "voyage-context-3" literal
# here would conflict with local mode's real (bge) profile. Resolve the
# box's actual write-time model for "knowledge" instead.
_KNOWLEDGE_MODEL = nexus_corpus._write_intent_embedding_model("knowledge")


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
        # nexus-aqbrk: list_collections() is the public equivalent of the raw
        # "SELECT name FROM collections" and is parity-registered on both
        # substrates (tests/catalog/test_shape_parity_tripwire.py).
        names = [c["name"] for c in cat.list_collections()]
        knowledge = [n for n in names if n.startswith("knowledge__")]
        assert "knowledge__myrepo-1-1__voyage-context-3__v1" in knowledge, knowledge

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


def _repo_hash(repo: Path) -> str:
    from nexus.repo_identity import _repo_identity_with_main
    _name, repo_hash, _main = _repo_identity_with_main(repo)
    return repo_hash


class TestBackfillsPreFixOptIn:
    """nexus-l52ms ship-blocker fixup, round 2 backfill requirement: a repo
    that opted into ``--corpus knowledge`` BEFORE the durable-marker fix has
    a ``knowledge__*`` collection registered to its owner with NO
    ``display_name`` -- the old code never stamped one. After this fix,
    ``nexus.repos.from_catalog`` requires the marker to admit a knowledge
    collection to the docs slot, so such a repo's prose silently reverts to
    ``docs__`` (GH #451 again) UNTIL the marker lands.

    No existing signal can safely backfill this automatically: the only
    candidate ("any knowledge collection registered under this repo's
    owner_id") is exactly the coincidental-owner-id signal the l52ms
    incident showed is unsafe, and there is no separate index-run-history
    record of *why* a knowledge collection was created. The remedy is a
    one-time re-run of ``nx index repo --corpus knowledge``.

    This proves that remedy actually works: ``index_repo_cmd``'s
    ``--corpus knowledge`` branch does not special-case "already
    knowledge-typed" -- it always resynthesizes the candidate docs__ name
    for the repo's owner and rewrites it to knowledge__ (src/nexus/
    commands/index.py ~1099-1144), so it calls ``reg.update(docs_collection=
    ...)`` again even when the docs slot was already knowledge__, and that
    call unconditionally stamps the marker (the round-1 l52ms fix). One
    re-run is a real, mechanical backfill, not merely advice."""

    def test_reissuing_corpus_knowledge_backfills_the_marker(
        self, cat: "Catalog", repo: Path, tmp_path: Path,
    ) -> None:
        adapter = _CatalogBackedRegistry(cat=cat, registry_path=tmp_path / "repos.json")
        adapter.add(repo)
        owner = cat.owner_for_repo(_repo_hash(repo))
        owner_id = str(owner).replace(".", "-")
        name = f"knowledge__{owner_id}__{_KNOWLEDGE_MODEL}__v1"

        # Pre-fix opt-in: registered directly, with NO display_name --
        # mirrors what the CLI used to do before the l52ms round-1 fix.
        cat.register_collection(
            name, content_type="knowledge", owner_id=owner_id,
            embedding_model=_KNOWLEDGE_MODEL, model_version="v1",
        )

        rec_before = read_dual(repo, cat=cat, registry_path=tmp_path / "repos.json")
        assert rec_before is not None
        assert rec_before.docs_collection == "", (
            "a pre-fix opt-in with no marker must NOT win the docs slot -- "
            "this is the exact regression the backfill remedy exists for"
        )

        # The backfill remedy: re-run `nx index repo --corpus knowledge`.
        # index_repo_cmd's own re-synthesis (not reproduced here) always
        # arrives at this same call; this is that call.
        adapter.update(repo, docs_collection=name)

        rec_after = read_dual(repo, cat=cat, registry_path=tmp_path / "repos.json")
        assert rec_after is not None
        assert rec_after.docs_collection == name, (
            "one re-run of --corpus knowledge must backfill the durable "
            "marker and restore routing, with no other intervention"
        )


class TestMarkerSurvivesGenericWrites:
    """nexus-l52ms ship-blocker fixup, round 2 (2026-09-23 critique + code
    review): the durable opt-in marker (``display_name`` carrying
    ``KNOWLEDGE_CORPUS_OPT_IN_MARKER``) must survive an ORDINARY
    re-registration of the SAME collection through every GENERIC write path
    -- not just avoid being written blank at opt-in time. Two named sites:

    - A T3 chunk write's ``corpus.ensure_collection_registered(kwargs=None)``
      (the generic derivation branch every ``HttpVectorClient.put``/
      ``upsert_chunks`` call reaches) -- exercised here with the
      module-level registration CACHE explicitly cleared first, simulating
      a genuinely fresh process (a real fresh process has an empty cache by
      construction; clearing it here is what makes this test reach the
      engine call instead of short-circuiting on the in-process memo).
    - ``indexer.py``'s Phase-4 migration cascade, which calls
      ``writer.register_collection(name, content_type=..., owner_id=...,
      embedding_model=..., model_version=...)`` DIRECTLY -- no
      ``display_name``, and no cache to bypass in the first place since it
      never goes through ``ensure_collection_registered`` at all.

    Both used to silently overwrite ``display_name`` back to ``""`` via
    ``upsertCollection``'s unconditional ON CONFLICT SET (engine-side fix:
    a blank incoming value no longer clobbers an existing non-blank one).
    """

    _MODEL = _KNOWLEDGE_MODEL

    def _opt_in(self, cat: "Catalog", repo: Path, tmp_path: Path) -> str:
        """Opts *repo* in and returns the REAL collection name (owner_id
        segment resolved from the repo's actual catalog owner, not a
        hand-picked label) -- required because
        ``ensure_collection_registered(kwargs=None)``'s generic derivation
        path (test 1 below) PARSES owner_id back out of the name string
        (nexus-f5wwx: same "the name's own segments must agree with
        reality" trap as the embedding-model one), and owner_id is NOT
        pinned on an upsert conflict (only embedding_model/model_version/
        dimension/lifecycle_state are) -- a name whose embedded owner_id
        segment disagreed with the real one would silently REPOINT the row
        to a fake owner on the very first generic write, which is a
        distinct bug from the one this test suite is proving fixed."""
        adapter = _CatalogBackedRegistry(cat=cat, registry_path=tmp_path / "repos.json")
        adapter.add(repo)
        owner = cat.owner_for_repo(_repo_hash(repo))
        owner_id = str(owner).replace(".", "-")
        name = f"knowledge__{owner_id}__{self._MODEL}__v1"
        adapter.update(repo, docs_collection=name)
        return name

    def test_marker_survives_a_generic_t3_write_from_a_fresh_process(
        self, cat: "Catalog", repo: Path, tmp_path: Path,
    ) -> None:
        import nexus.corpus as corpus_mod

        name = self._opt_in(cat, repo, tmp_path)

        # Simulate a fresh process: no in-process registration memo at all,
        # so ensure_collection_registered cannot short-circuit and must
        # actually re-issue the register_collection call -- exactly what a
        # real fresh process's first T3 write for this collection does.
        corpus_mod._REGISTERED_COLLECTIONS.clear()
        corpus_mod.ensure_collection_registered(
            name, registrar=lambda: cat, kwargs=None,
        )

        coll = cat.get_collection(name)
        assert coll is not None
        assert coll.get("display_name") == corpus_mod.KNOWLEDGE_CORPUS_OPT_IN_MARKER, (
            "a generic T3-write re-registration (kwargs=None, no display_name) "
            "must not wipe the durable opt-in marker"
        )

        rec = read_dual(repo, cat=cat, registry_path=tmp_path / "repos.json")
        assert rec is not None
        assert rec.docs_collection == name, (
            "routing must still favor the tagged knowledge collection after "
            "the generic write"
        )

    def test_marker_survives_the_indexer_migration_cascades_direct_register_call(
        self, cat: "Catalog", repo: Path, tmp_path: Path,
    ) -> None:
        name = self._opt_in(cat, repo, tmp_path)

        existing = cat.get_collection(name)
        owner_id = existing["owner_id"]

        # Mirrors indexer.py's Phase-4 migration cascade EXACTLY: a direct
        # register_collection call with explicit content_type/owner_id/
        # embedding_model/model_version and NO display_name -- it never
        # goes through ensure_collection_registered at all, so there is no
        # cache to clear here; this call always re-hits the engine.
        cat.register_collection(
            name,
            content_type="knowledge",
            owner_id=owner_id,
            embedding_model=self._MODEL,
            model_version="v1",
        )

        coll = cat.get_collection(name)
        assert coll is not None
        assert coll.get("display_name") == "nx-corpus-knowledge-opt-in", (
            "the indexer migration cascade's direct register_collection call "
            "(no display_name) must not wipe the durable opt-in marker"
        )

        rec = read_dual(repo, cat=cat, registry_path=tmp_path / "repos.json")
        assert rec is not None
        assert rec.docs_collection == name

    def test_coincidental_untagged_knowledge_collection_still_never_wins(
        self, cat: "Catalog", repo: Path, tmp_path: Path,
    ) -> None:
        """The other direction, unchanged by this durability fix: a
        knowledge collection that merely shares the repo's owner id, with
        NO marker ever stamped on it, is still never a docs-slot candidate."""
        adapter = _CatalogBackedRegistry(cat=cat, registry_path=tmp_path / "repos.json")
        adapter.add(repo)
        owner = cat.owner_for_repo(_repo_hash(repo))
        owner_id = str(owner).replace(".", "-")
        name = f"knowledge__{owner_id}__{self._MODEL}__v1"
        cat.register_collection(
            name, content_type="knowledge", owner_id=owner_id,
            embedding_model=self._MODEL, model_version="1",
        )

        rec = read_dual(repo, cat=cat, registry_path=tmp_path / "repos.json")
        assert rec is not None
        assert rec.docs_collection == "", (
            "an untagged knowledge collection must never win the docs slot, "
            "marker durability fix or not"
        )
