# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-121 Phase 3: ``nx hook routing-stats`` CLI + aggregation.

Reads the per-rule JSONL log written by routing hooks (via
``conexus/hooks/scripts/routing/_lib.log_routing_event``) and produces a
small report: total fires, allow / deny / escape counts, block-rate
and escape-rate per rule.
"""
from __future__ import annotations

import json
import pathlib

import pytest
from click.testing import CliRunner

from nexus.routing_stats import aggregate, RuleStats


def _write_log(path: pathlib.Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_aggregate_empty_log(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text("")
    assert aggregate(path) == {}


def test_aggregate_missing_log_returns_empty(tmp_path):
    assert aggregate(tmp_path / "absent.jsonl") == {}


def test_aggregate_counts_outcomes_per_rule(tmp_path):
    path = tmp_path / "log.jsonl"
    _write_log(path, [
        {"ts": "t1", "rule": "rule_a", "outcome": "allow"},
        {"ts": "t2", "rule": "rule_a", "outcome": "deny"},
        {"ts": "t3", "rule": "rule_a", "outcome": "deny"},
        {"ts": "t4", "rule": "rule_a", "outcome": "escape"},
        {"ts": "t5", "rule": "rule_b", "outcome": "allow"},
    ])
    stats = aggregate(path)
    assert set(stats.keys()) == {"rule_a", "rule_b"}
    a = stats["rule_a"]
    assert a.total == 4
    assert a.allow == 1
    assert a.deny == 2
    assert a.escape == 1
    assert a.block_rate == pytest.approx(2 / 4)
    assert a.escape_rate == pytest.approx(1 / 4)


def test_aggregate_handles_fail_closed_and_fail_open(tmp_path):
    path = tmp_path / "log.jsonl"
    _write_log(path, [
        {"ts": "t1", "rule": "r", "outcome": "allow_fail_open"},
        {"ts": "t2", "rule": "r", "outcome": "deny_fail_closed"},
    ])
    stats = aggregate(path)["r"]
    assert stats.total == 2
    assert stats.fail_open == 1
    assert stats.fail_closed == 1


def test_aggregate_skips_malformed_lines(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text(
        json.dumps({"ts": "x", "rule": "r", "outcome": "allow"}) + "\n"
        + "{not json\n"
        + json.dumps({"ts": "y", "rule": "r", "outcome": "deny"}) + "\n"
    )
    stats = aggregate(path)["r"]
    assert stats.total == 2


def test_aggregate_ignores_records_without_rule(tmp_path):
    path = tmp_path / "log.jsonl"
    _write_log(path, [
        {"ts": "t1", "outcome": "allow"},  # no rule
        {"ts": "t2", "rule": "r", "outcome": "allow"},
    ])
    assert list(aggregate(path).keys()) == ["r"]


# ---------------------------------------------------------------------------
# RuleStats dataclass behavior
# ---------------------------------------------------------------------------


def test_rule_stats_zero_total_has_zero_rates():
    s = RuleStats(rule="x")
    assert s.total == 0
    assert s.block_rate == 0.0
    assert s.escape_rate == 0.0


# ---------------------------------------------------------------------------
# CLI: `nx hook routing-stats`
# ---------------------------------------------------------------------------


def test_cli_routing_stats_reports_per_rule(tmp_path, monkeypatch):
    log = tmp_path / "log.jsonl"
    _write_log(log, [
        {"ts": "t1", "rule": "grep_for_symbols_redirects_to_serena", "outcome": "deny"},
        {"ts": "t2", "rule": "grep_for_symbols_redirects_to_serena", "outcome": "escape"},
        {"ts": "t3", "rule": "git_add_all_redirects_to_explicit_paths", "outcome": "deny"},
    ])
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(log))

    from nexus.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["hook", "routing-stats"])
    assert result.exit_code == 0, result.output
    assert "grep_for_symbols_redirects_to_serena" in result.output
    assert "git_add_all_redirects_to_explicit_paths" in result.output
    # Report shows numeric columns
    assert "deny" in result.output.lower()


def test_cli_routing_stats_empty_log(tmp_path, monkeypatch):
    log = tmp_path / "log.jsonl"
    log.write_text("")
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(log))

    from nexus.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["hook", "routing-stats"])
    assert result.exit_code == 0
    assert "no" in result.output.lower() or "0" in result.output


def test_cli_routing_stats_json_output(tmp_path, monkeypatch):
    log = tmp_path / "log.jsonl"
    _write_log(log, [
        {"ts": "t1", "rule": "r", "outcome": "deny"},
        {"ts": "t2", "rule": "r", "outcome": "allow"},
    ])
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(log))

    from nexus.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["hook", "routing-stats", "--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert parsed["r"]["total"] == 2
    assert parsed["r"]["deny"] == 1


def test_cli_routing_stats_custom_path(tmp_path):
    log = tmp_path / "elsewhere.jsonl"
    _write_log(log, [{"ts": "t", "rule": "r", "outcome": "deny"}])

    from nexus.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["hook", "routing-stats", "--log-path", str(log)])
    assert result.exit_code == 0
    assert "r" in result.output
