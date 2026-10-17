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
# WD_TEST_INSTALL points the suite at a mutated copy (the copy needs a sibling watchdog/ directory).
INSTALL = Path(os.environ.get("WD_TEST_INSTALL") or INFRA / "install-watchdog.sh")
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
    first = tmp_path / "bootstrap.seen"
    launchctl.write_text(
        f'#!/bin/sh\necho "$@" >> "{calls}"\n'
        '[ "${FAKE_BOOTSTRAP_FAIL:-}" = 1 ] && [ "$1" = bootstrap ] && exit 5\n'
        f'if [ "${{FAKE_BOOTSTRAP_FAIL_ONCE:-}}" = 1 ] && [ "$1" = bootstrap ] && [ ! -e "{first}" ]; then : > "{first}"; exit 5; fi\n'
        '[ "${FAKE_NOT_LOADED:-}" = 1 ] && [ "$1" = print ] && exit 113\n'
        '[ "${FAKE_ENABLE_FAIL:-}" = 1 ] && [ "$1" = enable ] && exit 1\nexit 0\n'
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
        "HELLMINI_WD_HOSTMARK": str(tmp_path / "no-such-hellmini"),
    }
    return {
        "env": env,
        "script": root / "usr" / "local" / "libexec" / "actions-runner-watchdog",
        "state": root / "var" / "db" / "actions-runner-watchdog",
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
    # HELLMINI_LAUNCHCTL is set, so the reload ran: bootout, enable (the daemon
    # may have been disabled), then bootstrap.
    assert _launchctl_calls(host) == [
        "bootout system/local.actions-runner-watchdog",
        "enable system/local.actions-runner-watchdog",
        f"bootstrap system {host['plist']}",
    ]
    assert oct(host["state"].stat().st_mode & 0o777) == "0o700"
    # Drift check on the result: exit 0, nothing written, no reload.
    host["calls"].unlink()
    again = _run(host)
    assert again.returncode == 0, again.stderr
    assert again.stdout.count("unchanged:") == 3  # script, plist, state dir
    assert _launchctl_calls(host) == ["print system/local.actions-runner-watchdog"]


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
    assert [c.split()[0] for c in _launchctl_calls(host)] == ["bootout", "enable", "bootstrap"]


def test_script_only_change_does_not_reload(host: dict) -> None:
    assert _run(host).returncode == 0
    host["calls"].unlink()
    host["script"].write_text("#!/bin/bash\nexit 0\n")
    assert _run(host).returncode == 1
    r = _run(host, "--force")
    assert r.returncode == 0, r.stderr
    assert host["script"].read_bytes() == SRC_SCRIPT.read_bytes()
    # The daemon is loaded (print succeeds), so no bootout/enable/bootstrap.
    assert _launchctl_calls(host) == ["print system/local.actions-runner-watchdog"]


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


def test_bootstrap_that_fails_once_is_retried(host: dict) -> None:
    r = _run(host, env_extra={"FAKE_BOOTSTRAP_FAIL_ONCE": "1"})
    assert r.returncode == 0, r.stderr
    assert [c.split()[0] for c in _launchctl_calls(host)] == ["bootout", "enable", "bootstrap", "bootstrap"]
    assert "loaded: system/local.actions-runner-watchdog" in r.stdout


def test_enable_precedes_bootstrap_and_a_failed_enable_is_not_fatal(host: dict) -> None:
    r = _run(host, env_extra={"FAKE_ENABLE_FAIL": "1"})
    assert r.returncode == 0, r.stderr
    verbs = [c.split()[0] for c in _launchctl_calls(host)]
    assert verbs.index("enable") < verbs.index("bootstrap")
    assert "launchctl enable" in r.stderr


def test_force_backs_up_the_script_too(host: dict) -> None:
    assert _run(host).returncode == 0
    host["script"].write_text("#!/bin/bash\nexit 0\n")
    r = _run(host, "--force")
    assert r.returncode == 0, r.stderr
    backups = list(host["script"].parent.glob("actions-runner-watchdog.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == "#!/bin/bash\nexit 0\n"
    assert host["script"].read_bytes() == SRC_SCRIPT.read_bytes()
    assert not list(host["plist"].parent.glob("*.bak-*")), "identical plist is not backed up"


def test_an_unchanged_plist_with_a_booted_out_daemon_is_loaded_on_force(host: dict) -> None:
    """The redeploy case: files already match, the daemon was disabled and booted out."""
    assert _run(host).returncode == 0
    host["calls"].unlink()
    # Without --force it only says so.
    r = _run(host, env_extra={"FAKE_NOT_LOADED": "1"})
    assert r.returncode == 0
    assert "is not loaded" in r.stderr
    assert [c.split()[0] for c in _launchctl_calls(host)] == ["print"]
    host["calls"].unlink()
    r = _run(host, "--force", env_extra={"FAKE_NOT_LOADED": "1"})
    assert r.returncode == 0, r.stderr
    assert [c.split()[0] for c in _launchctl_calls(host)] == ["print", "bootout", "enable", "bootstrap"]


def test_state_dir_is_created_root_only_and_wrong_mode_is_drift(host: dict) -> None:
    assert _run(host).returncode == 0
    assert oct(host["state"].stat().st_mode & 0o777) == "0o700"
    host["state"].chmod(0o755)
    r = _run(host)
    assert r.returncode == 1
    assert "state dir: live is" in r.stdout
    assert oct(host["state"].stat().st_mode & 0o777) == "0o755", "a refusal changes nothing"
    r = _run(host, "--force")
    assert r.returncode == 0, r.stderr
    assert oct(host["state"].stat().st_mode & 0o777) == "0o700"


def test_state_dir_that_is_a_symlink_is_refused(host: dict) -> None:
    target = host["root"].parent / "elsewhere"
    target.mkdir()
    host["state"].parent.mkdir(parents=True)
    host["state"].symlink_to(target)
    r = _run(host, "--force")
    assert r.returncode == 2
    assert "not a plain directory" in r.stderr
    assert not host["script"].exists()


def test_state_dir_matches_the_script_default() -> None:
    """The installer's directory and the script's default STATE_DIR are the same literal."""
    assert 'STATE_DEST="$PREFIX/var/db/actions-runner-watchdog"' in INSTALL.read_text()
    assert "STATE_DIR=${WD_STATE_DIR:-/var/db/actions-runner-watchdog}" in SRC_SCRIPT.read_text()
    assert "/Volumes/Bulk/ghrunner/actions-runner-watchdog" not in SRC_SCRIPT.read_text()


def test_refuses_off_hellmini_without_a_prefix(host: dict) -> None:
    env = {k: v for k, v in host["env"].items() if k != "HELLMINI_WD_ROOT"}
    host["env"] = env  # HELLMINI_WD_HOSTMARK names a directory that does not exist
    r = _run(host)
    assert r.returncode == 2
    assert "not hellmini" in r.stderr
    assert _launchctl_calls(host) == []
    assert not list(host["root"].rglob("actions-runner-watchdog*"))


def test_an_empty_prefix_is_refused_even_with_a_launchctl_stand_in(host: dict) -> None:
    host["env"] = {**host["env"], "HELLMINI_WD_ROOT": ""}
    r = _run(host)
    assert r.returncode == 2
    assert "set but empty" in r.stderr
    assert _launchctl_calls(host) == []
    assert not list(host["root"].rglob("actions-runner-watchdog*"))
