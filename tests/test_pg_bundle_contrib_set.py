# SPDX-License-Identifier: AGPL-3.0-or-later
"""The PG bundle's contrib set is vector, pg_trgm and pg_background (nexus-f3yqd.2), and a bundle
that lacks any of them is refused wherever the set is checked.

The set is asserted in four places that must agree: ``verify_and_mark`` in
``scripts/build_pg_bundle.sh`` (build time), the two ``for ctl in ...`` loops in the
engine-service-release and pg-bundle-cache-seed workflows (a cache hit skips the build, so these
are the only check on a restored prefix), and ``verify_layout`` in ``scripts/build_pg_bundle_windows.py``
(pinned per file in ``tests/test_pg_bundle_windows.py``). Here the shell function runs for real
against a staged tree, so "refuses" is shown, not read off the source.

pg_background has no ``shared_preload_libraries`` need, so nothing here (or in the bundle) touches
postgresql.conf.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import build_pg_bundle_windows as bw
from tests.test_pg_bundle_windows import stage_bundle

REPO = Path(__file__).resolve().parent.parent
SH = REPO / "scripts" / "build_pg_bundle.sh"
WORKFLOWS = REPO / ".github" / "workflows"
CONTRIB = {"vector", "pg_trgm", "pg_background"}
LICENSES = (
    "licenses/postgresql-COPYRIGHT.txt", "licenses/pgvector-LICENSE.txt",
    "licenses/pg_background-LICENSE.txt", "THIRD-PARTY-NOTICES.txt",
)
CHECKED_WORKFLOWS = ("engine-service-release.yml", "pg-bundle-cache-seed.yml")

_needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="verify_and_mark is a bash function; the Windows bundle's own check is tests/test_pg_bundle_windows.py",
)


def _function(text: str, name: str) -> str:
    """The body of a top-level ``name() { ... }`` shell function (closing brace in column 0)."""
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.DOTALL | re.MULTILINE)
    assert match, f"{name} not found in {SH.name}"
    return match.group(0)


def _stage(root: Path) -> Path:
    """A bundle tree that satisfies verify_and_mark: fake binaries, libs, controls and scripts."""
    darwin = sys.platform == "darwin"
    ext = "dylib" if darwin else "so"
    for d in ("bin", "lib/postgresql", "share/postgresql/extension"):
        (root / d).mkdir(parents=True, exist_ok=True)
    for b in ("initdb", "pg_ctl", "postgres", "psql", "createdb"):
        f = root / "bin" / b
        f.write_text('#!/bin/sh\necho "$0 (PostgreSQL) 17.5"\n')
        f.chmod(0o755)
    pg_config = root / "bin" / "pg_config"
    pg_config.write_text(
        "#!/bin/sh\n"
        f'case "$1" in --pkglibdir) echo "{root}/lib/postgresql";; '
        f'--sharedir) echo "{root}/share/postgresql";; --libdir) echo "{root}/lib";; esac\n'
    )
    pg_config.chmod(0o755)
    for lib in ("vector", "pg_background"):
        (root / "lib" / "postgresql" / f"{lib}.{ext}").write_bytes(b"x")
    extdir = root / "share" / "postgresql" / "extension"
    for ctl in sorted(CONTRIB):
        (extdir / f"{ctl}.control").write_text("comment = 'x'\ndefault_version = '3.1'\n")
        (extdir / f"{ctl}--3.1.sql").write_text("x")
    for lic in LICENSES:
        (root / lic).parent.mkdir(exist_ok=True)
        (root / lic).write_text("licence")
    return root


def _verify(root: Path) -> subprocess.CompletedProcess[str]:
    script = (
        "set -euo pipefail\n"
        f'BUNDLE_PREFIX="{root}"\nuname_s="$(uname -s)"\nlog() {{ echo "=== $* ==="; }}\n'
        + _function(SH.read_text(), "control_default_version")
        + _function(SH.read_text(), "has_default_script")
        + _function(SH.read_text(), "verify_and_mark")
        + "verify_and_mark\n"
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)


@_needs_bash
def test_a_complete_tree_passes_verify_and_mark(tmp_path: Path) -> None:
    proc = _verify(_stage(tmp_path / "bundle"))
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "pg_background" in proc.stdout  # the summary line names the new library


@_needs_bash
@pytest.mark.parametrize("control", sorted(CONTRIB))
def test_verify_and_mark_refuses_a_bundle_without_each_control_file(tmp_path: Path, control: str) -> None:
    root = _stage(tmp_path / "bundle")
    (root / "share" / "postgresql" / "extension" / f"{control}.control").unlink()
    proc = _verify(root)
    assert proc.returncode != 0
    assert f"{control}.control" in proc.stdout + proc.stderr


@_needs_bash
@pytest.mark.parametrize(
    "victim",
    ["lib/postgresql/pg_background.*", "share/postgresql/extension/pg_background--3.1.sql",
     "share/postgresql/extension/vector--3.1.sql", "share/postgresql/extension/pg_trgm--3.1.sql", *LICENSES],
)
def test_verify_and_mark_refuses_a_bundle_missing_a_library_script_or_licence(tmp_path: Path, victim: str) -> None:
    root = _stage(tmp_path / "bundle")
    gone = list(root.glob(victim))
    assert len(gone) == 1, f"non-vacuity: the staged tree holds {victim}"
    gone[0].unlink()
    proc = _verify(root)
    assert proc.returncode != 0
    assert Path(victim).name.split(".")[0] in proc.stdout + proc.stderr


@_needs_bash
def test_verify_and_mark_takes_the_script_name_from_the_control_file(tmp_path: Path) -> None:
    root = _stage(tmp_path / "bundle")
    control = root / "share" / "postgresql" / "extension" / "pg_background.control"
    control.write_text("comment = 'x'\ndefault_version = '4.2'\n")
    proc = _verify(root)
    assert proc.returncode != 0 and "pg_background--4.2.sql" in proc.stdout
    control.write_text("comment = 'x'\n")
    proc = _verify(root)
    assert proc.returncode != 0 and "names no default_version" in proc.stdout


def _ctl_loops(text: str) -> list[set[str]]:
    return [set(m.split()) for m in re.findall(r"for ctl in ([a-z_ ]+); do", text)]


@pytest.mark.parametrize("workflow", CHECKED_WORKFLOWS)
def test_the_workflow_contrib_checks_list_all_three_extensions(workflow: str) -> None:
    loops = _ctl_loops((WORKFLOWS / workflow).read_text())
    assert loops, f"non-vacuity: {workflow} has a contrib completeness loop"
    assert all(loop == CONTRIB for loop in loops), (workflow, loops)


def test_the_build_script_and_the_windows_builder_list_the_same_three(tmp_path: Path) -> None:
    assert _ctl_loops(SH.read_text()) == [CONTRIB]
    for ctl in sorted(CONTRIB):
        bundle = stage_bundle(tmp_path / ctl)
        (bundle / "share" / "postgresql" / "extension" / f"{ctl}.control").unlink()
        gone = bw.verify_layout(bundle, runtime=False)
        assert any(f"{ctl}.control" in p for p in gone), (ctl, gone)


def test_the_pg_background_pin_is_one_value_in_both_builders() -> None:
    sh = SH.read_text()
    default = re.search(r'PG_BACKGROUND_VERSION="\$\{PG_BACKGROUND_VERSION:-([^}]+)\}"', sh)
    assert default and default.group(1) == bw.DEFAULT_PG_BACKGROUND_VERSION
    shell_pins = dict(re.findall(r'^\s+(v[\d.]+)\) echo "([0-9a-f]{40})" ;;', sh, re.MULTILINE))
    assert shell_pins == bw.PINNED_PG_BACKGROUND_COMMITS, "the two builders pin different commits"
    assert bw.DEFAULT_PG_BACKGROUND_VERSION in shell_pins


def test_no_workflow_overrides_the_pg_background_pin() -> None:
    """The pin lives in the two scripts only; the cache keys carry it through the script hash (shell,
    ``hashFiles('scripts/build_pg_bundle.sh')``) and the ``-pgbg`` segment (Windows ``cache-key``). A workflow
    env that set it would move the build without moving the shell key."""
    for wf in WORKFLOWS.glob("*.yml"):
        assert "PG_BACKGROUND_VERSION" not in wf.read_text(), wf.name
    keyed = [
        line
        for name in ("engine-service-release.yml", "pg-bundle-cache-seed.yml", "ci.yml")
        for line in (WORKFLOWS / name).read_text().splitlines()
        if line.strip().startswith("key: pg-bundle-") and "-pgvector" in line
    ]
    assert keyed, "non-vacuity: shell-bundle cache keys exist"
    assert all("hashFiles('scripts/build_pg_bundle.sh')" in k for k in keyed), keyed


def test_the_windows_cache_key_moves_with_the_pg_background_version() -> None:
    old = bw.cache_key(bw.Pins("17.5", "v0.8.2", "v2.0.3"), "r", "ab" * 32)
    new = bw.cache_key(bw.Pins("17.5", "v0.8.2"), "r", "ab" * 32)
    assert old != new and "-pgbgv2.0.4-" in new


def test_the_windows_pg_background_command_compiles_both_files_with_the_random_shim() -> None:
    """MSVC has no POSIX random(); v2.0.4 calls it, so the link fails without the define (nexus-f3yqd.2)."""
    dirs = {"includedir": "I", "includedir-server": "S", "libdir": "L", "pkglibdir": "P", "sharedir": "H"}
    cmd = bw.pg_background_cl_cmd(Path("src-root"), dirs)
    assert cmd[0] == "cl" and "/LD" in cmd and "/Drandom=rand" in cmd and "/DWIN32" in cmd
    assert [Path(a).name for a in cmd if a.endswith(".c")] == ["pg_background.c", "pg_background_worker.c"]
    assert cmd[-2:] == ["/link", "L\\postgres.lib"]


def _run_build_pg_background(tmp_path: Path, version: str, head: str) -> subprocess.CompletedProcess[str]:
    """build_pg_background for real, with the VCS and make commands replaced by shell functions."""
    text = SH.read_text()
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    script = (
        "set -euo pipefail\n"
        f'BUNDLE_PREFIX="{tmp_path}/bundle"\nWORK_DIR="{work}"\nPG_BACKGROUND_VERSION="{version}"\n'
        'log() { echo "=== $* ==="; }\n'
        'git() { case "$1" in clone) mkdir -p pg_background ;; rev-parse) echo "$FAKE_HEAD" ;; esac; }\n'
        'make() { echo "MAKE-CALLED $*"; }\n'
        + _function(text, "pg_background_pinned_commit")
        + _function(text, "build_pg_background")
        + "build_pg_background\n"
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "FAKE_HEAD": head},
    )


PINNED_HEAD = "c1db2540408ce4626ea21bc97572ff43b1a19ff3"


@_needs_bash
def test_the_pinned_commit_builds_and_installs(tmp_path: Path) -> None:
    proc = _run_build_pg_background(tmp_path, "v2.0.4", PINNED_HEAD)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count("MAKE-CALLED") == 2 and "install" in proc.stdout  # build, then install


@_needs_bash
def test_a_tag_that_resolves_to_another_commit_is_refused_before_make(tmp_path: Path) -> None:
    proc = _run_build_pg_background(tmp_path, "v2.0.4", "f" * 40)
    assert proc.returncode == 1
    assert "resolved to commit " + "f" * 40 in proc.stderr and "the pin is " + PINNED_HEAD in proc.stderr
    assert "MAKE-CALLED" not in proc.stdout


@_needs_bash
def test_a_version_without_a_pin_is_refused_before_it_is_cloned(tmp_path: Path) -> None:
    proc = _run_build_pg_background(tmp_path, "v2.0.5", PINNED_HEAD)
    assert proc.returncode == 1 and "no pinned commit for pg_background v2.0.5" in proc.stderr
    assert not (tmp_path / "work" / "pg_background").exists(), "nothing was cloned"


@_needs_bash
def test_an_upgrade_script_ending_at_the_default_version_is_enough(tmp_path: Path) -> None:
    """PostgreSQL's own pg_trgm ships a base script plus upgrade scripts and no <ext>--<default_version>.sql."""
    root = _stage(tmp_path / "bundle")
    ext = root / "share" / "postgresql" / "extension"
    (ext / "pg_trgm--3.1.sql").rename(ext / "pg_trgm--3.0--3.1.sql")
    proc = _verify(root)
    assert proc.returncode == 0, proc.stdout + proc.stderr
