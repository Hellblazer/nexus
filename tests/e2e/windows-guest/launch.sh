#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Open a signed-in Claude Code window in the Windows gate guest, from the Mac.
# Used by the RDR-224 Phase 5 gate (tests/e2e/windows-phase5-gate.ps1).
#
#   tests/e2e/windows-guest/launch.sh [--stage FILE]... [--prompt TEXT] [--permission-mode MODE]
#
# Each run closes the previous gate window and opens a new one in the guest's
# console session, so it is also how the session is restarted (a plugin
# install, or the stack the gate's setup phase started). --stage copies files
# into the guest's %USERPROFILE%\nx-gate\ first. --prompt starts the session with
# that prompt and --permission-mode passes Claude Code's mode (e.g. auto), so a
# run needs nobody at the guest's console; the guest's first-run onboarding and
# the home folder's trust prompt are pre-accepted on every launch.
#
# Credentials (RDR-219): the guest password comes from the macOS keychain and
# travels on ssh stdin to host-recv.ps1. The automation token never touches
# this script: claude_credentials.py run --remote puts it in the environment of
# token-send.sh on the host, which writes it into host-recv.ps1's pipe; from
# there it reaches the guest launcher through PowerShell Direct and a second
# pipe. It is never written to a file, a task definition or a command line.
# Residual exposure, accepted for a disposable test guest: the claude process
# and its children hold it in their environment for the session's life, and a
# Hyper-V checkpoint taken while the window is open stores it in saved memory,
# so take checkpoints with the gate window closed.
# RDR-219's inventory does not list this shape yet (nexus-pwscd).
#
# Host shape (qwentescence): the ssh endpoint is elevated PowerShell; the
# POSIX shell the helper needs is Git for Windows' bash, because WSL interop on
# that host is disabled by policy.
set -euo pipefail

HOST="${NX_GUEST_HOST:-qwentescence}"
VM="${NX_GUEST_VM:-nx-clean-win11}"
GUSER="${NX_GUEST_USER:-nxguest}"
KEYCHAIN_SERVICE="${NX_GUEST_KEYCHAIN_SERVICE:-nexus-win-guest}"
REMOTE_SHELL="${NX_GUEST_POSIX_SHELL:-C:/PROGRA~1/Git/bin/bash.exe -s --}"
HOST_DIR='C:/build/guest/nxgate'

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
# shellcheck source=../lib/python.sh
source "$repo/tests/e2e/lib/python.sh"
e2e_python_resolve || exit 2

stage=()
claude_args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --prompt) [[ $# -ge 2 ]] || { echo "launch.sh: --prompt needs text" >&2; exit 2; }
                  claude_args+=("$2"); shift 2 ;;
        --permission-mode) [[ $# -ge 2 ]] || { echo "launch.sh: --permission-mode needs a mode" >&2; exit 2; }
                  claude_args=("--permission-mode" "$2" "${claude_args[@]}"); shift 2 ;;
        --stage) [[ $# -ge 2 ]] || { echo "launch.sh: --stage needs a file" >&2; exit 2; }
                 [[ -f "$2" ]] || { echo "launch.sh: no such file: $2" >&2; exit 2; }
                 stage+=("$2"); shift 2 ;;
        *) echo "usage: launch.sh [--stage FILE]... [--prompt TEXT] [--permission-mode MODE]" >&2; exit 2 ;;
    esac
done

quiet() { grep -v -i -E 'post-quantum|store now, decrypt later|pq\.html' || true; }

# The parentheses matter: cmd binds a bare "& del" to the if's body, so without
# them nothing is deleted whenever the directory exists, and a stale staged file
# overwrites the guest's copy on every launch (it did, 2026-10-06).
ssh "$HOST" 'cmd /c "(if not exist C:\build\guest\nxgate\stage mkdir C:\build\guest\nxgate\stage) & del /q C:\build\guest\nxgate\stage\*"' 2>&1 | quiet
scp -q "$here/host-recv.ps1" "$here/token-send.ps1" "$here/token-send.sh" "$HOST:$HOST_DIR/" 2>&1 | quiet
if [[ ${#claude_args[@]} -gt 0 ]]; then
    argsdir="$(mktemp -d "${TMPDIR:-/tmp}/nxgate-args.XXXXXX")"
    printf '%s\n' "${claude_args[@]}" > "$argsdir/claude-args.txt"
    stage+=("$argsdir/claude-args.txt")
fi
if [[ ${#stage[@]} -gt 0 ]]; then
    scp -q "${stage[@]}" "$HOST:$HOST_DIR/stage/" 2>&1 | quiet
fi

out="$(mktemp "${TMPDIR:-/tmp}/nxgate-recv.XXXXXX")"
recv=''
cleanup() {
    # A receiver left behind holds the nxgate-host pipe (and the guest
    # password in its memory) for up to 180 s and makes the next launch fail.
    if [[ -n "$recv" ]] && kill -0 "$recv" 2>/dev/null; then kill "$recv" 2>/dev/null || true; wait "$recv" 2>/dev/null || true; fi
    rm -f "$out"
    [[ -n "${argsdir:-}" ]] && rm -rf "$argsdir"
}
trap cleanup EXIT
security find-generic-password -a "$GUSER" -s "$KEYCHAIN_SERVICE" -w \
    | ssh "$HOST" "powershell -NoProfile -ExecutionPolicy Bypass -File C:\\build\\guest\\nxgate\\host-recv.ps1 -VMName $VM -GuestUser $GUSER" \
    >"$out" 2>&1 &
recv=$!

for _ in $(seq 1 60); do
    grep -q host-pipe-listening "$out" && break
    kill -0 "$recv" 2>/dev/null || break
    sleep 1
done
if ! grep -q host-pipe-listening "$out"; then
    quiet <"$out" >&2
    echo "launch.sh: the host receiver never started listening" >&2
    kill "$recv" 2>/dev/null || true
    exit 1
fi

"$E2E_PYTHON" "$repo/tests/e2e/lib/claude_credentials.py" run --remote "$HOST" \
    --remote-shell "$REMOTE_SHELL" -- "$HOST_DIR/token-send.sh" 2>&1 | quiet
rc=0
wait "$recv" || rc=$?
recv=''
quiet <"$out"
if [[ $rc -ne 0 ]] || ! grep -q 'guest: handed-off' "$out"; then
    echo "launch.sh: FAILED (receiver exit $rc)" >&2
    exit 1
fi
echo "launch.sh: Claude Code is open in the guest's console session (window 'Phase 5 gate: claude (automation token)')."
