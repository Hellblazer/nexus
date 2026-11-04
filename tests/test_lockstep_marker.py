# SPDX-License-Identifier: AGPL-3.0-or-later
"""The CLI side of the RDR-143 version-lockstep marker (nexus-sv2wx.28).

The marker is the CLI version last confirmed to match the plugin. The plugin's setup writes it
after a build it has checked (``conexus/launch/nx_setup.py``, which runs before any CLI exists
and so cannot import this module); ``nx self install --bootstrap`` writes it for the installer
scripts, which build the CLI without the plugin's setup. The SessionStart hook compares it with
the plugin's version; a missing or stale marker falls back to the CLI's own version (nexus-ehg1y).
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


def test_mark_upgrade_retry_replaces_the_version_and_the_marker_stays_unequal_to_any_plugin_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nexus-emiky: the lockstep child that lost the upgrade lock leaves the sentinel where the version was."""
    monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(tmp_path / "marker"))
    lockstep_marker.write_marker("7.94.1")
    lockstep_marker.mark_upgrade_retry("7.94.1")
    assert lockstep_marker.read_marker() == lockstep_marker.UPGRADE_RETRY_MARKER
    assert lockstep_marker.UPGRADE_RETRY_MARKER != "7.94.1"


def test_mark_upgrade_retry_never_raises_on_an_unwritable_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(blocker / "marker"))  # its parent is a file
    lockstep_marker.mark_upgrade_retry("7.94.1")  # must not raise


@pytest.mark.parametrize(
    "held, running, expect_sentinel",
    [
        ("7.94.1", "7.94.1", True),    # the action wrote the plugin's version, the child runs it
        ("7.94.1", "7.95.0", True),    # uv upgraded past the plugin's pin: still this run's marker
        (None, "7.94.1", True),        # nothing there: no newer marker to protect
        ("7.95.0", "7.94.1", False),   # a newer session already confirmed a newer version
        ("7.94.1", None, False),       # the running version is unknown: cannot show the marker is not newer
        ("not-a-version", "7.94.1", False),
    ],
)
def test_mark_upgrade_retry_never_overwrites_a_newer_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held: str | None, running: str | None, expect_sentinel: bool,
) -> None:
    """nexus-emiky review I3: a loser that waited 20 minutes must not put a stale sentinel over a marker a
    newer plugin's action has written since. A snapshot taken when the wait began would be the version BEFORE
    the action's own write (the action writes ~2 s after spawn), so the rule compares with the running CLI."""
    marker = tmp_path / "marker"
    monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(marker))
    if held is not None:
        marker.write_text(held, encoding="utf-8")
    lockstep_marker.mark_upgrade_retry(running)
    after = lockstep_marker.read_marker()
    assert after == (lockstep_marker.UPGRADE_RETRY_MARKER if expect_sentinel else held)


def test_settle_upgrade_retry_replaces_only_the_sentinel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """nexus-emiky review: a completed `nx upgrade` has done the owed work, so the sentinel becomes the
    running version; any other content is not its to touch."""
    marker = tmp_path / "marker"
    monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(marker))
    marker.write_text(lockstep_marker.UPGRADE_RETRY_MARKER, encoding="utf-8")
    lockstep_marker.settle_upgrade_retry("7.95.0")
    assert lockstep_marker.read_marker() == "7.95.0"
    marker.write_text("7.96.0", encoding="utf-8")
    lockstep_marker.settle_upgrade_retry("7.95.0")
    assert lockstep_marker.read_marker() == "7.96.0"
    marker.unlink()
    lockstep_marker.settle_upgrade_retry("7.95.0")
    assert lockstep_marker.read_marker() is None


def test_settle_upgrade_retry_with_an_unknown_running_version_leaves_the_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "marker"
    monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(marker))
    marker.write_text(lockstep_marker.UPGRADE_RETRY_MARKER, encoding="utf-8")
    lockstep_marker.settle_upgrade_retry(None)
    assert lockstep_marker.read_marker() == lockstep_marker.UPGRADE_RETRY_MARKER


def test_running_version_is_the_installed_conexus() -> None:
    from importlib.metadata import version

    assert lockstep_marker.running_version() == version("conexus")
