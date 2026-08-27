# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Shared harness for the scripts/reinstall-tool.sh suites. nexus-utpuw.8.

_SAFE_BASE_PATH lived in THREE byte-identical copies (the cycle-MCP module,
the classifier module, and the downgrade module). It is a safety property, not
a convenience: ~/.local/bin holds the real shims, and a test that finds the
real ``nx`` is testing the developer's machine rather than the script. Three
copies of a safety property is two chances for one to drift quietly, so the
rewrite that deleted two of those modules consolidates it here.
"""
from __future__ import annotations

import json
import stat
from pathlib import Path

# Deliberately EXCLUDES ~/.local/bin and every uv-tool-managed bin dir. Enough
# of the real system PATH for bash/git/python3/sed/grep/ps to resolve, nothing
# that could reach a real install.
SAFE_BASE_PATH = ":".join(
    p for p in (
        "/opt/homebrew/bin",
        "/opt/homebrew/opt/python@3.13/libexec/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    )
    if Path(p).is_dir()
)

ENTRY_POINTS = ["nx", "nx-mcp", "nx-mcp-catalog", "nx-session-end-launcher"]


def make_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def stub_uv(bin_dir: Path, *, marker: Path | None = None) -> None:
    """A fake ``uv`` whose ``venv`` fabricates a generation the shim writer can
    actually read: a python that answers the entry-point query, and one
    executable per declared script.

    Touches *marker* on ``venv`` when given, so a test can assert whether the
    build step was REACHED — the successor to the old stub's ``tool install``
    marker, since the new path never calls ``uv tool install`` at all.
    """
    touch = f'touch "{marker}"' if marker is not None else ":"
    eps = " ".join(ENTRY_POINTS)
    make_executable(bin_dir / "uv", f"""#!/bin/bash
if [[ "$1" == "venv" ]]; then
    {touch}
    target=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --python) shift 2 ;;
            -*) shift ;;
            venv) shift ;;
            *) target="$1"; shift ;;
        esac
    done
    mkdir -p "$target/bin"
    : > "$target/declared-entry-points.txt"
    for ep in {eps}; do echo "$ep" >> "$target/declared-entry-points.txt"; done
    printf '#!/bin/sh\\ncat "%s"\\n' "$target/declared-entry-points.txt" > "$target/bin/python"
    chmod +x "$target/bin/python"
    for ep in {eps}; do
        printf '#!/bin/sh\\necho "nx, version 9.9.9"\\n' > "$target/bin/$ep"
        chmod +x "$target/bin/$ep"
    done
    printf 'home = /opt/py/bin\\nversion = 3.12.8\\n' > "$target/pyvenv.cfg"
    exit 0
fi
exit 0
""")


def fabricate_generation(
    tools: Path, name: str, *, version: str, source_kind: str = "directory",
    extras: str = "", nx_sleeps: bool = False,
) -> Path:
    """A complete, valid generation: receipt, entry points, answerable python.

    ``nx`` answers ``--version`` rather than sleeping even when *nx_sleeps* is
    False for the others, because the surviving downgrade guard RUNS it to
    resolve the installed version (nexus-zfutt). A sleeping ``nx`` here does
    not fail a test, it hangs it.
    """
    gen = tools / f"gen-{name}"
    (gen / "bin").mkdir(parents=True)
    names = gen / "declared-entry-points.txt"
    names.write_text("".join(f"{ep}\n" for ep in ENTRY_POINTS))
    make_executable(gen / "bin" / "python", f'#!/bin/sh\ncat "{names}"\n')
    for ep in ENTRY_POINTS:
        if ep == "nx":
            make_executable(gen / "bin" / ep, f'#!/bin/sh\necho "nx, version {version}"\n')
        elif nx_sleeps:
            make_executable(gen / "bin" / ep, "#!/bin/sh\nsleep 120\n")
        else:
            make_executable(gen / "bin" / ep, f'#!/bin/sh\necho "ran-{ep}"\n')
    (gen / "pyvenv.cfg").write_text("home = /opt/py/bin\nversion = 3.12.8\n")
    (gen / "nexus-install.json").write_text(json.dumps({
        "schema": 1, "version": version, "spec": "conexus",
        "source_kind": source_kind, "source": "/src/nexus", "extras": extras,
        "python": "3.12", "base_interpreter": "/opt/py/bin",
        "created_at": "2026-08-25T00:00:00Z", "installer_schema": 1,
    }))
    return gen
