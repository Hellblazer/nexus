# SPDX-License-Identifier: AGPL-3.0-or-later
"""``census_core`` on Windows (nexus-f9bgu.21): its own snapshot, no ``ps``.

The census must see a held generation as held. Its prefix is built with ``/``
boundaries and a Windows argv spells the same directory with ``\\``, so a
snapshot that kept backslashes would under-report holders, the direction that
lets a live tree look free. Platform and API are injected; ``subprocess`` is
armed to explode so a fall-through to ``ps`` fails the test.
"""
from __future__ import annotations

import subprocess

import pytest

from nexus._install import census_core as cc
from tests._win_proc_fake import FakeProc, FakeWinInfoApi

NOW = 2_000_000_000.0


@pytest.fixture(autouse=True)
def _no_ps(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise AssertionError(f"Windows census reached ps: {a!r}")

    monkeypatch.setattr(subprocess, "run", boom)


def _api() -> FakeWinInfoApi:
    return FakeWinInfoApi({
        11: FakeProc(1, NOW, "C:\\t\\gen-A\\bin\\nx.exe",
                     '"C:\\t\\gen-A\\bin\\nx.exe" serve'),
        12: FakeProc(1, NOW, "C:\\t\\gen-B\\bin\\nx.exe", None),  # image fallback
        13: FakeProc(1, NOW, "C:\\other\\x.exe", "C:\\other\\x.exe"),
        14: FakeProc(1, NOW, "C:\\t\\gen-A2\\bin\\nx.exe", "C:\\t\\gen-A2\\bin\\nx.exe"),
    })


def test_snapshot_is_ps_shaped_with_forward_slashes() -> None:
    text = cc.ps_snapshot(platform="win32", win_info_api=_api())
    lines = text.splitlines()
    assert "11 C:/t/gen-A/bin/nx.exe serve" in lines
    assert "12 C:/t/gen-B/bin/nx.exe" in lines
    assert len(lines) == 4  # non-vacuity: every readable process, once


def test_failed_snapshot_is_empty_like_a_psless_box() -> None:
    bad = FakeWinInfoApi({}, snapshot_ok=False)
    assert cc.ps_snapshot(platform="win32", win_info_api=bad) == ""


def test_holders_found_through_the_windows_snapshot() -> None:
    snap = cc.ps_snapshot(platform="win32", win_info_api=_api())
    # Windows-shaped generation: backslashes, normalised by _boundary_text.
    prefix = cc._boundary_text("C:\\t\\gen-A", "win32") + "/"
    assert prefix == "C:/t/gen-A/"
    holders = [
        int(line.split(maxsplit=1)[0]) for line in snap.splitlines() if prefix in line
    ]
    # gen-A holds 11; gen-A2 (a sibling whose name starts the same) does not.
    assert holders == [11]


def test_the_unnormalised_prefix_would_report_no_holders() -> None:
    """The failure the normalisation prevents, stated: a raw backslash prefix
    never occurs in a slash-normalised snapshot."""
    snap = cc.ps_snapshot(platform="win32", win_info_api=_api())
    assert "C:\\t\\gen-A\\" not in snap


@pytest.mark.parametrize(
    ("text", "platform", "expected"),
    [
        ("C:\\t\\gen-A\\", "win32", "C:/t/gen-A"),
        ("C:\\t\\gen-A", "win32", "C:/t/gen-A"),
        ("/opt/tools/gen-A/", "linux", "/opt/tools/gen-A"),
        ("/opt/we\\ird/gen", "linux", "/opt/we\\ird/gen"),  # POSIX unchanged
    ],
)
def test_boundary_text(text: str, platform: str, expected: str) -> None:
    assert cc._boundary_text(text, platform) == expected


def test_posix_snapshot_still_runs_ps(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []

    class R:
        returncode = 0
        stdout = "1 /bin/x\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or R())
    assert cc.ps_snapshot(platform="linux") == "1 /bin/x\n"
    assert calls and calls[0][0] == cc.PS_COMMAND
