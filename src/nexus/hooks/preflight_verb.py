# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-hwbj (GH #619): conexus-plugin preflight, ported to a wheel verb
(RDR-215 bead nexus-q02nx.21).

Runs at SessionStart. Checks whether the tools the conexus skills route
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

**Move, not rewrite (RDR-215 Approach item 9).** This module carries
``conexus/hooks/scripts/preflight.py``'s body across mechanically: same
dataclass, same probe/hint helpers, same output text. The one structural
change is the exit boundary -- the original script's ``main()`` called
``sys.exit(0)`` directly from two places (silent-success and the FAILED
marker); this module's :func:`run` returns a
:class:`~nexus._hook_runtime._io.HookResult` instead, since
``nexus._hook_runtime.entry.main`` forces exit 0 for every non-ledger verb
regardless of what ``run`` returns (this verb is not in
``LEDGER_VERBS``).

**STDLIB ONLY.** This module must not import ``structlog``, must not import
``nexus.hooks`` (whose package ``__init__`` imports ``structlog`` and
``nexus.session`` at module scope), and must not import
``nexus.logging_setup`` -- the whole point of ``nexus._hook_runtime`` is
that a stdlib-only verb pays the ~20 ms dispatch floor, not the ~75 ms a
``structlog`` import costs (nexus-br31l). This verb never logs, so unlike
``session_start_verb.py`` it has no reason to call
``configure_hook_logging()`` either.

**Does not read stdin.** ``nexus._hook_runtime.entry.main`` already calls
``read_payload`` (or passes ``None`` for a ledger verb) before dispatching
to ``run`` -- this verb ignores the payload entirely, exactly as the
original script never read stdin at all (its only external inputs are
``PATH`` lookups via ``shutil.which`` and the two version-probe
subprocesses).

**Wired since bead ``nexus-q02nx.21``** -- ``conexus/hooks/hooks.json``
declares this as ``{"command": "nx-hook", "args": ["preflight"]}``, the
second SessionStart entry, and ``preflight.py`` and the
``_run_python_hook.sh`` launcher that used to run it are both gone. The
declaration is INERT until the next plugin cut, because ``marketplace.json``
pins ``source.ref`` to a release tag -- see ``conexus/PENDING_RELEASE.md``.

**Not a ledger verb.** ``preflight`` never appears in
``nexus._hook_runtime.entry.VERB_TABLE`` or ``LEDGER_VERBS``: it has no
caller that branches on an exit code, so ``HookResult``'s default
``exit_code=0`` is exactly right, and ``entry.main`` forces 0 for every
non-ledger verb regardless.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

from nexus._hook_runtime._io import HookResult


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
            "win32":  "winget install --id astral-sh.uv --scope user && uv tool install conexus --python 3.12",
            "darwin": "brew install uv && uv tool install conexus --python 3.12",
            "linux":  "curl -LsSf https://astral.sh/uv/install.sh | sh && uv tool install conexus --python 3.12",
        },
        "bd": {
            "win32":  "https://github.com/BeadsProject/beads/releases   (download for your OS)",
            "darwin": "https://github.com/BeadsProject/beads/releases   (download for macOS)",
            "linux":  "https://github.com/BeadsProject/beads/releases   (download for Linux)",
        },
    }
    plat = "win32" if is_windows else "darwin" if is_macos else "linux"
    return hints.get(name, {}).get(plat, "")


def run(payload: dict | None) -> HookResult:  # noqa: ARG001 — preflight never reads its payload; see module docstring
    """Run the preflight checks. Ignores *payload*: this verb never reads
    stdin, exactly as the original script never did.
    """
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
        return HookResult(stdout=None)

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
        "exploration; do NOT attempt to invoke conexus skills "
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
    return HookResult(stdout="\n".join(out))
