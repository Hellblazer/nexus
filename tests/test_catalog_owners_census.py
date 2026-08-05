# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx catalog owners --census`` (nexus-7kl32 arm a).

Owner-registry GC: classify every registered repo owner's on-disk root as
healthy / path_vanished / path_exists_empty / unreadable, surfacing the
dead-owner debris population (bench-index sandboxes from 2026-07-04,
/tmp/u8n4r_probe2/*, probe-rich-*, a stale worktree, nexus-rdr-125) that
dominated the 2026-08-04 shakedown's signal-density census (24 of 25
signal-free doctor greens). Read-only this round — see the module docstring
in ``nexus.commands.catalog_cmds.owners`` for why no mutation arm ships yet.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import main


def _owner(tumbler: str, name: str, repo_root: str) -> dict:
    return {
        "tumbler_prefix": tumbler,
        "name": name,
        "owner_type": "repo",
        "repo_root": repo_root,
    }


class TestClassifyOwnerRoot:
    """Unit coverage of the pure classifier — no catalog, no CLI."""

    def test_classifies_path_vanished(self, tmp_path):
        from nexus.commands.catalog_cmds.owners import _classify_owner_root

        dead = tmp_path / "gone" / "benchidx-w2"  # never created
        assert _classify_owner_root(str(dead)) == "path_vanished"

    def test_classifies_path_exists_empty(self, tmp_path):
        from nexus.commands.catalog_cmds.owners import _classify_owner_root

        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        assert _classify_owner_root(str(empty_dir)) == "path_exists_empty"

    def test_classifies_healthy(self, tmp_path):
        from nexus.commands.catalog_cmds.owners import _classify_owner_root

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "README.md").write_text("hi")
        assert _classify_owner_root(str(repo)) == "healthy"

    def test_unreadable_directory_is_distinct_not_silently_healthy(
        self, tmp_path, monkeypatch
    ):
        """Kill control for the honesty principle itself: a directory whose
        contents cannot be confirmed must never fall into 'healthy' by
        default — that would just relocate the nexus-9t86i vacuity bug into
        the new verb instead of fixing the class of bug."""
        from pathlib import Path

        from nexus.commands.catalog_cmds import owners as owners_mod

        repo = tmp_path / "locked"
        repo.mkdir()
        real_iterdir = Path.iterdir

        def _raise(self):
            if self == repo:
                raise PermissionError("simulated permission denied")
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", _raise)
        assert owners_mod._classify_owner_root(str(repo)) == "unreadable"

    def test_nonexistent_root_never_misclassified_as_healthy(self, tmp_path):
        """Falsification check: deleting the fixture directory after
        classifying it healthy must flip the classification, proving the
        function actually looks at the filesystem rather than returning a
        constant."""
        from nexus.commands.catalog_cmds.owners import _classify_owner_root

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "f").write_text("x")
        assert _classify_owner_root(str(repo)) == "healthy"

        import shutil

        shutil.rmtree(repo)
        assert _classify_owner_root(str(repo)) == "path_vanished"


class TestOwnersCensusCommand:
    def test_census_reports_all_buckets(self, tmp_path):
        vanished = tmp_path / "vanished"
        empty = tmp_path / "empty"
        empty.mkdir()
        healthy = tmp_path / "healthy"
        healthy.mkdir()
        (healthy / "f").write_text("x")

        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners_by_type.return_value = [
            _owner("1.1", "vanished-owner", str(vanished)),
            _owner("1.2", "empty-owner", str(empty)),
            _owner("1.3", "healthy-owner", str(healthy)),
        ]
        with patch("nexus.commands.catalog._get_catalog", return_value=fake):
            result = CliRunner().invoke(main, ["catalog", "owners", "--census"])
        assert result.exit_code == 0, result.output
        fake.list_owners_by_type.assert_called_once_with("repo")
        assert "1.1" in result.output
        assert "1.2" in result.output
        assert "path_vanished" in result.output
        assert "path_exists_empty" in result.output
        assert "healthy" in result.output
        assert "2 dead owner(s)" in result.output

    def test_census_json_shape(self, tmp_path):
        vanished = tmp_path / "vanished"
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners_by_type.return_value = [_owner("1.1", "v", str(vanished))]
        with patch("nexus.commands.catalog._get_catalog", return_value=fake):
            result = CliRunner().invoke(
                main, ["catalog", "owners", "--census", "--json"]
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["path_vanished"][0]["tumbler"] == "1.1"
        assert data["path_vanished"][0]["repo_root"] == str(vanished)
        assert data["dead_owner_count"] == 1
        assert data["mutation_status"] == "not_implemented"
        assert "mutation_note" in data

    def test_census_notes_mutation_unavailable_in_human_output(self, tmp_path):
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners_by_type.return_value = []
        with patch("nexus.commands.catalog._get_catalog", return_value=fake):
            result = CliRunner().invoke(main, ["catalog", "owners", "--census"])
        assert result.exit_code == 0, result.output
        assert "NOT implemented" in result.output
        assert "nexus-7kl32" in result.output

    def test_census_excludes_no_root_owners_from_dead_buckets(self, tmp_path):
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners_by_type.return_value = [_owner("1.9", "no-root", "")]
        with patch("nexus.commands.catalog._get_catalog", return_value=fake):
            result = CliRunner().invoke(
                main, ["catalog", "owners", "--census", "--json"]
            )
        data = json.loads(result.stdout)
        assert data["path_vanished"] == []
        assert data["path_exists_empty"] == []
        assert data["healthy"] == []
        assert len(data["no_repo_root"]) == 1
        assert data["no_repo_root"][0]["tumbler"] == "1.9"

    def test_census_never_calls_a_catalog_writer(self, tmp_path):
        """Report-first discipline (reconcile-stale precedent): census must
        never construct a catalog writer — there is no mutation arm to gate
        this round, and the default MUST stay read-only."""
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners_by_type.return_value = []
        with patch("nexus.commands.catalog._get_catalog", return_value=fake), \
             patch("nexus.commands.catalog._get_catalog_writer") as writer_factory:
            result = CliRunner().invoke(main, ["catalog", "owners", "--census"])
        assert result.exit_code == 0, result.output
        writer_factory.assert_not_called()

    def test_plain_owners_listing_unaffected_by_census_flag_absence(self):
        """Kill control: --census is opt-in; the pre-existing `nx catalog
        owners` behavior (list_owners, not list_owners_by_type) must be
        untouched by this change."""
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners.return_value = [
            {"tumbler_prefix": "1.1", "owner_type": "repo", "name": "sentinel-owner"},
        ]
        with patch("nexus.commands.catalog._get_catalog", return_value=fake):
            result = CliRunner().invoke(main, ["catalog", "owners"])
        assert result.exit_code == 0, result.output
        assert "sentinel-owner" in result.output
        fake.list_owners.assert_called_once()
        fake.list_owners_by_type.assert_not_called()
