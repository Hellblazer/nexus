# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""scripts/reinstall-tool.sh installs the lock, on both of its source shapes.
nexus-2k66t.4.1.

* A CHECKOUT source exports the checkout's own ``uv.lock`` (frozen: never
  re-resolves) and hands it to the builder, so a developer's install runs what
  the gates ran, including a lock edit not yet regenerated into the shipped
  file.
* A REGISTRY source (``conexus==X``, with ``--allow-registry-over-dev`` over a
  dev box) carries no lock, so the builder's default applies: fetch alone, then
  install under the constraints the fetched package ships.

``uv`` is a stub that records argv and the CONTENT of the constraints file at
the moment ``uv pip install`` reads it (the script deletes its temp file
afterwards). It proves the orchestration, not that uv honours the file; the
bootstrap test in tests/test_self_install_bootstrap.py does that against real uv.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from _generation_harness import SAFE_BASE_PATH, make_executable, stub_uv

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "reinstall-tool.sh"


def _bed(tmp_path: Path):
    stub_bin = tmp_path / "stubbin"
    inner = tmp_path / "inner"
    stub_bin.mkdir()
    stub_uv(inner)  # fabricates a venv the shim writer can read
    log = tmp_path / "uv.log"
    make_executable(stub_bin / "uv", f"""#!/bin/bash
echo "$@" >> "{log}"
if [[ "$1" == "export" ]]; then
    echo "mcp==1.2.3"
    echo "onnxruntime==1.24.2 ; sys_platform == 'darwin'"
    exit 0
fi
if [[ "$1" == "pip" && "$2" == "install" ]]; then
    py=""; cons=""; prev=""; nodeps=0
    for a in "$@"; do
        [[ "$prev" == "--python" ]] && py="$a"
        [[ "$prev" == "--constraints" ]] && cons="$a"
        [[ "$a" == "--no-deps" ]] && nodeps=1
        prev="$a"
    done
    if [[ -n "$cons" ]]; then
        echo "CONSTRAINTS-CONTENT: $(tr '\\n' '|' < "$cons")" >> "{log}"
    fi
    if [[ "$nodeps" -eq 1 ]]; then
        site="$(dirname "$(dirname "$py")")/lib/python3.12/site-packages/nexus/_install"
        mkdir -p "$site"
        echo "shipped==9.9.9" > "$site/constraints.txt"
    fi
    exit 0
fi
exec "{inner}/uv" "$@"
""")
    tools, bin_dir, home = tmp_path / "tools", tmp_path / "bin", tmp_path / "home"
    for d in (tools, bin_dir, home):
        d.mkdir()
    return stub_bin, log, tools, bin_dir, home


def _run(tmp_path: Path, source: str, *flags: str):
    stub_bin, log, tools, bin_dir, home = _bed(tmp_path)
    env = dict(os.environ)
    env.update({
        "PATH": f"{stub_bin}:{SAFE_BASE_PATH}", "HOME": str(home),
        "NX_TOOLS_DIR": str(tools), "NX_BIN_DIR": str(bin_dir),
    })
    proc = subprocess.run(
        ["bash", str(_SCRIPT), source, *flags],
        env=env, capture_output=True, text=True, timeout=120,
    )
    return proc, log.read_text() if log.exists() else ""


def test_a_checkout_install_exports_its_own_lock_and_passes_it(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text('[project]\nname = "conexus"\nversion = "9.9.9"\n')
    (checkout / "uv.lock").write_text("version = 1\n")
    proc, log = _run(tmp_path, str(checkout))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    installs = [ln for ln in log.splitlines() if ln.startswith("pip install")]
    assert len(installs) == 1, log  # the explicit file is the whole answer
    assert "--constraints " in installs[0] and "--no-deps" not in installs[0]
    assert "CONSTRAINTS-CONTENT: mcp==1.2.3|onnxruntime==1.24.2 ; sys_platform == 'darwin'|" in log


def test_a_registry_install_uses_the_constraints_the_package_ships(tmp_path: Path) -> None:
    proc, log = _run(tmp_path, "conexus==9.9.9", "--allow-registry-over-dev")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    installs = [ln for ln in log.splitlines() if ln.startswith("pip install")]
    assert len(installs) == 2, log
    assert "--no-deps" in installs[0] and "--constraints" not in installs[0]
    assert "--constraints " in installs[1] and "--no-deps" not in installs[1]
    assert "CONSTRAINTS-CONTENT: shipped==9.9.9|" in log
    assert not [ln for ln in log.splitlines() if ln.startswith("export")], "a registry source has no lock to export"
