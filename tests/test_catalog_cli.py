# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json

import click
import pytest
from click.testing import CliRunner

from nexus.catalog.catalog import Catalog
from nexus.cli import main
from nexus.daemon.catalog_write_shim import CATALOG_WRITE_OPS
from nexus.db.http_vector_client import HttpVectorClient

# RDR-109 Phase 2: this file asserts cloud-mode canonical behavior
# (voyage-* embedder names, canonical-set defaults). The cloud_mode
# fixture sets credentials and forces ``is_local_mode()`` to False so
# the assertions hold regardless of the host environment.
pytestmark = pytest.mark.usefixtures("cloud_mode")


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
        data = json.loads(result.stdout)
        assert data["title"] == "Test Paper"

    def test_register_with_explicit_source_uri(
        self, initialized_catalog, catalog_env,
    ):
        """RDR-096 P3.1: ``--source-uri`` flag stores the URI verbatim."""
        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "register",
            "--title", "Aleph",
            "--owner", "1.1",
            "--type", "paper",
            "--source-uri", "chroma://knowledge__delos//papers/aleph.pdf",
        ])
        assert result.exit_code == 0, result.output
        # Verify via show.
        show = runner.invoke(main, ["catalog", "show", "1.1.1"])
        assert show.exit_code == 0
        assert "URI:" in show.output
        assert "chroma://knowledge__delos//papers/aleph.pdf" in show.output

    def test_register_rejects_malformed_uri(
        self, initialized_catalog, catalog_env,
    ):
        """RDR-096 P3.1: malformed URIs are hard errors at the
        register boundary, not silent persistence.
        """
        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "register",
            "--title", "Broken",
            "--owner", "1.1",
            "--source-uri", "not-a-uri",
        ])
        # ClickException → non-zero exit + message in stdout (Click
        # renders ClickException via echo, not via stderr/exception).
        assert result.exit_code != 0
        assert "no scheme" in result.output

    def test_show_omits_uri_line_when_empty(
        self, initialized_catalog, catalog_env,
    ):
        """Legacy entries (no path, no URI) shouldn't render an empty
        ``URI:`` line. The display is conditional on a populated value.
        """
        runner = CliRunner()
        runner.invoke(main, [
            "catalog", "register",
            "--title", "Ghost",
            "--owner", "1.1",
            # No --file-path, no --source-uri.
        ])
        result = runner.invoke(main, ["catalog", "show", "1.1.1"])
        assert result.exit_code == 0
        assert "URI:" not in result.output

    def test_show_prints_bib_fields_when_enriched(
        self, initialized_catalog, catalog_env,
    ):
        """nexus-6ha8a follow-up (critic finding 2): resolve() carries
        bib_* since nexus-9l2lg, but the plain-text ``show`` output
        never printed them. Print when non-empty/non-zero."""
        from nexus.catalog.tumbler import Tumbler

        runner = CliRunner()
        runner.invoke(main, [
            "catalog", "register",
            "--title", "Enriched Paper",
            "--owner", "1.1",
            "--type", "paper",
        ])
        initialized_catalog.update(
            Tumbler.parse("1.1.1"),
            bib_year=2019, bib_authors="Dana", bib_venue="OSDI",
            bib_citation_count=314,
        )
        result = runner.invoke(main, ["catalog", "show", "1.1.1"])
        assert result.exit_code == 0
        assert "Bib Year:    2019" in result.output
        assert "Bib Authors: Dana" in result.output
        assert "Bib Venue:   OSDI" in result.output
        assert "Citations:   314" in result.output

    def test_show_omits_bib_fields_when_not_enriched(
        self, initialized_catalog, catalog_env,
    ):
        """Un-enriched entries (bib_* at column defaults) shouldn't
        render bib lines at all — display is conditional, matching the
        URI line's convention."""
        runner = CliRunner()
        runner.invoke(main, [
            "catalog", "register",
            "--title", "Plain Paper",
            "--owner", "1.1",
            "--type", "paper",
        ])
        result = runner.invoke(main, ["catalog", "show", "1.1.1"])
        assert result.exit_code == 0
        assert "Bib Year:" not in result.output
        assert "Bib Authors:" not in result.output
        assert "Bib Venue:" not in result.output
        assert "Citations:" not in result.output


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
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_list_owner_by_name_resolves_to_tumbler(
        self, initialized_catalog, catalog_env,
    ):
        """nx catalog list --owner <name> resolves the named owner
        ('test-repo' from the fixture) to its tumbler prefix and
        returns its entries (#537, nexus-1lx7).

        Pre-fix: this leaked Tumbler.parse's int() ValueError to the
        user as a stack trace. Schema has owners.name; CLI should
        resolve by name when the input doesn't parse as a tumbler.
        """
        runner = CliRunner()
        runner.invoke(main, [
            "catalog", "register", "--title", "A", "--owner", "1.1",
        ])
        runner.invoke(main, [
            "catalog", "register", "--title", "B", "--owner", "1.1",
        ])
        result = runner.invoke(main, [
            "catalog", "list", "--owner", "test-repo",
        ])
        assert result.exit_code == 0, result.output
        assert "A" in result.output
        assert "B" in result.output

    def test_list_type_filter_pushed_to_sql(
        self, initialized_catalog, catalog_env,
    ):
        """GH #568: --type filter must be pushed into the SQL WHERE
        clause. Pre-fix, the CLI fetched LIMIT+OFFSET rows then
        Python-filtered, so a small-cardinality content_type (rdr in
        a code/docs-heavy catalog) returned empty even when matching
        rows existed.

        Construct the worst case: register many code rows + 2 rdr rows.
        Without the SQL push, ``--type rdr -n 3`` returns 0 because
        the first 51 rows fetched are all code.
        """
        runner = CliRunner()
        # Register 20 code rows.
        for i in range(20):
            runner.invoke(main, [
                "catalog", "register", "--title", f"code-{i}",
                "--owner", "1.1", "--type", "code",
            ])
        # Register 2 rdr rows.
        runner.invoke(main, [
            "catalog", "register", "--title", "rdr-A",
            "--owner", "1.1", "--type", "rdr",
        ])
        runner.invoke(main, [
            "catalog", "register", "--title", "rdr-B",
            "--owner", "1.1", "--type", "rdr",
        ])
        # Crucially, fetch -n 3 (smaller than the code-row prefix).
        result = runner.invoke(main, [
            "catalog", "list", "--type", "rdr", "-n", "3",
        ])
        assert result.exit_code == 0, result.output
        assert "rdr-A" in result.output, result.output
        assert "rdr-B" in result.output, result.output
        # And no code rows leaked.
        assert "code-" not in result.output, result.output

    def test_list_type_filter_with_owner(
        self, initialized_catalog, catalog_env,
    ):
        """GH #568 sanity: --owner + --type combined still works.
        The owner path applies the type filter Python-side (small
        cardinality per owner makes that safe).
        """
        runner = CliRunner()
        runner.invoke(main, [
            "catalog", "register", "--title", "owner-code",
            "--owner", "1.1", "--type", "code",
        ])
        runner.invoke(main, [
            "catalog", "register", "--title", "owner-rdr",
            "--owner", "1.1", "--type", "rdr",
        ])
        result = runner.invoke(main, [
            "catalog", "list", "--owner", "test-repo", "--type", "rdr",
        ])
        assert result.exit_code == 0, result.output
        assert "owner-rdr" in result.output
        assert "owner-code" not in result.output

    def test_list_owner_unknown_emits_clean_error(
        self, initialized_catalog, catalog_env,
    ):
        """An owner that is neither a valid tumbler nor a known name
        must emit a friendly error, not a Tumbler.parse stack trace.
        """
        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "list", "--owner", "no-such-owner-12345",
        ])
        # ClickException → exit code 1; output names the owner.
        assert result.exit_code != 0
        out_lower = result.output.lower()
        assert "no-such-owner-12345" in result.output
        # The raw int() ValueError from Tumbler.parse must NOT leak.
        assert "invalid literal for int" not in out_lower
        assert "traceback" not in out_lower

    def test_list_owner_tumbler_form_still_works(
        self, initialized_catalog, catalog_env,
    ):
        """No regression for the documented dotted-tumbler form."""
        runner = CliRunner()
        runner.invoke(main, [
            "catalog", "register", "--title", "A", "--owner", "1.1",
        ])
        result = runner.invoke(main, [
            "catalog", "list", "--owner", "1.1",
        ])
        assert result.exit_code == 0, result.output
        assert "A" in result.output


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
        data = json.loads(result.stdout)
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
        from nexus.commands.catalog_cmds.links import _endpoint_label

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


class TestUpdateCommand:
    def test_update_source_uri_recovery_path(
        self, initialized_catalog, catalog_env,
    ):
        """``nx catalog update <tumbler> --source-uri <uri>`` is the
        recovery path for entries whose DT-URI stamp failed during
        ``nx dt index``. The flag must accept any URI in the
        ``_KNOWN_URI_SCHEMES`` allowlist (validated at register-boundary).
        """
        runner = CliRunner()
        # Register an entry with a file:// source_uri (mimics what a
        # plain indexer registers before the dt stamp would run).
        runner.invoke(main, [
            "catalog", "register",
            "--title", "Stamp-recovery target",
            "--owner", "1.1",
            "--file-path", "/Users/x/a.pdf",
        ])

        result = runner.invoke(main, [
            "catalog", "update", "1.1.1",
            "--source-uri",
            "x-devonthink-item://8EDC855D-213F-40AD-A9CF-9543CC76476B",
        ])
        assert result.exit_code == 0, result.output

        show = runner.invoke(main, ["catalog", "show", "1.1.1"])
        assert "x-devonthink-item://8EDC855D" in show.output

    def test_update_source_uri_validates_scheme(
        self, initialized_catalog, catalog_env,
    ):
        """Unknown URI schemes are rejected at the register-boundary
        validator (``_normalize_source_uri``); the CLI must surface
        the failure cleanly rather than silently persist garbage.

        nexus-fb6x: pre-fix, the ValueError propagated uncaught and
        the operator saw a 30-line Python stack trace. Post-fix, the
        CLI catches and re-raises as ClickException so the output is
        a clean ``Error: unknown source_uri scheme '...'`` line.
        """
        runner = CliRunner()
        runner.invoke(main, [
            "catalog", "register",
            "--title", "x",
            "--owner", "1.1",
            "--file-path", "/Users/x/a.pdf",
        ])
        result = runner.invoke(main, [
            "catalog", "update", "1.1.1",
            "--source-uri", "imaginary-scheme://nope",
        ])
        assert result.exit_code != 0
        # nexus-fb6x: friendly message present, no traceback leak.
        assert "unknown" in result.output.lower()
        assert "imaginary-scheme" in result.output
        assert "Traceback" not in result.output


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

    def test_delete_service_mode_never_touches_dir_or_db(self, catalog_env, tmp_path):
        """GH #1374: ``nx catalog delete`` in service mode crashed with
        ``AttributeError: 'HttpCatalogClient' object has no attribute
        '_dir'`` inside the RDR-106 backup-before-delete snapshot step
        (``catalog_backup.snapshot_documents`` read raw ``catalog._dir`` /
        ``catalog._db``, which only exist on the local-mode Catalog).

        Spec'd against the real ``HttpCatalogClient`` (not a bare
        MagicMock) so any attribute it doesn't have raises instead of
        silently auto-materializing — the same shakeout pattern that
        caught the analogous ``t3 gc`` bug in
        test_service_mode_cli_real_client.py.
        """
        from unittest.mock import MagicMock, patch

        from nexus.catalog.catalog import CatalogEntry
        from nexus.catalog.http_catalog_client import HttpCatalogClient
        from nexus.catalog.tumbler import Tumbler

        t = Tumbler.parse("1.1.1")
        entry = CatalogEntry(
            tumbler=t, title="doomed", author="", year=0,
            content_type="prose", file_path="doomed.md", corpus="",
            physical_collection="", chunk_count=0, head_hash="",
            indexed_at="",
        )

        fake_cat = MagicMock(spec=HttpCatalogClient)
        fake_cat.resolve.return_value = entry
        fake_cat.links_from.return_value = []
        fake_cat.links_to.return_value = []

        fake_writer = MagicMock(spec=HttpCatalogClient)
        fake_writer.delete_document.return_value = True

        with (
            patch("nexus.commands.catalog._get_catalog", return_value=fake_cat),
            patch("nexus.commands.catalog._get_catalog_writer", return_value=fake_writer),
        ):
            result = CliRunner().invoke(main, ["catalog", "delete", "1.1.1", "-y"])

        assert result.exit_code == 0, result.output
        assert "Deleted" in result.output
        fake_writer.delete_document.assert_called_once_with(t)
        # Backup snapshot was written via the public API, not raw SQL/_dir.
        backup_dir = tmp_path / "catalog" / ".deleted-backups"
        assert any(backup_dir.glob("catalog-delete-*.jsonl"))


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
        data = json.loads(result.stdout)
        assert data["total"] == 1


class TestOwnersCommand:
    def test_owners(self, initialized_catalog, catalog_env):
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "owners"])
        assert result.exit_code == 0
        assert "test-repo" in result.output


class TestSeedPlanTemplates:
    def test_seed_creates_legacy_templates(self, tmp_path, monkeypatch):
        """All 15 RDR-078/092/097/098 YAML templates seed on first run.

        RDR-092 Phase 0a retired the legacy ``_PLAN_TEMPLATES`` array:
        three entries migrated to dimensional YAML (find-by-author,
        citation-traversal, type-scoped-search); two were retired as
        redundant with research-default / analyze-default. Pre-existing
        9 YAML plus 3 migrated = 12. RDR-097 added the
        hybrid-factual-lookup and traverse-then-generate plans (P1.1 /
        P1.2), and RDR-098 added abstract-themes (CheapRAG community
        pattern), bringing the total to 15.
        """
        from nexus.db.t2 import T2Database
        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.config.default_db_path", lambda: db_path)
        from nexus.commands.catalog import _seed_plan_templates
        count = _seed_plan_templates()
        assert count == 15
        db = T2Database(db_path)
        # Every seeded template carries the builtin-template tag.
        results = db.search_plans("builtin-template", limit=20)
        assert len(results) == 15
        db.close()

    def test_seed_idempotent(self, tmp_path, monkeypatch):
        from nexus.db.t2 import T2Database
        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.config.default_db_path", lambda: db_path)
        from nexus.commands.catalog import _seed_plan_templates
        first = _seed_plan_templates()
        second = _seed_plan_templates()
        assert first == 15
        assert second == 0

    def test_seed_templates_have_builtin_tag(self, tmp_path, monkeypatch):
        from nexus.db.t2 import T2Database
        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.config.default_db_path", lambda: db_path)
        from nexus.commands.catalog import _seed_plan_templates
        _seed_plan_templates()
        db = T2Database(db_path)
        plans = db.list_plans(limit=20)
        assert len(plans) == 15
        for p in plans:
            assert "builtin-template" in p["tags"]
        db.close()

    def test_setup_fails_loud_on_zero_global_tier(
        self, tmp_path, monkeypatch,
    ):
        """RDR-092 Phase 0c.1: when the global-tier YAML directory is
        empty or missing, _seed_plan_templates must raise a
        ``click.ClickException`` rather than silently returning 0.

        Rationale: an empty global tier signals a deployment gap
        (plugin_root misrouted, YAMLs deleted, stale install). Silent
        zero results are how RDR-092 discovered 0/52 live plans had
        dimensional columns populated.
        """
        from nexus.plans.seed_loader import SeedLoadResult

        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.config.default_db_path", lambda: db_path)
        # Force the scoped loader to report an empty global tier.
        monkeypatch.setattr(
            "nexus.plans.loader.load_all_tiers",
            lambda **_kw: {"global": SeedLoadResult()},
        )

        from nexus.commands.catalog import _seed_plan_templates
        with pytest.raises(click.exceptions.ClickException) as excinfo:
            _seed_plan_templates()
        assert "global" in str(excinfo.value.message).lower()

    def test_setup_fails_loud_when_global_tier_absent(
        self, tmp_path, monkeypatch,
    ):
        """RDR-092 Phase 0c.1: when ``load_all_tiers`` returns no
        ``global`` key at all (plugin_root path does not exist), the
        seeder must also raise — not silently succeed with zero rows.
        """
        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.config.default_db_path", lambda: db_path)
        monkeypatch.setattr(
            "nexus.plans.loader.load_all_tiers",
            lambda **_kw: {},
        )

        from nexus.commands.catalog import _seed_plan_templates
        with pytest.raises(click.exceptions.ClickException) as excinfo:
            _seed_plan_templates()
        msg = str(excinfo.value.message).lower()
        assert "global" in msg

    def test_setup_surfaces_per_tier_errors_to_user(
        self, tmp_path, monkeypatch, capsys,
    ):
        """RDR-092 Phase 0c.1: per-tier load errors must land on stderr
        via ``click.echo`` (not only the structured log), so the setup
        run visibly differentiates 'files found but some malformed'
        from the quiet healthy case.
        """
        from nexus.plans.seed_loader import SeedLoadResult

        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.config.default_db_path", lambda: db_path)

        # Give the global tier one healthy insert so the fail-loud
        # zero-guard does not fire; rdr-099 scope surfaces an error.
        monkeypatch.setattr(
            "nexus.plans.loader.load_all_tiers",
            lambda **_kw: {
                "global": SeedLoadResult(
                    inserted=["ok.yml"],
                    skipped_existing=[],
                    errors=[],
                ),
                "rdr-099": SeedLoadResult(
                    inserted=[],
                    skipped_existing=[],
                    errors=[("/path/broken.yml", "schema: missing verb")],
                ),
            },
        )

        from nexus.commands.catalog import _seed_plan_templates
        _seed_plan_templates()
        err = capsys.readouterr().err
        assert "broken.yml" in err
        assert "rdr-099" in err

    def test_seed_templates_no_ttl(self, tmp_path, monkeypatch):
        from nexus.db.t2 import T2Database
        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.config.default_db_path", lambda: db_path)
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
        monkeypatch.setattr("nexus.config.default_db_path", lambda: db_path)
        from nexus.commands.catalog import _seed_plan_templates
        _seed_plan_templates()
        db = T2Database(db_path)
        plans = db.list_plans(limit=20)
        assert len(plans) == 15
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
        monkeypatch.setattr("nexus.config.default_db_path", lambda: db_path)
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

        import nexus.commands.catalog_cmds.report as catalog_mod

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
        import nexus.commands.catalog_cmds.report as catalog_mod

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
        data = json.loads(result.stdout)
        assert data["taxonomy"]["topics"] == 2
        assert data["taxonomy"]["projection_by_source"] == {"knowledge__a": 5}

    def test_stats_skips_taxonomy_block_when_absent(
        self, initialized_catalog, catalog_env, monkeypatch,
    ):
        """When _taxonomy_stats returns None (no T2 or no topics), the
        text output must not include a Topics line and --json must not
        include a taxonomy key. Regression guard against accidental
        inclusion of a misleading empty block."""
        import nexus.commands.catalog_cmds.report as catalog_mod

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

        # Module-wide cloud_mode fixture forces is_local_mode() False, so
        # the real _make_t3()/make_t3() hands back an HttpVectorClient here.
        fake = MagicMock(spec=HttpVectorClient)

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
        data = json.loads(result.stdout)
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

    def test_verify_excludes_alias_rows(
        self, initialized_catalog, catalog_env, monkeypatch,
    ):
        """Alias rows (alias_of != '') must NOT appear as ghosts in verify_cmd.

        This pins the fix for nexus-xnz0o: all_documents() previously omitted
        alias_of from its SELECT, so CatalogEntry.alias_of was always "" and
        the ``not e.alias_of`` guard in verify_cmd was vacuously True.

        Setup:
          - Register a real doc with a doc_id in coll_paper; T3 says it's gone (ghost).
          - Register a second doc with a doc_id; alias it to the first doc.
          - T3 mock returns no IDs (both are "ghosts" if the alias filter is broken).
          - Correct behaviour: only the real doc is a ghost; the alias is excluded.
        """
        from nexus.catalog.tumbler import Tumbler

        coll = "knowledge__alias_test"
        # Register the canonical doc (verifiable — has doc_id, has collection).
        self._register_with_doc_id(
            initialized_catalog, "1.1", "Canonical",
            coll, "alias-test-canon-docid",
        )
        # Register a second doc and alias it to the canonical.
        alias_tumbler = initialized_catalog.register(
            Tumbler.parse("1.1"),
            "Alias Doc",
            content_type="knowledge",
            physical_collection=coll,
            meta={"doc_id": "alias-test-alias-docid"},
        )
        # Mark it as an alias of the canonical.
        initialized_catalog.set_alias(alias_tumbler, Tumbler.parse("1.1.1"))

        # T3 reports nothing in coll — both would be "ghosts" if alias filter broken.
        self._patch_t3(monkeypatch, {coll: set()})

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify", "--collection", coll])

        assert result.exit_code == 0, result.output
        # "Canonical" is a ghost (expected).
        assert "Canonical" in result.output or "1 ghost" in result.output or "ghost" in result.output.lower(), (
            f"Expected Canonical to be flagged as ghost; output:\n{result.output}"
        )
        # "Alias Doc" must NOT appear — alias rows are excluded from ghost checks.
        assert "Alias Doc" not in result.output, (
            f"Alias row 'Alias Doc' appeared in verify output — alias filter broken:\n{result.output}"
        )

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


class TestLinkDensity:
    """Tests for `nx catalog link-density --by-collection` (RDR-097 P1.4)."""

    def test_empty_catalog_reports_no_collections(
        self, initialized_catalog, catalog_env
    ):
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "link-density"])
        assert result.exit_code == 0
        assert "No collections" in result.output

    def test_dense_collection_reports_nonzero_p50(
        self, initialized_catalog, catalog_env
    ):
        """A collection with linked entries reports a non-zero frontier-p50."""
        from nexus.catalog.tumbler import Tumbler
        cat = initialized_catalog
        owner = Tumbler.parse("1.1")
        # Register four entries in the same collection.
        a = cat.register(
            owner, "A", content_type="paper",
            physical_collection="knowledge__test_dense", file_path="a.pdf",
        )
        b = cat.register(
            owner, "B", content_type="paper",
            physical_collection="knowledge__test_dense", file_path="b.pdf",
        )
        c = cat.register(
            owner, "C", content_type="paper",
            physical_collection="knowledge__test_dense", file_path="c.pdf",
        )
        d = cat.register(
            owner, "D", content_type="paper",
            physical_collection="knowledge__test_dense", file_path="d.pdf",
        )
        # Wire them into a small connected graph so depth-2 BFS sees nodes.
        cat.link(a, b, "cites", created_by="test")
        cat.link(b, c, "cites", created_by="test")
        cat.link(c, d, "cites", created_by="test")
        cat.link(a, c, "relates", created_by="test")

        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "link-density", "--threshold", "1"]
        )
        assert result.exit_code == 0, result.output
        assert "knowledge__test_dense" in result.output
        # link types observed should appear in the row
        assert "cites" in result.output
        assert "relates" in result.output
        # A/B/C/D each see at least one node at depth 2 — flag should be 'ok'
        # at threshold=1.
        # Pull the row and check that the p50 column is not 0.0.
        for line in result.output.splitlines():
            if "knowledge__test_dense" in line:
                cols = line.split()
                # cols layout: collection seeds p50 p90 flag link_types
                assert float(cols[2]) > 0.0, f"p50 should be > 0: {line}"
                break

    def test_isolated_collection_reports_zero_density(
        self, initialized_catalog, catalog_env
    ):
        """A collection where entries have no outgoing links reports p50=0."""
        from nexus.catalog.tumbler import Tumbler
        cat = initialized_catalog
        owner = Tumbler.parse("1.1")
        cat.register(
            owner, "lonely-1", content_type="code",
            physical_collection="code__isolated", file_path="x.py",
        )
        cat.register(
            owner, "lonely-2", content_type="code",
            physical_collection="code__isolated", file_path="y.py",
        )

        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "link-density", "--threshold", "3"]
        )
        assert result.exit_code == 0, result.output
        assert "code__isolated" in result.output
        for line in result.output.splitlines():
            if "code__isolated" in line:
                cols = line.split()
                assert float(cols[2]) == 0.0, f"p50 should be 0: {line}"
                assert "low" in line, "low-density flag expected"
                break


class TestLinkGenerate:
    """Tests for `nx catalog link-generate` deprecation alias (nexus-2297).

    The canonical verb is now ``generate-links``; ``link-generate``
    delegates to it and emits a deprecation warning. Tests verify the
    delegation works end-to-end (dry-run path, empty-catalog path,
    idempotent path) and that the deprecation warning fires.
    """

    def test_link_generate_dry_run(self, initialized_catalog, catalog_env):
        """--dry-run outputs a message and exits cleanly without writing."""
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "link-generate", "--dry-run"])
        assert result.exit_code == 0
        # Deprecation warning fires from the alias.
        assert "deprecated" in result.output.lower()
        # The canonical command's dry-run message lands.
        assert "would generate" in result.output.lower()

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
        # Canonical generate_links_cmd phrases the count as
        # "RDR filepath links created: 0" (was "Generated 0 filepath links."
        # in the pre-deprecation impl).
        assert "filepath links created: 0" in result.output


class TestLinkGenerateDeprecation:
    """nexus-2297: alias must emit the deprecation warning on stderr."""

    def test_link_generate_alias_emits_deprecation_warning(
        self, initialized_catalog, catalog_env,
    ):
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "link-generate"])
        assert result.exit_code == 0
        assert "link-generate" in result.output
        assert "deprecated" in result.output.lower()
        # Points the operator at the canonical name.
        assert "generate-links" in result.output


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
        """nexus-tnz3: 4.29.1 dry-run is the DEFAULT — no flags = report-only.
        Entry must remain after a default invocation."""
        runner = CliRunner()
        cat = self._make_cat(catalog_env)
        owner = cat.register_owner("repo", "repo", repo_hash="abc")
        t = cat.register(owner, "old.py", content_type="code", file_path="src/old.py")
        cat.update(t, meta={"miss_count": 2})
        result = runner.invoke(main, ["catalog", "gc"])
        assert result.exit_code == 0
        # Default is dry-run; entry survives.
        assert "would be deleted" in result.output
        assert cat.resolve(t) is not None

    def test_gc_deletes_orphans(self, catalog_env):
        """4.29.1 requires --no-dry-run --confirm to actually delete."""
        runner = CliRunner()
        cat = self._make_cat(catalog_env)
        owner = cat.register_owner("repo", "repo", repo_hash="abc")
        t = cat.register(owner, "old.py", content_type="code", file_path="src/old.py")
        cat.update(t, meta={"miss_count": 2})
        result = runner.invoke(
            main, ["catalog", "gc", "--no-dry-run", "--confirm"],
        )
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
        """Only entries with miss_count >= 2 are deleted; others survive.

        4.29.1 default is dry-run; pass --no-dry-run --confirm to delete.
        """
        runner = CliRunner()
        cat = self._make_cat(catalog_env)
        owner = cat.register_owner("repo", "repo", repo_hash="abc")
        t_keep = cat.register(owner, "keep.py", content_type="code", file_path="src/keep.py")
        t_del = cat.register(owner, "del.py", content_type="code", file_path="src/del.py")
        cat.update(t_del, meta={"miss_count": 3})
        result = runner.invoke(
            main, ["catalog", "gc", "--no-dry-run", "--confirm"],
        )
        assert result.exit_code == 0
        assert "Deleted 1" in result.output
        assert cat.resolve(t_keep) is not None
        assert cat.resolve(t_del) is None


class TestCollectionNameCommand:
    """RDR-103 Phase 3b: ``nx catalog collection-name --content-type X``
    resolves the conformant ``CollectionName`` for the current repo and
    echoes its rendered string. Plugin-layer call sites (rdr-close
    SKILL.md) use this to look up the post-mortem target collection
    without constructing the legacy 2-segment shape themselves.
    """

    def test_emits_conformant_name_for_registered_repo(
        self, catalog_env, tmp_path, monkeypatch,
    ):
        cat = Catalog.init(catalog_env)
        repo = tmp_path / "myproject"
        repo.mkdir()
        cat.register_owner(
            name="myproject",
            owner_type="repo",
            repo_hash="cafef00d",
            repo_root=str(repo),
        )
        monkeypatch.setattr(
            "nexus.repo_identity._repo_identity",
            lambda r: ("myproject", "cafef00d"),
        )
        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "collection-name",
            "--content-type", "knowledge",
            "--repo", str(repo),
        ])
        assert result.exit_code == 0, result.output
        # Tumbler 1.1 to owner_segment 1-1; canonical model for knowledge
        # is voyage-context-3; new tuple lands at v1.
        assert result.output.strip() == "knowledge__1-1__voyage-context-3__v1"

    def test_emits_conformant_name_for_code(
        self, catalog_env, tmp_path, monkeypatch,
    ):
        cat = Catalog.init(catalog_env)
        repo = tmp_path / "myproject"
        repo.mkdir()
        cat.register_owner(
            name="myproject",
            owner_type="repo",
            repo_hash="cafef00d",
            repo_root=str(repo),
        )
        monkeypatch.setattr(
            "nexus.repo_identity._repo_identity",
            lambda r: ("myproject", "cafef00d"),
        )
        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "collection-name",
            "--content-type", "code",
            "--repo", str(repo),
        ])
        assert result.exit_code == 0, result.output
        # Code uses voyage-code-3.
        assert result.output.strip() == "code__1-1__voyage-code-3__v1"

    def test_rejects_unknown_content_type(
        self, catalog_env, tmp_path, monkeypatch,
    ):
        Catalog.init(catalog_env)
        repo = tmp_path / "anywhere"
        repo.mkdir()
        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "collection-name",
            "--content-type", "garbage",
            "--repo", str(repo),
        ])
        assert result.exit_code != 0

    def test_fails_when_owner_not_registered(
        self, catalog_env, tmp_path, monkeypatch,
    ):
        """No owner row for the repo: helper raises ``LookupError``;
        the CLI surfaces that as a non-zero exit with a clear message
        instructing the user to index first.
        """
        Catalog.init(catalog_env)
        repo = tmp_path / "fresh"
        repo.mkdir()
        monkeypatch.setattr(
            "nexus.repo_identity._repo_identity",
            lambda r: ("fresh", "deadbeef"),
        )
        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "collection-name",
            "--content-type", "knowledge",
            "--repo", str(repo),
        ])
        assert result.exit_code != 0
        # The message should be specific enough that operators realise
        # the repo needs to be indexed before the conformant name can be
        # resolved (the indexer's _catalog_hook is what registers the
        # owner row).
        assert "owner" in result.output.lower() or "index" in result.output.lower()

    def test_fails_when_catalog_not_initialized(
        self, catalog_env, tmp_path,
    ):
        repo = tmp_path / "anywhere"
        repo.mkdir()
        runner = CliRunner()
        # catalog_env points NEXUS_CATALOG_PATH at tmp_path/catalog but
        # no init has been run.
        result = runner.invoke(main, [
            "catalog", "collection-name",
            "--content-type", "knowledge",
            "--repo", str(repo),
        ])
        assert result.exit_code != 0
        assert "not initialized" in result.output.lower()

    def test_default_repo_is_cwd(
        self, catalog_env, tmp_path, monkeypatch,
    ):
        """When ``--repo`` is omitted, the command resolves the current
        working directory."""
        cat = Catalog.init(catalog_env)
        repo = tmp_path / "myproject"
        repo.mkdir()
        cat.register_owner(
            name="myproject",
            owner_type="repo",
            repo_hash="cafef00d",
            repo_root=str(repo),
        )
        monkeypatch.setattr(
            "nexus.repo_identity._repo_identity",
            lambda r: ("myproject", "cafef00d"),
        )
        monkeypatch.chdir(repo)
        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "collection-name",
            "--content-type", "rdr",
        ])
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "rdr__1-1__voyage-context-3__v1"


class TestSeam3OwnersCarve:
    """Contract pins for the nexus-kgyoz seam 3 owners command carve.

    Non-vacuous: each pin fails if the carve regresses — by re-inlining the
    commands into ``commands.catalog``, dropping the ``register`` wiring, or
    binding ``_get_catalog`` at import time (which would break the
    ``patch("nexus.commands.catalog._get_catalog", …)`` test seam).

    Note the two commands take different catalog-access paths: ``owners``
    reads via ``_get_catalog()`` (defended by the patch seam below), while
    ``dedupe-owners`` opens an admin catalog via ``make_catalog_admin()`` and
    is NOT covered by that patch target — its behavioural coverage lives in
    ``test_catalog_dedupe.py`` through ``NEXUS_CATALOG_PATH`` env plumbing.
    """

    def test_owner_commands_registered_on_group(self):
        from nexus.cli import main
        catalog_group = main.commands["catalog"]
        assert "owners" in catalog_group.commands
        assert "dedupe-owners" in catalog_group.commands

    def test_owner_commands_defined_in_carved_module(self):
        """The callbacks live in catalog_cmds.owners, not commands.catalog."""
        from nexus.cli import main
        from nexus.commands.catalog_cmds import owners as owners_mod
        catalog_group = main.commands["catalog"]
        assert catalog_group.commands["owners"].callback is owners_mod.owners_cmd.callback
        assert (
            catalog_group.commands["dedupe-owners"].callback
            is owners_mod.dedupe_owners_cmd.callback
        )

    def test_owners_command_routes_get_catalog_through_module(self):
        """Patching commands.catalog._get_catalog is observed by the carved
        ``owners`` command — proves module-routed (not import-bound) access."""
        from unittest.mock import MagicMock, patch

        from nexus.cli import main

        # NX_STORAGE_BACKEND stays pinned to sqlite (autouse fixture), so
        # _get_catalog() really returns a local Catalog reader here.
        fake = MagicMock(spec=Catalog)
        fake.list_owners.return_value = [
            {"tumbler_prefix": "1.1", "owner_type": "repo", "name": "sentinel-owner"},
        ]
        with patch("nexus.commands.catalog._get_catalog", return_value=fake):
            result = CliRunner().invoke(main, ["catalog", "owners"])
        assert result.exit_code == 0, result.output
        assert "sentinel-owner" in result.output
        fake.list_owners.assert_called_once()


class TestKgyozBackfillCarve:
    """Contract pins for the nexus-kgyoz backfill-owner-id command carve.

    Non-vacuous: fails if the command is re-inlined into ``commands.catalog``,
    if the ``register`` wiring is dropped, or if the carved module's lazy
    service-mode guard / module-routed access regresses.
    """

    def test_backfill_command_registered_on_group(self):
        from nexus.cli import main
        assert "backfill-owner-id" in main.commands["catalog"].commands

    def test_backfill_command_defined_in_carved_module(self):
        from nexus.cli import main
        from nexus.commands.catalog_cmds import backfill as backfill_mod
        cmd = main.commands["catalog"].commands["backfill-owner-id"]
        assert cmd.callback is backfill_mod.backfill_owner_id_cmd.callback

    def test_backfill_service_mode_guard_fires_through_carved_module(self):
        """End-to-end through the group: service mode is refused with a clear
        error. Exercises the carved command's lazy imports and guard before
        any catalog access — no real catalog needed."""
        from unittest.mock import patch

        from nexus.cli import main

        with patch("nexus.catalog.factory._is_catalog_service_mode", return_value=True):
            result = CliRunner().invoke(main, ["catalog", "backfill-owner-id"])
        assert result.exit_code != 0
        assert "not supported in service mode" in result.output


class TestKgyozLinksCarve:
    """Contract pins for the nexus-kgyoz links-family command carve.

    Non-vacuous: fails if any link command is re-inlined into
    ``commands.catalog``, if the ``register`` wiring is dropped, if the
    moved link-only helpers regress, or if module-routed ``_get_catalog``
    access is bound at import time.
    """

    LINK_COMMANDS = [
        "link", "unlink", "links", "link-bulk-delete", "link-audit",
        "links-for-file", "link-density", "suggest-links",
        "generate-links", "link-generate",
    ]

    def test_all_link_commands_registered_on_group(self):
        from nexus.cli import main
        catalog_group = main.commands["catalog"]
        for name in self.LINK_COMMANDS:
            assert name in catalog_group.commands, f"{name} not registered"

    def test_link_commands_defined_in_carved_module(self):
        """Every link command's callback lives in catalog_cmds.links."""
        from nexus.cli import main
        catalog_group = main.commands["catalog"]
        for name in self.LINK_COMMANDS:
            cmd = catalog_group.commands[name]
            assert cmd.callback.__module__ == "nexus.commands.catalog_cmds.links", (
                f"{name} callback not in carved module: {cmd.callback.__module__}"
            )

    def test_link_only_helpers_live_in_carved_module(self):
        """The two link-render helpers moved with the commands."""
        from nexus.commands.catalog_cmds import links as links_mod
        assert hasattr(links_mod, "_endpoint_label")
        assert hasattr(links_mod, "_unique_edges_by_target")

    def test_link_audit_routes_get_catalog_through_module(self):
        """End-to-end through the group: patching
        commands.catalog._get_catalog is observed by the carved link-audit
        command — proves module-routed (not import-bound) access."""
        from unittest.mock import MagicMock, patch

        from nexus.cli import main

        # NX_STORAGE_BACKEND stays pinned to sqlite (autouse fixture), so
        # _get_catalog() really returns a local Catalog reader here.
        fake = MagicMock(spec=Catalog)
        fake.link_audit.return_value = {
            "total": 7, "orphaned_count": 0, "duplicate_count": 0,
            "by_type": {}, "by_creator": {}, "orphaned": [],
        }
        with patch("nexus.commands.catalog._get_catalog", return_value=fake):
            result = CliRunner().invoke(main, ["catalog", "link-audit", "--json"])
        assert result.exit_code == 0, result.output
        assert '"total": 7' in result.output
        fake.link_audit.assert_called_once()

    def test_links_flat_query_runs_through_carved_body(self):
        """End-to-end through the group for the most complex carved command:
        the `links` flat-query + JSON render path runs intact (guards against
        an intra-body line drop that the __module__ pin would miss)."""
        from unittest.mock import MagicMock, patch

        from nexus.cli import main

        edge = MagicMock()
        edge.to_dict.return_value = {"from": "1.1.1", "to": "1.2.1", "link_type": "cites"}
        # NX_STORAGE_BACKEND stays pinned to sqlite (autouse fixture), so
        # _get_catalog() really returns a local Catalog reader here.
        fake = MagicMock(spec=Catalog)
        fake.link_query.return_value = [edge]
        with patch("nexus.commands.catalog._get_catalog", return_value=fake):
            result = CliRunner().invoke(
                main, ["catalog", "links", "--created-by", "bib_enricher", "--json"],
            )
        assert result.exit_code == 0, result.output
        assert '"link_type": "cites"' in result.output
        fake.link_query.assert_called_once()

    def test_link_generate_delegates_to_registered_generate_links(self):
        """link-generate's ctx.invoke target IS the object registered as
        generate-links — pins the delegation across the carve."""
        from nexus.cli import main
        from nexus.commands.catalog_cmds import links as links_mod

        catalog_group = main.commands["catalog"]
        assert catalog_group.commands["generate-links"] is links_mod.generate_links_cmd


class TestWhh61BackupsCarve:
    """Contract pins for the nexus-whh61.4 backups command carve.

    Non-vacuous: fails on re-inline into ``commands.catalog``, a dropped
    ``register`` call, or import-bound (non-module-routed) ``_get_catalog``.

    Note the two access paths: ``list-backups`` / ``vacuum-backups`` read via
    ``_get_catalog()`` (defended by the patch-seam pins below), while
    ``undelete`` opens an admin catalog via ``make_catalog_admin()`` — pinned
    here by its daemon-live guard; its full restore round-trip lives in
    ``test_catalog_backup_and_safety.py`` via ``NEXUS_CATALOG_PATH`` plumbing.
    """

    BACKUP_COMMANDS = ["list-backups", "undelete", "vacuum-backups"]

    def test_backup_commands_registered_on_group(self):
        from nexus.cli import main
        catalog_group = main.commands["catalog"]
        for name in self.BACKUP_COMMANDS:
            assert name in catalog_group.commands, f"{name} not registered"

    def test_backup_commands_defined_in_carved_module(self):
        from nexus.cli import main
        catalog_group = main.commands["catalog"]
        for name in self.BACKUP_COMMANDS:
            assert catalog_group.commands[name].callback.__module__ == (
                "nexus.commands.catalog_cmds.backups"
            ), f"{name} not in carved module"

    def test_list_backups_routes_get_catalog_through_module(self):
        """End-to-end: patching commands.catalog._get_catalog is observed by
        the carved list-backups command — proves module-routed access."""
        from unittest.mock import MagicMock, patch

        from nexus.cli import main

        # NX_STORAGE_BACKEND stays pinned to sqlite (autouse fixture), so
        # _get_catalog() really returns a local Catalog reader here.
        fake = MagicMock(spec=Catalog)
        with patch("nexus.commands.catalog._get_catalog", return_value=fake), \
                patch("nexus.catalog.catalog_backup.list_backups", return_value=[]) as lb:
            result = CliRunner().invoke(main, ["catalog", "list-backups"])
        assert result.exit_code == 0, result.output
        assert "No backups found." in result.output
        lb.assert_called_once_with(fake)

    def test_vacuum_backups_routes_get_catalog_through_module(self):
        """Symmetric to list-backups: vacuum-backups also routes _get_catalog
        through the module object (would fail if bound at import time)."""
        from unittest.mock import MagicMock, patch

        from nexus.cli import main

        # NX_STORAGE_BACKEND stays pinned to sqlite (autouse fixture), so
        # _get_catalog() really returns a local Catalog reader here.
        fake = MagicMock(spec=Catalog)
        with patch("nexus.commands.catalog._get_catalog", return_value=fake), \
                patch(
                    "nexus.catalog.catalog_backup.vacuum_old_backups",
                    return_value=(0, 0),
                ) as vac:
            result = CliRunner().invoke(main, ["catalog", "vacuum-backups"])
        assert result.exit_code == 0, result.output
        vac.assert_called_once()
        assert vac.call_args.args[0] is fake

    def test_undelete_surfaces_daemon_live_guard(self):
        """undelete uses the admin path (make_catalog_admin), not _get_catalog.
        Pin the CLI-layer guard: a live daemon surfaces as a ClickException."""
        from unittest.mock import patch

        from nexus.catalog.factory import CatalogAdminDaemonLiveError
        from nexus.cli import main

        with patch(
            "nexus.catalog.factory.make_catalog_admin",
            side_effect=CatalogAdminDaemonLiveError("daemon is live"),
        ):
            result = CliRunner().invoke(main, ["catalog", "undelete", "snap.jsonl"])
        assert result.exit_code != 0
        assert "daemon is live" in result.output


class TestWhh61CollectionsCarve:
    """Contract pins for the nexus-whh61.4 collections command carve.

    Non-vacuous: fails on re-inline into ``commands.catalog``, a dropped
    ``register`` call, or import-bound (non-module-routed) ``_get_catalog``.
    """

    COLLECTION_COMMANDS = [
        "backfill-collections", "collection-name",
        "rename-collection", "collection-gc",
    ]

    def test_collection_commands_registered_on_group(self):
        from nexus.cli import main
        catalog_group = main.commands["catalog"]
        for name in self.COLLECTION_COMMANDS:
            assert name in catalog_group.commands, f"{name} not registered"

    def test_collection_commands_defined_in_carved_module(self):
        from nexus.cli import main
        catalog_group = main.commands["catalog"]
        for name in self.COLLECTION_COMMANDS:
            assert catalog_group.commands[name].callback.__module__ == (
                "nexus.commands.catalog_cmds.collections"
            ), f"{name} not in carved module"

    def test_backfill_collections_routes_get_catalog_through_module(self):
        """End-to-end: patching commands.catalog._get_catalog + _get_catalog_writer
        is observed by the carved backfill-collections command — proves
        module-routed access. Empty T3 + empty catalog → nothing to backfill."""
        from unittest.mock import MagicMock, patch

        from nexus.cli import main

        # NX_STORAGE_BACKEND stays pinned to sqlite (autouse fixture), so
        # _get_catalog() really returns a local Catalog reader here.
        cat = MagicMock(spec=Catalog)
        cat.distinct_doc_collections.return_value = []
        cat.list_collections.return_value = []
        # cloud_mode (module-wide) forces is_local_mode() False -> real
        # make_t3() would hand back an HttpVectorClient here.
        t3 = MagicMock(spec=HttpVectorClient)
        t3.list_collections.return_value = []
        with patch("nexus.commands.catalog._get_catalog", return_value=cat), \
                patch(
                    "nexus.commands.catalog._get_catalog_writer",
                    return_value=MagicMock(spec=list(CATALOG_WRITE_OPS)),
                ), \
                patch("nexus.db.make_t3", return_value=t3):
            result = CliRunner().invoke(main, ["catalog", "backfill-collections", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "Nothing to backfill" in result.output
        cat.distinct_doc_collections.assert_called()


class TestWhh61MigrationCarve:
    """Contract pins for the nexus-whh61.4 migration command carve.

    Non-vacuous: fails on re-inline into ``commands.catalog``, a dropped
    ``register`` call, or import-bound (non-module-routed) ``_get_catalog``.
    The original used the DIRECT ``_get_catalog()`` form; the carve converts
    it to the module-routed form — pin 3 proves the patch seam still fires.
    """

    def test_migrate_fallback_registered_on_group(self):
        from nexus.cli import main
        assert "migrate-fallback" in main.commands["catalog"].commands

    def test_migrate_fallback_defined_in_carved_module(self):
        from nexus.cli import main
        cmd = main.commands["catalog"].commands["migrate-fallback"]
        assert cmd.callback.__module__ == "nexus.commands.catalog_cmds.migration"

    def test_migrate_fallback_routes_get_catalog_through_module(self):
        """Patching commands.catalog._get_catalog is observed by the carved
        migrate-fallback — proves the direct->module-routed conversion."""
        from unittest.mock import MagicMock, patch

        from nexus.cli import main

        # NX_STORAGE_BACKEND stays pinned to sqlite (autouse fixture), so
        # _get_catalog() really returns a local Catalog reader here.
        cat = MagicMock(spec=Catalog)
        cat.get_collection.return_value = None  # -> ClickException before any write
        with patch("nexus.commands.catalog._get_catalog", return_value=cat), \
                patch(
                    "nexus.commands.catalog._get_catalog_writer",
                    return_value=MagicMock(spec=list(CATALOG_WRITE_OPS)),
                ):
            result = CliRunner().invoke(main, ["catalog", "migrate-fallback", "docs__default"])
        assert result.exit_code != 0
        assert "not registered in the collections" in result.output
        cat.get_collection.assert_called_once_with("docs__default")

    def test_migrate_fallback_dry_run_emits_proposal_through_carved_body(self):
        """Deeper pin: the dry-run proposal path runs intact through the
        carved body (guards an intra-body line drop the early-exit pin and
        __module__ pin would miss)."""
        from unittest.mock import MagicMock, patch

        from nexus.cli import main

        entry = MagicMock()
        entry.tumbler = "1.1.1"
        # NX_STORAGE_BACKEND stays pinned to sqlite (autouse fixture), so
        # _get_catalog() really returns a local Catalog reader here.
        cat = MagicMock(spec=Catalog)
        cat.get_collection.return_value = {"name": "docs__default"}  # non-None
        cat.list_by_collection.return_value = [entry]
        with patch("nexus.commands.catalog._get_catalog", return_value=cat), \
                patch(
                    "nexus.commands.catalog._get_catalog_writer",
                    return_value=MagicMock(spec=list(CATALOG_WRITE_OPS)),
                ):
            result = CliRunner().invoke(
                main, ["catalog", "migrate-fallback", "docs__default", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "docs__default: 1 doc(s) ->" in result.output
        cat.list_by_collection.assert_called_once_with("docs__default")


class TestWhh61MaintenanceCarve:
    """Contract pins for the nexus-whh61.4 maintenance (gc + chash-reconcile) carve.

    Non-vacuous: fails on re-inline into ``commands.catalog``, a dropped
    ``register`` call, or import-bound (non-module-routed) ``_get_catalog``.
    """

    MAINT_COMMANDS = ["gc", "chash-reconcile"]

    def test_maintenance_commands_registered_on_group(self):
        from nexus.cli import main
        catalog_group = main.commands["catalog"]
        for name in self.MAINT_COMMANDS:
            assert name in catalog_group.commands, f"{name} not registered"

    def test_maintenance_commands_defined_in_carved_module(self):
        from nexus.cli import main
        catalog_group = main.commands["catalog"]
        for name in self.MAINT_COMMANDS:
            assert catalog_group.commands[name].callback.__module__ == (
                "nexus.commands.catalog_cmds.maintenance"
            ), f"{name} not in carved module"

    def test_gc_routes_get_catalog_through_module(self):
        """End-to-end: patching commands.catalog._get_catalog is observed by
        the carved gc command — proves module-routed access. Empty catalog →
        no orphans."""
        from unittest.mock import MagicMock, patch

        from nexus.cli import main

        # NX_STORAGE_BACKEND stays pinned to sqlite (autouse fixture), so
        # _get_catalog() really returns a local Catalog reader here.
        cat = MagicMock(spec=Catalog)
        cat.all_documents.return_value = []
        with patch("nexus.commands.catalog._get_catalog", return_value=cat), \
                patch(
                    "nexus.commands.catalog._get_catalog_writer",
                    return_value=MagicMock(spec=list(CATALOG_WRITE_OPS)),
                ):
            result = CliRunner().invoke(main, ["catalog", "gc"])
        assert result.exit_code == 0, result.output
        assert "No orphan entries found." in result.output
        cat.all_documents.assert_called()


class TestWhh61RemediationCarve:
    """Contract pins for the nexus-whh61.4 remediation carve.

    Non-vacuous: fails on re-inline into ``commands.catalog``, a dropped
    ``register`` call, the six shared helpers not moving, or import-bound
    (non-module-routed) ``_get_catalog``.
    """

    REMEDIATION_COMMANDS = ["remediate-paths", "prune-stale"]
    MOVED_HELPERS = [
        "_build_basename_index", "_entry_needs_remediation",
        "_resolve_via_devonthink", "_resolve_candidate",
        "_rdr_prefix_of", "_build_rdr_prefix_index",
        # module constants that anchor the helpers, moved with them:
        "_REMEDIATE_DEFAULT_EXTENSIONS", "_RDR_PREFIX_RE",
    ]

    def test_remediation_commands_registered_on_group(self):
        from nexus.cli import main
        catalog_group = main.commands["catalog"]
        for name in self.REMEDIATION_COMMANDS:
            assert name in catalog_group.commands, f"{name} not registered"

    def test_remediation_commands_defined_in_carved_module(self):
        from nexus.cli import main
        catalog_group = main.commands["catalog"]
        for name in self.REMEDIATION_COMMANDS:
            assert catalog_group.commands[name].callback.__module__ == (
                "nexus.commands.catalog_cmds.remediation"
            ), f"{name} not in carved module"

    def test_shared_helpers_relocated_to_remediation_module(self):
        """The six private helpers moved out of commands.catalog into the
        carved module (and the test files that import them were repointed)."""
        import nexus.commands.catalog as cat_mod
        from nexus.commands.catalog_cmds import remediation as rem_mod
        for h in self.MOVED_HELPERS:
            assert hasattr(rem_mod, h), f"{h} missing from remediation module"
            assert not hasattr(cat_mod, h), f"{h} still in commands.catalog"

    def test_prune_stale_routes_get_catalog_through_module(self):
        """End-to-end: patching commands.catalog._get_catalog is observed by
        the carved prune-stale command — proves module-routed access."""
        from unittest.mock import MagicMock, patch

        from nexus.cli import main

        # NX_STORAGE_BACKEND stays pinned to sqlite (autouse fixture), so
        # _get_catalog() really returns a local Catalog reader here.
        cat = MagicMock(spec=Catalog)
        cat.all_documents.return_value = []
        cat.owners_with_roots.return_value = {}
        with patch("nexus.commands.catalog._get_catalog", return_value=cat), \
                patch(
                    "nexus.commands.catalog._get_catalog_writer",
                    return_value=MagicMock(spec=list(CATALOG_WRITE_OPS)),
                ):
            result = CliRunner().invoke(main, ["catalog", "prune-stale"])
        assert result.exit_code == 0, result.output
        assert "0 stale" in result.output
        cat.all_documents.assert_called()


class TestWhh61ReportCarve:
    """Contract pins for the nexus-whh61.4 report carve.

    Non-vacuous: fails on re-inline into ``commands.catalog``, a dropped
    ``register`` call, ``_taxonomy_stats`` not moving, or import-bound
    (non-module-routed) ``_get_catalog``.
    """

    REPORT_COMMANDS = ["stats", "orphans", "session-summary", "coverage"]

    def test_report_commands_registered_on_group(self):
        from nexus.cli import main
        catalog_group = main.commands["catalog"]
        for name in self.REPORT_COMMANDS:
            assert name in catalog_group.commands, f"{name} not registered"

    def test_report_commands_defined_in_carved_module(self):
        from nexus.cli import main
        catalog_group = main.commands["catalog"]
        for name in self.REPORT_COMMANDS:
            assert catalog_group.commands[name].callback.__module__ == (
                "nexus.commands.catalog_cmds.report"
            ), f"{name} not in carved module"

    def test_taxonomy_stats_relocated(self):
        import nexus.commands.catalog as cat_mod
        from nexus.commands.catalog_cmds import report as rep_mod
        assert hasattr(rep_mod, "_taxonomy_stats")
        assert not hasattr(cat_mod, "_taxonomy_stats")

    def test_stats_routes_get_catalog_through_module(self):
        """End-to-end: patching commands.catalog._get_catalog is observed by
        the carved stats command — proves module-routed access."""
        from unittest.mock import MagicMock, patch

        from nexus.cli import main

        # NX_STORAGE_BACKEND stays pinned to sqlite (autouse fixture), so
        # _get_catalog() really returns a local Catalog reader here.
        cat = MagicMock(spec=Catalog)
        cat.stats.return_value = {
            "owner_count": 3, "doc_count": 9, "link_count": 4, "chunk_count": 0,
            "by_content_type": {}, "links_by_type": {},
        }
        with patch("nexus.commands.catalog._get_catalog", return_value=cat), \
                patch("nexus.commands.catalog_cmds.report._taxonomy_stats", return_value=None):
            result = CliRunner().invoke(main, ["catalog", "stats"])
        assert result.exit_code == 0, result.output
        assert "Documents: 9" in result.output
        cat.stats.assert_called()


class TestWhh61IntegrityCarve:
    """Contract pins for the nexus-whh61.4 integrity carve.

    Non-vacuous: fails on re-inline, dropped ``register``, the four private
    helpers not moving, ``_make_t3`` wrongly moving (it is SHARED and must
    stay in commands.catalog), or import-bound ``_get_catalog``.
    """

    INTEGRITY_COMMANDS = ["audit-membership", "verify"]
    MOVED_HELPERS = [
        "_audit_membership_all", "_home_matches_root",
        "_source_uri_home_key", "_heal_ghosts",
        # module constants that moved with _source_uri_home_key:
        "_EMPTY_HOME_KEY", "_DEVONTHINK_HOME_KEY",
    ]

    def test_integrity_commands_registered_on_group(self):
        from nexus.cli import main
        catalog_group = main.commands["catalog"]
        for name in self.INTEGRITY_COMMANDS:
            assert name in catalog_group.commands, f"{name} not registered"

    def test_integrity_commands_defined_in_carved_module(self):
        from nexus.cli import main
        catalog_group = main.commands["catalog"]
        for name in self.INTEGRITY_COMMANDS:
            assert catalog_group.commands[name].callback.__module__ == (
                "nexus.commands.catalog_cmds.integrity"
            ), f"{name} not in carved module"

    def test_private_helpers_relocated_but_make_t3_stays(self):
        """The four exclusive helpers move; the SHARED _make_t3 stays in
        commands.catalog (verify routes to it via the module object)."""
        import nexus.commands.catalog as cat_mod
        from nexus.commands.catalog_cmds import integrity as integ_mod
        for h in self.MOVED_HELPERS:
            assert hasattr(integ_mod, h), f"{h} missing from integrity module"
            assert not hasattr(cat_mod, h), f"{h} still in commands.catalog"
        # _make_t3 is shared (setup/consolidate/backfill) — must NOT move.
        assert hasattr(cat_mod, "_make_t3")
        assert not hasattr(integ_mod, "_make_t3")

    def test_verify_routes_get_catalog_through_module(self):
        """End-to-end: patching commands.catalog._get_catalog is observed by
        the carved verify command — proves module-routed access. (The shared
        _make_t3 routing is pinned structurally above and exercised by the
        real verify suite, which reaches the t3 path with non-empty docs.)"""
        from unittest.mock import MagicMock, patch

        from nexus.cli import main

        # NX_STORAGE_BACKEND stays pinned to sqlite (autouse fixture), so
        # _get_catalog() really returns a local Catalog reader here.
        cat = MagicMock(spec=Catalog)
        cat.all_documents.return_value = []  # empty → clean early return
        with patch("nexus.commands.catalog._get_catalog", return_value=cat), \
                patch(
                    "nexus.commands.catalog._get_catalog_writer",
                    return_value=MagicMock(spec=list(CATALOG_WRITE_OPS)),
                ):
            result = CliRunner().invoke(main, ["catalog", "verify"])
        assert result.exit_code == 0, result.output
        cat.all_documents.assert_called()


class TestWhh61DoctorCarve:
    """Contract pins for the nexus-whh61.4 doctor carve (the final family).

    Non-vacuous: fails on re-inline, dropped ``register``, the diagnostic
    helpers not moving, or import-bound ``_get_catalog``.
    """

    DOCTOR_COMMANDS = ["doctor", "synthesize-log"]
    SAMPLE_MOVED_HELPERS = [
        "_run_replay_equality", "_snapshot_table", "_check_bootstrap_status",
        "_run_name_vs_embed_dim", "_percentile", "_run_t3_doc_id_coverage",
        "_run_collections_drift", "_run_chunk_size_distribution",
        "_run_chunk_text_dedup", "_run_t3_vs_catalog",
        "_print_replay_equality_text", "_expected_dim_for_model_token",
        # threshold constants moved with the helpers:
        "_MICRO_CHUNK_BYTES", "_VOYAGE_DIM", "_ORPHAN_RATIO_WARN_THRESHOLD",
    ]

    def test_prune_deprecated_keys_stayed_in_catalog(self):
        """_PRUNE_DEPRECATED_KEYS is an indexer/normalisation constant, NOT a
        diagnostic — it must stay in commands.catalog (indexer contract tests
        import it from there), not get swept into the doctor module."""
        import nexus.commands.catalog as cat_mod
        from nexus.commands.catalog_cmds import doctor as doc_mod
        assert hasattr(cat_mod, "_PRUNE_DEPRECATED_KEYS")
        assert not hasattr(doc_mod, "_PRUNE_DEPRECATED_KEYS")

    def test_doctor_commands_registered_on_group(self):
        from nexus.cli import main
        catalog_group = main.commands["catalog"]
        for name in self.DOCTOR_COMMANDS:
            assert name in catalog_group.commands, f"{name} not registered"

    def test_doctor_commands_defined_in_carved_module(self):
        from nexus.cli import main
        catalog_group = main.commands["catalog"]
        for name in self.DOCTOR_COMMANDS:
            assert catalog_group.commands[name].callback.__module__ == (
                "nexus.commands.catalog_cmds.doctor"
            ), f"{name} not in carved module"

    def test_diagnostic_helpers_relocated(self):
        import nexus.commands.catalog as cat_mod
        from nexus.commands.catalog_cmds import doctor as doc_mod
        for h in self.SAMPLE_MOVED_HELPERS:
            assert hasattr(doc_mod, h), f"{h} missing from doctor module"
            assert not hasattr(cat_mod, h), f"{h} still in commands.catalog"

    def test_doctor_requires_a_check_flag(self):
        """Behavioural: no flag → UsageError (exit 2), proving the carved
        command runs through its arg-validation path."""
        from nexus.cli import main
        result = CliRunner().invoke(main, ["catalog", "doctor"])
        assert result.exit_code == 2
        assert "Pass a check flag" in result.output


class TestWhh61OrphanBackfillCarve:
    """Contract pins for the nexus-whh61.4 orphan-backfill subgroup carve.

    Non-vacuous: fails on re-inline, dropped ``register``, ``_get_owner_for``
    not moving, or import-bound ``_get_catalog``.
    """

    SUBCOMMANDS = ["dt-link", "synthetic", "dump-csv", "apply-csv", "link-existing"]

    def test_orphan_backfill_group_registered(self):
        from nexus.cli import main
        catalog_group = main.commands["catalog"]
        assert "orphan-backfill" in catalog_group.commands
        ob = catalog_group.commands["orphan-backfill"]
        for sub in self.SUBCOMMANDS:
            assert sub in ob.commands, f"{sub} missing from orphan-backfill group"

    def test_orphan_backfill_defined_in_carved_module(self):
        from nexus.cli import main
        ob = main.commands["catalog"].commands["orphan-backfill"]
        assert ob.callback.__module__ == "nexus.commands.catalog_cmds.orphan_backfill"
        for sub in self.SUBCOMMANDS:
            assert ob.commands[sub].callback.__module__ == (
                "nexus.commands.catalog_cmds.orphan_backfill"
            ), f"{sub} not in carved module"

    def test_get_owner_for_relocated(self):
        """_get_owner_for is orphan-backfill-exclusive and moved; the SHARED
        _make_t3 / _make_registry / _backfill_repos stayed (used by setup)."""
        import nexus.commands.catalog as cat_mod
        from nexus.commands.catalog_cmds import orphan_backfill as ob_mod
        assert hasattr(ob_mod, "_get_owner_for")
        assert not hasattr(cat_mod, "_get_owner_for")
        for shared in ("_make_t3", "_make_registry", "_backfill_repos"):
            assert hasattr(cat_mod, shared), f"{shared} must stay in catalog (shared w/ setup)"

    def test_orphan_backfill_subgroup_resolves_through_main_group(self):
        """The carved subgroup is reachable as ``nx catalog orphan-backfill``
        and its help lists every subcommand — proves the add_command wiring.
        (Module-routed _get_catalog access is exercised end-to-end by
        test_orphan_backfill.py.)"""
        from nexus.cli import main

        result = CliRunner().invoke(main, ["catalog", "orphan-backfill", "--help"])
        assert result.exit_code == 0
        for sub in self.SUBCOMMANDS:
            assert sub in result.output, f"{sub} not listed in group help"
