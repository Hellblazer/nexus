# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The wheel's shipped constraints file is the lock, exactly. nexus-2k66t.4.1.

``src/nexus/_install/constraints.txt`` ships inside the wheel and every install
path hands it to ``uv pip install --constraints``. If it drifts from ``uv.lock``
the install resolves versions nobody tested (or, if it is stale in the other
direction, pins versions the release no longer supports). Measured 2026-10-23:
an installed generation ran onnxruntime 1.31.0, opencv 5.0 and pymupdf 1.28.2
against a lock of 1.24.2, 4.13 and 1.27.1, because nothing passed the lock.

REGENERATE with ``scripts/regen-install-constraints.sh`` after ANY change to
``uv.lock`` and commit the result. The release flow (``.claude/skills/release``
Step 5) runs it right after the lock refresh; this test is what makes that step
mandatory, because it fails the default suite (and so the release battery)
while the two disagree.

Two independent sources are compared: a fresh ``uv export`` of the lock, and a
direct read of ``uv.lock`` as TOML. The export alone would still pass if the
generator and the test shared a bug in how they read uv's output.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

_REPO = Path(__file__).resolve().parents[1]
_SHIPPED = _REPO / "src" / "nexus" / "_install" / "constraints.txt"
_LOCK = _REPO / "uv.lock"

#: The packages whose drift motivated the file (T2 nexus/install-weight-
#: measurement-2026-10-23), plus mcp (nexus-l2ku5). Each must be pinned.
_WITNESSES = ("onnxruntime", "opencv-python-headless", "pymupdf", "mcp")

#: The lock has ~220 pins today. A floor, not a count: it only has to be far
#: above "the export came back nearly empty".
_FLOOR = 150


def _normalise(line: str) -> str:
    """One pin as a comparable string: the ``+cpu`` local label dropped (the
    generator drops it so a GPU backend still resolves) and whitespace
    collapsed."""
    line = re.sub(r"^([A-Za-z0-9._-]+==[0-9][^ ;+]*)\+cpu", r"\1", line.strip())
    return " ".join(line.split())


def _pin_lines(text: str) -> list[str]:
    return [_normalise(ln) for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def _fresh_export() -> list[str]:
    uv = shutil.which("uv")
    assert uv, "uv is not on PATH; the pin cannot be checked, and a skipped pin proves nothing"
    out = subprocess.run(  # noqa: S603 — fixed argv
        [uv, "export", "--frozen", "--no-hashes", "--no-annotate", "--no-header",
         "--no-emit-project", "--no-dev", "--all-extras", "--directory", str(_REPO)],
        capture_output=True, text=True, check=True, timeout=120,
    ).stdout
    return _pin_lines(out)


def test_the_shipped_constraints_are_the_lock() -> None:
    assert _SHIPPED.is_file(), (
        f"{_SHIPPED} is missing; run scripts/regen-install-constraints.sh and commit it"
    )
    shipped = _pin_lines(_SHIPPED.read_text())
    fresh = _fresh_export()
    assert len(fresh) >= _FLOOR, f"uv export returned only {len(fresh)} pins; the comparison would be vacuous"
    missing = sorted(set(fresh) - set(shipped))
    extra = sorted(set(shipped) - set(fresh))
    assert not missing and not extra, (
        "src/nexus/_install/constraints.txt is out of step with uv.lock. "
        "Run scripts/regen-install-constraints.sh and commit the result.\n"
        f"  in the lock, not in the file ({len(missing)}): {missing[:8]}\n"
        f"  in the file, not in the lock ({len(extra)}): {extra[:8]}"
    )
    assert len(shipped) == len(set(shipped)), "duplicate pins in the shipped file"


def test_every_pin_is_a_version_the_lock_holds() -> None:
    """Cross-check against uv.lock read as TOML, not through ``uv export``."""
    locked: dict[str, set[str]] = {}
    for pkg in tomllib.loads(_LOCK.read_text())["package"]:
        locked.setdefault(canonicalize_name(pkg["name"]), set()).add(pkg["version"].split("+")[0])
    pins = _pin_lines(_SHIPPED.read_text())
    assert len(pins) >= _FLOOR
    stray: list[str] = []
    for line in pins:
        req = Requirement(line)
        specs = list(req.specifier)
        assert len(specs) == 1 and specs[0].operator == "==", f"not an exact pin: {line}"
        if specs[0].version not in locked.get(canonicalize_name(req.name), set()):
            stray.append(line)
    assert not stray, f"pins whose version uv.lock does not hold: {stray[:8]}"


@pytest.mark.parametrize("name", _WITNESSES)
def test_the_motivating_packages_are_pinned(name: str) -> None:
    """Non-vacuity: the packages that drifted are in the file, exactly pinned."""
    reqs = [Requirement(ln) for ln in _pin_lines(_SHIPPED.read_text())]
    mine = [r for r in reqs if canonicalize_name(r.name) == canonicalize_name(name)]
    assert mine, f"{name} is not pinned in the shipped constraints"
    assert all(str(next(iter(r.specifier))).startswith("==") for r in mine)


def test_the_project_itself_is_not_constrained() -> None:
    """``--no-emit-project``: conexus is named by the installer, so a version
    bump alone must not require regenerating the file."""
    names = {canonicalize_name(Requirement(ln).name) for ln in _pin_lines(_SHIPPED.read_text())}
    assert "conexus" not in names


def test_no_pin_carries_a_local_label_that_would_fence_out_a_gpu_backend() -> None:
    """``torch==X.Y.Z+cpu`` would refuse ``X.Y.Z+cu130`` (NX_TORCH_BACKEND=auto)."""
    labelled = [ln for ln in _pin_lines(_SHIPPED.read_text()) if re.match(r"^[^ ;]+==[^ ;]*\+", ln)]
    assert not labelled, f"local-labelled pins: {labelled}"
