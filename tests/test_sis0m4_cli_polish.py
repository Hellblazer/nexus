# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-sis0m.4: exit codes that told a caller the wrong thing
(shakeout 7.64.1 Surface E F8, F11; T2 nexus/shakeout-7.64.1-cli-2026-09-28)."""
from __future__ import annotations

from click.testing import CliRunner

from nexus.cli import main


def test_merge_candidates_json_does_not_exit_0_with_prose():
    """The analysis is unavailable; exiting 0 with prose under --format json
    read as an empty result to a script parsing it."""
    result = CliRunner().invoke(main, ["collection", "merge-candidates", "--format", "json"])
    assert result.exit_code == 1, result.output
    assert "unavailable" in result.output


def test_rdr_verdict_with_a_missing_argument_is_a_usage_error(tmp_path, monkeypatch):
    """A malformed call printed usage at rc 0, which a skill could read as a
    computed gate verdict."""
    (tmp_path / "docs" / "rdr").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["rdr", "preamble", "rdr-verdict", "223"])
    assert result.exit_code == 2, result.output
    assert "Usage" in result.output


def test_daemon_service_status_json_with_no_lease_is_json(tmp_path):
    import json

    result = CliRunner().invoke(
        main, ["daemon", "service", "status", "--json", "--config-dir", str(tmp_path)],
    )
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "no_lease" and payload["running"] is False


def test_session_summary_since_takes_iso_8601_as_well_as_hours(monkeypatch):
    """Every other --since takes ISO 8601; this one took only integer hours."""
    from types import SimpleNamespace

    argvs: list[list[str]] = []

    def _fake_run(argv, timeout):
        argvs.append(argv)
        return SimpleNamespace(stdout="")

    monkeypatch.setattr("nexus.bounded_subprocess.run_bounded", _fake_run)
    from unittest.mock import MagicMock

    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: MagicMock())

    iso = CliRunner().invoke(main, ["catalog", "session-summary", "--since", "2026-09-27T00:00:00"])
    hours = CliRunner().invoke(main, ["catalog", "session-summary", "--since", "48"])
    bad = CliRunner().invoke(main, ["catalog", "session-summary", "--since", "yesterday-ish"])

    assert iso.exit_code == 0, iso.output
    assert hours.exit_code == 0, hours.output
    assert bad.exit_code == 2, bad.output
    assert "--since=2026-09-27T00:00:00" in argvs[0]
    assert "--since=48 hours ago" in argvs[1]


def test_link_density_reads_only_the_seeds_it_samples(monkeypatch):
    """link-density listed every document of every collection before its
    first line of output (4 minutes on a 23k-document catalog). It needs only
    --sample seeds per collection, and says what it is doing first."""
    from unittest.mock import MagicMock

    cat = MagicMock()
    cat.distinct_doc_collections.return_value = ["docs__a", "docs__b"]
    cat.list_by_collection.return_value = []
    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)

    result = CliRunner().invoke(main, ["catalog", "link-density", "--sample", "7"])

    assert result.exit_code == 0, result.output
    for call in cat.list_by_collection.call_args_list:
        assert call.kwargs.get("limit") == 7, call
    assert "Sampling up to 7 seed(s) in each of 2 collection(s)" in result.output
