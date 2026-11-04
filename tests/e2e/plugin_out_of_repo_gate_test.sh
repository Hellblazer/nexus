#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/e2e/plugin_out_of_repo_gate_test.sh -- offline self-test for
# tests/e2e/plugin-out-of-repo-gate.sh and tests/e2e/lib/plugin_out_of_repo.py
# (nexus-z7ddd). Needs no model call and no network: the static pass runs on
# fixture plugins, and the live judgement runs on hand-written stream-json event
# files. It proves the gate goes red when it should (fail-first) and does not
# pass over nothing (the nexus-moht0 doctrine):
#
#   1. a clean fixture plugin passes the static pass; its counts are exact;
#   2. a relative link in an agent, a repo-only path and a ${CLAUDE_PLUGIN_ROOT}
#      path to a missing file each fail it, named with file:line;
#   3. a skill's link that opens from the skill's own directory does not fail;
#   4. a plugin with no references at all fails (a census of nothing);
#   5. the live judgement: a quoted ${CLAUDE_PLUGIN_ROOT}-substituted path that
#      the event stream shows opened is clean; a Read error, a missing Read, a
#      relative target in an agent (even if some Read opened), and a run that
#      quoted nothing where references are expected each fail; a skill's
#      relative target is judged against its announced base directory; a path
#      read from a nexus checkout outside the plugin fails; no run with a
#      result at all exits 2;
#   6. the gate exits 2, not 0, when claude is absent.
#
# Run with: bash tests/e2e/plugin_out_of_repo_gate_test.sh
# shellcheck disable=SC2016  # the fixtures hold a literal ${CLAUDE_PLUGIN_ROOT}
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE="$HERE/plugin-out-of-repo-gate.sh"
MOD="$HERE/lib/plugin_out_of_repo.py"
# shellcheck source=lib/python.sh disable=SC1091
source "$HERE/lib/python.sh"
e2e_python_resolve 10 || exit 2
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/plugin_out_of_repo_gate_test.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT
PASS=0; FAIL=0
ok()  { echo "  [ok] $1"; PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }
expect_has()   { case "$2" in *"$3"*) ok "$1" ;; *) bad "$1: missing [$3]" ;; esac; }
expect_lacks() { case "$2" in *"$3"*) bad "$1: unexpected [$3]" ;; *) ok "$1" ;; esac; }
expect_eq()    { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1: want [$2] got [$3]"; fi; }

[ -f "$GATE" ] && [ -f "$MOD" ] || { bad "the gate or its module does not exist"; echo "$PASS passed, $FAIL failed"; exit 1; }

# ---- fixture plugins -------------------------------------------------------
mkplugin() {  # <dir> : a plugin with one agent, one skill and two resources
    local d="$1"
    mkdir -p "$d/.claude-plugin" "$d/agents" "$d/commands" "$d/skills/probe" "$d/resources/agent-shared"
    printf '{"name": "fx", "version": "1.0.0"}\n' >"$d/.claude-plugin/plugin.json"
    printf '# relay\nsee [ctx](./CTX.md)\n' >"$d/resources/agent-shared/RELAY.md"
    printf '# ctx\n' >"$d/resources/agent-shared/CTX.md"
    printf '# ref\n' >"$d/skills/probe/reference.md"
    printf -- '---\nname: probe\n---\nSee [ref](./reference.md) and [relay](../../resources/agent-shared/RELAY.md).\n' >"$d/skills/probe/SKILL.md"
    printf -- '---\nname: worker\n---\nUse [relay](${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/RELAY.md).\nDocs: https://example.com/x.md and `$RDR_DIR/README.md`.\n' >"$d/agents/worker.md"
    printf 'Run it.\n' >"$d/commands/go.md"
}
CLEAN="$WORKDIR/clean"; mkplugin "$CLEAN"
CLEAN="$(cd "$CLEAN" && pwd -P)"

run_static() { "$E2E_PYTHON" -I "$MOD" static --plugin "$1" 2>&1; }

echo "static pass"
out="$(run_static "$CLEAN")"; rc=$?
expect_eq "clean fixture exits 0" 0 "$rc"
cnt() {  # <plugin> <surface> <class>
    "$E2E_PYTHON" -I "$MOD" static --plugin "$1" --json 2>&1 | "$E2E_PYTHON" -I -c \
        'import json,sys; d=json.load(sys.stdin); print(d["counts"].get(sys.argv[1],{}).get(sys.argv[2],0))' "$2" "$3"
}
expect_eq "clean fixture: 1 RESOLVES in agents" 1 "$(cnt "$CLEAN" agents RESOLVES)"
expect_eq "clean fixture: 1 PLACEHOLDER in agents" 1 "$(cnt "$CLEAN" agents PLACEHOLDER)"
expect_eq "clean fixture: 2 RELATIVE-NEAR in skills" 2 "$(cnt "$CLEAN" skills RELATIVE-NEAR)"
expect_eq "clean fixture: 1 RELATIVE-NEAR in resources" 1 "$(cnt "$CLEAN" resources RELATIVE-NEAR)"
expect_has "clean fixture is reported clean" "$out" "static pass clean: 0 failing"

BAD="$WORKDIR/bad"; mkplugin "$BAD"; BAD="$(cd "$BAD" && pwd -P)"
printf -- '---\nname: worker\n---\nline two\nUse [relay](../resources/agent-shared/RELAY.md) and `conexus/registry.yaml` and [x](${CLAUDE_PLUGIN_ROOT}/nope.md).\n' >"$BAD/agents/worker.md"
out="$(run_static "$BAD")"; rc=$?
expect_eq "bad fixture exits 1" 1 "$rc"
expect_has "relative link in an agent is RELATIVE-DEAD with file:line" "$out" "RELATIVE-DEAD  agents/worker.md:5  ../resources/agent-shared/RELAY.md"
expect_has "repo-only path is REPO-ONLY with file:line" "$out" "REPO-ONLY      agents/worker.md:5  conexus/registry.yaml"
expect_has "missing substituted path is MISSING with file:line" "$out" 'MISSING        agents/worker.md:5  ${CLAUDE_PLUGIN_ROOT}/nope.md'
expect_has "the skill's ../../ link is still not a failure" "$out" "3 failing reference(s)"

EMPTY="$WORKDIR/empty"; mkdir -p "$EMPTY/.claude-plugin" "$EMPTY/agents"
printf '{"name": "fx"}\n' >"$EMPTY/.claude-plugin/plugin.json"; printf -- '---\nname: a\n---\nno links\n' >"$EMPTY/agents/a.md"
out="$(run_static "$EMPTY")"; rc=$?
expect_eq "a plugin with no references fails" 1 "$rc"
expect_has "and says why" "$out" "a census of nothing proves nothing"

# ---- live judgement on hand-written event streams --------------------------
ev_line() { printf '%s\n' "$1"; }
mkevents() {  # <events-dir> <surface> <name> <result-text> <read-path|-> <read-ok:true|false>
    local f="$1/$2__$3.events.jsonl"
    "$E2E_PYTHON" -I - "$f" "$4" "$5" "$6" <<'PY'
import json, sys
path, text, read_path, ok = sys.argv[1:5]
import os
ev = []
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
LIVE="$WORKDIR/live-plugin"; mkplugin "$LIVE"; LIVE="$(cd "$LIVE" && pwd -P)"
# the fixture agent carries the references the runs below quote; a quoted target the
# plugin's markdown does not contain is reported as (extra) and never fails
CK="$WORKDIR/ck"; mkdir -p "$CK/conexus/.claude-plugin" "$CK/src/nexus" "$CK/scripts"; : >"$CK/conexus/.claude-plugin/plugin.json"; : >"$CK/scripts/x.sh"
printf -- '---\nname: worker\n---\nUse [relay](${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/RELAY.md).\nAlso [rel](../resources/agent-shared/RELAY.md) and [ck](%s/scripts/x.sh).\n' "$CK" >"$LIVE/agents/worker.md"
rm -rf "$LIVE/skills/probe"; mkdir -p "$LIVE/skills/probe"; printf -- '---\nname: probe\n---\nNo references here.\n' >"$LIVE/skills/probe/SKILL.md"
T=$'\t'
run_live() { "$E2E_PYTHON" -I "$MOD" live --plugin "$LIVE" --events-dir "$1" --partial 2>&1; }

D="$WORKDIR/ev-ok"; mkdir -p "$D"
mkevents "$D" agents worker "REF${T}$LIVE/resources/agent-shared/RELAY.md${T}$LIVE/resources/agent-shared/RELAY.md" "$LIVE/resources/agent-shared/RELAY.md" true
out="$(run_live "$D")"; rc=$?
expect_eq "an opened absolute reference is clean" 0 "$rc"
expect_has "and judged" "$out" "OPENED=1"

D="$WORKDIR/ev-err"; mkdir -p "$D"
mkevents "$D" agents worker "REF${T}$LIVE/resources/agent-shared/RELAY.md${T}$LIVE/resources/agent-shared/RELAY.md" "$LIVE/resources/agent-shared/RELAY.md" false
out="$(run_live "$D")"; rc=$?
expect_eq "a Read error fails" 1 "$rc"
expect_has "as FAILED" "$out" "FAILED"

D="$WORKDIR/ev-unread"; mkdir -p "$D"
mkevents "$D" agents worker "REF${T}$LIVE/resources/agent-shared/RELAY.md${T}$LIVE/resources/agent-shared/RELAY.md" - true
out="$(run_live "$D")"; rc=$?
expect_eq "a claimed read with no Read call fails" 1 "$rc"
expect_has "as UNREAD" "$out" "UNREAD"

D="$WORKDIR/ev-rel"; mkdir -p "$D"
mkevents "$D" agents worker "REF${T}../resources/agent-shared/RELAY.md${T}$LIVE/resources/agent-shared/RELAY.md" "$LIVE/resources/agent-shared/RELAY.md" true
out="$(run_live "$D")"; rc=$?
expect_eq "a relative target in an agent fails even when a guessed path opened" 1 "$rc"
expect_has "as RELATIVE-DEAD" "$out" "RELATIVE-DEAD"

D="$WORKDIR/ev-extra"; mkdir -p "$D"
mkevents "$D" agents worker "REF${T}$LIVE/resources/agent-shared/RELAY.md${T}$LIVE/resources/agent-shared/RELAY.md
REF${T}scripts/not-in-the-plugin.sh${T}/nowhere/scripts/not-in-the-plugin.sh" "$LIVE/resources/agent-shared/RELAY.md" true
out="$(run_live "$D")"; rc=$?
expect_eq "a quoted target the plugin does not contain does not fail the run" 0 "$rc"
expect_has "and is reported as extra" "$out" "(extra)"

D="$WORKDIR/ev-none"; mkdir -p "$D"
mkevents "$D" agents worker "nothing to report" - true
out="$(run_live "$D")"; rc=$?
expect_eq "a run that quotes nothing where references are expected fails" 1 "$rc"
expect_has "as NO-REFS" "$out" "NO-REFS"

D="$WORKDIR/ev-checkout"; mkdir -p "$D"
mkevents "$D" agents worker "REF${T}$CK/scripts/x.sh${T}$CK/scripts/x.sh" "$CK/scripts/x.sh" true
out="$(run_live "$D")"; rc=$?
expect_eq "a path that opens in a nexus checkout outside the plugin fails" 1 "$rc"
expect_has "as INTO-CHECKOUT" "$out" "INTO-CHECKOUT"

# a skill's relative target is judged against the announced base directory
SK="$WORKDIR/sk-plugin"; mkplugin "$SK"; SK="$(cd "$SK" && pwd -P)"
D="$WORKDIR/ev-skill"; mkdir -p "$D"
BASEDIR_TEXT="$SK/skills/probe" mkevents "$D" skills probe "REF${T}../../resources/agent-shared/RELAY.md${T}$SK/resources/agent-shared/RELAY.md" "$SK/resources/agent-shared/RELAY.md" true
out="$("$E2E_PYTHON" -I "$MOD" live --plugin "$SK" --events-dir "$D" --partial 2>&1)"; rc=$?
expect_eq "a skill relative target resolved against the base directory Claude Code announced is clean" 0 "$rc"
D="$WORKDIR/ev-skill-bad"; mkdir -p "$D"
mkevents "$D" skills probe "REF${T}../../resources/agent-shared/RELAY.md${T}$SK/resources/agent-shared/RELAY.md" "$SK/resources/agent-shared/RELAY.md" true
out="$("$E2E_PYTHON" -I "$MOD" live --plugin "$SK" --events-dir "$D" --partial 2>&1)"; rc=$?
expect_eq "the same target with no announced base directory fails" 1 "$rc"
expect_has "as RELATIVE-DEAD" "$out" "RELATIVE-DEAD"

D="$WORKDIR/ev-noresult"; mkdir -p "$D"
printf '{"type": "system"}\n' >"$D/agents__worker.events.jsonl"
out="$(run_live "$D")"; rc=$?
expect_eq "no run with a result is UNVERIFIED, exit 2" 2 "$rc"

# ---- claude absent ----------------------------------------------------------
out="$(NX_GATE_CLAUDE="$WORKDIR/no-such-claude" bash "$GATE" --plugin "$CLEAN" 2>&1)"; rc=$?
expect_eq "claude absent exits 2" 2 "$rc"
expect_has "and says UNVERIFIED" "$out" "UNVERIFIED"
expect_lacks "and never says PASSED" "$out" "PASSED"
out="$(bash "$GATE" --plugin "$CLEAN" --static-only 2>&1)"; rc=$?
expect_eq "--static-only needs no claude and passes a clean plugin" 0 "$rc"
expect_has "and says it was the static pass only" "$out" "static pass only"
out="$(bash "$GATE" --plugin "$BAD" --static-only 2>&1)"; rc=$?
expect_eq "--static-only fails a bad plugin" 1 "$rc"


# ===========================================================================
# nexus-z7ddd, second half: directories, NOT-APPLICABLE, hook markdown,
# the allowlist, the multi-plugin modes. Each block was run red against the
# first version of the gate (commit e172281db) before the code existed.
# ===========================================================================

named_plugin() {  # <dir> <name> : the standard fixture plugin under another name
    mkplugin "$1"
    printf '{"name": "%s", "version": "1.0.0"}\n' "$2" >"$1/.claude-plugin/plugin.json"
}
mkhooks() {  # <dir> <name> : a hook-only plugin: one injected section, no agents or skills
    local d="$1"
    mkdir -p "$d/.claude-plugin" "$d/hooks/scripts"
    printf '{"name": "%s", "version": "1.0.0"}\n' "${2:-hk}" >"$d/.claude-plugin/plugin.json"
    printf '# Section\nNo file references in here.\n' >"$d/hooks/scripts/a-section.md"
    # documentation under hooks/ is not injected: it must not be scanned
    printf '# readme\nSee `src/nexus/x.py`.\n' >"$d/hooks/README.md"
}

echo "directory references"
DIRP="$WORKDIR/dirs"; mkplugin "$DIRP"; DIRP="$(cd "$DIRP" && pwd -P)"
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

# the live pass cannot Read a directory, so a directory is not an expectation: an agent whose
# only reference is a directory expects nothing, and a run that quotes nothing is clean
DIRL="$WORKDIR/dirs-live"; mkplugin "$DIRL"; DIRL="$(cd "$DIRL" && pwd -P)"
mkdir -p "$DIRL/scripts"
printf -- '---\nname: worker\n---\nThe scripts are in ${CLAUDE_PLUGIN_ROOT}/scripts/ only.\n' >"$DIRL/agents/worker.md"
rm -rf "$DIRL/skills/probe"; mkdir -p "$DIRL/skills/probe"; printf -- '---\nname: probe\n---\nNo references here.\n' >"$DIRL/skills/probe/SKILL.md"
D="$WORKDIR/ev-dir"; mkdir -p "$D"
mkevents "$D" agents worker "nothing to report" - true
out="$("$E2E_PYTHON" -I "$MOD" live --plugin "$DIRL" --events-dir "$D" --partial 2>&1)"; rc=$?
expect_eq "an agent whose only reference is a directory expects no Read" 0 "$rc"
expect_lacks "and is not NO-REFS" "$out" "NO-REFS"

echo "plugins with nothing to census"
HOOKP="$WORKDIR/hookonly"; mkhooks "$HOOKP"; HOOKP="$(cd "$HOOKP" && pwd -P)"
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
# non-vacuity still holds for a plugin that has agents, skills, commands or resources
SKONLY="$WORKDIR/skills-only"; mkdir -p "$SKONLY/.claude-plugin" "$SKONLY/skills/s"
printf '{"name": "so"}\n' >"$SKONLY/.claude-plugin/plugin.json"; printf -- '---\nname: s\n---\nno links\n' >"$SKONLY/skills/s/SKILL.md"
out="$(run_static "$SKONLY")"; rc=$?
expect_eq "a plugin with a skill and no reference still fails" 1 "$rc"
expect_has "as a census of nothing" "$out" "a census of nothing proves nothing"
RESONLY="$WORKDIR/res-only"; mkdir -p "$RESONLY/.claude-plugin" "$RESONLY/resources"
printf '{"name": "ro"}\n' >"$RESONLY/.claude-plugin/plugin.json"; printf 'no links\n' >"$RESONLY/resources/R.md"
out="$(run_static "$RESONLY")"; rc=$?
expect_eq "a plugin with only a resource and no reference still fails" 1 "$rc"
# a plugin whose only references are in hook markdown, but which has an agent without any: still a census of nothing
HOOKAG="$WORKDIR/hook-and-agent"; mkhooks "$HOOKAG"; mkdir -p "$HOOKAG/agents"; printf -- '---\nname: a\n---\nno links\n' >"$HOOKAG/agents/a.md"
printf 'see [x](${CLAUDE_PLUGIN_ROOT}/hooks/scripts/a-section.md)\n' >"$HOOKAG/hooks/scripts/b-section.md"
out="$(run_static "$HOOKAG")"; rc=$?
expect_eq "references in hook markdown do not satisfy the census of a plugin with agents" 1 "$rc"
expect_has "it is still a census of nothing" "$out" "a census of nothing proves nothing"

echo "hook-injected markdown"
HOOKB="$WORKDIR/hook-bad"; mkhooks "$HOOKB"; HOOKB="$(cd "$HOOKB" && pwd -P)"
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
run_al() { "$E2E_PYTHON" -I "$MOD" static --plugin "$1" --allowlist "$AL" 2>&1; }
{
    echo '# comment and blank lines are fine'
    echo
    echo 'fx | agents/worker.md | ../resources/agent-shared/RELAY.md | the fixture documents a relative link on purpose'
    echo 'fx | agents/worker.md | conexus/registry.yaml | named as an example of a repo path, never opened'
    echo 'fx | agents/worker.md | ${CLAUDE_PLUGIN_ROOT}/nope.md | the file ships in a later release of the fixture'
} >"$AL"
out="$(run_al "$BAD")"; rc=$?
expect_eq "every failing reference allowlisted: exit 0" 0 "$rc"
expect_has "each is listed as ALLOWED with its reason" "$out" "ALLOWED REPO-ONLY      agents/worker.md:5  conexus/registry.yaml  [named as an example of a repo path, never opened]"
expect_has "the summary counts them" "$out" "0 failing reference(s), 3 allowed"
out="$(run_static "$BAD")"; rc=$?
expect_eq "the same plugin without the allowlist still fails" 1 "$rc"
printf 'fx | agents/worker.md | conexus/registry.yaml | named as an example of a repo path, never opened\n' >"$AL"
out="$(run_al "$BAD")"; rc=$?
expect_eq "an entry covers only its own reference" 1 "$rc"
expect_has "the others still fail" "$out" "2 failing reference(s), 1 allowed"
printf 'fx | agents/worker.md | conexus/registry.yaml | \n' >"$AL"
out="$(run_al "$BAD")"; rc=$?
expect_eq "an entry with an empty reason is refused" 1 "$rc"
expect_has "naming the line and the reason" "$out" "allowlist: allow.txt:1: the reason is empty"
expect_has "and nothing is censused under it" "$out" "the allowlist is refused"
printf 'fx | agents/worker.md | conexus/registry.yaml | todo\n' >"$AL"
out="$(run_al "$BAD")"; rc=$?
expect_eq "a one-word reason is refused too" 1 "$rc"
expect_has "as too short" "$out" "the reason is too short"
printf 'fx | agents/worker.md | conexus/registry.yaml\n' >"$AL"
out="$(run_al "$BAD")"; rc=$?
expect_eq "an entry with a missing field is refused" 1 "$rc"
expect_has "naming the shape" "$out" "want '<plugin> | <file> | <target> | <reason>'"
{
    echo 'fx | agents/worker.md | conexus/registry.yaml | a perfectly good reason here'
    echo 'fx | agents/worker.md | conexus/registry.yaml | the same key written twice'
} >"$AL"
out="$(run_al "$BAD")"; rc=$?
expect_eq "a repeated key is refused" 1 "$rc"
expect_has "naming the repeat" "$out" "allow.txt:2: repeats an earlier entry"
# stale entries fail
STALE_R='named as an example of a repo path, never opened'
printf 'fx | agents/worker.md | conexus/never-written.yaml | %s\n' "$STALE_R" >"$AL"
out="$(run_al "$CLEAN")"; rc=$?
expect_eq "an entry whose target is no longer in the file is STALE and fails" 1 "$rc"
expect_has "naming the target" "$out" "STALE agents/worker.md  conexus/never-written.yaml  [the target is no longer in the file]"
printf 'fx | agents/gone.md | conexus/registry.yaml | %s\n' "$STALE_R" >"$AL"
out="$(run_al "$CLEAN")"; rc=$?
expect_eq "an entry whose file is gone is STALE and fails" 1 "$rc"
expect_has "naming the file" "$out" "[the file is no longer in the plugin]"
printf 'fx | agents/worker.md | ${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/RELAY.md | %s\n' "$STALE_R" >"$AL"
out="$(run_al "$CLEAN")"; rc=$?
expect_eq "an entry for a reference that no longer fails is STALE and fails" 1 "$rc"
expect_has "naming why" "$out" "[the reference no longer fails (it is RESOLVES); drop the entry]"
printf 'someone-else | agents/gone.md | conexus/registry.yaml | %s\n' "$STALE_R" >"$AL"
out="$(run_al "$CLEAN")"; rc=$?
expect_eq "an entry for another plugin is not this plugin's to judge" 0 "$rc"
# the shipped allowlist parses and censuses a clean plugin without a stale entry
out="$("$E2E_PYTHON" -I "$MOD" static --plugin "$CLEAN" --allowlist "$HERE/plugin-out-of-repo-allowlist.txt" 2>&1)"; rc=$?
expect_eq "the shipped allowlist is accepted" 0 "$rc"
expect_lacks "and allows nothing in a clean plugin" "$out" "ALLOWED"
out="$(bash "$GATE" --plugin "$CLEAN" --static-only --allowlist "$WORKDIR/no-such-allowlist" 2>&1)"; rc=$?
expect_eq "--allowlist naming a file that does not exist is a usage error" 64 "$rc"
printf 'fx | agents/worker.md | conexus/registry.yaml | named as an example of a repo path, never opened\n' >"$AL"
out="$(bash "$GATE" --plugin "$BAD" --static-only --allowlist "$AL" 2>&1)"; rc=$?
expect_eq "the gate honours --allowlist (still 2 failing)" 1 "$rc"
expect_has "and the static pass saw the entry" "$out" "1 allowed"
# live: an allowlisted reference is a decision, not an expectation
ALV="$WORKDIR/allow-live"; mkplugin "$ALV"; ALV="$(cd "$ALV" && pwd -P)"
printf -- '---\nname: worker\n---\nSee `conexus/registry.yaml` only.\n' >"$ALV/agents/worker.md"
rm -rf "$ALV/skills/probe"; mkdir -p "$ALV/skills/probe"; printf -- '---\nname: probe\n---\nNo references here.\n' >"$ALV/skills/probe/SKILL.md"
D="$WORKDIR/ev-allow"; mkdir -p "$D"
mkevents "$D" agents worker "nothing to report" - true
out="$("$E2E_PYTHON" -I "$MOD" live --plugin "$ALV" --events-dir "$D" --partial 2>&1)"; rc=$?
expect_eq "without an allowlist the run that quoted nothing fails" 1 "$rc"
expect_has "as NO-REFS" "$out" "NO-REFS"
out="$("$E2E_PYTHON" -I "$MOD" live --plugin "$ALV" --events-dir "$D" --partial --allowlist "$AL" 2>&1)"; rc=$?
expect_eq "with the entry the same run is clean" 0 "$rc"

echo "skills the Skill tool will not load"
DMI="$WORKDIR/dmi"; mkplugin "$DMI"; DMI="$(cd "$DMI" && pwd -P)"
mkdir -p "$DMI/skills/manual"
printf -- '---\nname: manual\ndisable-model-invocation: true\n---\nSee [relay](${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/RELAY.md).\n' >"$DMI/skills/manual/SKILL.md"
out="$("$E2E_PYTHON" -I "$MOD" targets --plugin "$DMI" 2>&1)"
expect_lacks "a disable-model-invocation skill is not a live target" "$out" "manual"
expect_has "the loadable skill still is" "$out" "probe"
D="$WORKDIR/ev-dmi"; mkdir -p "$D"
mkevents "$D" agents worker "REF${T}$DMI/resources/agent-shared/RELAY.md${T}$DMI/resources/agent-shared/RELAY.md" "$DMI/resources/agent-shared/RELAY.md" true
printf -- '---\nname: probe\n---\nNo references here.\n' >"$DMI/skills/probe/SKILL.md"
printf -- '---\nname: worker\n---\nUse [relay](${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/RELAY.md).\n' >"$DMI/agents/worker.md"
mkevents "$D" skills probe "nothing to report" - true
out="$("$E2E_PYTHON" -I "$MOD" live --plugin "$DMI" --events-dir "$D" 2>&1)"; rc=$?
expect_eq "a complete live run (every loadable target) is clean without one for the manual skill" 0 "$rc"
expect_has "and the manual skill is named as static-only" "$out" "(not live-testable) skills/manual"
expect_lacks "never NO-EVENTS" "$out" "NO-EVENTS"

echo "multi-plugin modes"
CACHE="$WORKDIR/cache"; V=9.9.9
mkdir -p "$CACHE"
named_plugin "$CACHE/conexus/$V" conexus
mkhooks "$CACHE/sn/$V" sn
mkdir -p "$CACHE/conexus-tool-spill/$V/.claude-plugin"; printf '{"name": "conexus-tool-spill"}\n' >"$CACHE/conexus-tool-spill/$V/.claude-plugin/plugin.json"
named_plugin "$CACHE/conexus-prose-edit/$V" conexus-prose-edit
run_inst() { NX_GATE_PLUGIN_CACHE="$CACHE" bash "$GATE" --installed "$V" --static-only 2>&1; }
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
named_plugin "$CACHE/conexus-prose-edit/$V" conexus-prose-edit
rm -rf "$CACHE/sn/$V"
out="$(run_inst)"; rc=$?
expect_eq "a plugin that is not installed at that version fails, never skipped" 1 "$rc"
expect_has "naming it" "$out" "sn                     FAILED (not found)"
mkhooks "$CACHE/sn/$V" sn
out="$(NX_GATE_PLUGIN_CACHE="$CACHE" bash "$GATE" --installed 1.2.3 --static-only 2>&1)"; rc=$?
expect_eq "a version nothing is installed at fails" 1 "$rc"
# a run in which nothing was applicable examined nothing
ALLNA="$WORKDIR/cache-na"
mkhooks "$ALLNA/conexus/$V" conexus; mkhooks "$ALLNA/sn/$V" sn; mkhooks "$ALLNA/conexus-tool-spill/$V" conexus-tool-spill; mkhooks "$ALLNA/conexus-prose-edit/$V" conexus-prose-edit
out="$(NX_GATE_PLUGIN_CACHE="$ALLNA" bash "$GATE" --installed "$V" --static-only 2>&1)"; rc=$?
expect_eq "four NOT-APPLICABLE plugins are not a pass" 1 "$rc"
expect_has "and the verdict says nothing was examined" "$out" "no plugin was applicable, so nothing was examined"
out="$(NX_GATE_PLUGIN_CACHE="$CACHE" NX_GATE_CLAUDE="$WORKDIR/no-such-claude" bash "$GATE" --installed "$V" 2>&1)"; rc=$?
expect_eq "with claude absent the live pass is due for conexus: UNVERIFIED, exit 2" 2 "$rc"
expect_has "the closing verdict is UNVERIFIED" "$(printf '%s\n' "$out" | tail -1)" "PLUGIN OUT-OF-REPO GATE UNVERIFIED"
out="$(bash "$GATE" --installed notaversion 2>&1)"; rc=$?
expect_eq "--installed wants a version" 64 "$rc"
out="$(bash "$GATE" --all --only worker 2>&1)"; rc=$?
expect_eq "--only does not combine with --all" 64 "$rc"
# --all runs the four directories of this checkout (conexus, sn, tool-spill, prose-edit)
out="$(bash "$GATE" --all --static-only 2>&1)"
expect_has "--all names all four directories of the checkout" "$out" "######## plugin: prose-edit"
expect_has "including the hook-only ones" "$out" "######## plugin: tool-spill"

echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
