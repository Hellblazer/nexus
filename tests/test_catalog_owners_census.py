# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx catalog owners --census`` (nexus-7kl32 arm a), ``--execute deactivate``
and ``--execute reactivate`` (nexus-cw262 arm b).

Owner-registry GC: classify every registered repo owner's on-disk root as
healthy / path_vanished / path_exists_empty / unreadable, surfacing the
dead-owner debris population (bench-index sandboxes from 2026-07-04,
/tmp/u8n4r_probe2/*, probe-rich-*, a stale worktree, nexus-rdr-125) that
dominated the 2026-08-04 shakedown's signal-density census (24 of 25
signal-free doctor greens). The census arm shipped read-only in nexus-7kl32;
nexus-cw262 adds the engine soft-delete column + route and the CLI mutation
arms — see the module docstring in ``nexus.commands.catalog_cmds.owners``
for the full eligibility/corroboration/residual design (round 3
substantive-critic pass, T2 21467).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import main


def _owner(tumbler: str, name: str, repo_root: str, *, deactivated_at=None) -> dict:
    """nexus-cw262 round-3: every owner dict carries a ``deactivated_at`` key
    by default (None = active) — this is the REAL wire shape a cw262-capable
    engine returns (CatalogRepository.ownerRow always adds the key), and is
    what makes ``nexus.repos.owner_deactivate_capability`` read "available"
    for these fixtures. Tests that specifically want to simulate a
    pre-cw262 engine build the dict without the key directly (see
    TestCapabilityHonesty)."""
    return {
        "tumbler_prefix": tumbler,
        "name": name,
        "owner_type": "repo",
        "repo_root": repo_root,
        "deactivated_at": deactivated_at,
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
        fake.by_owner.return_value = []
        with patch("nexus.commands.catalog._get_catalog", return_value=fake):
            result = CliRunner().invoke(main, ["catalog", "owners", "--census"])
        assert result.exit_code == 0, result.output
        fake.list_owners_by_type.assert_called_once_with("repo", include_deactivated=True)
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
        fake.by_owner.return_value = []
        with patch("nexus.commands.catalog._get_catalog", return_value=fake):
            result = CliRunner().invoke(
                main, ["catalog", "owners", "--census", "--json"]
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["path_vanished"][0]["tumbler"] == "1.1"
        assert data["path_vanished"][0]["repo_root"] == str(vanished)
        assert data["dead_owner_count"] == 1
        assert data["mutation_status"] == "available"
        assert data["execute_command"] == (
            "nx catalog owners --census --execute deactivate --no-dry-run --confirm"
        )
        assert data["reactivate_command_template"] == (
            "nx catalog owners --execute reactivate --owner {tumbler} --no-dry-run --confirm"
        )
        assert data["mutation_eligible"][0]["tumbler"] == "1.1"
        assert data["mutation_eligible"][0]["doc_count"] == 0
        assert "RESIDUAL" in data["mutation_eligible"][0]["residual_note"]
        assert data["mutation_excluded"] == []

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
        # No path_vanished rows -> corroboration never runs -> by_owner unused.
        fake.by_owner.assert_not_called()

    def test_census_never_calls_a_catalog_writer_without_execute(self, tmp_path):
        """Report-first discipline (reconcile-stale precedent): a plain
        --census (no --execute) must never construct a catalog writer."""
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
        fake.list_owners.assert_called_once_with(include_deactivated=False)
        fake.list_owners_by_type.assert_not_called()


class TestIncludeDeactivatedVisibility:
    """nexus-cw262 round-3 critique (T2 21467 Critical mitigation (c),
    VISIBILITY): a deactivated owner is not gone -- --include-deactivated
    surfaces it, converting silent permanent exclusion into auditable state."""

    def test_census_include_deactivated_shows_deactivated_section(self, tmp_path):
        vanished = tmp_path / "vanished"
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners_by_type.return_value = [
            _owner("1.1", "v", str(vanished)),
            _owner("1.2", "dead-owner", "/tmp/dead", deactivated_at="2026-08-05T00:00:00Z"),
        ]
        fake.by_owner.return_value = []
        with patch("nexus.commands.catalog._get_catalog", return_value=fake):
            result = CliRunner().invoke(
                main, ["catalog", "owners", "--census", "--include-deactivated"]
            )
        assert result.exit_code == 0, result.output
        assert "Deactivated owners (1)" in result.output
        assert "1.2" in result.output
        assert "--execute reactivate --owner 1.2" in result.output
        # The active-owner census buckets must NOT include the deactivated one.
        assert "dead-owner" not in result.output.split("Deactivated owners")[0]

    def test_census_without_include_deactivated_hints_at_hidden_count(self, tmp_path):
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners_by_type.return_value = [
            _owner("1.2", "dead-owner", "/tmp/dead", deactivated_at="2026-08-05T00:00:00Z"),
        ]
        with patch("nexus.commands.catalog._get_catalog", return_value=fake):
            result = CliRunner().invoke(main, ["catalog", "owners", "--census"])
        assert result.exit_code == 0, result.output
        assert "1 owner(s) are currently deactivated" in result.output
        assert "--include-deactivated" in result.output

    def test_census_json_include_deactivated_carries_the_list(self, tmp_path):
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners_by_type.return_value = [
            _owner("1.2", "dead-owner", "/tmp/dead", deactivated_at="2026-08-05T00:00:00Z"),
        ]
        with patch("nexus.commands.catalog._get_catalog", return_value=fake):
            result = CliRunner().invoke(
                main, ["catalog", "owners", "--census", "--json", "--include-deactivated"]
            )
        data = json.loads(result.stdout)
        assert data["deactivated_owners"][0]["tumbler_prefix"] == "1.2"

    def test_plain_listing_include_deactivated(self):
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners.return_value = [
            {"tumbler_prefix": "1.1", "owner_type": "repo", "name": "active-owner",
             "deactivated_at": None},
            {"tumbler_prefix": "1.2", "owner_type": "repo", "name": "dead-owner",
             "deactivated_at": "2026-08-05T00:00:00Z"},
        ]
        with patch("nexus.commands.catalog._get_catalog", return_value=fake):
            result = CliRunner().invoke(
                main, ["catalog", "owners", "--include-deactivated"]
            )
        assert result.exit_code == 0, result.output
        fake.list_owners.assert_called_once_with(include_deactivated=True)
        assert "dead-owner" in result.output
        assert "deactivated_at=2026-08-05T00:00:00Z" in result.output


class TestOwnersCorroborationAndEligibility:
    """nexus-cw262 critic design note (T2 21455): corroborating signal +
    eligibility rules, exercised at the pure-function level (no CLI)."""

    def test_corroborate_attaches_doc_and_chunk_counts(self):
        from nexus.commands.catalog_cmds.owners import _corroborate

        class _Doc:
            def __init__(self, chunk_count):
                self.chunk_count = chunk_count

        fake = MagicMock()
        fake.by_owner.return_value = [_Doc(3), _Doc(4)]
        rows = [{"tumbler": "1.1", "name": "x", "repo_root": "/tmp/x"}]
        out = _corroborate(fake, rows)
        assert out[0]["doc_count"] == 2
        assert out[0]["chunk_count"] == 7
        fake.by_owner.assert_called_once_with("1.1")

    def test_corroborate_read_failure_is_recorded_not_swallowed(self):
        from nexus.commands.catalog_cmds.owners import _corroborate

        fake = MagicMock()
        fake.by_owner.side_effect = RuntimeError("boom")
        rows = [{"tumbler": "1.1", "name": "x", "repo_root": "/tmp/x"}]
        out = _corroborate(fake, rows)
        assert out[0]["doc_count"] is None
        assert out[0]["chunk_count"] is None
        assert "boom" in out[0]["evidence_error"]

    def test_eligible_excludes_rows_with_live_documents(self):
        from nexus.commands.catalog_cmds.owners import _eligible_and_excluded

        rows = [
            {"tumbler": "1.1", "doc_count": 0, "chunk_count": 0},
            {"tumbler": "1.2", "doc_count": 3, "chunk_count": 12},
        ]
        eligible, excluded = _eligible_and_excluded(rows)
        assert [r["tumbler"] for r in eligible] == ["1.1"]
        assert [r["tumbler"] for r in excluded] == ["1.2"]
        assert "3 live document(s)" in excluded[0]["exclusion_reason"]

    def test_eligible_excludes_rows_with_failed_corroboration(self):
        """doc_count=None (the corroboration-failed sentinel) must be
        excluded, not silently treated as 0 -- absence of evidence is not
        evidence of absence."""
        from nexus.commands.catalog_cmds.owners import _eligible_and_excluded

        rows = [{"tumbler": "1.1", "doc_count": None, "chunk_count": None}]
        eligible, excluded = _eligible_and_excluded(rows)
        assert eligible == []
        assert excluded[0]["exclusion_reason"] == "corroboration_read_failed"

    def test_only_zero_doc_count_rows_are_eligible(self):
        from nexus.commands.catalog_cmds.owners import _eligible_and_excluded

        rows = [{"tumbler": "1.1", "doc_count": 0, "chunk_count": 0}]
        eligible, excluded = _eligible_and_excluded(rows)
        assert len(eligible) == 1
        assert excluded == []

    def test_eligible_rows_carry_the_residual_disclosure(self):
        """nexus-cw262 round-3 critique CRITICAL mitigation (b), DRY-RUN
        HONESTY: every eligible row states the doc_count==0 residual
        explicitly and names the exact undo command for THAT owner."""
        from nexus.commands.catalog_cmds.owners import _eligible_and_excluded

        rows = [{"tumbler": "1.42", "doc_count": 0, "chunk_count": 0}]
        eligible, _excluded = _eligible_and_excluded(rows)
        note = eligible[0]["residual_note"]
        assert "RESIDUAL" in note
        assert "transiently-unmounted" in note
        assert "--execute reactivate --owner 1.42" in note


class TestOwnersDeactivateMutationArm:
    """nexus-cw262: the ``--execute deactivate`` double-gated mutation arm."""

    def _fake_with_one_vanished(self, tmp_path):
        vanished = tmp_path / "vanished"
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners_by_type.return_value = [_owner("1.1", "v", str(vanished))]
        fake.by_owner.return_value = []  # 0 live docs -> eligible
        return fake

    def test_execute_deactivate_without_census_is_refused(self):
        result = CliRunner().invoke(
            main, ["catalog", "owners", "--execute", "deactivate"]
        )
        assert result.exit_code != 0
        assert "requires --census" in result.output

    def test_json_and_execute_are_mutually_exclusive(self, tmp_path):
        fake = self._fake_with_one_vanished(tmp_path)
        with patch("nexus.commands.catalog._get_catalog", return_value=fake):
            result = CliRunner().invoke(
                main,
                ["catalog", "owners", "--census", "--json", "--execute", "deactivate"],
            )
        assert result.exit_code != 0
        assert "cannot be combined" in result.output

    def test_bare_execute_flag_refuses_to_mutate(self, tmp_path):
        """--census --execute deactivate alone (no --no-dry-run/--confirm)
        must report only, never construct a writer -- the double-gate."""
        fake = self._fake_with_one_vanished(tmp_path)
        with patch("nexus.commands.catalog._get_catalog", return_value=fake), \
             patch("nexus.commands.catalog._get_catalog_writer") as writer_factory:
            result = CliRunner().invoke(
                main, ["catalog", "owners", "--census", "--execute", "deactivate"]
            )
        assert result.exit_code == 0, result.output
        writer_factory.assert_not_called()
        assert "dry-run" in result.output

    def test_no_dry_run_without_confirm_still_refuses_to_mutate(self, tmp_path):
        """--no-dry-run alone (no --confirm) is report-only, same contract
        as reconcile-stale's identical gate."""
        fake = self._fake_with_one_vanished(tmp_path)
        with patch("nexus.commands.catalog._get_catalog", return_value=fake), \
             patch("nexus.commands.catalog._get_catalog_writer") as writer_factory:
            result = CliRunner().invoke(
                main,
                ["catalog", "owners", "--census", "--execute", "deactivate", "--no-dry-run"],
            )
        assert result.exit_code == 0, result.output
        writer_factory.assert_not_called()
        assert "report-only" in result.output

    def test_no_dry_run_and_confirm_deactivates_eligible_owners(self, tmp_path):
        fake = self._fake_with_one_vanished(tmp_path)
        writer = MagicMock()
        writer.deactivate_owner.return_value = True
        with patch("nexus.commands.catalog._get_catalog", return_value=fake), \
             patch("nexus.commands.catalog._get_catalog_writer", return_value=writer):
            result = CliRunner().invoke(
                main,
                ["catalog", "owners", "--census", "--execute", "deactivate",
                 "--no-dry-run", "--confirm"],
            )
        assert result.exit_code == 0, result.output
        writer.deactivate_owner.assert_called_once_with("1.1")
        writer.close.assert_called_once()
        assert "deactivated 1 owner(s)" in result.output

    def test_owners_with_live_documents_are_never_deactivated(self, tmp_path):
        """The binding eligibility rule end-to-end: a path_vanished owner
        with live documents must survive --no-dry-run --confirm untouched."""
        vanished = tmp_path / "vanished"
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners_by_type.return_value = [_owner("1.1", "v", str(vanished))]

        class _Doc:
            chunk_count = 5

        fake.by_owner.return_value = [_Doc()]
        writer = MagicMock()
        with patch("nexus.commands.catalog._get_catalog", return_value=fake), \
             patch("nexus.commands.catalog._get_catalog_writer", return_value=writer):
            result = CliRunner().invoke(
                main,
                ["catalog", "owners", "--census", "--execute", "deactivate",
                 "--no-dry-run", "--confirm"],
            )
        assert result.exit_code == 0, result.output
        writer.deactivate_owner.assert_not_called()
        assert "No eligible owners to deactivate" in result.output
        assert "live document(s)" in result.output

    def test_path_exists_empty_owners_are_never_deactivated(self, tmp_path):
        """Eligibility is scoped to path_vanished ONLY -- path_exists_empty
        never reaches the mutation arm's candidate set at all, regardless of
        --confirm."""
        empty = tmp_path / "empty"
        empty.mkdir()
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners_by_type.return_value = [_owner("1.2", "e", str(empty))]
        fake.by_owner.return_value = []
        writer = MagicMock()
        with patch("nexus.commands.catalog._get_catalog", return_value=fake), \
             patch("nexus.commands.catalog._get_catalog_writer", return_value=writer):
            result = CliRunner().invoke(
                main,
                ["catalog", "owners", "--census", "--execute", "deactivate",
                 "--no-dry-run", "--confirm"],
            )
        assert result.exit_code == 0, result.output
        writer.deactivate_owner.assert_not_called()
        assert "No eligible owners to deactivate" in result.output

    def test_deactivate_failure_is_isolated_and_reported(self, tmp_path):
        fake = self._fake_with_one_vanished(tmp_path)
        writer = MagicMock()
        writer.deactivate_owner.side_effect = RuntimeError("engine unreachable")
        with patch("nexus.commands.catalog._get_catalog", return_value=fake), \
             patch("nexus.commands.catalog._get_catalog_writer", return_value=writer):
            result = CliRunner().invoke(
                main,
                ["catalog", "owners", "--census", "--execute", "deactivate",
                 "--no-dry-run", "--confirm"],
            )
        assert result.exit_code == 1
        assert "engine unreachable" in result.output
        writer.close.assert_called_once()

    def test_no_eligible_owners_skips_writer_construction_entirely(self, tmp_path):
        """will_act=True but an empty eligible set must not even construct a
        writer -- there is nothing to mutate."""
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners_by_type.return_value = []
        with patch("nexus.commands.catalog._get_catalog", return_value=fake), \
             patch("nexus.commands.catalog._get_catalog_writer") as writer_factory:
            result = CliRunner().invoke(
                main,
                ["catalog", "owners", "--census", "--execute", "deactivate",
                 "--no-dry-run", "--confirm"],
            )
        assert result.exit_code == 0, result.output
        writer_factory.assert_not_called()
        assert "No eligible owners to deactivate" in result.output

    def test_report_shows_exact_hal_gated_command(self, tmp_path):
        """Bead directive: 'print the exact command in the dry-run output'."""
        fake = self._fake_with_one_vanished(tmp_path)
        with patch("nexus.commands.catalog._get_catalog", return_value=fake):
            result = CliRunner().invoke(main, ["catalog", "owners", "--census"])
        assert result.exit_code == 0, result.output
        assert (
            "nx catalog owners --census --execute deactivate --no-dry-run --confirm"
            in result.output
        )


class TestOwnersDeactivateTOCTOU:
    """nexus-cw262 round-3 critique (T2 21467 Significant-1): re-verify
    eligibility IMMEDIATELY before EACH mutating write, not just once at
    classification time -- the reconcile_stale._assert_empty_manifest
    precedent."""

    def test_recheck_skips_owner_whose_path_reappeared(self, tmp_path):
        """Path re-classification changed between the census pass and the
        write (e.g. a volume remounted mid-run) -- must skip, not deactivate."""
        vanished = tmp_path / "vanished"
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners_by_type.return_value = [_owner("1.1", "v", str(vanished))]
        fake.by_owner.return_value = []
        writer = MagicMock()

        import nexus.commands.catalog_cmds.owners as owners_mod

        calls = {"n": 0}

        def _fake_classify(root):
            calls["n"] += 1
            # 1st call: bucket-building in _run_census (must read path_vanished
            # so the row becomes eligible). 2nd call: the TOCTOU re-check
            # immediately before the write -- simulate the path reappearing.
            return "path_vanished" if calls["n"] == 1 else "healthy"

        with patch.object(owners_mod, "_classify_owner_root", side_effect=_fake_classify), \
             patch("nexus.commands.catalog._get_catalog", return_value=fake), \
             patch("nexus.commands.catalog._get_catalog_writer", return_value=writer):
            result = CliRunner().invoke(
                main,
                ["catalog", "owners", "--census", "--execute", "deactivate",
                 "--no-dry-run", "--confirm"],
            )
        assert result.exit_code == 0, result.output
        writer.deactivate_owner.assert_not_called()
        assert calls["n"] == 2, "expected exactly 2 classify calls: build + re-check"
        assert "skipped at the immediate-pre-write re-check" in result.output
        assert "now classified healthy" in result.output

    def test_recheck_skips_owner_with_documents_registered_since_classification(
        self, tmp_path
    ):
        """by_owner's SECOND call (the re-check) returns live documents that
        did not exist at classification time -- must skip, not deactivate."""
        vanished = tmp_path / "vanished"
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners_by_type.return_value = [_owner("1.1", "v", str(vanished))]

        class _Doc:
            chunk_count = 2

        # 1st call: corroboration during the census pass (0 docs -> eligible).
        # 2nd call: the TOCTOU re-check immediately before the write.
        fake.by_owner.side_effect = [[], [_Doc()]]
        writer = MagicMock()
        with patch("nexus.commands.catalog._get_catalog", return_value=fake), \
             patch("nexus.commands.catalog._get_catalog_writer", return_value=writer):
            result = CliRunner().invoke(
                main,
                ["catalog", "owners", "--census", "--execute", "deactivate",
                 "--no-dry-run", "--confirm"],
            )
        assert result.exit_code == 0, result.output
        writer.deactivate_owner.assert_not_called()
        assert fake.by_owner.call_count == 2
        assert "now has 1 live document(s)" in result.output

    def test_recheck_read_failure_skips_rather_than_deactivates(self, tmp_path):
        """The re-check's own by_owner call can fail independently of the
        first (classification-time) call -- must skip, never fall back to
        treating an unreadable re-check as still-eligible."""
        vanished = tmp_path / "vanished"
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners_by_type.return_value = [_owner("1.1", "v", str(vanished))]
        fake.by_owner.side_effect = [[], RuntimeError("transient network blip")]
        writer = MagicMock()
        with patch("nexus.commands.catalog._get_catalog", return_value=fake), \
             patch("nexus.commands.catalog._get_catalog_writer", return_value=writer):
            result = CliRunner().invoke(
                main,
                ["catalog", "owners", "--census", "--execute", "deactivate",
                 "--no-dry-run", "--confirm"],
            )
        assert result.exit_code == 0, result.output
        writer.deactivate_owner.assert_not_called()
        assert "re-check read failed" in result.output

    def test_recheck_passes_and_deactivates_when_nothing_changed(self, tmp_path):
        """Kill control: when nothing changed between classification and the
        write, the re-check must not block a genuinely-eligible owner."""
        vanished = tmp_path / "vanished"
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners_by_type.return_value = [_owner("1.1", "v", str(vanished))]
        fake.by_owner.return_value = []  # every call returns 0 docs
        writer = MagicMock()
        writer.deactivate_owner.return_value = True
        with patch("nexus.commands.catalog._get_catalog", return_value=fake), \
             patch("nexus.commands.catalog._get_catalog_writer", return_value=writer):
            result = CliRunner().invoke(
                main,
                ["catalog", "owners", "--census", "--execute", "deactivate",
                 "--no-dry-run", "--confirm"],
            )
        assert result.exit_code == 0, result.output
        writer.deactivate_owner.assert_called_once_with("1.1")
        assert "deactivated 1 owner(s)" in result.output
        assert "skipped at the immediate-pre-write re-check" not in result.output


class TestOwnersReactivateMutationArm:
    """nexus-cw262 round-3 critique (T2 21467 Critical mitigation (a) /
    Significant-5): the undo affordance, double-gated like every other
    mutation arm in this module."""

    def test_execute_reactivate_without_owner_is_refused(self):
        result = CliRunner().invoke(
            main, ["catalog", "owners", "--execute", "reactivate"]
        )
        assert result.exit_code != 0
        assert "requires --owner" in result.output

    def test_bare_reactivate_refuses_to_mutate(self):
        """--execute reactivate --owner X alone (no --no-dry-run/--confirm)
        must report only, never construct a writer."""
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners.return_value = [
            {"tumbler_prefix": "1.1", "name": "x", "deactivated_at": "2026-08-05T00:00:00Z"},
        ]
        with patch("nexus.commands.catalog._get_catalog", return_value=fake), \
             patch("nexus.commands.catalog._get_catalog_writer") as writer_factory:
            result = CliRunner().invoke(
                main, ["catalog", "owners", "--execute", "reactivate", "--owner", "1.1"]
            )
        assert result.exit_code == 0, result.output
        writer_factory.assert_not_called()
        assert "dry-run" in result.output
        assert "currently deactivated" in result.output

    def test_no_dry_run_without_confirm_refuses_to_mutate(self):
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners.return_value = []
        with patch("nexus.commands.catalog._get_catalog", return_value=fake), \
             patch("nexus.commands.catalog._get_catalog_writer") as writer_factory:
            result = CliRunner().invoke(
                main,
                ["catalog", "owners", "--execute", "reactivate", "--owner", "1.1",
                 "--no-dry-run"],
            )
        assert result.exit_code == 0, result.output
        writer_factory.assert_not_called()
        assert "report-only" in result.output

    def test_no_dry_run_and_confirm_reactivates_the_named_owner(self):
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners.return_value = [
            {"tumbler_prefix": "1.1", "name": "x", "deactivated_at": "2026-08-05T00:00:00Z"},
        ]
        writer = MagicMock()
        writer.reactivate_owner.return_value = True
        with patch("nexus.commands.catalog._get_catalog", return_value=fake), \
             patch("nexus.commands.catalog._get_catalog_writer", return_value=writer):
            result = CliRunner().invoke(
                main,
                ["catalog", "owners", "--execute", "reactivate", "--owner", "1.1",
                 "--no-dry-run", "--confirm"],
            )
        assert result.exit_code == 0, result.output
        writer.reactivate_owner.assert_called_once_with("1.1")
        writer.close.assert_called_once()
        assert "Done: reactivated 1.1" in result.output

    def test_reactivate_already_active_owner_reports_no_change(self):
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners.return_value = [
            {"tumbler_prefix": "1.1", "name": "x", "deactivated_at": None},
        ]
        writer = MagicMock()
        writer.reactivate_owner.return_value = False
        with patch("nexus.commands.catalog._get_catalog", return_value=fake), \
             patch("nexus.commands.catalog._get_catalog_writer", return_value=writer):
            result = CliRunner().invoke(
                main,
                ["catalog", "owners", "--execute", "reactivate", "--owner", "1.1",
                 "--no-dry-run", "--confirm"],
            )
        assert result.exit_code == 0, result.output
        assert "No change" in result.output

    def test_reactivate_failure_is_reported_and_exits_nonzero(self):
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners.return_value = []
        writer = MagicMock()
        writer.reactivate_owner.side_effect = RuntimeError("engine unreachable")
        with patch("nexus.commands.catalog._get_catalog", return_value=fake), \
             patch("nexus.commands.catalog._get_catalog_writer", return_value=writer):
            result = CliRunner().invoke(
                main,
                ["catalog", "owners", "--execute", "reactivate", "--owner", "1.1",
                 "--no-dry-run", "--confirm"],
            )
        assert result.exit_code == 1
        assert "engine unreachable" in result.output
        writer.close.assert_called_once()

    def test_reactivate_json_flag_still_refused_together(self):
        result = CliRunner().invoke(
            main,
            ["catalog", "owners", "--json", "--execute", "reactivate", "--owner", "1.1"],
        )
        assert result.exit_code != 0
        assert "cannot be combined" in result.output


class TestCapabilityHonesty:
    """nexus-cw262 round-3 critique (T2 21467 Significant-2): mutation_status
    and the printed command must reflect whether the CONNECTED engine
    actually carries the route -- never a hardcoded claim."""

    def test_available_when_owner_dicts_carry_the_key(self, tmp_path):
        vanished = tmp_path / "vanished"
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners_by_type.return_value = [_owner("1.1", "v", str(vanished))]
        fake.by_owner.return_value = []
        with patch("nexus.commands.catalog._get_catalog", return_value=fake):
            result = CliRunner().invoke(main, ["catalog", "owners", "--census", "--json"])
        data = json.loads(result.stdout)
        assert data["mutation_status"] == "available"

    def test_unavailable_when_engine_predates_the_route(self, tmp_path):
        """A pre-cw262 engine's owner dict carries NO deactivated_at key at
        all -- the wire-shape signal nexus.repos.owner_deactivate_capability
        reads. The report must say the mutation arm requires an upgrade, and
        must NOT print the bare execute command as if it would work."""
        vanished = tmp_path / "vanished"
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners_by_type.return_value = [
            {"tumbler_prefix": "1.1", "name": "v", "owner_type": "repo",
             "repo_root": str(vanished)},  # no "deactivated_at" key at all
        ]
        fake.by_owner.return_value = []
        with patch("nexus.commands.catalog._get_catalog", return_value=fake):
            result = CliRunner().invoke(main, ["catalog", "owners", "--census"])
        assert result.exit_code == 0, result.output
        assert "requires an engine build" in result.output
        assert "not yet deployed" in result.output

        with patch("nexus.commands.catalog._get_catalog", return_value=fake):
            json_result = CliRunner().invoke(
                main, ["catalog", "owners", "--census", "--json"]
            )
        data = json.loads(json_result.stdout)
        assert data["mutation_status"] == "unavailable"

    def test_unknown_when_no_owners_to_read_the_signal_from(self):
        fake = MagicMock(spec=HttpCatalogClient)
        fake.list_owners_by_type.return_value = []
        with patch("nexus.commands.catalog._get_catalog", return_value=fake):
            result = CliRunner().invoke(main, ["catalog", "owners", "--census", "--json"])
        data = json.loads(result.stdout)
        assert data["mutation_status"] == "unknown"
