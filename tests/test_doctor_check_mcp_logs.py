# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nx doctor --check-mcp-logs`` (RDR-094 nexus-50u5).

Pin three things:
  * Slug derivation from cwd matches the observed Claude Code cache layout
  * Silent-death + tool-failure signature scanning correctly classifies
    cache JSONL records inside the lookback window
  * The Click-level CLI flow exits 0 with a clean message on platforms
    that don't have ``~/Library/Caches/claude-cli-nodejs`` (Linux/Windows)
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.commands.doctor import (
    _resolve_claude_cache_dir,
    _run_check_mcp_logs,
    _scan_mcp_log_jsonl,
)


def _mk_record(
    *,
    timestamp: str,
    debug: str | None = None,
    error: str | None = None,
    session_id: str = "abc-123",
) -> str:
    rec: dict[str, object] = {"timestamp": timestamp, "sessionId": session_id}
    if debug is not None:
        rec["debug"] = debug
    if error is not None:
        rec["error"] = error
    return json.dumps(rec)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z",
    )


def _epoch(dt: _dt.datetime) -> float:
    return dt.timestamp()


# ── _resolve_claude_cache_dir ───────────────────────────────────────────────


class TestResolveClaudeCacheDir:

    def test_slug_replaces_path_separators(self, tmp_path):
        cwd = Path("/Users/hal.hildebrand/git/nexus")
        result = _resolve_claude_cache_dir(cwd)
        assert result.name == "-Users-hal-hildebrand-git-nexus"
        assert "claude-cli-nodejs" in result.parts

    def test_default_uses_cwd_when_unspecified(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        result = _resolve_claude_cache_dir()
        # tmp_path under /private/var/... or /tmp/... -> slug starts with "-"
        assert result.name.startswith("-")

    def test_root_path_falls_back_to_cache_parent(self):
        result = _resolve_claude_cache_dir(Path("/"))
        # Root cwd produces an empty slug; fall back to parent so
        # caller's exists() check still does the right thing.
        assert result.name == "claude-cli-nodejs"


# ── _scan_mcp_log_jsonl ─────────────────────────────────────────────────────


class TestScanMcpLogJsonl:

    def test_silent_death_signature_matched(self, tmp_path):
        log = tmp_path / "log.jsonl"
        log.write_text(_mk_record(
            timestamp=_now_iso(),
            debug="STDIO connection dropped after 23092s uptime",
        ) + "\n")
        cutoff = _epoch(_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=24))
        sd, tf = _scan_mcp_log_jsonl(log, cutoff)
        assert len(sd) == 1
        assert "STDIO connection dropped after" in sd[0]["signature"]
        assert sd[0]["session_id"] == "abc-123"
        assert tf == []

    def test_transport_error_signature_matched(self, tmp_path):
        log = tmp_path / "log.jsonl"
        log.write_text(_mk_record(
            timestamp=_now_iso(),
            debug="Closing transport (stdio transport error: Error)",
        ) + "\n")
        cutoff = _epoch(_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=24))
        sd, _ = _scan_mcp_log_jsonl(log, cutoff)
        assert len(sd) == 1
        assert sd[0]["signature"] == "stdio transport error"

    def test_abort_error_classified_as_tool_failure(self, tmp_path):
        log = tmp_path / "log.jsonl"
        log.write_text(_mk_record(
            timestamp=_now_iso(),
            debug="Tool 'query' failed after 9s: MCP error -32001: AbortError: ...",
        ) + "\n")
        cutoff = _epoch(_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=24))
        sd, tf = _scan_mcp_log_jsonl(log, cutoff)
        assert sd == []
        assert len(tf) == 1
        assert "AbortError" in tf[0]["signature"]

    def test_records_outside_window_skipped(self, tmp_path):
        old = (
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=48)
        ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        log = tmp_path / "log.jsonl"
        log.write_text(_mk_record(
            timestamp=old,
            debug="STDIO connection dropped after 5s uptime",
        ) + "\n")
        cutoff = _epoch(_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=24))
        sd, tf = _scan_mcp_log_jsonl(log, cutoff)
        assert sd == []
        assert tf == []

    def test_normal_lifecycle_lines_ignored(self, tmp_path):
        log = tmp_path / "log.jsonl"
        log.write_text(
            _mk_record(timestamp=_now_iso(),
                       debug="Successfully connected (transport: stdio) in 467ms")
            + "\n"
            + _mk_record(timestamp=_now_iso(),
                         debug="Calling MCP tool: memory_get")
            + "\n"
            + _mk_record(timestamp=_now_iso(),
                         debug="UNKNOWN connection closed after 14s (cleanly)")
            + "\n"
        )
        cutoff = _epoch(_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=24))
        sd, tf = _scan_mcp_log_jsonl(log, cutoff)
        assert sd == []
        assert tf == []

    def test_malformed_json_lines_swallowed(self, tmp_path):
        log = tmp_path / "log.jsonl"
        log.write_text(
            "not-valid-json\n"
            + _mk_record(timestamp=_now_iso(),
                         debug="STDIO connection dropped after 5s uptime")
            + "\n"
            + "{broken: json\n"
        )
        cutoff = _epoch(_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=24))
        sd, _ = _scan_mcp_log_jsonl(log, cutoff)
        assert len(sd) == 1

    def test_missing_file_returns_empty_lists(self, tmp_path):
        sd, tf = _scan_mcp_log_jsonl(tmp_path / "nonexistent.jsonl", 0.0)
        assert sd == []
        assert tf == []


# ── _run_check_mcp_logs ─────────────────────────────────────────────────────


class TestRunCheckMcpLogs:
    """End-to-end check function. Uses tmp_path to stand in for
    ``~/Library/Caches/claude-cli-nodejs`` so the test is deterministic
    on every platform."""

    def test_skips_cleanly_when_cache_dir_missing(self, tmp_path, capsys):
        nonexistent = tmp_path / "no_cache_here"
        with patch(
            "nexus.commands.doctor._resolve_claude_cache_dir",
            return_value=nonexistent,
        ):
            _run_check_mcp_logs(json_out=False)
        out = capsys.readouterr().out
        assert "not present" in out

    def test_json_output_skip_path_includes_platform_supported_false(
        self, tmp_path, capsys,
    ):
        nonexistent = tmp_path / "no_cache_here"
        with patch(
            "nexus.commands.doctor._resolve_claude_cache_dir",
            return_value=nonexistent,
        ):
            _run_check_mcp_logs(json_out=True)
        payload = json.loads(capsys.readouterr().out)
        assert payload["platform_supported"] is False
        assert payload["silent_deaths"] == []

    def test_silent_death_surfaces_warning_and_remediation(
        self, tmp_path, capsys,
    ):
        cache_dir = tmp_path / "fake_cache"
        log_dir = cache_dir / "mcp-logs-plugin-nx-nexus"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "test.jsonl"
        log_file.write_text(_mk_record(
            timestamp=_now_iso(),
            debug="STDIO connection dropped after 23092s uptime",
            session_id="0c700072-0841-4bd6-9fd7-f2933e33a065",
        ) + "\n")

        with patch(
            "nexus.commands.doctor._resolve_claude_cache_dir",
            return_value=cache_dir,
        ):
            _run_check_mcp_logs(json_out=False)

        out = capsys.readouterr().out
        assert "Silent-death signatures: 1" in out
        assert "Cross-reference these timestamps" in out
        assert "RDR-094" in out
        # Session ID prefix should be visible for cross-referencing.
        assert "0c700072" in out

    def test_clean_scan_reports_no_signatures(self, tmp_path, capsys):
        cache_dir = tmp_path / "fake_cache"
        log_dir = cache_dir / "mcp-logs-plugin-nx-nexus"
        log_dir.mkdir(parents=True)
        (log_dir / "test.jsonl").write_text(_mk_record(
            timestamp=_now_iso(),
            debug="Successfully connected (transport: stdio) in 467ms",
        ) + "\n")
        with patch(
            "nexus.commands.doctor._resolve_claude_cache_dir",
            return_value=cache_dir,
        ):
            _run_check_mcp_logs(json_out=False)
        out = capsys.readouterr().out
        assert "No silent-death or tool-failure signatures" in out

    def test_files_outside_lookback_window_skipped(self, tmp_path, capsys):
        """File mtime older than the window must short-circuit the file."""
        import os as _os

        cache_dir = tmp_path / "fake_cache"
        log_dir = cache_dir / "mcp-logs-plugin-nx-nexus"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "old.jsonl"
        log_file.write_text(_mk_record(
            timestamp=_now_iso(),
            debug="STDIO connection dropped after 5s uptime",
        ) + "\n")
        # Push the file's mtime to 48h ago.
        old_epoch = (
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=48)
        ).timestamp()
        _os.utime(log_file, (old_epoch, old_epoch))

        with patch(
            "nexus.commands.doctor._resolve_claude_cache_dir",
            return_value=cache_dir,
        ):
            _run_check_mcp_logs(json_out=True, hours=24)

        payload = json.loads(capsys.readouterr().out)
        assert payload["log_files_scanned"] == 0
        assert payload["silent_deaths"] == []


# ── Click CLI integration ────────────────────────────────────────────────────


def test_check_mcp_logs_cli_flag_runs():
    """nx doctor --check-mcp-logs runs and exits 0, even on Linux."""
    runner = CliRunner()
    with patch(
        "nexus.commands.doctor._resolve_claude_cache_dir",
        return_value=Path("/nonexistent/claude/cache"),
    ):
        result = runner.invoke(main, ["doctor", "--check-mcp-logs"])
    assert result.exit_code == 0, result.output
    assert "not present" in result.output


def test_check_mcp_logs_respects_custom_window():
    runner = CliRunner()
    with patch(
        "nexus.commands.doctor._resolve_claude_cache_dir",
        return_value=Path("/nonexistent/claude/cache"),
    ):
        result = runner.invoke(
            main,
            ["doctor", "--check-mcp-logs", "--mcp-log-hours", "1"],
        )
    assert result.exit_code == 0, result.output
