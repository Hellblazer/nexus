# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``scripts/check_deferral_staleness.py`` cannot itself go vacuous. nexus-arsjx.

Two defects in the script written to prevent this class:

* the open-bead scan read ONE page with no truncation assert -- past the
  limit, check 3 silently under-scanned and the script still exited 0
  (the nexus-moht0 shape, reproduced inside the moht0 remedy);
* a "Deferral-sweep verdict" note suppressed marker findings for its bead
  FOREVER -- a condition judged once and never re-examined, one level up.

``_bd`` is stubbed: these are tests of the sweep's logic, not of bd.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_deferral_staleness.py"


@pytest.fixture
def sweep():
    spec = importlib.util.spec_from_file_location("check_deferral_staleness", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stub_bd(monkeypatch, sweep, *, deferred: dict[str, str], open_lines: list[str]):
    """``deferred`` maps bead id -> `bd show` body; ``open_lines`` is `bd list --status=open`."""
    def fake(*args: str) -> str:
        if args[:2] == ("list", "--status=deferred"):
            return "\n".join(f"{bid} [P2] [task] deferred - x" for bid in deferred)
        if args[:2] == ("list", "--status=open"):
            return "\n".join(open_lines)
        if args[0] == "show":
            return deferred[args[1]]
        raise AssertionError(f"unexpected bd call: {args}")
    monkeypatch.setattr(sweep, "_bd", fake)


def test_a_full_open_page_is_unrunnable_not_clean(sweep, monkeypatch, capsys) -> None:
    n = sweep.OPEN_SCAN_LIMIT
    _stub_bd(
        monkeypatch, sweep, deferred={},
        open_lines=[f"nexus-a{i:05d} [P2] [task] open - fine" for i in range(n)],
    )

    rc = sweep.main()

    assert rc == 2, "a page that filled the limit was reported as a pass"
    assert "UNRUNNABLE" in capsys.readouterr().out


def test_an_open_page_under_the_limit_scans_normally(sweep, monkeypatch, capsys) -> None:
    """Non-vacuity control for the assert above: a real WORLD-BLOCKED title is found."""
    _stub_bd(
        monkeypatch, sweep, deferred={},
        open_lines=["nexus-zzz01 [P2] [task] open - WORLD-BLOCKED: waiting on x"],
    )

    rc = sweep.main()

    assert rc == 1
    assert "WORLD-BLOCKED" in capsys.readouterr().out


def test_a_fresh_dated_verdict_suppresses_marker_findings(sweep, monkeypatch, capsys) -> None:
    today = dt.date.today().isoformat()
    _stub_bd(
        monkeypatch, sweep,
        deferred={"nexus-d1": f"Deferral-sweep verdict {today}: the CLEARED line is fine\nCLEARED\n"},
        open_lines=[],
    )

    rc = sweep.main()

    assert rc == 0, capsys.readouterr().out


def test_an_expired_verdict_no_longer_suppresses(sweep, monkeypatch, capsys) -> None:
    old = (dt.date.today() - dt.timedelta(days=sweep.VERDICT_TTL_DAYS + 1)).isoformat()
    _stub_bd(
        monkeypatch, sweep,
        deferred={"nexus-d2": f"Deferral-sweep verdict {old}: judged once\nCLEARED\n"},
        open_lines=[],
    )

    rc = sweep.main()

    out = capsys.readouterr().out
    assert rc == 1
    assert "older than" in out, out
    assert "CLEARED" in out, "the marker the verdict used to hide must be reported again"


def test_an_undated_verdict_does_not_suppress(sweep, monkeypatch, capsys) -> None:
    _stub_bd(
        monkeypatch, sweep,
        deferred={"nexus-d3": "Deferral-sweep verdict: judged, no date\nCLEARED\n"},
        open_lines=[],
    )

    rc = sweep.main()

    out = capsys.readouterr().out
    assert rc == 1
    assert "undated" in out, out


def test_an_expired_deferral_date_is_never_suppressed(sweep, monkeypatch, capsys) -> None:
    """Unchanged behaviour, pinned: a date is a promise, not a judgment."""
    today = dt.date.today().isoformat()
    _stub_bd(
        monkeypatch, sweep,
        deferred={"nexus-d4": f"Deferral-sweep verdict {today}: fine\nDeferred: 2026-01-01\n"},
        open_lines=[],
    )

    rc = sweep.main()

    assert rc == 1
    assert "expired" in capsys.readouterr().out
