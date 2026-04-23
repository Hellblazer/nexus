# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from nexus.catalog.catalog import Catalog
from nexus.cli import main


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


@pytest.fixture
def catalog_env(tmp_path, monkeypatch):
    """Set up a catalog in tmp_path and point NEXUS_CATALOG_PATH at it."""
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    return catalog_dir


@pytest.fixture
def initialized_catalog(catalog_env):
    """Return a Catalog that has been init'd with one owner."""
    cat = Catalog.init(catalog_env)
    cat.register_owner("test-repo", "repo", repo_hash="abcd1234")
    return cat


class TestInitCommand:
    def test_init(self, catalog_env):
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "init"])
        assert result.exit_code == 0
        assert Catalog.is_initialized(catalog_env)

    def test_init_idempotent(self, catalog_env):
        runner = CliRunner()
        runner.invoke(main, ["catalog", "init"])
        result = runner.invoke(main, ["catalog", "init"])
        assert result.exit_code == 0


class TestNotInitialized:
    def test_list_without_init(self, catalog_env):
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "list"])
        assert result.exit_code != 0
        assert "not initialized" in result.output.lower()


class TestRegisterAndShow:
    def test_register_document(self, initialized_catalog, catalog_env):
        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "register",
            "--title", "Test Paper",
            "--owner", "1.1",
            "--type", "paper",
        ])
        assert result.exit_code == 0
        assert "1.1.1" in result.output

    def test_show_by_tumbler(self, initialized_catalog, catalog_env):
        runner = CliRunner()
        runner.invoke(main, [
            "catalog", "register",
            "--title", "Test Paper",
            "--owner", "1.1",
        ])
        result = runner.invoke(main, ["catalog", "show", "1.1.1"])
        assert result.exit_code == 0
        assert "Test Paper" in result.output

    def test_show_json(self, initialized_catalog, catalog_env):
        runner = CliRunner()
        runner.invoke(main, [
            "catalog", "register",
            "--title", "Test Paper",
            "--owner", "1.1",
        ])
        result = runner.invoke(main, ["catalog", "show", "1.1.1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["title"] == "Test Paper"


class TestListCommand:
    def test_list_entries(self, initialized_catalog, catalog_env):
        runner = CliRunner()
        runner.invoke(main, ["catalog", "register", "--title", "A", "--owner", "1.1"])
        runner.invoke(main, ["catalog", "register", "--title", "B", "--owner", "1.1"])
        result = runner.invoke(main, ["catalog", "list"])
        assert result.exit_code == 0
        assert "A" in result.output
        assert "B" in result.output

    def test_list_json(self, initialized_catalog, catalog_env):
        runner = CliRunner()
        runner.invoke(main, ["catalog", "register", "--title", "A", "--owner", "1.1"])
        result = runner.invoke(main, ["catalog", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) >= 1


class TestSearchCommand:
    def test_search(self, initialized_catalog, catalog_env):
        runner = CliRunner()
        runner.invoke(main, [
            "catalog", "register",
            "--title", "authentication module",
            "--owner", "1.1",
            "--type", "code",
        ])
        result = runner.invoke(main, ["catalog", "search", "authentication"])
        assert result.exit_code == 0
        assert "authentication" in result.output


class TestLinkCommands:
    def test_link_and_links(self, initialized_catalog, catalog_env):
        runner = CliRunner()
        runner.invoke(main, ["catalog", "register", "--title", "A", "--owner", "1.1"])
        runner.invoke(main, ["catalog", "register", "--title", "B", "--owner", "1.1"])
        result = runner.invoke(main, [
            "catalog", "link", "1.1.1", "1.1.2", "--type", "cites",
        ])
        assert result.exit_code == 0
        result = runner.invoke(main, ["catalog", "links", "1.1.1"])
        assert result.exit_code == 0
        assert "cites" in result.output

    def test_unlink(self, initialized_catalog, catalog_env):
        runner = CliRunner()
        runner.invoke(main, ["catalog", "register", "--title", "A", "--owner", "1.1"])
        runner.invoke(main, ["catalog", "register", "--title", "B", "--owner", "1.1"])
        runner.invoke(main, ["catalog", "link", "1.1.1", "1.1.2", "--type", "cites"])
        result = runner.invoke(main, ["catalog", "unlink", "1.1.1", "1.1.2", "--type", "cites"])
        assert result.exit_code == 0
        assert "1" in result.output  # removed count


class TestLinksFilterCommand:
    def test_links_filter_by_type(self, initialized_catalog, catalog_env):
        runner = CliRunner()
        runner.invoke(main, ["catalog", "register", "--title", "A", "--owner", "1.1"])
        runner.invoke(main, ["catalog", "register", "--title", "B", "--owner", "1.1"])
        runner.invoke(main, ["catalog", "link", "1.1.1", "1.1.2", "--type", "cites"])
        result = runner.invoke(main, ["catalog", "links", "--type", "cites"])
        assert result.exit_code == 0
        assert "cites" in result.output

    def test_links_filter_json(self, initialized_catalog, catalog_env):
        runner = CliRunner()
        runner.invoke(main, ["catalog", "register", "--title", "A", "--owner", "1.1"])
        runner.invoke(main, ["catalog", "register", "--title", "B", "--owner", "1.1"])
        runner.invoke(main, ["catalog", "link", "1.1.1", "1.1.2", "--type", "cites"])
        result = runner.invoke(main, ["catalog", "links", "--type", "cites", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1

    def test_links_filter_empty(self, initialized_catalog, catalog_env):
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "links", "--type", "nonexistent"])
        assert result.exit_code == 0
        assert "No links found." in result.output

    def test_links_resolve_renders_title_and_path(
        self, initialized_catalog, catalog_env,
    ):
        """--resolve renders '<title-or-path> (<tumbler>)' per endpoint.

        Bead nexus-iojz (formerly nexus-i63n). Default output is raw
        tumblers which are unreadable for external audiences.
        """
        runner = CliRunner()
        runner.invoke(main, [
            "catalog", "register", "--title", "RDR-010", "--owner", "1.1",
        ])
        runner.invoke(main, [
            "catalog", "register", "--title", "hooks",
            "--file-path", "src/nexus/hooks.py", "--owner", "1.1",
        ])
        runner.invoke(main, [
            "catalog", "link", "1.1.1", "1.1.2", "--type", "implements",
        ])

        result = runner.invoke(main, [
            "catalog", "links", "--type", "implements", "--resolve",
        ])
        assert result.exit_code == 0, result.output
        # Title form for both endpoints (register requires --title, so
        # both entries carry one; the file-path fallback is exercised
        # elsewhere via _endpoint_label unit coverage).
        assert "RDR-010 (1.1.1)" in result.output
        assert "hooks (1.1.2)" in result.output
        assert "(implements)" in result.output

    def test_endpoint_label_falls_back_to_file_path_when_no_title(
        self, initialized_catalog,
    ) -> None:
        """_endpoint_label helper prefers title, falls back to file_path,
        then bare tumbler. Covers the register-via-API path where
        documents can carry a file_path with empty title."""
        from nexus.catalog.tumbler import Tumbler
        from nexus.commands.catalog import _endpoint_label

        cat = initialized_catalog
        # Register programmatically to bypass the CLI --title requirement.
        tumbler = cat.register(
            Tumbler.parse("1.1"), "", content_type="code",
            file_path="src/nexus/session.py",
        )
        assert _endpoint_label(cat, tumbler) == f"src/nexus/session.py ({tumbler})"

    def test_links_unique_targets_dedupes_by_file_path(
        self, initialized_catalog, catalog_env,
    ):
        """--unique-targets collapses edges that point at the same file_path
        via different owner tumblers (bead nexus-iojz, formerly nexus-x6eu).
        """
        from nexus.catalog.tumbler import Tumbler

        cat = initialized_catalog
        # Register dedupes by (owner, file_path), so two tumblers sharing
        # a file_path only arise when the file is registered under
        # distinct owners, which is exactly what re-indexing after
        # owner-rename produces (before `dedupe-owners` reconciles).
        owner_a = Tumbler.parse("1.1")
        owner_b_id = cat.register_owner("second-repo", "repo", repo_hash="deadbeef")
        owner_b = Tumbler.parse(str(owner_b_id))

        src = cat.register(owner_a, "RDR-A", content_type="rdr")
        tgt_v1 = cat.register(
            owner_a, "session-v1", content_type="code",
            file_path="src/nexus/session.py",
        )
        tgt_v2 = cat.register(
            owner_b, "session-v2", content_type="code",
            file_path="src/nexus/session.py",
        )
        assert str(tgt_v1) != str(tgt_v2), (
            "this test requires two distinct tumblers sharing a file_path"
        )
        cat.link(src, tgt_v1, "implements", created_by="test")
        cat.link(src, tgt_v2, "implements", created_by="test")

        runner = CliRunner()
        default = runner.invoke(main, [
            "catalog", "links", str(src), "--type", "implements",
        ])
        assert default.exit_code == 0, default.output
        assert default.output.count("implements") == 2

        uniq = runner.invoke(main, [
            "catalog", "links", str(src), "--type", "implements",
            "--unique-targets",
        ])
        assert uniq.exit_code == 0, uniq.output
        assert uniq.output.count("implements") == 1


class TestDeleteCommand:
    def test_delete_by_tumbler(self, initialized_catalog, catalog_env):
        runner = CliRunner()
        runner.invoke(main, ["catalog", "register", "--title", "A", "--owner", "1.1"])
        result = runner.invoke(main, ["catalog", "delete", "1.1.1", "-y"])
        assert result.exit_code == 0
        assert "Deleted" in result.output

    def test_delete_not_found(self, initialized_catalog, catalog_env):
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "delete", "1.1.999", "-y"])
        assert result.exit_code != 0


class TestLinkBulkDeleteCommand:
    def test_link_bulk_delete_dry_run(self, initialized_catalog, catalog_env):
        runner = CliRunner()
        runner.invoke(main, ["catalog", "register", "--title", "A", "--owner", "1.1"])
        runner.invoke(main, ["catalog", "register", "--title", "B", "--owner", "1.1"])
        runner.invoke(main, ["catalog", "link", "1.1.1", "1.1.2", "--type", "cites"])
        result = runner.invoke(main, [
            "catalog", "link-bulk-delete", "--type", "cites", "--dry-run",
        ])
        assert result.exit_code == 0
        assert "Would remove 1 link(s)" in result.output


class TestLinkAuditCommand:
    def test_link_audit_cli(self, initialized_catalog, catalog_env):
        runner = CliRunner()
        runner.invoke(main, ["catalog", "register", "--title", "A", "--owner", "1.1"])
        runner.invoke(main, ["catalog", "register", "--title", "B", "--owner", "1.1"])
        runner.invoke(main, ["catalog", "link", "1.1.1", "1.1.2", "--type", "cites"])
        result = runner.invoke(main, ["catalog", "link-audit"])
        assert result.exit_code == 0
        assert "Total links" in result.output
        assert "cites" in result.output

    def test_link_audit_cli_json(self, initialized_catalog, catalog_env):
        runner = CliRunner()
        runner.invoke(main, ["catalog", "register", "--title", "A", "--owner", "1.1"])
        runner.invoke(main, ["catalog", "register", "--title", "B", "--owner", "1.1"])
        runner.invoke(main, ["catalog", "link", "1.1.1", "1.1.2", "--type", "cites"])
        result = runner.invoke(main, ["catalog", "link-audit", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["total"] == 1


class TestOwnersCommand:
    def test_owners(self, initialized_catalog, catalog_env):
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "owners"])
        assert result.exit_code == 0
        assert "test-repo" in result.output


class TestSeedPlanTemplates:
    def test_seed_creates_legacy_templates(self, tmp_path, monkeypatch):
        """All 12 RDR-078/092 YAML templates seed on first run.

        RDR-092 Phase 0a retired the legacy ``_PLAN_TEMPLATES`` array:
        three entries migrated to dimensional YAML (find-by-author,
        citation-traversal, type-scoped-search); two were retired as
        redundant with research-default / analyze-default. Pre-existing
        9 YAML plus 3 new = 12 total.
        """
        from nexus.db.t2 import T2Database
        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: db_path)
        from nexus.commands.catalog import _seed_plan_templates
        count = _seed_plan_templates()
        assert count == 12
        db = T2Database(db_path)
        # Every seeded template carries the builtin-template tag.
        results = db.search_plans("builtin-template", limit=20)
        assert len(results) == 12
        db.close()

    def test_seed_idempotent(self, tmp_path, monkeypatch):
        from nexus.db.t2 import T2Database
        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: db_path)
        from nexus.commands.catalog import _seed_plan_templates
        first = _seed_plan_templates()
        second = _seed_plan_templates()
        assert first == 12
        assert second == 0

    def test_seed_templates_have_builtin_tag(self, tmp_path, monkeypatch):
        from nexus.db.t2 import T2Database
        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: db_path)
        from nexus.commands.catalog import _seed_plan_templates
        _seed_plan_templates()
        db = T2Database(db_path)
        plans = db.list_plans(limit=20)
        assert len(plans) == 12
        for p in plans:
            assert "builtin-template" in p["tags"]
        db.close()

    def test_seed_templates_no_ttl(self, tmp_path, monkeypatch):
        from nexus.db.t2 import T2Database
        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: db_path)
        from nexus.commands.catalog import _seed_plan_templates
        _seed_plan_templates()
        db = T2Database(db_path)
        plans = db.list_plans(limit=20)
        for p in plans:
            assert p["ttl"] is None
        db.close()

    def test_setup_produces_dimensional_rows(self, tmp_path, monkeypatch):
        """RDR-092 Phase 0a regression: every seeded plan carries the
        dimensional identity columns (verb/name/dimensions) populated
        with no ``dimensions=NULL`` legacy leakage remaining after
        retiring ``_PLAN_TEMPLATES``.
        """
        from nexus.db.t2 import T2Database
        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: db_path)
        from nexus.commands.catalog import _seed_plan_templates
        _seed_plan_templates()
        db = T2Database(db_path)
        plans = db.list_plans(limit=20)
        assert len(plans) == 12
        for p in plans:
            assert p["verb"], f"missing verb on {p['query']!r}"
            assert p["name"], f"missing name on {p['query']!r}"
            assert p["dimensions"], f"missing dimensions on {p['query']!r}"
            assert p["scope"] == "global"
        db.close()

    def test_legacy_templates_no_longer_ingested_non_dimensionally(
        self, tmp_path, monkeypatch,
    ):
        """RDR-092 Phase 0a regression: the three migrated legacy
        shapes (find-by-author, citation-traversal, type-scoped-search)
        now enter the DB from YAML with full dimensional columns, not
        from the retired ``_PLAN_TEMPLATES`` array with NULLs.
        """
        from nexus.db.t2 import T2Database
        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: db_path)
        from nexus.commands.catalog import _seed_plan_templates
        _seed_plan_templates()
        db = T2Database(db_path)
        # Each migrated shape is identified by {strategy, expected name}.
        expected = [
            ("find-by-author", "find-by-author"),
            ("citation-traversal", "citation-traversal"),
            ("type-scoped", "type-scoped-search"),
        ]
        for strategy, name in expected:
            rows = [
                p for p in db.list_plans(limit=20)
                if (p["dimensions"] or "").find(f'"strategy":"{strategy}"') >= 0
            ]
            assert rows, f"no YAML plan carries strategy={strategy!r}"
            assert rows[0]["verb"] == "research"
            assert rows[0]["scope"] == "global"
            assert rows[0]["name"] == name
        db.close()

    def test_plan_templates_module_attr_is_retired(self):
        """RDR-092 Phase 0a: the ``_PLAN_TEMPLATES`` array was deleted;
        re-importing it must raise ``ImportError`` so any rogue caller
        fails loudly rather than silently ingesting NULL-dimension rows.
        """
        import nexus.commands.catalog as mod
        assert not hasattr(mod, "_PLAN_TEMPLATES")


class TestStatsCommand:
    def test_stats(self, initialized_catalog, catalog_env):
        runner = CliRunner()
        runner.invoke(main, ["catalog", "register", "--title", "A", "--owner", "1.1"])
        result = runner.invoke(main, ["catalog", "stats"])
        assert result.exit_code == 0
        assert "1" in result.output  # at least 1 document

    def test_stats_includes_topics_block_when_available(
        self, initialized_catalog, catalog_env, tmp_path, monkeypatch,
    ):
        """stats surfaces topics / assignments / per-source projection counts.

        Bead nexus-iojz (formerly nexus-1n0t). The catalog has three
        layers; stats previously enumerated only the first two.
        """
        from nexus.db.t2 import T2Database

        # Seed a T2 DB with one topic + one projection assignment and
        # point the command helper at it via monkeypatched default_db_path.
        t2_path = tmp_path / "memory.db"
        with T2Database(t2_path) as db:
            db.taxonomy.conn.execute(
                "INSERT INTO topics (label, collection, doc_count, created_at) "
                "VALUES ('t', 'docs__src', 5, '2026-01-01T00:00:00Z')"
            )
            tid = db.taxonomy.conn.execute(
                "SELECT id FROM topics WHERE label='t'"
            ).fetchone()[0]
            db.taxonomy.conn.commit()
            db.taxonomy.assign_topic(
                "doc-1", tid, assigned_by="projection",
                similarity=0.8, source_collection="docs__src",
            )

        import nexus.commands.catalog as catalog_mod

        monkeypatch.setattr(
            catalog_mod, "_taxonomy_stats",
            lambda: {
                "topics": 1,
                "assignments": 1,
                "distinct_topics_assigned": 1,
                "projection_by_source": {"docs__src": 1},
            },
        )

        runner = CliRunner()
        runner.invoke(main, [
            "catalog", "register", "--title", "A", "--owner", "1.1",
        ])
        result = runner.invoke(main, ["catalog", "stats"])
        assert result.exit_code == 0, result.output
        assert "Topics:" in result.output
        assert "1 topics, 1 assignments" in result.output
        assert "Projection by source:" in result.output
        assert "docs__src" in result.output

    def test_stats_json_includes_taxonomy_when_available(
        self, initialized_catalog, catalog_env, monkeypatch,
    ):
        """--json output carries the taxonomy block under a top-level
        ``taxonomy`` key so machine readers can consume it."""
        import nexus.commands.catalog as catalog_mod

        monkeypatch.setattr(
            catalog_mod, "_taxonomy_stats",
            lambda: {
                "topics": 2, "assignments": 5,
                "distinct_topics_assigned": 2,
                "projection_by_source": {"knowledge__a": 5},
            },
        )

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "stats", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["taxonomy"]["topics"] == 2
        assert data["taxonomy"]["projection_by_source"] == {"knowledge__a": 5}

    def test_stats_skips_taxonomy_block_when_absent(
        self, initialized_catalog, catalog_env, monkeypatch,
    ):
        """When _taxonomy_stats returns None (no T2 or no topics), the
        text output must not include a Topics line and --json must not
        include a taxonomy key. Regression guard against accidental
        inclusion of a misleading empty block."""
        import nexus.commands.catalog as catalog_mod

        monkeypatch.setattr(catalog_mod, "_taxonomy_stats", lambda: None)

        runner = CliRunner()
        text = runner.invoke(main, ["catalog", "stats"])
        assert text.exit_code == 0
        assert "Topics:" not in text.output
        js = runner.invoke(main, ["catalog", "stats", "--json"])
        assert js.exit_code == 0
        assert "taxonomy" not in json.loads(js.output)


class TestDiscoveryTools:
    def test_orphans_no_links(self, initialized_catalog, catalog_env):
        """Entries with no links are reported as orphans."""
        runner = CliRunner()
        runner.invoke(main, ["catalog", "register", "--title", "Orphan Doc", "--owner", "1.1", "--type", "code"])
        result = runner.invoke(main, ["catalog", "orphans", "--no-links"])
        assert result.exit_code == 0
        assert "Orphan Doc" in result.output

    def test_orphans_all_linked(self, initialized_catalog, catalog_env):
        """When all entries have links, report zero orphans."""
        runner = CliRunner()
        runner.invoke(main, ["catalog", "register", "--title", "A", "--owner", "1.1"])
        runner.invoke(main, ["catalog", "register", "--title", "B", "--owner", "1.1"])
        runner.invoke(main, ["catalog", "link", "1.1.1", "1.1.2", "--type", "cites"])
        result = runner.invoke(main, ["catalog", "orphans", "--no-links"])
        assert result.exit_code == 0
        assert "No orphan" in result.output

    def test_orphans_empty_catalog(self, initialized_catalog, catalog_env):
        """Empty catalog handles gracefully."""
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "orphans", "--no-links"])
        assert result.exit_code == 0
        assert "No orphan" in result.output


class TestVerifyCommand:
    """GH #249: nx catalog verify reconciles tumblers against ChromaDB."""

    @staticmethod
    def _register_with_doc_id(
        cat, owner_str, title, coll, doc_id, content_type="knowledge",
    ):
        from nexus.catalog.tumbler import Tumbler
        return cat.register(
            Tumbler.parse(owner_str), title,
            content_type=content_type,
            physical_collection=coll,
            meta={"doc_id": doc_id},
        )

    def _patch_t3(self, monkeypatch, present_ids_by_collection):
        """Patch _make_t3 in commands.catalog so existing_ids returns the seeded set."""
        from unittest.mock import MagicMock

        fake = MagicMock()

        def _existing_ids(coll, ids):
            present = set(present_ids_by_collection.get(coll, []))
            return {i for i in ids if i in present}

        fake.existing_ids.side_effect = _existing_ids
        monkeypatch.setattr(
            "nexus.commands.catalog._make_t3", lambda: fake,
        )
        return fake

    def test_verify_clean(self, initialized_catalog, catalog_env, monkeypatch):
        """All tumblers present in ChromaDB → '0 ghosts' summary, exit 0."""
        self._register_with_doc_id(
            initialized_catalog, "1.1", "Doc One",
            "knowledge__thing", "abc123def456aaaa",
        )
        self._patch_t3(monkeypatch, {"knowledge__thing": ["abc123def456aaaa"]})

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify"])

        assert result.exit_code == 0, result.output
        assert "0 ghosts" in result.output

    def test_verify_flags_ghosts(
        self, initialized_catalog, catalog_env, monkeypatch,
    ):
        """Tumbler in catalog but missing from T3 → surfaced as ghost, summary non-zero."""
        self._register_with_doc_id(
            initialized_catalog, "1.1", "Present Doc",
            "knowledge__thing", "aaaa111111111111",
        )
        self._register_with_doc_id(
            initialized_catalog, "1.1", "Ghost Doc",
            "knowledge__thing", "bbbb222222222222",
        )
        self._patch_t3(
            monkeypatch, {"knowledge__thing": ["aaaa111111111111"]},
        )

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify"])

        assert result.exit_code == 0, result.output
        assert "1 ghost(s) found" in result.output
        assert "Ghost Doc" in result.output
        assert "bbbb222222222222" in result.output
        # The present doc must NOT be reported as a ghost.
        assert "Present Doc" not in result.output

    def test_verify_missing_collection_is_all_ghosts(
        self, initialized_catalog, catalog_env, monkeypatch,
    ):
        """Collection absent from T3 (deleted/renamed) → every tumbler is a ghost."""
        self._register_with_doc_id(
            initialized_catalog, "1.1", "A",
            "knowledge__gone", "cccc333333333333",
        )
        # T3 patch returns empty for any collection.
        self._patch_t3(monkeypatch, {})

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify"])

        assert result.exit_code == 0, result.output
        assert "1 ghost(s) found" in result.output
        assert "knowledge__gone" in result.output

    def test_verify_collection_filter(
        self, initialized_catalog, catalog_env, monkeypatch,
    ):
        """--collection scopes the sweep to a single physical_collection."""
        self._register_with_doc_id(
            initialized_catalog, "1.1", "In Scope",
            "knowledge__foo", "dddd444444444444",
        )
        self._register_with_doc_id(
            initialized_catalog, "1.1", "Out Of Scope",
            "knowledge__bar", "eeee555555555555",
        )
        # Neither is present — both would ghost if unfiltered.
        self._patch_t3(monkeypatch, {})

        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "verify", "--collection", "knowledge__foo"],
        )

        assert result.exit_code == 0, result.output
        assert "In Scope" in result.output
        assert "Out Of Scope" not in result.output

    def test_verify_json_output(
        self, initialized_catalog, catalog_env, monkeypatch,
    ):
        """--json emits a collection→ghosts map machine-parseable output."""
        self._register_with_doc_id(
            initialized_catalog, "1.1", "Ghost",
            "knowledge__x", "ffff666666666666",
        )
        self._patch_t3(monkeypatch, {})

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "knowledge__x" in data
        assert data["knowledge__x"][0]["doc_id"] == "ffff666666666666"
        assert data["knowledge__x"][0]["title"] == "Ghost"

    def test_verify_skips_tumblers_without_doc_id(
        self, initialized_catalog, catalog_env, monkeypatch,
    ):
        """Tumblers with no meta.doc_id (e.g. raw catalog register with no indexer
        hook) are unverifiable and must be skipped silently — not reported as ghosts."""
        from nexus.catalog.tumbler import Tumbler

        # Register without the doc_id meta field.
        initialized_catalog.register(
            Tumbler.parse("1.1"), "Unverifiable",
            content_type="knowledge",
            physical_collection="knowledge__thing",
        )
        self._patch_t3(monkeypatch, {})

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify"])

        assert result.exit_code == 0, result.output
        assert "Unverifiable" not in result.output
        # No tumblers to verify, since doc_id was missing.
        assert "nothing to verify" in result.output.lower()

    def test_verify_heal_drops_ghost(
        self, initialized_catalog, catalog_env, monkeypatch,
    ):
        """--heal with `d` (drop) removes the ghost tumbler from the catalog."""
        self._register_with_doc_id(
            initialized_catalog, "1.1", "Ghost",
            "knowledge__thing", "7777aaaaaaaaaaaa",
        )
        self._patch_t3(monkeypatch, {})

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["catalog", "verify", "--heal"],
            input="d\n",
        )

        assert result.exit_code == 0, result.output
        assert "dropped" in result.output.lower()

        # Reopen the catalog and confirm the tumbler is gone.
        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        row = cat._db.execute(
            "SELECT tumbler FROM documents WHERE title = 'Ghost'"
        ).fetchone()
        assert row is None

    def test_coverage_report(self, initialized_catalog, catalog_env):
        """Coverage shows linked vs total count per content type."""
        runner = CliRunner()
        runner.invoke(main, ["catalog", "register", "--title", "A", "--owner", "1.1", "--type", "code"])
        runner.invoke(main, ["catalog", "register", "--title", "B", "--owner", "1.1", "--type", "code"])
        runner.invoke(main, ["catalog", "register", "--title", "C", "--owner", "1.1", "--type", "paper"])
        runner.invoke(main, ["catalog", "link", "1.1.1", "1.1.2", "--type", "cites"])
        result = runner.invoke(main, ["catalog", "coverage"])
        assert result.exit_code == 0
        assert "code" in result.output
        assert "%" in result.output

    def test_coverage_empty_catalog(self, initialized_catalog, catalog_env):
        """Empty catalog shows a graceful message."""
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "coverage"])
        assert result.exit_code == 0
        assert "No documents" in result.output

    def test_coverage_with_owner_filter(self, initialized_catalog, catalog_env):
        """--owner filters by tumbler prefix."""
        runner = CliRunner()
        runner.invoke(main, ["catalog", "register", "--title", "A", "--owner", "1.1", "--type", "code"])
        runner.invoke(main, ["catalog", "register", "--title", "B", "--owner", "1.1", "--type", "paper"])
        result = runner.invoke(main, ["catalog", "coverage", "--owner", "1.1"])
        assert result.exit_code == 0
        assert "%" in result.output

    def test_suggest_links_no_candidates(self, initialized_catalog, catalog_env):
        """When no code-RDR pairs match, report zero suggestions."""
        runner = CliRunner()
        runner.invoke(main, ["catalog", "register", "--title", "something", "--owner", "1.1", "--type", "code"])
        result = runner.invoke(main, ["catalog", "suggest-links"])
        assert result.exit_code == 0
        assert "0" in result.output or "No suggestions" in result.output

    def test_suggest_links_finds_unlinked_pair(self, initialized_catalog, catalog_env):
        """Finds a code-RDR pair by module name overlap that has no existing link."""
        runner = CliRunner()
        # Register code entry with a file path so stem extraction works
        cat = initialized_catalog
        from nexus.catalog.tumbler import Tumbler
        owner = Tumbler.parse("1.1")
        cat.register(owner, "chunker module", content_type="code", file_path="src/nexus/chunker.py")
        cat.register(owner, "RDR-027 chunker improvements", content_type="rdr", file_path="docs/rdr/rdr-027.md")
        result = runner.invoke(main, ["catalog", "suggest-links"])
        assert result.exit_code == 0
        # Should find the chunker → RDR pair
        assert "chunker" in result.output.lower()

    def test_suggest_links_skips_already_linked(self, initialized_catalog, catalog_env):
        """Already-linked pairs are not suggested again."""
        runner = CliRunner()
        cat = initialized_catalog
        from nexus.catalog.tumbler import Tumbler
        owner = Tumbler.parse("1.1")
        code_t = cat.register(owner, "chunker module", content_type="code", file_path="src/nexus/chunker.py")
        rdr_t = cat.register(owner, "RDR-027 chunker improvements", content_type="rdr", file_path="docs/rdr/rdr-027.md")
        cat.link(code_t, rdr_t, "implements-heuristic", created_by="index_hook")
        result = runner.invoke(main, ["catalog", "suggest-links"])
        assert result.exit_code == 0
        # The pair is already linked — should not appear
        assert "chunker" not in result.output.lower() or "0" in result.output


class TestLinkGenerate:
    """Tests for `nx catalog link-generate` command."""

    def test_link_generate_dry_run(self, initialized_catalog, catalog_env):
        """--dry-run outputs a message and exits cleanly without writing."""
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "link-generate", "--dry-run"])
        assert result.exit_code == 0
        assert "dry-run" in result.output.lower()

    def test_link_generate_empty_catalog(self, initialized_catalog, catalog_env):
        """Running on a catalog with no entries produces 0 links."""
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "link-generate"])
        assert result.exit_code == 0
        assert "0" in result.output

    def test_link_generate_idempotent(self, initialized_catalog, catalog_env):
        """Running twice produces 0 new links the second time."""
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "link-generate"])
        result = runner.invoke(main, ["catalog", "link-generate"])
        assert result.exit_code == 0
        assert "0 filepath" in result.output


class TestAgentIntegration:
    """Tests for agent-facing discovery commands: links-for-file, session-summary."""

    def _make_catalog_with_links(self, catalog_env: object) -> "Catalog":
        from nexus.catalog.catalog import Catalog
        from nexus.catalog.tumbler import Tumbler

        cat = Catalog.init(catalog_env)  # type: ignore[arg-type]
        owner = cat.register_owner("test", "repo", repo_hash="abc")
        t1 = cat.register(owner, "catalog.py", content_type="code", file_path="src/nexus/catalog.py")
        t2 = cat.register(owner, "RDR-060", content_type="rdr", file_path="docs/rdr/rdr-060.md")
        cat.link(t1, t2, "implements", created_by="test")
        return cat

    def test_links_for_file_found(self, catalog_env):
        runner = CliRunner()
        self._make_catalog_with_links(catalog_env)
        result = runner.invoke(main, ["catalog", "links-for-file", "src/nexus/catalog.py"])
        assert result.exit_code == 0
        assert "RDR-060" in result.output
        assert "implements" in result.output

    def test_links_for_file_not_found(self, catalog_env):
        runner = CliRunner()
        self._make_catalog_with_links(catalog_env)
        result = runner.invoke(main, ["catalog", "links-for-file", "nonexistent.py"])
        assert result.exit_code == 0
        assert "No catalog entry" in result.output

    def test_links_for_file_shows_direction(self, catalog_env):
        """Incoming and outgoing links are shown with arrow direction."""
        runner = CliRunner()
        cat = self._make_catalog_with_links(catalog_env)
        # Also check from the RDR side (incoming link)
        result = runner.invoke(main, ["catalog", "links-for-file", "docs/rdr/rdr-060.md"])
        assert result.exit_code == 0
        assert "implements" in result.output
        # Arrow direction — incoming or outgoing arrow
        assert ("→" in result.output or "←" in result.output)

    def test_links_for_file_not_initialized(self, tmp_path, monkeypatch):
        """Graceful failure when catalog not initialized."""
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "nocat"))
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "links-for-file", "some.py"])
        # Should fail with catalog-not-initialized error, not crash
        assert result.exit_code != 0 or "not initialized" in result.output.lower()

    def test_session_summary_no_catalog(self, tmp_path, monkeypatch):
        """Should not crash when catalog not initialized."""
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "nocat"))
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "session-summary"])
        # Either exits cleanly (0) or with a non-zero code — must not raise
        assert result.exit_code in (0, 1)

    def test_session_summary_shows_link_count(self, catalog_env):
        """session-summary should print link graph total at the end."""
        runner = CliRunner()
        self._make_catalog_with_links(catalog_env)
        # Pass --since=99999 so we don't need git to have recent activity
        result = runner.invoke(main, ["catalog", "session-summary", "--since", "99999"])
        assert result.exit_code == 0
        # Should show link graph total
        assert "link" in result.output.lower()


class TestGcCommand:
    """Tests for `nx catalog gc` — remove orphan entries with miss_count >= 2."""

    def _make_cat(self, catalog_env: object) -> "Catalog":
        from nexus.catalog.catalog import Catalog
        cat = Catalog.init(catalog_env)  # type: ignore[arg-type]
        return cat

    def test_gc_no_orphans(self, catalog_env):
        runner = CliRunner()
        cat = self._make_cat(catalog_env)
        owner = cat.register_owner("repo", "repo", repo_hash="abc")
        cat.register(owner, "current.py", content_type="code", file_path="src/current.py")
        # miss_count is 0 by default — not an orphan
        result = runner.invoke(main, ["catalog", "gc"])
        assert result.exit_code == 0
        assert "No orphan" in result.output

    def test_gc_dry_run_does_not_delete(self, catalog_env):
        runner = CliRunner()
        cat = self._make_cat(catalog_env)
        owner = cat.register_owner("repo", "repo", repo_hash="abc")
        t = cat.register(owner, "old.py", content_type="code", file_path="src/old.py")
        cat.update(t, meta={"miss_count": 2})
        result = runner.invoke(main, ["catalog", "gc", "--dry-run"])
        assert result.exit_code == 0
        assert "[dry-run]" in result.output
        # Entry still exists after dry run
        assert cat.resolve(t) is not None

    def test_gc_deletes_orphans(self, catalog_env):
        runner = CliRunner()
        cat = self._make_cat(catalog_env)
        owner = cat.register_owner("repo", "repo", repo_hash="abc")
        t = cat.register(owner, "old.py", content_type="code", file_path="src/old.py")
        cat.update(t, meta={"miss_count": 2})
        result = runner.invoke(main, ["catalog", "gc"])
        assert result.exit_code == 0
        assert "Deleted 1" in result.output
        # Entry gone from catalog
        assert cat.resolve(t) is None

    def test_gc_skips_low_miss_count(self, catalog_env):
        """Entries with miss_count < 2 must NOT be deleted."""
        runner = CliRunner()
        cat = self._make_cat(catalog_env)
        owner = cat.register_owner("repo", "repo", repo_hash="abc")
        t = cat.register(owner, "maybe.py", content_type="code", file_path="src/maybe.py")
        cat.update(t, meta={"miss_count": 1})
        result = runner.invoke(main, ["catalog", "gc"])
        assert result.exit_code == 0
        assert "No orphan" in result.output
        assert cat.resolve(t) is not None

    def test_gc_mixed_entries(self, catalog_env):
        """Only entries with miss_count >= 2 are deleted; others survive."""
        runner = CliRunner()
        cat = self._make_cat(catalog_env)
        owner = cat.register_owner("repo", "repo", repo_hash="abc")
        t_keep = cat.register(owner, "keep.py", content_type="code", file_path="src/keep.py")
        t_del = cat.register(owner, "del.py", content_type="code", file_path="src/del.py")
        cat.update(t_del, meta={"miss_count": 3})
        result = runner.invoke(main, ["catalog", "gc"])
        assert result.exit_code == 0
        assert "Deleted 1" in result.output
        assert cat.resolve(t_keep) is not None
        assert cat.resolve(t_del) is None
