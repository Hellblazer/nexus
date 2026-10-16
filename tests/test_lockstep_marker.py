# SPDX-License-Identifier: AGPL-3.0-or-later
"""The CLI side of the RDR-143 version-lockstep marker (nexus-sv2wx.28).

The marker is the CLI version last confirmed to match the plugin. The plugin's setup writes it
after a build it has checked (``conexus/launch/nx_setup.py``, which runs before any CLI exists
and so cannot import this module); ``nx self install --bootstrap`` writes it for the installer
scripts, which build the CLI without the plugin's setup. The SessionStart hook compares it with
the plugin's version, and a missing marker makes the first session announce an upgrade and
dispatch one.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus import lockstep_marker


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NX_LOCKSTEP_MARKER", raising=False)
    monkeypatch.delenv("NEXUS_CONFIG_DIR", raising=False)


def test_the_path_is_the_one_the_hook_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    assert lockstep_marker.marker_path() == tmp_path / "cfg" / "cli_lockstep_marker"


def test_the_marker_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(tmp_path / "elsewhere"))
    assert lockstep_marker.marker_path() == tmp_path / "elsewhere"


def test_write_then_read_round_trips_and_makes_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(tmp_path / "a" / "b" / "marker"))
    assert lockstep_marker.read_marker() is None
    lockstep_marker.write_marker("7.81.0")
    assert (tmp_path / "a" / "b" / "marker").read_text(encoding="utf-8") == "7.81.0"
    assert lockstep_marker.read_marker() == "7.81.0"


def test_write_replaces_an_older_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(tmp_path / "marker"))
    lockstep_marker.write_marker("7.80.0")
    lockstep_marker.write_marker("7.81.0")
    assert lockstep_marker.read_marker() == "7.81.0"


def test_an_empty_or_unreadable_marker_reads_as_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "marker"
    monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(marker))
    marker.write_text("  \n", encoding="utf-8")
    assert lockstep_marker.read_marker() is None
    marker.unlink()
    marker.mkdir()  # reading a directory fails
    assert lockstep_marker.read_marker() is None


def test_the_upgrade_ladders_precondition_reads_the_same_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One reader, so the precondition and the writer cannot name different files."""
    from nexus.upgrade_ladder import preconditions  # noqa: PLC0415 -- heavy import, needed by this test only

    monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(tmp_path / "marker"))
    lockstep_marker.write_marker("7.81.0")
    assert preconditions._default_lockstep_marker() == "7.81.0"
