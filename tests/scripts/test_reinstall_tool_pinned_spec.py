# SPDX-License-Identifier: AGPL-3.0-or-later
"""A pinned registry spec keeps its extras before the pin. nexus-jea5y.

``claude-restart-sessions --nx 7.86.0`` runs
``scripts/reinstall-tool.sh conexus==7.86.0 --allow-registry-over-dev``. On a box
whose receipt carries ``pdf`` the script handed the builder the whole spec as the
source plus ``--extras pdf``, and ``build_spec`` appended the extras to it:
``conexus==7.86.0[pdf]``, which uv refuses. The restart aborted with nothing
upgraded. The real script runs here against the stub ``uv`` and the spec the
builder recorded is read back from the receipt.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from _generation_harness import SAFE_BASE_PATH, stub_uv

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "reinstall-tool.sh"


@pytest.mark.parametrize(
    ("arg", "extras", "spec"),
    [
        ("conexus==7.86.0", "pdf", "conexus[pdf]==7.86.0"),
        ("conexus[local]==7.86.0", "pdf", "conexus[local,pdf]==7.86.0"),
        ("conexus==7.86.0", "", "conexus==7.86.0"),
    ],
)
def test_the_recorded_spec_puts_extras_before_the_pin(tmp_path, arg, extras, spec) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    stub_uv(stub_bin)
    env = {
        "PATH": f"{stub_bin}:{SAFE_BASE_PATH}",
        "HOME": str(tmp_path / "home"),
        "NX_TOOLS_DIR": str(tools),
        "NX_NO_TELEMETRY": "1",
    }
    if extras:
        env["NX_REINSTALL_EXTRAS"] = extras

    r = subprocess.run(
        ["bash", str(_SCRIPT), arg, "--allow-registry-over-dev"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )

    receipts = sorted(tools.glob("gen-*/nexus-install.json"))
    assert receipts, f"no generation was built (exit {r.returncode}): {r.stderr}"
    receipt = json.loads(receipts[-1].read_text())
    assert receipt["spec"] == spec, receipt
    assert receipt["source"] == "conexus", receipt
    assert receipt["source_kind"] == "registry", receipt
