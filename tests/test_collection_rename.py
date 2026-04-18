# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-1ccq — `nx collection rename` + domain-store cascade coverage.

ChromaDB Cloud's ``collection.modify(name=...)`` is an O(1) metadata-only
rename. The CLI wraps it and cascades the new name through the three T2
surfaces that store a collection string:

  * ``chash_index.physical_collection``
  * ``topics.collection`` / ``topic_assignments.source_collection`` /
    ``taxonomy_meta.collection``
  * Catalog documents' ``physical_collection`` (JSONL + SQLite cache).

The cascade is fail-open after the T3 rename lands — T2/catalog errors
log but do not abort, mirroring the delete-cascade contract.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner


@pytest.fixture
def env_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHROMA_API_KEY", "test")
    monkeypatch.setenv("VOYAGE_API_KEY", "test")
    monkeypatch.setenv("CHROMA_TENANT", "test")
    monkeypatch.setenv("CHROMA_DATABASE", "test")


# ── ChashIndex.rename_collection ────────────────────────────────────────────


class TestChashIndexRename:
    def test_updates_matching_rows(self, tmp_path: Path) -> None:
        from nexus.db.t2.chash_index import ChashIndex

        idx = ChashIndex(tmp_path / "chash.db")
        idx.upsert(chash="aa", collection="code__old", doc_id="d1")
        idx.upsert(chash="bb", collection="code__old", doc_id="d2")
        idx.upsert(chash="cc", collection="code__stays", doc_id="d3")

        count = idx.rename_collection(old="code__old", new="code__new")
        assert count == 2

        old_rows = idx.conn.execute(
            "SELECT COUNT(*) FROM chash_index WHERE physical_collection = ?",
            ("code__old",),
        ).fetchone()[0]
        new_rows = idx.conn.execute(
            "SELECT COUNT(*) FROM chash_index WHERE physical_collection = ?",
            ("code__new",),
        ).fetchone()[0]
        stays_rows = idx.conn.execute(
            "SELECT COUNT(*) FROM chash_index WHERE physical_collection = ?",
            ("code__stays",),
        ).fetchone()[0]
        assert (old_rows, new_rows, stays_rows) == (0, 2, 1)

    def test_pk_collision_new_side_wins(self, tmp_path: Path) -> None:
        """When `(chash, new)` already exists, the rename's updated doc_id
        must win — pre-existing new-side row is cleared first."""
        from nexus.db.t2.chash_index import ChashIndex

        idx = ChashIndex(tmp_path / "chash.db")
        idx.upsert(chash="aa", collection="code__old", doc_id="from_old")
        idx.upsert(chash="aa", collection="code__new", doc_id="stale_new")

        count = idx.rename_collection(old="code__old", new="code__new")
        assert count == 1

        doc_id = idx.conn.execute(
            "SELECT doc_id FROM chash_index WHERE chash = ? AND physical_collection = ?",
            ("aa", "code__new"),
        ).fetchone()[0]
        assert doc_id == "from_old"

    def test_no_rows_returns_zero(self, tmp_path: Path) -> None:
        from nexus.db.t2.chash_index import ChashIndex

        idx = ChashIndex(tmp_path / "chash.db")
        assert idx.rename_collection(old="docs__ghost", new="docs__phantom") == 0


# ── CatalogTaxonomy.rename_collection ───────────────────────────────────────


class TestTaxonomyRename:
    def _seed(self, tmp_path: Path):
        from nexus.db.t2 import T2Database

        db = T2Database(tmp_path / "memory.db")
        tax = db.taxonomy
        t_old = tax.conn.execute(
            "INSERT INTO topics (label, collection, centroid_hash, doc_count, terms, created_at) "
            "VALUES (?, ?, ?, ?, ?, '2026-04-18T00:00:00Z')",
            ("T", "docs__old", "h1", 1, "[]"),
        ).lastrowid
        t_stays = tax.conn.execute(
            "INSERT INTO topics (label, collection, centroid_hash, doc_count, terms, created_at) "
            "VALUES (?, ?, ?, ?, ?, '2026-04-18T00:00:00Z')",
            ("K", "docs__stays", "h2", 1, "[]"),
        ).lastrowid
        tax.conn.execute(
            "INSERT INTO topic_assignments (doc_id, topic_id, assigned_by, source_collection) "
            "VALUES (?, ?, ?, ?)",
            ("d1", t_old, "hdbscan", "docs__old"),
        )
        tax.conn.execute(
            "INSERT INTO topic_assignments (doc_id, topic_id, assigned_by, source_collection) "
            "VALUES (?, ?, ?, ?)",
            ("d2", t_stays, "projection", "docs__old"),
        )
        tax.conn.execute(
            "INSERT INTO topic_links (from_topic_id, to_topic_id, link_count, link_types) "
            "VALUES (?, ?, ?, ?)",
            (t_old, t_stays, 1, "[]"),
        )
        tax.conn.execute(
            "INSERT INTO taxonomy_meta (collection, last_discover_at) VALUES (?, ?)",
            ("docs__old", "2026-04-18T00:00:00Z"),
        )
        tax.conn.commit()
        return db, tax, t_old

    def test_updates_topics_assignments_and_meta(self, tmp_path: Path) -> None:
        db, tax, _ = self._seed(tmp_path)
        try:
            counts = tax.rename_collection("docs__old", "docs__new")
            assert counts == {"topics": 1, "assignments": 2, "meta": 1}

            assert tax.conn.execute(
                "SELECT COUNT(*) FROM topics WHERE collection = ?",
                ("docs__new",),
            ).fetchone()[0] == 1
            assert tax.conn.execute(
                "SELECT COUNT(*) FROM topic_assignments WHERE source_collection = ?",
                ("docs__new",),
            ).fetchone()[0] == 2
            # Survivor untouched.
            assert tax.conn.execute(
                "SELECT COUNT(*) FROM topics WHERE collection = ?",
                ("docs__stays",),
            ).fetchone()[0] == 1
        finally:
            db.close()

    def test_topic_links_survive_rename(self, tmp_path: Path) -> None:
        """topic_links use topic_id FK, not collection name — rename is
        a no-op for links and must not drop or mutate them."""
        db, tax, _ = self._seed(tmp_path)
        try:
            before = tax.conn.execute("SELECT COUNT(*) FROM topic_links").fetchone()[0]
            tax.rename_collection("docs__old", "docs__new")
            after = tax.conn.execute("SELECT COUNT(*) FROM topic_links").fetchone()[0]
            assert before == after == 1
        finally:
            db.close()


# ── Catalog.rename_collection ───────────────────────────────────────────────


class TestCatalogRename:
    def _seed(self, tmp_path: Path):
        from nexus.catalog.catalog import Catalog

        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        cat = Catalog(cat_dir, cat_dir / ".catalog.db")
        owner = cat.register_owner("knowledge-corpus", "corpus")
        tumbler_a = cat.register(
            owner, title="doc-a", content_type="paper", file_path="a.pdf",
            physical_collection="knowledge__old", chunk_count=3,
        )
        tumbler_b = cat.register(
            owner, title="doc-b", content_type="paper", file_path="b.pdf",
            physical_collection="knowledge__stays", chunk_count=2,
        )
        return cat, cat_dir, tumbler_a, tumbler_b

    def test_updates_matching_docs(self, tmp_path: Path) -> None:
        cat, cat_dir, tumbler_a, tumbler_b = self._seed(tmp_path)
        count = cat.rename_collection("knowledge__old", "knowledge__new")
        assert count == 1

        # SQLite cache reflects the rename.
        rows = cat._db.execute(
            "SELECT physical_collection FROM documents ORDER BY tumbler",
        ).fetchall()
        assert [r[0] for r in rows] == ["knowledge__new", "knowledge__stays"]

    def test_jsonl_appended_so_rebuild_preserves_rename(self, tmp_path: Path) -> None:
        cat, cat_dir, tumbler_a, tumbler_b = self._seed(tmp_path)
        cat.rename_collection("knowledge__old", "knowledge__new")

        # Last record for tumbler 1.1 in JSONL must have the new collection.
        records = [
            json.loads(line)
            for line in (cat_dir / "documents.jsonl").read_text().splitlines()
            if line.strip()
        ]
        by_tumbler: dict[str, dict] = {}
        for r in records:
            by_tumbler[r["tumbler"]] = r
        assert by_tumbler[str(tumbler_a)]["physical_collection"] == "knowledge__new"
        assert by_tumbler[str(tumbler_b)]["physical_collection"] == "knowledge__stays"

    def test_no_matches_returns_zero(self, tmp_path: Path) -> None:
        cat, *_ = self._seed(tmp_path)
        assert cat.rename_collection("knowledge__ghost", "knowledge__phantom") == 0


# ── CLI `nx collection rename` ──────────────────────────────────────────────


class TestRenameCLI:
    def _fake_t3(self, *, old_exists: bool = True, new_exists: bool = False) -> MagicMock:
        fake = MagicMock()
        fake.collection_exists = MagicMock(
            side_effect=lambda name: (
                old_exists if name == "code__old" else
                new_exists if name == "code__new" else
                False
            ),
        )
        fake.rename_collection = MagicMock()
        return fake

    def test_rename_happy_path(self, tmp_path: Path, env_creds) -> None:
        from nexus.commands.collection import rename_cmd

        db_path = tmp_path / "memory.db"
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        from nexus.db.t2 import T2Database
        with T2Database(db_path) as db:
            db.chash_index.upsert(
                chash="aa", collection="code__old", doc_id="d1",
            )

        fake = self._fake_t3(old_exists=True, new_exists=False)
        runner = CliRunner()
        with patch("nexus.commands.collection._t3", return_value=fake), \
             patch("nexus.mcp_infra.default_db_path", return_value=db_path), \
             patch("nexus.commands._helpers.default_db_path", return_value=db_path), \
             patch("nexus.config.catalog_path", return_value=cat_dir):
            result = runner.invoke(rename_cmd, ["code__old", "code__new"])

        assert result.exit_code == 0, result.output
        fake.rename_collection.assert_called_once_with("code__old", "code__new")

        # Cascade actually happened.
        with T2Database(db_path) as verify_db:
            new_rows = verify_db.chash_index.conn.execute(
                "SELECT COUNT(*) FROM chash_index WHERE physical_collection = ?",
                ("code__new",),
            ).fetchone()[0]
            old_rows = verify_db.chash_index.conn.execute(
                "SELECT COUNT(*) FROM chash_index WHERE physical_collection = ?",
                ("code__old",),
            ).fetchone()[0]
        assert new_rows == 1 and old_rows == 0

    def test_rename_rejects_unknown_old(self, tmp_path: Path, env_creds) -> None:
        from nexus.commands.collection import rename_cmd

        fake = self._fake_t3(old_exists=False, new_exists=False)
        runner = CliRunner()
        with patch("nexus.commands.collection._t3", return_value=fake):
            result = runner.invoke(rename_cmd, ["code__old", "code__new"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()
        fake.rename_collection.assert_not_called()

    def test_rename_rejects_collision(self, tmp_path: Path, env_creds) -> None:
        from nexus.commands.collection import rename_cmd

        fake = self._fake_t3(old_exists=True, new_exists=True)
        runner = CliRunner()
        with patch("nexus.commands.collection._t3", return_value=fake):
            result = runner.invoke(rename_cmd, ["code__old", "code__new"])
        assert result.exit_code != 0
        assert "already exists" in result.output.lower()
        fake.rename_collection.assert_not_called()

    def test_rename_rejects_prefix_mismatch(self, env_creds) -> None:
        from nexus.commands.collection import rename_cmd

        runner = CliRunner()
        # No _t3 patch — the prefix gate runs before we touch T3.
        result = runner.invoke(rename_cmd, ["code__foo", "docs__foo"])
        assert result.exit_code != 0
        assert "prefix mismatch" in result.output.lower()

    def test_force_prefix_change_bypasses_gate(self, tmp_path: Path, env_creds) -> None:
        from nexus.commands.collection import rename_cmd

        db_path = tmp_path / "memory.db"
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        from nexus.db.t2 import T2Database
        with T2Database(db_path):
            pass

        fake = MagicMock()
        fake.collection_exists = MagicMock(
            side_effect=lambda n: n == "code__foo",
        )
        fake.rename_collection = MagicMock()

        runner = CliRunner()
        with patch("nexus.commands.collection._t3", return_value=fake), \
             patch("nexus.commands._helpers.default_db_path", return_value=db_path), \
             patch("nexus.config.catalog_path", return_value=cat_dir):
            result = runner.invoke(
                rename_cmd,
                ["code__foo", "docs__foo", "--force-prefix-change"],
            )
        assert result.exit_code == 0, result.output
        fake.rename_collection.assert_called_once_with("code__foo", "docs__foo")
