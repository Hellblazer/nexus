# SPDX-License-Identifier: AGPL-3.0-or-later
"""Doctor index-log check (2026-07-15 fix): the check watched only the
git-HOOK append log (``index.log``) and reported "last write 460 hours ago"
during a session with two live index runs — real runs write per-run rotated
logs at ``logs/index-*.log``. It now reports the newest of either surface,
saying which one it is.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from nexus.health import _check_index_log


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "logs").mkdir()
    return tmp_path


def _touch(path: Path, age_s: float) -> None:
    path.write_text("x")
    ts = time.time() - age_s
    os.utime(path, (ts, ts))


def test_reports_newer_run_log_over_stale_hook_log(config_dir: Path) -> None:
    _touch(config_dir / "index.log", age_s=460 * 3600)  # the incident shape
    _touch(config_dir / "logs" / "index-nexus-abc123.log", age_s=120)

    (result,) = _check_index_log()

    assert result.ok
    assert "index-nexus-abc123.log" in result.detail
    assert "run log" in result.detail
    assert "hours ago" not in result.detail


def test_reports_hook_log_when_it_is_newest(config_dir: Path) -> None:
    _touch(config_dir / "index.log", age_s=30)
    _touch(config_dir / "logs" / "index-nexus-abc123.log", age_s=7200)

    (result,) = _check_index_log()

    assert result.ok
    assert "hook log" in result.detail


def test_no_logs_reports_honestly(config_dir: Path) -> None:
    (result,) = _check_index_log()

    assert result.ok
    assert "no index activity recorded yet" in result.detail


def test_run_logs_only_no_hook_log(config_dir: Path) -> None:
    _touch(config_dir / "logs" / "index-nexus-abc123.log", age_s=90)

    (result,) = _check_index_log()

    assert result.ok
    assert "run log" in result.detail


# ── warning surfacing (nexus-lgdel follow-on) ────────────────────────────────
#
# The check above returned ok=True UNCONDITIONALLY: it reported recency and
# never read content. That is the nexus-moht0 vacuous-gate shape — a check
# that cannot fail. It mattered because the hook log is the ONLY sink for a
# DETACHED background `nx index repo` run, so it is also the only place that
# run's warnings land. On a working box it had silently accumulated 1528
# `aspect_source_path_uncanonical` warnings and 49 `manifest_write_many_failed`
# events, none of which any surface reported.

_HEADER = "=== nx index post-commit /repo 2026-08-22T07:00:00+0000 ===\n"


def _warn_line(event: str) -> str:
    return (
        f"event='{event}' timestamp='2026-08-22T14:00:00Z' level='warning' "
        "collection='rdr__1-1__voyage-context-3__v1'\n"
    )


def test_warnings_in_the_last_run_are_surfaced(config_dir: Path) -> None:
    (config_dir / "index.log").write_text(
        _HEADER + _warn_line("aspect_source_path_uncanonical") * 3
        + _warn_line("manifest_write_many_failed")
    )

    (result,) = _check_index_log()

    assert result.ok is False and result.warn is True
    assert "aspect_source_path_uncanonical x3" in result.detail
    assert "manifest_write_many_failed x1" in result.detail
    assert any("sed -n" in f for f in result.fix_suggestions)


def test_warnings_from_EARLIER_runs_do_not_nag(config_dir: Path) -> None:
    """Scoped to the last stamped run, so history cannot warn forever.

    Without this the 1528 historical warnings above would make doctor warn on
    every invocation for as long as the file survived, which is how a real
    signal becomes something people learn to ignore.
    """
    (config_dir / "index.log").write_text(
        _HEADER + _warn_line("aspect_source_path_uncanonical") * 500
        + _HEADER + "event='indexer_done' level='info'\n"
    )

    (result,) = _check_index_log()

    assert result.ok is True
    # NB: match on the emitted phrase, not the bare word — pytest's tmp_path
    # is named after the test, so "warnings" appears in the path itself.
    assert "emitted warnings" not in result.detail


def test_clean_last_run_stays_ok(config_dir: Path) -> None:
    (config_dir / "index.log").write_text(_HEADER + "event='x' level='info'\n")

    (result,) = _check_index_log()

    assert result.ok is True


def test_unreadable_log_never_fails_the_check(config_dir: Path) -> None:
    """Doctor must not fail on its own telemetry being unreadable."""
    log = config_dir / "index.log"
    log.write_text(_HEADER)
    log.chmod(0o000)
    try:
        (result,) = _check_index_log()
        assert result.ok is True
    finally:
        log.chmod(0o644)
