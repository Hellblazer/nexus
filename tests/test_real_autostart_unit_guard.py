# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-q81g7: the session guard that fails a run when a REAL autostart unit
changed.

The HOME fence (``tests/_fence_home.py``) is what keeps a test away from the
operator's ``nexus-service.service`` / ``com.nexus.service.plist``. This guard
is the backstop for a fence ESCAPE, the same relationship the nexus-pfuns
real-config-dir guard has to the config-dir fence: snapshot the real unit files
at session start, compare at session finish, fail the run on any change.

Pure-function cases first, then a child ``pytest`` against THIS repo's real
conftest (the only thing that proves the hooks are registered here at all).
Every "real" home below is a ``tmp_path``; ``NX_REAL_CONFIG_DIR_FOR_GUARD_TEST``
points the guard at it, so nothing here reads or writes the operator's HOME.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.conftest import (
    _diff_autostart_snapshots,
    _snapshot_real_autostart_units,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

_UNIT = ".config/systemd/user/nexus-service.service"
_DROPIN = ".config/systemd/user/nexus-service.service.d/10-bind-all.conf"
_PLIST = "Library/LaunchAgents/com.nexus.service.plist"


def _write(home: Path, rel: str, body: str) -> Path:
    p = home / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return p


class TestSnapshot:
    def test_empty_when_no_unit_exists(self, tmp_path: Path) -> None:
        with patch.object(Path, "home", return_value=tmp_path):
            assert _snapshot_real_autostart_units() == {}

    def test_captures_the_unit_its_dropins_and_the_plist(self, tmp_path: Path) -> None:
        _write(tmp_path, _UNIT, "[Service]\nExecStart=/real/nx\n")
        _write(tmp_path, _DROPIN, "[Service]\n")
        _write(tmp_path, _PLIST, "<plist/>\n")
        # An unrelated unit and plist are none of this guard's business.
        _write(tmp_path, ".config/systemd/user/other.service", "x")
        _write(tmp_path, "Library/LaunchAgents/com.other.plist", "x")

        with patch.object(Path, "home", return_value=tmp_path):
            snap = _snapshot_real_autostart_units()

        assert set(snap) == {_UNIT, _DROPIN, _PLIST}

    def test_a_same_size_same_mtime_rewrite_is_still_seen(self, tmp_path: Path) -> None:
        """mtime+size alone would miss a byte-for-byte same-length rewrite with
        a preserved mtime (``cp -p`` of a backup over the unit)."""
        unit = _write(tmp_path, _UNIT, "AAAA")
        with patch.object(Path, "home", return_value=tmp_path):
            before = _snapshot_real_autostart_units()
            st = unit.stat()
            unit.write_text("BBBB")
            os.utime(unit, ns=(st.st_atime_ns, st.st_mtime_ns))
            after = _snapshot_real_autostart_units()
        assert _diff_autostart_snapshots(before, after) == [f"MODIFIED {_UNIT}"]

    def test_the_snapshot_never_carries_file_content(self, tmp_path: Path) -> None:
        _write(tmp_path, _UNIT, "Environment=NX_SECRET=hunter2\n")
        with patch.object(Path, "home", return_value=tmp_path):
            snap = _snapshot_real_autostart_units()
        assert "hunter2" not in repr(snap)


class TestDiff:
    def test_unchanged_is_empty(self) -> None:
        snap = {_UNIT: (1, 2, "h")}
        assert _diff_autostart_snapshots(snap, dict(snap)) == []

    def test_added_removed_modified(self) -> None:
        before = {_UNIT: (1, 2, "a"), _DROPIN: (1, 2, "b")}
        after = {_UNIT: (9, 2, "z"), _PLIST: (1, 2, "c")}
        assert _diff_autostart_snapshots(before, after) == [
            f"REMOVED {_DROPIN}",
            f"ADDED {_PLIST}",
            f"MODIFIED {_UNIT}",
        ]


# ── end to end: a child pytest against this repo's real conftest ────────────

_PROBE_PLUGIN = '''
import os, pathlib

def pytest_collection_finish(session):
    """Mutate the fake real home's unit mid-session: AFTER the guard's
    sessionstart baseline and BEFORE its sessionfinish check."""
    home = pathlib.Path(os.environ["NX_REAL_CONFIG_DIR_FOR_GUARD_TEST"])
    mode = os.environ["NX_PROBE_MODE"]
    unit = home / ".config" / "systemd" / "user" / "nexus-service.service"
    if mode == "rewrite":
        unit.write_text("[Service]\\nExecStart=/destroyed\\n")
    elif mode == "delete":
        unit.unlink()
    # mode == "none": touch nothing
'''

_INNER_TARGET = "tests/test_pfuns_append_only_logs_do_not_red_a_run.py"


def _run_inner(tmp_path: Path, mode: str, *pytest_args: str) -> tuple[int, str]:
    home = tmp_path / "home"
    (home / ".config" / "nexus").mkdir(parents=True)
    _write(home, _UNIT, "[Service]\nExecStart=/real/nx\n")

    plugin_dir = tmp_path / "probeplug"
    plugin_dir.mkdir()
    (plugin_dir / "nxprobe_q81g7.py").write_text(_PROBE_PLUGIN)

    env = dict(os.environ)
    env["NX_REAL_CONFIG_DIR_FOR_GUARD_TEST"] = str(home)
    env["NX_PROBE_MODE"] = mode
    env["NX_BUILD_LEASE_ROOT"] = str(tmp_path / "build-lease")
    env.pop("NX_BUILD_LEASE_WAIT", None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(plugin_dir), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest", _INNER_TARGET,
            "-q", "-p", "nxprobe_q81g7", "--collect-only", *pytest_args,
        ],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    return proc.returncode, proc.stdout + proc.stderr


@pytest.mark.lint
@pytest.mark.parametrize(
    "xdist_args",
    [pytest.param(("-p", "no:xdist"), id="serial"), pytest.param(("-n", "2"), id="xdist")],
)
@pytest.mark.parametrize("mode", ["rewrite", "delete"])
def test_a_changed_real_unit_reddens_a_real_run(
    tmp_path: Path, mode: str, xdist_args: tuple[str, ...],
) -> None:
    rc, out = _run_inner(tmp_path, mode, *xdist_args)
    assert "FAIL: nexus-q81g7" in out, out[-3000:]
    assert _UNIT in out, out[-3000:]
    assert rc != 0, f"a changed real autostart unit must fail the run (rc={rc})\n{out[-3000:]}"


@pytest.mark.lint
def test_an_untouched_real_unit_does_not_redden_a_run(tmp_path: Path) -> None:
    """The other half, so the FAIL above is not simply always printed."""
    rc, out = _run_inner(tmp_path, "none", "-p", "no:xdist")
    assert "FAIL: nexus-q81g7" not in out, out[-3000:]
    assert rc == 0, f"an untouched unit must not fail the run (rc={rc})\n{out[-3000:]}"
