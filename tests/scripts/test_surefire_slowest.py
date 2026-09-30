# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-rjk2a: per-class timing report for the Java CI job.

Covers ``scripts/surefire_slowest.py``, which the ``service-ci`` job runs under
``if: always()`` so a slow or timed-out run leaves per-class evidence. The
contract under test: rank ``<testsuite time=...>`` descending, print the total,
and NEVER fail on its own (missing or empty directory, malformed XML).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).parent.parent.parent / "scripts" / "surefire_slowest.py"

spec = importlib.util.spec_from_file_location("surefire_slowest", _SCRIPT)
_mod = importlib.util.module_from_spec(spec)
sys.modules["surefire_slowest"] = _mod
spec.loader.exec_module(_mod)


def _suite(d: Path, name: str, time: str, tests: int = 3) -> None:
    (d / f"TEST-{name}.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name="{name}" time="{time}" tests="{tests}" errors="0" '
        'skipped="0" failures="0"><testcase name="t" time="0.1"/></testsuite>\n',
        encoding="utf-8",
    )


def test_ranks_by_time_descending_and_prints_total(tmp_path, capsys):
    _suite(tmp_path, "dev.nexus.A", "12.5")
    _suite(tmp_path, "dev.nexus.B", "300.25")
    _suite(tmp_path, "dev.nexus.C", "1,234.5")  # surefire locale grouping
    rc = _mod.main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.index("dev.nexus.C") < out.index("dev.nexus.B") < out.index("dev.nexus.A")
    assert "3 classes" in out
    assert "1547.25" in out  # 12.5 + 300.25 + 1234.5


def test_top_limits_the_listing_but_not_the_total(tmp_path, capsys):
    for i in range(5):
        _suite(tmp_path, f"dev.nexus.T{i}", str(10 * (i + 1)))
    assert _mod.main([str(tmp_path), "--top", "2"]) == 0
    out = capsys.readouterr().out
    assert "dev.nexus.T4" in out and "dev.nexus.T3" in out
    assert "dev.nexus.T2" not in out
    assert "5 classes" in out and "150.00" in out


def test_missing_directory_prints_a_line_and_exits_zero(tmp_path, capsys):
    assert _mod.main([str(tmp_path / "nope")]) == 0
    assert "no surefire reports" in capsys.readouterr().out


def test_empty_directory_prints_a_line_and_exits_zero(tmp_path, capsys):
    assert _mod.main([str(tmp_path)]) == 0
    assert "no surefire reports" in capsys.readouterr().out


def test_malformed_report_is_skipped_not_fatal(tmp_path, capsys):
    _suite(tmp_path, "dev.nexus.Good", "5")
    (tmp_path / "TEST-dev.nexus.Bad.xml").write_text("<testsuite", encoding="utf-8")
    assert _mod.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "dev.nexus.Good" in out
    assert "unreadable" in out
