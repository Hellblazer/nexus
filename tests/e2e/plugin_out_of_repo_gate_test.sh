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

echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
