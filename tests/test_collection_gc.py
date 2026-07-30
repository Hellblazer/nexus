# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ks40: ``nx catalog collection-gc`` verb.

Sweeps zombie T3 collections (0 chunks, no catalog projection, no
``documents.physical_collection`` reference). Targets the junkyard
pattern flagged by ``nx catalog doctor --collections-drift``: empty
T3 collections that accumulate from interrupted indexes, deleted
worktrees, or any indexer path that pre-creates a collection name
via ``get_or_create_collection`` and then never writes a chunk.

Conservative: a non-empty unreferenced collection is preserved
(operator review required); a referenced collection is preserved;
``taxonomy__*`` bypass-schema collections are out of scope.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction
from click.testing import CliRunner

from tests._catalog_fixture_ops import ActiveCatalog
from nexus.cli import main
from nexus.db.t3 import T3Database
from tests.conftest import make_vector_test_client


class TestCollectionGCCli:
    """``nx catalog collection-gc`` over LOCAL, INJECTED artifacts.

    nexus-aqbrk: PINNED. Every test here patches ``nexus.db.make_t3`` to a
    local EphemeralClient T3 (it has to — the subject is zombie collections,
    which the test must be able to create) and seeds a real local Catalog.
    Under the engine substrate the catalog half diverged: the seed went to
    the local .catalog.db while the CLI's ``_get_catalog()`` read the SERVICE
    catalog, and the writes died on the frozen-migration-source invariant —
    ``sqlite3.OperationalError: attempt to write a readonly database``.

    Pinning the catalog makes both halves local again, matching the T3
    injection the test already required. Converting instead would leave a
    local T3 reconciled against a service catalog, which is neither the
    production shape nor a coherent test.

    NO SERVICE COVERAGE IS LOST, and that is checked rather than asserted:
    the verb's only substrate-touching operations are ``list_collections``
    and ``distinct_doc_collections``, BOTH parity-registered in
    tests/catalog/test_shape_parity_tripwire.py. Everything else in
    ``collection_gc_cmd`` is set arithmetic over collection names, which is
    substrate-independent by construction.
    """

    @pytest.fixture()
    def runner(self) -> CliRunner:
        return CliRunner()

    @pytest.fixture()
    def t3_db(self) -> T3Database:
        # Mirror test_chash_reconcile.py: clear collections on entry
        # because EphemeralClient instances share an in-memory backend
        # across the test session.
        client = make_vector_test_client()
        for c in list(client.list_collections()):
            name = c if isinstance(c, str) else c.name
            try:
                client.delete_collection(name)
            except Exception:
                pass
        return T3Database(
            _client=client,
            _ef_override=DefaultEmbeddingFunction(),
        )

    @pytest.fixture()
    def catalog_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> Catalog:
        from nexus.catalog.catalog import Catalog
        catalog_dir = tmp_path / "catalog"
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        Catalog.init(catalog_dir)
        # nexus-i711w C-store: route seeding at the ACTIVE catalog. Service mode
    # opens the local .catalog.db READ-ONLY (frozen migration source,
    # RDR-176 P1 Gap 2), so a direct Catalog handle dies on "attempt to
    # write a readonly database". The subject here is the verb, not the
    # substrate.
        return ActiveCatalog()

    def _seed(
        self,
        t3_db: T3Database,
        catalog: Catalog,
        monkeypatch: pytest.MonkeyPatch,
        *,
        zombie_collections: list[str],
        registered_collections: list[str],
        unreferenced_with_chunks: list[str],
    ) -> None:
        """Wire env so the CLI sees ``t3_db`` + the seeded catalog,
        then create the requested mixture of T3 collections.

        ``zombie_collections``: empty T3 collections, NOT in catalog,
        NOT in documents — should be deleted.

        ``registered_collections``: empty T3 collections that ARE in
        catalog projection — should be preserved.

        ``unreferenced_with_chunks``: T3 collections with at least one
        chunk, NOT in catalog — should be preserved (operator review).
        """
        for name in zombie_collections:
            t3_db._client.get_or_create_collection(name)
        for name in registered_collections:
            t3_db._client.get_or_create_collection(name)
            catalog.register_collection(name)
        for name in unreferenced_with_chunks:
            col = t3_db._client.get_or_create_collection(name)
            col.upsert(
                ids=["seed-1"], documents=["seed text"],
                metadatas=[{"title": "seed.md"}],
            )

        monkeypatch.setattr("nexus.db.make_t3", lambda: t3_db)

    def test_no_zombies_clean_summary(
        self, monkeypatch, runner, t3_db, catalog_env,
    ) -> None:
        self._seed(
            t3_db, catalog_env, monkeypatch,
            zombie_collections=[],
            registered_collections=["code__live"],
            unreferenced_with_chunks=[],
        )
        result = runner.invoke(main, ["catalog", "collection-gc"])
        assert result.exit_code == 0, result.output
        assert "0" in result.output
        assert "Nothing to gc" in result.output

    def test_dry_run_lists_zombies_without_deleting(
        self, monkeypatch, runner, t3_db, catalog_env,
    ) -> None:
        self._seed(
            t3_db, catalog_env, monkeypatch,
            zombie_collections=[
                "rdr__zombie-1__voyage-context-3__v1",
                "rdr__zombie-2__voyage-context-3__v1",
                "code__zombie-leak",
            ],
            registered_collections=["code__live"],
            unreferenced_with_chunks=[],
        )

        result = runner.invoke(main, ["catalog", "collection-gc"])

        assert result.exit_code == 0, result.output
        assert "zombie candidates): 3" in result.output
        assert "would delete" in result.output
        assert "rdr__zombie-1__voyage-context-3__v1" in result.output
        assert "rdr__zombie-2__voyage-context-3__v1" in result.output
        assert "code__zombie-leak" in result.output
        assert "Re-run with --apply" in result.output

        # T3 unchanged.
        names = {c["name"] for c in t3_db.list_collections()}
        assert {"rdr__zombie-1__voyage-context-3__v1",
                "rdr__zombie-2__voyage-context-3__v1",
                "code__zombie-leak",
                "code__live"} <= names

    def test_apply_deletes_only_zombies(
        self, monkeypatch, runner, t3_db, catalog_env,
    ) -> None:
        """``--apply`` deletes the zombies but preserves registered
        collections and unreferenced-but-non-empty collections.
        """
        self._seed(
            t3_db, catalog_env, monkeypatch,
            zombie_collections=[
                "rdr__zombie-a__voyage-context-3__v1",
                "docs__zombie-b",
            ],
            registered_collections=["code__live"],
            unreferenced_with_chunks=[
                "knowledge__operator-loaded",
            ],
        )

        result = runner.invoke(main, ["catalog", "collection-gc", "--apply"])

        assert result.exit_code == 0, result.output
        assert "deleted 2 zombie collection(s)" in result.output

        names = {c["name"] for c in t3_db.list_collections()}
        # Zombies gone.
        assert "rdr__zombie-a__voyage-context-3__v1" not in names
        assert "docs__zombie-b" not in names
        # Registered + non-empty preserved.
        assert "code__live" in names
        assert "knowledge__operator-loaded" in names

    def test_unreferenced_with_chunks_is_kept_for_review(
        self, monkeypatch, runner, t3_db, catalog_env,
    ) -> None:
        """A T3 collection NOT in catalog but with chunks may be
        operator-loaded data not yet registered. The verb must NOT
        delete it; it counts under "needs operator review".
        """
        self._seed(
            t3_db, catalog_env, monkeypatch,
            zombie_collections=[],
            registered_collections=[],
            unreferenced_with_chunks=["knowledge__operator-loaded"],
        )

        result = runner.invoke(main, ["catalog", "collection-gc", "--apply"])

        assert result.exit_code == 0, result.output
        assert "unreferenced but non-empty (kept" in result.output
        assert "1" in result.output
        # The non-empty unreferenced collection survives.
        names = {c["name"] for c in t3_db.list_collections()}
        assert "knowledge__operator-loaded" in names

    def test_documents_physical_collection_reference_protects_from_delete(
        self, monkeypatch, runner, t3_db, catalog_env,
    ) -> None:
        """A T3 collection that's referenced by ``documents.physical_collection``
        must NOT be deleted even when empty in T3 (the document row is
        the operator's intent — data may have been deleted out-of-band
        and is being restored).
        """
        # Seed an empty T3 collection AND a documents row referencing it.
        empty_referenced = "rdr__doc-referenced__voyage-context-3__v1"
        t3_db._client.get_or_create_collection(empty_referenced)
        # Register a document referencing the collection. Was a raw INSERT at a
        # pinned tumbler "to skip the register flow"; the assertion only needs
        # SOME document row pointing at empty_referenced, and register() is the
        # path that exists on both substrates (nexus-i711w C-store).
        owner = catalog_env.register_owner("gc-protect", "repo", repo_hash="gcp")
        catalog_env.register(
            owner, "protected", content_type="paper",
            file_path="protected.md", physical_collection=empty_referenced,
        )
        monkeypatch.setattr("nexus.db.make_t3", lambda: t3_db)

        result = runner.invoke(main, ["catalog", "collection-gc", "--apply"])
        assert result.exit_code == 0, result.output

        names = {c["name"] for c in t3_db.list_collections()}
        assert empty_referenced in names, (
            "documents.physical_collection reference must protect the "
            "T3 collection from gc-induced deletion"
        )

    def test_taxonomy_bypass_schema_excluded(
        self, monkeypatch, runner, t3_db, catalog_env,
    ) -> None:
        """``taxonomy__*`` collections use bypass-schema (vector-only,
        operator-managed) and must be excluded from gc consideration.
        """
        # Real chromadb would refuse to upsert into taxonomy__ without
        # special metadata; just create the empty collection.
        t3_db._client.get_or_create_collection(
            "taxonomy__centroids", metadata={"hnsw:space": "cosine"},
        )
        # Plus one zombie that SHOULD be swept.
        t3_db._client.get_or_create_collection("rdr__zombie")
        monkeypatch.setattr("nexus.db.make_t3", lambda: t3_db)

        result = runner.invoke(main, ["catalog", "collection-gc", "--apply"])

        assert result.exit_code == 0, result.output
        assert "deleted 1 zombie" in result.output
        names = {c["name"] for c in t3_db.list_collections()}
        assert "taxonomy__centroids" in names
        assert "rdr__zombie" not in names

    def test_catalog_sqlite_error_exits_clean_not_traceback(
        self, monkeypatch, runner, t3_db, catalog_env,
    ) -> None:
        """nexus-pz24 (RDR-108 Phase 4 review CR-M2): a SQLite error
        on the catalog query (locked DB, schema mismatch, FS issue)
        must surface a clean operator message, not a raw Python
        traceback. The T3 query path already had this guard;
        the catalog query path was the lone exception.
        """
        import sqlite3
        from unittest.mock import MagicMock

        # Stub _get_catalog to return a catalog whose distinct_doc_collections
        # raises on the documents.physical_collection query.
        # nexus-xnz0o: _db.execute is no longer called directly; the code now
        # uses distinct_doc_collections() which is the uniform API.
        fake_cat = MagicMock()
        fake_cat.list_collections.return_value = []
        fake_cat.distinct_doc_collections.side_effect = sqlite3.OperationalError(
            "database is locked"
        )

        monkeypatch.setattr(
            "nexus.commands.catalog._get_catalog", lambda: fake_cat,
        )
        monkeypatch.setattr("nexus.db.make_t3", lambda: t3_db)

        result = runner.invoke(main, ["catalog", "collection-gc"])

        assert result.exit_code != 0, (
            "should exit nonzero on catalog failure"
        )
        assert "Failed to query catalog" in result.output, (
            f"expected clean operator message, got: {result.output!r}"
        )
        assert "Traceback" not in result.output
