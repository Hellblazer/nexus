#!/usr/bin/env bash
# RDR-126 P6-B (nexus-awh4q): stand up an ISOLATED Claude Desktop profile for
# the literal fresh-account .mcpb Desktop Extension verification, without
# disturbing your primary Claude Desktop (a different subscription).
#
# Mechanism (mirrors recording-rig's desktop-doctor): Claude Desktop is an
# Electron app, so a separate --user-data-dir is an independently-authed second
# instance — the fresh-account-equivalent for the Desktop surface. We use a
# dedicated profile dir (Claude-P6) so it never clashes with the recording-rig
# Claude-Rig profile.
#
# HARD CONSTRAINT (recording-rig RDR-001 A9): a concurrent OAuth login across
# two Claude.app instances collides. This script REFUSES to launch while any
# Claude.app is running — quit your primary Claude Desktop first.
#
# PREREQUISITE: this is the POST-RELEASE half of P6. The Desktop .mcpb resolves
# `conexus` from PyPI, so it only carries the RDR-126 banner / daemon_uninstall
# code once this work is merged AND a release is cut. Running it before then
# verifies nothing about this PR. The automated, pre-release half is
# scripts/p6-clean-run.sh.
#
# Usage:
#   scripts/p6-desktop-profile.sh launch     # create + open the isolated profile
#   scripts/p6-desktop-profile.sh cleanup    # remove the isolated profile dir
set -euo pipefail

P6_PROFILE="${P6_PROFILE:-$HOME/Library/Application Support/Claude-P6}"
CMD="${1:-launch}"

_refuse_if_claude_running() {
  if pgrep -x Claude >/dev/null 2>&1; then
    echo "ERROR: a Claude.app instance is running — quit it first." >&2
    echo "  (a concurrent OAuth login across two instances collides, RDR-001 A9)" >&2
    exit 6
  fi
}

case "$CMD" in
  launch)
    _refuse_if_claude_running
    mkdir -p "$P6_PROFILE"
    echo "[p6-desktop] isolated profile: $P6_PROFILE"
    # NEVER pass --remote-debugging-* (the app guard quits).
    open -n -a Claude --args \
      --user-data-dir="$P6_PROFILE" \
      --force-renderer-accessibility
    cat <<EOF

[p6-desktop] An isolated Claude Desktop launched on the Claude-P6 profile.
It is signed out and shares nothing with your primary Desktop.

Run this checklist inside THAT window:

  1. Sign in to the Desktop subscription you want to test with.
  2. Settings -> Extensions -> install the released conexus.mcpb
     (download it from the GitHub release assets first).
  3. Start a new chat. On the FIRST turn that touches nexus, confirm the
     first-run BANNER appears, e.g. ask:
       "Call memory_put to store project _p6_test, title mvv, content hello."
     Expect a banner like:
       "nexus: background knowledge daemon installed at <path> ... ask me to
        run the daemon_uninstall tool ..."
  4. Round-trip:  ask it to "call memory_get for project _p6_test, title mvv"
     -> must return "hello".
  5. In-chat uninstall: ask it to "call daemon_uninstall with confirm=true".
  6. Quit + relaunch this profile; confirm the daemon does NOT come back:
       ls "\$HOME/Library/LaunchAgents/com.nexus.t2.plist"   # expect: gone
     (NOTE: the Desktop .mcpb installs the daemon into your REAL
     ~/Library/LaunchAgents — the daemon is host-level, not per-profile.
     This is the one part that touches your real host; daemon_uninstall in
     step 5 is what cleans it back up. If you want to keep your normal
     daemon, run 'nx daemon t2 install --autostart' afterwards.)
  7. Record any gotchas in docs/desktop-deployment.md (MVV section).

When done, quit this Claude window, then:  scripts/p6-desktop-profile.sh cleanup
EOF
    ;;

  cleanup)
    _refuse_if_claude_running
    if [[ -d "$P6_PROFILE" ]]; then
      rm -rf "$P6_PROFILE"
      echo "[p6-desktop] removed isolated profile: $P6_PROFILE"
    else
      echo "[p6-desktop] nothing to remove (no profile at $P6_PROFILE)"
    fi
    ;;

  *)
    echo "usage: $0 {launch|cleanup}" >&2
    exit 2
    ;;
esac
