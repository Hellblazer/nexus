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
