#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/e2e/plugin_out_of_repo_gate_test.sh -- offline self-test for
# tests/e2e/plugin-out-of-repo-gate.sh, tests/e2e/lib/plugin_out_of_repo.py and
# tests/e2e/lib/plugin_canary.py (nexus-z7ddd). Needs no model call and no network:
# the static pass runs on fixture plugins, the live judgement on hand-written
# stream-json event files, and every claude run of the gate goes to a STUB
# (tests/e2e/lib/stub_claude.py) that records argv and environment, so the gate's
# isolation is pinned without touching the real install. It proves the gate goes
# red when it should (fail-first) and does not pass over nothing (nexus-moht0):
#
#   1. the static pass: counts exact; relative link in an agent, repo-only path,
#      missing path, a variable in a Read file or unbraced, each fails with
#      file:line; extraction covers fences and spaced backticks; a near path wins
#      over REPO-ONLY; generic roots are REPO-ONLY only when the checkout has them;
#   2. the allowlist: five fields, class-keyed, >= 3 words, plugin names validated;
#   3. the sweep judgement on event files: Read events decide; noise is UNPROBED;
#      definite evidence fails; no result at all exits 2;
#   4. the canary judgement, and the pins it measures; a session whose model skips a
#      step is NOISE (retried), never ok; the init event of every run is judged;
#   5. the gate end to end with the stub: argv and env pinned (no hooks, no real
#      tool dirs, UPGRADE_AUTO off, scratch cwd), retries, the watchdog and an
#      interrupt kill the process tree;
#   6. the multi-plugin modes: expected applicability, plugin.json name and version.
#
# Run with: bash tests/e2e/plugin_out_of_repo_gate_test.sh
# shellcheck disable=SC2016  # the fixtures hold a literal ${CLAUDE_PLUGIN_ROOT}
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE="$HERE/plugin-out-of-repo-gate.sh"
MOD="$HERE/lib/plugin_out_of_repo.py"
CAN="$HERE/lib/plugin_canary.py"
STUBPY="$HERE/lib/stub_claude.py"
# shellcheck source=lib/python.sh disable=SC1091
source "$HERE/lib/python.sh"
e2e_python_resolve 10 || exit 2
export NX_GATE_RETRY_SLEEP=0   # the backoff between retries is tested on its own
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/plugin_out_of_repo_gate_test.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT
# No run in this self-test may reach the operator's real ~/.claude: the gate's exit cleanup
# resolves <CLAUDE_CONFIG_DIR>/projects, and a test that left it unset once let a mutant of
# that cleanup delete real project directories. Tests that need their own config dir still
# set it per call.
mkdir -p "$WORKDIR/claude-config/projects"
export CLAUDE_CONFIG_DIR="$WORKDIR/claude-config"
PASS=0; FAIL=0
ok()  { echo "  [ok] $1"; PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }
expect_has()   { case "$2" in *"$3"*) ok "$1" ;; *) bad "$1: missing [$3]" ;; esac; }
expect_lacks() { case "$2" in *"$3"*) bad "$1: unexpected [$3]" ;; *) ok "$1" ;; esac; }
chk()          { if "${@:2}"; then ok "$1"; else bad "$1"; fi; }
expect_eq()    { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1: want [$2] got [$3]"; fi; }

[ -f "$GATE" ] && [ -f "$MOD" ] && [ -f "$CAN" ] && [ -f "$STUBPY" ] || { bad "the gate, its modules or the stub do not exist"; echo "$PASS passed, $FAIL failed"; exit 1; }

# ---- fixture plugins -------------------------------------------------------
mkplugin() {  # <dir> [name] [version] : a plugin with one agent, one skill, a command and two resources
    local d="$1" name="${2:-fx}" ver="${3:-1.0.0}"
    mkdir -p "$d/.claude-plugin" "$d/agents" "$d/commands" "$d/skills/probe" "$d/resources/agent-shared"
    printf '{"name": "%s", "version": "%s"}\n' "$name" "$ver" >"$d/.claude-plugin/plugin.json"
    printf '# relay\nsee [ctx](./CTX.md)\n' >"$d/resources/agent-shared/RELAY.md"
    printf '# ctx\n' >"$d/resources/agent-shared/CTX.md"
    printf '# ref\n' >"$d/skills/probe/reference.md"
    printf -- '---\nname: probe\n---\nSee [ref](./reference.md) and [relay](../../resources/agent-shared/RELAY.md).\n' >"$d/skills/probe/SKILL.md"
    printf -- '---\nname: worker\n---\nUse [relay](${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/RELAY.md).\nDocs: https://example.com/x.md and `$RDR_DIR/README.md`.\n' >"$d/agents/worker.md"
    printf 'Run it.\n' >"$d/commands/go.md"
}
mkhooks() {  # <dir> <name> [version] : a hook-only plugin: one injected section, no agents or skills
    local d="$1"
    mkdir -p "$d/.claude-plugin" "$d/hooks/scripts"
    printf '{"name": "%s", "version": "%s"}\n' "${2:-hk}" "${3:-1.0.0}" >"$d/.claude-plugin/plugin.json"
    printf '# Section\nNo file references in here.\n' >"$d/hooks/scripts/a-section.md"
    # documentation under hooks/ is not injected: it must not be scanned
    printf '# readme\nSee `src/nexus/x.py`.\n' >"$d/hooks/README.md"
}
realpath_of() { (cd "$1" && pwd -P); }

CLEAN="$WORKDIR/clean"; mkplugin "$CLEAN"; CLEAN="$(realpath_of "$CLEAN")"
# a nexus checkout stand-in: the structure _is_nexus_checkout wants, plus files under generic roots
CK="$WORKDIR/ck"; mkdir -p "$CK/conexus/.claude-plugin" "$CK/src/nexus" "$CK/scripts" "$CK/docs"
: >"$CK/conexus/.claude-plugin/plugin.json"; : >"$CK/scripts/x.sh"; : >"$CK/scripts/run.py"; : >"$CK/docs/n.md"
CK="$(realpath_of "$CK")"

run_static() { "$E2E_PYTHON" -I "$MOD" static --plugin "$1" --repo-root "$CK" "${@:2}" 2>&1; }
cnt() {  # <plugin> <surface> <class>
    "$E2E_PYTHON" -I "$MOD" static --plugin "$1" --repo-root "$CK" --json 2>&1 | "$E2E_PYTHON" -I -c \
        'import json,sys; d=json.load(sys.stdin); print(d["counts"].get(sys.argv[1],{}).get(sys.argv[2],0))' "$2" "$3"
}

echo "static pass"
out="$(run_static "$CLEAN")"; rc=$?
expect_eq "clean fixture exits 0" 0 "$rc"
expect_eq "clean fixture: 1 RESOLVES in agents" 1 "$(cnt "$CLEAN" agents RESOLVES)"
expect_eq "clean fixture: 1 PLACEHOLDER in agents" 1 "$(cnt "$CLEAN" agents PLACEHOLDER)"
expect_eq "clean fixture: 2 RELATIVE-NEAR in skills" 2 "$(cnt "$CLEAN" skills RELATIVE-NEAR)"
expect_eq "clean fixture: 1 RELATIVE-NEAR in resources" 1 "$(cnt "$CLEAN" resources RELATIVE-NEAR)"
expect_has "clean fixture is reported clean" "$out" "static pass clean: 0 failing"

BAD="$WORKDIR/bad"; mkplugin "$BAD"; BAD="$(realpath_of "$BAD")"
printf -- '---\nname: worker\n---\nline two\nUse [relay](../resources/agent-shared/RELAY.md) and `conexus/registry.yaml` and [x](${CLAUDE_PLUGIN_ROOT}/nope.md).\n' >"$BAD/agents/worker.md"
out="$(run_static "$BAD")"; rc=$?
expect_eq "bad fixture exits 1" 1 "$rc"
expect_has "relative link in an agent is RELATIVE-DEAD with file:line" "$out" "RELATIVE-DEAD  agents/worker.md:5  ../resources/agent-shared/RELAY.md"
expect_has "repo-only path is REPO-ONLY with file:line" "$out" "REPO-ONLY      agents/worker.md:5  conexus/registry.yaml"
expect_has "missing substituted path is MISSING with file:line" "$out" 'MISSING        agents/worker.md:5  ${CLAUDE_PLUGIN_ROOT}/nope.md'
expect_has "exactly three references fail; the skill's ../../ link, which opens, is not one of them" "$out" "3 failing reference(s)"

EMPTY="$WORKDIR/empty"; mkdir -p "$EMPTY/.claude-plugin" "$EMPTY/agents"
printf '{"name": "fx"}\n' >"$EMPTY/.claude-plugin/plugin.json"; printf -- '---\nname: a\n---\nno links\n' >"$EMPTY/agents/a.md"
out="$(run_static "$EMPTY")"; rc=$?
expect_eq "a plugin with no references fails" 1 "$rc"
expect_has "and says why" "$out" "a census of nothing proves nothing"

echo "what Claude Code substitutes (measured by the canary, pinned here)"
SUBP="$WORKDIR/subst"; mkplugin "$SUBP"; SUBP="$(realpath_of "$SUBP")"
printf '# relay\nRead ${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/CTX.md first.\n' >"$SUBP/resources/agent-shared/RELAY.md"
printf '# ref\nSee ${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/CTX.md.\n' >"$SUBP/skills/probe/reference.md"
printf -- '---\nname: probe\n---\nSee ${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/CTX.md.\n' >"$SUBP/skills/probe/SKILL.md"
printf 'Run ${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/CTX.md.\n' >"$SUBP/commands/go.md"
out="$(run_static "$SUBP")"; rc=$?
expect_eq "a variable in a resource and in a skill's reference file fails" 1 "$rc"
expect_has "the resource is UNSUBSTITUTED (a file the model Reads is not substituted)" "$out" 'UNSUBSTITUTED  resources/agent-shared/RELAY.md:2  ${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/CTX.md'
expect_has "the skill's reference.md is UNSUBSTITUTED" "$out" 'UNSUBSTITUTED  skills/probe/reference.md:2'
expect_lacks "SKILL.md is substituted: not a failure" "$out" "skills/probe/SKILL.md"
expect_lacks "a command body is substituted: not a failure" "$out" "commands/go.md"
expect_eq "agent body and SKILL.md and command: RESOLVES" 1 "$(cnt "$SUBP" commands RESOLVES)"
printf -- '---\nname: worker\n---\nUse $CLAUDE_PLUGIN_ROOT/resources/agent-shared/CTX.md here.\n' >"$SUBP/agents/worker.md"
out="$(run_static "$SUBP")"
expect_has "the unbraced variable is UNSUBSTITUTED even in an agent body" "$out" 'UNSUBSTITUTED  agents/worker.md:4  $CLAUDE_PLUGIN_ROOT/resources/agent-shared/CTX.md'

printf -- '---\nname: worker\ndescription: Reads ${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/CTX.md first\nallowed-tools: Read(${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/CTX.md)\n---\nbody\n' >"$SUBP/agents/worker.md"
out="$(run_static "$SUBP")"
expect_has "a variable in an agent's frontmatter DESCRIPTION is UNSUBSTITUTED (the model's Agent listing shows it literally)" "$out" 'UNSUBSTITUTED  agents/worker.md:3  ${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/CTX.md'
expect_lacks "an allowed-tools line, which Claude Code consumes, is not judged" "$out" "agents/worker.md:4"
printf -- '---\nname: worker\n---\nBody ${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/CTX.md here.\n' >"$SUBP/agents/worker.md"
out="$("$E2E_PYTHON" -I -c '
import sys
sys.path.insert(0, sys.argv[1])
import plugin_out_of_repo as c
sk = sys.argv[2] + "/skills/probe/reference.md"
print("default", c._substituted_text("skills", sk), c._substituted_text("resources", sys.argv[2] + "/resources/agent-shared/RELAY.md"))
c.READ_FILES_SUBSTITUTED = True
print("flipped", c._substituted_text("skills", sk), c._substituted_text("resources", sys.argv[2] + "/resources/agent-shared/RELAY.md"))
' "$HERE/lib" "$SUBP")"
expect_has "READ_FILES_SUBSTITUTED governs a skill's reference file as it does a resource (False)" "$out" "default False False"
expect_has "and flipping it changes both (the pin measured on each is the classifier's, not decoration)" "$out" "flipped True True"
echo "extraction scope"
EXT="$WORKDIR/ext"; mkplugin "$EXT"; EXT="$(realpath_of "$EXT")"
{
    printf -- '---\nname: worker\n---\n'
    printf 'Spaced span: `conexus/in-span.yaml --flag` here.\n'
    printf '```bash\n'
    printf 'cat conexus/in-fence.yaml | head\n'
    printf '```\n'
    printf 'After the fence: nothing.\n'
} >"$EXT/agents/worker.md"
out="$(run_static "$EXT")"
expect_has "a file inside a backtick span that holds spaces is extracted" "$out" "REPO-ONLY      agents/worker.md:4  conexus/in-span.yaml"
expect_has "a file inside a fenced code block is extracted" "$out" "REPO-ONLY      agents/worker.md:6  conexus/in-fence.yaml"

echo "near paths and repo-only roots"
NEAR="$WORKDIR/near"; mkplugin "$NEAR"; NEAR="$(realpath_of "$NEAR")"
mkdir -p "$NEAR/skills/probe/scripts" "$NEAR/skills/probe/docs"; : >"$NEAR/skills/probe/scripts/run.py"; : >"$NEAR/skills/probe/docs/n.md"
printf -- '---\nname: probe\n---\nRun `scripts/run.py` and read [n](docs/n.md).\n' >"$NEAR/skills/probe/SKILL.md"
out="$(run_static "$NEAR")"; rc=$?
expect_eq "a skill's own scripts/run.py and docs/n.md open from its directory: not REPO-ONLY, though the checkout has both" 0 "$rc"
expect_eq "both are RELATIVE-NEAR" 2 "$(cnt "$NEAR" skills RELATIVE-NEAR)"
printf -- '---\nname: worker\n---\nRun `scripts/x.sh` and `scripts/deploy.sh` and `docs/n.md`, `src/nexus/other.py`.\n' >"$NEAR/agents/worker.md"
out="$(run_static "$NEAR")"; rc=$?
expect_eq "generic roots: a path the nexus checkout holds fails" 1 "$rc"
expect_has "scripts/x.sh (in the checkout) is REPO-ONLY" "$out" "REPO-ONLY      agents/worker.md:4  scripts/x.sh"
expect_has "docs/n.md (in the checkout) is REPO-ONLY" "$out" "REPO-ONLY      agents/worker.md:4  docs/n.md"
expect_has "src/nexus/... is REPO-ONLY by name" "$out" "REPO-ONLY      agents/worker.md:4  src/nexus/other.py"
expect_lacks "scripts/deploy.sh (a user's own file, absent from the checkout) is PROJECT, not a failure" "$out" "scripts/deploy.sh"
UG="$WORKDIR/ug"; mkdir -p "$UG"; mkplugin "$UG/plug"; : >"$UG/outside.md"
printf -- '---\nname: probe\n---\nSee [x](../../../outside.md).\n' >"$UG/plug/skills/probe/SKILL.md"
out="$(run_static "$UG/plug")"; rc=$?
expect_eq "a relative target that opens only OUTSIDE the plugin is not RELATIVE-NEAR" 1 "$rc"
expect_has "it is RELATIVE-DEAD" "$out" "RELATIVE-DEAD  skills/probe/SKILL.md:4  ../../../outside.md"

# ---- sweep judgement on hand-written event streams --------------------------
mkevents() {  # <events-dir> <surface> <name> <result-text> <read-path|-> <read-ok:true|false>
    local f="$1/$2__$3.events.jsonl"
    "$E2E_PYTHON" -I - "$f" "$4" "$5" "$6" <<'PY'
import json, sys
path, text, read_path, ok = sys.argv[1:5]
import os
ev = []
if not os.environ.get("NOINIT"):
    name = os.environ.get("INIT_PLUGIN", "fx")
    init = {"type": "system", "subtype": "init",
            "plugins": [{"name": name, "path": "/p", "source": name + "@inline"},
                        {"name": "cc-plugin-x", "path": "builtin", "source": "cc-plugin-x@builtin"}],
            "mcp_servers": [], "tools": ["Task", "Read", "Skill"], "permissionMode": "default",
            "slash_commands": [name + ":a", "debug"], "skills": [name + ":b"], "agents": ["Explore"]}
    tweak = os.environ.get("INIT_TWEAK", "")
    if tweak == "plugin":
        init["plugins"].append({"name": "conexus", "path": "/h/cache", "source": "conexus@nexus-plugins"})
    elif tweak == "mcp":
        init["mcp_servers"] = [{"name": "serena"}]
    elif tweak == "tools":
        init["tools"] = ["Task", "Read", "Skill", "Bash"]
    elif tweak == "perm":
        init["permissionMode"] = "acceptEdits"
    elif tweak == "foreign":
        init["skills"].append("conexus:orchestration")
    ev.append(init)
if os.environ.get("BASEDIR_TEXT"):
    ev.append({"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": "Base directory for this skill: " + os.environ["BASEDIR_TEXT"]}]}})
if read_path != "-":
    ev.append({"type": "assistant", "parent_tool_use_id": "t0", "message": {"content": [
        {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": read_path}}]}})
    ev.append({"type": "user", "parent_tool_use_id": "t0", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "r1", "is_error": ok != "true", "content": "x"}]}})
ev.append({"type": "result", "subtype": "success", "is_error": False, "result": text})
open(path, "w").write("\n".join(json.dumps(e) for e in ev) + "\n")
PY
}
LIVE="$WORKDIR/live-plugin"; mkplugin "$LIVE"; LIVE="$(realpath_of "$LIVE")"
printf -- '---\nname: worker\n---\nUse [relay](${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/RELAY.md).\nAlso [rel](../resources/agent-shared/RELAY.md) and [ck](%s/scripts/x.sh).\n' "$CK" >"$LIVE/agents/worker.md"
rm -rf "$LIVE/skills/probe"; mkdir -p "$LIVE/skills/probe"; printf -- '---\nname: probe\n---\nNo references here.\n' >"$LIVE/skills/probe/SKILL.md"
T=$'\t'
RELAY="$LIVE/resources/agent-shared/RELAY.md"
run_live() { "$E2E_PYTHON" -I "$MOD" live --plugin "$LIVE" --repo-root "$CK" --events-dir "$1" --partial "${@:2}" 2>&1; }

echo "sweep judgement"
D="$WORKDIR/ev-ok"; mkdir -p "$D"
mkevents "$D" agents worker "REF${T}$RELAY${T}$RELAY" "$RELAY" true
out="$(run_live "$D")"; rc=$?
expect_eq "an opened absolute reference is clean" 0 "$rc"
expect_has "and judged" "$out" "OPENED=1"

ln -s "$LIVE" "$WORKDIR/live-alias"
D="$WORKDIR/ev-alias"; mkdir -p "$D"
mkevents "$D" agents worker "nothing printed in the requested shape" "$WORKDIR/live-alias/resources/agent-shared/RELAY.md" true
out="$(run_live "$D")"; rc=$?
expect_eq "a Read through a symlinked alias of the plugin dir (macOS /var vs /private/var) still counts as opened" 0 "$rc"
expect_has "OPENED" "$out" "OPENED=1"

D="$WORKDIR/ev-noref"; mkdir -p "$D"
mkevents "$D" agents worker "nothing printed in the requested shape" "$RELAY" true
out="$(run_live "$D")"; rc=$?
expect_eq "the Read events decide: no REF line at all, but the file was opened: clean" 0 "$rc"
expect_has "and counted OPENED" "$out" "OPENED=1"

D="$WORKDIR/ev-pipe"; mkdir -p "$D"
mkevents "$D" agents worker "- REF | $RELAY | $RELAY" - true
out="$(run_live "$D")"
expect_has "a pipe-separated REF line with a bullet parses (read nothing: the claim is UNREAD, not a failure)" "$out" "UNREAD=1"
D="$WORKDIR/ev-ws"; mkdir -p "$D"
mkevents "$D" agents worker "REF   $RELAY   $RELAY" - true
out="$(run_live "$D")"
expect_has "a whitespace-separated REF line parses" "$out" "UNREAD=1"

D="$WORKDIR/ev-err"; mkdir -p "$D"
mkevents "$D" agents worker "REF${T}$RELAY${T}$RELAY" "$RELAY" false
out="$(run_live "$D")"; rc=$?
expect_eq "a Read error on a path that exists fails" 1 "$rc"
expect_has "as FAILED" "$out" "FAILED"

D="$WORKDIR/ev-unread"; mkdir -p "$D"
mkevents "$D" agents worker "REF${T}$RELAY${T}$RELAY" - true
out="$(run_live "$D")"; rc=$?
expect_eq "a claimed read with no Read call is UNPROBED, not red (model variance)" 0 "$rc"
expect_has "counted as UNREAD" "$out" "UNREAD=1"
expect_has "named UNPROBED" "$out" "UNPROBED       agents/worker"
expect_eq "and offered for a retry" "agents__worker" "$(run_live "$D" --retry-list)"

D="$WORKDIR/ev-misres"; mkdir -p "$D"
mkevents "$D" agents worker "REF${T}$RELAY${T}$LIVE/resources/agent-shared/CTX.md" "$LIVE/resources/agent-shared/CTX.md" true
out="$(run_live "$D")"; rc=$?
expect_eq "the model read another file than the one quoted: MISRESOLVED is noise, not red" 0 "$rc"
expect_has "counted as MISRESOLVED" "$out" "MISRESOLVED=1"

D="$WORKDIR/ev-literal"; mkdir -p "$D"
mkevents "$D" agents worker "REF${T}\${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/RELAY.md${T}\${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/RELAY.md" '${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/RELAY.md' false
out="$(run_live "$D")"; rc=$?
expect_eq "a Read handed the variable literally fails" 1 "$rc"
expect_has "as UNSUBSTITUTED" "$out" "UNSUBSTITUTED"

D="$WORKDIR/ev-quotedvar"; mkdir -p "$D"
mkevents "$D" agents worker "REF${T}\${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/RELAY.md${T}$RELAY" "$RELAY" true
out="$(run_live "$D")"; rc=$?
expect_eq "a variable the model only QUOTED literally (every Read was given a real path) is model text, not evidence: exit 0" 0 "$rc"
expect_has "counted QUOTE-ONLY" "$out" "QUOTE-ONLY=1"
expect_has "named UNPROBED" "$out" "UNPROBED       agents/worker"
expect_eq "and offered for a retry" "agents__worker" "$(run_live "$D" --retry-list)"
expect_lacks "never UNSUBSTITUTED" "$out" "UNSUBSTITUTED="

D="$WORKDIR/ev-rel"; mkdir -p "$D"
mkevents "$D" agents worker "REF${T}../resources/agent-shared/RELAY.md${T}$RELAY" "$RELAY" true
out="$(run_live "$D")"; rc=$?
expect_eq "a relative target the model only QUOTED from an agent (it Read an absolute guess) is not evidence: exit 0" 0 "$rc"
expect_lacks "and no RELATIVE-DEAD is reported from the quote" "$out" "RELATIVE-DEAD"
D="$WORKDIR/ev-rel2"; mkdir -p "$D"
mkevents "$D" agents worker "REF${T}../resources/agent-shared/RELAY.md${T}../resources/agent-shared/RELAY.md" "../resources/agent-shared/RELAY.md" false
out="$(run_live "$D")"; rc=$?
expect_eq "the same quote with a Read that WAS given the relative path is corroborated: fails" 1 "$rc"
expect_has "as RELATIVE-DEAD" "$out" "RELATIVE-DEAD"

D="$WORKDIR/ev-extra"; mkdir -p "$D"
mkevents "$D" agents worker "REF${T}$RELAY${T}$RELAY
REF${T}scripts/not-in-the-plugin.sh${T}/nowhere/scripts/not-in-the-plugin.sh" "$RELAY" true
out="$(run_live "$D")"; rc=$?
expect_eq "a quoted target the plugin does not contain does not fail the run" 0 "$rc"

D="$WORKDIR/ev-none"; mkdir -p "$D"
mkevents "$D" agents worker "nothing to report" - true
out="$(run_live "$D")"; rc=$?
expect_eq "a run that quotes and reads nothing where references are expected is UNPROBED, exit 0" 0 "$rc"
expect_has "counted NO-REFS" "$out" "NO-REFS=1"
expect_has "named UNPROBED" "$out" "UNPROBED"

D="$WORKDIR/ev-checkout"; mkdir -p "$D"
mkevents "$D" agents worker "REF${T}$CK/scripts/x.sh${T}$CK/scripts/x.sh" "$CK/scripts/x.sh" true
out="$(run_live "$D")"; rc=$?
expect_eq "a path that opens in a nexus checkout outside the plugin fails" 1 "$rc"
expect_has "as INTO-CHECKOUT" "$out" "INTO-CHECKOUT"

SK="$WORKDIR/sk-plugin"; mkplugin "$SK"; SK="$(realpath_of "$SK")"
D="$WORKDIR/ev-skill"; mkdir -p "$D"
BASEDIR_TEXT="$SK/skills/probe" mkevents "$D" skills probe "REF${T}../../resources/agent-shared/RELAY.md${T}$SK/resources/agent-shared/RELAY.md" "$SK/resources/agent-shared/RELAY.md" true
out="$("$E2E_PYTHON" -I "$MOD" live --plugin "$SK" --events-dir "$D" --partial 2>&1)"; rc=$?
expect_eq "a skill relative target resolved against the base directory Claude Code announced is clean" 0 "$rc"
D="$WORKDIR/ev-skill-bad"; mkdir -p "$D"
mkevents "$D" skills probe "REF${T}../../resources/agent-shared/RELAY.md${T}$SK/resources/agent-shared/RELAY.md" "$SK/resources/agent-shared/RELAY.md" true
out="$("$E2E_PYTHON" -I "$MOD" live --plugin "$SK" --events-dir "$D" --partial 2>&1)"; rc=$?
expect_eq "the same target with no announced base directory fails" 1 "$rc"
expect_has "as RELATIVE-DEAD" "$out" "RELATIVE-DEAD"

for tweak in plugin mcp tools perm foreign; do
    D="$WORKDIR/ev-init-$tweak"; mkdir -p "$D"
    INIT_TWEAK="$tweak" mkevents "$D" agents worker "REF${T}$RELAY${T}$RELAY" "$RELAY" true
    out="$(run_live "$D")"; rc=$?
    expect_eq "a sweep run whose init event shows $tweak is a definite red (a Read that opened does not save it)" 1 "$rc"
    expect_has "named ISOLATION ($tweak)" "$out" "ISOLATION"
done
D="$WORKDIR/ev-init-none"; mkdir -p "$D"
NOINIT=1 mkevents "$D" agents worker "REF${T}$RELAY${T}$RELAY" "$RELAY" true
out="$(run_live "$D")"; rc=$?
expect_eq "a sweep run with no init event cannot show its isolation: red" 1 "$rc"
expect_has "saying so" "$out" "no system init event"
D="$WORKDIR/ev-init-ok"; mkdir -p "$D"
INIT_PLUGIN=fx mkevents "$D" agents worker "REF${T}$RELAY${T}$RELAY" "$RELAY" true
out="$(run_live "$D")"
expect_lacks "the plugin under test and Claude Code's own builtin plugins are not isolation failures" "$out" "ISOLATION"

D="$WORKDIR/ev-noresult"; mkdir -p "$D"
printf '{"type": "system"}\n' >"$D/agents__worker.events.jsonl"
out="$(run_live "$D")"; rc=$?
expect_eq "no run with a result is UNVERIFIED, exit 2" 2 "$rc"

# a directory is not a Read target: an agent whose only reference is a directory expects nothing
DIRL="$WORKDIR/dirs-live"; mkplugin "$DIRL"; DIRL="$(realpath_of "$DIRL")"
mkdir -p "$DIRL/scripts"
printf -- '---\nname: worker\n---\nThe scripts are in ${CLAUDE_PLUGIN_ROOT}/scripts/ only.\n' >"$DIRL/agents/worker.md"
rm -rf "$DIRL/skills/probe"; mkdir -p "$DIRL/skills/probe"; printf -- '---\nname: probe\n---\nNo references here.\n' >"$DIRL/skills/probe/SKILL.md"
D="$WORKDIR/ev-dir"; mkdir -p "$D"
mkevents "$D" agents worker "nothing to report" - true
out="$("$E2E_PYTHON" -I "$MOD" live --plugin "$DIRL" --events-dir "$D" --partial 2>&1)"; rc=$?
expect_eq "an agent whose only reference is a directory expects no Read" 0 "$rc"
expect_lacks "and is not NO-REFS" "$out" "NO-REFS"

# a complete (non-partial) sweep: the manual skill is static-only; the coverage floor applies
DMI="$WORKDIR/dmi"; mkplugin "$DMI"; DMI="$(realpath_of "$DMI")"
mkdir -p "$DMI/skills/manual"
printf -- '---\nname: manual\ndisable-model-invocation: true\n---\nSee [relay](${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/RELAY.md).\n' >"$DMI/skills/manual/SKILL.md"
out="$("$E2E_PYTHON" -I "$MOD" targets --plugin "$DMI" 2>&1)"
expect_lacks "a disable-model-invocation skill is not a live target" "$out" "manual"
expect_has "the loadable skill still is" "$out" "probe"
printf -- '---\nname: probe\n---\nNo references here.\n' >"$DMI/skills/probe/SKILL.md"
printf -- '---\nname: worker\n---\nUse [relay](${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/RELAY.md).\n' >"$DMI/agents/worker.md"
DMIR="$DMI/resources/agent-shared/RELAY.md"
D="$WORKDIR/ev-dmi"; mkdir -p "$D"
mkevents "$D" agents worker "REF${T}$DMIR${T}$DMIR" "$DMIR" true
mkevents "$D" skills probe "nothing to report" - true
out="$("$E2E_PYTHON" -I "$MOD" live --plugin "$DMI" --events-dir "$D" 2>&1)"; rc=$?
expect_eq "a complete sweep (every loadable target) is clean without a run for the manual skill" 0 "$rc"
expect_has "and the manual skill is named as static-only" "$out" "(not live-testable) skills/manual"
D="$WORKDIR/ev-floor"; mkdir -p "$D"
mkevents "$D" agents worker "nothing to report" - true
mkevents "$D" skills probe "nothing to report" - true
out="$("$E2E_PYTHON" -I "$MOD" live --plugin "$DMI" --events-dir "$D" 2>&1)"; rc=$?
expect_eq "a complete sweep with no OPENED reference for a mechanism the plugin has is UNVERIFIED, exit 2" 2 "$rc"
expect_has "naming the floor" "$out" "coverage floor not met"

# ---- the canary judgement ----------------------------------------------------
echo "canary judgement"
STUBW="$WORKDIR/claude-stub"
printf '#!/usr/bin/env bash\nexec "%s" -I "%s" "$@"\n' "$E2E_PYTHON" "$STUBPY" >"$STUBW"; chmod +x "$STUBW"
export STUB_STATE="$WORKDIR/stub-state"; mkdir -p "$STUB_STATE"
CW="$WORKDIR/canary"; mkdir -p "$CW"
"$E2E_PYTHON" -I "$CAN" build --dest "$CW/plugin" --manifest "$CW/manifest.json"
"$E2E_PYTHON" -I "$CAN" prompts >"$CW/prompts.tsv"
expect_eq "five sessions" 5 "$(wc -l <"$CW/prompts.tsv" | tr -d ' ')"
expect_has "the fixture has a skill reference file (a non-SKILL.md file the model Reads)" "$(cat "$CW/plugin/skills/canary-skill-var/reference.md")" 'BRACED: ${CLAUDE_PLUGIN_ROOT}'
expect_has "the agent fixture has a link inside the resource it Reads" "$(cat "$CW/plugin/resources/relay.md")" "[sibling](./sibling.txt)"
expect_has "the agent-ub fixture has an unbraced line" "$(cat "$CW/plugin/agents/canary-agent-ub.md")" 'Read the file at $CLAUDE_PLUGIN_ROOT/nonces/agentub.txt'
expect_has "and a variable in its frontmatter description" "$(head -4 "$CW/plugin/agents/canary-agent-ub.md")" 'description: Gate canary. Its marker file is ${CLAUDE_PLUGIN_ROOT}/nonces/fm.txt'
expect_has "the command fixture has an unbraced line" "$(cat "$CW/plugin/commands/canary-cmd.md")" 'Read the file $CLAUDE_PLUGIN_ROOT/nonces/cmdub.txt'
canary_events() {  # <dir> <mode> [VAR=value ...]
    local dir="$1" mode="$2" s p
    shift 2
    rm -rf "$STUB_STATE"; mkdir -p "$STUB_STATE" "$dir"
    while IFS=$'\t' read -r s p; do
        env STUB_LOG="$WORKDIR/canary-stub.log" STUB_MODE="$mode" "$@" "$STUBW" -p --plugin-dir "$CW/plugin" "$p" >"$dir/canary__$s.events.jsonl"
    done <"$CW/prompts.tsv"
}
judge() {  # <events dir> [extra args]
    "$E2E_PYTHON" -I "$CAN" judge --plugin "$CW/plugin" --manifest "$CW/manifest.json" --events-dir "$1" "${@:2}" 2>&1
}
canary_events "$WORKDIR/cev-ok" ok
out="$(judge "$WORKDIR/cev-ok")"; rc=$?
expect_eq "a canary run that behaves is clean" 0 "$rc"
expect_has "it measured that a Read file is not substituted" "$out" "MEASURED read-file-substituted=no"
expect_has "it measured that a skill body is" "$out" "MEASURED skill-body-substituted=yes"
expect_has "it measured the unbraced variable in a skill" "$out" "MEASURED skill-unbraced-substituted=no"
expect_has "and in an agent" "$out" "MEASURED agent-unbraced-substituted=no"
expect_has "and in a command" "$out" "MEASURED command-unbraced-substituted=no"
expect_has "a skill's reference file" "$out" "MEASURED skill-reference-file-substituted=no"
expect_has "a link inside a Read resource" "$out" "MEASURED resource-relative-resolves=yes"
expect_has "the announcement of the base directory" "$out" "MEASURED skill-base-dir-announced=yes"
expect_has "a variable in an agent's frontmatter description (real Claude Code 2.1.318: literal)" "$out" "MEASURED agent-description-substituted=no"
expect_has "twelve measurements" "$out" "5 sessions, 12 measurements"
ln -s "$CW/plugin" "$WORKDIR/canary-alias"
rm -rf "$STUB_STATE"; mkdir -p "$STUB_STATE"
mkdir -p "$WORKDIR/cev-alias"
while IFS=$'\t' read -r s p; do
    env STUB_LOG="$WORKDIR/canary-stub.log" STUB_MODE=ok "$STUBW" -p --plugin-dir "$WORKDIR/canary-alias" "$p" >"$WORKDIR/cev-alias/canary__$s.events.jsonl"
done <"$CW/prompts.tsv"
out="$(judge "$WORKDIR/cev-alias")"; rc=$?
expect_eq "canary events whose paths go through a symlinked alias of the plugin dir are judged the same" 0 "$rc"
canary_events "$WORKDIR/cev-lit" literal
out="$(judge "$WORKDIR/cev-lit")"; rc=$?
expect_eq "a variable that reaches the model literally breaks the canary (exit 1)" 1 "$rc"
expect_has "naming the mechanism" "$out" "BROKEN"
expect_has "and the contradicted pin" "$out" "CONTRADICTS the classifier's pin"
canary_events "$WORKDIR/cev-sub" ok STUB_READ_SUBSTITUTED=1
out="$(judge "$WORKDIR/cev-sub")"; rc=$?
expect_eq "a Claude Code that starts substituting Read files contradicts the pin: exit 1" 1 "$rc"
expect_has "naming the resource" "$out" "read-file-substituted=yes (CONTRADICTS"
expect_has "and the skill's reference file (the classifier applies the pin to both)" "$out" "skill-reference-file-substituted=yes (CONTRADICTS"
canary_events "$WORKDIR/cev-unb" ok STUB_UNBRACED_SUBSTITUTED=1
out="$(judge "$WORKDIR/cev-unb")"; rc=$?
expect_eq "a Claude Code that starts substituting the unbraced variable contradicts the pin: exit 1" 1 "$rc"
expect_has "in a skill" "$out" "skill-unbraced-substituted=yes (CONTRADICTS"
expect_has "in an agent" "$out" "agent-unbraced-substituted=yes (CONTRADICTS"
expect_has "in a command" "$out" "command-unbraced-substituted=yes (CONTRADICTS"
canary_events "$WORKDIR/cev-els" ok STUB_REL_ELSEWHERE=1
out="$(judge "$WORKDIR/cev-els")"; rc=$?
expect_eq "a relative Read that goes somewhere else than the announced base directory is BROKEN, not a retry: exit 1" 1 "$rc"
expect_has "the skill's relative link is recorded as not resolving" "$out" "skill-relative-resolves=no (CONTRADICTS"
expect_has "and so is the link inside a Read resource" "$out" "resource-relative-resolves=no (CONTRADICTS"
expect_eq "and nothing is offered for a retry" "" "$(judge "$WORKDIR/cev-els" --retry-list)"
canary_events "$WORKDIR/cev-noann" ok STUB_NO_ANNOUNCE=1
out="$(judge "$WORKDIR/cev-noann")"; rc=$?
expect_eq "a skill delivered without its base-directory announcement is red, not a noise retry: exit 1" 1 "$rc"
expect_has "the announcement is a measured pin" "$out" "skill-base-dir-announced=no (CONTRADICTS"
expect_eq "and nothing is offered for a retry" "" "$(judge "$WORKDIR/cev-noann" --retry-list)"
canary_events "$WORKDIR/cev-fm" ok STUB_FM_SUBSTITUTED=1
out="$(judge "$WORKDIR/cev-fm")"; rc=$?
expect_eq "a Claude Code that starts substituting an agent's description contradicts the pin: exit 1" 1 "$rc"
expect_has "naming it" "$out" "agent-description-substituted=yes (CONTRADICTS"
canary_events "$WORKDIR/cev-noise" noise-once
out="$(judge "$WORKDIR/cev-noise")"; rc=$?
expect_eq "sessions that produced no result are UNVERIFIED, never a pass (exit 2)" 2 "$rc"
expect_eq "and all five are offered for a retry" 5 "$(judge "$WORKDIR/cev-noise" --retry-list | wc -l | tr -d ' ')"
# the flake that would have turned a release red with no retry: a subagent that skips step 2
canary_events "$WORKDIR/cev-skip" skip-relay
out="$(judge "$WORKDIR/cev-skip")"; rc=$?
expect_eq "an agent session that never Read relay.md is not ok: UNVERIFIED, exit 2" 2 "$rc"
expect_has "and says why" "$out" "relay.md was not Read"
expect_lacks "the agent session is not reported OPENED" "$(printf '%s\n' "$out" | grep ' agent ')" "OPENED"
expect_eq "and it is offered for a retry (the flake is retried, not final)" "agent" "$(judge "$WORKDIR/cev-skip" --retry-list)"
# an init event that shows a run which was not isolated
for tweak in plugin mcp tools perm none; do
    canary_events "$WORKDIR/cev-init-$tweak" ok STUB_INIT="$tweak"
    out="$(judge "$WORKDIR/cev-init-$tweak")"; rc=$?
    expect_eq "an init event showing '$tweak' is a definite red (exit 1)" 1 "$rc"
    expect_has "naming the isolation ($tweak)" "$out" "isolation: "
    expect_eq "and it is not retried ($tweak)" "" "$(judge "$WORKDIR/cev-init-$tweak" --retry-list)"
done
out="$(judge "$WORKDIR/cev-init-plugin")"
expect_has "the extra plugin is named" "$out" "plugin conexus (conexus@nexus-plugins) loaded beside the one under test"
out="$(judge "$WORKDIR/cev-init-tools")"
expect_has "the extra tool is named" "$out" "extra: ['Bash']"
IS="$WORKDIR/isolated"
cp -R "$CLEAN" "$WORKDIR/iso-src"; mkdir -p "$WORKDIR/iso-src/hooks"
printf '{"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "nx"}]}]}}\n' >"$WORKDIR/iso-src/hooks/hooks.json"
printf -- '---\nname: worker\nmemory: user\ntools: Bash, Write, Edit\ndisallowedTools: Foo\nmcpServers:\n  - serena\n  - other\nhooks:\n  Stop: []\nmodel: sonnet\n---\nbody\n' >"$WORKDIR/iso-src/agents/worker.md"
out="$("$E2E_PYTHON" -I "$CAN" isolate --src "$WORKDIR/iso-src" --dest "$IS" 2>&1)"; rc=$?
expect_eq "isolate succeeds" 0 "$rc"
expect_eq "the copy's hooks are emptied" '{"hooks": {}}' "$(cat "$IS/hooks/hooks.json")"
expect_lacks "the copy's agents lose memory:" "$(cat "$IS/agents/worker.md")" "memory:"
expect_has "and keep their body" "$(cat "$IS/agents/worker.md")" "body"
expect_has "and keep their other keys" "$(cat "$IS/agents/worker.md")" "model: sonnet"
expect_eq "the copy's agents carry exactly one tools: line, Read (a subagent's tools come from its frontmatter, not --allowedTools)" "tools: Read" "$(grep -E '^tools:' "$IS/agents/worker.md")"
expect_lacks "no Bash, Write or Edit" "$(cat "$IS/agents/worker.md")" "Bash"
expect_lacks "no disallowedTools" "$(cat "$IS/agents/worker.md")" "disallowedTools"
expect_lacks "no mcpServers, and none of its list items" "$(cat "$IS/agents/worker.md")" "serena"
expect_lacks "no hooks" "$(cat "$IS/agents/worker.md")" "Stop"
printf -- '---\nname: bare\n---\nbody\n' >"$WORKDIR/iso-src/agents/bare.md"
rm -rf "$WORKDIR/iso2"; "$E2E_PYTHON" -I "$CAN" isolate --src "$WORKDIR/iso-src" --dest "$WORKDIR/iso2" >/dev/null
expect_eq "an agent with no tools: line gets Read too" "tools: Read" "$(grep -E '^tools:' "$WORKDIR/iso2/agents/bare.md")"
expect_has "the original is untouched" "$(cat "$WORKDIR/iso-src/hooks/hooks.json")" "SessionStart"
expect_has "and its agent keeps memory" "$(cat "$WORKDIR/iso-src/agents/worker.md")" "memory: user"

# ---- the gate end to end, against the stub ------------------------------------
echo "the gate with a stub claude: isolation pinned"
GP="$WORKDIR/gate-plugin"; mkplugin "$GP" fx 1.2.3; GP="$(realpath_of "$GP")"
mkdir -p "$GP/hooks"; printf '{"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "nx upgrade --auto"}]}]}}\n' >"$GP/hooks/hooks.json"
printf -- '---\nname: worker\nmemory: user\n---\nUse [relay](${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/RELAY.md).\n' >"$GP/agents/worker.md"
printf -- '---\nname: probe\n---\nRead ${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/CTX.md and [ref](./reference.md).\n' >"$GP/skills/probe/SKILL.md"
export STUB_LOG="$WORKDIR/gate-stub.log" STUB_MODE=ok STUB_PIDS="$WORKDIR/stub.pids"
: >"$STUB_LOG"; rm -rf "$STUB_STATE"; mkdir -p "$STUB_STATE"
out="$(NX_GATE_CLAUDE="$STUBW" bash "$GATE" --plugin "$GP" --keep 2>&1)"; rc=$?
expect_eq "the default run (static + canary) passes against the stub" 0 "$rc"
expect_has "and says so" "$out" "PLUGIN OUT-OF-REPO GATE PASSED (static + canary)"
expect_eq "five canary sessions ran" 5 "$(grep -c '^ARGV' "$STUB_LOG")"
expect_eq "every run has --strict-mcp-config" 5 "$(grep '^ARGV' "$STUB_LOG" | grep -c -- $'\t--strict-mcp-config\t')"
expect_eq "every run has an EMPTY --setting-sources (no installed plugin or user setting loads)" 5 "$(grep '^ARGV' "$STUB_LOG" | grep -c -- $'--setting-sources\t\t--strict-mcp-config')"
expect_eq "every run has --no-session-persistence" 5 "$(grep '^ARGV' "$STUB_LOG" | grep -c -- $'\t--no-session-persistence\t')"
expect_eq "every run is pinned to Read,Agent,Task,Skill tools" 5 "$(grep '^ARGV' "$STUB_LOG" | grep -c -- $'--tools\tRead,Agent,Task,Skill\t')"
expect_eq "NX_UPGRADE_AUTO=0 in every run" 5 "$(grep -c $'^ENV\tNX_UPGRADE_AUTO=0$' "$STUB_LOG")"
expect_eq "NX_NO_TELEMETRY=1 in every run" 5 "$(grep -c $'^ENV\tNX_NO_TELEMETRY=1$' "$STUB_LOG")"
for v in NEXUS_CONFIG_DIR NX_TOOLS_DIR UV_TOOL_DIR UV_TOOL_BIN_DIR UV_CACHE_DIR NX_LOCKSTEP_MARKER NX_PLUGIN_REGISTRY NX_PLUGIN_MARKETPLACES; do
    n="$(grep -c "^ENV${T}$v=.*/plugin-out-of-repo-gate\..*/iso/" "$STUB_LOG")"
    expect_eq "$v points into the gate's scratch iso/ directory in every run" 5 "$n"
done
expect_eq "NX_SERVICE_URL is dead in every run" 5 "$(grep -c $'^ENV\tNX_SERVICE_URL=http://127.0.0.1:9$' "$STUB_LOG")"
expect_lacks "no env value points under the real HOME's tool or config dirs" "$(grep '^ENV' "$STUB_LOG" | grep -v '^ENV.HOME=')" "$HOME/.local"
expect_lacks "nor under ~/.config" "$(grep '^ENV' "$STUB_LOG" | grep -v '^ENV.HOME=')" "$HOME/.config"
expect_eq "HOME itself is the real one (the sign-in lives there)" 5 "$(grep -c "^ENV${T}HOME=$HOME\$" "$STUB_LOG")"
expect_eq "every run's cwd is the scratch project" 5 "$(grep -c "^CWD${T}.*/plugin-out-of-repo-gate\..*/project\$" "$STUB_LOG")"
expect_lacks "no env value points under the real HOME's uv cache" "$(grep '^ENV' "$STUB_LOG" | grep -v '^ENV.HOME=')" "$HOME/.cache"
expect_lacks "the canary runs against the fixture, never the plugin under test" "$(grep '^ARGV' "$STUB_LOG")" "$GP"
: >"$STUB_LOG"; rm -rf "$STUB_STATE"; mkdir -p "$STUB_STATE"
out="$(NX_GATE_CLAUDE="$STUBW" bash "$GATE" --plugin "$GP" --sweep --no-canary --keep 2>&1)"; rc=$?
expect_eq "a sweep against the stub passes" 0 "$rc"
expect_has "and says it is advisory" "$out" "sweep, advisory"
expect_eq "one run per agent and loadable skill (2)" 2 "$(grep -c '^ARGV' "$STUB_LOG")"
expect_lacks "the sweep runs a scratch COPY, not the plugin under test" "$(grep '^ARGV' "$STUB_LOG")" "--plugin-dir${T}$GP${T}"
expect_eq "the copy's hooks are emptied in every run" 2 "$(grep -c "^HOOKS${T}{\"hooks\": {}}\$" "$STUB_LOG")"
expect_eq "the copy's agents have no memory: in every run" 2 "$(grep -c "^AGENT${T}worker.md${T}memory=no${T}" "$STUB_LOG")"
expect_eq "and are narrowed to the Read tool in every run" 2 "$(grep -c "^AGENT${T}worker.md${T}memory=no${T}tools=Read\$" "$STUB_LOG")"
scratch="$(printf '%s\n' "$out" | sed -n 's/^  scratch: //p')"
scratch="${scratch%%$'\n'*}"
if [ -n "$scratch" ] && [ -f "$scratch/iso/lockstep-marker" ]; then
    expect_eq "the lockstep marker is pre-written with the plugin version" "1.2.3" "$(cat "$scratch/iso/lockstep-marker")"
    expect_has "the plugin registry is an empty one" "$(cat "$scratch/iso/installed_plugins.json")" '"plugins": {}'
    rm -rf "$scratch"
else
    bad "the gate printed no kept scratch directory (--keep)"
fi

: >"$STUB_LOG"; rm -rf "$STUB_STATE"; mkdir -p "$STUB_STATE"
out="$(NX_GATE_CLAUDE="$STUBW" bash "$GATE" --plugin "$GP" --only worker 2>&1)"; rc=$?
expect_eq "--only is a targeted investigation: it implies the sweep and passes" 0 "$rc"
expect_eq "and it makes exactly one claude run: no canary" 1 "$(grep -c '^ARGV' "$STUB_LOG")"

echo "retries and honest verdicts"
: >"$STUB_LOG"; rm -rf "$STUB_STATE"; mkdir -p "$STUB_STATE"
out="$(STUB_MODE=noise-once NX_GATE_CLAUDE="$STUBW" bash "$GATE" --plugin "$GP" --sweep 2>&1)"; rc=$?
expect_eq "noise on the first run of every canary session and sweep target is retried and passes" 0 "$rc"
expect_has "a canary retry was made" "$out" "canary retry 1"
expect_has "a sweep retry was made" "$out" "sweep retry 1"
: >"$STUB_LOG"; rm -rf "$STUB_STATE"; mkdir -p "$STUB_STATE"
out="$(STUB_MODE=noise-once NX_GATE_RETRIES=0 NX_GATE_CLAUDE="$STUBW" bash "$GATE" --plugin "$GP" 2>&1)"; rc=$?
expect_eq "with no retries the same noise leaves the canary UNVERIFIED, exit 2" 2 "$rc"
expect_has "and the closing verdict says so" "$(printf '%s\n' "$out" | tail -1)" "PLUGIN OUT-OF-REPO GATE UNVERIFIED"
: >"$STUB_LOG"; rm -rf "$STUB_STATE"; mkdir -p "$STUB_STATE"
out="$(STUB_MODE=skip-relay-once NX_GATE_CLAUDE="$STUBW" bash "$GATE" --plugin "$GP" 2>&1)"; rc=$?
expect_eq "an agent that skips the relay.md Read on its first run is retried and the gate passes (the ship-blocker flake)" 0 "$rc"
expect_eq "the retry names exactly the agent session" 1 "$(printf '%s\n' "$out" | grep -c '^  canary retry 1: agent$')"
expect_lacks "no second retry was needed" "$out" "canary retry 2"
: >"$STUB_LOG"; rm -rf "$STUB_STATE"; mkdir -p "$STUB_STATE"
out="$(STUB_MODE=skip-relay NX_GATE_CLAUDE="$STUBW" bash "$GATE" --plugin "$GP" 2>&1)"; rc=$?
expect_eq "an agent that never reads relay.md ends UNVERIFIED after its retries, exit 2" 2 "$rc"
expect_eq "it was retried twice (3 runs of the agent prompt, 5 + 2 = 7 canary runs)" 7 "$(grep -c '^ARGV' "$STUB_LOG")"
: >"$STUB_LOG"; rm -rf "$STUB_STATE"; mkdir -p "$STUB_STATE"
out="$(STUB_MODE=literal NX_GATE_CLAUDE="$STUBW" bash "$GATE" --plugin "$GP" 2>&1)"; rc=$?
expect_eq "a Claude Code that hands the variable over literally fails the gate, exit 1" 1 "$rc"
expect_has "FAILED" "$(printf '%s\n' "$out" | tail -1)" "PLUGIN OUT-OF-REPO GATE FAILED"

# backoff between retries
: >"$STUB_LOG"; rm -rf "$STUB_STATE"; mkdir -p "$STUB_STATE"
FAKEBIN="$WORKDIR/fakebin"; mkdir -p "$FAKEBIN"; : >"$WORKDIR/sleep.log"
printf '#!/bin/sh\necho "$1" >>"%s"\n[ "$1" = 7 ] && exit 0\nexec /bin/sleep "$@"\n' "$WORKDIR/sleep.log" >"$FAKEBIN/sleep"; chmod +x "$FAKEBIN/sleep"
out="$(PATH="$FAKEBIN:$PATH" STUB_MODE=noise-once NX_GATE_RETRY_SLEEP=7 NX_GATE_CLAUDE="$STUBW" bash "$GATE" --plugin "$GP" 2>&1)"; rc=$?
expect_eq "a retried canary still passes with the backoff on" 0 "$rc"
expect_has "the pause is announced" "$out" "(pausing 7s before the retry)"
expect_eq "and the gate really called sleep 7 (attempt 1 x NX_GATE_RETRY_SLEEP), once" 1 "$(grep -c '^7$' "$WORKDIR/sleep.log")"

# the init event, through the gate
for tweak in plugin mcp tools perm none; do
    : >"$STUB_LOG"; rm -rf "$STUB_STATE"; mkdir -p "$STUB_STATE"
    out="$(STUB_INIT="$tweak" NX_GATE_CLAUDE="$STUBW" bash "$GATE" --plugin "$GP" 2>&1)"; rc=$?
    expect_eq "a canary whose init event shows '$tweak' fails the gate, exit 1" 1 "$rc"
    expect_has "and says the run was not isolated ($tweak)" "$out" "isolation: "
done
: >"$STUB_LOG"; rm -rf "$STUB_STATE"; mkdir -p "$STUB_STATE"
out="$(STUB_INIT=mcp NX_GATE_CLAUDE="$STUBW" bash "$GATE" --plugin "$GP" --sweep --no-canary 2>&1)"; rc=$?
expect_eq "a sweep run whose init event shows an MCP server fails the sweep, exit 1" 1 "$rc"
expect_has "as ISOLATION" "$out" "ISOLATION"

echo "the project directory Claude Code leaves under its config dir"
CFG="$WORKDIR/claude-config"; PROJ="$CFG/projects"; mkdir -p "$PROJ"
DECOY1="$PROJ/-Users-someone-code-my-project"; mkdir -p "$DECOY1/memory"; : >"$DECOY1/keep.jsonl"
DECOY2="$PROJ/-tmp-other-plugin-out-of-repo-gate-ABCDEF-project"; mkdir -p "$DECOY2/memory"            # a gate-shaped name that is not this run's
DECOY3="$PROJ/-tmp-x-plugin-out-of-repo-gate-ZZZZZZ-project"; mkdir -p "$DECOY3/s/subagents"; printf 'x' >"$DECOY3/s/transcript.jsonl"; printf '{}' >"$DECOY3/s/subagents/agent-q.meta.json"   # gate-shaped, holds a transcript
: >"$STUB_LOG"; rm -rf "$STUB_STATE"; mkdir -p "$STUB_STATE"
out="$(CLAUDE_CONFIG_DIR="$CFG" STUB_RESIDUE=1 NX_GATE_CLAUDE="$STUBW" bash "$GATE" --plugin "$GP" 2>&1)"; rc=$?
expect_eq "the gate passes with a stub that leaves the residue" 0 "$rc"
expect_eq "it made residue five times (one per run, one directory: the scratch project)" 5 "$(grep -c '^RESIDUE' "$STUB_LOG")"
left=0; while read -r _ d; do [ -e "$d" ] && left=$((left + 1)); done < <(grep '^RESIDUE' "$STUB_LOG")
expect_eq "the gate removed its own project directory (memory/ and the subagent meta file included)" 0 "$left"
chk "another project's directory is untouched" [ -f "$DECOY1/keep.jsonl" ]
chk "a gate-shaped directory that is not this run's is untouched" [ -d "$DECOY2" ]
chk "a gate-shaped directory holding a transcript is untouched" [ -f "$DECOY3/s/transcript.jsonl" ]
out="$(CLAUDE_CONFIG_DIR="$CFG" bash "$GATE" --clean-residue 2>&1)"; rc=$?
expect_eq "--clean-residue exits 0" 0 "$rc"
expect_has "it lists the gate-shaped directories first" "$out" "gate project directories under $PROJ: 2"
expect_has "naming them" "$out" "-tmp-other-plugin-out-of-repo-gate-ABCDEF-project"
expect_has "it removed the empty one and left the one holding a transcript" "$out" "removed 1, left in place 1"
chk "the empty gate-shaped directory is gone" [ ! -e "$DECOY2" ]
chk "the transcript survives --clean-residue" [ -f "$DECOY3/s/transcript.jsonl" ]
chk "and so does the subagent meta file beside it (a directory holding anything else is left whole)" [ -f "$DECOY3/s/subagents/agent-q.meta.json" ]
chk "and another project's file survives" [ -f "$DECOY1/keep.jsonl" ]

echo "the watchdog and an interrupt kill the process tree"
alive() { kill -0 "$1" 2>/dev/null; }
: >"$STUB_PIDS"; : >"$STUB_LOG"
out="$(STUB_MODE=hang NX_GATE_RETRIES=0 NX_GATE_CLAUDE="$STUBW" bash "$GATE" --plugin "$GP" --sweep --no-canary --only worker --timeout 2 2>&1)"; rc=$?
expect_eq "a run that hangs is killed at the timeout and the sweep is UNVERIFIED (exit 2)" 2 "$rc"
sleep 1
left=0
while read -r a b; do alive "$a" && left=$((left + 1)); alive "$b" && left=$((left + 1)); done <"$STUB_PIDS"
expect_eq "neither the stub nor the child it spawned survives the watchdog" 0 "$left"
if [ -s "$STUB_PIDS" ]; then ok "the stub did start (pids recorded)"; else bad "the stub never started"; fi
: >"$STUB_PIDS"
STUB_MODE=hang NX_GATE_RETRIES=0 NX_GATE_CLAUDE="$STUBW" bash "$GATE" --plugin "$GP" --sweep --no-canary --only worker --timeout 120 >"$WORKDIR/int.out" 2>&1 &
GPID=$!
for _ in $(seq 1 100); do [ -s "$STUB_PIDS" ] && break; sleep 0.1; done
if [ -s "$STUB_PIDS" ]; then ok "the hung run started"; else bad "the hung run never started"; fi
kill -TERM "$GPID" 2>/dev/null
wait "$GPID" 2>/dev/null
sleep 1
left=0
while read -r a b; do alive "$a" && { left=$((left + 1)); kill -9 "$a" 2>/dev/null; }; alive "$b" && { left=$((left + 1)); kill -9 "$b" 2>/dev/null; }; done <"$STUB_PIDS"
expect_eq "a TERM to the gate leaves no claude process or child behind" 0 "$left"

echo "an applicability crash is not NOT-APPLICABLE"
PYW="$WORKDIR/py-crash"
printf '#!/usr/bin/env bash\nfor a in "$@"; do [ "$a" = applicability ] && exit 3; done\nexec "%s" "$@"\n' "$E2E_PYTHON" >"$PYW"; chmod +x "$PYW"
out="$(NX_E2E_PYTHON="$PYW" bash "$GATE" --plugin "$CLEAN" --static-only 2>&1)"; rc=$?
expect_eq "an empty applicability fails the gate" 1 "$rc"
expect_lacks "and is never read as NOT-APPLICABLE" "$out" "GATE NOT-APPLICABLE"

# ---- claude absent ----------------------------------------------------------
echo "claude absent"
out="$(NX_GATE_CLAUDE="$WORKDIR/no-such-claude" bash "$GATE" --plugin "$CLEAN" 2>&1)"; rc=$?
expect_eq "claude absent exits 2" 2 "$rc"
expect_has "and says UNVERIFIED" "$out" "UNVERIFIED"
expect_lacks "and never says PASSED" "$out" "PASSED"
out="$(bash "$GATE" --plugin "$CLEAN" --static-only 2>&1)"; rc=$?
expect_eq "--static-only needs no claude and passes a clean plugin" 0 "$rc"
expect_has "and says it was the static pass only" "$out" "static pass only"
out="$(bash "$GATE" --plugin "$BAD" --static-only 2>&1)"; rc=$?
expect_eq "--static-only fails a bad plugin" 1 "$rc"
out="$(bash "$GATE" --plugin "$CLEAN" --static-only --sweep 2>&1)"; rc=$?
expect_eq "--static-only does not combine with --sweep" 64 "$rc"

# ===========================================================================
# directories, NOT-APPLICABLE, hook markdown, the allowlist, the multi modes
# ===========================================================================
echo "directory references"
DIRP="$WORKDIR/dirs"; mkplugin "$DIRP"; DIRP="$(realpath_of "$DIRP")"
mkdir -p "$DIRP/skills/probe/scripts"; : >"$DIRP/skills/probe/scripts/run.py"
printf -- '---\nname: probe\nallowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/probe/scripts/*)\n---\nSee [ref](./reference.md), the [dir](./scripts/) and ${CLAUDE_PLUGIN_ROOT}/skills/probe/scripts/ and ${CLAUDE_PLUGIN_ROOT}/skills/probe/scripts/run.py.\n' >"$DIRP/skills/probe/SKILL.md"
out="$(run_static "$DIRP")"; rc=$?
expect_eq "a reference to a directory that exists is not a failure" 0 "$rc"
expect_eq "the absolute directory references are RESOLVES (frontmatter dir, body dir, body file)" 3 "$(cnt "$DIRP" skills RESOLVES)"
expect_eq "the relative directory link is RELATIVE-NEAR (with reference.md)" 2 "$(cnt "$DIRP" skills RELATIVE-NEAR)"
printf -- '---\nname: probe\n---\nSee ${CLAUDE_PLUGIN_ROOT}/skills/probe/no-such-dir/ and [d](./nodir/).\n' >"$DIRP/skills/probe/SKILL.md"
out="$(run_static "$DIRP")"; rc=$?
expect_eq "a directory that does not exist still fails" 1 "$rc"
expect_has "as MISSING with file:line" "$out" 'MISSING        skills/probe/SKILL.md:4  ${CLAUDE_PLUGIN_ROOT}/skills/probe/no-such-dir/'
expect_has "and the relative one as RELATIVE-DEAD" "$out" "RELATIVE-DEAD  skills/probe/SKILL.md:4  ./nodir/"

echo "plugins with nothing to census"
HOOKP="$WORKDIR/hookonly"; mkhooks "$HOOKP" hk; HOOKP="$(realpath_of "$HOOKP")"
out="$(run_static "$HOOKP")"; rc=$?
expect_eq "a hook-only plugin passes the static pass" 0 "$rc"
expect_has "and its census is NOT-APPLICABLE" "$out" "census NOT-APPLICABLE"
expect_lacks "not a census of nothing" "$out" "census of nothing"
expect_has "the hook markdown file is counted" "$out" "1 hook markdown file(s)"
out="$(NX_GATE_CLAUDE="$WORKDIR/no-such-claude" bash "$GATE" --plugin "$HOOKP" 2>&1)"; rc=$?
expect_eq "the gate passes a hook-only plugin without claude" 0 "$rc"
expect_has "with the verdict NOT-APPLICABLE" "$out" "PLUGIN OUT-OF-REPO GATE NOT-APPLICABLE"
expect_lacks "and never UNVERIFIED" "$out" "UNVERIFIED"
EMPTYP="$WORKDIR/nomd"; mkdir -p "$EMPTYP/.claude-plugin"; printf '{"name": "nomd"}\n' >"$EMPTYP/.claude-plugin/plugin.json"
out="$(bash "$GATE" --plugin "$EMPTYP" --static-only 2>&1)"; rc=$?
expect_eq "a plugin with no markdown at all is NOT-APPLICABLE, exit 0" 0 "$rc"
expect_has "and the verdict says so" "$out" "NOT-APPLICABLE"
SKONLY="$WORKDIR/skills-only"; mkdir -p "$SKONLY/.claude-plugin" "$SKONLY/skills/s"
printf '{"name": "so"}\n' >"$SKONLY/.claude-plugin/plugin.json"; printf -- '---\nname: s\n---\nno links\n' >"$SKONLY/skills/s/SKILL.md"
out="$(run_static "$SKONLY")"; rc=$?
expect_eq "a plugin with a skill and no reference still fails" 1 "$rc"
expect_has "as a census of nothing" "$out" "a census of nothing proves nothing"
RESONLY="$WORKDIR/res-only"; mkdir -p "$RESONLY/.claude-plugin" "$RESONLY/resources"
printf '{"name": "ro"}\n' >"$RESONLY/.claude-plugin/plugin.json"; printf 'no links\n' >"$RESONLY/resources/R.md"
out="$(run_static "$RESONLY")"; rc=$?
expect_eq "a plugin with only a resource and no reference still fails" 1 "$rc"
HOOKAG="$WORKDIR/hook-and-agent"; mkhooks "$HOOKAG" ha; mkdir -p "$HOOKAG/agents"; printf -- '---\nname: a\n---\nno links\n' >"$HOOKAG/agents/a.md"
printf 'see [x](${CLAUDE_PLUGIN_ROOT}/hooks/scripts/a-section.md)\n' >"$HOOKAG/hooks/scripts/b-section.md"
out="$(run_static "$HOOKAG")"; rc=$?
expect_eq "references in hook markdown do not satisfy the census of a plugin with agents" 1 "$rc"
expect_has "it is still a census of nothing" "$out" "a census of nothing proves nothing"

echo "hook-injected markdown"
HOOKB="$WORKDIR/hook-bad"; mkhooks "$HOOKB" hb; HOOKB="$(realpath_of "$HOOKB")"
printf '# Section\nSee [rel](../../resources/foo.md) and `src/nexus/session.py`.\nAlso ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/a-section.md here.\n' >"$HOOKB/hooks/scripts/a-section.md"
out="$(run_static "$HOOKB")"; rc=$?
expect_eq "hook markdown with bad references fails" 1 "$rc"
expect_has "a relative link is RELATIVE-DEAD (a hook's text carries no location)" "$out" "RELATIVE-DEAD  hooks/scripts/a-section.md:2  ../../resources/foo.md"
expect_has "a repo path is REPO-ONLY" "$out" "REPO-ONLY      hooks/scripts/a-section.md:2  src/nexus/session.py"
expect_has "a plugin-root variable is UNSUBSTITUTED (the hook prints it literally)" "$out" 'UNSUBSTITUTED  hooks/scripts/a-section.md:3  ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/a-section.md'
expect_eq "three failures, all on the hooks surface" 3 "$(( $(cnt "$HOOKB" hooks RELATIVE-DEAD) + $(cnt "$HOOKB" hooks REPO-ONLY) + $(cnt "$HOOKB" hooks UNSUBSTITUTED) ))"
expect_lacks "README.md under hooks/ is not scanned" "$out" "hooks/README.md"
out="$(bash "$GATE" --plugin "$HOOKB" --static-only 2>&1)"; rc=$?
expect_eq "the gate fails a hook-only plugin with bad hook markdown" 1 "$rc"
expect_has "FAILED, not NOT-APPLICABLE" "$out" "PLUGIN OUT-OF-REPO GATE FAILED"

echo "allowlist"
AL="$WORKDIR/allow.txt"
run_al() { run_static "$1" --allowlist "$AL"; }
{
    echo '# comment and blank lines are fine'
    echo
    echo 'fx | agents/worker.md | ../resources/agent-shared/RELAY.md | RELATIVE-DEAD | the fixture documents a relative link on purpose'
    echo 'fx | agents/worker.md | conexus/registry.yaml | REPO-ONLY | named as an example of a repo path, never opened'
    echo 'fx | agents/worker.md | ${CLAUDE_PLUGIN_ROOT}/nope.md | MISSING | the file ships in a later release of the fixture'
} >"$AL"
out="$(run_al "$BAD")"; rc=$?
expect_eq "every failing reference allowlisted: exit 0" 0 "$rc"
expect_has "each is listed as ALLOWED with its reason" "$out" "ALLOWED REPO-ONLY      agents/worker.md:5  conexus/registry.yaml  [named as an example of a repo path, never opened]"
expect_has "the summary counts them" "$out" "0 failing reference(s), 3 allowed"
out="$(run_static "$BAD")"; rc=$?
expect_eq "the same plugin without the allowlist still fails" 1 "$rc"
printf 'fx | agents/worker.md | conexus/registry.yaml | REPO-ONLY | named as an example of a repo path, never opened\n' >"$AL"
out="$(run_al "$BAD")"; rc=$?
expect_eq "an entry covers only its own reference" 1 "$rc"
expect_has "the others still fail" "$out" "2 failing reference(s), 1 allowed"
printf 'fx | agents/worker.md | conexus/registry.yaml | REPO-ONLY | \n' >"$AL"
out="$(run_al "$BAD")"; rc=$?
expect_eq "an entry with an empty reason is refused" 1 "$rc"
expect_has "naming the line and the reason" "$out" "allowlist: allow.txt:1: the reason is empty"
expect_has "and nothing is censused under it" "$out" "the allowlist is refused"
printf 'fx | agents/worker.md | conexus/registry.yaml | REPO-ONLY | todo\n' >"$AL"
out="$(run_al "$BAD")"; rc=$?
expect_eq "a one-word reason is refused" 1 "$rc"
expect_has "as too short" "$out" "the reason is too short"
printf 'fx | agents/worker.md | conexus/registry.yaml | REPO-ONLY | acknowledged elsewhere\n' >"$AL"
out="$(run_al "$BAD")"; rc=$?
expect_has "a two-word reason is refused too (the documented floor is three words)" "$out" "at least 3 words"
printf 'fx | agents/worker.md | conexus/registry.yaml | REPO-ONLY | an example, never opened\n' >"$AL"
out="$(run_al "$BAD")"
expect_has "three words are accepted" "$out" "ALLOWED REPO-ONLY"
printf 'fx | agents/worker.md | conexus/registry.yaml\n' >"$AL"
out="$(run_al "$BAD")"; rc=$?
expect_eq "an entry with a missing field is refused" 1 "$rc"
expect_has "naming the shape" "$out" "want '<plugin> | <file> | <target> | <class> | <reason>'"
{
    echo 'fx | agents/worker.md | conexus/registry.yaml | REPO-ONLY | a perfectly good reason here'
    echo 'fx | agents/worker.md | conexus/registry.yaml | REPO-ONLY | the same key written twice'
} >"$AL"
out="$(run_al "$BAD")"; rc=$?
expect_eq "a repeated key is refused" 1 "$rc"
expect_has "naming the repeat" "$out" "allow.txt:2: repeats an earlier entry"
printf 'conexs | agents/worker.md | conexus/registry.yaml | REPO-ONLY | a perfectly good reason here\n' >"$AL"
out="$(run_al "$BAD")"; rc=$?
expect_eq "an entry naming a plugin that is none of the four (a typo) is refused, not silently inert" 1 "$rc"
expect_has "naming the plugin" "$out" "unknown plugin 'conexs'"
printf 'fx | agents/worker.md | conexus/registry.yaml | MAYBE | a perfectly good reason here\n' >"$AL"
out="$(run_al "$BAD")"
expect_has "an unknown class is refused" "$out" "class 'MAYBE' is not a failing class"
# stale entries fail
STALE_R='named as an example of a repo path, never opened'
printf 'fx | agents/worker.md | conexus/never-written.yaml | REPO-ONLY | %s\n' "$STALE_R" >"$AL"
out="$(run_al "$CLEAN")"; rc=$?
expect_eq "an entry whose target is no longer in the file is STALE and fails" 1 "$rc"
expect_has "naming the target" "$out" "STALE agents/worker.md  conexus/never-written.yaml  [the target is no longer in the file]"
printf 'fx | agents/gone.md | conexus/registry.yaml | REPO-ONLY | %s\n' "$STALE_R" >"$AL"
out="$(run_al "$CLEAN")"; rc=$?
expect_eq "an entry whose file is gone is STALE and fails" 1 "$rc"
expect_has "naming the file" "$out" "[the file is no longer in the plugin]"
printf 'fx | agents/worker.md | ${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/RELAY.md | MISSING | %s\n' "$STALE_R" >"$AL"
out="$(run_al "$CLEAN")"; rc=$?
expect_eq "an entry for a reference that no longer fails is STALE and fails" 1 "$rc"
expect_has "naming why" "$out" "[the reference is RESOLVES now, not MISSING; drop the entry]"
printf 'fx | agents/worker.md | conexus/registry.yaml | MISSING | %s\n' "$STALE_R" >"$AL"
out="$(run_al "$BAD")"; rc=$?
expect_eq "an entry keyed on the wrong class neither covers the reference nor stays quiet" 1 "$rc"
expect_has "it is STALE (fix the entry)" "$out" "[the reference is REPO-ONLY now, not MISSING; fix the entry]"
expect_has "and the reference it was meant for still fails" "$out" "REPO-ONLY      agents/worker.md:5  conexus/registry.yaml"
printf 'sn | agents/gone.md | conexus/registry.yaml | REPO-ONLY | %s\n' "$STALE_R" >"$AL"
out="$(run_al "$CLEAN")"; rc=$?
expect_eq "an entry for another real plugin is not this plugin's to judge" 0 "$rc"
out="$(run_static "$CLEAN" --allowlist "$HERE/plugin-out-of-repo-allowlist.txt")"; rc=$?
expect_eq "the shipped allowlist is accepted" 0 "$rc"
expect_lacks "and allows nothing in a clean plugin" "$out" "ALLOWED"
out="$(bash "$GATE" --plugin "$CLEAN" --static-only --allowlist "$WORKDIR/no-such-allowlist" 2>&1)"; rc=$?
expect_eq "--allowlist naming a file that does not exist is a usage error" 64 "$rc"
printf 'fx | agents/worker.md | conexus/registry.yaml | REPO-ONLY | %s\n' "$STALE_R" >"$AL"
out="$(bash "$GATE" --plugin "$BAD" --static-only --allowlist "$AL" 2>&1)"; rc=$?
expect_eq "the gate honours --allowlist (still 2 failing)" 1 "$rc"
expect_has "and the static pass saw the entry" "$out" "1 allowed"

echo "multi-plugin modes"
CACHE="$WORKDIR/cache"; V=9.9.9
mkdir -p "$CACHE"
mkplugin "$CACHE/conexus/$V" conexus "$V"
mkhooks "$CACHE/sn/$V" sn "$V"
mkdir -p "$CACHE/conexus-tool-spill/$V/.claude-plugin"; printf '{"name": "conexus-tool-spill", "version": "%s"}\n' "$V" >"$CACHE/conexus-tool-spill/$V/.claude-plugin/plugin.json"
mkplugin "$CACHE/conexus-prose-edit/$V" conexus-prose-edit "$V"
export NX_GATE_CONEXUS_FLOOR="1 1"   # the fixtures are one-agent, one-skill plugins; the real floor is tested on its own below
# the checked-in allowlist names real conexus files, which these one-agent fixtures lack (its entries would read stale)
NOALLOW="$WORKDIR/no-allowlist.txt"; : >"$NOALLOW"
run_inst() { NX_GATE_PLUGIN_CACHE="$CACHE" bash "$GATE" --installed "$V" --static-only --allowlist "$NOALLOW" 2>&1; }
out="$(run_inst)"; rc=$?
expect_eq "four installed plugins, all clean: exit 0" 0 "$rc"
expect_has "conexus passed" "$out" "conexus                PASSED"
expect_has "sn is NOT-APPLICABLE" "$out" "sn                     NOT-APPLICABLE"
expect_has "tool-spill is NOT-APPLICABLE" "$out" "conexus-tool-spill     NOT-APPLICABLE"
expect_has "prose-edit passed" "$out" "conexus-prose-edit     PASSED"
expect_has "one verdict line" "$out" "PLUGIN OUT-OF-REPO GATE PASSED (static pass only) -- installed $V: 2 passed, 2 not applicable"
printf -- '---\nname: worker\n---\nUse [x](../nope.md).\n' >"$CACHE/conexus-prose-edit/$V/agents/worker.md"
out="$(run_inst)"; rc=$?
expect_eq "one bad installed plugin fails the run" 1 "$rc"
expect_has "naming it" "$out" "conexus-prose-edit     FAILED"
expect_lacks "and the closing verdict is not PASSED (a child's own PASSED line does not count)" "$(printf '%s\n' "$out" | tail -1)" "GATE PASSED"
mkplugin "$CACHE/conexus-prose-edit/$V" conexus-prose-edit "$V"
rm -rf "$CACHE/sn/$V"
out="$(run_inst)"; rc=$?
expect_eq "a plugin that is not installed at that version fails, never skipped" 1 "$rc"
expect_has "naming it" "$out" "sn                     FAILED (not found)"
mkhooks "$CACHE/sn/$V" sn "$V"
out="$(NX_GATE_PLUGIN_CACHE="$CACHE" bash "$GATE" --installed 1.2.3 --static-only 2>&1)"; rc=$?
expect_eq "a version nothing is installed at fails" 1 "$rc"
# the real floor: a conexus of one agent and one skill is APPLICABLE and still not a pass
out="$(NX_GATE_CONEXUS_FLOOR="" NX_GATE_PLUGIN_CACHE="$CACHE" bash "$GATE" --installed "$V" --static-only 2>&1)"; rc=$?
expect_eq "a one-agent one-skill conexus is below the census floor: the run FAILS" 1 "$rc"
expect_has "naming the counts and the floor" "$out" "conexus                FAILED (hollow: 1 agents, 1 skills; floor 5/10)"
expect_lacks "and the closing verdict is not PASSED" "$(printf '%s\n' "$out" | tail -1)" "GATE PASSED"
# a hollow plugin (NOT-APPLICABLE where it must be APPLICABLE) is not a pass
HOLLOW="$WORKDIR/cache-hollow"; cp -R "$CACHE" "$HOLLOW"
rm -rf "$HOLLOW/conexus/$V/agents" "$HOLLOW/conexus/$V/commands" "$HOLLOW/conexus/$V/skills" "$HOLLOW/conexus/$V/resources"
mkdir -p "$HOLLOW/conexus/$V/hooks/scripts"; printf '# s\n' >"$HOLLOW/conexus/$V/hooks/scripts/a-section.md"
out="$(NX_GATE_PLUGIN_CACHE="$HOLLOW" bash "$GATE" --installed "$V" --static-only 2>&1)"; rc=$?
expect_eq "conexus emptied to its hooks, with a real prose-edit: the run FAILS (it used to PASS)" 1 "$rc"
expect_has "naming the hollow plugin" "$out" "conexus                FAILED (hollow: HOOKS-ONLY, want APPLICABLE)"
expect_lacks "and the closing verdict is not PASSED" "$(printf '%s\n' "$out" | tail -1)" "GATE PASSED"
ALLNA="$WORKDIR/cache-na"
mkhooks "$ALLNA/conexus/$V" conexus "$V"; mkhooks "$ALLNA/sn/$V" sn "$V"; mkhooks "$ALLNA/conexus-tool-spill/$V" conexus-tool-spill "$V"; mkhooks "$ALLNA/conexus-prose-edit/$V" conexus-prose-edit "$V"
out="$(NX_GATE_PLUGIN_CACHE="$ALLNA" bash "$GATE" --installed "$V" --static-only 2>&1)"; rc=$?
expect_eq "four hook-only plugins are not a pass" 1 "$rc"
# sn must have hook markdown; an sn that is not even that is hollow
NOSN="$WORKDIR/cache-nosn"; cp -R "$CACHE" "$NOSN"; rm -rf "$NOSN/sn/$V/hooks"
out="$(NX_GATE_PLUGIN_CACHE="$NOSN" bash "$GATE" --installed "$V" --static-only 2>&1)"; rc=$?
expect_eq "an sn with no hook markdown is hollow too" 1 "$rc"
# plugin.json name and version
WRONGV="$WORKDIR/cache-wrongv"; cp -R "$CACHE" "$WRONGV"
printf '{"name": "conexus", "version": "9.9.8"}\n' >"$WRONGV/conexus/$V/.claude-plugin/plugin.json"
out="$(NX_GATE_PLUGIN_CACHE="$WRONGV" bash "$GATE" --installed "$V" --static-only 2>&1)"; rc=$?
expect_eq "an install whose plugin.json version is not the one asked for fails" 1 "$rc"
expect_has "naming both" "$out" "plugin.json version '9.9.8', not '$V'"
WRONGN="$WORKDIR/cache-wrongn"; cp -R "$CACHE" "$WRONGN"
printf '{"name": "something-else", "version": "%s"}\n' "$V" >"$WRONGN/conexus-prose-edit/$V/.claude-plugin/plugin.json"
out="$(NX_GATE_PLUGIN_CACHE="$WRONGN" bash "$GATE" --installed "$V" --static-only 2>&1)"; rc=$?
expect_eq "a directory whose plugin.json names another plugin fails" 1 "$rc"
out="$(NX_GATE_PLUGIN_CACHE="$CACHE" NX_GATE_CLAUDE="$WORKDIR/no-such-claude" bash "$GATE" --installed "$V" 2>&1)"; rc=$?
expect_eq "with claude absent the canary is due: UNVERIFIED, exit 2" 2 "$rc"
expect_has "the closing verdict is UNVERIFIED" "$(printf '%s\n' "$out" | tail -1)" "PLUGIN OUT-OF-REPO GATE UNVERIFIED"
: >"$STUB_LOG"; rm -rf "$STUB_STATE"; mkdir -p "$STUB_STATE"
out="$(NX_GATE_PLUGIN_CACHE="$CACHE" NX_GATE_CLAUDE="$STUBW" bash "$GATE" --installed "$V" --allowlist "$NOALLOW" 2>&1)"; rc=$?
expect_eq "four installed plugins with the stub: static + canary, once" 0 "$rc"
expect_has "one closing verdict naming the canary" "$(printf '%s\n' "$out" | tail -1)" "PLUGIN OUT-OF-REPO GATE PASSED (static + canary) -- installed $V: 2 passed, 2 not applicable"
expect_eq "the canary ran once for the whole run (5 sessions), not once per plugin" 5 "$(grep -c '^ARGV' "$STUB_LOG")"
out="$(bash "$GATE" --installed notaversion 2>&1)"; rc=$?
expect_eq "--installed wants a version" 64 "$rc"
out="$(bash "$GATE" --all --only worker 2>&1)"; rc=$?
expect_eq "--only does not combine with --all" 64 "$rc"
# --all runs the four directories of this checkout (conexus, sn, tool-spill, prose-edit)
out="$(bash "$GATE" --all --static-only 2>&1)"; rc=$?
expect_has "--all names all four directories of the checkout" "$out" "######## plugin: prose-edit"
expect_has "including the hook-only ones" "$out" "######## plugin: tool-spill"
case "$rc" in 0|1) ok "--all --static-only on the real tree ends 0 (clean) or 1 (a plugin fails), never 2 or 64" ;; *) bad "--all --static-only exited $rc" ;; esac
case "$rc" in 0) expect_has "a clean tree reports PASSED (static pass only)" "$(printf '%s\n' "$out" | tail -1)" "GATE PASSED (static pass only)" ;;
              *) expect_has "a failing tree reports FAILED on the last line" "$(printf '%s\n' "$out" | tail -1)" "GATE FAILED" ;; esac

echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
