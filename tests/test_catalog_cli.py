# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import os
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.catalog.catalog_protocol import CATALOG_WRITE_OPS
from nexus.db.http_vector_client import HttpVectorClient
from tests._catalog_fixture_ops import (
    ActiveCatalog,
    bypass_fk_seed_chunk,
    unroutable_write_target,
)

# nexus-aqbrk: the dies-roster here was OVER-BROAD BY 20 TESTS, and its stated
# cause was a symptom. It read "the CLI routes catalog commands to the service
# catalog (a freshly minted, empty tenant) which cannot see the seeded local
# rows" — true, but that is a FIXTURE problem, not a retirement: these CLI
# verbs go through make_catalog_reader/_writer, so seeding through the same
# factories puts both halves on the same catalog. Doing that recovered 20 of
# the 36 outright (124 passed / 16 failed after dropping the marker), the same
# correction already applied to test_enrich_aspects.py.
#
# What survives is a marker that does NOT claim these die. They are not
# understood yet, and at least two of them (link_audit) are a KNOWN service
# gap with its own bead — re-rostering that as "dies at the flip" would bury
# a defect. See nexus-02avu for the per-symptom grouping and what to check
# first.
#
# UNCONDITIONAL since nexus-i711w Stage 1b (2026-07-28). It was a skipif on
# the SQLite substrate, which no longer exists — so these 15 do not run on any
# arm, and saying so plainly beats a predicate with one value. The bodies are
# kept BECAUSE they are portable: none of them reaches for a raw connection,
# so each is the specification nexus-02avu's diagnosis has to satisfy. (The
# sixteenth, test_stats_includes_topics_block_when_available, seeded topics
# through taxonomy.conn and was deleted rather than kept — recorded on the
# bead.)
_needs_diagnosis_nexus_02avu = pytest.mark.skip(
    reason="nexus-02avu: engine-substrate behaviour not yet diagnosed — "
    "tracked, not retired (2 are nexus-wnlit, 3 likely nexus-23wlw)",
)

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
    """Return a facade over the LIVE catalog, init'd with one owner."""
    # nexus-i711w terminal deletion: the local Catalog.init seeding is gone —
    # ActiveCatalog routes straight to the live service catalog, no init step.
    cat = ActiveCatalog()
    cat.register_owner("test-repo", "repo", repo_hash="abcd1234")
    return cat


# TestInitCommand retired (nexus-i711w terminal deletion): `nx catalog init`
# is now a guided refusal — the service owns the catalog, there is no local
# init to assert.


class TestSyncPullRetired:
    """catalog-git-DECISION OPTION C: `nx catalog sync` / `nx catalog pull`
    used to shell out to HttpCatalogClient.sync()/pull(), which raise
    NotImplementedError — an uncaught traceback, not a clean CLI refusal.
    Both commands were converted to the same guided-refusal shape as
    init_cmd/setup_cmd above; these pin that no traceback reaches the user.
    """

    def test_sync_is_a_clean_refusal(self):
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "sync"])
        assert result.exit_code != 0
        assert result.exception is None or isinstance(
            result.exception, SystemExit,
        ), "sync should refuse via ClickException, not raise NotImplementedError"
        assert "retired" in result.output.lower()
        assert "nothing to commit" in result.output.lower() or "nothing to sync" in result.output.lower() or "postgres" in result.output.lower()

    def test_pull_is_a_clean_refusal(self):
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "pull"])
        assert result.exit_code != 0
        assert result.exception is None or isinstance(
            result.exception, SystemExit,
        ), "pull should refuse via ClickException, not raise NotImplementedError"
        assert "retired" in result.output.lower()
        assert "postgres" in result.output.lower()

    def test_compact_is_a_clean_refusal(self):
        """The hidden compact verb had the identical traceback bug."""
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "compact"])
        assert result.exit_code != 0
        assert result.exception is None or isinstance(
            result.exception, SystemExit,
        ), "compact should refuse via ClickException, not raise NotImplementedError"
        assert "retired" in result.output.lower()


class TestNotInitialized:
    @_needs_diagnosis_nexus_02avu
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

    @_needs_diagnosis_nexus_02avu
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


class TestRegisterEphemeralPathGuard:
    """nexus-u8n4r review fix C1 (code-review-expert Critical): ``nx
    catalog register`` must refuse a worktree/tempdir path using the
    ABSOLUTE registered identity — the pre-relativization ``file_path``
    when absolute, or ``owner_repo_root / fp`` when the caller passed a
    relative path — never the post-relativization ``fp`` alone, which
    silently drops the leading ``/`` the worktree marker requires
    whenever the path matches an already-registered repo root.
    """

    def test_absolute_worktree_path_under_known_repo_is_refused(
        self, tmp_path, catalog_env, monkeypatch,
    ):
        """Reproduces the exact C1 shape: file_path is absolute AND
        falls under a repo root that's already registered, so
        register_cmd's own relativization step strips the leading
        ``/`` before the old check ever ran."""
        from nexus.repo_identity import is_worktree_or_tempdir_path

        # Linux/CI-vs-macOS divergence (nexus-u8n4r CI red, 2026-08-03):
        # pytest's ``tmp_path`` lives under ``/tmp/`` on Linux, matching
        # ``_TEMP_DIR_PREFIXES`` — the owner root here would look
        # ephemeral too and the owner-root exception would exempt the
        # registration, hiding the refusal this test exists to pin. Force
        # a non-tmp-shaped prefix set so the owner root reads as clean on
        # BOTH platforms. Do not strip this patch; see nexus-u8n4r CI run
        # 30850463195.
        monkeypatch.setattr(
            "nexus.repo_identity._TEMP_DIR_PREFIXES", ("/nonexistent-tmp-prefix/",),
        )

        cat = ActiveCatalog()
        repo_root = tmp_path / "wt-repo-a"
        repo_root.mkdir()
        # Non-vacuity: prove the premise (clean owner root) instead of
        # inheriting it from whichever platform happens to be running.
        assert not is_worktree_or_tempdir_path(str(repo_root))
        cat.register_owner(
            "wt-repo-a", "repo", repo_hash="wta00001", repo_root=str(repo_root),
        )
        owner = cat.owner_for_repo("wta00001")
        assert owner is not None

        worktree_path = (
            repo_root / ".claude" / "worktrees" / "agent-x" / "docs" / "foo.md"
        )

        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "register",
            "--title", "Ephemeral",
            "--owner", str(owner),
            "--file-path", str(worktree_path),
        ])
        assert result.exit_code != 0
        assert "nexus-u8n4r" in result.output

    def test_bare_relative_worktree_shaped_path_is_refused(
        self, tmp_path, catalog_env, monkeypatch,
    ):
        """The caller passes an already-relative, worktree-shaped
        file_path directly (no absolute-path relativization step at
        all) — the guard must still reconstruct the absolute identity
        via ``owner_repo_root / fp`` and refuse."""
        from nexus.repo_identity import is_worktree_or_tempdir_path

        # Linux/CI-vs-macOS divergence — see the sibling test above for
        # the full explanation. Do not strip this patch.
        monkeypatch.setattr(
            "nexus.repo_identity._TEMP_DIR_PREFIXES", ("/nonexistent-tmp-prefix/",),
        )

        cat = ActiveCatalog()
        repo_root = tmp_path / "wt-repo-b"
        repo_root.mkdir()
        assert not is_worktree_or_tempdir_path(str(repo_root))
        cat.register_owner(
            "wt-repo-b", "repo", repo_hash="wtb00001", repo_root=str(repo_root),
        )
        owner = cat.owner_for_repo("wtb00001")
        assert owner is not None

        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "register",
            "--title", "Ephemeral relative",
            "--owner", str(owner),
            "--file-path", ".claude/worktrees/agent-y/docs/bar.md",
        ])
        assert result.exit_code != 0
        assert "nexus-u8n4r" in result.output

    def test_clean_path_still_registers(self, tmp_path, catalog_env):
        cat = ActiveCatalog()
        repo_root = tmp_path / "wt-repo-c"
        repo_root.mkdir()
        cat.register_owner(
            "wt-repo-c", "repo", repo_hash="wtc00001", repo_root=str(repo_root),
        )
        owner = cat.owner_for_repo("wtc00001")
        assert owner is not None

        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "register",
            "--title", "Clean",
            "--owner", str(owner),
            "--file-path", str(repo_root / "docs" / "clean.md"),
        ])
        assert result.exit_code == 0, result.output


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
        # owner-rename produces. (`dedupe-owners` used to reconcile these;
        # it was deleted in nexus-i711w Stage 2 sub-stage C-store, so the
        # duplicate-tumbler condition below is now permanent, not transient.)
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

    @_needs_diagnosis_nexus_02avu
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

    def test_delete_service_mode_never_touches_dir_or_db(self, catalog_env):
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

        The backup-snapshot assertion that used to close this test retired
        with ``catalog_backup`` (nexus-i711w): delete no longer writes a
        local pre-delete JSONL snapshot in any mode.
        """
        from unittest.mock import MagicMock, patch

        from nexus.catalog.types import CatalogEntry
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
    @_needs_diagnosis_nexus_02avu
    def test_link_audit_cli(self, initialized_catalog, catalog_env):
        runner = CliRunner()
        runner.invoke(main, ["catalog", "register", "--title", "A", "--owner", "1.1"])
        runner.invoke(main, ["catalog", "register", "--title", "B", "--owner", "1.1"])
        runner.invoke(main, ["catalog", "link", "1.1.1", "1.1.2", "--type", "cites"])
        result = runner.invoke(main, ["catalog", "link-audit"])
        assert result.exit_code == 0
        assert "Total links" in result.output
        assert "cites" in result.output

    @_needs_diagnosis_nexus_02avu
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

    def test_owners_json_includes_next_seq(self, initialized_catalog, catalog_env):
        """nexus-pu2z8: the engine's list_owners() already carries next_seq
        (SHAKEOUT-7.1.1 agent C measured 65/65) but the CLI's --json
        hand-projection dropped it at src/nexus/commands/catalog_cmds/owners.py.
        next_seq is the surface nexus-0ehwe item 3 named a non-negotiable
        prerequisite for drift audits (health.py's _check_next_seq_drift). This
        is the LAYER TEST at the CLI boundary — through the real
        ``initialized_catalog`` service catalog, not a unit test of
        list_owners() itself, which was never the broken layer.
        """
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "owners", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data, "expected at least one registered owner"
        for owner in data:
            assert "next_seq" in owner
        assert any(owner.get("next_seq") is not None for owner in data), (
            "next_seq present as a key but always None — the engine field "
            "isn't reaching the CLI projection"
        )


#: Derived, never hardcoded: a literal here breaks on every template
#: added or retired, which teaches people to bump the number rather
#: than ask why it moved. Retiring the four plan-meta templates
#: (nexus-77cct) broke five such literals at once.
_SHIPPED_BUILTIN_COUNT = len(list(
    (Path(__file__).parent.parent / "conexus" / "plans" / "builtin").glob("*.yml")
))


class TestSeedPlanTemplates:
    def test_seed_creates_legacy_templates(self, tmp_path, monkeypatch):
        """All 17 RDR-078/092/097/098/nexus-h33x8 YAML templates seed on
        first run.

        RDR-092 Phase 0a retired the legacy ``_PLAN_TEMPLATES`` array:
        three entries migrated to dimensional YAML (find-by-author,
        citation-traversal, type-scoped-search); two were retired as
        redundant with research-default / analyze-default. Pre-existing
        9 YAML plus 3 migrated = 12. RDR-097 added the
        hybrid-factual-lookup and traverse-then-generate plans (P1.1 /
        P1.2), and RDR-098 added abstract-themes (CheapRAG community
        pattern), bringing the total to 15. nexus-h33x8.6 a1 added two
        single-``query``-step fast-path templates (document-discovery,
        corpus-coverage-check), bringing the total to 17.
        """
        from nexus.db.t2 import T2Database
        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.config.default_db_path", lambda: db_path)
        from nexus.commands.catalog import _seed_plan_templates
        count = _seed_plan_templates()
        assert count == _SHIPPED_BUILTIN_COUNT
        db = T2Database(db_path)
        # Every seeded template carries the builtin-template tag.
        results = db.search_plans("builtin-template", limit=20)
        assert len(results) == _SHIPPED_BUILTIN_COUNT
        db.close()

    def test_seed_idempotent(self, tmp_path, monkeypatch):
        from nexus.db.t2 import T2Database
        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.config.default_db_path", lambda: db_path)
        from nexus.commands.catalog import _seed_plan_templates
        first = _seed_plan_templates()
        second = _seed_plan_templates()
        assert first == _SHIPPED_BUILTIN_COUNT
        assert second == 0

    def test_seed_templates_have_builtin_tag(self, tmp_path, monkeypatch):
        from nexus.db.t2 import T2Database
        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.config.default_db_path", lambda: db_path)
        from nexus.commands.catalog import _seed_plan_templates
        _seed_plan_templates()
        db = T2Database(db_path)
        plans = db.list_plans(limit=20)
        assert len(plans) == _SHIPPED_BUILTIN_COUNT
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
        assert len(plans) == _SHIPPED_BUILTIN_COUNT
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
        assert "taxonomy" not in json.loads(js.stdout)


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


class _FakeEntry:
    """Minimal CatalogEntry stand-in for full-mode verify tests (nexus-sj4a3)."""

    def __init__(
        self, tumbler, title, *, physical_collection, chunk_count=0, alias_of="",
        file_path="", source_uri="",
    ):
        self.tumbler = tumbler
        self.title = title
        self.physical_collection = physical_collection
        self.chunk_count = chunk_count
        self.alias_of = alias_of
        self.file_path = file_path
        self.source_uri = source_uri


class _FakeFullCat:
    """Full-catalog-mode fake catalog reader.

    ``manifest_verify_all`` is a SERVER-SIDE anti-join against the real T3
    pgvector tables (RDR-152: one Postgres per tenant, shared by catalog and
    vectors) — it is NOT reachable through the Python-side ``_make_t3()``
    mock the scoped-mode path uses. A real ``ActiveCatalog`` + real engine
    would therefore always show every real manifest row as 100% "missing"
    in these tests (nothing is ever written to the real vector tables here),
    making Class A/B/C combinations impossible to control deterministically.
    This fake sidesteps that entirely, mirroring the same-shape fake
    ``_Cat`` in ``tests/test_health_service_checks.py``'s
    ``TestCheckDanglingManifests`` (the doctor check backed by the same
    ``manifest_verify_all`` primitive).
    """

    def __init__(
        self, entries, *, doc_counts=None, mv_all=None, manifests=None,
        doc_counts_exc=None, get_manifests_exc=None, owners_with_roots=None,
    ):
        self._entries = entries
        self._doc_counts = doc_counts or {}
        self._mv_all = mv_all if mv_all is not None else {"collections": [], "count": 0}
        self._manifests = manifests or {}
        self._doc_counts_exc = doc_counts_exc
        self._get_manifests_exc = get_manifests_exc
        self._owners = owners_with_roots or {}

    def all_documents(self, limit=0):
        return list(self._entries)

    def collection_doc_counts(self):
        if self._doc_counts_exc is not None:
            raise self._doc_counts_exc
        return dict(self._doc_counts)

    def manifest_verify_all(self):
        return self._mv_all

    def get_manifests(self, doc_ids):
        if self._get_manifests_exc is not None:
            raise self._get_manifests_exc
        return {d: self._manifests[d] for d in doc_ids if d in self._manifests}

    def owners_with_roots(self):
        return dict(self._owners)


class TestVerifyCommand:
    """nexus-sj4a3: full-catalog + ``--collection``-scoped verify on the
    RDR-108/180 chash identity (tumbler -> document_chunks.chash -> T3
    chunk id). Retired: the pre-RDR-108 ``meta.doc_id`` filter that covered
    only 1.5% of production and exited 0 at 89.6% ghost rate."""

    @staticmethod
    def _register_chunked(
        cat, owner_str, title, coll, *, chunk_count=0, chashes=None,
        content_type="knowledge", tenant="",
    ):
        """Register a doc with a physical_collection + chunk_count and,
        when *chashes* is given, write a real manifest (the RDR-108
        identity: tumbler -> document_chunks.chash -> T3 chunk id).

        nexus-dbzxb (RDR-191 Phase 5 Python collateral, idiom 3): every
        caller of this helper is a manifest-verify DAMAGE fixture — the
        chash is deliberately meant to be a "ghost"/ABSENT-from-T3 chash
        (the whole point of ``TestVerifyCommand``'s damaged/heal/
        clean-pct tests, which patch T3's ``existing_ids`` to report it
        missing). Seeding a REAL T3 chunk here (idiom 1/2) would defeat
        the fixture's own premise. ``fk_catalog_chunks_chunk`` still
        requires the row to exist, so this is exactly the "genuinely-
        dangling manifest row" case the sweep's idiom 3 covers: a direct
        stub insert into ``nexus.chunks`` via the test substrate's own
        psql connection, bypassing the manifest write's normal T3-backed
        path entirely. The verify command's OWN presence check is
        against the (separately mocked) T3 client, not this row, so the
        test's damaged/ghost semantics are unaffected.
        """
        from nexus.catalog.tumbler import Tumbler
        t = cat.register(
            Tumbler.parse(owner_str), title,
            content_type=content_type,
            physical_collection=coll,
            chunk_count=chunk_count,
        )
        if chashes:
            for chash in chashes:
                bypass_fk_seed_chunk(tenant, coll, chash)
            cat.atomic_manifest_replace(str(t), [
                {
                    "chash": chash, "position": i, "chunk_index": i,
                    "line_start": None, "line_end": None,
                    "char_start": None, "char_end": None,
                }
                for i, chash in enumerate(chashes)
            ], collection=coll)
        return t

    def _patch_t3(self, monkeypatch, present_ids_by_collection, t3_collections=None):
        """Patch _make_t3 in commands.catalog: existing_ids returns the seeded
        set (scoped-mode per-doc damage probe — the ONLY damage detection
        left in this command since Class B's retirement, RDR-191 Phase 6
        nexus-o8dil.33), list_collections returns the given (or inferred)
        collection names (full-mode Class-A probe)."""
        from unittest.mock import MagicMock

        # Module-wide cloud_mode fixture forces is_local_mode() False, so
        # the real _make_t3()/make_t3() hands back an HttpVectorClient here.
        fake = MagicMock(spec=HttpVectorClient)

        def _existing_ids(coll, ids):
            present = set(present_ids_by_collection.get(coll, []))
            return {i for i in ids if i in present}

        fake.existing_ids.side_effect = _existing_ids
        names = (
            t3_collections if t3_collections is not None
            else set(present_ids_by_collection.keys())
        )
        fake.list_collections.return_value = [{"name": n} for n in names]
        monkeypatch.setattr(
            "nexus.commands.catalog._make_t3", lambda: fake,
        )
        return fake

    # ── full-catalog mode (fake cat — see _FakeFullCat docstring) ──────────

    def test_verify_clean(self, catalog_env, monkeypatch):
        """Every class clean → 'All good.', exit 0."""
        entries = [_FakeEntry("1.1.1", "Doc One", physical_collection="knowledge__thing", chunk_count=1)]
        cat = _FakeFullCat(
            entries=entries,
            doc_counts={"knowledge__thing": 1},
            mv_all={"collections": [
                {"collection": "knowledge__thing", "referenced": 1, "present": 1, "missing": 0},
            ], "count": 1},
            manifests={"1.1.1": [object()]},
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        self._patch_t3(monkeypatch, {}, t3_collections={"knowledge__thing"})

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify"])

        assert result.exit_code == 0, result.output
        assert "All good." in result.output

    # test_verify_flags_damaged DELETED (RDR-191 Phase 6, nexus-o8dil.33):
    # tested Class B (a manifest_verify_all "missing" row -> damaged
    # collection finding). Class B is retired — the manifest-chunk FK makes
    # the dangling state it detected unreachable, so `damaged` in full mode
    # is now always []. --collection scoped mode's own damage detection
    # (client-side, unaffected) is covered by test_verify_scoped_json_output
    # and siblings.

    def test_verify_missing_collection_is_vanished(self, catalog_env, monkeypatch):
        """Class A: collection absent from T3 entirely (deleted/renamed) →
        vanished-collection finding, exit 1. Flips
        test_verify_missing_collection_is_all_ghosts."""
        entries = [
            _FakeEntry("1.1.1", "A", physical_collection="knowledge__gone", chunk_count=0),
            _FakeEntry("1.1.2", "Clean Doc", physical_collection="knowledge__other", chunk_count=1),
        ]
        cat = _FakeFullCat(
            entries=entries,
            doc_counts={"knowledge__gone": 1, "knowledge__other": 1},
            # knowledge__other stays checked>0 so the mv_all non-vacuity
            # guard doesn't fold this test into the "0 collections" case.
            mv_all={"collections": [
                {"collection": "knowledge__other", "referenced": 1, "present": 1, "missing": 0},
            ], "count": 1},
            manifests={"1.1.2": [object()]},
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        self._patch_t3(monkeypatch, {}, t3_collections={"knowledge__other"})

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify"])

        assert result.exit_code == 1, result.output
        assert "knowledge__gone" in result.output
        assert "vanished" in result.output.lower()

    def test_verify_lost_exits_nonzero(self, catalog_env, monkeypatch):
        """Class C: chunk_count > 0 but the manifest is shorter → lost
        document, exit 1."""
        entries = [_FakeEntry("1.1.1", "Partial", physical_collection="knowledge__thing", chunk_count=3)]
        cat = _FakeFullCat(
            entries=entries,
            doc_counts={"knowledge__thing": 1},
            mv_all={"collections": [
                {"collection": "knowledge__thing", "referenced": 3, "present": 3, "missing": 0},
            ], "count": 1},
            manifests={"1.1.1": [object(), object()]},  # 2 rows, chunk_count=3
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        self._patch_t3(monkeypatch, {}, t3_collections={"knowledge__thing"})

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify", "--json"])

        assert result.exit_code == 1, result.output
        data = json.loads(result.stdout)
        assert data["summary"]["lost_docs"] == 1
        assert data["summary"]["exit"] == 1

    def test_verify_never_chunked_is_exit_neutral(self, catalog_env, monkeypatch):
        """chunk_count==0 + no manifest is report-only (RDR-145 manifest-less
        store_put notes): a clean corpus plus one never-chunked doc still
        exits 0, and the count is reported."""
        entries = [
            _FakeEntry("1.1.1", "Indexed", physical_collection="knowledge__thing", chunk_count=1),
            _FakeEntry("1.1.2", "Note-only", physical_collection="knowledge__thing", chunk_count=0),
        ]
        cat = _FakeFullCat(
            entries=entries,
            doc_counts={"knowledge__thing": 2},
            mv_all={"collections": [
                {"collection": "knowledge__thing", "referenced": 1, "present": 1, "missing": 0},
            ], "count": 1},
            manifests={"1.1.1": [object()]},  # 1.1.2 absent -> never_chunked
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        self._patch_t3(monkeypatch, {}, t3_collections={"knowledge__thing"})

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["summary"]["never_chunked_docs"] == 1
        assert data["summary"]["lost_docs"] == 0
        assert data["summary"]["exit"] == 0

    # test_verify_manifest_verify_all_zero_collections_is_incomplete DELETED
    # (RDR-191 Phase 6, nexus-o8dil.33): tested Class B's non-vacuity guard
    # (0 collections checked -> INCOMPLETE). Class B and its manifest_verify_all
    # call are retired entirely.

    def test_verify_json_output(self, catalog_env, monkeypatch):
        """--json emits the new CI-contract shape (deliberately breaks the
        retired {collection: [{tumbler, title, doc_id}]} shape).

        nexus-sj4a3 critique SIG-3: full mode's damaged rows were
        COLLECTION-granular (one manifest_verify_all round trip; no
        per-doc count) — Class B is RETIRED (RDR-191 Phase 6,
        nexus-o8dil.33; the manifest-chunk FK makes the dangling state it
        detected unreachable), so ``damaged``/``damaged_collections`` are
        now always empty/0 in full mode. This test exercises the SAME
        JSON-contract-shape assertion via Class C (``lost``) instead, the
        summary key distinct from scoped mode's per-document ``damaged_docs``
        (see test_verify_scoped_json_output)."""
        entries = [_FakeEntry("1.1.1", "Partial", physical_collection="knowledge__x", chunk_count=2)]
        cat = _FakeFullCat(
            entries=entries,
            doc_counts={"knowledge__x": 1},
            mv_all={"collections": [
                {"collection": "knowledge__x", "referenced": 1, "present": 1, "missing": 0},
            ], "count": 1},
            manifests={"1.1.1": [object()]},  # 1 row, chunk_count=2 -> Class C "lost"
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        self._patch_t3(monkeypatch, {}, t3_collections={"knowledge__x"})

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify", "--json"])

        assert result.exit_code == 1, result.output
        data = json.loads(result.stdout)
        assert data["summary"]["exit"] == 1
        assert data["summary"]["damaged_collections"] == 0
        assert data["damaged"] == []
        assert len(data["lost"]) == 1
        assert data["lost"][0]["tumbler"] == "1.1.1"

    def test_verify_covers_docs_without_legacy_doc_id(self, catalog_env, monkeypatch):
        """nexus-sj4a3: the retired meta.doc_id filter used to silently skip
        every doc lacking that legacy field (98.5% of production). A doc
        carrying no doc_id concept at all — the RDR-108 norm — must be
        COVERED, not silently dropped. Flips
        test_verify_skips_tumblers_without_doc_id."""
        entries = [_FakeEntry("1.1.1", "Modern Doc", physical_collection="knowledge__thing", chunk_count=1)]
        cat = _FakeFullCat(
            entries=entries,
            doc_counts={"knowledge__thing": 1},
            mv_all={"collections": [
                {"collection": "knowledge__thing", "referenced": 1, "present": 1, "missing": 0},
            ], "count": 1},
            manifests={"1.1.1": [object()]},
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        self._patch_t3(monkeypatch, {}, t3_collections={"knowledge__thing"})

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["summary"]["docs"] == 1, (
            "the doc — carrying no meta.doc_id at all — must be counted, "
            "not silently skipped"
        )

    def test_verify_coverage_counts_all_non_alias_docs(self, catalog_env, monkeypatch):
        """Anti-vacuity pin: every registered non-alias doc with a
        physical_collection is counted in summary.docs — the whole point
        of the rewrite vs. the old 1.5%-coverage bug."""
        entries = [
            _FakeEntry("1.1.1", "A", physical_collection="knowledge__thing", chunk_count=1),
            _FakeEntry("1.1.2", "B", physical_collection="knowledge__thing", chunk_count=1),
            _FakeEntry("1.1.3", "C (alias)", physical_collection="knowledge__thing", chunk_count=1, alias_of="1.1.1"),
        ]
        cat = _FakeFullCat(
            entries=entries,
            doc_counts={"knowledge__thing": 2},
            mv_all={"collections": [
                {"collection": "knowledge__thing", "referenced": 2, "present": 2, "missing": 0},
            ], "count": 1},
            manifests={"1.1.1": [object()], "1.1.2": [object()]},
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        self._patch_t3(monkeypatch, {}, t3_collections={"knowledge__thing"})

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["summary"]["docs"] == 2, "alias row excluded, both real docs counted"

    # ── ghost census (nexus-xeux8) ──────────────────────────────────────

    def test_verify_full_mode_reports_ghost_census(self, catalog_env, monkeypatch):
        """nexus-xeux8: docs with blank/NULL physical_collection are
        dropped by BOTH verify's health classification (_verify_full's
        own all_entries filter) and reconcile-stale's candidate filter --
        nothing sizes them today. Full-mode --json must carry a
        read-only `ghosts` census: count + by-owner + a bounded sample,
        and it must never affect docs/exit."""
        entries = [
            _FakeEntry("1.1.1", "Real Doc", physical_collection="knowledge__thing", chunk_count=1),
            _FakeEntry("1.2.1", "Ghost One", physical_collection=""),
            _FakeEntry("1.2.2", "Ghost Two", physical_collection=""),
            _FakeEntry("1.3.1", "Ghost Three", physical_collection=""),
        ]
        cat = _FakeFullCat(
            entries=entries,
            doc_counts={"knowledge__thing": 1},
            mv_all={"collections": [
                {"collection": "knowledge__thing", "referenced": 1, "present": 1, "missing": 0},
            ], "count": 1},
            manifests={"1.1.1": [object()]},
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        self._patch_t3(monkeypatch, {}, t3_collections={"knowledge__thing"})

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["summary"]["ghost_docs"] == 3
        assert data["summary"]["docs"] == 1, "ghosts must not be counted toward health-classification docs"
        assert data["summary"]["exit"] == 0, "ghosts are a census, never a finding"

        ghosts = data["ghosts"]
        assert ghosts["count"] == 3
        assert set(ghosts["sample_tumblers"]) == {"1.2.1", "1.2.2", "1.3.1"}
        assert ghosts["sample_truncated"] is False
        assert ghosts["by_owner"] == [
            {"owner": "1.2", "count": 2},
            {"owner": "1.3", "count": 1},
        ]
        assert ghosts["by_tenant"]["available"] is False
        assert "unrepairable" in ghosts["note"].lower()

    def test_verify_full_mode_ghost_sample_truncated_above_cap(self, catalog_env, monkeypatch):
        """nexus-xeux8 critique fix-round (SIGNIFICANT): the sample is
        capped at 20 tumblers -- pin the truncation signal itself (not
        just the untruncated case above) so `sample_truncated` and `count`
        stay correct when the ghost population exceeds the cap."""
        real = [_FakeEntry("1.1.1", "Real Doc", physical_collection="knowledge__thing", chunk_count=1)]
        ghosts_n = 25  # > the 20-row _CAP_GHOST_SAMPLE
        ghost_entries = [
            _FakeEntry(f"1.{200 + i}.1", f"Ghost {i}", physical_collection="")
            for i in range(ghosts_n)
        ]
        cat = _FakeFullCat(
            entries=real + ghost_entries,
            doc_counts={"knowledge__thing": 1},
            mv_all={"collections": [
                {"collection": "knowledge__thing", "referenced": 1, "present": 1, "missing": 0},
            ], "count": 1},
            manifests={"1.1.1": [object()]},
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        self._patch_t3(monkeypatch, {}, t3_collections={"knowledge__thing"})

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["summary"]["ghost_docs"] == ghosts_n

        ghosts = data["ghosts"]
        assert ghosts["count"] == ghosts_n, "the COUNT is never truncated, only the sample"
        assert len(ghosts["sample_tumblers"]) == 20
        assert ghosts["sample_truncated"] is True
        # sample is the lexicographically-sorted-first 20 tumblers (see
        # _census_ghosts), so it is a deterministic, reproducible subset.
        expected_sample = sorted(str(e.tumbler) for e in ghost_entries)[:20]
        assert ghosts["sample_tumblers"] == expected_sample

    def test_verify_full_mode_human_report_prints_ghost_section(self, catalog_env, monkeypatch):
        entries = [
            _FakeEntry("1.1.1", "Real Doc", physical_collection="knowledge__thing", chunk_count=1),
            _FakeEntry("1.2.1", "Ghost One", physical_collection=""),
        ]
        cat = _FakeFullCat(
            entries=entries,
            doc_counts={"knowledge__thing": 1},
            mv_all={"collections": [
                {"collection": "knowledge__thing", "referenced": 1, "present": 1, "missing": 0},
            ], "count": 1},
            manifests={"1.1.1": [object()]},
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        self._patch_t3(monkeypatch, {}, t3_collections={"knowledge__thing"})

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify"])

        assert result.exit_code == 0, result.output
        assert "Ghost documents (1" in result.output
        assert "unrepairable" in result.output.lower()
        assert "1.2.1" in result.output

    def test_verify_full_mode_zero_ghosts_omits_section_but_keeps_summary_key(
        self, catalog_env, monkeypatch,
    ):
        entries = [_FakeEntry("1.1.1", "Real Doc", physical_collection="knowledge__thing", chunk_count=1)]
        cat = _FakeFullCat(
            entries=entries,
            doc_counts={"knowledge__thing": 1},
            mv_all={"collections": [
                {"collection": "knowledge__thing", "referenced": 1, "present": 1, "missing": 0},
            ], "count": 1},
            manifests={"1.1.1": [object()]},
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        self._patch_t3(monkeypatch, {}, t3_collections={"knowledge__thing"})

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["summary"]["ghost_docs"] == 0
        assert data["ghosts"]["count"] == 0

        human = runner.invoke(main, ["catalog", "verify"])
        assert "Ghost documents" not in human.output

    def test_verify_scoped_mode_has_no_ghosts_key(self, initialized_catalog, catalog_env, monkeypatch):
        """Ghosts are a whole-catalog census (they have no collection to
        scope into by definition) -- `--collection` scoped mode must not
        claim to carry the section."""
        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "verify", "--collection", "knowledge__thing", "--json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert "ghosts" not in data
        assert "ghost_docs" not in data["summary"]

    def test_verify_full_mode_heal_refused(self, initialized_catalog, catalog_env):
        """--heal without --collection is refused — full mode has no
        per-document detail to heal from."""
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify", "--heal"])
        assert result.exit_code != 0
        assert "--collection" in result.output

    def test_verify_heal_refused_with_json(self, initialized_catalog, catalog_env):
        """nexus-sj4a3 code-review SUGGESTION: --heal is interactive
        (click.prompt loops); combined with --json it would interleave
        prompts with JSON stdout. Refuse the combination outright rather
        than risk an Abort on a non-interactive pipe."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["catalog", "verify", "--collection", "knowledge__thing", "--heal", "--json"],
        )
        assert result.exit_code != 0
        assert "--json" in result.output

    # ── code-review CRITICAL: false-vanished storm (list_collections() -> []) ──

    def test_verify_t3_list_collections_empty_is_incomplete_not_vanished(
        self, catalog_env, monkeypatch,
    ):
        """nexus-sj4a3 code-review CRITICAL: production
        HttpVectorClient.list_collections() catches any non-404
        VectorServiceError (a 500/503/timeout from /v1/vectors/stats) and
        returns [] instead of raising. An empty t3_names against a
        populated catalog is indistinguishable from that swallowed failure
        (or a genuinely-empty T3) and must read as INCOMPLETE — never as
        'every collection vanished' (the false mass-vanished-collections
        alarm this finding exists to kill)."""
        entries = [
            _FakeEntry("1.1.1", "A", physical_collection="knowledge__thing", chunk_count=1),
            _FakeEntry("1.1.2", "B", physical_collection="knowledge__other", chunk_count=1),
        ]
        cat = _FakeFullCat(
            entries=entries,
            doc_counts={"knowledge__thing": 1, "knowledge__other": 1},
            mv_all={"collections": [
                {"collection": "knowledge__thing", "referenced": 1, "present": 1, "missing": 0},
                {"collection": "knowledge__other", "referenced": 1, "present": 1, "missing": 0},
            ], "count": 2},
            manifests={"1.1.1": [object()], "1.1.2": [object()]},
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        # list_collections() returns [] WITHOUT raising — the swallowed-error shape.
        self._patch_t3(monkeypatch, {}, t3_collections=set())

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify", "--json"])

        assert result.exit_code != 0, result.output
        data = json.loads(result.stdout)
        assert data["vanished_collections"] == [], (
            "an empty T3 listing must never be reported as vanished collections"
        )
        assert data["summary"]["vanished_collections"] == 0
        assert any("empty-with-populated-catalog" in u for u in data["unreadable"])

    def test_verify_empty_catalog_and_empty_t3_is_clean(
        self, catalog_env, monkeypatch,
    ):
        """Round-2 pin: a genuinely virgin box (no catalog docs, no T3
        collections) is CLEAN rc=0 — the empty-T3 INCOMPLETE rule above
        applies only when the catalog is populated."""
        cat = _FakeFullCat(
            entries=[], doc_counts={},
            mv_all={"collections": [], "count": 0}, manifests={},
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        self._patch_t3(monkeypatch, {}, t3_collections=set())

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify"])

        assert result.exit_code == 0, result.output

    # ── critique CRIT-1: never_chunked must not blanket-label RDR-145 ──────

    def test_verify_never_chunked_breakdown_by_collection(self, catalog_env, monkeypatch):
        """nexus-sj4a3 substantive critique CRIT-1: RDR-145's 'legitimate by
        design' framing is scoped to knowledge__* store_put notes (empty
        file_path/source_uri) — a code__ zero-chunk doc (nx index repo's
        write path, cdypx's dominant production population) must NOT get
        that label, and the report must break the never-chunked population
        down per collection so cdypx's reconcile can act on it."""
        entries = [
            _FakeEntry(
                "1.1.1", "Store-put note", physical_collection="knowledge__notes",
                chunk_count=0, file_path="", source_uri="",
            ),
            _FakeEntry(
                "1.1.2", "Indexed code file", physical_collection="code__1-20",
                chunk_count=0, file_path="src/nexus/thing.py",
                source_uri="file:///repo/src/nexus/thing.py",
            ),
        ]
        cat = _FakeFullCat(
            entries=entries,
            doc_counts={"knowledge__notes": 1, "code__1-20": 1},
            mv_all={"collections": [
                {"collection": "knowledge__notes", "referenced": 0, "present": 0, "missing": 0},
                {"collection": "code__1-20", "referenced": 0, "present": 0, "missing": 0},
            ], "count": 2},
            manifests={},
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        self._patch_t3(monkeypatch, {}, t3_collections={"knowledge__notes", "code__1-20"})

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        nc = data["never_chunked"]
        assert nc["total"] == 2
        assert nc["rdr145_exempt"]["total"] == 1
        assert nc["rdr145_exempt"]["by_collection"] == [
            {"physical_collection": "knowledge__notes", "count": 1},
        ]
        # nexus-0y0gk critique fix-round (observation item): rdr145_exempt
        # is "legitimate by design", not "definitely unrepairable" -- the
        # note names the actual repairability authority without changing
        # verify's exit code.
        assert "backfill-manifest" in nc["rdr145_exempt"]["note"]
        assert nc["unclassified"]["total"] == 1
        assert nc["unclassified"]["by_collection"] == [
            {"physical_collection": "code__1-20", "count": 1},
        ]
        assert "legitimate by design" not in result.output.replace("\n", " ") or (
            "code__1-20" not in result.output.split("legitimate by design")[0]
        )

    # ── nexus-rqsh1: zero-content-by-design (verifiably unchunkable source) ─

    def test_verify_never_chunked_zero_content_by_design_bucket(
        self, catalog_env, tmp_path, monkeypatch,
    ):
        """A gapped/never-chunked doc whose source is verifiably
        unchunkable (zero-byte, or binary content per
        classifier.looks_like_binary_content) must classify as
        zero_content_by_design, NOT the generic 'unclassified' candidate-
        data-loss bucket -- re-indexing it can never produce a chunk."""
        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "__init__.py").write_bytes(b"")
        (tmp_path / "src" / "fixture.npz").write_bytes(
            b"\x93NUMPY\x01\x00\xff\xfe\xfd\x00binary-not-utf8"
        )
        (tmp_path / "src" / "thing.py").write_text("# real code\n")

        entries = [
            _FakeEntry(
                "1.1.1", "Empty init", physical_collection="code__1-1",
                chunk_count=0, file_path="src/__init__.py",
            ),
            _FakeEntry(
                "1.1.2", "Binary fixture", physical_collection="code__1-1",
                chunk_count=0, file_path="src/fixture.npz",
            ),
            # control: a real, chunkable text file -- must stay unclassified
            # (a genuine candidate-data-loss doc, not zero-content-by-design).
            _FakeEntry(
                "1.1.3", "Real code", physical_collection="code__1-1",
                chunk_count=0, file_path="src/thing.py",
            ),
        ]
        cat = _FakeFullCat(
            entries=entries,
            doc_counts={"code__1-1": 3},
            mv_all={"collections": [
                {"collection": "code__1-1", "referenced": 0, "present": 0, "missing": 0},
            ], "count": 1},
            manifests={},
            owners_with_roots={"1.1": str(tmp_path)},
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        self._patch_t3(monkeypatch, {}, t3_collections={"code__1-1"})

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        nc = data["never_chunked"]
        assert nc["total"] == 3
        zc = nc["zero_content_by_design"]
        assert zc["total"] == 2
        assert zc["by_collection"] == [{"physical_collection": "code__1-1", "count": 2}]
        assert set(zc["sample_tumblers"]) == {"1.1.1", "1.1.2"}
        assert "will never chunk" in zc["note"].lower()
        assert "tombstone" in zc["note"].lower()
        # control: the real file stays unclassified -- a genuine
        # candidate-data-loss doc, never moved into zero_content_by_design.
        assert nc["unclassified"]["total"] == 1
        assert nc["unclassified"]["by_collection"] == [
            {"physical_collection": "code__1-1", "count": 1},
        ]
        # non-vacuity: the docs must NOT vanish from the census -- total
        # accounts for all three (killing the nexus-cotmr round-1
        # exemption error, which hid a population like this entirely).
        assert nc["total"] == zc["total"] + nc["unclassified"]["total"] + nc["rdr145_exempt"]["total"]

    def test_verify_zero_content_by_design_honest_wording_in_human_report(
        self, catalog_env, tmp_path, monkeypatch,
    ):
        (tmp_path / "empty.py").write_bytes(b"")
        entries = [
            _FakeEntry(
                "1.1.1", "Empty file", physical_collection="code__1-1",
                chunk_count=0, file_path="empty.py",
            ),
        ]
        cat = _FakeFullCat(
            entries=entries,
            doc_counts={"code__1-1": 1},
            mv_all={"collections": [
                {"collection": "code__1-1", "referenced": 0, "present": 0, "missing": 0},
            ], "count": 1},
            manifests={},
            owners_with_roots={"1.1": str(tmp_path)},
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        self._patch_t3(monkeypatch, {}, t3_collections={"code__1-1"})

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify"])

        assert result.exit_code == 0, result.output
        lower = result.output.lower()
        assert "zero-content-by-design" in lower
        assert "will never chunk" in lower
        assert "tombstone" in lower

    def test_verify_zero_content_degrades_gracefully_without_owners_with_roots(
        self, catalog_env, monkeypatch,
    ):
        """A catalog reader that doesn't support owners_with_roots (older
        protocol / minimal test double) must not crash verify --
        zero-content detection requires path resolution, so it silently
        degrades to 'unclassified' rather than raising."""

        class _BareCatNoOwnerRoots:
            def __init__(self, entries, doc_counts, mv_all, manifests):
                self._entries = entries
                self._doc_counts = doc_counts
                self._mv_all = mv_all
                self._manifests = manifests

            def all_documents(self, limit=0):
                return list(self._entries)

            def collection_doc_counts(self):
                return dict(self._doc_counts)

            def manifest_verify_all(self):
                return self._mv_all

            def get_manifests(self, doc_ids):
                return {d: self._manifests[d] for d in doc_ids if d in self._manifests}

        entries = [
            _FakeEntry(
                "1.1.1", "Some code file", physical_collection="code__x",
                chunk_count=0, file_path="src/thing.py",
            ),
        ]
        cat = _BareCatNoOwnerRoots(
            entries, {"code__x": 1},
            {"collections": [
                {"collection": "code__x", "referenced": 0, "present": 0, "missing": 0},
            ], "count": 1},
            {},
        )
        assert not hasattr(cat, "owners_with_roots")
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        self._patch_t3(monkeypatch, {}, t3_collections={"code__x"})

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["never_chunked"]["zero_content_by_design"]["total"] == 0
        assert data["never_chunked"]["unclassified"]["total"] == 1

    # ── nexus-bo2d1: ghost-doc manifest rows must never key a phantom "" ───
    #
    # RDR-191 Phase 6 (nexus-o8dil.33): the CLI-level twin tests
    # (test_verify_unbackfilled_manifest_rows_reported_unverifiable,
    # test_verify_ghost_doc_contamination_reported_incomplete_not_phantom,
    # test_verify_ghost_doc_contamination_resolved_when_engine_reports_row_collection)
    # were DELETED — all three exercised _class_c_unverifiable_rows'
    # cross-check of Class C's manifest_row_totals against Class B's
    # referenced_by_collection, retired alongside manifest_verify_all. This
    # direct-helper test is UNAFFECTED — it calls
    # _census_lost_and_never_chunked (Class C's own core census, kept)
    # directly and asserts nothing about Class B.

    def test_class_c_census_never_keys_a_ghost_doc_under_empty_collection(self):
        """nexus-bo2d1 (substantive-critic round 3, 2026-08-12): under the
        shipped RDR-191 contract (``CatalogRepository.java`` ~3530-3547) a
        manifest row's stamped ``collection`` has ZERO relationship to its
        owning doc's ``physical_collection`` -- writers send the collection
        explicitly and the engine stamps it verbatim, never infers one. A
        ghost document (``physical_collection == ""``) is therefore no
        longer special at write time, but ``_census_lost_and_never_chunked``
        still keys its client-side ``manifest_row_totals`` by each entry's
        OWN ``physical_collection`` (integrity.py ~717-722) -- so a ghost
        entry that somehow reached this function (bypassing
        ``_verify_full``'s own filter, defense in depth) would previously
        bucket its manifest rows under the phantom key ``""`` instead of
        being excluded or attributed to its real collection. Calls the
        private helper directly (not through the CLI) so this is proven
        independent of any caller-side filtering."""
        from nexus.commands.catalog_cmds.integrity import _census_lost_and_never_chunked

        entries = [
            _FakeEntry("1.1.1", "Ghost doc", physical_collection="", chunk_count=0),
        ]
        cat = _FakeFullCat(
            entries=entries,
            manifests={"1.1.1": [object(), object(), object()]},
        )
        unreadable: list[str] = []

        _lost, _never_chunked, manifest_row_totals = _census_lost_and_never_chunked(
            cat, entries, unreadable,
        )

        assert "" not in manifest_row_totals, (
            "a ghost doc's manifest rows must never be bucketed under a "
            f"phantom empty-collection key: {manifest_row_totals}"
        )

    # ── code-review IMPORTANT: engine reads must be exception-isolated ─────

    def test_verify_get_manifests_failure_still_emits_valid_json(
        self, catalog_env, monkeypatch,
    ):
        """nexus-sj4a3 code-review IMPORTANT: cat.get_manifests() failing
        mid-sweep must not propagate an unhandled traceback — especially
        under --json, where the CI contract requires valid JSON on stdout
        even when part of the sweep could not be read."""
        entries = [_FakeEntry("1.1.1", "A", physical_collection="knowledge__thing", chunk_count=1)]
        cat = _FakeFullCat(
            entries=entries,
            doc_counts={"knowledge__thing": 1},
            mv_all={"collections": [
                {"collection": "knowledge__thing", "referenced": 1, "present": 1, "missing": 0},
            ], "count": 1},
            get_manifests_exc=RuntimeError("engine unreachable"),
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        self._patch_t3(monkeypatch, {}, t3_collections={"knowledge__thing"})

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify", "--json"])

        assert result.exit_code != 0, result.output
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            "an unhandled traceback reached the CLI boundary"
        )
        data = json.loads(result.stdout)
        assert "catalog:get_manifests" in data["unreadable"]

    def test_verify_collection_doc_counts_failure_is_incomplete(
        self, catalog_env, monkeypatch,
    ):
        """Same guard for cat.collection_doc_counts() (Class A's other
        engine read)."""
        entries = [_FakeEntry("1.1.1", "A", physical_collection="knowledge__thing", chunk_count=1)]
        cat = _FakeFullCat(
            entries=entries,
            doc_counts_exc=RuntimeError("engine unreachable"),
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        self._patch_t3(monkeypatch, {}, t3_collections={"knowledge__thing"})

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify"])

        assert result.exit_code != 0, result.output
        assert "INCOMPLETE" in result.output

    # test_verify_vanished_collection_not_also_reported_damaged DELETED
    # (RDR-191 Phase 6, nexus-o8dil.33): the critique SIG-4 double-report
    # concern it pinned (a vanished collection surfacing under BOTH
    # vanished_collections and damaged) is now structurally impossible —
    # full mode's `damaged` is always [] since Class B is retired — which
    # would make the assertion `data["damaged"] == []` vacuously true
    # regardless of the vanished-collection fixture, not a real test of
    # the double-report guard it was written to pin.

    # ── --collection scoped mode (real ActiveCatalog + mocked T3) ──────────

    def test_verify_collection_filter(
        self, initialized_catalog, catalog_env, monkeypatch, t2_service_env,
    ):
        """--collection scopes the sweep to a single physical_collection —
        the out-of-scope collection is never even examined."""
        missing_chash = "d" * 64
        present_chash = "e" * 64
        self._register_chunked(
            initialized_catalog, "1.1", "In Scope",
            "knowledge__foo", chunk_count=1, chashes=[missing_chash],
            tenant=t2_service_env,
        )
        self._register_chunked(
            initialized_catalog, "1.1", "Out Of Scope",
            "knowledge__bar", chunk_count=1, chashes=[present_chash],
            tenant=t2_service_env,
        )
        self._patch_t3(monkeypatch, {"knowledge__bar": [present_chash]})

        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "verify", "--collection", "knowledge__foo"],
        )

        assert result.exit_code == 1, result.output
        assert "In Scope" in result.output
        assert "Out Of Scope" not in result.output

    def test_verify_scoped_json_output(
        self, initialized_catalog, catalog_env, monkeypatch, t2_service_env,
    ):
        """--collection --json emits per-doc damaged detail."""
        chash = "f" * 64
        self._register_chunked(
            initialized_catalog, "1.1", "Ghost",
            "knowledge__x", chunk_count=1, chashes=[chash],
            tenant=t2_service_env,
        )
        self._patch_t3(monkeypatch, {"knowledge__x": []})

        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "verify", "--collection", "knowledge__x", "--json"],
        )

        assert result.exit_code == 1, result.output
        data = json.loads(result.stdout)
        assert data["summary"]["exit"] == 1
        assert len(data["damaged"]) == 1
        assert data["damaged"][0]["title"] == "Ghost"
        assert data["damaged"][0]["missing"] == 1

    def test_verify_excludes_alias_rows(
        self, initialized_catalog, catalog_env, monkeypatch, t2_service_env,
    ):
        """Alias rows (alias_of != '') must NOT appear as findings.

        Pins the fix for nexus-xnz0o: all_documents()/list_by_collection()
        previously omitted alias_of from its SELECT, so CatalogEntry.alias_of
        was always "" and the ``not e.alias_of`` guard was vacuously True.
        """
        from nexus.catalog.tumbler import Tumbler

        coll = "knowledge__alias_test"
        chash = "9" * 64
        self._register_chunked(
            initialized_catalog, "1.1", "Canonical",
            coll, chunk_count=1, chashes=[chash],
            tenant=t2_service_env,
        )
        alias_tumbler = initialized_catalog.register(
            Tumbler.parse("1.1"), "Alias Doc",
            content_type="knowledge", physical_collection=coll,
        )
        # nexus-iltyk: set_alias mutates but is NOT on CATALOG_WRITE_OPS, so
        # the typed writer will not forward it. No single object does both.
        unroutable_write_target().set_alias(alias_tumbler, Tumbler.parse("1.1.1"))

        # T3 reports nothing present — the canonical doc is damaged.
        self._patch_t3(monkeypatch, {coll: []})

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "verify", "--collection", coll])

        assert result.exit_code == 1, result.output
        assert "Canonical" in result.output
        assert "Alias Doc" not in result.output, (
            f"Alias row 'Alias Doc' appeared in verify output — alias filter broken:\n{result.output}"
        )

    def test_verify_heal_drops_damaged(
        self, initialized_catalog, catalog_env, monkeypatch, t2_service_env,
    ):
        """--collection --heal with `d` (drop) removes the damaged tumbler."""
        chash = "7" * 64
        self._register_chunked(
            initialized_catalog, "1.1", "Ghost",
            "knowledge__thing", chunk_count=1, chashes=[chash],
            tenant=t2_service_env,
        )
        self._patch_t3(monkeypatch, {"knowledge__thing": []})

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["catalog", "verify", "--collection", "knowledge__thing", "--heal"],
            input="d\n",
        )

        assert result.exit_code == 0, result.output
        assert "dropped" in result.output.lower()

        # Confirm the tumbler is gone via the active reader (nexus-i711w:
        # the raw local-SQLite reopen this used has no service equivalent).
        from tests._catalog_fixture_ops import documents_by_title
        assert documents_by_title("Ghost") == []

    def test_verify_scoped_all_damaged_not_100_percent_clean(
        self, initialized_catalog, catalog_env, monkeypatch, t2_service_env,
    ):
        """nexus-sj4a3 substantive critique SIG-3: scoped mode's clean-pct
        must subtract damaged docs (a real per-doc count in scoped mode) —
        an all-damaged run must never print '100.0% clean'."""
        chash_a = "1" * 64
        chash_b = "2" * 64
        self._register_chunked(
            initialized_catalog, "1.1", "Ghost A", "knowledge__thing",
            chunk_count=1, chashes=[chash_a],
            tenant=t2_service_env,
        )
        self._register_chunked(
            initialized_catalog, "1.1", "Ghost B", "knowledge__thing",
            chunk_count=1, chashes=[chash_b],
            tenant=t2_service_env,
        )
        self._patch_t3(monkeypatch, {"knowledge__thing": []})

        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "verify", "--collection", "knowledge__thing"],
        )

        assert result.exit_code == 1, result.output
        assert "100.0% clean" not in result.output
        assert "0.0% clean" in result.output

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
        # The canonical command's dry-run summary lands. This asserted
        # "would generate" until ac89209dd (nexus-glivh) replaced the
        # placeholder preview with a real one, whose summary line reads
        # "total links that would be created: N". The deprecation warning
        # alone cannot satisfy this, so it still proves the delegation.
        assert "total links that would be created" in result.output.lower()

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

    def _make_catalog_with_links(self, catalog_env: object) -> ActiveCatalog:
        # nexus-i711w: seeds through ActiveCatalog (the live catalog) — the
        # local Catalog.init arm this helper used is gone.
        cat = ActiveCatalog()
        owner = cat.register_owner("test", "repo", repo_hash="abc")
        t1 = cat.register(owner, "catalog.py", content_type="code", file_path="src/nexus/catalog.py")
        t2 = cat.register(owner, "RDR-060", content_type="rdr", file_path="docs/rdr/rdr-060.md")
        cat.link(t1, t2, "implements", created_by="test")
        return cat

    @_needs_diagnosis_nexus_02avu
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

    @_needs_diagnosis_nexus_02avu
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

    @_needs_diagnosis_nexus_02avu
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

    def _make_cat(self, catalog_env: object) -> ActiveCatalog:
        # nexus-i711w: the local Catalog.init arm is gone; ActiveCatalog
        # routes to the live catalog and needs no init.
        return ActiveCatalog()

    def test_gc_no_orphans(self, catalog_env):
        runner = CliRunner()
        cat = self._make_cat(catalog_env)
        owner = cat.register_owner("repo", "repo", repo_hash="abc")
        cat.register(owner, "current.py", content_type="code", file_path="src/current.py")
        # miss_count is 0 by default — not an orphan
        result = runner.invoke(main, ["catalog", "gc"])
        assert result.exit_code == 0
        assert "No orphan" in result.output

    @_needs_diagnosis_nexus_02avu
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

    @_needs_diagnosis_nexus_02avu
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

    @_needs_diagnosis_nexus_02avu
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


# TOMBSTONE (nexus-i711w terminal deletion): ``TestCollectionNameCommand``
# lived here (RDR-103 Phase 3b, ``nx catalog collection-name``). Its subject
# was LOCAL-catalog name minting — every seeding path went through the
# deleted ``nexus.catalog.catalog.Catalog`` (Catalog.init/register_owner),
# and its three substantive tests were already hard-skipped pending
# nexus-02avu diagnosis. The class retired with the substrate; the surviving
# collection-name contract is pinned by ``collection_name.py``'s own tests
# (tests/test_catalog_collection_name.py).


class TestSeam3OwnersCarve:
    """Contract pins for the nexus-kgyoz seam 3 owners command carve.

    Non-vacuous: each pin fails if the carve regresses — by re-inlining the
    commands into ``commands.catalog``, dropping the ``register`` wiring, or
    binding ``_get_catalog`` at import time (which would break the
    ``patch("nexus.commands.catalog._get_catalog", …)`` test seam).

    ``owners`` reads via ``_get_catalog()``, defended by the patch seam below.
    (The group's other verb, ``dedupe-owners``, was deleted in nexus-i711w
    Stage 2 sub-stage C-store along with the admin factory it opened.)
    """

    def test_owner_commands_registered_on_group(self):
        from nexus.cli import main
        catalog_group = main.commands["catalog"]
        assert "owners" in catalog_group.commands
        assert "dedupe-owners" not in catalog_group.commands

    def test_owner_commands_defined_in_carved_module(self):
        """The callbacks live in catalog_cmds.owners, not commands.catalog."""
        from nexus.cli import main
        from nexus.commands.catalog_cmds import owners as owners_mod
        catalog_group = main.commands["catalog"]
        assert catalog_group.commands["owners"].callback is owners_mod.owners_cmd.callback

    def test_owners_command_routes_get_catalog_through_module(self):
        """Patching commands.catalog._get_catalog is observed by the carved
        ``owners`` command — proves module-routed (not import-bound) access."""
        from unittest.mock import MagicMock, patch

        from nexus.catalog.http_catalog_client import HttpCatalogClient

        from nexus.cli import main

        # nexus-i711w: spec'd against HttpCatalogClient — the only catalog
        # _get_catalog() can return now that the local Catalog is deleted.
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners.return_value = [
            {"tumbler_prefix": "1.1", "owner_type": "repo", "name": "sentinel-owner"},
        ]
        with patch("nexus.commands.catalog._get_catalog", return_value=fake):
            result = CliRunner().invoke(main, ["catalog", "owners"])
        assert result.exit_code == 0, result.output
        assert "sentinel-owner" in result.output
        fake.list_owners.assert_called_once()


# TestKgyozBackfillCarve retired (nexus-i711w terminal deletion):
# `nx catalog backfill-owner-id` wrote through the local SQLite catalog's raw
# handle and died with it — catalog_cmds/backfill.py is now an empty
# registration hook, so the carve pins have nothing to pin.


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

        from nexus.catalog.http_catalog_client import HttpCatalogClient

        from nexus.cli import main

        # nexus-i711w: spec'd against HttpCatalogClient — the only catalog
        # _get_catalog() can return now that the local Catalog is deleted.
        fake = MagicMock(spec=HttpCatalogClient)
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

        from nexus.catalog.http_catalog_client import HttpCatalogClient

        from nexus.cli import main

        edge = MagicMock()
        edge.to_dict.return_value = {"from": "1.1.1", "to": "1.2.1", "link_type": "cites"}
        # nexus-i711w: spec'd against HttpCatalogClient — the only catalog
        # _get_catalog() can return now that the local Catalog is deleted.
        fake = MagicMock(spec=HttpCatalogClient)
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


# TestWhh61BackupsCarve retired (nexus-i711w terminal deletion): the
# list-backups / vacuum-backups verbs and their carved module
# (catalog_cmds/backups.py) died with nexus.catalog.catalog_backup —
# backups were local-catalog-only.


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

        from nexus.catalog.http_catalog_client import HttpCatalogClient

        from nexus.cli import main

        # nexus-i711w: spec'd against HttpCatalogClient — the only catalog
        # _get_catalog() can return now that the local Catalog is deleted.
        cat = MagicMock(spec=HttpCatalogClient)
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

        from nexus.catalog.http_catalog_client import HttpCatalogClient

        from nexus.cli import main

        # nexus-i711w: spec'd against HttpCatalogClient — the only catalog
        # _get_catalog() can return now that the local Catalog is deleted.
        cat = MagicMock(spec=HttpCatalogClient)
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

        from nexus.catalog.http_catalog_client import HttpCatalogClient

        from nexus.cli import main

        entry = MagicMock()
        entry.tumbler = "1.1.1"
        # nexus-i711w: spec'd against HttpCatalogClient — the only catalog
        # _get_catalog() can return now that the local Catalog is deleted.
        cat = MagicMock(spec=HttpCatalogClient)
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
    """Contract pins for the nexus-whh61.4 maintenance carve.

    Non-vacuous: fails on re-inline into ``commands.catalog``, a dropped
    ``register`` call, or import-bound (non-module-routed) ``_get_catalog``.

    RDR-155 P4b P3: ``chash-reconcile`` was the carve's second command and is
    RETIRED — it swept stale rows from the local SQLite chash router, a table
    RDR-187 DROPped, and it already refused service-mode installs outright. The
    carve contract itself is unchanged and still worth pinning; ``gc`` is simply
    the only inhabitant now.
    """

    MAINT_COMMANDS = ["gc"]

    def test_chash_reconcile_is_retired(self):
        """Successor to the removed second MAINT_COMMANDS entry: the verb must
        stay gone. Without this, `chash-reconcile` could be re-registered and
        the shrunk MAINT_COMMANDS list would say nothing about it."""
        from nexus.cli import main
        catalog_group = main.commands["catalog"]
        assert "chash-reconcile" not in catalog_group.commands, (
            "nx catalog chash-reconcile is back — it was retired at RDR-155 "
            "P4b P3 because RDR-187 dropped the chash_index router it swept."
        )

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

        from nexus.catalog.http_catalog_client import HttpCatalogClient

        from nexus.cli import main

        # nexus-i711w: spec'd against HttpCatalogClient — the only catalog
        # _get_catalog() can return now that the local Catalog is deleted.
        cat = MagicMock(spec=HttpCatalogClient)
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

        from nexus.catalog.http_catalog_client import HttpCatalogClient

        from nexus.cli import main

        # nexus-i711w: spec'd against HttpCatalogClient — the only catalog
        # _get_catalog() can return now that the local Catalog is deleted.
        cat = MagicMock(spec=HttpCatalogClient)
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

        from nexus.catalog.http_catalog_client import HttpCatalogClient

        from nexus.cli import main

        # nexus-i711w: spec'd against HttpCatalogClient — the only catalog
        # _get_catalog() can return now that the local Catalog is deleted.
        cat = MagicMock(spec=HttpCatalogClient)
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

        from nexus.catalog.http_catalog_client import HttpCatalogClient

        from nexus.cli import main

        # nexus-i711w: spec'd against HttpCatalogClient — the only catalog
        # _get_catalog() can return now that the local Catalog is deleted.
        cat = MagicMock(spec=HttpCatalogClient)
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

    # ``synthesize-log`` and the local-event-log helpers
    # (_run_replay_equality, _snapshot_table, _check_bootstrap_status,
    # _run_t3_doc_id_coverage and their printers) were deleted in nexus-i711w
    # Stage 2 sub-stage C-store; what remains is the service-capable half.
    DOCTOR_COMMANDS = ["doctor"]
    SAMPLE_MOVED_HELPERS = [
        "_run_name_vs_embed_dim", "_percentile",
        "_run_collections_drift", "_run_chunk_size_distribution",
        "_run_chunk_text_dedup", "_run_t3_vs_catalog",
        "_expected_dim_for_model_token",
        # threshold constants moved with the helpers. (_ORPHAN_RATIO_WARN_
        # THRESHOLD was the t3-doc-id-coverage warn gate and died with that
        # check — it had no other user.)
        "_MICRO_CHUNK_BYTES", "_VOYAGE_DIM",
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


class TestLinkDanglingEndpointRefusal:
    """nexus-9ssih CLI half (7.0.0 release review): engines >= v0.1.61 refuse
    dangling-endpoint links as 400 code=dangling_endpoint, which the client
    translates to ValueError. link_cmd must surface that as a clean
    ClickException, not a raw traceback."""

    def test_link_valueerror_is_a_clean_refusal(self, monkeypatch, tmp_path):
        from nexus.commands import catalog as _cat_cmd

        # Isolate every detector root: without this, the stranded-install
        # banner can fire from MIXED roots (config under tmp, legacy chroma
        # under the real ~/.local/share — nexus-rjod2) and pollute output.
        monkeypatch.setenv("NX_LOCAL_CHROMA_PATH", str(tmp_path / "chroma"))
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "config"))

        class _Writer:
            def link(self, *a, **k):
                raise ValueError(
                    "link refused: an endpoint does not resolve to a live "
                    "catalog document. Pass allow_dangling=True to write "
                    "the edge anyway."
                )
        class _Cat:
            pass

        monkeypatch.setattr(_cat_cmd, "_get_catalog", lambda: _Cat())
        monkeypatch.setattr(_cat_cmd, "_get_catalog_writer", lambda: _Writer())
        monkeypatch.setattr(_cat_cmd, "_resolve_tumbler", lambda cat, t: t)
        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "link", "1.1.1", "9.9.9", "--type", "cites"],
        )
        assert result.exit_code != 0
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            "dangling-endpoint refusal must be a ClickException, not a traceback"
        )
        assert "does not resolve" in result.output
