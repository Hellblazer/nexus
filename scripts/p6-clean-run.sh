#!/usr/bin/env bash
# RDR-126 P6-A (nexus-awh4q): isolated "clean machine" verification of the
# first-run banner + daemon_uninstall code paths, WITHOUT a fresh OS account
# and WITHOUT touching your real ~/.claude, your live T2 daemon, or your
# rotating CLI subscriptions.
#
# Modelled on recording-rig's meta-rig-runner.sh (isolated HOME) + the
# cc-validation launchctl-shim idea. Mechanism:
#
#   1. mktemp an isolated $HOME. nexus resolves every install path off
#      Path.home(): the LaunchAgent lands in $SANDBOX/Library/LaunchAgents
#      and the first-run marker in $SANDBOX/.config/nexus — never your real ~.
#   2. Shim `launchctl`/`systemctl` onto PATH so "activation" returns 0
#      without registering a service into your real launchd gui domain
#      (the real daemon's label is com.nexus.t2 — a real bootstrap would
#      collide). The plist is still written + read for real.
#   3. Run the BRANCH nx-mcp (python -m nexus.mcp.core via uv) over a raw
#      MCP stdio client and drive memory_put -> memory_get -> daemon_uninstall.
#      memory_* is pure T2/SQLite, so no Voyage/Chroma creds are needed.
#
# This exercises THIS PR's code. The literal fresh-account Desktop .mcpb run
# (Recipe B) still belongs after merge+release, because the .mcpb resolves
# conexus from PyPI.
#
# Usage:  scripts/p6-clean-run.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

BRANCH="$(git -C "$REPO" rev-parse --abbrev-ref HEAD)"
echo "[p6] repo=$REPO branch=$BRANCH"

command -v uv >/dev/null || { echo "[p6] ERROR: uv not on PATH" >&2; exit 1; }

SANDBOX="$(mktemp -d /tmp/p6-clean-XXXXXX)"
echo "[p6] sandbox HOME = $SANDBOX"

cleanup() {
  local rc=$?
  # Best-effort: stop any sandbox daemon spawned by ensure-running, then nuke.
  HOME="$SANDBOX" PATH="$SANDBOX/bin:$PATH" nx daemon t2 stop >/dev/null 2>&1 || true
  rm -rf "$SANDBOX"
  echo "[p6] cleanup done (sandbox removed)"
  return $rc
}
trap cleanup EXIT

# --- launchctl / systemctl shims: succeed without touching real launchd -------
mkdir -p "$SANDBOX/bin"
for tool in launchctl systemctl; do
  cat > "$SANDBOX/bin/$tool" <<SHIM
#!/usr/bin/env bash
echo "[$tool-shim] \$*" >> "$SANDBOX/activation.log"
exit 0
SHIM
  chmod +x "$SANDBOX/bin/$tool"
done

# --- the MCP-client probe -----------------------------------------------------
PROBE="$SANDBOX/p6_probe.py"
cat > "$PROBE" <<'PY'
import os, sys, asyncio
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SANDBOX = Path(os.environ["HOME"])
PLIST = SANDBOX / "Library" / "LaunchAgents" / "com.nexus.t2.plist"
MARKER = SANDBOX / ".config" / "nexus" / ".mcp_first_run_complete"

server = StdioServerParameters(
    command=sys.executable,
    args=["-m", "nexus.mcp.core"],
    env=os.environ.copy(),  # HOME + shimmed PATH + NX_T1_ISOLATED already set
)

failures: list[str] = []

def check(cond: bool, label: str) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)

def text_of(result) -> str:
    parts = []
    for block in getattr(result, "content", []) or []:
        t = getattr(block, "text", None)
        if isinstance(t, str):
            parts.append(t)
    return "\n".join(parts)

async def main() -> int:
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as s:
            await asyncio.wait_for(s.initialize(), timeout=60)

            print("[p6] Step 2 — first tool call (banner expected):")
            put = await asyncio.wait_for(
                s.call_tool("memory_put", {
                    "content": "hello", "project": "_p6_test",
                    "title": "mvv", "ttl": 0,
                }), timeout=60)
            put_txt = text_of(put)
            print("  ---- first response ----")
            for line in put_txt.splitlines():
                print(f"  | {line}")
            print("  ------------------------")
            check("Stored:" in put_txt, "memory_put stored the entry")
            check("daemon" in put_txt.lower(), "banner text present on first response")
            check("daemon_uninstall" in put_txt, "banner carries the in-chat uninstall hint")
            check(PLIST.exists(), f"LaunchAgent written into sandbox ({PLIST.name})")
            check(MARKER.exists(), "first-run marker written after delivery")

            print("[p6] Step 3 — memory round-trip:")
            got = text_of(await asyncio.wait_for(
                s.call_tool("memory_get", {"project": "_p6_test", "title": "mvv"}),
                timeout=60))
            check("hello" in got, "memory_get round-trips the payload")
            check("daemon" not in got.lower() or "Stored" in got,
                  "banner is one-shot (not repeated on 2nd call)")

            print("[p6] Step 4a — daemon_uninstall(confirm=false) dry run:")
            dry = text_of(await asyncio.wait_for(
                s.call_tool("daemon_uninstall", {"confirm": False}), timeout=60))
            for line in dry.splitlines():
                print(f"  | {line}")
            check("confirm" in dry.lower(), "dry-run asks for confirm=true")
            check(PLIST.exists(), "dry-run did NOT remove the unit")

            print("[p6] Step 4b — daemon_uninstall(confirm=true):")
            done = text_of(await asyncio.wait_for(
                s.call_tool("daemon_uninstall", {"confirm": True}), timeout=60))
            for line in done.splitlines():
                print(f"  | {line}")
            check(not PLIST.exists(), "unit removed after confirm=true")
            check(not MARKER.exists(), "first-run marker cleared after uninstall")

    return 1 if failures else 0

rc = asyncio.run(main())
print()
if rc == 0:
    print("[p6] RESULT: PASS — banner + round-trip + uninstall all verified.")
else:
    print(f"[p6] RESULT: FAIL — {len(failures)} check(s) failed: {failures}")
sys.exit(rc)
PY

echo "[p6] launching branch nx-mcp under isolated HOME (server log -> $SANDBOX/nxmcp.stderr)"

# NX_T1_ISOLATED=1 -> in-process EphemeralClient, no T1 chroma spawn (we only
# touch T2). HOME + shimmed PATH isolate every install/marker/daemon path.
HOME="$SANDBOX" \
PATH="$SANDBOX/bin:$PATH" \
NX_T1_ISOLATED=1 \
uv run --project "$REPO" --quiet python "$PROBE" 2> "$SANDBOX/nxmcp.stderr"
RC=$?

echo
echo "[p6] activation.log (shimmed launchctl calls — proves no real launchd touched):"
sed 's/^/  /' "$SANDBOX/activation.log" 2>/dev/null || echo "  (none captured)"
echo "[p6] (full server stderr at $SANDBOX/nxmcp.stderr — sandbox is removed on exit;"
echo "      re-run with the trap disabled if you need to inspect it)"

exit $RC
