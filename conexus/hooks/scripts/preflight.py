#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
nexus-hwbj (GH #619): nx-plugin preflight.

Runs at SessionStart. Checks whether the tools the nx skills route
to are actually reachable. When everything works, emits NOTHING
(silent on healthy hosts). When something is missing or broken,
emits a "## nx Preflight: FAILED" marker that names the gap and
tells the model the using-nx-skills routing is unsafe in this
session.

Why a marker rather than gating the SKILL itself: skills are
loaded by Claude Code's plugin manager from the marketplace, not
from the SessionStart hook output. We can't unload the
``using-nx-skills`` routing once it's installed, but we can plant
a loud counter-signal in session context that the model will see
before it tries to call the routing.

Cross-platform by design: pure stdlib, ``shutil.which`` for tool
detection, no shell-out except a 3-second probe of ``nx
--version`` and ``bd --version`` (so a hung nx process can't
freeze SessionStart).

Exit code is always 0; failure mode is "emit the marker and
move on", never "block the session".
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class _ToolStatus:
    name: str
    available: bool
    detail: str
    install_hint: str


def _probe(name: str, args: list[str], timeout: float = 3.0) -> _ToolStatus:
    """Run ``args`` with the given ``timeout``. Returns availability +
    a short detail string.
    """
    path = shutil.which(args[0])
    if not path:
        return _ToolStatus(
            name=name, available=False,
            detail="not on PATH", install_hint="",
        )
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _ToolStatus(
            name=name, available=False,
            detail=f"hung > {timeout:.0f}s", install_hint="",
        )
    except OSError as exc:
        return _ToolStatus(
            name=name, available=False,
            detail=f"OSError: {exc}", install_hint="",
        )
    if result.returncode != 0:
        return _ToolStatus(
            name=name, available=False,
            detail=f"exit {result.returncode}", install_hint="",
        )
    detail = (result.stdout.strip().splitlines() or [""])[0][:80]
    return _ToolStatus(
        name=name, available=True, detail=detail, install_hint="",
    )


def _install_hint(name: str) -> str:
    """One-line install hint per tool, OS-aware. Windows uses winget
    --scope user (avoids UAC prompts on unattended install) per
    nexus-njmg; macOS uses brew; Ubuntu uses apt; otherwise points
    at the upstream URL.
    """
    is_windows = sys.platform == "win32"
    is_macos = sys.platform == "darwin"
    hints = {
        "nx": {
            "win32":  "winget install --id astral-sh.uv --scope user && uv tool install conexus",
            "darwin": "brew install uv && uv tool install conexus",
            "linux":  "curl -LsSf https://astral.sh/uv/install.sh | sh && uv tool install conexus",
        },
        "bd": {
            "win32":  "https://github.com/BeadsProject/beads/releases   (download for your OS)",
            "darwin": "https://github.com/BeadsProject/beads/releases   (download for macOS)",
            "linux":  "https://github.com/BeadsProject/beads/releases   (download for Linux)",
        },
    }
    plat = "win32" if is_windows else "darwin" if is_macos else "linux"
    return hints.get(name, {}).get(plat, "")


def main() -> None:
    checks = [
        _probe("nx (conexus CLI)",  ["nx", "--version"]),
        _probe("bd (beads, optional)", ["bd", "version"]),
    ]
    # bd is optional; only nx-being-broken triggers the degraded
    # marker. A missing bd reduces task-tracking convenience but
    # doesn't make the using-nx-skills routing unsafe.
    nx_ok = checks[0].available
    if nx_ok:
        # Healthy host: emit nothing. Existing SessionStart hooks
        # downstream of this one (session_start_hook.py + the
        # using-nx-skills cat) inject the normal routing guidance
        # and capability summary as before.
        sys.exit(0)

    # Degraded mode: surface a loud, named marker so the model can
    # see the gap and skip routing into broken backends.
    out: list[str] = []
    out.append("## nx Preflight: FAILED")
    out.append("")
    out.append(
        "The nx CLI is not reachable in this session. The "
        "``using-nx-skills`` routing guidance below is INACTIVE: "
        "any tool path that starts with ``nx ...`` or "
        "``mcp__plugin_conexus_nexus__*`` will fail. Fall back to "
        "direct ``Read`` / ``Grep`` / ``Glob`` for code "
        "exploration; do NOT attempt to invoke nx skills "
        "(``/conexus:query``, ``/conexus:debug``, ``/conexus:create-plan``, "
        "etc.); they will produce confusing partial errors."
    )
    out.append("")
    out.append("### Missing")
    for c in checks:
        if c.available:
            continue
        hint = _install_hint(c.name.split()[0])
        out.append(f"- **{c.name}** ({c.detail})")
        if hint:
            out.append(f"  - Install: ``{hint}``")
    out.append("")
    out.append(
        "Restart Claude Code after installing so the new tools "
        "land on the inherited PATH."
    )
    out.append("")
    print("\n".join(out))
    sys.exit(0)


if __name__ == "__main__":
    main()
