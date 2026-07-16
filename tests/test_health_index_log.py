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
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)
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
