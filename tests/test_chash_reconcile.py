# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-w9vq: ``nx catalog chash-reconcile`` verb.

The T2 ``chash_index`` is a routing table mapping ``chash`` to
``physical_collection``. When a collection is deleted from T3, the
chash_index rows for that collection are NOT cascaded today; they
remain as ghosts pointing at a non-existent collection. The Phase 5
verification probe (2026-05-10) found 1682 ghost rows (1824 distinct
collections in chash_index vs 153 in T3).

These tests pin the contract for ``ChashIndex.distinct_collections``
and the ``nx catalog chash-reconcile`` CLI verb.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.t2.chash_index import ChashIndex
from nexus.db.t3 import T3Database


# ── ChashIndex.distinct_collections ────────────────────────────────────────


class TestChashIndexDistinctCollections:
    def test_empty_returns_empty_set(self, tmp_path: Path) -> None:
        store = ChashIndex(tmp_path / "t2.db")
        try:
            assert store.distinct_collections() == set()
        finally:
            store.close()

    def test_returns_distinct_collection_names(self, tmp_path: Path) -> None:
        store = ChashIndex(tmp_path / "t2.db")
        try:
            store.upsert(chash="a" * 64, collection="code__one")
            store.upsert(chash="b" * 64, collection="code__one")
            store.upsert(chash="c" * 64, collection="code__two")
            store.upsert(chash="d" * 64, collection="docs__three")
            assert store.distinct_collections() == {
                "code__one", "code__two", "docs__three",
            }
        finally:
            store.close()


# ── nx catalog chash-reconcile CLI ─────────────────────────────────────────


class TestChashReconcileCLI:
    @pytest.fixture()
    def runner(self) -> CliRunner:
        return CliRunner()

    @pytest.fixture()
    def t3_db(self) -> T3Database:
        # ``chromadb.EphemeralClient`` instances share an in-memory
        # backend across the test session — collections leak between
        # tests when the same process opens multiple ephemerals. Tests
        # that assert on ``list_collections``-derived counts (e.g.
        # ``test_unindexed_t3_collections_reported_not_deleted``)
        # silently fail under shared state. Drop every existing
        # collection on fixture entry so each test sees a clean slate.
        client = chromadb.EphemeralClient()
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

    def _setup_env(
        self, tmp_path: Path, monkeypatch, t3_db: T3Database,
        *,
        live_collections: list[str],
        indexed_collections: list[tuple[str, list[str]]],
    ) -> Path:
        """Configure env so the CLI sees this T3 + a tmp t2 db.

        ``live_collections``: collection names to create in T3.
        ``indexed_collections``: per-collection (name, [chashes]) to seed
        in the chash_index. Pass ghost names that aren't in
        live_collections to simulate stale rows.
        """
        for name in live_collections:
            t3_db._client.get_or_create_collection(name)

        mem_db = tmp_path / "memory.db"
        idx = ChashIndex(mem_db)
        try:
            for name, chashes in indexed_collections:
                for c in chashes:
                    idx.upsert(chash=c, collection=name)
        finally:
            idx.close()

        import nexus.commands._helpers as h
        monkeypatch.setattr("nexus.config.default_db_path", lambda: mem_db)
        monkeypatch.setattr("nexus.mcp_infra.default_db_path", lambda: mem_db)
        monkeypatch.setattr("nexus.mcp_infra.get_t3", lambda: t3_db)
        return mem_db

    def test_no_ghosts_clean_summary(
        self, tmp_path: Path, monkeypatch, runner, t3_db,
    ) -> None:
        self._setup_env(
            tmp_path, monkeypatch, t3_db,
            live_collections=["code__live"],
            indexed_collections=[("code__live", ["a" * 64])],
        )
        result = runner.invoke(main, ["catalog", "chash-reconcile"])
        assert result.exit_code == 0, result.output
        assert "0 ghost" in result.output
        assert "Nothing to reconcile" in result.output

    def test_dry_run_default_reports_without_deleting(
        self, tmp_path: Path, monkeypatch, runner, t3_db,
    ) -> None:
        mem_db = self._setup_env(
            tmp_path, monkeypatch, t3_db,
            live_collections=["code__live"],
            indexed_collections=[
                ("code__live", ["a" * 64]),
                ("code__ghost1", ["b" * 64, "c" * 64]),  # 2 rows
                ("code__ghost2", ["d" * 64]),             # 1 row
            ],
        )

        result = runner.invoke(main, ["catalog", "chash-reconcile"])

        assert result.exit_code == 0, result.output
        assert "2 ghost" in result.output  # 2 ghost collections
        assert "3 row(s) total" in result.output
        assert "would delete" in result.output
        assert "Re-run with --apply" in result.output

        # Verify nothing was actually deleted.
        idx = ChashIndex(mem_db)
        try:
            assert idx.distinct_collections() == {
                "code__live", "code__ghost1", "code__ghost2",
            }
            assert idx.count_for_collection("code__ghost1") == 2
        finally:
            idx.close()

    def test_apply_deletes_ghost_rows(
        self, tmp_path: Path, monkeypatch, runner, t3_db,
    ) -> None:
        mem_db = self._setup_env(
            tmp_path, monkeypatch, t3_db,
            live_collections=["code__live"],
            indexed_collections=[
                ("code__live", ["a" * 64, "b" * 64]),
                ("code__ghost1", ["c" * 64, "d" * 64]),
                ("code__ghost2", ["e" * 64]),
            ],
        )

        result = runner.invoke(main, ["catalog", "chash-reconcile", "--apply"])

        assert result.exit_code == 0, result.output
        assert "deleted 3 row(s)" in result.output
        assert "2 ghost collection(s)" in result.output

        idx = ChashIndex(mem_db)
        try:
            # Live collection rows preserved; ghost rows gone.
            assert idx.distinct_collections() == {"code__live"}
            assert idx.count_for_collection("code__live") == 2
            assert idx.count_for_collection("code__ghost1") == 0
            assert idx.count_for_collection("code__ghost2") == 0
        finally:
            idx.close()

    def test_apply_idempotent_after_first_run(
        self, tmp_path: Path, monkeypatch, runner, t3_db,
    ) -> None:
        """A second --apply on a clean state finds 0 ghosts."""
        self._setup_env(
            tmp_path, monkeypatch, t3_db,
            live_collections=["code__live"],
            indexed_collections=[
                ("code__live", ["a" * 64]),
                ("code__ghost", ["b" * 64]),
            ],
        )

        runner.invoke(main, ["catalog", "chash-reconcile", "--apply"])
        result = runner.invoke(main, ["catalog", "chash-reconcile", "--apply"])

        assert result.exit_code == 0, result.output
        assert "0 ghost" in result.output

    def test_unindexed_t3_collections_reported_not_deleted(
        self, tmp_path: Path, monkeypatch, runner, t3_db,
    ) -> None:
        """T3 collections that have NO chash_index rows are reported
        as 'unindexed' but are NOT classified as ghosts (they're the
        opposite direction: live in T3, missing from index)."""
        self._setup_env(
            tmp_path, monkeypatch, t3_db,
            live_collections=[
                "code__live",
                "code__no_index1",
                "code__no_index2",
            ],
            indexed_collections=[("code__live", ["a" * 64])],
        )

        result = runner.invoke(main, ["catalog", "chash-reconcile"])

        assert result.exit_code == 0, result.output
        assert "0 ghost" in result.output
        assert "2 T3 collection(s)" in result.output  # unindexed count
        assert "no chash_index rows" in result.output

    def test_no_t2_db_exits_nonzero(
        self, tmp_path: Path, monkeypatch, runner,
    ) -> None:
        absent_db = tmp_path / "absent" / "memory.db"
        import nexus.commands._helpers as h
        monkeypatch.setattr("nexus.config.default_db_path", lambda: absent_db)
        monkeypatch.setattr("nexus.mcp_infra.default_db_path", lambda: absent_db)

        result = runner.invoke(main, ["catalog", "chash-reconcile"])
        assert result.exit_code != 0
        assert "No T2 db" in result.output

    def test_truncates_long_ghost_list(
        self, tmp_path: Path, monkeypatch, runner, t3_db,
    ) -> None:
        """When there are >20 ghost collections, the per-collection
        breakdown caps at 20 and reports the remainder count."""
        ghost_names = [f"code__ghost_{i:03d}" for i in range(25)]
        self._setup_env(
            tmp_path, monkeypatch, t3_db,
            live_collections=["code__live"],
            indexed_collections=[("code__live", ["a" * 64])] + [
                (n, [f"{i:064x}"]) for i, n in enumerate(ghost_names)
            ],
        )

        result = runner.invoke(main, ["catalog", "chash-reconcile"])

        assert result.exit_code == 0, result.output
        assert "25 ghost" in result.output
        assert "and 5 more ghost collection(s)" in result.output

    def test_handles_string_returning_list_collections_backend(
        self, tmp_path: Path, monkeypatch, runner,
    ) -> None:
        """nexus-l1yt (RDR-108 Phase 4 review CR-H1): chromadb's
        ``list_collections`` shape varies by backend version. Some
        return Collection objects (with ``.name``); others return
        bare strings. Every other call site in nexus uses
        ``isinstance(c, str)`` to defend; chash_reconcile_cmd was
        the lone exception and crashed with AttributeError on the
        string-returning shape. This regression test stubs the
        client to return strings and verifies the verb completes.
        """
        from unittest.mock import MagicMock

        # Stub a T3 whose underlying client returns BARE STRINGS
        # from list_collections (the shape the original code crashed
        # on).
        from nexus.db.t3 import T3Database
        stub_client = MagicMock()
        stub_client.list_collections.return_value = ["code__live"]
        # Stub get_collection to return something with a count() so
        # the rest of the verb can run end-to-end.
        stub_col = MagicMock()
        stub_col.count.return_value = 1
        stub_client.get_collection.return_value = stub_col
        t3 = T3Database(_client=stub_client)

        # Seed a chash_index so the verb has something to look at.
        mem_db = tmp_path / "memory.db"
        idx = ChashIndex(mem_db)
        try:
            idx.upsert(chash="a" * 64, collection="code__live")
        finally:
            idx.close()

        import nexus.commands._helpers as h
        monkeypatch.setattr("nexus.config.default_db_path", lambda: mem_db)
        monkeypatch.setattr("nexus.mcp_infra.default_db_path", lambda: mem_db)
        monkeypatch.setattr("nexus.mcp_infra.get_t3", lambda: t3)

        result = runner.invoke(main, ["catalog", "chash-reconcile"])

        assert result.exit_code == 0, (
            f"verb crashed on string-shape list_collections: {result.output!r}"
        )
        # The verb's normal output should appear (no exception).
        assert "ghost" in result.output or "Nothing to reconcile" in result.output
