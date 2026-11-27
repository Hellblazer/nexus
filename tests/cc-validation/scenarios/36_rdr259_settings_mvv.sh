#!/usr/bin/env bash
# Scenario 36 — RDR-259's Minimum Viable Validation, run live (nexus-sby49.16).
#
# RDR-259 (conexus project and user settings) says the design is shipped when, in a scratch repository
# with the conexus plugin of THIS tree and nothing else (P0):
#   (1) with no settings file, a dispatched subagent can `git commit` and is denied `git push --force`;
#   (2) after `.nexus/project.json` holds {"version": 1, "subagent_git": "strict"}, the same kind of
#       subagent is denied `git commit` in the primary checkout and allowed it in a linked worktree;
#   (3) after the key is misspelled, the next session's FIRST PROMPT shows the user a message naming the
#       file and suggesting `subagent_git`;
#   (4) `python3 <plugin root>/hooks/scripts/_audience.py show` reports each of those states;
#   (5) the same with `nx` on PATH (P2): `nx settings show` prints the same report except the version lines.
#
# What is judged, and from where (never the model's prose):
#   - the repository's own git state (the primary's log, the bare remote's refs, the worktree list);
#   - what each subagent's Bash calls returned, read from the subagent's OWN transcript (agent-*.jsonl):
#     the command it ran, the cwd it ran in, the tool result with the hook's deny text;
#   - Claude Code's debug log (`--debug-file`): the plugin's PreToolUse and UserPromptSubmit hooks finished;
#   - the pane, only for what a user sees (the first-prompt notice) and the transcript's own record of it;
#   - the reports `_audience.py show` and `nx settings show` print.
# Every expected marker is asserted PRESENT (fail-closed): a session that never ran the plugin, a subagent
# that never attempted a command, or a hook that never fired, fails; it cannot pass by absence.
#
# P0 is built, not assumed: the sessions run with a PATH that holds `claude` and `uv` (symlinks in a directory
# of its own) and the system directories, and the scenario FAILS unless `nx` is not found on it. The nx
# P2 reports run with the worktree's own `.venv/bin` first on that PATH.
#
# Plugin under test: the conexus tree of THIS checkout ($REPO_ROOT/conexus), installed with cc_plugin_require
# into the sandbox's own HOME (the installed 8.5.8 under the user's real ~/.claude is not visible to the
# session, and its version string equals this tree's, so the version proves nothing). Provenance is
# the installed tree being byte-identical to $REPO_ROOT/conexus, and the sessions' debug logs naming it.
#
# Known limit, stated once: `_audience.py` reads the USER file (<account home>/.config/nexus/conexus.json)
# from the operating system's account database, never from HOME (RDR-259 Decision 2), so a sandbox HOME
# does not move it. The scenario writes nothing there and FAILS if the user file exists on this machine,
# because it would change the states under test.
#
# Launched enforced (no --dangerously-skip-permissions), defaultMode acceptEdits, with Bash and the Agent
# tool allowed by rule: a hook's deny wins over an allow rule, and everything a hook does not deny runs.

scenario "36 rdr259_settings_mvv: RDR-259's Minimum Viable Validation live: P0 (plugin of this tree, no nx on PATH) subagent git guard in seatbelt and strict mode, the first-prompt notice, show; then P2 nx settings show"

S36_ROOT="$TEST_HOME/s36"
S36_ORIGIN="$S36_ROOT/origin.git"
S36_REPO=""
# Claude Code reads hooks.json and runs the hook scripts from the marketplace copy cc_plugin_install made
# (`claude plugin list` says "Read from:"; the debug log's hook commands name it), not from the cache copy.
S36_RUN_ROOT="$TEST_HOME/marketplace-conexus/conexus"
S36_BIN="$TEST_HOME/p0bin"
S36_P0_PATH="$S36_BIN:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
S36_P2_PATH="$REPO_ROOT/.venv/bin:$S36_P0_PATH"
S36_ORIG_PATH="$TEST_HOME/.local/bin:$PATH"
S36_EVID="$TEST_HOME/s36-evidence"
S36_CALLS="$TEST_HOME/s36_calls.py"
S36_OK=1

# Nothing of the maintainer's own git configuration reaches the scratch repository this script makes.
_s36_git() { GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 git "$@"; }

# _s36_run <path> <command...>: one command in a clean environment (no inherited session, nexus or token
# variable), the sandbox's HOME and Claude config directory, PATH as given.
_s36_run() {
    local path="$1"
    shift
    env -i PATH="$path" HOME="$TEST_HOME" CLAUDE_CONFIG_DIR="$TEST_HOME/.claude" NX_NO_TELEMETRY=1 LANG=C.UTF-8 "$@"
}

_s36_seed_trust() {
    python3 - "$TEST_HOME/.claude.json" "$S36_REPO" <<'PY'
import json, os, pathlib, sys
cfg_p, proj = pathlib.Path(sys.argv[1]), sys.argv[2]
try:
    data = json.loads(cfg_p.read_text())
except Exception:
    data = {}
if not isinstance(data, dict):
    data = {}
projects = data.setdefault("projects", {})
for key in {proj, os.path.realpath(proj)}:
    entry = projects.setdefault(key, {})
    entry["hasTrustDialogAccepted"] = True
    entry["hasCompletedProjectOnboarding"] = True
cfg_p.write_text(json.dumps(data, indent=2))
PY
}

# The subagents' Bash calls, one per line, tab separated: transcript file, cwd, is_error (0|1|none), command,
# result text (control characters folded to spaces, cut at 600). Read from the agents' own transcripts.
cat > "$S36_CALLS" <<'PY'
import glob, json, os, sys

def clean(text):
    return " ".join(str(text).replace("\t", " ").replace("\r", " ").replace("\n", " ").split())

root = sys.argv[1]
for path in sorted(glob.glob(os.path.join(root, "**", "agent-*.jsonl"), recursive=True)):
    uses, order, results = {}, [], {}
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        content = (d.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if d.get("type") == "assistant" and b.get("type") == "tool_use" and b.get("name") == "Bash":
                uses[b.get("id")] = (d.get("cwd") or "", (b.get("input") or {}).get("command") or "")
                order.append(b.get("id"))
            elif d.get("type") == "user" and b.get("type") == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                results[b.get("tool_use_id")] = (b.get("is_error"), c if c is not None else "")
    for i in order:
        cwd, cmd = uses[i]
        err, text = results.get(i, (None, "<no result recorded>"))
        print("\t".join([os.path.basename(path), clean(cwd), "none" if err is None else ("1" if err else "0"), clean(cmd), clean(text)[:600]]))
PY

# _s36_calls_live: the Bash calls of every subagent of the session just ended (its transcripts are cleared by
# the next session, so _s36_end saves them to bash-calls-<label>.tsv).
_s36_calls_live() { python3 -I "$S36_CALLS" "$TEST_HOME/.claude/projects" 2>/dev/null || true; }

# _s36_call <command-needle> <field>: field N (1 file, 2 cwd, 3 is_error, 4 command, 5 result) of the first
# subagent Bash call of any session so far whose command holds the needle; empty when there is none. Each
# session's needles are its own (mvv1-..., mvv2-...), so sessions cannot answer for each other.
_s36_call() {
    cat "$S36_EVID"/bash-calls-*.tsv 2>/dev/null | awk -F'\t' -v t="$1" -v n="$2" 'index($4, t) { print $n; exit }'
}

# _s36_ref <git-dir> <ref>: 0 when the ref exists there.
_s36_ref() { _s36_git -C "$1" show-ref --verify --quiet "$2"; }

# _s36_begin <label>: a fresh session in the scratch repository, with fresh transcripts and debug log.
_s36_begin() {
    cc_reset_transcripts
    rm -f "$CC_DEBUG_FILE"
    : > "$HOOK_LOG"
    send_keys "cd $S36_REPO" Enter
    sleep 0.5
    claude_start_enforced
}

# _s36_end <label>: leave the session and keep what it left: the pane (before exit), then the debug log and the
# transcripts (after, so they are flushed).
_s36_end() {
    local label="$1"
    cc_save_pane 120
    cp "$TEST_HOME/pane-tail.txt" "$S36_EVID/pane-$label.txt" 2>/dev/null || true
    cc_dismiss_permission_prompt
    claude_exit
    send_keys "cd $REPO_ROOT" Enter
    sleep 0.5
    cp "$CC_DEBUG_FILE" "$S36_EVID/debug-$label.log" 2>/dev/null || true
    rm -rf "$S36_EVID/projects-$label"
    cp -R "$TEST_HOME/.claude/projects" "$S36_EVID/projects-$label" 2>/dev/null || true
    _s36_calls_live > "$S36_EVID/bash-calls-$label.tsv"
    cc_keep_evidence "36-$label"
}

# _s36_hook_ran <script-ere>: the plugin's PreToolUse hook for <script> finished with status 0 in the debug log.
_s36_hook_ran() { cc_hook_ran "PreToolUse" "$CC_PLUGIN_ID" "$1"; }

# _s36_dispatch <token-suffix> <description> <isolation: 0|1> <command-for-the-subagent>: prompt the main
# session to dispatch one general-purpose subagent that runs the command once and answers with a reply token
# spelled in two pieces; wait for that reply (read from the subagent's own transcript) and settle.
_s36_dispatch() {
    local suffix="$1" desc="$2" iso="$3" command="$4" via=""
    if [[ "$iso" == 1 ]]; then via=", and isolation set to worktree"; fi
    claude_prompt "This is an automated test of a PreToolUse hook, in a throwaway repository whose origin is a local bare repository in a temporary directory, so nothing leaves this machine. The point is to see what the hook allows and what it refuses, so each command below must be run exactly as written, once. Dispatch the general-purpose agent with the Agent tool (older builds call it Task; if it is not directly callable, first load it with ToolSearch, query select:Agent), description '$desc'${via}. Give it exactly this prompt: 'This is an automated hook test in a throwaway repository whose remote is a local bare repository. Run these Bash commands in your current directory, each as its own Bash call, in this order: ${command}. Run each exactly as written, once, even when an earlier one was refused; when one is refused, do not reword it, retry it or work around it. Do not create, clone or initialise any repository and do not change directory. When done, reply with the word PROBE immediately followed by -${suffix} (joined, no space), then one line per command saying whether it ran or was refused.' Do not run any command yourself."
    cc_wait_subagent_reply "PROBE-${suffix}" 240 || true
    sleep 3
}

# --- scratch repository, remote, P0 PATH, preconditions ----------------------------------------------------

rm -rf "$S36_ROOT" "$S36_BIN" "$S36_EVID"
mkdir -p "$S36_ROOT" "$S36_BIN" "$S36_EVID"
_s36_git init -q --bare -b main "$S36_ORIGIN"
_s36_git init -q -b main "$S36_ROOT/repo"
S36_REPO="$(cd "$S36_ROOT/repo" && pwd -P)"
_s36_git -C "$S36_REPO" config user.name mvv
_s36_git -C "$S36_REPO" config user.email mvv@example.invalid
_s36_git -C "$S36_REPO" remote add origin "$S36_ORIGIN"
echo base > "$S36_REPO/base.txt"
_s36_git -C "$S36_REPO" add base.txt
_s36_git -C "$S36_REPO" commit -q -m "mvv base"
_s36_git -C "$S36_REPO" push -q origin main
S36_BASE_SHA="$(_s36_git -C "$S36_REPO" rev-parse HEAD)"
_s36_seed_trust
ln -s "$(command -v claude)" "$S36_BIN/claude"
ln -s "$(command -v uv)" "$S36_BIN/uv"

if _s36_run "$S36_P0_PATH" sh -c 'command -v nx || command -v nx-mcp || command -v nx-hook' >/dev/null 2>&1; then
    _cc_fail 36 "36: P0 precondition: an nx program IS on the sessions' PATH ($S36_P0_PATH): this would be a P2 machine, so the P0 legs prove nothing"
    S36_OK=0
fi
if ! _s36_run "$S36_P0_PATH" sh -c 'command -v claude && command -v uv && command -v git && command -v python3' >/dev/null 2>&1; then
    _cc_fail 36 "36: P0 PATH ($S36_P0_PATH) cannot find claude, uv, git and python3: the sessions cannot start"
    S36_OK=0
fi
if ! _s36_run "$S36_P2_PATH" sh -c 'nx settings --help' >/dev/null 2>&1; then
    _cc_fail 36 "36: P2 precondition: $REPO_ROOT/.venv/bin/nx has no 'settings' command (run uv sync in the checkout)"
    S36_OK=0
fi
if [[ -n "${CLAUDE_CONFIG_DIR:-}" && "$CLAUDE_CONFIG_DIR" != "$TEST_HOME"* ]]; then
    _cc_fail 36 "36: CLAUDE_CONFIG_DIR=$CLAUDE_CONFIG_DIR points outside the sandbox: the installed plugins of the real account could be read"
    S36_OK=0
fi

# Settings first (the plugin install merges enabledPlugins into this file).
cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Agent", "Task", "Bash"], "defaultMode": "acceptEdits" }
}
EOF

if [[ "$S36_OK" == 1 ]] && cc_plugin_require "$REPO_ROOT/conexus" conexus nexus-plugins; then

    # --- provenance: the plugin under test is this checkout's conexus/, not the installed 8.5.8 -------------
    echo "    claude: $(cc_claude_version); plugin $CC_PLUGIN_ID $CC_PLUGIN_VERSION installed at $CC_PLUGIN_INSTALLED_ROOT"
    sed 's/^/    | plugin list: /' "$TEST_HOME/plugin-list.txt"
    if [[ "$CC_PLUGIN_INSTALLED_ROOT" == "$TEST_HOME/.claude/plugins/"* ]]; then
        _cc_pass 36 "36 provenance: the plugin is installed under the sandbox's own HOME ($CC_PLUGIN_INSTALLED_ROOT), never the user's ~/.claude"
    else
        _cc_fail 36 "36 provenance: the plugin's install path $CC_PLUGIN_INSTALLED_ROOT is not under $TEST_HOME/.claude/plugins"
    fi
    for prov_root in "$CC_PLUGIN_INSTALLED_ROOT" "$S36_RUN_ROOT"; do
        PROV_DIFF="$(diff -rq -x __pycache__ "$REPO_ROOT/conexus" "$prov_root" 2>&1 || true)"
        if [[ -z "$PROV_DIFF" ]]; then
            _cc_pass 36 "36 provenance: $prov_root is byte-identical to $REPO_ROOT/conexus ($(find "$prov_root" -type f -not -path '*/__pycache__/*' | wc -l | tr -d ' ') files, checkout HEAD $(_s36_git -C "$REPO_ROOT" rev-parse --short HEAD))"
        else
            _cc_fail 36 "36 provenance: $prov_root differs from $REPO_ROOT/conexus: $(head -5 <<<"$PROV_DIFF" | tr '\n' ';')"
        fi
    done
    if [[ -f "$S36_RUN_ROOT/hooks/scripts/settings_notice.py" && -f "$S36_RUN_ROOT/hooks/scripts/_audience.py" ]]; then
        _cc_pass 36 "36 provenance: the installed plugin carries the RDR-259 files settings_notice.py and _audience.py (the 8.5.8 release does not)"
    else
        _cc_fail 36 "36 provenance: the installed plugin lacks settings_notice.py or _audience.py"
    fi

    # --- the P0 sessions' PATH ------------------------------------------------------------------------------
    send_keys "export PATH='$S36_P0_PATH'" Enter
    sleep 0.5

    # ============ (1) no settings file: commit allowed, force push denied ======================================
    scenario "36a no settings file: a dispatched subagent can commit and push, and is denied a force push"
    rm -rf "$S36_REPO/.nexus"
    _s36_run "$S36_P0_PATH" python3 "$S36_RUN_ROOT/hooks/scripts/_audience.py" show --cwd "$S36_REPO" > "$S36_EVID/show-p0-a.txt" 2>&1 || true
    _s36_run "$S36_P2_PATH" python3 "$S36_RUN_ROOT/hooks/scripts/_audience.py" show --cwd "$S36_REPO" > "$S36_EVID/show-p2plugin-a.txt" 2>&1 || true
    _s36_run "$S36_P2_PATH" nx settings show --cwd "$S36_REPO" > "$S36_EVID/show-p2nx-a.txt" 2>&1 || true
    _s36_begin a
    _s36_dispatch 36A "git guard probe one" 0 "(1) git commit --allow-empty -m mvv1-commit  (2) git push origin HEAD:refs/heads/mvv1-plain  (3) git push --force origin HEAD:refs/heads/mvv1-forced"
    _s36_end a

    if _s36_git -C "$S36_REPO" log --format=%s | grep -qx "mvv1-commit"; then
        _cc_pass 36 "36a(1) the subagent's git commit landed in the primary checkout (log holds mvv1-commit; its Bash call ran in cwd '$(_s36_call mvv1-commit 2)', is_error=$(_s36_call mvv1-commit 3))"
    else
        _cc_fail 36 "36a(1) no commit mvv1-commit in the primary's log: the subagent's commit did not land (call result: $(_s36_call mvv1-commit 5))"
    fi
    if _s36_ref "$S36_ORIGIN" refs/heads/mvv1-plain; then
        _cc_pass 36 "36a(1) control: the plain git push landed on the bare remote (refs/heads/mvv1-plain exists), so the remote is reachable and a missing mvv1-forced is the guard's doing"
    else
        _cc_fail 36 "36a(1) control: the plain git push did not land (result: $(_s36_call mvv1-plain 5)); the force-push leg cannot be read"
    fi
    FORCE_CMD="$(_s36_call mvv1-forced 4)"
    FORCE_RESULT="$(_s36_call mvv1-forced 5)"
    if [[ -z "$FORCE_CMD" ]]; then
        _cc_fail 36 "36a(1) the subagent never ran the force push (no Bash call naming mvv1-forced in its transcript): not attempted, so nothing to conclude"
    elif _s36_ref "$S36_ORIGIN" refs/heads/mvv1-forced; then
        _cc_fail 36 "36a(1) PRODUCT: the force push LANDED on the remote (refs/heads/mvv1-forced exists): the seatbelt did not deny it. Call: $FORCE_CMD; result: $FORCE_RESULT"
    elif grep -qF "was stopped: it can destroy history" <<<"$FORCE_RESULT"; then
        _cc_pass 36 "36a(1) the force push was denied by the plugin's seatbelt: '${FORCE_RESULT:0:160}' and refs/heads/mvv1-forced is absent from the remote"
    else
        _cc_fail 36 "36a(1) the force push did not land but its result is not the seatbelt's deny text: '$FORCE_RESULT'"
    fi
    if _s36_hook_ran "subagent_git_write_requires_orchestrator"; then
        _cc_pass 36 "36a debug log: the plugin's subagent_git_write_requires_orchestrator PreToolUse hook finished with status 0 ($(grep -cE 'Hooks: PreToolUse[^[]*\[[^]]*subagent_git_write_requires_orchestrator' "$S36_EVID/debug-a.log" 2>/dev/null || echo 0) log lines)"
    else
        _cc_fail 36 "36a debug log: no finished subagent_git_write_requires_orchestrator hook of $CC_PLUGIN_ID: the guard never ran"
    fi
    # The session ran its hooks FROM the byte-identical copy of this tree: the guard's command names a script
    # under $S36_RUN_ROOT, and no plugin hook line of the log names any other directory.
    if grep -qF "python $S36_RUN_ROOT/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py" "$S36_EVID/debug-a.log" 2>/dev/null \
        && ! grep -E 'Hooks: [A-Za-z:]+ \[' "$S36_EVID/debug-a.log" | grep -F '(plugin ' | grep -vF "$S36_RUN_ROOT" | grep -q .; then
        _cc_pass 36 "36a provenance: the session's debug log shows its plugin hooks running from $S36_RUN_ROOT ($(grep -cF "$S36_RUN_ROOT/hooks/" "$S36_EVID/debug-a.log") lines) and from no other plugin directory"
    else
        _cc_fail 36 "36a provenance: the debug log does not show the plugin's hooks running from $S36_RUN_ROOT only"
    fi
    if grep -qE 'Hook PreToolUse \([^)]*subagent_git_write_requires_orchestrator[^)]*\) returned permissionDecision: deny \(reason: git push --force' "$S36_EVID/debug-a.log" 2>/dev/null; then
        _cc_pass 36 "36a debug log: Claude Code logged the guard's own answer, permissionDecision: deny, for the force push"
    else
        _cc_fail 36 "36a debug log: no 'returned permissionDecision: deny' line from the guard for the force push"
    fi

    # ============ (2) strict: denied in the primary, allowed in a linked worktree ==============================
    scenario "36b subagent_git strict: a dispatched subagent is denied git commit in the primary and allowed it in a linked worktree"
    mkdir -p "$S36_REPO/.nexus"
    printf '{"version": 1, "subagent_git": "strict"}\n' > "$S36_REPO/.nexus/project.json"
    _s36_run "$S36_P0_PATH" python3 "$S36_RUN_ROOT/hooks/scripts/_audience.py" show --cwd "$S36_REPO" > "$S36_EVID/show-p0-b.txt" 2>&1 || true
    _s36_run "$S36_P2_PATH" python3 "$S36_RUN_ROOT/hooks/scripts/_audience.py" show --cwd "$S36_REPO" > "$S36_EVID/show-p2plugin-b.txt" 2>&1 || true
    _s36_run "$S36_P2_PATH" nx settings show --cwd "$S36_REPO" > "$S36_EVID/show-p2nx-b.txt" 2>&1 || true
    _s36_begin b1
    _s36_dispatch 36B "git guard probe primary" 0 "(1) git commit --allow-empty -m mvv2-primary"
    _s36_end b1
    # A fresh session for the worktree dispatch: one session's refusal makes the model wary of the next dispatch
    # (seen live: the model declined to repeat the "same" commit with isolation, as a way round the refusal).
    _s36_begin b2
    _s36_dispatch 36C "git guard probe worktree" 1 "(1) git commit --allow-empty -m mvv2-worktree"
    _s36_end b2
    if grep -qE 'Hook PreToolUse \([^)]*subagent_git_write_requires_orchestrator[^)]*\) returned permissionDecision: deny \(reason: Denied: subagent' "$S36_EVID/debug-b1.log" 2>/dev/null; then
        _cc_pass 36 "36b debug log: Claude Code logged the guard's own answer, permissionDecision: deny (strict guard text), for the primary commit"
    else
        _cc_fail 36 "36b debug log: no 'returned permissionDecision: deny (reason: Denied: subagent ...' line from the guard in the primary session"
    fi
    if grep -qE 'Hook PreToolUse \([^)]*subagent_git_write_requires_orchestrator[^)]*\) returned permissionDecision: deny' "$S36_EVID/debug-b2.log" 2>/dev/null; then
        _cc_fail 36 "36b debug log: the guard answered permissionDecision: deny in the worktree session"
    else
        _cc_pass 36 "36b debug log: the guard gave no deny in the worktree session"
    fi

    PRIM_RESULT="$(_s36_call mvv2-primary 5)"
    if [[ -z "$(_s36_call mvv2-primary 4)" ]]; then
        _cc_fail 36 "36b(2) the subagent never ran the primary commit (no Bash call naming mvv2-primary): not attempted, so nothing to conclude"
    elif _s36_git -C "$S36_REPO" log --all --format=%s | grep -qx "mvv2-primary"; then
        _cc_fail 36 "36b(2) PRODUCT: the commit mvv2-primary LANDED in the primary with subagent_git strict. Call cwd '$(_s36_call mvv2-primary 2)'; result: $PRIM_RESULT"
    elif grep -qF "may not run git write verbs" <<<"$PRIM_RESULT"; then
        _cc_pass 36 "36b(2) with subagent_git strict the subagent's git commit in the primary was denied by the strict guard ('${PRIM_RESULT:0:140}') and no mvv2-primary commit exists"
    else
        _cc_fail 36 "36b(2) the primary commit did not land but its result is not the strict guard's deny text: '$PRIM_RESULT'"
    fi
    WT_CWD="$(_s36_call mvv2-worktree 2)"
    WT_RESULT="$(_s36_call mvv2-worktree 5)"
    WT_SHA="$(_s36_git -C "$S36_REPO" log --all --format='%H %s' | awk '$2 == "mvv2-worktree" { print $1; exit }')"
    if [[ -z "$(_s36_call mvv2-worktree 4)" ]]; then
        _cc_fail 36 "36b(2) the worktree subagent never ran its commit (no Bash call naming mvv2-worktree): not attempted, so nothing to conclude"
    elif [[ -z "$WT_SHA" ]]; then
        _cc_fail 36 "36b(2) the commit mvv2-worktree does not exist in the repository: it was refused or never ran (cwd '$WT_CWD'; result: $WT_RESULT)"
    elif [[ "$WT_CWD" != *"/.claude/worktrees/"* ]]; then
        _cc_fail 36 "36b(2) the commit exists but its Bash call ran in cwd '$WT_CWD', not in a linked worktree under .claude/worktrees: the dispatch was not isolated, so the linked-worktree allowance is unproven"
    elif _s36_git -C "$S36_REPO" log --format=%s HEAD | grep -qx "mvv2-worktree"; then
        _cc_fail 36 "36b(2) the worktree commit is on the primary's own branch: it was made in the primary"
    elif _s36_git -C "$S36_REPO" worktree list --porcelain | grep -qE "^worktree .*/\.claude/worktrees/"; then
        _cc_pass 36 "36b(2) with subagent_git strict the subagent's git commit in a linked worktree was allowed: $WT_SHA mvv2-worktree, run in cwd '$WT_CWD', off the primary's branch (is_error=$(_s36_call mvv2-worktree 3))"
    else
        _cc_fail 36 "36b(2) the commit exists but no linked worktree under .claude/worktrees is registered"
    fi
    _s36_git -C "$S36_REPO" worktree list | sed 's/^/    | worktree list: /'
    if _s36_hook_ran "subagent_git_write_requires_orchestrator"; then
        _cc_pass 36 "36b debug log: the plugin's subagent_git_write_requires_orchestrator hook finished with status 0"
    else
        _cc_fail 36 "36b debug log: no finished subagent_git_write_requires_orchestrator hook of $CC_PLUGIN_ID"
    fi

    # ============ (3) a misspelled key: the next session's first prompt shows a message ========================
    scenario "36c misspelled key: the next session's first prompt shows a message naming the file and suggesting subagent_git"
    printf '{"version": 1, "subagent_gti": "strict"}\n' > "$S36_REPO/.nexus/project.json"
    _s36_run "$S36_P0_PATH" python3 "$S36_RUN_ROOT/hooks/scripts/_audience.py" show --cwd "$S36_REPO" > "$S36_EVID/show-p0-c.txt" 2>&1 || true
    _s36_run "$S36_P2_PATH" python3 "$S36_RUN_ROOT/hooks/scripts/_audience.py" show --cwd "$S36_REPO" > "$S36_EVID/show-p2plugin-c.txt" 2>&1 || true
    _s36_run "$S36_P2_PATH" nx settings show --cwd "$S36_REPO" > "$S36_EVID/show-p2nx-c.txt" 2>&1 || true
    _s36_begin c
    claude_prompt "Reply with the single word OK and use no tools."
    claude_wait 90
    sleep 3
    cc_save_pane 200
    cp "$TEST_HOME/pane-tail.txt" "$S36_EVID/pane-c-after-first-prompt.txt" 2>/dev/null || true
    PANE_C="$(capture -200 | tr '\n' ' ' | tr -s ' ')"
    # A second prompt: the notice must not repeat (it speaks on the first prompt of a session only).
    claude_prompt "Reply with the single word FINE and use no tools."
    claude_wait 90
    sleep 3
    _s36_end c
    # What Claude Code recorded as shown to the user: hook_system_message attachments of the main session's transcript.
    S36_NOTICES="$(find "$TEST_HOME/.claude/projects" -name '*.jsonl' ! -name 'agent-*' -print0 2>/dev/null | xargs -0 -r python3 -I -c '
import json, sys
for path in sys.argv[1:]:
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        a = d.get("attachment") if isinstance(d, dict) else None
        if isinstance(a, dict) and a.get("type") == "hook_system_message" and a.get("hookEvent") == "UserPromptSubmit":
            print(" ".join(str(a.get("content", "")).split()))
' || true)"
    S36_NOTICE_N="$(grep -c . <<<"$S36_NOTICES" || true)"
    if [[ "$S36_NOTICE_N" == 1 ]] && grep -qF ".nexus/project.json: subagent_gti: unknown key" <<<"$S36_NOTICES" && grep -qF "did you mean subagent_git?" <<<"$S36_NOTICES"; then
        _cc_pass 36 "36c transcript: Claude Code recorded exactly one UserPromptSubmit message for two prompts, and it names .nexus/project.json, the key subagent_gti and 'did you mean subagent_git?': ${S36_NOTICES:0:230}"
    else
        _cc_fail 36 "36c transcript: expected exactly one UserPromptSubmit hook_system_message naming .nexus/project.json and suggesting subagent_git over two prompts; found $S36_NOTICE_N: ${S36_NOTICES:0:300}"
    fi
    if cc_hook_ran "UserPromptSubmit" "$CC_PLUGIN_ID" "settings_notice"; then
        _cc_pass 36 "36c debug log: the plugin's settings_notice UserPromptSubmit hook finished with status 0"
    else
        _cc_fail 36 "36c debug log: no finished settings_notice UserPromptSubmit hook of $CC_PLUGIN_ID"
    fi
    if grep -qE 'Hook UserPromptSubmit \([^)]*settings_notice[^)]*\) provided additionalContext \([1-9][0-9]* chars\)' "$S36_EVID/debug-c.log" 2>/dev/null; then
        _cc_pass 36 "36c debug log: Claude Code accepted additionalContext from the settings_notice hook ($(grep -oE 'settings_notice[^)]*\) provided additionalContext \([0-9]+ chars\)' "$S36_EVID/debug-c.log" | head -1 | grep -oE '[0-9]+ chars'))"
    else
        _cc_fail 36 "36c debug log: Claude Code accepted no additionalContext from the settings_notice hook"
    fi
    if grep -qF ".nexus/project.json" <<<"$PANE_C" && grep -qF "subagent_gti" <<<"$PANE_C" && grep -qE "did you mean subagent_git" <<<"$PANE_C"; then
        _cc_pass 36 "36c first prompt: the pane (what the user sees) shows the notice: it names .nexus/project.json, the key subagent_gti and 'did you mean subagent_git'"
    else
        _cc_fail 36 "36c first prompt: the pane does not show a notice naming .nexus/project.json and suggesting subagent_git (see $S36_EVID/pane-c-after-first-prompt.txt)"
    fi

    # ============ (4) show, each state: P0 plugin reader ======================================================
    scenario "36d show: python3 <plugin root>/hooks/scripts/_audience.py show reports each state (P0, no nx on PATH)"
    _s36_show_has() {  # <label> <file> <ere> <what>
        if grep -qE "$3" "$2" 2>/dev/null; then _cc_pass 36 "36d $1: $4"; else _cc_fail 36 "36d $1: $4 -- not in $2 (no line matches: $3)"; fi
    }
    for st in a b c; do
        f="$S36_EVID/show-p0-$st.txt"
        _s36_show_has "state $st" "$f" '^versions: conexus plugin [0-9][0-9.]*, nx not found$' "P0 is real: the report finds no nx on the PATH it ran with"
        _s36_show_has "state $st" "$f" '^  user: .*conexus\.json \(not present\)$' "no user file, so the project file alone sets the state"
    done
    _s36_show_has "a" "$S36_EVID/show-p0-a.txt" '^  project: .*/\.nexus/project\.json \(not present\)$' "no project file"
    _s36_show_has "a" "$S36_EVID/show-p0-a.txt" '^  subagent_git = seatbelt  \(source: default\)$' "subagent_git is seatbelt from the default"
    _s36_show_has "a" "$S36_EVID/show-p0-a.txt" '^  no problems$' "no problems"
    _s36_show_has "b" "$S36_EVID/show-p0-b.txt" '^  project: .*/\.nexus/project\.json \(present\)$' "the project file is present"
    _s36_show_has "b" "$S36_EVID/show-p0-b.txt" '^  subagent_git = strict  \(source: project\)$' "subagent_git is strict from the project file"
    _s36_show_has "b" "$S36_EVID/show-p0-b.txt" '^  no problems$' "no problems"
    _s36_show_has "c" "$S36_EVID/show-p0-c.txt" '^  subagent_git = seatbelt  \(source: default\)$' "the misspelled key changed nothing: subagent_git is still seatbelt from the default"
    _s36_show_has "c" "$S36_EVID/show-p0-c.txt" '^  \.nexus/project\.json: subagent_gti: unknown key "subagent_gti"; did you mean subagent_git\?; used: ignored$' "the problem names the file, the key and the suggestion"

    # ============ (5) P2: nx settings show equals the plugin's report except the version lines =================
    scenario "36e P2 (nx on PATH): nx settings show gives the same output as the plugin's reader except the version lines"
    for st in a b c; do
        P0F="$S36_EVID/show-p0-$st.txt"; P2P="$S36_EVID/show-p2plugin-$st.txt"; P2N="$S36_EVID/show-p2nx-$st.txt"
        if grep -qE '^versions: conexus plugin [0-9][0-9.]*, nx [0-9]' "$P2P" 2>/dev/null && grep -qE '^versions: conexus plugin [0-9][0-9.]*, nx [0-9]' "$P2N" 2>/dev/null; then
            _cc_pass 36 "36e state $st: P2 is real: both reports find an nx ($(grep -m1 '^versions:' "$P2N"))"
        else
            _cc_fail 36 "36e state $st: a P2 report does not show an nx version (plugin reader: $(grep -m1 '^versions:' "$P2P" 2>&1); nx settings show: $(head -3 "$P2N" | tr '\n' ' '))"
        fi
        # The version lines are `versions:`, an indented `warning:` and an indented `note:`; nothing else is removed.
        grep -vE '^versions: |^  warning: |^  note: ' "$P2P" > "$S36_EVID/norm-p2plugin-$st.txt" || true
        grep -vE '^versions: |^  warning: |^  note: ' "$P2N" > "$S36_EVID/norm-p2nx-$st.txt" || true
        grep -vE '^versions: |^  warning: |^  note: ' "$P0F" > "$S36_EVID/norm-p0-$st.txt" || true
        if [[ -s "$S36_EVID/norm-p2nx-$st.txt" ]] && diff -u "$S36_EVID/norm-p2plugin-$st.txt" "$S36_EVID/norm-p2nx-$st.txt" > "$S36_EVID/diff-p2-$st-normalized.txt"; then
            _cc_pass 36 "36e state $st: 'nx settings show' equals the plugin reader's report except the version lines ($(wc -l < "$S36_EVID/norm-p2nx-$st.txt" | tr -d ' ') lines compared)"
        else
            _cc_fail 36 "36e state $st: 'nx settings show' differs from the plugin reader's report beyond the version lines: $(head -12 "$S36_EVID/diff-p2-$st-normalized.txt" | tr '\n' '|')"
        fi
        if [[ -s "$S36_EVID/norm-p0-$st.txt" ]] && diff -u "$S36_EVID/norm-p0-$st.txt" "$S36_EVID/norm-p2plugin-$st.txt" > "$S36_EVID/diff-p0-p2-$st-normalized.txt"; then
            _cc_pass 36 "36e state $st: the plugin reader prints the same report on P0 and on P2 except the version lines"
        else
            _cc_fail 36 "36e state $st: the plugin reader's P0 and P2 reports differ beyond the version lines: $(head -12 "$S36_EVID/diff-p0-p2-$st-normalized.txt" | tr '\n' '|')"
        fi
        echo "    --- state $st: diff of the full reports, plugin reader on P0 vs 'nx settings show' on P2 ---"
        { diff "$P0F" "$P2N" || true; } | sed 's/^/    | /'
    done
fi

# Teardown: the pane's PATH and the plugin registry go back as the runner made them.
send_keys "export PATH='$S36_ORIG_PATH'" Enter
sleep 0.5
cc_plugin_uninstall_all
rm -rf "$S36_ROOT" "$S36_BIN" "$S36_CALLS"
scenario_end
