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
ONLY_SCENARIO=""

# RDR-219: the harness's own automation identity (`nexus-automation-oauth-token`
# in the keychain), never the operator's interactive `Claude Code-credentials`
# login. Thin wrapper around the shared helper (nexus-galkv.19,
# tests/e2e/lib/claude_credentials.py) so this file has one call site to
# change if the helper's path ever moves.
#
# NX_CC_VAL_IN_CONTAINER=1 (set only by container/entrypoint_in_container.sh, inside its own
# container, which has no keychain): the automation token already sits in this
# process's environment, put there by `claude_credentials.py run -- docker run
# -e CLAUDE_CODE_OAUTH_TOKEN`, so `run --` degrades to its own fail-loud gate
# (an empty token stops the run before anything is started) and then execs the
# command. Every other subcommand, and every run outside that container, goes
# to the shared helper untouched.
_cred_tool() {
    if [[ "${NX_CC_VAL_IN_CONTAINER:-}" == "1" && "${1:-}" == "run" ]]; then
        if [[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
            echo "[auth] no automation token in this container's environment -- start it with container/run.sh under claude_credentials.py run --" >&2
            return 1
        fi
        shift
        [[ "${1:-}" == "--" ]] && shift
        "$@"
        return
    fi
    python3 "$REPO_ROOT/tests/e2e/lib/claude_credentials.py" "$@"
}

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
# SUCCEEDED, which a token-less husk item satisfies -- see RDR-219
# nexus_rdr/219-research-9. The fail-loud gate now lives at the tmux
# server-start call below (`_cred_tool run -- tmux ... new-session`): `run`
# exits non-zero, naming the remedy, before it execs anything, if the
# automation token is absent or expired, and it runs before any scenario
# starts, so nothing expensive happens ahead of it.

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
    # nexus-spwp2: a scenario that installed a plugin through cc_plugin_install
    # has CC_DEBUG_FILE set; Claude Code's debug log is the only record of a
    # hook that ran SILENTLY (the transcript omits a hook whose stdout and
    # stderr are empty), so those sessions always write one.
    if [[ -n "${CC_DEBUG_FILE:-}" ]]; then
        CLAUDE_EXTRA_ARGS="${CLAUDE_EXTRA_ARGS} --debug-file ${CC_DEBUG_FILE}"
    fi
    export CLAUDE_EXTRA_ARGS
    _lib_claude_start "$@"
}

# claude_start_enforced: claude_start WITHOUT --dangerously-skip-permissions
# (nexus-qsmc5). Under the bypass flag every tool call is allowed whatever
# settings.json says, so a scenario whose point is the permission system (a
# defaultMode, an allow rule, a PermissionRequest hook) measures nothing with
# claude_start. Such a scenario launches with this and proves the mode is in
# force with a control: a call no rule grants must stop at a permission prompt
# (cc_wait_permission_prompt). The pane is cleared first so an earlier session's
# prompt in the scrollback cannot be read as this one's.
claude_start_enforced() {
    local CLAUDE_PERMISSION_FLAG=""
    send_keys "clear" Enter
    sleep 0.5
    _tmux clear-history -t "$TMUX_SESSION" 2>/dev/null || true
    claude_start "$@"
}

# cc_permission_prompt_up: 0 when the pane shows a pending permission prompt.
cc_permission_prompt_up() {
    local pane
    pane="$(capture -60)"
    grep -qE "Do you want to proceed\?" <<<"$pane"
}

# cc_wait_permission_prompt [timeout-seconds]: poll the pane for the prompt.
cc_wait_permission_prompt() {
    poll_for "Do you want to proceed\?" "${1:-90}" "a permission prompt"
}

# cc_dismiss_permission_prompt: answer a pending prompt with Escape (deny), so the
# session can exit cleanly and the call never runs.
cc_dismiss_permission_prompt() {
    if cc_permission_prompt_up; then
        _tmux send-keys -t "$TMUX_SESSION" Escape
        sleep 1
    fi
    return 0
}

# cc_claude_version: the Claude Code build this session runs, first word of
# `claude --version` ("2.1.302"), else of the claude-version.txt the container
# entrypoint wrote into the evidence directory, else "unknown". Every verdict of a
# scenario whose answer has changed between Claude Code releases (06, 07) carries it.
cc_claude_version() {
    local v=""
    v="$(claude --version 2>/dev/null | awk 'NR==1{print $1}')" || true
    if [[ -z "$v" && -n "${CC_VAL_EVIDENCE_DIR:-}" && -f "$CC_VAL_EVIDENCE_DIR/claude-version.txt" ]]; then
        v="$(awk 'NR==1{print $1}' "$CC_VAL_EVIDENCE_DIR/claude-version.txt")"
    fi
    printf '%s' "${v:-unknown}"
}

# cc_note <label> <line>: print a verdict-related line and, when the container mounted
# an evidence directory, append it to <evidence>/<label>/verdict.txt, so the run's
# verdict survives the container (nexus-qsmc5: 05-07 left no record of their own).
cc_note() {
    echo "    $2"
    if [[ -n "${CC_VAL_EVIDENCE_DIR:-}" ]]; then
        mkdir -p "$CC_VAL_EVIDENCE_DIR/$1" 2>/dev/null && printf '%s\n' "$2" >> "$CC_VAL_EVIDENCE_DIR/$1/verdict.txt"
    fi
    return 0
}

# _cc_pass / _cc_fail <label> <message>: pass()/fail() that also record the line.
_cc_pass() { cc_note "$1" "PASS: $2"; pass "$2"; }
_cc_fail() { cc_note "$1" "FAIL: $2"; fail "$2"; }

# cc_version_check <scenario> <claude-version> <answer> <observation>
# The version-answer table (docs/tables/cc-version-answers.toml): scenarios 06 and 07
# ask questions whose answers have flipped across Claude Code releases, so a run
# compares what it OBSERVED with what was recorded for the same build.
#   recorded version, same answer   pass, saying so
#   recorded version, other answer  FAIL "answer changed since <version>"
#   unrecorded version              INFO "record this version's answer", then pass
#   unreadable version ("unknown")  FAIL: no version, so neither a recorded nor a new answer
#                                   can be claimed (nexus-ijate)
cc_version_check() {
    local scenario="$1" ver="$2" answer="$3" what="$4" out verdict since observed
    if [[ -z "$ver" || "$ver" == "unknown" ]]; then
        _cc_fail "$scenario" "${scenario}: the Claude Code version could not be read (claude --version printed nothing and no claude-version.txt): ${what}; with no version the answer cannot be compared with docs/tables/cc-version-answers.toml"
        return 0
    fi
    local table="${CC_VAL_VERSION_TABLE:-$REPO_ROOT/docs/tables/cc-version-answers.toml}"
    if ! out="$(python3 -I "$REPO_ROOT/tests/cc-validation/lib/version_answers.py" lookup "$table" "$scenario" "$ver" "$answer" 2>&1)"; then
        _cc_fail "$scenario" "${scenario}: observed on Claude Code ${ver}: ${what}; the version-answer table could not be read: ${out}"
        return 0
    fi
    verdict="$(cut -f1 <<<"$out")"; since="$(cut -f2 <<<"$out")"; observed="$(cut -f3 <<<"$out")"
    case "$verdict" in
        agrees)
            _cc_pass "$scenario" "${scenario}: observed on Claude Code ${ver}: ${what} (the answer recorded for ${since} on ${observed})" ;;
        changed)
            _cc_fail "$scenario" "${scenario}: observed on Claude Code ${ver}: ${what}; answer changed since ${since} (recorded ${observed}): read why before touching docs/tables/cc-version-answers.toml, and rely on neither answer" ;;
        *)
            cc_note "$scenario" "INFO: no answer is recorded for Claude Code ${ver}: record this version's answer (${answer}) in docs/tables/cc-version-answers.toml"
            _cc_pass "$scenario" "${scenario}: observed on Claude Code ${ver}: ${what} (no recorded answer for this version)" ;;
    esac
    return 0
}

# cc_allow_rule_verdict <name> <rule> <control_prompt> <control_ran> <allowed_ran> <allowed_prompt> [report]
# The verdict ladder of scenarios 05 and 06 (an allow rule granting an MCP tool
# call), 1/0 flags. The allowed leg means nothing unless the CONTROL leg (a rule
# that does not match the tool) stopped at a permission prompt without running
# the tool: that is the proof permissions are enforced in these sessions. With
# `report` (scenario 06, whose question is whether a rule SHAPE matches) either
# honest answer is an observation, not a pass or a fail: it is compared with the
# answer recorded for this Claude Code version (cc_version_check). Every line
# says which build it observed.
cc_allow_rule_verdict() {
    local name="$1" rule="$2" cprompt="$3" cran="$4" aran="$5" aprompt="$6" mode="${7:-}"
    local ver; ver="$(cc_claude_version)"
    local obs="observed on Claude Code ${ver}"
    cc_note "$name" "${name}: ${obs}: control prompt=$cprompt tool_ran=$cran; allowed prompt=$aprompt tool_ran=$aran"
    if [[ "$cprompt" -ne 1 || "$cran" -ne 0 ]]; then
        _cc_fail "$name" "${name}: ${obs}: the CONTROL session (a rule that does not match the tool) did not stop at a permission prompt (prompt=$cprompt tool_ran=$cran): permissions are not enforced in these sessions (launched with --dangerously-skip-permissions?), so the allowed leg proves nothing about $rule"
    elif [[ "$aprompt" -eq 1 && "$aran" -eq 0 && "$mode" == "report" ]]; then
        cc_version_check "$name" "$ver" "does-not-match" "the call with the rule $rule stopped at a permission prompt and the tool did not run, as the control did, in the same mode"
    elif [[ "$aprompt" -eq 1 ]]; then
        _cc_fail "$name" "${name}: ${obs}: the allowed leg still stopped at a permission prompt: $rule did NOT grant the call"
    elif [[ "$aran" -ne 1 ]]; then
        _cc_fail "$name" "${name}: ${obs}: the allowed leg neither stopped at a prompt nor ran the tool: nothing to conclude about $rule"
    elif [[ "$mode" == "report" ]]; then
        cc_version_check "$name" "$ver" "matches" "the call with the rule $rule ran the tool with no prompt, while a rule that does not match stopped at a permission prompt in the same mode"
    else
        _cc_pass "$name" "${name}: ${obs}: $rule granted the call with no prompt (the tool ran) while a rule that does not match stopped at a permission prompt in the same mode: the rule did it, not a bypass"
    fi
}

# cc_preempt_verdict <hook_A> <ran_A> <prompt_A> <hook_B> <ran_B> <prompt_B>
# The verdict ladder of scenario 07: with an allow rule present, does the
# PermissionRequest hook still fire? A = allow rule present, B = absent (1/0 flags).
# B is the control: with no rule granting the call it must reach a permission gate,
# which is the hook firing or the prompt appearing; neither means permissions are not
# enforced. The answer to the question is an observation for THIS Claude Code build
# (it has changed between releases): it is compared with the answer recorded for the
# same version (cc_version_check), never asserted.
cc_preempt_verdict() {
    local ha="$1" ra="$2" pa="$3" hb="$4" rb="$5" pb="$6"
    local ver; ver="$(cc_claude_version)"
    local obs="observed on Claude Code ${ver}"
    cc_note "07" "07: ${obs}"
    cc_note "07" "sub-A (allow present): hook_fired=$ha tool_ran=$ra prompt_pending=$pa"
    cc_note "07" "sub-B (allow absent):  hook_fired=$hb tool_ran=$rb prompt_pending=$pb"
    if [[ "$hb" -eq 0 && "$pb" -eq 0 ]]; then
        _cc_fail "07" "07: ${obs}: with NO allow rule the call neither reached the PermissionRequest hook nor stopped at a permission prompt: permissions are not enforced in these sessions (launched with --dangerously-skip-permissions?), so sub-A proves nothing"
    elif [[ "$pa" -eq 1 ]]; then
        _cc_fail "07" "07: ${obs}: with the allow rule present the call still stopped at a permission prompt: the rule did not grant it"
    elif [[ "$ra" -ne 1 ]]; then
        _cc_fail "07" "07: ${obs}: sub-A: the allow rule was present but the tool did not run (server not connected?), so the PermissionRequest gate cannot be assessed"
    elif [[ "$hb" -eq 0 ]]; then
        _cc_fail "07" "07: ${obs}: sub-B stopped at a permission prompt but the PermissionRequest hook never fired: the hook does not govern this prompt (matcher or event), investigate"
    elif [[ "$rb" -ne 1 ]]; then
        _cc_fail "07" "07: ${obs}: sub-B: the PermissionRequest hook fired and allowed the call but the tool did not run: the hook's allow was not honoured"
    elif [[ "$ha" -eq 0 ]]; then
        cc_version_check "07" "$ver" "preempts" "with the allow rule present the PermissionRequest hook did not fire and the tool ran; with no allow rule the hook fired and its allow ran the tool"
    else
        cc_version_check "07" "$ver" "fires-regardless" "the PermissionRequest hook fired both with the allow rule present and with none, and the tool ran"
    fi
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

# RDR-219: no credential is provisioned into $TEST_HOME at all. The
# harness's `claude` sessions authenticate from CLAUDE_CODE_OAUTH_TOKEN,
# which reaches them via the private tmux SERVER's own environment (see the
# tmux-start section below) -- there is nothing to write, refresh or fall
# back to here. The Phase 0 spike (T2 nexus_rdr/219-research-14) verified
# that a `.claude.json` holding only `hasCompletedOnboarding` authenticates
# in this launch shape; the `oauthAccount` seed this harness used to copy
# from tests/e2e/.claude-auth/claude.json is not needed.
echo '{"hasCompletedOnboarding":true}' > "$TEST_HOME/.claude.json"

# Empty plugin registry — no plugins loaded by default (_cc_plugins_baseline,
# defined with the plugin helpers below, writes it).

# Default settings: bypass dangerous-mode dialog. Each scenario overwrites this.
cat > "$TEST_HOME/.claude/settings.json" <<'EOF'
{
  "skipDangerousModePermissionPrompt": true
}
EOF

# Env file the tmux pane sources before launching claude.
cat > "$TEST_HOME/.env.test" <<EOF
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT
# RDR-219: an API key outranks CLAUDE_CODE_OAUTH_TOKEN in Claude Code's auth
# precedence, so a stray key in the pane's own environment would make the
# session bill it instead of the automation token this harness provisions
# via the tmux server's environment (see the tmux-start section) -- unset
# it here so nothing but the automation token can be picked up.
unset ANTHROPIC_API_KEY
export HOME="$TEST_HOME"
export PATH="\$HOME/.local/bin:\$PATH"
export STUB_LOG="$STUB_LOG"
export HOOK_LOG="$HOOK_LOG"
# nexus-cf3p2: scenario 12 installs the REAL conexus plugin into this fresh
# HOME and dispatches a real subagent, which boots nx-mcp resolved off PATH
# -- a uv-tool-installed wheel, never this dev checkout, so install_ping's
# dev-checkout auto-suppression does not cover it. Every scenario inherits
# this, not just the ones that install the plugin: the flag is free and the
# judgement of which scenario needs it is not.
export NX_NO_TELEMETRY=1
cd "$REPO_ROOT"
EOF
chmod 600 "$TEST_HOME/.env.test"

# ─── Scenario helpers ─────────────────────────────────────────────────────────

# cc_reset_transcripts: forget every session and subagent transcript so far. A scenario
# part that reads transcripts starts from this, so it reads only its own session's.
cc_reset_transcripts() {
    rm -rf "$TEST_HOME/.claude/projects"
}
export -f cc_reset_transcripts

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
    # nexus-qsmc5: every reader of a session or subagent transcript (cc_subagent_replies,
    # cc_session_replies, cc_session_user_text, cc_nested_dispatch_facts) walks the whole
    # projects dir, so an earlier scenario's transcripts would be read as this one's
    # (a 17 verdict satisfied by 01's Agent call, a 21 verdict by 19's expansion).
    cc_reset_transcripts
    # Restore the dangerous-mode bypass — every scenario needs it.
    echo '{"skipDangerousModePermissionPrompt": true}' > "$TEST_HOME/.claude/settings.json"
    # A plugin installed by an earlier scenario must not leak into this one.
    _cc_plugins_baseline
}
export -f reset_scenario_state

# ─── Plugin install (nexus-spwp2) ─────────────────────────────────────────────
#
# THE way a scenario gets a plugin loaded. Claude Code 2.1.302 does NOT load a
# plugin from a hand-written installed_plugins.json entry: `claude plugin list`
# says "failed to load: Marketplace <name> not found", and no hook, agent or
# server of it exists in the session. A scenario that wrote one passed on
# whatever the plugin-less session did (found by nexus-ueztp; census and fix in
# nexus-spwp2). So a plugin is installed through a local-directory marketplace
# with `claude plugin marketplace add` + `claude plugin install`, and the
# scenario does not start a session unless `claude plugin list` reports it
# enabled at the version its plugin.json declares.
#
# The install replaces ALL plugin state: one plugin per scenario part. It also
# merges enabledPlugins/extraKnownMarketplaces into
# $TEST_HOME/.claude/settings.json, so a scenario writes its settings.json FIRST
# and installs after.

CC_PLUGIN_ID=""
CC_PLUGIN_VERSION=""
CC_PLUGIN_INSTALLED_ROOT=""
CC_DEBUG_FILE=""

# The empty registry: the state before any install and after every teardown.
# The one place an installed_plugins.json is written, and it names no plugin.
_cc_plugins_baseline() {
    rm -rf "$TEST_HOME/.claude/plugins"
    mkdir -p "$TEST_HOME/.claude/plugins"
    printf '%s\n' '{"version": 2, "plugins": {}}' > "$TEST_HOME/.claude/plugins/installed_plugins.json"
    CC_PLUGIN_ID="" CC_PLUGIN_VERSION="" CC_PLUGIN_INSTALLED_ROOT="" CC_DEBUG_FILE=""
}

# cc_plugin_install <plugin_root> <plugin_name> <marketplace_name> [prepare_fn]
#
# Copies <plugin_root> into a synthesized marketplace named <marketplace_name>,
# installs it at user scope, and succeeds only when `claude plugin list` shows
# <plugin_name>@<marketplace_name> enabled at the version <plugin_root>'s
# plugin.json declares. <prepare_fn>, when given, runs on the COPY before the
# install (scenario 32 appends its marker hook there). Sets CC_PLUGIN_ID,
# CC_PLUGIN_VERSION, CC_PLUGIN_INSTALLED_ROOT and CC_DEBUG_FILE (the file
# claude_start hands to --debug-file); the evidence files plugin-install.log and
# plugin-list.txt land in $TEST_HOME. Never skips: a caller that gets non-zero
# back must fail its scenario (cc_plugin_require does).
cc_plugin_install() {
    local root="$1" plugin="$2" mkt_name="$3" prepare="${4:-}"
    local mkt="$TEST_HOME/marketplace-$plugin" list="$TEST_HOME/plugin-list.txt"
    CC_PLUGIN_ID="" CC_PLUGIN_VERSION="" CC_PLUGIN_INSTALLED_ROOT="" CC_DEBUG_FILE=""
    CC_PLUGIN_VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$root/.claude-plugin/plugin.json" 2>/dev/null)" || return 1
    [[ -n "$CC_PLUGIN_VERSION" ]] || return 1
    rm -rf "$mkt" "$TEST_HOME/.claude/plugins"
    mkdir -p "$mkt/.claude-plugin" "$TEST_HOME/.claude/plugins"
    cp -R "$root" "$mkt/$plugin" || return 1
    if [[ -n "$prepare" ]]; then "$prepare" "$mkt/$plugin" || return 1; fi
    cat > "$mkt/.claude-plugin/marketplace.json" <<EOF
{"name":"$mkt_name","owner":{"name":"cc-validation"},"plugins":[{"name":"$plugin","source":"./$plugin","version":"$CC_PLUGIN_VERSION"}]}
EOF
    : > "$TEST_HOME/plugin-install.log"
    HOME="$TEST_HOME" claude plugin marketplace add "$mkt" >> "$TEST_HOME/plugin-install.log" 2>&1 || return 1
    HOME="$TEST_HOME" claude plugin install "$plugin@$mkt_name" --scope user >> "$TEST_HOME/plugin-install.log" 2>&1 || return 1
    HOME="$TEST_HOME" claude plugin list > "$list" 2>&1 || return 1
    grep -qF "$plugin@$mkt_name" "$list" || return 1
    # The value after "Version:" must BE the declared version: a substring match
    # read 7.82.01 and 17.82.0 as 7.82.0.
    local listed_versions
    listed_versions="$(sed -n 's/^[[:space:]]*Version:[[:space:]]*//p' "$list")"
    grep -qxF "$CC_PLUGIN_VERSION" <<<"$listed_versions" || return 1
    grep -qF "Status: ✔ enabled" "$list" || return 1
    ! grep -qiE "failed to load" "$list" || return 1
    CC_PLUGIN_INSTALLED_ROOT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plugins"][sys.argv[2]][0]["installPath"])' "$TEST_HOME/.claude/plugins/installed_plugins.json" "$plugin@$mkt_name" 2>/dev/null)" || return 1
    [[ -f "$CC_PLUGIN_INSTALLED_ROOT/.claude-plugin/plugin.json" ]] || return 1
    CC_PLUGIN_ID="$plugin@$mkt_name"
    CC_DEBUG_FILE="$TEST_HOME/cc-debug-$plugin.log"
    rm -f "$CC_DEBUG_FILE"
    return 0
}

# cc_plugin_require <same arguments>: the precondition every plugin-dependent
# scenario part runs before it starts a session. Fails the scenario (never skips)
# and returns non-zero when the plugin is not loaded at the expected version, so
# a part that would have measured a plugin-less session says so instead. Use it
# as `if cc_plugin_require ...; then <the part>; fi`.
cc_plugin_require() {
    if cc_plugin_install "$@"; then
        return 0
    fi
    fail "${_SCENARIO:-scenario}: plugin ${2:-?}@${3:-?} is NOT loaded at version ${CC_PLUGIN_VERSION:-?} -- nothing of it can run in this session, so the measurement would be of a plugin-less session"
    echo "    'claude plugin list' said:" >&2
    sed 's/^/    | /' "$TEST_HOME/plugin-list.txt" "$TEST_HOME/plugin-install.log" >&2 2>/dev/null || true
    return 1
}

# Copy one file into an evidence directory unless it holds a credential. Claude
# Code's debug log and transcripts can echo the environment; a file containing
# `sk-ant-` is refused (named on stderr, never its content), the same guard as
# scenario 32's _s32_keep_evidence. The evidence directory is world-readable.
_cc_evidence_copy() {
    local src="$1" dest="$2"
    [[ -n "$src" && -f "$src" ]] || return 0
    if grep -q 'sk-ant-' "$src" 2>/dev/null; then
        echo "    [evidence] refused to keep ${src##*/}: it contains a credential-shaped string" >&2
        return 0
    fi
    cp "$src" "$dest/" 2>/dev/null || true
}

# Copy a plugin session's evidence (plugin list/install output, debug log,
# settings, registry, transcripts) to $CC_VAL_EVIDENCE_DIR/<label> when the
# container run.sh mounted one. $TEST_HOME is deleted at exit.
cc_keep_evidence() {
    [[ -n "${CC_VAL_EVIDENCE_DIR:-}" ]] || return 0
    local d="$CC_VAL_EVIDENCE_DIR/${1:-plugin}" f
    mkdir -p "$d"
    for f in "$TEST_HOME/plugin-list.txt" "$TEST_HOME/plugin-install.log" "$TEST_HOME/.claude/settings.json" \
             "$TEST_HOME/.claude/plugins/installed_plugins.json" "${CC_DEBUG_FILE:-}" \
             "${STUB_LOG:-}" "${HOOK_LOG:-}" "$TEST_HOME/pane-tail.txt"; do
        _cc_evidence_copy "$f" "$d"
    done
    while IFS= read -r f; do
        _cc_evidence_copy "$f" "$d"
    done < <(find "$TEST_HOME/.claude/projects" -name '*.jsonl' 2>/dev/null)
    return 0
}

# cc_save_pane [lines]: write the pane's last lines to $TEST_HOME/pane-tail.txt, which
# cc_keep_evidence keeps. The pane is NOT a verdict source (a subagent's reply, a
# hook's output are read from transcripts and the debug log); it is the record of
# what a permission prompt looked like.
cc_save_pane() {
    capture "-${1:-60}" > "$TEST_HOME/pane-tail.txt" 2>/dev/null || true
    return 0
}

# Teardown: keep the evidence, then back to the empty registry.
cc_plugin_uninstall_all() {
    cc_keep_evidence "$(printf '%s' "${_SCENARIO:-scenario}" | awk '{print $1}')"
    _cc_plugins_baseline
}

# cc_hook_ran <event-ere> <plugin_id> [script-ere]: 0 when Claude Code's debug
# log ($CC_DEBUG_FILE) shows a hook of <event-ere> from plugin <plugin_id>
# finished with status 0 (and, when given, whose command names <script-ere>).
# The debug log records every hook, silent ones too; the session transcript
# records a hook only when its stdout or stderr is non-empty, so a hook's
# absence there is not evidence it did not run (nexus-ueztp).
cc_hook_ran() {
    local event="$1" id="$2" script="${3:-}"
    [[ -n "${CC_DEBUG_FILE:-}" && -f "$CC_DEBUG_FILE" ]] || return 1
    grep -Eq "Hooks: ${event}[^[]*\[[^]]*${script}[^]]*\] \(plugin ${id}\) finished with status 0" "$CC_DEBUG_FILE"
}

# cc_mcp_hook_ran <plugin_id> <tool>: the same for a `type: mcp_tool` hook, which
# the debug log records differently (`Hooks: mcp_tool calling
# plugin:<plugin>:<server>/<tool>`, and `Hooks: mcp_tool hook error: ...` on
# failure). 0 only when THAT plugin's call (plugin:<name of plugin_id>:) was made
# and the log holds no mcp_tool hook error: a plugin whose CLI is absent loads
# fine and then fails every such hook with "Nexus is not installed". The tool
# name alone would accept another plugin's server exposing the same tool.
cc_mcp_hook_ran() {
    local id="$1" tool="$2" plugin
    plugin="${id%@*}"
    [[ -n "$plugin" && -n "$tool" ]] || return 1
    [[ -n "${CC_DEBUG_FILE:-}" && -f "$CC_DEBUG_FILE" ]] || return 1
    grep -Eq "Hooks: mcp_tool calling plugin:${plugin}:[^ /]+/${tool}( |$)" "$CC_DEBUG_FILE" || return 1
    ! grep -q "Hooks: mcp_tool hook error" "$CC_DEBUG_FILE"
}

# cc_report_context_lines: print (informational, never a verdict) the additionalContext
# Claude Code accepted from hooks this session, from the debug log, so a probe that
# read NONE can be told apart from a hook that never delivered anything. The line
# names the hook's command, not its plugin; the sizes are the point.
cc_report_context_lines() {
    local lines
    if [[ -z "${CC_DEBUG_FILE:-}" || ! -f "$CC_DEBUG_FILE" ]]; then
        echo "    [debug] no debug log for this session"
        return 0
    fi
    lines="$(grep -E 'Hook [A-Za-z]+ \(.*\) provided additionalContext' "$CC_DEBUG_FILE" | cut -c1-200 || true)"
    if [[ -z "$lines" ]]; then
        echo "    [debug] Claude Code accepted NO additionalContext from any hook this session"
    else
        echo "    [debug] additionalContext accepted from hooks this session:"
        sed 's/^/    [debug]   /' <<<"$lines"
    fi
    return 0
}

# cc_cmd_hook_finished <event> <needle>: 0 when the debug log ($CC_DEBUG_FILE) shows a
# settings.json command hook of <event> whose command contains <needle> (letters, digits,
# '-', '_') finished with status 0: the hook ran. A hook that is mis-registered leaves no
# such line, and its subagent's NONE would read the same as a hook that ran and was ignored.
cc_cmd_hook_finished() {
    [[ "${2:-}" =~ ^[A-Za-z0-9_-]+$ ]] || return 1
    [[ -n "${CC_DEBUG_FILE:-}" && -f "$CC_DEBUG_FILE" ]] || return 1
    python3 -I "$REPO_ROOT/tests/cc-validation/lib/debug_log.py" finished "$CC_DEBUG_FILE" "$1" "$2"
}

# cc_event_context_chars <event>: the sizes, one per line, of the non-empty additionalContext
# Claude Code ACCEPTED from any <event> hook this session (the debug log's `Hook <Event>
# (<command>) provided additionalContext (N chars)`); returns 1 when there is none. These
# scenarios register one hook, so "any" is that hook.
cc_event_context_chars() {
    [[ -n "${CC_DEBUG_FILE:-}" && -f "$CC_DEBUG_FILE" ]] || return 1
    python3 -I "$REPO_ROOT/tests/cc-validation/lib/debug_log.py" context "$CC_DEBUG_FILE" "$1"
}

# cc_injection_reading <event> <marker> <tag> <replies>: one line, the reading of a
# plain-stdout injection probe (scenarios 01 and 15), from BOTH the subagent's answer and
# the debug log, which must agree:
#   VISIBLE            the subagent reported the whole marker (its code is in no prompt) AND
#                      the hook finished AND Claude Code accepted additionalContext from it
#   NOT-VISIBLE        the subagent answered NONE AND the hook finished AND Claude Code
#                      accepted no additionalContext
#   MISMATCH <why>     the two disagree, or the log shows the hook never ran: a NONE with no
#                      finished line is what a dead hook looks like (nexus-qsmc5)
#   INDETERMINATE <why> the subagent answered neither
cc_injection_reading() {
    local event="$1" marker="$2" tag="$3" replies="$4" found_line fin=0 sizes=""
    found_line="$(grep -F "FOUND-${tag}" <<<"$replies" || true)"
    if cc_cmd_hook_finished "$event" "$marker"; then fin=1; fi
    sizes="$(cc_event_context_chars "$event" || true)"
    sizes="${sizes//$'\n'/,}"
    if [[ -n "$found_line" && "$found_line" == *"$marker"* ]]; then
        if [[ $fin -eq 1 && -n "$sizes" ]]; then
            echo "VISIBLE"
        else
            echo "MISMATCH the subagent reported the whole marker but the debug log shows hook_finished=$fin and accepted additionalContext=${sizes:-none}"
        fi
    elif grep -qF "NONE-${tag}" <<<"$replies"; then
        if [[ -n "$sizes" ]]; then
            echo "MISMATCH the subagent answered NONE but the debug log shows Claude Code accepted additionalContext ($sizes chars) from a $event hook"
        elif [[ $fin -ne 1 ]]; then
            echo "MISMATCH the subagent answered NONE but the debug log has no finished-with-status-0 line for the hook: it may never have run, and then NONE proves nothing"
        else
            echo "NOT-VISIBLE"
        fi
    else
        echo "INDETERMINATE the subagent answered neither FOUND-${tag} with the whole marker nor NONE-${tag}"
    fi
}

# cc_expanded_command_has <header> <needle>...: look in the main session's user turns (not the
# model's text, not the pane) for a slash command that Claude Code expanded. Returns
#   0  the turn carrying <header> (a line of the command's own body) also carries every <needle>
#      (fixed strings, newlines allowed): the `!` injection's output reached the model
#   1  the body reached the model but a needle is missing: the injection produced something else
#   2  no user turn carries <header>: the command never reached the model
#   3  a user turn carries Claude Code's own shell-error text
cc_expanded_command_has() {
    local header="$1" text needle
    shift
    text="$(cc_session_user_text || true)"
    [[ "$text" == *"$header"* ]] || return 2
    if grep -qiE 'Shell command failed|\(eval\):|No such command' <<<"$text"; then return 3; fi
    for needle in "$@"; do
        [[ "$text" == *"$needle"* ]] || return 1
    done
    return 0
}

# cc_wait_user_text <fixed-string> [timeout-seconds]: poll the main session's user turns
# for <fixed-string> (the slash command's header), then give the turn a moment to finish.
cc_wait_user_text() {
    local want="$1" timeout="${2:-90}" i text
    for i in $(seq 1 $((timeout / 2))); do
        sleep 2
        text="$(cc_session_user_text || true)"
        if [[ "$text" == *"$want"* ]]; then sleep 2; return 0; fi
    done
    echo "    TIMEOUT waiting for a user turn carrying: $want (${timeout}s)"
    return 1
}

# cc_command_render_check <label> <render|empty> <header> <needle>...: the verdict of a
# slash-command injection scenario (21, 24, 25), read from the expanded command in the
# session transcript (cc_expanded_command_has). `render`: every needle must be there.
# `empty` (a negative control whose injection prints nothing): the body must have reached
# the model and the needles must NOT be there, which is the proof the check discriminates.
cc_command_render_check() {
    local label="$1" expect="$2" header="$3" rc=0
    shift 3
    cc_expanded_command_has "$header" "$@" || rc=$?
    case "$rc:$expect" in
        0:render) _cc_pass "$label" "${label}: the expanded command in the session transcript carries the injection's output (${#} marker(s) found in the user turn; the model's own account was not consulted)" ;;
        1:render) _cc_fail "$label" "${label}: the command body reached the model but the injection's output is not in it: the \`!\` block did not run or its output was mangled" ;;
        1:empty)  _cc_pass "$label" "${label}: negative control: with an injection that prints nothing the command body reached the model and the output markers are absent, so the check does discriminate" ;;
        0:empty)  _cc_fail "$label" "${label}: negative control failed: the output markers are in the user turn although the injection prints nothing, so the check cannot tell a rendered injection from an absent one" ;;
        2:*)      _cc_fail "$label" "${label}: no user turn of the main session carries the command body ('${header}'): the command never reached the model, nothing to conclude" ;;
        3:*)      _cc_fail "$label" "${label}: Claude Code reported a shell error in the expanded command: the injection errored" ;;
    esac
    return 0
}

# cc_hook_context_chars <event> <hook-ere> <plugin_id>: the sizes (one per line) of
# the non-empty additionalContext Claude Code ACCEPTED from a hook of <event> of
# plugin <plugin_id> whose command (or mcp_tool target) matches <hook-ere>: the
# debug log's `Hook <Event> (<command>) provided additionalContext (N chars)`.
# Returns 1 and prints nothing when there is none. A hook that finishes with
# status 0 and prints nothing, or prints text Claude Code drops, never logs this
# line, so it is the delivery half of the evidence cc_hook_ran/cc_mcp_hook_ran
# cannot give; and N is the length of what was delivered, so a caller that knows
# what the hook should emit can tell a partial delivery from a whole one.
#
# Scoped to the plugin like cc_mcp_hook_ran (nexus-qsmc5): an mcp_tool hook's
# line names its plugin (`plugin:<name>:<server>/<tool>`), so the name is matched
# whole; a command hook's line carries only the command (with
# ${CLAUDE_PLUGIN_ROOT} unexpanded), so the plugin is established by the
# `... (plugin <id>) finished with status 0` line for the same script
# (cc_hook_ran). Two plugins that each run a command hook of the same script name
# cannot be told apart by the log; the sn and conexus plugins do not.
cc_hook_context_chars() {
    local event="$1" hook="$2" id="${3:-}" plugin lines=""
    plugin="${id%@*}"
    [[ -n "$id" && -n "$plugin" && -n "$hook" ]] || return 1
    [[ -n "${CC_DEBUG_FILE:-}" && -f "$CC_DEBUG_FILE" ]] || return 1
    lines="$(grep -E "Hook ${event} \(plugin:${plugin}:.*${hook}.*\) provided additionalContext \([1-9][0-9]* chars\)" "$CC_DEBUG_FILE" || true)"
    if [[ -z "$lines" ]] && cc_hook_ran "$event" "$id" "$hook"; then
        # command hooks only: another plugin's mcp_tool line names its plugin and is not this one's
        lines="$(grep -E "Hook ${event} \(.*${hook}.*\) provided additionalContext \([1-9][0-9]* chars\)" "$CC_DEBUG_FILE" | grep -vF " (plugin:" || true)"
    fi
    [[ -n "$lines" ]] || return 1
    sed -E 's/.*\(([1-9][0-9]*) chars\)[[:space:]]*$/\1/' <<<"$lines"
}

# cc_hook_context_injected <event> <hook-ere> <plugin_id>: 0 when
# cc_hook_context_chars finds delivered context.
cc_hook_context_injected() {
    local sizes
    sizes="$(cc_hook_context_chars "$@")" || return 1
    [[ -n "$sizes" ]]
}

# cc_hook_context_is_whole <event> <hook-ere> <plugin_id> <chars>...: 0 when the
# plugin's hook delivered additionalContext of EXACTLY one of the given sizes.
# The caller knows what the hook emits (the lengths of the files it concatenates),
# so a delivery of only some of it, or of something else, does not pass
# (nexus-qsmc5: scenario 18 could not tell a partial delivery from a whole one).
cc_hook_context_is_whole() {
    local event="$1" hook="$2" id="$3" got n w
    shift 3
    got="$(cc_hook_context_chars "$event" "$hook" "$id")" || return 1
    for n in $got; do
        for w in "$@"; do
            if [[ "$n" == "$w" ]]; then return 0; fi
        done
    done
    return 1
}

# _cc_transcript_items <text|tool_use|user_text> [find-predicates]: from the session
# transcripts (*.jsonl under the sandbox's projects dir) the predicates select,
# the assistant text blocks (kind text), the names of the tools the assistant
# called (kind tool_use), or the user turns' text, tool results excluded (kind
# user_text), one per line.
_cc_transcript_items() {
    local kind="$1"
    shift
    find "$TEST_HOME/.claude/projects" -name '*.jsonl' "$@" -print0 2>/dev/null | CC_ITEM_KIND="$kind" xargs -0 -r python3 -I -c '
import json, os, sys
kind = os.environ["CC_ITEM_KIND"]
want = "user" if kind == "user_text" else "assistant"
for path in sys.argv[1:]:
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("type") != want:
            continue
        content = (d.get("message") or {}).get("content")
        if kind == "user_text":
            # what was SENT to the model as a user turn (a typed prompt, an expanded slash
            # command); a tool_result block is tool output, not a turn
            if isinstance(content, str):
                print(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        print(block.get("text", ""))
            continue
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if kind == "text" and block.get("type") == "text":
                    print(block.get("text", ""))
                elif kind == "tool_use" and block.get("type") == "tool_use":
                    print(block.get("name", ""))
'
}

# cc_subagent_replies: the text the dispatched subagents themselves answered,
# read from their own transcripts (agent-<id>.jsonl under the session's
# projects dir), one assistant text block per line group. The pane is NOT a
# source for a subagent's answer: the main agent paraphrases it, redacts agent
# ids ("internal ID - do not mention"), and sometimes disputes it, so a probe
# token can be missing from the pane although the subagent said it (seen live,
# 2026-10-17) or present there although the subagent did not.
cc_subagent_replies() {
    _cc_transcript_items text -name 'agent-*.jsonl'
}

# cc_subagent_tool_uses: the names of the tools the dispatched subagents themselves
# called (their own transcripts). A subagent that dispatches another one calls the
# Task/Agent tool here, which no amount of text in its reply can fake (nexus-qsmc5).
cc_subagent_tool_uses() {
    _cc_transcript_items tool_use -name 'agent-*.jsonl'
}

# cc_session_replies: the same for the MAIN session (its own transcript, never a
# subagent's). The pane shows the prompt and the hook output Claude Code prints,
# so a marker a hook injected can be on the pane without the model ever having
# seen it as context (nexus-qsmc5); the model's own text is the evidence.
cc_session_replies() {
    _cc_transcript_items text ! -name 'agent-*.jsonl'
}

# cc_session_user_text: the user turns of the MAIN session as sent to the model, tool
# results excluded: the typed prompt and, for a slash command, the command body AFTER
# Claude Code expanded its `!` injections. This is the only place the expansion's
# output can be read without trusting the model's account of it, and the model's
# own text (cc_session_replies) is not in it (nexus-qsmc5: 21, 24 and 25 judged the pane).
cc_session_user_text() {
    _cc_transcript_items user_text ! -name 'agent-*.jsonl'
}

# cc_nested_dispatch_facts <nested-token-ere> <blocked-token-ere>: key=value lines about
# what the dispatched subagents did, from their own transcripts (lib/agent_facts.py).
cc_nested_dispatch_facts() {
    python3 -I "$REPO_ROOT/tests/cc-validation/lib/agent_facts.py" "$TEST_HOME/.claude/projects" "$1" "$2"
}

# cc_wait_subagent_reply <ere> [timeout-seconds]: poll the subagents' own
# replies (cc_subagent_replies), not the pane, for <ere>.
cc_wait_subagent_reply() {
    local pat="$1" timeout="${2:-180}" i
    for i in $(seq 1 $((timeout / 2))); do
        sleep 2
        local replies
        replies="$(cc_subagent_replies || true)"
        if grep -qE "$pat" <<<"$replies"; then return 0; fi
    done
    echo "    TIMEOUT waiting for a subagent reply matching: $pat (${timeout}s)"
    return 1
}

# cc_wait_session_reply <ere> [timeout-seconds]: the same for the main session.
cc_wait_session_reply() {
    local pat="$1" timeout="${2:-180}" i
    for i in $(seq 1 $((timeout / 2))); do
        sleep 2
        local replies
        replies="$(cc_session_replies || true)"
        if grep -qE "$pat" <<<"$replies"; then return 0; fi
    done
    echo "    TIMEOUT waiting for a session reply matching: $pat (${timeout}s)"
    return 1
}

_cc_plugins_baseline

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

# RDR-219 transport rule: a tmux session takes its environment from the tmux
# SERVER, not from the command that asks for the session -- so kill-server
# (not just kill-session) first, ensuring a stale server from a killed prior
# run is never reused, then start the private server fresh under
# `_cred_tool run --`. That exec puts CLAUDE_CODE_OAUTH_TOKEN in the
# environment of the process that starts the server, which every later
# pane and session on this socket inherits; it is also this harness's
# fail-loud credential gate (see the NOTE near the top of this file) and
# runs before anything else in the harness, tmux included.
_tmux kill-server 2>/dev/null || true
_cred_tool run -- tmux -L "$NX_TMUX_SOCKET" new-session -d -s "$TMUX_SESSION" -x 220 -y 50

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
