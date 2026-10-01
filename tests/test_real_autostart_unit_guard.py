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


# ── the real home is captured once, never recomputed from env at finish ─────


def test_the_guards_real_home_survives_a_leaked_fence_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A test that installs a fence and fails to restore NX_FENCED_HOME used to
    make the finish-time guards read the fenced mirror (3478 phantom REMOVED
    paths against an intact real ~/.config/nexus). The session's own capture
    wins over whatever the environment says by then."""
    import tests.conftest as conftest_mod

    real = tmp_path / "real"
    fence = tmp_path / "fence"
    real.mkdir()
    fence.mkdir()
    monkeypatch.setattr(conftest_mod, "_session_real_home", real)
    monkeypatch.setattr(conftest_mod, "_session_fenced_home", fence)
    monkeypatch.setenv("HOME", str(fence))
    # the leak: env now names some OTHER fence and some OTHER real home
    monkeypatch.setenv("NX_FENCED_HOME", str(tmp_path / "leaked-fence"))
    monkeypatch.setenv("NX_REAL_HOME", str(tmp_path / "leaked-real"))
    monkeypatch.delenv("NX_REAL_CONFIG_DIR_FOR_GUARD_TEST", raising=False)

    assert conftest_mod._real_home_for_guard() == real


def test_a_test_that_patches_path_home_still_gets_its_own_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests.conftest as conftest_mod

    monkeypatch.setattr(conftest_mod, "_session_real_home", tmp_path / "real")
    monkeypatch.setattr(conftest_mod, "_session_fenced_home", tmp_path / "fence")
    monkeypatch.delenv("NX_REAL_CONFIG_DIR_FOR_GUARD_TEST", raising=False)
    with patch.object(Path, "home", return_value=tmp_path / "elsewhere"):
        assert conftest_mod._real_home_for_guard() == tmp_path / "elsewhere"


# ── scope: legacy T2 units, the enable symlink, and the manager's own state ──


def test_the_snapshot_covers_the_legacy_t2_units_and_the_enable_symlink(tmp_path: Path) -> None:
    _write(tmp_path, ".config/systemd/user/nexus-t2.service", "[Service]\n")
    _write(tmp_path, ".config/systemd/user/nexus-t2.service.d/x.conf", "[Service]\n")
    _write(tmp_path, "Library/LaunchAgents/com.nexus.t2.plist", "<plist/>\n")
    wants = tmp_path / ".config/systemd/user/default.target.wants"
    wants.mkdir(parents=True)
    unit = _write(tmp_path, ".config/systemd/user/nexus-service.service", "[Service]\n")
    (wants / "nexus-service.service").symlink_to(unit)

    with patch.object(Path, "home", return_value=tmp_path):
        before = _snapshot_real_autostart_units()
        assert {
            ".config/systemd/user/nexus-t2.service",
            ".config/systemd/user/nexus-t2.service.d/x.conf",
            "Library/LaunchAgents/com.nexus.t2.plist",
            ".config/systemd/user/default.target.wants/nexus-service.service",
            ".config/systemd/user/nexus-service.service",
        } <= set(before)
        # `systemctl --user disable` removes the symlink and leaves the unit file:
        (wants / "nexus-service.service").unlink()
        after = _snapshot_real_autostart_units()
    assert _diff_autostart_snapshots(before, after) == [
        "REMOVED .config/systemd/user/default.target.wants/nexus-service.service",
    ]


def _shim_dir(tmp_path: Path, systemctl_body: str, launchctl_body: str, sub: str = "mgr-bin") -> Path:
    d = tmp_path / sub
    d.mkdir()
    for name, body in (("systemctl", systemctl_body), ("launchctl", launchctl_body)):
        p = d / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(0o755)
    return d


def test_the_manager_probe_reads_state_and_tolerates_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests.conftest as conftest_mod

    monkeypatch.delenv("NX_NO_MANAGER_STATE_PROBE", raising=False)
    monkeypatch.setattr(conftest_mod, "_real_manager_env", {})
    shims = _shim_dir(
        tmp_path,
        'printf "NRestarts=3\\nUnitFileState=enabled\\nFragmentPath=/u/nexus-service.service\\nOther=x\\n"\n',
        'printf "com.nexus.service = {\\n\\tstate = running\\n\\tpid = 9\\n}\\n"\n',
    )
    monkeypatch.setenv("PATH", f"{shims}{os.pathsep}/usr/bin{os.pathsep}/bin")
    assert conftest_mod._snapshot_manager_state() == {
        "systemd:NRestarts": "3",
        "systemd:UnitFileState": "enabled",
        "systemd:FragmentPath": "/u/nexus-service.service",
        "launchd": "loaded",
        "launchd:state": "running",
    }

    # absent: no user bus / label not loaded. Not an error, a value.
    absent = _shim_dir(
        tmp_path, "echo 'Failed to connect to bus' >&2\nexit 1\n", "exit 113\n", sub="absent-bin",
    )
    monkeypatch.setenv("PATH", f"{absent}{os.pathsep}/usr/bin{os.pathsep}/bin")
    assert conftest_mod._snapshot_manager_state() == {
        "systemd": "unavailable", "launchd": "absent",
    }

    monkeypatch.setenv("NX_NO_MANAGER_STATE_PROBE", "1")
    assert conftest_mod._snapshot_manager_state() == {}


def test_the_manager_probe_asks_the_real_bus_not_the_fenced_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fence replaces XDG_RUNTIME_DIR; the probe must use the value captured
    before it, or it would read the fenced void and report 'unavailable' forever."""
    import tests.conftest as conftest_mod

    monkeypatch.delenv("NX_NO_MANAGER_STATE_PROBE", raising=False)
    shims = _shim_dir(
        tmp_path,
        'echo "UnitFileState=$XDG_RUNTIME_DIR"\n',
        "exit 113\n",
    )
    monkeypatch.setenv("PATH", f"{shims}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/fenced/void")
    monkeypatch.setattr(conftest_mod, "_real_manager_env", {"XDG_RUNTIME_DIR": "/run/user/real"})
    assert conftest_mod._snapshot_manager_state()["systemd:UnitFileState"] == "/run/user/real"


def test_manager_diff_fails_on_state_change_and_only_notes_a_rising_restart_count() -> None:
    import tests.conftest as conftest_mod

    base = {"systemd:NRestarts": "3", "systemd:UnitFileState": "enabled", "launchd": "absent"}
    assert conftest_mod._diff_manager_state(base, dict(base)) == ([], [])
    changes, notes = conftest_mod._diff_manager_state(
        base, {**base, "systemd:NRestarts": "9"},
    )
    assert changes == [] and notes == ["systemd:NRestarts 3 -> 9"]
    changes, notes = conftest_mod._diff_manager_state(
        base, {**base, "systemd:UnitFileState": "disabled", "launchd": "loaded"},
    )
    assert notes == []
    assert changes == [
        "launchd 'absent' -> 'loaded'",
        "systemd:UnitFileState 'enabled' -> 'disabled'",
    ]


def test_a_manager_state_change_reddens_a_real_run(tmp_path: Path) -> None:
    """End to end through the real conftest hooks: the unit FILE is untouched,
    but the (shim) manager reports the unit disabled at session finish."""
    home = tmp_path / "home"
    (home / ".config" / "nexus").mkdir(parents=True)
    _write(home, _UNIT, "[Service]\nExecStart=/real/nx\n")
    counter = tmp_path / "calls"
    shims = _shim_dir(
        tmp_path,
        f'n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "{counter}"\n'
        'if [ "$n" -le 1 ]; then echo "UnitFileState=enabled"; else echo "UnitFileState=disabled"; fi\n',
        "exit 113\n",
    )
    plugin_dir = tmp_path / "probeplug"
    plugin_dir.mkdir()
    (plugin_dir / "nxprobe_mgr.py").write_text("")
    env = dict(os.environ)
    env["NX_REAL_CONFIG_DIR_FOR_GUARD_TEST"] = str(home)
    env["NX_BUILD_LEASE_ROOT"] = str(tmp_path / "build-lease")
    env.pop("NX_BUILD_LEASE_WAIT", None)
    env.pop("NX_NO_MANAGER_STATE_PROBE", None)
    env["PATH"] = f"{shims}{os.pathsep}{env['PATH']}"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(plugin_dir), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", _INNER_TARGET, "-q", "-p", "nxprobe_mgr",
         "--collect-only", "-p", "no:xdist"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    out = proc.stdout + proc.stderr
    assert "FAIL: nexus-q81g7" in out, out[-3000:]
    assert "MANAGER systemd:UnitFileState" in out, out[-3000:]
    assert proc.returncode != 0, out[-3000:]
