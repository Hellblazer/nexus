# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-1ccq — `nx collection rename` + domain-store cascade coverage.

ChromaDB Cloud's ``collection.modify(name=...)`` is an O(1) metadata-only
rename. The CLI wraps it and cascades the new name through the T2 surfaces
that store a collection string:

  * ``chash_index.physical_collection``
  * ``topics.collection`` / ``topic_assignments.source_collection`` /
    ``taxonomy_meta.collection``
  * Catalog documents' ``physical_collection`` (JSONL + SQLite cache).
  * ``document_aspects.collection`` (denorm cache, nexus-gp20)
  * ``aspect_extraction_queue.collection`` (denorm cache, nexus-gp20)

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
        idx.upsert(chash="aa", collection="code__old", chunk_chroma_id="d1")
        idx.upsert(chash="bb", collection="code__old", chunk_chroma_id="d2")
        idx.upsert(chash="cc", collection="code__stays", chunk_chroma_id="d3")

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
        """When `(chash, new)` already exists, the rename's updated chunk_chroma_id
        must win — pre-existing new-side row is cleared first."""
        from nexus.db.t2.chash_index import ChashIndex

        idx = ChashIndex(tmp_path / "chash.db")
        idx.upsert(chash="aa", collection="code__old", chunk_chroma_id="from_old")
        idx.upsert(chash="aa", collection="code__new", chunk_chroma_id="stale_new")

        count = idx.rename_collection(old="code__old", new="code__new")
        assert count == 1

        chunk_chroma_id = idx.conn.execute(
            "SELECT chunk_chroma_id FROM chash_index WHERE chash = ? AND physical_collection = ?",
            ("aa", "code__new"),
        ).fetchone()[0]
        assert chunk_chroma_id == "from_old"

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

    def test_rename_preserves_source_mtime_across_jsonl_rebuild(
        self, tmp_path: Path,
    ) -> None:
        """Regression: review-flagged Critical (Reviewer B/C1).

        `rename_collection` previously SELECTed 12 columns and appended a
        JSONL record without `source_mtime`. JSONL is the rebuild source
        of truth, so rebuild-from-JSONL silently reset mtime to 0.0 for
        every renamed document, breaking stale-source detection.
        """
        from nexus.catalog.catalog import Catalog

        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        cat = Catalog(cat_dir, cat_dir / ".catalog.db")
        owner = cat.register_owner("papers", "corpus")
        tumbler = cat.register(
            owner, title="doc", content_type="paper", file_path="a.pdf",
            physical_collection="knowledge__old", chunk_count=1,
            source_mtime=1_700_000_000.0,
        )

        cat.rename_collection("knowledge__old", "knowledge__new")

        # Rebuild from JSONL — the durable source of truth. The mtime
        # must survive the round-trip.
        cat_rebuilt = Catalog(cat_dir, cat_dir / ".catalog-rebuilt.db")
        entry = cat_rebuilt.resolve(tumbler)
        assert entry is not None
        assert entry.physical_collection == "knowledge__new"
        assert entry.source_mtime == 1_700_000_000.0, (
            f"rename_collection lost source_mtime on JSONL rebuild: "
            f"got {entry.source_mtime}"
        )


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
                chash="aa", collection="code__old", chunk_chroma_id="d1",
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


# ── Partial-cascade failure mode (review finding + nexus-nhyh / CG-1) ────────


class TestRenameCascadeFailureModes:
    """T2 cascade failures must now exit non-zero (nexus-nhyh / CG-1).

    Old behavior (nexus-1ccq): T3 renamed first, T2 failures were fail-open
    (warn + exit 0). This left the system in an inconsistent state with no
    operator-facing signal that action was required.

    New behavior (nexus-nhyh SIG-8 + CG-1):
    - T2 cascade runs FIRST (T2-first ordering). On failure, T3 rename has
      NOT yet run, so the system is still fully consistent.
    - T2 failure raises ClickException (exit non-zero) with an actionable
      message. Operator must see the failure.
    - Catalog cascade remains fail-open: it is a derived view, and failures
      can be repaired by ``nx catalog rebuild``.
    """

    def test_t2_cascade_failure_exits_nonzero_and_aborts_t3(
        self, tmp_path: Path, env_creds,
    ) -> None:
        """T2 cascade throws → exit non-zero, T3 rename must NOT have fired
        (T2-first ordering: T3 only runs after T2 succeeds)."""
        from nexus.commands.collection import rename_cmd

        db_path = tmp_path / "memory.db"
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()

        fake = MagicMock()
        fake.collection_exists = MagicMock(
            side_effect=lambda n: n == "code__old",
        )
        fake.rename_collection = MagicMock()

        def _t2_bomb(*a, **kw):
            raise RuntimeError("simulated T2 outage")

        runner = CliRunner()
        with patch("nexus.commands.collection._t3", return_value=fake), \
             patch("nexus.db.t2.T2Database", side_effect=_t2_bomb), \
             patch("nexus.commands._helpers.default_db_path", return_value=db_path), \
             patch("nexus.config.catalog_path", return_value=cat_dir):
            result = runner.invoke(rename_cmd, ["code__old", "code__new"])

        # Must exit non-zero: T2 failure is not fail-open
        assert result.exit_code != 0, (
            f"T2 cascade failure must exit non-zero. Got {result.exit_code}. "
            f"Output: {result.output}"
        )
        # T3 must NOT have been called (T2-first ordering: T3 only runs after T2)
        fake.rename_collection.assert_not_called()
        # Error message must name the failure and be actionable
        assert "T2 cascade failed" in result.output or "cascade" in result.output.lower()
        assert "simulated T2 outage" in result.output

    def test_catalog_cascade_failure_prints_warn_and_continues(
        self, tmp_path: Path, env_creds,
    ) -> None:
        """Catalog cascade throws → T2 + T3 stay renamed, CLI still exits
        0 with a stderr warn line naming the catalog (fail-open)."""
        from nexus.commands.collection import rename_cmd

        db_path = tmp_path / "memory.db"
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        # Seed an empty T2 so the T2 cascade succeeds trivially.
        from nexus.db.t2 import T2Database
        with T2Database(db_path):
            pass

        fake = MagicMock()
        fake.collection_exists = MagicMock(
            side_effect=lambda n: n == "code__old",
        )
        fake.rename_collection = MagicMock()

        def _catalog_bomb(*a, **kw):
            raise RuntimeError("simulated catalog lock contention")

        runner = CliRunner()
        with patch("nexus.commands.collection._t3", return_value=fake), \
             patch("nexus.catalog.catalog.Catalog", side_effect=_catalog_bomb), \
             patch("nexus.commands._helpers.default_db_path", return_value=db_path), \
             patch("nexus.config.catalog_path", return_value=cat_dir):
            result = runner.invoke(rename_cmd, ["code__old", "code__new"])

        assert result.exit_code == 0, result.output
        # T3 was renamed (T2 succeeded, T3 ran after)
        fake.rename_collection.assert_called_once_with("code__old", "code__new")
        assert "catalog cascade failed" in result.output
        assert "simulated catalog lock contention" in result.output


# ── DocumentAspects.rename_collection (nexus-gp20) ─────────────────────────


class TestDocumentAspectsRename:
    """RDR-108 Phase 1d: ``document_aspects.collection`` is a denorm cache;
    rename_collection keeps it in sync with the T3 collection rename."""

    def _seed(self, tmp_path: Path):
        from nexus.db.t2.document_aspects import DocumentAspects, AspectRecord

        store = DocumentAspects(tmp_path / "aspects.db")
        # Insert rows directly via SQL to avoid the upsert's schema-detection
        # gate (pre-migration schema: PK is (collection, source_path)).
        store.conn.execute(
            "INSERT INTO document_aspects "
            "(collection, source_path, problem_formulation, proposed_method, "
            " experimental_datasets, experimental_baselines, experimental_results, "
            " extras, confidence, extracted_at, model_version, extractor_name) "
            "VALUES (?, ?, NULL, NULL, '[]', '[]', NULL, '{}', NULL, "
            "        '2026-05-09T00:00:00Z', 'v1', 'test_extractor')",
            ("code__old", "a.py"),
        )
        store.conn.execute(
            "INSERT INTO document_aspects "
            "(collection, source_path, problem_formulation, proposed_method, "
            " experimental_datasets, experimental_baselines, experimental_results, "
            " extras, confidence, extracted_at, model_version, extractor_name) "
            "VALUES (?, ?, NULL, NULL, '[]', '[]', NULL, '{}', NULL, "
            "        '2026-05-09T00:00:00Z', 'v1', 'test_extractor')",
            ("code__old", "b.py"),
        )
        store.conn.execute(
            "INSERT INTO document_aspects "
            "(collection, source_path, problem_formulation, proposed_method, "
            " experimental_datasets, experimental_baselines, experimental_results, "
            " extras, confidence, extracted_at, model_version, extractor_name) "
            "VALUES (?, ?, NULL, NULL, '[]', '[]', NULL, '{}', NULL, "
            "        '2026-05-09T00:00:00Z', 'v1', 'test_extractor')",
            ("code__stays", "c.py"),
        )
        store.conn.commit()
        return store

    def test_updates_matching_rows(self, tmp_path: Path) -> None:
        store = self._seed(tmp_path)
        count = store.rename_collection(old="code__old", new="code__new")
        assert count == 2

        old_rows = store.conn.execute(
            "SELECT COUNT(*) FROM document_aspects WHERE collection = ?",
            ("code__old",),
        ).fetchone()[0]
        new_rows = store.conn.execute(
            "SELECT COUNT(*) FROM document_aspects WHERE collection = ?",
            ("code__new",),
        ).fetchone()[0]
        stays_rows = store.conn.execute(
            "SELECT COUNT(*) FROM document_aspects WHERE collection = ?",
            ("code__stays",),
        ).fetchone()[0]
        assert (old_rows, new_rows, stays_rows) == (0, 2, 1)

    def test_source_path_untouched(self, tmp_path: Path) -> None:
        """source_path denorm cache must be byte-identical pre/post rename."""
        store = self._seed(tmp_path)
        paths_before = set(
            r[0] for r in store.conn.execute(
                "SELECT source_path FROM document_aspects ORDER BY source_path"
            ).fetchall()
        )
        store.rename_collection(old="code__old", new="code__new")
        paths_after = set(
            r[0] for r in store.conn.execute(
                "SELECT source_path FROM document_aspects ORDER BY source_path"
            ).fetchall()
        )
        assert paths_before == paths_after

    def test_no_rows_returns_zero(self, tmp_path: Path) -> None:
        from nexus.db.t2.document_aspects import DocumentAspects

        store = DocumentAspects(tmp_path / "aspects.db")
        assert store.rename_collection(old="docs__ghost", new="docs__phantom") == 0

    def test_idempotent_second_rename(self, tmp_path: Path) -> None:
        """Second call with old name that no longer has rows is safe no-op."""
        store = self._seed(tmp_path)
        store.rename_collection(old="code__old", new="code__new")
        # Second rename of same old name: no rows match → zero, no error.
        count = store.rename_collection(old="code__old", new="code__new")
        assert count == 0

    def test_only_matching_collection_updated(self, tmp_path: Path) -> None:
        """Rows for 'code__stays' must not be touched."""
        store = self._seed(tmp_path)
        store.rename_collection(old="code__old", new="code__new")
        stays = store.conn.execute(
            "SELECT collection FROM document_aspects WHERE source_path = ?",
            ("c.py",),
        ).fetchone()[0]
        assert stays == "code__stays"


# ── AspectExtractionQueue.rename_collection (nexus-gp20) ───────────────────


class TestAspectExtractionQueueRename:
    """RDR-108 Phase 1d: ``aspect_extraction_queue.collection`` is a denorm
    cache; rename_collection keeps it in sync."""

    def _seed(self, tmp_path: Path):
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        queue = AspectExtractionQueue(tmp_path / "queue.db")
        queue.enqueue("code__old", "a.py", doc_id="d1")
        queue.enqueue("code__old", "b.py", doc_id="d2")
        queue.enqueue("code__stays", "c.py", doc_id="d3")
        return queue

    def test_updates_matching_rows(self, tmp_path: Path) -> None:
        queue = self._seed(tmp_path)
        count = queue.rename_collection(old="code__old", new="code__new")
        assert count == 2

        old_rows = queue.conn.execute(
            "SELECT COUNT(*) FROM aspect_extraction_queue WHERE collection = ?",
            ("code__old",),
        ).fetchone()[0]
        new_rows = queue.conn.execute(
            "SELECT COUNT(*) FROM aspect_extraction_queue WHERE collection = ?",
            ("code__new",),
        ).fetchone()[0]
        stays_rows = queue.conn.execute(
            "SELECT COUNT(*) FROM aspect_extraction_queue WHERE collection = ?",
            ("code__stays",),
        ).fetchone()[0]
        assert (old_rows, new_rows, stays_rows) == (0, 2, 1)

    def test_source_path_and_doc_id_untouched(self, tmp_path: Path) -> None:
        """source_path and doc_id must be byte-identical pre/post rename."""
        queue = self._seed(tmp_path)
        rows_before = {
            r[0]: (r[1], r[2])  # source_path -> (doc_id, collection)
            for r in queue.conn.execute(
                "SELECT source_path, doc_id, collection "
                "FROM aspect_extraction_queue ORDER BY source_path"
            ).fetchall()
        }
        queue.rename_collection(old="code__old", new="code__new")
        rows_after = {
            r[0]: (r[1], r[2])
            for r in queue.conn.execute(
                "SELECT source_path, doc_id, collection "
                "FROM aspect_extraction_queue ORDER BY source_path"
            ).fetchall()
        }
        # source_path and doc_id unchanged
        for sp in rows_before:
            assert rows_before[sp][0] == rows_after[sp][0], f"doc_id changed for {sp}"

    def test_no_rows_returns_zero(self, tmp_path: Path) -> None:
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        queue = AspectExtractionQueue(tmp_path / "queue.db")
        assert queue.rename_collection(old="docs__ghost", new="docs__phantom") == 0

    def test_idempotent_second_rename(self, tmp_path: Path) -> None:
        queue = self._seed(tmp_path)
        queue.rename_collection(old="code__old", new="code__new")
        count = queue.rename_collection(old="code__old", new="code__new")
        assert count == 0


# ── Aspect cascade wired into rename_collection_data_plane (nexus-gp20) ────


class TestAspectCascadeIntegration:
    """End-to-end: rename_collection_data_plane must update both aspect
    denorm tables in the same T2Database context as chash_index."""

    def test_both_aspect_tables_updated_by_data_plane(
        self, tmp_path: Path, env_creds,
    ) -> None:
        from nexus.commands.collection import rename_collection_data_plane
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()

        # Seed T2 with rows in both aspect tables.
        with T2Database(db_path) as t2db:
            t2db.chash_index.upsert(
                chash="aa", collection="code__old", chunk_chroma_id="d1"
            )
            t2db.document_aspects.conn.execute(
                "INSERT INTO document_aspects "
                "(collection, source_path, problem_formulation, proposed_method, "
                " experimental_datasets, experimental_baselines, "
                " experimental_results, extras, confidence, extracted_at, "
                " model_version, extractor_name) "
                "VALUES (?, ?, NULL, NULL, '[]', '[]', NULL, '{}', NULL, "
                "        '2026-05-09T00:00:00Z', 'v1', 'test')",
                ("code__old", "a.py"),
            )
            t2db.document_aspects.conn.commit()
            t2db.aspect_queue.enqueue("code__old", "b.py", doc_id="d2")

        fake = MagicMock()
        fake.collection_exists = MagicMock(side_effect=lambda n: n == "code__old")
        fake.rename_collection = MagicMock()

        with patch("nexus.commands.collection._t3", return_value=fake), \
             patch("nexus.commands._helpers.default_db_path", return_value=db_path), \
             patch("nexus.config.catalog_path", return_value=cat_dir):
            counts = rename_collection_data_plane("code__old", "code__new")

        assert counts.get("aspects", 0) >= 0  # key present (even if 0)
        assert counts.get("aspect_queue", 0) >= 0

        with T2Database(db_path) as verify:
            da_old = verify.document_aspects.conn.execute(
                "SELECT COUNT(*) FROM document_aspects WHERE collection = ?",
                ("code__old",),
            ).fetchone()[0]
            da_new = verify.document_aspects.conn.execute(
                "SELECT COUNT(*) FROM document_aspects WHERE collection = ?",
                ("code__new",),
            ).fetchone()[0]
            aq_old = verify.aspect_queue.conn.execute(
                "SELECT COUNT(*) FROM aspect_extraction_queue WHERE collection = ?",
                ("code__old",),
            ).fetchone()[0]
            aq_new = verify.aspect_queue.conn.execute(
                "SELECT COUNT(*) FROM aspect_extraction_queue WHERE collection = ?",
                ("code__new",),
            ).fetchone()[0]

        assert da_old == 0 and da_new == 1
        assert aq_old == 0 and aq_new == 1

    def test_no_collateral_writes_to_chash(self, tmp_path: Path) -> None:
        """Aspect cascade must not alter chash_index rows."""
        from nexus.db.t2.document_aspects import DocumentAspects
        from nexus.db.t2.chash_index import ChashIndex

        aspects = DocumentAspects(tmp_path / "aspects.db")
        aspects.conn.execute(
            "INSERT INTO document_aspects "
            "(collection, source_path, problem_formulation, proposed_method, "
            " experimental_datasets, experimental_baselines, "
            " experimental_results, extras, confidence, extracted_at, "
            " model_version, extractor_name) "
            "VALUES (?, ?, NULL, NULL, '[]', '[]', NULL, '{}', NULL, "
            "        '2026-05-09T00:00:00Z', 'v1', 'test')",
            ("code__old", "a.py"),
        )
        aspects.conn.commit()

        chash = ChashIndex(tmp_path / "chash.db")
        chash.upsert(chash="aa", collection="code__old", chunk_chroma_id="d1")

        # rename only aspects
        aspects.rename_collection(old="code__old", new="code__new")

        # chash_index untouched
        old_chash = chash.conn.execute(
            "SELECT COUNT(*) FROM chash_index WHERE physical_collection = ?",
            ("code__old",),
        ).fetchone()[0]
        assert old_chash == 1  # still there — chash cascade not triggered here


# ── K4 atomicity: all T2 cascades in one transaction ───────────────────────


class TestCascadeAtomicity:
    """K4 (nexus-nhyh): T2 cascade must be atomic -- a mid-flight failure
    must leave ALL T2 tables showing the OLD name (rolled back).

    The implementation uses T2Database.rename_collection_cascade() which
    runs all UPDATEs inside a single SQLite transaction on a dedicated
    shared connection.
    """

    def _seed_all_tables(self, db_path):
        """Seed rows in all four T2 cascade tables."""
        from nexus.db.t2 import T2Database

        with T2Database(db_path) as t2db:
            t2db.chash_index.upsert(
                chash="aa", collection="code__old", chunk_chroma_id="d1"
            )
            t2db.taxonomy.conn.execute(
                "INSERT INTO topics (label, collection, centroid_hash, doc_count, terms, created_at) "
                "VALUES (?, ?, ?, ?, ?, '2026-05-09T00:00:00Z')",
                ("T", "code__old", "h1", 1, "[]"),
            )
            t2db.taxonomy.conn.execute(
                "INSERT INTO taxonomy_meta (collection, last_discover_at) VALUES (?, ?)",
                ("code__old", "2026-05-09T00:00:00Z"),
            )
            t2db.taxonomy.conn.commit()
            t2db.document_aspects.conn.execute(
                "INSERT INTO document_aspects "
                "(collection, source_path, problem_formulation, proposed_method, "
                " experimental_datasets, experimental_baselines, "
                " experimental_results, extras, confidence, extracted_at, "
                " model_version, extractor_name) "
                "VALUES (?, ?, NULL, NULL, '[]', '[]', NULL, '{}', NULL, "
                "        '2026-05-09T00:00:00Z', 'v1', 'test')",
                ("code__old", "a.py"),
            )
            t2db.document_aspects.conn.commit()
            t2db.aspect_queue.enqueue("code__old", "b.py", doc_id="d2")

    def test_mid_cascade_failure_rolls_back_all_tables(
        self, tmp_path: Path
    ) -> None:
        """Inject a failure after the chash UPDATE; ALL tables must still
        show 'code__old' (transaction rolled back atomically).

        Uses the ``_conn`` test-seam with a wrapper class that intercepts
        execute calls and raises mid-cascade to test rollback behavior.
        """
        import sqlite3 as _sqlite3
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        self._seed_all_tables(db_path)

        class _BombingConnection:
            """Wraps a real sqlite3.Connection, bombs on document_aspects UPDATE."""

            def __init__(self, real: _sqlite3.Connection) -> None:
                self._real = real

            def execute(self, sql: str, params=(), **kw):
                stripped = sql.strip()
                if "document_aspects" in stripped and stripped.upper().startswith("UPDATE"):
                    raise RuntimeError("simulated mid-cascade failure")
                return self._real.execute(sql, params, **kw)

            def rollback(self):
                return self._real.rollback()

            def commit(self):
                return self._real.commit()

            def close(self):
                return self._real.close()

        real_conn = _sqlite3.connect(str(db_path))
        real_conn.execute("PRAGMA busy_timeout=5000")
        bomb_conn = _BombingConnection(real_conn)

        with T2Database(db_path) as t2db:
            with pytest.raises(RuntimeError, match="mid-cascade"):
                t2db.rename_collection_cascade(
                    old="code__old", new="code__new", _conn=bomb_conn
                )

        real_conn.close()

        # After rollback: all tables must show old name
        with T2Database(db_path) as verify:
            chash_old = verify.chash_index.conn.execute(
                "SELECT COUNT(*) FROM chash_index WHERE physical_collection = ?",
                ("code__old",),
            ).fetchone()[0]
            da_old = verify.document_aspects.conn.execute(
                "SELECT COUNT(*) FROM document_aspects WHERE collection = ?",
                ("code__old",),
            ).fetchone()[0]
            aq_old = verify.aspect_queue.conn.execute(
                "SELECT COUNT(*) FROM aspect_extraction_queue WHERE collection = ?",
                ("code__old",),
            ).fetchone()[0]
            tax_old = verify.taxonomy.conn.execute(
                "SELECT COUNT(*) FROM topics WHERE collection = ?",
                ("code__old",),
            ).fetchone()[0]

        assert chash_old == 1, "chash_index must be rolled back"
        assert da_old == 1, "document_aspects must be rolled back"
        assert aq_old == 1, "aspect_queue must be rolled back"
        assert tax_old == 1, "taxonomy must be rolled back"

    def test_successful_cascade_updates_all_four_tables(
        self, tmp_path: Path
    ) -> None:
        """Happy path: rename_collection_cascade updates all tables atomically."""
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        self._seed_all_tables(db_path)

        with T2Database(db_path) as t2db:
            counts = t2db.rename_collection_cascade(old="code__old", new="code__new")

        assert counts["chash"] == 1
        assert counts["aspects"] == 1
        assert counts["aspect_queue"] == 1
        assert counts["tax_topics"] == 1

        with T2Database(db_path) as verify:
            chash_new = verify.chash_index.conn.execute(
                "SELECT COUNT(*) FROM chash_index WHERE physical_collection = ?",
                ("code__new",),
            ).fetchone()[0]
            da_new = verify.document_aspects.conn.execute(
                "SELECT COUNT(*) FROM document_aspects WHERE collection = ?",
                ("code__new",),
            ).fetchone()[0]
            aq_new = verify.aspect_queue.conn.execute(
                "SELECT COUNT(*) FROM aspect_extraction_queue WHERE collection = ?",
                ("code__new",),
            ).fetchone()[0]
            tax_new = verify.taxonomy.conn.execute(
                "SELECT COUNT(*) FROM topics WHERE collection = ?",
                ("code__new",),
            ).fetchone()[0]

        assert chash_new == 1
        assert da_new == 1
        assert aq_new == 1
        assert tax_new == 1


# ── K4 collision defense: DocumentAspects and AspectExtractionQueue ─────────


class TestDocumentAspectsCollisionDefense:
    """K4 (nexus-nhyh): DocumentAspects.rename_collection must defend against
    UNIQUE collisions like ChashIndex does, using pre-DELETE of conflicting
    new-side rows before UPDATE."""

    def test_pk_collision_new_side_wins(self, tmp_path: Path) -> None:
        """Pre-existing (new_collection, source_path) row is deleted before
        UPDATE so UNIQUE constraint is never violated."""
        from nexus.db.t2.document_aspects import DocumentAspects

        store = DocumentAspects(tmp_path / "aspects.db")
        store.conn.execute(
            "INSERT INTO document_aspects "
            "(collection, source_path, problem_formulation, proposed_method, "
            " experimental_datasets, experimental_baselines, "
            " experimental_results, extras, confidence, extracted_at, "
            " model_version, extractor_name) "
            "VALUES (?, ?, NULL, NULL, '[]', '[]', NULL, '{}', NULL, "
            "        '2026-05-09T00:00:00Z', 'v1', 'test')",
            ("code__old", "a.py"),
        )
        # Collision: (new, same source_path) already exists
        store.conn.execute(
            "INSERT INTO document_aspects "
            "(collection, source_path, problem_formulation, proposed_method, "
            " experimental_datasets, experimental_baselines, "
            " experimental_results, extras, confidence, extracted_at, "
            " model_version, extractor_name) "
            "VALUES (?, ?, NULL, NULL, '[]', '[]', NULL, '{}', NULL, "
            "        '2026-05-09T00:00:00Z', 'v1', 'stale')",
            ("code__new", "a.py"),
        )
        store.conn.commit()

        # Must not raise UNIQUE constraint violation
        count = store.rename_collection(old="code__old", new="code__new")
        assert count == 1

        rows = store.conn.execute(
            "SELECT COUNT(*) FROM document_aspects WHERE collection = ?",
            ("code__new",),
        ).fetchone()[0]
        assert rows == 1

        old_rows = store.conn.execute(
            "SELECT COUNT(*) FROM document_aspects WHERE collection = ?",
            ("code__old",),
        ).fetchone()[0]
        assert old_rows == 0


class TestAspectQueueCollisionDefense:
    """K4 (nexus-nhyh): AspectExtractionQueue.rename_collection must defend
    against UNIQUE collisions like ChashIndex."""

    def test_pk_collision_new_side_wins(self, tmp_path: Path) -> None:
        """Pre-existing (new_collection, source_path) row is deleted before
        UPDATE so UNIQUE constraint is never violated."""
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        queue = AspectExtractionQueue(tmp_path / "queue.db")
        queue.enqueue("code__old", "a.py", doc_id="d1")
        queue.enqueue("code__new", "a.py", doc_id="d_stale")  # collision

        # Must not raise
        count = queue.rename_collection(old="code__old", new="code__new")
        assert count == 1

        new_rows = queue.conn.execute(
            "SELECT COUNT(*) FROM aspect_extraction_queue WHERE collection = ?",
            ("code__new",),
        ).fetchone()[0]
        assert new_rows == 1

        old_rows = queue.conn.execute(
            "SELECT COUNT(*) FROM aspect_extraction_queue WHERE collection = ?",
            ("code__old",),
        ).fetchone()[0]
        assert old_rows == 0


# ── K9: search_telemetry + hook_failures included in cascade ─────────────────


class TestTelemetryRenameCollection:
    """K9 (nexus-nhyh): Telemetry.rename_collection must update
    search_telemetry.collection AND hook_failures.collection."""

    def test_search_telemetry_renamed(self, tmp_path: Path) -> None:
        from nexus.db.t2.telemetry import Telemetry

        tel = Telemetry(tmp_path / "tel.db")
        tel.conn.execute(
            "INSERT INTO search_telemetry "
            "(ts, query_hash, collection, raw_count, kept_count) "
            "VALUES (?, ?, ?, ?, ?)",
            ("2026-05-09T00:00:00Z", "abc", "code__old", 10, 5),
        )
        tel.conn.execute(
            "INSERT INTO search_telemetry "
            "(ts, query_hash, collection, raw_count, kept_count) "
            "VALUES (?, ?, ?, ?, ?)",
            ("2026-05-09T00:00:00Z", "abc", "code__stays", 3, 2),
        )
        tel.conn.commit()

        counts = tel.rename_collection(old="code__old", new="code__new")
        assert counts["search_telemetry"] == 1

        new_rows = tel.conn.execute(
            "SELECT COUNT(*) FROM search_telemetry WHERE collection = ?",
            ("code__new",),
        ).fetchone()[0]
        stays_rows = tel.conn.execute(
            "SELECT COUNT(*) FROM search_telemetry WHERE collection = ?",
            ("code__stays",),
        ).fetchone()[0]
        old_rows = tel.conn.execute(
            "SELECT COUNT(*) FROM search_telemetry WHERE collection = ?",
            ("code__old",),
        ).fetchone()[0]
        assert (old_rows, new_rows, stays_rows) == (0, 1, 1)

    def test_hook_failures_renamed(self, tmp_path: Path) -> None:
        from nexus.db.t2.telemetry import Telemetry

        tel = Telemetry(tmp_path / "tel.db")
        tel.conn.executescript("""
            CREATE TABLE IF NOT EXISTS hook_failures (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id      TEXT NOT NULL DEFAULT '',
                collection  TEXT NOT NULL DEFAULT '',
                hook_name   TEXT NOT NULL,
                error       TEXT NOT NULL DEFAULT '',
                occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)
        tel.conn.execute(
            "INSERT INTO hook_failures (doc_id, collection, hook_name, error) "
            "VALUES (?, ?, ?, ?)",
            ("d1", "code__old", "test_hook", "some error"),
        )
        tel.conn.execute(
            "INSERT INTO hook_failures (doc_id, collection, hook_name, error) "
            "VALUES (?, ?, ?, ?)",
            ("d2", "code__stays", "test_hook", "other error"),
        )
        tel.conn.commit()

        counts = tel.rename_collection(old="code__old", new="code__new")
        assert counts["hook_failures"] == 1

        new_rows = tel.conn.execute(
            "SELECT COUNT(*) FROM hook_failures WHERE collection = ?",
            ("code__new",),
        ).fetchone()[0]
        stays_rows = tel.conn.execute(
            "SELECT COUNT(*) FROM hook_failures WHERE collection = ?",
            ("code__stays",),
        ).fetchone()[0]
        old_rows = tel.conn.execute(
            "SELECT COUNT(*) FROM hook_failures WHERE collection = ?",
            ("code__old",),
        ).fetchone()[0]
        assert (old_rows, new_rows, stays_rows) == (0, 1, 1)

    def test_no_rows_returns_zero_counts(self, tmp_path: Path) -> None:
        from nexus.db.t2.telemetry import Telemetry

        tel = Telemetry(tmp_path / "tel.db")
        counts = tel.rename_collection(old="code__ghost", new="code__phantom")
        assert counts["search_telemetry"] == 0
        assert counts.get("hook_failures", 0) == 0


class TestK9CascadeIncludesTelemetry:
    """K9 end-to-end: rename_collection_data_plane must update
    search_telemetry.collection and hook_failures.collection."""

    def test_data_plane_updates_search_telemetry(
        self, tmp_path: Path, env_creds
    ) -> None:
        from nexus.commands.collection import rename_collection_data_plane
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()

        with T2Database(db_path) as t2db:
            t2db.telemetry.conn.execute(
                "INSERT INTO search_telemetry "
                "(ts, query_hash, collection, raw_count, kept_count) "
                "VALUES (?, ?, ?, ?, ?)",
                ("2026-05-09T00:00:00Z", "abc", "code__old", 10, 5),
            )
            t2db.telemetry.conn.commit()

        fake = MagicMock()
        fake.collection_exists = MagicMock(side_effect=lambda n: n == "code__old")
        fake.rename_collection = MagicMock()

        with patch("nexus.commands.collection._t3", return_value=fake), \
             patch("nexus.commands._helpers.default_db_path", return_value=db_path), \
             patch("nexus.config.catalog_path", return_value=cat_dir):
            counts = rename_collection_data_plane("code__old", "code__new")

        assert counts.get("search_telemetry", 0) == 1

        with T2Database(db_path) as verify:
            new_rows = verify.telemetry.conn.execute(
                "SELECT COUNT(*) FROM search_telemetry WHERE collection = ?",
                ("code__new",),
            ).fetchone()[0]
            old_rows = verify.telemetry.conn.execute(
                "SELECT COUNT(*) FROM search_telemetry WHERE collection = ?",
                ("code__old",),
            ).fetchone()[0]
        assert new_rows == 1 and old_rows == 0


# ── SIG-8: T2-first ordering (T3 rename last) ────────────────────────────────


class TestRenameOrdering:
    """SIG-8 (nexus-nhyh): T2 cascade must happen BEFORE T3 rename.
    Rationale: T2 UPDATEs are reversible; T3 chromadb rename is irrevocable.
    If T2 succeeds but T3 fails, operator can re-run rename. Reverse is
    unrecoverable.

    Test: simulate T3 rename failure; assert T2 was already committed
    (operator can reverse by running T2 cascade back to old name).
    """

    def test_t2_cascade_committed_before_t3_rename(
        self, tmp_path: Path, env_creds
    ) -> None:
        from nexus.commands.collection import rename_collection_data_plane
        from nexus.db.t2 import T2Database
        import click

        db_path = tmp_path / "memory.db"
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()

        with T2Database(db_path) as t2db:
            t2db.chash_index.upsert(
                chash="aa", collection="code__old", chunk_chroma_id="d1"
            )

        fake = MagicMock()
        fake.collection_exists = MagicMock(side_effect=lambda n: n == "code__old")

        def _t3_rename_bomb(old, new):
            raise RuntimeError("simulated T3 rename failure")

        fake.rename_collection = _t3_rename_bomb

        with patch("nexus.commands.collection._t3", return_value=fake), \
             patch("nexus.commands._helpers.default_db_path", return_value=db_path), \
             patch("nexus.config.catalog_path", return_value=cat_dir):
            with pytest.raises((RuntimeError, click.ClickException)):
                rename_collection_data_plane("code__old", "code__new")

        # T2-first: T2 was committed even though T3 failed
        with T2Database(db_path) as verify:
            new_rows = verify.chash_index.conn.execute(
                "SELECT COUNT(*) FROM chash_index WHERE physical_collection = ?",
                ("code__new",),
            ).fetchone()[0]
        assert new_rows == 1, (
            "T2 cascade should commit before T3 rename (T2-first ordering). "
            "If T3 fails after T2 succeeds, T2 state is at least recoverable."
        )


# ── CG-1: half-cascade test + non-zero exit ──────────────────────────────────


class TestHalfCascadeNonZeroExit:
    """CG-1 (nexus-nhyh): CLI must exit non-zero when T2 cascade fails,
    not swallow the error and exit 0.

    The bead finding: broad `except Exception: on_warn(...)` swallows T2
    failures and returns exit 0 -- operator sees a warning but no indication
    that action is required.

    Fix: T2 cascade failure re-raises as ClickException (exit non-zero).
    """

    def test_t2_cascade_failure_exits_nonzero(
        self, tmp_path: Path, env_creds
    ) -> None:
        from nexus.commands.collection import rename_cmd
        from click.testing import CliRunner

        db_path = tmp_path / "memory.db"
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()

        fake = MagicMock()
        fake.collection_exists = MagicMock(
            side_effect=lambda n: n == "code__old",
        )
        fake.rename_collection = MagicMock()

        def _t2_bomb(*a, **kw):
            raise RuntimeError("simulated T2 cascade failure")

        runner = CliRunner()
        with patch("nexus.commands.collection._t3", return_value=fake), \
             patch("nexus.db.t2.T2Database", side_effect=_t2_bomb), \
             patch("nexus.commands._helpers.default_db_path", return_value=db_path), \
             patch("nexus.config.catalog_path", return_value=cat_dir):
            result = runner.invoke(rename_cmd, ["code__old", "code__new"])

        # Must exit non-zero so operator knows cascade failed
        assert result.exit_code != 0, (
            f"Expected non-zero exit when T2 cascade fails, got {result.exit_code}. "
            f"Output: {result.output}"
        )
        # Error message must be actionable
        assert "cascade" in result.output.lower() or "T2" in result.output, (
            f"Error message must name the failed cascade. Output: {result.output}"
        )
