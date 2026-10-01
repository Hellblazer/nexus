# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-q81g7: no test may reach a real service manager with a mutating verb.

HOME does not isolate service managers (T2 ``project_home_does_not_isolate_launchd``,
2026-08-11; it recurred 2026-09-30). ``launchctl bootout gui/<uid>/<label>`` and
``systemctl --user disable --now <unit>`` are addressed by label / over the bus,
so the HOME fence only hides the unit FILE. Two backstops:

* ``tests/conftest.py::_service_manager_tripwire`` (autouse) wraps
  ``nexus.daemon.installer._run_manager``, the single funnel for every manager
  call, and RAISES on a mutating verb unless the test replaced the spawn
  (``installer.run_bounded``, the module's documented seam), replaced
  ``_run_manager`` itself, or the manager cannot be found at all. Read-only
  probes pass through.
* ``installer._run_manager`` itself refuses mutating verbs when ``NX_FENCED_HOME``
  is set, so a real ``nx`` child of a fenced test or gate refuses too (a conftest
  patch does not reach a child process).

Every manager here is a shim script in ``tmp_path`` that records its argv; the
real ``launchctl`` / ``systemctl`` are never on PATH.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.daemon import installer

_SHIM = "#!/bin/sh\necho \"$0 $*\" >> \"{log}\"\nexit 0\n"


@pytest.fixture
def shims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """PATH holding ONLY recording shims for launchctl and systemctl."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "spawned.log"
    for name in ("launchctl", "systemctl"):
        p = bin_dir / name
        p.write_text(_SHIM.format(log=log))
        p.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    return bin_dir, log


def _spawned(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


# ── classification ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cmd,mutating",
    [
        (["launchctl", "print-disabled", "gui/501"], False),
        (["launchctl", "print", "gui/501/com.nexus.service"], False),
        (["launchctl", "bootstrap", "gui/501", "/x.plist"], True),
        (["launchctl", "bootout", "gui/501/com.nexus.service"], True),
        (["launchctl", "enable", "gui/501/com.nexus.service"], True),
        (["launchctl", "disable", "gui/501/com.nexus.service"], True),
        (["launchctl", "kickstart", "-k", "gui/501/com.nexus.service"], True),
        (["systemctl", "--user", "is-enabled", "nexus-service.service"], False),
        (["systemctl", "--user", "show", "nexus-service.service", "-p", "NRestarts"], False),
        (["systemctl", "--user", "enable", "--now", "nexus-service.service"], True),
        (["systemctl", "--user", "disable", "--now", "nexus-service.service"], True),
        (["systemctl", "--user", "restart", "nexus-service.service"], True),
        (["systemctl", "--user", "daemon-reload"], True),
        # unknown verbs and malformed argv are NOT read-only: deny by default
        (["systemctl", "--user", "frobnicate"], True),
        (["launchctl"], True),
        (["systemctl", "--user"], True),
        (["/sbin/launchctl", "bootout", "x"], True),
    ],
)
def test_mutating_verbs_are_classified_and_unknown_ones_deny(cmd: list[str], mutating: bool) -> None:
    assert installer.is_mutating_manager_cmd(cmd) is mutating


# ── the in-process tripwire (tests/conftest.py, autouse) ────────────────────


def test_an_unpatched_mutating_verb_raises_before_spawning(shims) -> None:
    _bin, log = shims
    with pytest.raises(AssertionError, match="service manager"):
        installer._run_manager(
            ["launchctl", "bootout", "gui/0/com.nexus.fake"], timeout=5,
        )
    assert _spawned(log) == [], "the tripwire let a mutating verb reach the manager"


def test_a_read_only_probe_passes_through_to_the_manager(shims) -> None:
    _bin, log = shims
    result = installer._run_manager(["launchctl", "print-disabled", "gui/0"], timeout=5)
    assert result.returncode == 0
    assert len(_spawned(log)) == 1 and "print-disabled" in _spawned(log)[0]


def test_a_test_that_replaces_the_spawn_may_issue_mutating_verbs(shims) -> None:
    _bin, log = shims
    seen: list[list[str]] = []

    def fake_run_bounded(argv, *, timeout, **_kw):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    with patch.object(installer, "run_bounded", new=fake_run_bounded):
        installer._run_manager(["systemctl", "--user", "enable", "--now", "x.service"], timeout=5)
    assert seen and seen[0][1:] == ["--user", "enable", "--now", "x.service"]
    assert _spawned(log) == []


def test_a_test_that_replaces_run_manager_is_not_wrapped(shims) -> None:
    calls: list[list[str]] = []
    with patch.object(installer, "_run_manager", new=lambda cmd, **kw: calls.append(cmd)):
        installer._run_manager(["launchctl", "bootout", "x"], timeout=5)
    assert calls == [["launchctl", "bootout", "x"]]


def test_an_unfindable_manager_still_reads_as_no_manager(tmp_path: Path, monkeypatch) -> None:
    """The PATH-stripping tests (tests/test_upgrade_finish.py, test_service_install.py)
    prove the no-manager branches: the tripwire must not turn their
    FileNotFoundError into a failure."""
    monkeypatch.setenv("PATH", str(tmp_path))
    with patch.dict(installer._MANAGER_ABSOLUTE_PATHS, {"launchctl": (), "systemctl": ()}):
        with pytest.raises(FileNotFoundError):
            installer._run_manager(["systemctl", "--user", "disable", "--now", "x"], timeout=5)


# ── the product-side refusal, exercised in a REAL child process ─────────────

_CHILD = (
    "import sys\n"
    "from nexus.daemon import installer\n"
    "try:\n"
    "    r = installer._run_manager(sys.argv[1:], timeout=5)\n"
    "    print('RAN', r.returncode)\n"
    "except installer.ManagerRefusedUnderFence as exc:\n"
    "    print('REFUSED', isinstance(exc, FileNotFoundError))\n"
)


def _child(shims, fenced: bool, *argv: str) -> tuple[str, list[str]]:
    _bin, log = shims
    env = dict(os.environ)
    env.pop("NX_FENCED_HOME", None)
    if fenced:
        env["NX_FENCED_HOME"] = "/some/fenced/home"
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, *argv],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip(), _spawned(log)


def test_a_fenced_child_refuses_a_mutating_verb_and_never_spawns(shims) -> None:
    out, spawned = _child(shims, True, "launchctl", "bootstrap", "gui/0", "/x.plist")
    # a FileNotFoundError subclass, so every existing caller degrades to its
    # "no manager on this box" branch rather than crashing
    assert out == "REFUSED True"
    assert spawned == []


def test_a_fenced_child_still_runs_read_only_probes(shims) -> None:
    out, spawned = _child(shims, True, "systemctl", "--user", "is-enabled", "x.service")
    assert out == "RAN 0"
    assert len(spawned) == 1


def test_an_unfenced_child_is_not_refused(shims) -> None:
    """The refusal is keyed on the fence marker, not on being a dev checkout:
    a developer's own `nx daemon service install` must keep working."""
    out, spawned = _child(shims, False, "launchctl", "bootstrap", "gui/0", "/x.plist")
    assert out == "RAN 0"
    assert len(spawned) == 1


def test_a_command_that_is_not_a_service_manager_is_outside_the_fence(shims) -> None:
    """The suite's fake managers (``sleep``, ``true`` in test_installer_lift's
    hung-manager cases) are routed through _run_manager too; they are neither
    launchctl nor systemctl and must not trip a wire built for those two."""
    assert installer.is_service_manager_cmd(["launchctl", "print", "x"])
    assert installer.is_service_manager_cmd(["/bin/launchctl", "print", "x"])
    assert not installer.is_service_manager_cmd(["sleep", "30"])
    assert not installer.is_service_manager_cmd([])
    assert installer._run_manager(["true"], timeout=5).returncode == 0
