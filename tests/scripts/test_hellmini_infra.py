# SPDX-License-Identifier: AGPL-3.0-or-later
"""infra/hellmini (nexus-xmj1r): the versioned hellmini runner hooks, colima
provision blocks and install.sh. No ssh: install.sh runs against a temp
HELLMINI_ROOT with a stub standing in for ``sudo -u``."""
from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
INFRA = REPO_ROOT / "infra" / "hellmini"
INSTALL = INFRA / "install.sh"
HOOKS = INFRA / "hooks"

RUNNER_USERS = ("ghci", "ghrunner")
EXPECTED_HOOKS = {
    "ghci": {"job-started.sh", "job-completed.sh", "wait-for-host.sh"},
    "ghrunner": {"job-started.sh", "job-completed.sh"},
}
PORT_RANGES = {
    "hhildebrand": "32768 39999",
    "ghrunner": "40600 44999",
    "ghci": "45000 49151",
}


def _all_scripts() -> list[Path]:
    return sorted(INFRA.rglob("*.sh"))


def test_every_script_parses() -> None:
    scripts = _all_scripts()
    # Non-vacuity: 5 hooks + install.sh. A rename that empties the glob must fail.
    assert len(scripts) >= 6, scripts
    for s in scripts:
        r = subprocess.run(["bash", "-n", str(s)], capture_output=True, text=True)
        assert r.returncode == 0, f"{s}: {r.stderr}"


def test_expected_hooks_are_versioned() -> None:
    for user, names in EXPECTED_HOOKS.items():
        found = {p.name for p in (HOOKS / user).glob("*.sh")}
        assert found == names, (user, found)


def test_colima_provision_blocks_carry_the_disjoint_ranges() -> None:
    for user, rng in PORT_RANGES.items():
        doc = yaml.safe_load((INFRA / "colima" / f"{user}-provision.yaml").read_text())
        blocks = doc["provision"]
        assert len(blocks) == 1 and blocks[0]["mode"] == "system", user
        script = blocks[0]["script"]
        assert f'net.ipv4.ip_local_port_range = {rng}' in script, user
        assert f'sysctl -q -w net.ipv4.ip_local_port_range="{rng}"' in script, user
        assert "systemctl restart docker" in script, user


def test_readme_names_every_range() -> None:
    text = (INFRA / "README.md").read_text()
    for lo_hi in PORT_RANGES.values():
        lo, hi = lo_hi.split()
        assert f"{lo}-{hi}" in text, lo_hi
    assert "40552" in text


def test_no_credentials_in_versioned_files() -> None:
    pat = re.compile(r"(token|secret|password\s*[:=]|api[_-]?key|PRIVATE KEY)", re.I)
    for p in INFRA.rglob("*"):
        if p.is_file():
            assert not pat.search(p.read_text()), p


# --- install.sh behavior against a temp root ---------------------------------


@pytest.fixture()
def host(tmp_path):
    root = tmp_path / "Bulk"
    for u in RUNNER_USERS:
        (root / u / "actions-runner" / "hooks").mkdir(parents=True)
    stub = tmp_path / "run-as"
    stub.write_text('#!/bin/bash\nshift\nexec "$@"\n')
    stub.chmod(0o755)
    env = {**os.environ, "HELLMINI_ROOT": str(root), "HELLMINI_RUN_AS": str(stub)}
    return root, env


def _live(root: Path, user: str, name: str) -> Path:
    return root / user / "actions-runner" / "hooks" / name


def _install(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(INSTALL), *args], env=env, capture_output=True, text=True, timeout=60
    )


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def _backups(root: Path, user: str, name: str) -> list[Path]:
    return sorted(_live(root, user, name).parent.glob(f"{name}.bak-*"))


def test_fresh_install_writes_all_hooks_with_modes_and_no_backup(host) -> None:
    root, env = host
    r = _install(env)
    assert r.returncode == 0, r.stderr
    for user, names in EXPECTED_HOOKS.items():
        for name in names:
            live = _live(root, user, name)
            assert live.read_bytes() == (HOOKS / user / name).read_bytes()
            assert _mode(live) == (0o755 if name == "wait-for-host.sh" else 0o700)
            assert _backups(root, user, name) == []
    assert not list(root.rglob("*.new.*")), "temp file left behind"


def test_identical_live_is_a_no_op_and_exits_zero(host) -> None:
    root, env = host
    assert _install(env).returncode == 0
    r = _install(env)  # no --force needed: nothing differs
    assert r.returncode == 0, r.stderr
    assert "unchanged: ghci/job-started.sh" in r.stdout
    assert _backups(root, "ghci", "job-started.sh") == []


def test_differing_live_refuses_without_force_and_writes_nothing(host) -> None:
    root, env = host
    assert _install(env).returncode == 0
    drifted = _live(root, "ghci", "job-started.sh")
    drifted.write_text("#!/bin/bash\necho hand edit\n")
    # a second, missing file: a refusal must not install it either
    _live(root, "ghrunner", "job-completed.sh").unlink()

    r = _install(env)
    assert r.returncode == 1
    assert "echo hand edit" in r.stdout, "the diff must be printed"
    assert "--force" in r.stderr
    assert drifted.read_text() == "#!/bin/bash\necho hand edit\n"
    assert not _live(root, "ghrunner", "job-completed.sh").exists()
    assert _backups(root, "ghci", "job-started.sh") == []


def test_force_overwrites_after_a_backup_and_keeps_the_live_mode(host) -> None:
    root, env = host
    assert _install(env).returncode == 0
    live = _live(root, "ghci", "job-started.sh")
    live.write_text("#!/bin/bash\necho hand edit\n")
    live.chmod(0o750)

    r = _install(env, "--force")
    assert r.returncode == 0, r.stderr
    assert live.read_bytes() == (HOOKS / "ghci" / "job-started.sh").read_bytes()
    assert _mode(live) == 0o750
    backups = _backups(root, "ghci", "job-started.sh")
    assert len(backups) == 1
    assert backups[0].read_text() == "#!/bin/bash\necho hand edit\n"
    assert _mode(backups[0]) == 0o750


def test_user_argument_limits_the_install(host) -> None:
    root, env = host
    assert _install(env, "ghrunner").returncode == 0
    assert _live(root, "ghrunner", "job-started.sh").exists()
    assert not _live(root, "ghci", "job-started.sh").exists()


def test_unknown_option_and_missing_hooks_dir_are_usage_errors(host) -> None:
    root, env = host
    assert _install(env, "--frce").returncode == 2
    (root / "ghci" / "actions-runner" / "hooks").rmdir()
    r = _install(env, "ghci")
    assert r.returncode == 2
    assert "does not exist" in r.stderr
