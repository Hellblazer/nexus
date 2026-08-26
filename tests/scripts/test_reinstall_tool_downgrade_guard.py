# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The guards that SURVIVE the nexus-utpuw rewrite of scripts/reinstall-tool.sh.

nexus-utpuw comment 1 deletes the live-holder refusal, --force, --cycle-daemons
and --cycle-mcp, because in-place swap is what made them necessary and installs
are side-by-side now. It preserves these two explicitly: they are a DIFFERENT
failure class. They do not protect live processes from a swap -- they protect
the install from being replaced by something older, or by something from a
different source.

Both were born from live incidents (nexus-q3xrx, 2026-06-11 and 2026-06-12): a
reinstall from a stale checkout silently DOWNGRADED the shared CLI until `nx
daemon service` vanished and the stack would not restart, and a PyPI reinstall
over a dev install wiped 31 unreleased modules while keeping the version string.

nexus-zfutt is the property that makes the downgrade guard trustworthy: resolve
the installed version from the TARGET tree, never from a bare `nx` lookup on the
ambient $PATH. tests/e2e/release-sandbox.sh activates an isolated sandbox $HOME
and prepends its own bin dir; on a fresh sandbox no `nx` exists there yet, so a
PATH lookup falls through to the REAL global install and a develop checkout's
lagging pyproject version reads as a false downgrade of an install the run has
nothing to do with. Under generations the target is whatever `current` resolves
to, and a missing one correctly skips the comparison.

These tests never touch the real global install: PATH excludes ~/.local/bin (see
_generation_harness.SAFE_BASE_PATH) and $HOME is a throwaway tmp_path.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from _generation_harness import (
    SAFE_BASE_PATH,
    fabricate_generation,
    make_executable,
    stub_uv,
)

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "reinstall-tool.sh"


def _write_pyproject(source_dir: Path, version: str) -> None:
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "pyproject.toml").write_text(
        f'[project]\nname = "conexus"\nversion = "{version}"\n'
    )


class _Bed:
    """tools/ + bin/ + a stub uv + a throwaway HOME."""

    def __init__(self, tmp_path: Path):
        self.tools = tmp_path / "tools"
        self.tools.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.stub_bin = tmp_path / "stubbin"
        self.stub_bin.mkdir()
        self.marker = tmp_path / "build-ran.marker"
        stub_uv(self.stub_bin, marker=self.marker)
        self.ambient_bin = tmp_path / "ambientbin"
        self.ambient_bin.mkdir()
        self.source = tmp_path / "checkout"

    def install(self, version: str, *, source_kind: str = "directory") -> Path:
        gen = fabricate_generation(
            self.tools, "INSTALLED", version=version, source_kind=source_kind
        )
        (self.tools / "current").symlink_to(gen)
        return gen

    def ambient_nx(self, version: str) -> None:
        """A stray `nx` on PATH, standing in for the real global install."""
        make_executable(
            self.ambient_bin / "nx",
            f'#!/bin/bash\nif [[ "$1" == "--version" ]]; then echo "nx, version {version}"; fi\nexit 0\n',
        )

    def run(self, *args, source: str | None = None, cwd: Path | None = None):
        env = dict(os.environ)
        env["PATH"] = f"{self.stub_bin}:{self.ambient_bin}:{SAFE_BASE_PATH}"
        env["HOME"] = str(self.home)
        env["NX_TOOLS_DIR"] = str(self.tools)
        env["NX_BIN_DIR"] = str(self.bin)
        return subprocess.run(
            ["bash", str(_SCRIPT), source or str(self.source), *args],
            env=env, capture_output=True, text=True, timeout=120,
            cwd=str(cwd) if cwd else None,
        )


# --------------------------------------------------------------------------
# nexus-zfutt: the ambient PATH is never the comparison target
# --------------------------------------------------------------------------

@pytest.mark.parametrize("ambient_version", ["6.16.0", "6.10.0"])
def test_no_current_generation_never_compares_against_ambient_path(
    tmp_path: Path, ambient_version: str
) -> None:
    """Nothing installed yet, but a stray ambient `nx` resolves on PATH. It must
    not be consulted whether it looks newer OR older than the checkout: there is
    nothing installed to downgrade. Parametrised in both directions because a
    guard that reads the wrong binary can accidentally be right in one of them."""
    bed = _Bed(tmp_path)
    _write_pyproject(bed.source, "6.11.0")
    bed.ambient_nx(ambient_version)

    result = bed.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "REFUSING" not in result.stdout
    assert bed.marker.exists(), "the build step was never reached"


def test_a_real_downgrade_of_the_current_generation_is_refused(tmp_path: Path) -> None:
    """The guard's actual purpose. The version comes from the generation
    `current` points at -- the tree this run is about to replace."""
    bed = _Bed(tmp_path)
    bed.install("6.16.0")
    _write_pyproject(bed.source, "6.11.0")

    result = bed.run()

    assert result.returncode == 1, result.stdout + result.stderr
    assert "DOWNGRADE" in result.stdout
    assert not bed.marker.exists(), "refused, yet the build ran anyway"


def test_allow_downgrade_overrides_the_downgrade_guard(tmp_path: Path) -> None:
    """A guard you cannot deliberately override is not a guard, it is a wall
    (audit finding F4). A genuine downgrade is a real workflow."""
    bed = _Bed(tmp_path)
    bed.install("6.16.0")
    _write_pyproject(bed.source, "6.11.0")

    result = bed.run("--allow-downgrade")

    assert result.returncode == 0, result.stdout + result.stderr
    assert bed.marker.exists()


# --------------------------------------------------------------------------
# divergent source: a registry package over a dev checkout
# --------------------------------------------------------------------------

def test_a_registry_source_over_a_directory_install_is_refused(tmp_path: Path) -> None:
    """nexus-q3xrx incident #2 verbatim: a PyPI reinstall over a dev install
    wiped 31 unreleased modules while keeping the version string. The signal
    used to be a `directory = ` line in uv's receipt; it is source_kind in ours."""
    bed = _Bed(tmp_path)
    bed.install("6.16.0", source_kind="directory")

    result = bed.run(source="conexus")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "REFUSING" in result.stdout
    assert not bed.marker.exists()


def test_allow_registry_over_dev_overrides_it(tmp_path: Path) -> None:
    """The script's own same-version advice tells you to install a released
    build over a dev one, so this override has a first-party caller."""
    bed = _Bed(tmp_path)
    bed.install("6.16.0", source_kind="directory")

    result = bed.run("--allow-registry-over-dev", source="conexus")

    assert result.returncode == 0, result.stdout + result.stderr
    assert bed.marker.exists()


def test_a_registry_source_over_a_registry_install_is_fine(tmp_path: Path) -> None:
    """Non-vacuity for the two above: the guard keys on the INSTALLED source
    being a directory, not merely on the new source being a registry package.
    Without this, a guard that refused every registry install would pass both."""
    bed = _Bed(tmp_path)
    bed.install("6.16.0", source_kind="registry")

    result = bed.run(source="conexus")

    assert result.returncode == 0, result.stdout + result.stderr
    assert bed.marker.exists()


# --------------------------------------------------------------------------
# F4's MUST NOT: no single flag bypasses both guards
# --------------------------------------------------------------------------

def test_neither_override_bypasses_the_other_guard(tmp_path: Path) -> None:
    """Audit finding F4 permits a narrow override PER GUARD and forbids one flag
    that bypasses both -- that is --force wearing a new name, and it drifts back
    into bypassing everything. Each override must be inert against the guard it
    does not name.

    Both directions are checked, because a shared bypass variable would satisfy
    either one alone."""
    bed = _Bed(tmp_path)
    bed.install("6.16.0", source_kind="directory")

    # --allow-registry-over-dev must NOT let a downgrade through.
    _write_pyproject(bed.source, "6.11.0")
    downgrade = bed.run("--allow-registry-over-dev")
    assert downgrade.returncode == 1, (
        "--allow-registry-over-dev bypassed the DOWNGRADE guard; the two "
        "overrides share a bypass and are one flag wearing two names\n"
        + downgrade.stdout
    )
    assert "DOWNGRADE" in downgrade.stdout

    # --allow-downgrade must NOT let a registry-over-dev install through.
    registry = bed.run("--allow-downgrade", source="conexus")
    assert registry.returncode == 1, (
        "--allow-downgrade bypassed the REGISTRY-OVER-DIRECTORY guard\n"
        + registry.stdout
    )
    assert "REFUSING" in registry.stdout


# --------------------------------------------------------------------------
# one classifier, not two (nexus-pk9yt)
# --------------------------------------------------------------------------

def test_a_bare_source_name_is_a_registry_source_even_if_a_directory_shadows_it(
    tmp_path: Path,
) -> None:
    """install_generation.sh:79-82 decides source KIND by SHAPE and says so:
    "a bare distribution name is a registry source wherever you happen to be
    standing" -- deliberately, so that running from a checkout root does not
    turn `conexus` into a local directory install.

    This guard used to answer the same question by EXISTENCE
    (`[ -f "$SOURCE/pyproject.toml" ]`). The two agree for `.` and for
    `conexus`, and diverge for a bare name that happens to name a real
    directory in cwd: shape says registry, existence said directory. Since the
    guard only fires when it concludes "registry", the divergence SKIPPED the
    registry-over-dev refusal for exactly that input -- while the builder went
    on to install from the registry and bake source_kind="registry" into the
    receipt, so the disagreement outlived the run.

    That is nexus-q3xrx incident #2's class (a PyPI reinstall over a dev
    install wipes unreleased modules while keeping the version string), reached
    through a classifier mismatch rather than a missing guard."""
    bed = _Bed(tmp_path)
    bed.install("6.16.0", source_kind="directory")

    # A real directory, with a real pyproject, whose NAME has no slash.
    shadow = tmp_path / "workdir"
    shadow.mkdir()
    (shadow / "demopkg").mkdir()
    _write_pyproject(shadow / "demopkg", "9.9.9")

    result = bed.run(source="demopkg", cwd=shadow)

    assert result.returncode == 1, (
        "a bare source name was treated as a directory install because a "
        "directory of that name existed in cwd, so the registry-over-dev "
        "refusal was skipped\n" + result.stdout + result.stderr
    )
    assert "REFUSING" in result.stdout
    assert not bed.marker.exists(), "refused, yet the build ran anyway"
