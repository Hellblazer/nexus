# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-q81g7: the test HOME fence must not expose the OS autostart units.

The fence used to symlink every entry of the operator's real ``~/.config`` and
all of ``~/Library``, shadowing only ``.config/nexus``. A test's HOME therefore
resolved ``~/.config/systemd/user`` and ``~/Library/LaunchAgents`` to the real
directories, so ``upgrade_finish.converge_service_autostart_unit`` found a
real ``nexus-service.service`` / ``com.nexus.service.plist`` and, on the human
path (``nx daemon restart-stale``), backed it up, disabled it, rewrote it and
enabled it against the operator's real user manager. That destroyed the real
unit on qwentescence on 2026-09-30.

Nothing here touches the operator's HOME. Every "real" home is a ``tmp_path``
directory, and every service-manager spawn is replaced with a function that
fails the test.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tests._fence_home import (
    FENCED_HOME_ENV,
    REAL_HOME_ENV,
    fence_home,
    install_fence,
)
from tests._module_seam import patch_in

_SHELL_FENCE = Path(__file__).parent / "e2e" / "lib" / "fence_home.sh"

#: The directories the fence must replace with empty real ones, and the unit
#: files under them that must therefore stop resolving, as relative to a HOME.
_SHADOWED_DIRS = (".config/systemd", "Library/LaunchAgents")
_UNIT_FILES = (
    ".config/systemd/user/nexus-service.service",
    ".config/systemd/user/nexus-service.service.d/10-bind-all.conf",
    "Library/LaunchAgents/com.nexus.service.plist",
)


def _seed_real_home(real: Path) -> dict[str, str]:
    """A fake operator home holding real-looking autostart units. Returns
    ``{relative path: content}`` for every file seeded."""
    files = {
        ".config/systemd/user/nexus-service.service": "[Service]\nExecStart=/real/nx\n",
        ".config/systemd/user/nexus-service.service.d/10-bind-all.conf": "[Service]\n",
        "Library/LaunchAgents/com.nexus.service.plist": "<plist>real</plist>\n",
        "Library/Caches/keep.txt": "an entry the fence must still pass through\n",
        ".config/xtool/hosts.yml": "x\n",
    }
    for rel, body in files.items():
        p = real / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    (real / ".config" / "nexus").mkdir(parents=True, exist_ok=True)
    return files


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    """``{relative path: (mtime_ns, size)}`` for every file under *root*,
    without following symlinks out of it."""
    out: dict[str, tuple[int, int]] = {}
    for dirpath, _dirs, names in os.walk(root, followlinks=False):
        for name in names:
            p = Path(dirpath) / name
            st = p.lstat()
            out[str(p.relative_to(root))] = (st.st_mtime_ns, st.st_size)
    return out


def _assert_unit_dirs_are_empty_and_fenced(home: Path) -> None:
    for rel in _SHADOWED_DIRS:
        shadowed = home / rel
        assert shadowed.is_dir() and not shadowed.is_symlink(), f"{shadowed} is not a real dir"
        assert list(shadowed.iterdir()) == [], f"{shadowed} is not empty: {list(shadowed.iterdir())}"
    for rel in _UNIT_FILES:
        assert not (home / rel).exists(), f"{home / rel} still resolves"


def test_python_fence_hides_the_autostart_unit_dirs(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    _seed_real_home(real)

    home = fence_home(real, tmp_path / "fenced", ".config/nexus")

    _assert_unit_dirs_are_empty_and_fenced(home)
    # Everything else still passes through, including siblings of the shadowed
    # leaves: this is a denylist, not a rebuild.
    assert (home / "Library" / "Caches" / "keep.txt").read_text().startswith("an entry")
    assert (home / ".config" / "xtool" / "hosts.yml").exists()


def test_the_autostart_shadows_apply_whatever_shadow_the_caller_names(tmp_path: Path) -> None:
    """A caller that names only its own shadow (the e2e gate passes
    ``.config/nexus`` and nothing else) must not get an unfenced unit dir."""
    real = tmp_path / "real"
    real.mkdir()
    _seed_real_home(real)

    home = fence_home(real, tmp_path / "fenced", ".config/something-else")

    _assert_unit_dirs_are_empty_and_fenced(home)
    assert (home / ".config" / "something-else").is_dir()


@pytest.mark.skipif(not _SHELL_FENCE.exists(), reason="fence_home.sh missing")
def test_shell_fence_hides_the_autostart_unit_dirs(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    _seed_real_home(real)
    gate = tmp_path / "gate"

    r = subprocess.run(
        ["bash", "-c", f'source "{_SHELL_FENCE}"; fence_home "{real}" "{gate}" ".config/nexus"'],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr

    _assert_unit_dirs_are_empty_and_fenced(gate)
    assert (gate / "Library" / "Caches" / "keep.txt").exists()
    assert (gate / ".config" / "xtool" / "hosts.yml").exists()


def test_install_fence_makes_the_user_manager_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``systemctl --user`` finds the user manager through
    ``$XDG_RUNTIME_DIR`` or ``$DBUS_SESSION_BUS_ADDRESS``. The fence points the
    first at an empty directory it owns and removes the second."""
    real = tmp_path / "real"
    real.mkdir()
    _seed_real_home(real)
    real_runtime = tmp_path / "run-user-1000"
    real_runtime.mkdir()

    with patch.dict(os.environ), patch_in("tests._fence_home", "sys.platform", "linux"):
        monkeypatch.setenv("HOME", str(real))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(real_runtime))
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={real_runtime}/bus")
        monkeypatch.delenv(REAL_HOME_ENV, raising=False)
        monkeypatch.delenv(FENCED_HOME_ENV, raising=False)

        gate = tmp_path / "fenced"
        assert install_fence(gate) is not None

        runtime = Path(os.environ["XDG_RUNTIME_DIR"])
        assert runtime != real_runtime
        assert runtime.is_dir() and list(runtime.iterdir()) == []
        assert gate in runtime.parents
        assert "DBUS_SESSION_BUS_ADDRESS" not in os.environ


def test_a_fenced_convergence_leaves_a_real_unit_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proof. A fake operator home holds a stale unit at the platform's
    real unit path. Unfenced, the convergence probe finds it (the control, so
    this cannot pass vacuously). Fenced, the human-path convergence finds
    nothing, spawns nothing, and leaves every byte of the fake home alone.

    Every service-manager spawn is replaced with a function that fails, and
    HOME is the fake one the whole time, so this test cannot reach the
    operator's units even if the fence regresses."""
    from nexus import upgrade_finish as uf
    from nexus.commands import daemon as daemon_cmd
    from nexus.upgrade_finish import converge_service_autostart_unit

    real = tmp_path / "real"
    real.mkdir()
    _seed_real_home(real)

    def _no_spawn(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"a service-manager / nx spawn was attempted: {args!r}")

    config_dir = tmp_path / "cfg"
    config_dir.mkdir()

    with patch.dict(os.environ), \
         patch("nexus.config.is_local_mode", return_value=True), \
         patch("nexus.daemon.installer.run_bounded", new=_no_spawn), \
         patch.object(uf, "run_bounded", new=_no_spawn):
        monkeypatch.setenv("HOME", str(real))
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))
        monkeypatch.delenv(REAL_HOME_ENV, raising=False)
        monkeypatch.delenv(FENCED_HOME_ENV, raising=False)

        # Control: with the real home as HOME the stale unit IS found and drifts.
        unit = daemon_cmd._service_autostart_unit_installed()
        if unit is None:
            # Platform with no autostart support (neither darwin nor linux).
            pytest.skip("no autostart unit location on this platform")
        # The seeded files are at the Linux path AND the macOS path, so the
        # one for this platform exists; make its content stale for the render.
        assert unit.parent.name in {"user", "LaunchAgents"}
        probe = uf._probe_service_autostart_drift()
        assert probe is not None and not probe.content_matches, (
            "control failed: the unfenced probe did not find the stale unit, "
            "so the fenced assertion below would prove nothing"
        )

        before = _tree(real)

        # Fence it, exactly as pytest_sessionstart does.
        assert install_fence(tmp_path / "fenced") is not None
        assert Path(os.environ["HOME"]) == tmp_path / "fenced"

        actions = converge_service_autostart_unit(config_dir)  # the human path

        assert actions == [], f"a fenced run still saw a unit: {actions}"
        assert daemon_cmd._service_autostart_unit_installed() is None

    assert _tree(real) == before, "the fake REAL home was modified through the fence"
    for rel, body in {
        ".config/systemd/user/nexus-service.service": "[Service]\nExecStart=/real/nx\n",
        "Library/LaunchAgents/com.nexus.service.plist": "<plist>real</plist>\n",
    }.items():
        assert (real / rel).read_text() == body
