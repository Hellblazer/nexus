#!/usr/bin/env bash
# run-ca6-spike.sh — interactive runner for the RDR-111 CA-6 spike
# (nexus-1h26): capture the four `[inferred]` hook payloads.
#
# Each invocation runs ONE trigger and captures its hook stdin
# payload. Hook types:
#
#   UserPromptSubmit  — fires on every user turn (trivial trigger)
#   SubagentStop      — dispatch a Task subagent and wait for return
#   PreCompact        — manual /compact after pre-filling context
#   Notification      — permission prompt path (no --dangerously-skip-permissions)
#
# Usage:
#   ./tests/cc-validation/run-ca6-spike.sh UserPromptSubmit
#   ./tests/cc-validation/run-ca6-spike.sh SubagentStop
#   ./tests/cc-validation/run-ca6-spike.sh PreCompact
#   ./tests/cc-validation/run-ca6-spike.sh Notification
#
# Each invocation reuses ~/nexus-sandbox (installs via release-sandbox.sh
# smoke on first run) and writes captures to
# $HOME/nexus-sandbox/spike-ca6/<type>.jsonl. Print + persist after each.

set -euo pipefail

HOOK_TYPE="${1:-}"
case "$HOOK_TYPE" in
    UserPromptSubmit|SubagentStop|PreCompact|Notification) ;;
    *) echo "usage: $0 {UserPromptSubmit|SubagentStop|PreCompact|Notification}" >&2; exit 1 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SANDBOX="$HOME/nexus-sandbox"
TMUX_SESSION="ca6-spike-$HOOK_TYPE"
AUTH_DIR="$REPO_ROOT/tests/e2e/.claude-auth"
CAPTURE_DIR="$SANDBOX/spike-ca6"
LOGGER="$REPO_ROOT/nx/hooks/scripts/spike_ca6/log_payload.py"

if [[ ! -f "$AUTH_DIR/.credentials.json" ]]; then
    echo "ERROR: missing $AUTH_DIR/.credentials.json — run tests/e2e/auth-login.sh first" >&2
    exit 1
fi

# Reuse sandbox install across runs.
INSTALL_MARKER="$SANDBOX/.ca6-spike-installed"
if [[ ! -f "$INSTALL_MARKER" ]]; then
    echo "[setup] First run: release-sandbox.sh smoke (installs conexus venv) ..."
    "$REPO_ROOT/tests/e2e/release-sandbox.sh" smoke >/dev/null 2>&1 || true
    if [[ ! -d "$SANDBOX/.claude" ]]; then
        echo "[setup] smoke didn't populate sandbox; falling back to sandbox.sh ..."
        "$REPO_ROOT/tests/e2e/sandbox.sh" >/dev/null 2>&1
    fi
    touch "$INSTALL_MARKER"
    cp "$AUTH_DIR/.credentials.json" "$SANDBOX/.claude/.credentials.json"
fi

mkdir -p "$SANDBOX/.claude" "$CAPTURE_DIR"

# Pick the conexus venv python (the spike logger is pure stdlib but
# stay consistent with the CA-8 pattern).
STUB_PYTHON="$HOME/.local/share/uv/tools/conexus/bin/python3"
if [[ ! -x "$STUB_PYTHON" ]]; then
    STUB_PYTHON="$REPO_ROOT/.venv/bin/python"
fi

# ─── settings.json registers all four hooks ──────────────────────────────────
# Always register all four; only one trigger per run. Notification needs
# permissions.defaultMode=default (no auto-allow) so prompts fire.
case "$HOOK_TYPE" in
    Notification)
        # Permission mode must require approval for Bash so the prompt fires.
        SETTINGS_PERMS='{ "defaultMode": "default" }'
        # Don't pass --dangerously-skip-permissions in the launch.
        BYPASS_FLAG=""
        ;;
    *)
        SETTINGS_PERMS='{ "allow": ["Task", "Bash"], "defaultMode": "default" }'
        BYPASS_FLAG="--dangerously-skip-permissions"
        ;;
esac

cat > "$SANDBOX/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": $SETTINGS_PERMS,
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command",
        "command": "NX_SPIKE_CAPTURE_DIR=$CAPTURE_DIR $STUB_PYTHON $LOGGER UserPromptSubmit" }] }
    ],
    "SubagentStop": [
      { "hooks": [{ "type": "command",
        "command": "NX_SPIKE_CAPTURE_DIR=$CAPTURE_DIR $STUB_PYTHON $LOGGER SubagentStop" }] }
    ],
    "PreCompact": [
      { "hooks": [{ "type": "command",
        "command": "NX_SPIKE_CAPTURE_DIR=$CAPTURE_DIR $STUB_PYTHON $LOGGER PreCompact" }] }
    ],
    "Notification": [
      { "hooks": [{ "type": "command",
        "command": "NX_SPIKE_CAPTURE_DIR=$CAPTURE_DIR $STUB_PYTHON $LOGGER Notification" }] }
    ]
  }
}
EOF

# Clear THIS hook type's capture file so we observe a fresh payload.
: > "$CAPTURE_DIR/$HOOK_TYPE.jsonl"

# ─── Launch tmux + claude ────────────────────────────────────────────────────
export TEST_HOME="$SANDBOX" TMUX_SESSION
# shellcheck source=/dev/null
. "$REPO_ROOT/tests/e2e/lib.sh"

tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
echo "[run $HOOK_TYPE] starting tmux session '$TMUX_SESSION' ..."
tmux new-session -d -s "$TMUX_SESSION" -x 220 -y 50 \
    "env HOME='$SANDBOX' PATH='$SANDBOX/.local/bin:$PATH' bash -i"
sleep 1
send_keys "cd $SANDBOX" Enter
sleep 0.3
send_keys "claude $BYPASS_FLAG" Enter

# Auth state machine (copy of claude_start body, since the launch line varies).
sleep 8
deadline=$(( $(date +%s) + 60 ))
_trust_done=0 _bypass_done=0 _login_done=0
while [[ $(date +%s) -lt $deadline ]]; do
    pane=$(capture)
    if [[ $_trust_done -eq 0 ]] && echo "$pane" | grep -qiE "trust this folder|project you trust"; then
        echo "    [auth] Workspace trust — accepting..."
        tmux send-keys -t "$TMUX_SESSION" Enter; _trust_done=1; sleep 2
    elif [[ $_bypass_done -eq 0 ]] && echo "$pane" | grep -qiE "Bypass Permissions|Yes, I accept"; then
        echo "    [auth] Bypass permissions — accepting..."
        tmux send-keys -t "$TMUX_SESSION" Down; sleep 0.5
        tmux send-keys -t "$TMUX_SESSION" Enter; _bypass_done=1; sleep 5
    elif echo "$pane" | grep -qiE "custom API key|Do you want to use this API key"; then
        tmux send-keys -t "$TMUX_SESSION" Enter; sleep 5
    elif [[ $_login_done -eq 0 ]] && echo "$pane" | grep -qiE "Select login|login method|How would you like"; then
        tmux send-keys -t "$TMUX_SESSION" Down; sleep 0.5
        tmux send-keys -t "$TMUX_SESSION" Enter; _login_done=1; sleep 6
    elif echo "$pane" | grep -qiE "bypass permissions on|Type a message|esc to interrupt"; then
        break
    fi
    sleep 1
done
sleep 5

# ─── Trigger per hook type ───────────────────────────────────────────────────
case "$HOOK_TYPE" in
    UserPromptSubmit)
        # Any prompt fires the hook.
        claude_prompt "Echo the literal token CA6-USERPROMPT-XYZ. Reply DONE."
        claude_wait 60
        ;;
    SubagentStop)
        # Dispatch a one-shot subagent; SubagentStop fires when it returns.
        claude_prompt "Use the Task tool to dispatch a general-purpose subagent. description='CA-6 spike trigger'. prompt='Reply with the single token CA6-SUBAGENT-OK then stop.'. Wait for return and reply DONE."
        claude_wait 180
        ;;
    PreCompact)
        # Two prompts to build context, then /compact.
        claude_prompt "Read tests/cc-validation/scenarios/13_ca8_pretooluse_order.sh and paste its first 80 lines verbatim into your reply."
        claude_wait 60
        claude_prompt "Read src/nexus/db/migrations.py and quote the docstring of every function whose name starts with 'migrate_'. One per paragraph."
        claude_wait 180
        # Trigger /compact via slash command.
        send_keys "/compact" Enter
        sleep 5
        claude_wait 60
        ;;
    Notification)
        # CC 2.1.x auto-allows safe tools (Write/Bash echo) even in
        # default mode, defeating the permission-prompt trigger. The
        # other documented trigger is idle-wait: send a short prompt
        # so the agent replies, then sit idle until CC's
        # "waiting-for-input" Notification fires (~60 s default).
        claude_prompt "Reply with the single token READY."
        claude_wait 30
        echo "    [notif] sitting idle 90s for idle-Notification trigger..."
        sleep 90
        ;;
esac

claude_exit
sleep 2
tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true

# ─── Report ──────────────────────────────────────────────────────────────────
CAPTURE_FILE="$CAPTURE_DIR/$HOOK_TYPE.jsonl"
echo ""
echo "═══════════════════════════════════════════════════"
echo "  CA-6 spike: $HOOK_TYPE result"
echo "═══════════════════════════════════════════════════"
if [[ -s "$CAPTURE_FILE" ]]; then
    echo "  capture: $CAPTURE_FILE"
    echo "  entries: $(wc -l < "$CAPTURE_FILE")"
    echo ""
    echo "  first payload:"
    python3 -c "
import json, sys
with open('$CAPTURE_FILE') as f:
    rec = json.loads(f.readline())
print(json.dumps(rec, indent=2))
" 2>&1 | sed 's/^/    /' | head -60
else
    echo "  WARN: no captures in $CAPTURE_FILE"
    echo "  Hook did not fire. Diagnose:"
    echo "    - check settings.json was applied"
    echo "    - re-run with -x to trace bash"
    echo "    - attach to tmux session before claude_exit"
fi
