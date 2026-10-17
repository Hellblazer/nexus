# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tests for infra/hellmini/install-watchdog.sh (nexus-ey1me).

The script installs the runner watchdog and its LaunchDaemon plist as root on
hellmini. Off the host it runs against a tmp tree: HELLMINI_WD_ROOT prefixes
the two destination paths, HELLMINI_RUN_AS is a stand-in for ``sudo -n``,
HELLMINI_WD_OWNER is the current user, and HELLMINI_LAUNCHCTL is a stub that
records its arguments. Real bash, real files, no mocks.

Also pins that the repo copies are the shape the host runs: the script is
executable, parses under ``bash -n``, and the plist names the installed path.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parent.parent / "infra" / "hellmini"
INSTALL = INFRA / "install-watchdog.sh"
SRC_SCRIPT = INFRA / "watchdog" / "actions-runner-watchdog"
SRC_PLIST = INFRA / "watchdog" / "local.actions-runner-watchdog.plist"


@pytest.fixture
def host(tmp_path: Path) -> dict:
    """A fake host tree plus the stand-ins; returns paths and the env."""
    root = tmp_path / "root"
    (root / "Library" / "LaunchDaemons").mkdir(parents=True)
    # /usr/local/libexec is deliberately absent: a fresh host lacks it.
    runas = tmp_path / "runas"
    runas.write_text('#!/bin/sh\nshift\nexec "$@"\n')
    runas.chmod(0o755)
    calls = tmp_path / "launchctl.calls"
    launchctl = tmp_path / "launchctl"
    launchctl.write_text(
        f'#!/bin/sh\necho "$@" >> "{calls}"\n'
        '[ "${FAKE_BOOTSTRAP_FAIL:-}" = 1 ] && [ "$1" = bootstrap ] && exit 5\nexit 0\n'
    )
    launchctl.chmod(0o755)
    owner = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
    group = subprocess.run(["id", "-gn"], capture_output=True, text=True, check=True).stdout.strip()
    env = {
        **os.environ,
        "HELLMINI_WD_ROOT": str(root),
        "HELLMINI_RUN_AS": str(runas),
        "HELLMINI_WD_OWNER": f"{owner}:{group}",
        "HELLMINI_LAUNCHCTL": str(launchctl),
        "HELLMINI_RETRY_SLEEP": "0",
    }
    return {
        "env": env,
        "script": root / "usr" / "local" / "libexec" / "actions-runner-watchdog",
        "plist": root / "Library" / "LaunchDaemons" / "local.actions-runner-watchdog.plist",
        "calls": calls,
        "root": root,
    }


def _run(host: dict, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(INSTALL), *args],
        env={**host["env"], **(env_extra or {})},
        capture_output=True,
        text=True,
        timeout=60,
    )


def _launchctl_calls(host: dict) -> list[str]:
    return host["calls"].read_text().splitlines() if host["calls"].exists() else []


def test_repo_files_have_the_shape_the_host_runs() -> None:
    assert os.access(INSTALL, os.X_OK)
    assert os.access(SRC_SCRIPT, os.X_OK)
    for sh in (INSTALL, SRC_SCRIPT):
        r = subprocess.run(["bash", "-n", str(sh)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    plist = plistlib.loads(SRC_PLIST.read_bytes())
    assert plist["Label"] == "local.actions-runner-watchdog"
    assert plist["ProgramArguments"] == ["/usr/local/libexec/actions-runner-watchdog"]
    assert plist["StartInterval"] == 60


def test_install_when_absent_then_check_is_clean(host: dict) -> None:
    r = _run(host)
    assert r.returncode == 0, r.stderr
    assert host["script"].read_bytes() == SRC_SCRIPT.read_bytes()
    assert host["plist"].read_bytes() == SRC_PLIST.read_bytes()
    assert oct(host["script"].stat().st_mode & 0o777) == "0o755"
    assert oct(host["plist"].stat().st_mode & 0o777) == "0o644"
    # HELLMINI_LAUNCHCTL is set, so the reload ran: bootout, then bootstrap.
    assert _launchctl_calls(host) == [
        "bootout system/local.actions-runner-watchdog",
        f"bootstrap system {host['plist']}",
    ]
    # Drift check on the result: exit 0, nothing written, no reload.
    host["calls"].unlink()
    again = _run(host)
    assert again.returncode == 0, again.stderr
    assert again.stdout.count("unchanged:") == 2
    assert _launchctl_calls(host) == []


def test_launchctl_is_skipped_for_a_prefixed_tree_without_a_stand_in(host: dict) -> None:
    env = {k: v for k, v in host["env"].items() if k != "HELLMINI_LAUNCHCTL"}
    host["env"] = env
    r = _run(host)
    assert r.returncode == 0, r.stderr
    assert "launchctl skipped" in r.stdout
    assert not host["calls"].exists()


def test_differing_live_file_is_refused_and_nothing_is_written(host: dict) -> None:
    assert _run(host).returncode == 0
    host["calls"].unlink()
    live_script = host["script"].read_bytes()
    host["plist"].write_text(host["plist"].read_text().replace("60", "61"))
    live_plist = host["plist"].read_bytes()
    r = _run(host)
    assert r.returncode == 1
    assert "local.actions-runner-watchdog.plist: live differs from repo" in r.stdout
    assert "nothing written" in r.stderr
    assert host["script"].read_bytes() == live_script
    assert host["plist"].read_bytes() == live_plist
    assert not list(host["plist"].parent.glob("*.bak-*"))
    assert _launchctl_calls(host) == []


def test_refusal_is_all_or_nothing_when_only_one_file_differs(host: dict) -> None:
    host["plist"].parent.mkdir(parents=True, exist_ok=True)
    host["plist"].write_text("stale\n")  # plist differs, script absent
    r = _run(host)
    assert r.returncode == 1
    assert not host["script"].exists(), "an absent file must not be installed past a refusal"


def test_force_backs_up_overwrites_and_reloads(host: dict) -> None:
    assert _run(host).returncode == 0
    host["calls"].unlink()
    host["plist"].write_text("stale\n")
    r = _run(host, "--force")
    assert r.returncode == 0, r.stderr
    backups = list(host["plist"].parent.glob("*.plist.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == "stale\n"
    assert host["plist"].read_bytes() == SRC_PLIST.read_bytes()
    assert not list(host["script"].parent.glob("*.bak-*")), "identical script is not backed up"
    assert len(_launchctl_calls(host)) == 2


def test_script_only_change_does_not_reload(host: dict) -> None:
    assert _run(host).returncode == 0
    host["calls"].unlink()
    host["script"].write_text("#!/bin/bash\nexit 0\n")
    assert _run(host).returncode == 1
    r = _run(host, "--force")
    assert r.returncode == 0, r.stderr
    assert host["script"].read_bytes() == SRC_SCRIPT.read_bytes()
    assert _launchctl_calls(host) == []


def test_wrong_mode_on_identical_content_is_drift(host: dict) -> None:
    assert _run(host).returncode == 0
    host["script"].chmod(0o700)
    r = _run(host)
    assert r.returncode == 1
    assert "content matches but live is" in r.stdout
    assert _run(host, "--force").returncode == 0
    assert oct(host["script"].stat().st_mode & 0o777) == "0o755"
    assert not list(host["script"].parent.glob("*.bak-*")), "a mode fix needs no backup"


def test_bootstrap_failure_exits_3(host: dict) -> None:
    r = _run(host, env_extra={"FAKE_BOOTSTRAP_FAIL": "1"})
    assert r.returncode == 3
    assert "launchctl bootstrap" in r.stderr


def test_missing_launchdaemons_dir_is_a_usage_error(host: dict) -> None:
    (host["plist"].parent).rmdir()
    r = _run(host)
    assert r.returncode == 2
    assert "does not exist" in r.stderr


def test_unknown_argument_is_a_usage_error(host: dict) -> None:
    assert _run(host, "--nope").returncode == 2
