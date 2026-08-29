#!/usr/bin/env bash
# Claude Code feature-validation harness — interactive tmux sandbox without
# any plugin install. Each scenario writes its own settings.json/agents/skills
# into $TEST_HOME/.claude before claude_start. Reuses lib.sh helpers from
# tests/e2e for tmux/claude primitives.
#
# Usage:
#   ./tests/cc-validation/runner.sh
#   ./tests/cc-validation/runner.sh --scenario 03
#   tmux attach -t cc-val   # watch live in another terminal

set -euo pipefail

unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT 2>/dev/null || true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
AUTH_DIR="$REPO_ROOT/tests/e2e/.claude-auth"
ONLY_SCENARIO=""

# Distinct from e2e harness — keeps state separate so concurrent runs don't collide.
TEST_HOME="${TMPDIR%/}/nexus-cc-val-home"
TMUX_SESSION="cc-val"
# Dedicated tmux socket: the harness runs on its OWN socket, invisible to the
# user's default socket. This is a hard isolation boundary — a kill-session
# (or even kill-server) here can never touch the interactive session the
# developer is working in. lib.sh's _tmux wrapper honours NX_TMUX_SOCKET.
NX_TMUX_SOCKET="cc-val-sock"
STUB_LOG="$TEST_HOME/stub_calls.log"
HOOK_LOG="$TEST_HOME/hook.log"
export TEST_HOME REPO_ROOT TMUX_SESSION STUB_LOG HOOK_LOG NX_TMUX_SOCKET

while [[ $# -gt 0 ]]; do
    case "$1" in
        --scenario) ONLY_SCENARIO="$2"; shift 2 ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

# NOTE: there is deliberately NO early "do we have credentials?" check here.
# The one that used to live at this spot asked only whether a keychain lookup
# SUCCEEDED, which a token-less husk item satisfies (see provision_credentials
# below, 2026-08-28). provision_credentials is the single fail-loud gate, and
# it runs before tmux starts, so nothing expensive happens ahead of it.

source "$REPO_ROOT/tests/e2e/lib.sh"
TMUX_SESSION="cc-val"  # override the e2e default after sourcing

# ─── Deterministic launch: trust pre-seed + explicit MCP config ──────────────
# (Patterns ported from ~/git/recording-rig; see tests/cc-validation/README.md.)
#
# Two robustness upgrades applied at every claude_start:
#
#   1. TRUST PRE-SEED. Instead of polling the pane for the "trust this folder"
#      dialog and pressing Enter (fragile; the source of scenario 16's custom-
#      launcher class of bug), write hasTrustDialogAccepted into
#      $TEST_HOME/.claude.json for the project paths claude may resolve as cwd.
#      The dialog then never fires.
#
#   2. EXPLICIT MCP CONFIG. Project-scoped .mcp.json servers do NOT connect in
#      the interactive sandbox (approval gate #9189 non-functional; enable keys
#      only honored in ~/.claude.json, #24657). Launch with
#      `--mcp-config <file> --strict-mcp-config` to load the servers directly,
#      bypassing the gate. Also normalize the stub launcher from bare `python3`
#      (no `mcp` module) to the repo venv interpreter.
VENV_PY="$REPO_ROOT/.venv/bin/python"
# Fail fast with a clear message if the venv interpreter (which must have `mcp`)
# is missing — otherwise _prepare_mcp_args rewrites the stub command to a
# non-existent path and every MCP scenario fails as "server never spawned",
# which is far harder to diagnose than this up-front error.
if [[ ! -x "$VENV_PY" ]]; then
    echo "Error: venv interpreter not found at $VENV_PY — run 'uv sync' first." >&2
    echo "       (MCP scenarios launch the stub with this python; it needs the 'mcp' package.)" >&2
    exit 1
fi

_preseed_trust() {
    python3 - "$TEST_HOME/.claude.json" "$TEST_HOME" "$REPO_ROOT" <<'PY'
import json, os, pathlib, sys
cfg_p = pathlib.Path(sys.argv[1])
paths = sys.argv[2:]
try:
    data = json.loads(cfg_p.read_text())
except Exception:
    data = {}
if not isinstance(data, dict):
    data = {}
projects = data.setdefault("projects", {})
seen = set()
for p in paths:
    for key in {p, os.path.realpath(p)}:
        if key in seen:
            continue
        seen.add(key)
        entry = projects.setdefault(key, {})
        entry["hasTrustDialogAccepted"] = True
        entry["hasCompletedProjectOnboarding"] = True
cfg_p.write_text(json.dumps(data, indent=2))
PY
}

# Echo the --mcp-config flags for the next launch (empty when no .mcp.json).
# Side effect: normalizes a python3/python launcher in the .mcp.json to $VENV_PY
# so the stub's `import mcp` resolves.
_prepare_mcp_args() {
    local mcp="$TEST_HOME/.mcp.json"
    [[ -f "$mcp" ]] || { printf ''; return 0; }
    python3 - "$mcp" "$VENV_PY" <<'PY'
import json, pathlib, sys
mcp_p, venv_py = pathlib.Path(sys.argv[1]), sys.argv[2]
data = json.loads(mcp_p.read_text())
changed = False
for spec in (data.get("mcpServers") or {}).values():
    if isinstance(spec, dict) and spec.get("command") in ("python3", "python"):
        spec["command"] = venv_py
        changed = True
if changed:
    mcp_p.write_text(json.dumps(data, indent=2))
PY
    printf -- '--mcp-config %s --strict-mcp-config' "$mcp"
}

# Wrap lib.sh's claude_start: pre-seed trust and compute MCP launch flags just
# before launch (the scenario writes settings/.mcp.json in its body, so this
# must run at claude_start time, not at runner setup).
eval "$(declare -f claude_start | sed '1s/claude_start/_lib_claude_start/')"
claude_start() {
    _preseed_trust
    CLAUDE_EXTRA_ARGS="$(_prepare_mcp_args)"
    export CLAUDE_EXTRA_ARGS
    _lib_claude_start "$@"
}

# ─── Live pane capture (CC_VAL_DEBUG_CAPTURE=1) ──────────────────────────────
# The README's standing lesson is that every cc-val mystery is settled by
# MEASURING the pane, and that a capture taken after the TUI exits is useless
# (the alternate screen is gone by then) — the pane has to be sampled DURING
# the run. This poller is that instrument, kept in-tree instead of being
# re-improvised each time it is needed. It is what showed "Login expired ·
# Please run /login" behind four scenarios that were all reporting
# "MCP connection issue" (2026-08-28, nexus-qs1g6).
#
#   CC_VAL_DEBUG_CAPTURE=1 ./tests/cc-validation/runner.sh --scenario 16
#
# Snapshots land OUTSIDE $TEST_HOME so the EXIT trap's rm -rf cannot eat them.
CC_VAL_DEBUG_DIR="${CC_VAL_DEBUG_DIR:-${TMPDIR%/}/cc-val-debug}"
_DEBUG_CAPTURE_PID=""

start_debug_capture() {
    [[ "${CC_VAL_DEBUG_CAPTURE:-0}" == "1" ]] || return 0
    rm -rf "$CC_VAL_DEBUG_DIR"
    mkdir -p "$CC_VAL_DEBUG_DIR/panes"
    (
        i=0
        while :; do
            i=$(( i + 1 ))
            n=$(printf '%04d' "$i")
            {
                echo "### tick=$n epoch=$(date +%s)"
                _tmux capture-pane -t "$TMUX_SESSION" -p -S -200 2>&1
            } > "$CC_VAL_DEBUG_DIR/panes/pane-$n.txt"
            if [[ -f "$STUB_LOG" ]]; then cp "$STUB_LOG" "$CC_VAL_DEBUG_DIR/stub-$n.log" 2>/dev/null || true; fi
            if [[ -f "$HOOK_LOG" ]]; then cp "$HOOK_LOG" "$CC_VAL_DEBUG_DIR/hook-$n.log" 2>/dev/null || true; fi
            sleep 2
        done
    ) &
    _DEBUG_CAPTURE_PID=$!
    echo "  [debug] pane capture every 2s -> $CC_VAL_DEBUG_DIR"
}

cleanup() {
    echo ""
    echo "Cleaning up..."
    if [[ -n "${_DEBUG_CAPTURE_PID:-}" ]]; then
        kill "$_DEBUG_CAPTURE_PID" 2>/dev/null || true
        echo "  [debug] pane capture kept at $CC_VAL_DEBUG_DIR"
    fi
    # Tear down the whole private socket — safe precisely because it is ours
    # alone (NX_TMUX_SOCKET). Falls back to a scoped kill-session if the
    # server is already gone.
    _tmux kill-server 2>/dev/null || _tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
    rm -rf "$TEST_HOME"
}
trap cleanup EXIT

# ─── Set up isolated test home (no plugin install) ────────────────────────────

echo "Setting up isolated test home at $TEST_HOME..."
rm -rf "$TEST_HOME"
mkdir -p "$TEST_HOME/.claude/plugins" "$TEST_HOME/.claude/agents" "$TEST_HOME/.claude/skills" "$TEST_HOME/.claude/commands"

# Provision OAuth credentials into the isolated TEST_HOME.
#
# The sandbox session reads $TEST_HOME/.claude/.credentials.json (and
# .env.test unsets ANTHROPIC_API_KEY so this file is the auth source).
# A frozen snapshot file goes stale fast: OAuth access tokens are
# short-lived and the refresh token rotates out from under a frozen copy
# once the live CLI refreshes, so a stale snapshot 401s ("Invalid
# authentication credentials") and every scenario fails before the model
# runs anything. Prefer the live macOS keychain at runtime; refresh the
# on-disk snapshot from it so the Linux/CI fallback path stays usable.
#
# TWO DEFECTS FIXED 2026-08-28 (nexus-qs1g6), both of the same class —
# treating a successful FETCH as a valid CREDENTIAL:
#
#   1. MORE THAN ONE keychain item can carry the service name
#      'Claude Code-credentials'. Measured on this box: an acct="unknown"
#      item created 2026-08-23 whose claudeAiOauth is an empty husk
#      (accessToken "", refreshToken "", expiresAt 0) sitting alongside the
#      live acct="<login user>" item the CLI actually refreshes. `security
#      find-generic-password -s <svc> -w` with no `-a` returns an ARBITRARY
#      match, and it returned the husk. The old code's only validity test
#      was json.load() parseability — which a husk passes — so it printed
#      "provisioned from macOS keychain (live)" and every scenario then ran
#      against a logged-out session. The pane showed the real answer the
#      whole time ("Login expired · Please run /login"), while the verdicts
#      blamed the MCP connection. Choose the credential by CONTENT now:
#      enumerate the accounts under the service and take the freshest item
#      that actually carries a token.
#   2. The snapshot refresh (`cp "$dest" "$AUTH_DIR/..."`) ran on that path
#      too, so the husk OVERWROTE the fallback snapshot — the harness
#      destroyed its own only alternative credential source. The snapshot is
#      only ever refreshed from a credential that passed the check now.
#
# _cred_tool pick  → freshest usable keychain credential on stdout, rc 1 if none
# _cred_tool check <file> → rc 0 iff that file holds a usable credential
_cred_tool() {
    python3 - "$@" <<'PY'
import json
import re
import subprocess
import sys
import time

SERVICE = "Claude Code-credentials"


def verdict(data):
    """(ok, reason). Usable == carries a token we can authenticate or refresh with."""
    oauth = (data or {}).get("claudeAiOauth") or {}
    access = oauth.get("accessToken") or ""
    refresh = oauth.get("refreshToken") or ""
    if not access and not refresh:
        return False, "empty husk — accessToken and refreshToken are both blank"
    expires = oauth.get("expiresAt") or 0
    if expires and expires <= int(time.time() * 1000) and not refresh:
        return False, "accessToken expired and no refreshToken to renew it"
    return True, ""


def expiry(data):
    return ((data or {}).get("claudeAiOauth") or {}).get("expiresAt") or 0


mode = sys.argv[1]

if mode == "check":
    try:
        with open(sys.argv[2]) as fh:
            payload = json.load(fh)
    except Exception as exc:
        print(f"unreadable: {exc}", file=sys.stderr)
        sys.exit(1)
    ok, why = verdict(payload)
    if not ok:
        print(why, file=sys.stderr)
        sys.exit(1)
    sys.exit(0)

# mode == "pick"


def fetch(acct):
    cmd = ["security", "find-generic-password", "-s", SERVICE]
    if acct is not None:
        cmd += ["-a", acct]
    proc = subprocess.run(cmd + ["-w"], capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except Exception:
        return None


# `security` has no list-by-service, so enumerate accounts from an
# ATTRIBUTE-ONLY dump (no `-d`, so no secrets are read and no unlock prompt).
accounts, block = [], []
dump = subprocess.run(["security", "dump-keychain"], capture_output=True, text=True).stdout
for line in dump.splitlines() + ["keychain: <eof>"]:
    if line.startswith("keychain: "):
        text = "\n".join(block)
        svce = re.search(r'"svce"<blob>="([^"]*)"', text)
        acct = re.search(r'"acct"<blob>="([^"]*)"', text)
        if svce and acct and svce.group(1) == SERVICE:
            accounts.append(acct.group(1))
        block = [line]
    else:
        block.append(line)

usable, seen = [], set()
for acct in accounts + [None]:  # None == the old first-match form, tried last
    if acct in seen:
        continue
    seen.add(acct)
    payload = fetch(acct)
    if payload is None:
        continue
    label = acct if acct is not None else "<first-match>"
    ok, why = verdict(payload)
    if ok:
        usable.append((expiry(payload), label, payload))
    else:
        print(f"  [auth] skipping keychain item acct={label!r}: {why}", file=sys.stderr)

if not usable:
    sys.exit(1)
usable.sort(key=lambda row: row[0], reverse=True)
expires, label, payload = usable[0]
print(f"  [auth] keychain item acct={label!r} selected (expiresAt={expires})", file=sys.stderr)
sys.stdout.write(json.dumps(payload))
PY
}

provision_credentials() {
    local dest="$TEST_HOME/.claude/.credentials.json"
    local kc_json=""
    if command -v security >/dev/null 2>&1; then
        kc_json="$(_cred_tool pick || true)"
    fi
    if [[ -n "$kc_json" ]]; then
        printf '%s' "$kc_json" > "$dest"
        # Only now — with a credential that passed the check — is it safe to
        # refresh the Linux/CI fallback snapshot.
        cp "$dest" "$AUTH_DIR/.credentials.json" 2>/dev/null || true
        echo "  [auth] provisioned from macOS keychain (token present, refreshable)"
    elif [[ -f "$AUTH_DIR/.credentials.json" ]] && _cred_tool check "$AUTH_DIR/.credentials.json"; then
        cp "$AUTH_DIR/.credentials.json" "$dest"
        echo "  [auth] provisioned from snapshot file (no usable keychain item)"
    else
        # FAIL LOUD. The alternative — proceeding on an unusable credential —
        # burns ~5 minutes per scenario and then reports "MCP connection issue"
        # for what is really a logged-out session.
        echo "Error: no USABLE credentials — every candidate is token-less or expired." >&2
        echo "       Keychain items under 'Claude Code-credentials' were checked (reasons above)" >&2
        echo "       and the snapshot at $AUTH_DIR/.credentials.json is missing or unusable." >&2
        echo "       Remedy: run 'claude /login' in a normal session, then re-run this harness." >&2
        exit 1
    fi
    chmod 600 "$dest"
}
provision_credentials

if [[ -f "$AUTH_DIR/claude.json" ]]; then
    cp "$AUTH_DIR/claude.json" "$TEST_HOME/.claude.json"
else
    echo '{"hasCompletedOnboarding":true}' > "$TEST_HOME/.claude.json"
fi

# Empty plugin registry — no plugins loaded by default.
cat > "$TEST_HOME/.claude/plugins/installed_plugins.json" <<'EOF'
{"version": 2, "plugins": {}}
EOF

# Default settings: bypass dangerous-mode dialog. Each scenario overwrites this.
cat > "$TEST_HOME/.claude/settings.json" <<'EOF'
{
  "skipDangerousModePermissionPrompt": true
}
EOF

# Env file the tmux pane sources before launching claude.
cat > "$TEST_HOME/.env.test" <<EOF
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT
unset ANTHROPIC_API_KEY  # OAuth from .credentials.json takes priority
export HOME="$TEST_HOME"
export PATH="\$HOME/.local/bin:\$PATH"
export STUB_LOG="$STUB_LOG"
export HOOK_LOG="$HOOK_LOG"
cd "$REPO_ROOT"
EOF
chmod 600 "$TEST_HOME/.env.test"

# ─── Scenario helpers ─────────────────────────────────────────────────────────

# Wipe per-scenario state without disturbing the OAuth/credentials/plugin bits.
reset_scenario_state() {
    # NOTE (2026-05-31): scenarios write `.mcp.json` to the WORKSPACE ROOT
    # ($TEST_HOME/.mcp.json), not under .claude/. Cleaning only
    # .claude/.mcp.json left a stale project .mcp.json across scenarios — the
    # claude_start wrapper then fed it via --mcp-config to a LATER scenario's
    # parent, manufacturing a false "inline mcpServers leaked to parent" result
    # in scenario 11. Remove BOTH paths so scenarios are isolated.
    rm -f "$TEST_HOME/.claude/settings.json" \
          "$TEST_HOME/.claude/.mcp.json" \
          "$TEST_HOME/.mcp.json" \
          "$STUB_LOG" "$HOOK_LOG"
    rm -rf "$TEST_HOME/.claude/agents" "$TEST_HOME/.claude/skills" "$TEST_HOME/.claude/commands"
    mkdir -p "$TEST_HOME/.claude/agents" "$TEST_HOME/.claude/skills" "$TEST_HOME/.claude/commands"
    # Restore the dangerous-mode bypass — every scenario needs it.
    echo '{"skipDangerousModePermissionPrompt": true}' > "$TEST_HOME/.claude/settings.json"
}
export -f reset_scenario_state

# write_settings <path-to-fixture-json>: install settings.json for the next claude_start
write_settings() {
    cp "$1" "$TEST_HOME/.claude/settings.json"
}
export -f write_settings

write_mcp_config() {
    cp "$1" "$TEST_HOME/.claude/.mcp.json"
}
export -f write_mcp_config

write_agent() {
    local name="$1" src="$2"
    cp "$src" "$TEST_HOME/.claude/agents/$name.md"
}
export -f write_agent

write_skill() {
    local name="$1" src="$2"
    mkdir -p "$TEST_HOME/.claude/skills/$name"
    cp "$src" "$TEST_HOME/.claude/skills/$name/SKILL.md"
}
export -f write_skill

# write_command <name> <src>: install a slash command (.claude/commands/<name>.md)
# for the next claude_start. Used by scenario 19 (nexus-ln9y5) to validate that a
# command's ```! bash-injection block actually renders.
write_command() {
    local name="$1" src="$2"
    cp "$src" "$TEST_HOME/.claude/commands/$name.md"
}
export -f write_command

# ─── Start tmux ───────────────────────────────────────────────────────────────

echo "Starting tmux session '$TMUX_SESSION' on private socket '$NX_TMUX_SOCKET'..."
echo "  (run 'tmux -L $NX_TMUX_SOCKET attach -t $TMUX_SESSION' to watch live)"

_tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
_tmux new-session -d -s "$TMUX_SESSION" -x 220 -y 50

_tmux send-keys -t "$TMUX_SESSION" "source $TEST_HOME/.env.test" Enter
sleep 1
touch "$TEST_HOME/.zshrc"

start_debug_capture

# ─── Run scenarios ────────────────────────────────────────────────────────────

run_scenario() {
    local file="$1"
    local num
    num=$(basename "$file" | cut -d_ -f1)
    if [[ -n "$ONLY_SCENARIO" && ",$ONLY_SCENARIO," != *",$num,"* ]]; then
        return 0
    fi
    echo ""
    echo "════════════════════════════════════════════════════"
    reset_scenario_state
    # shellcheck source=/dev/null
    source "$file"
}

for scenario_file in "$SCRIPT_DIR"/scenarios/[0-9]*.sh; do
    run_scenario "$scenario_file"
done

summary
