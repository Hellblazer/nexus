# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nx catalog prune-stale`` and the ``--rdr-prefix-mode`` of
``nx catalog remediate-paths`` (nexus-zg4c, RDR-090 P1.5).

The pair handles catalog-side staleness left by file renames and deletions:

  * ``remediate-paths --rdr-prefix-mode`` repairs entries whose RDR file
    was renamed (``rdr-066-old-title.md`` → ``rdr-066-new-title.md``) by
    matching on the ``rdr-NNN-`` prefix when basename-match fails.
  * ``prune-stale`` drops entries whose ``file_path`` is absolute and
    missing from disk and has no plausible same-prefix replacement —
    the catalog-side counterpart to ``nx t3 prune-stale`` (#349).

Two-flag delete dance (``--no-dry-run --confirm``) mirrors the T3
subcommand for symmetry.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.catalog.catalog import Catalog
from nexus.catalog.tumbler import Tumbler
from nexus.cli import main


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


@pytest.fixture
def catalog_env(tmp_path, monkeypatch):
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    return catalog_dir


@pytest.fixture
def initialized_catalog(catalog_env):
    cat = Catalog.init(catalog_env)
    cat.register_owner("rdr", "design")
    cat.register_owner("test-papers", "curator")
    return cat


@pytest.fixture
def rdr_dir(tmp_path: Path) -> Path:
    """A simulated docs/rdr subtree."""
    root = tmp_path / "rdr"
    root.mkdir(parents=True)
    return root


def _register(
    cat: Catalog,
    *,
    title: str,
    file_path: str,
    owner_name: str = "rdr",
    content_type: str = "rdr",
    physical_collection: str = "",
) -> Tumbler:
    """Register an entry under ``owner_name`` with the given ``file_path``."""
    owner_row = cat._db.execute(
        "SELECT tumbler_prefix FROM owners WHERE name = ?", (owner_name,),
    ).fetchone()
    assert owner_row is not None, f"owner {owner_name!r} not registered"
    owner = Tumbler.parse(owner_row[0])
    return cat.register(
        owner=owner,
        title=title,
        content_type=content_type,
        file_path=file_path,
        physical_collection=physical_collection,
    )


# ── Pure helper: _rdr_prefix_of ────────────────────────────────────────────


def test_rdr_prefix_of_extracts_three_digit_id() -> None:
    from nexus.commands.catalog import _rdr_prefix_of

    assert _rdr_prefix_of("rdr-066-enrichment-time.md") == "rdr-066-"
    assert _rdr_prefix_of("rdr-090-bench.md") == "rdr-090-"
    assert _rdr_prefix_of("/abs/docs/rdr/rdr-067-audit-loop.md") == "rdr-067-"


def test_rdr_prefix_of_handles_four_digit_id() -> None:
    """RDR numbering may extend past 999; the regex must accept 3+ digits."""
    from nexus.commands.catalog import _rdr_prefix_of

    assert _rdr_prefix_of("rdr-1042-future.md") == "rdr-1042-"


def test_rdr_prefix_of_returns_empty_for_non_rdr() -> None:
    from nexus.commands.catalog import _rdr_prefix_of

    assert _rdr_prefix_of("paper.pdf") == ""
    assert _rdr_prefix_of("README.md") == ""
    assert _rdr_prefix_of("rdr-no-number.md") == ""
    assert _rdr_prefix_of("") == ""


# ── remediate-paths --rdr-prefix-mode ──────────────────────────────────────


class TestRemediateRdrPrefixMode:
    def test_renamed_rdr_resolved_via_prefix(
        self, initialized_catalog: Catalog, catalog_env: Path, rdr_dir: Path,
    ) -> None:
        """A renamed RDR with the same NNN prefix is found by --rdr-prefix-mode."""
        # On disk: only the new name exists.
        (rdr_dir / "rdr-066-composition-smoke.md").write_text("# new title")

        # Catalog has the old absolute path (now missing).
        old_abs = str(rdr_dir / "rdr-066-enrichment-time.md")
        tumbler = _register(initialized_catalog, title="rdr-066", file_path=old_abs)

        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "remediate-paths", str(rdr_dir), "--rdr-prefix-mode",
        ])
        assert result.exit_code == 0, result.output

        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        entry = cat2.resolve(tumbler)
        assert entry.file_path == str(rdr_dir / "rdr-066-composition-smoke.md")

    def test_disabled_by_default(
        self, initialized_catalog: Catalog, catalog_env: Path, rdr_dir: Path,
    ) -> None:
        """Without --rdr-prefix-mode, the renamed RDR is reported 'no candidate'."""
        (rdr_dir / "rdr-066-composition-smoke.md").write_text("# new title")
        old_abs = str(rdr_dir / "rdr-066-enrichment-time.md")
        tumbler = _register(initialized_catalog, title="rdr-066", file_path=old_abs)

        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "remediate-paths", str(rdr_dir),
        ])
        assert result.exit_code == 0, result.output
        assert "1 no candidate" in result.output

        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        entry = cat2.resolve(tumbler)
        assert entry.file_path == old_abs

    def test_ambiguous_prefix_skipped(
        self, initialized_catalog: Catalog, catalog_env: Path, rdr_dir: Path,
    ) -> None:
        """Two RDR files sharing the same NNN prefix → ambiguous, skip."""
        (rdr_dir / "rdr-066-one.md").write_text("# a")
        (rdr_dir / "rdr-066-two.md").write_text("# b")
        old_abs = str(rdr_dir / "rdr-066-orig.md")
        tumbler = _register(initialized_catalog, title="rdr-066", file_path=old_abs)

        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "remediate-paths", str(rdr_dir), "--rdr-prefix-mode",
        ])
        assert result.exit_code == 0, result.output
        assert "ambiguous" in result.output

        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        entry = cat2.resolve(tumbler)
        assert entry.file_path == old_abs

    def test_basename_match_takes_precedence(
        self, initialized_catalog: Catalog, catalog_env: Path, rdr_dir: Path,
    ) -> None:
        """When basename matches, prefix-mode never fires (first hit wins)."""
        (rdr_dir / "rdr-066-orig.md").write_text("# original")
        (rdr_dir / "rdr-066-other.md").write_text("# distractor")
        # Catalog points to a basename-only path that matches a real file.
        tumbler = _register(initialized_catalog, title="rdr-066", file_path="rdr-066-orig.md")

        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "remediate-paths", str(rdr_dir), "--rdr-prefix-mode",
        ])
        assert result.exit_code == 0, result.output

        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        entry = cat2.resolve(tumbler)
        assert entry.file_path == str(rdr_dir / "rdr-066-orig.md")

    def test_non_rdr_basename_unaffected(
        self, initialized_catalog: Catalog, catalog_env: Path, rdr_dir: Path,
    ) -> None:
        """Non-RDR paths (no rdr-NNN- prefix) get no prefix-mode help."""
        (rdr_dir / "design-notes.md").write_text("# notes")
        old_abs = str(rdr_dir / "design-other.md")
        tumbler = _register(initialized_catalog, title="design", file_path=old_abs)

        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "remediate-paths", str(rdr_dir), "--rdr-prefix-mode",
        ])
        assert result.exit_code == 0, result.output
        assert "1 no candidate" in result.output

        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        entry = cat2.resolve(tumbler)
        assert entry.file_path == old_abs


# ── prune-stale ────────────────────────────────────────────────────────────


class TestCatalogPruneStale:
    def test_dry_run_default_no_writes(
        self, initialized_catalog: Catalog, catalog_env: Path, tmp_path: Path,
    ) -> None:
        """Default --dry-run reports stale entries without deleting."""
        missing = str(tmp_path / "rdr-999-test-gone.md")
        tumbler = _register(initialized_catalog, title="rdr-999-test", file_path=missing)

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "prune-stale"])
        assert result.exit_code == 0, result.output
        assert "1 stale entr" in result.output
        # Default is dry-run; no deletion.
        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        assert cat2.resolve(tumbler) is not None

    def test_no_dry_run_alone_is_report_only(
        self, initialized_catalog: Catalog, catalog_env: Path, tmp_path: Path,
    ) -> None:
        """--no-dry-run without --confirm reports but does not delete."""
        missing = str(tmp_path / "rdr-999-test-gone.md")
        tumbler = _register(initialized_catalog, title="rdr-999-test", file_path=missing)

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "prune-stale", "--no-dry-run"])
        assert result.exit_code == 0, result.output
        assert "--confirm" in result.output  # explicit nudge

        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        assert cat2.resolve(tumbler) is not None

    def test_no_dry_run_confirm_deletes(
        self, initialized_catalog: Catalog, catalog_env: Path, tmp_path: Path,
    ) -> None:
        """--no-dry-run --confirm actually deletes the catalog row."""
        missing = str(tmp_path / "rdr-999-test-gone.md")
        tumbler = _register(initialized_catalog, title="rdr-999-test", file_path=missing)

        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "prune-stale", "--no-dry-run", "--confirm"],
        )
        assert result.exit_code == 0, result.output

        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        assert cat2.resolve(tumbler) is None

    def test_live_paths_never_flagged(
        self, initialized_catalog: Catalog, catalog_env: Path, tmp_path: Path,
    ) -> None:
        """Entries whose file_path exists on disk are never reported."""
        live = tmp_path / "rdr-001-live.md"
        live.write_text("# live")
        tumbler = _register(initialized_catalog, title="rdr-001", file_path=str(live))

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "prune-stale"])
        assert result.exit_code == 0, result.output
        assert "0 stale" in result.output

        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        assert cat2.resolve(tumbler) is not None

    def test_basename_only_entries_skipped(
        self, initialized_catalog: Catalog, catalog_env: Path, tmp_path: Path,
    ) -> None:
        """Basename-only entries are remediable, not stale; never deleted by prune.

        prune-stale targets entries that are unambiguously gone (absolute
        path, missing on disk). Basename-only entries should be repaired
        via remediate-paths instead.
        """
        tumbler = _register(initialized_catalog, title="basename", file_path="paper.pdf")

        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "prune-stale", "--no-dry-run", "--confirm"],
        )
        assert result.exit_code == 0, result.output

        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        assert cat2.resolve(tumbler) is not None

    def test_empty_file_path_skipped(
        self, initialized_catalog: Catalog, catalog_env: Path,
    ) -> None:
        """MCP-stored entries with empty file_path are never deleted by prune."""
        tumbler = _register(initialized_catalog, title="mcp", file_path="")

        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "prune-stale", "--no-dry-run", "--confirm"],
        )
        assert result.exit_code == 0, result.output

        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        assert cat2.resolve(tumbler) is not None

    def test_collection_filter_scopes_deletion(
        self, initialized_catalog: Catalog, catalog_env: Path, tmp_path: Path,
    ) -> None:
        """--collection limits the sweep to one physical_collection."""
        miss_a = str(tmp_path / "a-gone.md")
        miss_b = str(tmp_path / "b-gone.md")
        t_a = _register(
            initialized_catalog, title="A", file_path=miss_a,
            physical_collection="rdr__nexus_a",
        )
        t_b = _register(
            initialized_catalog, title="B", file_path=miss_b,
            physical_collection="rdr__nexus_b",
        )

        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "prune-stale",
            "--collection", "rdr__nexus_a",
            "--no-dry-run", "--confirm",
        ])
        assert result.exit_code == 0, result.output

        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        assert cat2.resolve(t_a) is None  # in scope → deleted
        assert cat2.resolve(t_b) is not None  # out of scope → kept

    def test_rdr_prefix_replacement_skips_prune(
        self, initialized_catalog: Catalog, catalog_env: Path, rdr_dir: Path,
    ) -> None:
        """prune-stale never deletes an entry that has a plausible same-prefix
        replacement on disk — the rename should be remediated, not pruned.

        This check fires when ``--rdr-prefix-skip`` is on (default). Pass
        ``--no-rdr-prefix-skip`` to ignore the rename hint and prune anyway.
        """
        (rdr_dir / "rdr-066-renamed.md").write_text("# new")
        old = str(rdr_dir / "rdr-066-orig.md")
        tumbler = _register(initialized_catalog, title="rdr-066", file_path=old)

        runner = CliRunner()
        # Default behaviour: skip prune when a same-prefix replacement exists.
        result = runner.invoke(main, [
            "catalog", "prune-stale",
            "--source-dir", str(rdr_dir),
            "--no-dry-run", "--confirm",
        ])
        assert result.exit_code == 0, result.output
        assert "skipped" in result.output.lower()

        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        assert cat2.resolve(tumbler) is not None  # preserved

    def test_no_entries_clean_summary(
        self, initialized_catalog: Catalog, catalog_env: Path,
    ) -> None:
        """Empty catalog produces a clean 0-stale summary."""
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "prune-stale"])
        assert result.exit_code == 0, result.output
        assert "0 stale" in result.output
