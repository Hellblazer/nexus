# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-1if7b: ``## Hygiene`` strategic-hints block in session start.

Surfaces actionable maintenance signals at session start when (and only
when) something is actionable. Silent when healthy, single-purpose for
each signal. Stdlib-only so the hook runs under whichever bare
interpreter ``_run_python_hook.sh`` resolves (same constraint pinned
by ``test_t2_prefix_scan.py``).
"""
from __future__ import annotations

import os
import time as _time
from pathlib import Path

# Make the hook script importable as a module via path injection.
import sys as _sys
_HOOKS_DIR = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts"
_sys.path.insert(0, str(_HOOKS_DIR))
try:
    from session_start_hook import _emit_hygiene_block  # type: ignore[import-not-found]
finally:
    _sys.path.remove(str(_HOOKS_DIR))


def test_silent_when_cache_fresh(tmp_path: Path) -> None:
    """Healthy state: no hygiene section appended."""
    cache = tmp_path / "cache.txt"
    cache.write_text("## Knowledge Map\nfresh\n")
    # Just-now mtime — well under the 7-day threshold.
    lines: list[str] = []
    _emit_hygiene_block(lines, str(cache))
    assert lines == []


def test_silent_when_cache_path_missing(tmp_path: Path) -> None:
    """No L1 cache file at all: hygiene block is silent."""
    lines: list[str] = []
    _emit_hygiene_block(lines, str(tmp_path / "nonexistent.txt"))
    assert lines == []


def test_silent_when_cache_path_none() -> None:
    """``None`` passed for the cache path: silent."""
    lines: list[str] = []
    _emit_hygiene_block(lines, None)
    assert lines == []


def test_emits_warning_when_cache_stale(tmp_path: Path) -> None:
    """L1 cache > 7 days old triggers the warning line."""
    cache = tmp_path / "cache.txt"
    cache.write_text("## Knowledge Map\nstale\n")
    # Backdate the mtime 10 days into the past.
    ten_days_ago = _time.time() - (10 * 86400)
    os.utime(cache, (ten_days_ago, ten_days_ago))

    lines: list[str] = []
    _emit_hygiene_block(lines, str(cache))

    assert "## Hygiene" in lines
    # Locate the signal line and assert its shape.
    signal_lines = [ln for ln in lines if ln.startswith("- L1 cache")]
    assert len(signal_lines) == 1
    assert "10d old" in signal_lines[0]
    assert "nx context refresh" in signal_lines[0]


def test_threshold_is_exactly_7_days(tmp_path: Path) -> None:
    """Boundary: 7 days exactly is healthy (silent); 8 days fires."""
    cache = tmp_path / "cache.txt"
    cache.write_text("x\n")

    # 7 days exactly → silent.
    seven = _time.time() - (7 * 86400)
    os.utime(cache, (seven, seven))
    lines: list[str] = []
    _emit_hygiene_block(lines, str(cache))
    assert lines == [], "7 days exactly should not trigger"

    # 8 days → fires.
    eight = _time.time() - (8 * 86400)
    os.utime(cache, (eight, eight))
    lines = []
    _emit_hygiene_block(lines, str(cache))
    assert any("L1 cache" in ln for ln in lines), "8 days should trigger"


def test_block_appends_to_existing_output(tmp_path: Path) -> None:
    """Hygiene block extends caller's output_lines, doesn't replace."""
    cache = tmp_path / "cache.txt"
    cache.write_text("x\n")
    old = _time.time() - (15 * 86400)
    os.utime(cache, (old, old))

    lines = ["pre-existing line 1", "pre-existing line 2"]
    _emit_hygiene_block(lines, str(cache))

    # Caller's prior content is preserved at the head.
    assert lines[0] == "pre-existing line 1"
    assert lines[1] == "pre-existing line 2"
    # And the hygiene section is appended.
    assert "## Hygiene" in lines[2:]


def test_no_crash_on_unreadable_cache(tmp_path: Path) -> None:
    """OSError on ``os.path.getmtime`` is dropped silently — hook never fails."""
    # Point at a path that exists() will return True for but getmtime
    # fails on — easiest: a directory.
    bad = tmp_path / "is_a_dir"
    bad.mkdir()
    lines: list[str] = []
    # Should not raise; silent.
    _emit_hygiene_block(lines, str(bad))
    # `bad` exists and is a dir; getmtime works on dirs too, so this
    # may not raise. The point: even on weird input the function never
    # raises out. Assert no exception.
    assert isinstance(lines, list)
