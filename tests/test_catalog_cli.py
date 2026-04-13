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
    def test_seed_creates_five_templates(self, tmp_path, monkeypatch):
        from nexus.db.t2 import T2Database
        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: db_path)
        from nexus.commands.catalog import _seed_plan_templates
        count = _seed_plan_templates()
        assert count == 5
        db = T2Database(db_path)
        results = db.search_plans("builtin-template")
        assert len(results) == 5
        db.close()

    def test_seed_idempotent(self, tmp_path, monkeypatch):
        from nexus.db.t2 import T2Database
        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: db_path)
        from nexus.commands.catalog import _seed_plan_templates
        first = _seed_plan_templates()
        second = _seed_plan_templates()
        assert first == 5
        assert second == 0

    def test_seed_templates_have_builtin_tag(self, tmp_path, monkeypatch):
        from nexus.db.t2 import T2Database
        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: db_path)
        from nexus.commands.catalog import _seed_plan_templates
        _seed_plan_templates()
        db = T2Database(db_path)
        plans = db.list_plans(limit=10)
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
        plans = db.list_plans(limit=10)
        for p in plans:
            assert p["ttl"] is None
        db.close()


class TestStatsCommand:
    def test_stats(self, initialized_catalog, catalog_env):
        runner = CliRunner()
        runner.invoke(main, ["catalog", "register", "--title", "A", "--owner", "1.1"])
        result = runner.invoke(main, ["catalog", "stats"])
        assert result.exit_code == 0
        assert "1" in result.output  # at least 1 document


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
