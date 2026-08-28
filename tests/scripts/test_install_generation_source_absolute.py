# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The receipt records an ABSOLUTE directory source. nexus-hibpr.

The receipt is read back by ``nx self install`` from whatever cwd that later
process has -- the SessionStart lockstep hook is one such caller -- so a
receipt saying ``"source": "."`` describes the reader's directory, not the
checkout the generation was built from. Measured 2026-08-27 on the live box:
``"source": "."``, ``"spec": "."``, and ``nx self install --dry-run`` run from
``$HOME`` re-emitted ``--source .``.

The builder is invoked with cwd = the checkout and ``--source .``, exactly
the shape ``scripts/reinstall-tool.sh`` produces by default, and the receipt
must name the resolved checkout. The control below proves the assertion can
fail: the same invocation against the pre-fix builder writes ``"."``.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from _generation_harness import SAFE_BASE_PATH, stub_uv

_REPO = Path(__file__).resolve().parents[2]
_INSTALLER = _REPO / "src" / "nexus" / "_install" / "install_generation.sh"


@pytest.fixture
def bed(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text('[project]\nname = "conexus"\nversion = "7.20.0"\n')
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    stub_uv(stub_bin)
    env = {
        "PATH": f"{stub_bin}:{SAFE_BASE_PATH}",
        "HOME": str(tmp_path / "home"),
        "NX_TOOLS_DIR": str(tools),
    }
    return tools, checkout, env


def _build(cwd: Path, env: dict, *args: str) -> Path:
    r = subprocess.run(
        ["bash", str(_INSTALLER), *args],
        capture_output=True, text=True, env=env, cwd=str(cwd),
    )
    assert r.returncode == 0, f"builder failed: {r.stderr}"
    return Path(r.stdout.strip().splitlines()[-1])


def _receipt(gen: Path) -> dict:
    return json.loads((gen / "nexus-install.json").read_text())


def test_a_dot_source_is_recorded_as_the_resolved_checkout(bed) -> None:
    tools, checkout, env = bed

    gen = _build(checkout, env, "--source", ".")

    receipt = _receipt(gen)
    resolved = str(checkout.resolve())
    assert receipt["source"] == resolved, (
        f"receipt records {receipt['source']!r}; a later `nx self install` from another "
        f"cwd would rebuild from the wrong directory (nexus-hibpr)"
    )
    assert receipt["source_kind"] == "directory"
    # SPEC is derived from SOURCE, so it is fixed at the same origin.
    assert receipt["spec"].startswith(resolved), receipt["spec"]
    assert os.path.isabs(receipt["source"])


def test_a_relative_subpath_source_is_recorded_absolute(bed) -> None:
    """``./sub`` and ``../checkout`` are directory-kind by shape; both absolutize."""
    tools, checkout, env = bed
    sub = checkout / "sub"
    sub.mkdir()
    (sub / "pyproject.toml").write_text('[project]\nname = "conexus"\nversion = "7.20.0"\n')

    gen = _build(checkout, env, "--source", "./sub")

    assert _receipt(gen)["source"] == str(sub.resolve())


def test_a_file_source_resolves_through_its_parent(bed) -> None:
    """A wheel path is directory-kind by shape (it has a slash) and is a file."""
    tools, checkout, env = bed
    dist = checkout / "dist"
    dist.mkdir()
    wheel = dist / "conexus-7.20.0-py3-none-any.whl"
    wheel.write_bytes(b"")

    gen = _build(checkout, env, "--source", "./dist/conexus-7.20.0-py3-none-any.whl")

    assert _receipt(gen)["source"] == str(wheel.resolve())


def test_an_absolute_source_is_unchanged(bed) -> None:
    """Non-regression: the shape that was already correct stays byte-identical."""
    tools, checkout, env = bed

    gen = _build(checkout.parent, env, "--source", str(checkout))

    assert _receipt(gen)["source"] == str(checkout.resolve())


def test_a_registry_source_is_not_touched(bed) -> None:
    """A bare distribution name is a registry source wherever you stand; no
    path resolution applies, and a directory named ``conexus`` in cwd must
    not turn it into one (the nexus-pk9yt shape, re-asserted here because the
    absolutizing branch sits right next to the classifier)."""
    tools, checkout, env = bed
    (checkout / "conexus").mkdir()

    gen = _build(checkout, env, "--source", "conexus", "--version", "7.20.0")

    receipt = _receipt(gen)
    assert receipt["source"] == "conexus"
    assert receipt["source_kind"] == "registry"


def test_a_missing_directory_source_still_fails_loud(bed) -> None:
    tools, checkout, env = bed
    r = subprocess.run(
        ["bash", str(_INSTALLER), "--source", "./does-not-exist"],
        capture_output=True, text=True, env=env, cwd=str(checkout),
    )
    assert r.returncode != 0
    assert "does not exist" in r.stderr


def test_the_control_the_prefix_builder_wrote_a_dot(bed) -> None:
    """KILL CONTROL (playbook §3.1). Strip the absolutizing block from a copy of
    the builder and run the same invocation: the receipt must read ``"."``.
    If this ever passes with the real builder's output, the assertion above has
    stopped meaning anything."""
    tools, checkout, env = bed
    text = _INSTALLER.read_text()
    start = text.index('if [ "$SOURCE_KIND" = "directory" ]; then')
    # The OUTER fi, at column 0 -- the block nests an if/else/fi inside.
    end = text.index("\nfi\n", start) + len("\nfi\n")
    pre_fix = (
        text[:start]
        + 'if [ "$SOURCE_KIND" = "directory" ] && [ ! -e "$SOURCE" ]; then _die "missing"; fi\n'
        + text[end:]
    )
    # A copy of the builder's directory in tmp, never a scratch file in the
    # source tree: the script sources "$_here/layout.sh" next to itself.
    crippled_dir = tools.parent / "crippled-install"
    crippled_dir.mkdir()
    (crippled_dir / "layout.sh").write_text((_INSTALLER.parent / "layout.sh").read_text())
    crippled = crippled_dir / "install_generation.sh"
    crippled.write_text(pre_fix)

    r = subprocess.run(
        ["bash", str(crippled), "--source", "."],
        capture_output=True, text=True, env=env, cwd=str(checkout),
    )

    assert r.returncode == 0, r.stderr
    gen = Path(r.stdout.strip().splitlines()[-1])
    assert _receipt(gen)["source"] == ".", "the control did not reproduce the defect"
