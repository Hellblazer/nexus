# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-126 spike (nexus-vlo2b): deliver the first-run banner via the MCP
server ``instructions`` field instead of (or in addition to) the
tool-response content prepend.

P6-B (2026-06-02) found the content-prepend banner is delivered into the
tool result but Claude Desktop's model paraphrases the result and drops
it, so the user never sees it. ``instructions`` rides the ``initialize``
handshake (delivered before any tool call, as standing server context),
the same channel ``_first_run.apply_embedder_notice`` already uses. This
spike answers: does the banner survive into the client's
``InitializeResult.instructions``?

Run: uv run python scripts/spikes/spike_rdr126_instructions_banner.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


# --- prototype: the instructions-channel banner -------------------------------
# Mirrors apply_embedder_notice: writes to the low-level server.instructions.
# Unlike content-prepend, instructions is static for the session and delivered
# at initialize, so the one-shot marker is written immediately (initialize
# always happens; there is no "delivery might fail" retry window).
_PROTOTYPE = r'''
from nexus.mcp import _first_run
from nexus.daemon.installer import InstallStatus


def apply_first_run_banner_instructions(server, status, dest) -> bool:
    if _first_run._first_run_marker_path().exists():
        return False
    spec = _first_run.maybe_banner(status, dest)
    if spec is None:
        return False
    # Frame as an explicit relay instruction so the model surfaces it once,
    # rather than treating it as passive server documentation.
    notice = (
        "FIRST-RUN NOTICE (surface this to the user once, verbatim, at the "
        "start of your first reply): " + spec.text
    )
    try:
        low = server._mcp_server
        existing = getattr(low, "instructions", None)
        low.instructions = f"{existing}\n\n{notice}" if existing else notice
        _first_run.mark_shown()
        return True
    except Exception:
        return False
'''

# A tiny server program that builds a FastMCP, applies the prototype, and serves.
_SERVER = _PROTOTYPE + r'''
from mcp.server.fastmcp import FastMCP
from nexus.daemon.installer import InstallStatus
from pathlib import Path

mcp = FastMCP("spike-banner", instructions="Base server instructions.")

@mcp.tool(description="echo")
def echo(value: str) -> str:
    return value

applied = apply_first_run_banner_instructions(
    mcp, InstallStatus.NEWLY_INSTALLED, Path("/x/com.nexus.t2.plist")
)
import sys as _sys
print(f"[server] banner-into-instructions applied={applied}", file=_sys.stderr)
mcp.run()
'''


async def _probe(env: dict[str, str], server_py: Path) -> str | None:
    params = StdioServerParameters(
        command=sys.executable, args=[str(server_py)], env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            init = await asyncio.wait_for(s.initialize(), timeout=30)
            return init.instructions


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        server_py = td / "spike_server.py"
        server_py.write_text(_SERVER)

        # Case 1: marker ABSENT -> banner should land in instructions.
        cfg1 = td / "cfg1"
        env1 = os.environ.copy()
        env1["NEXUS_CONFIG_DIR"] = str(cfg1)
        instr1 = asyncio.run(_probe(env1, server_py))
        print("\n=== Case 1: marker absent (banner expected in instructions) ===")
        print(instr1)
        ok_present = instr1 is not None and "daemon" in instr1.lower() and "daemon_uninstall" in instr1
        print(f"  [{'PASS' if ok_present else 'FAIL'}] banner text present in InitializeResult.instructions")
        ok_relay = instr1 is not None and "surface this to the user" in instr1.lower()
        print(f"  [{'PASS' if ok_relay else 'FAIL'}] framed as an explicit relay instruction")
        ok_base = instr1 is not None and "Base server instructions." in instr1
        print(f"  [{'PASS' if ok_base else 'FAIL'}] base instructions preserved (banner appended, not clobbered)")
        marker1 = cfg1 / ".mcp_first_run_complete"
        print(f"  [{'PASS' if marker1.exists() else 'FAIL'}] one-shot marker written at startup")
        for cond, label in [(ok_present, "present"), (ok_relay, "relay"), (ok_base, "base"), (marker1.exists(), "marker")]:
            if not cond:
                failures.append(label)

        # Case 2: marker PRESENT -> no banner, instructions unchanged.
        cfg2 = td / "cfg2"
        cfg2.mkdir(parents=True)
        (cfg2 / ".mcp_first_run_complete").touch()
        env2 = os.environ.copy()
        env2["NEXUS_CONFIG_DIR"] = str(cfg2)
        instr2 = asyncio.run(_probe(env2, server_py))
        print("\n=== Case 2: marker present (no banner) ===")
        print(instr2)
        ok_noban = instr2 == "Base server instructions."
        print(f"  [{'PASS' if ok_noban else 'FAIL'}] instructions unchanged when marker present")
        if not ok_noban:
            failures.append("case2")

    print()
    if failures:
        print(f"[spike] RESULT: FAIL -- {failures}")
        return 1
    print("[spike] RESULT: PASS -- the banner survives into InitializeResult.instructions,")
    print("        framed as a relay instruction, one-shot, base-preserving.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
