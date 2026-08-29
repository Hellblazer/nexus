#!/usr/bin/env bash
# Mutation-falsification harness for nexus-qc4p1. Every mutation disables ONE
# mechanism and asserts the pin that claims to cover it goes RED. A green test
# that has not been falsified is not evidence.
#
# ⛔ COMMIT FIRST. Each mutation is reverted with `git checkout --`, which
# restores the COMMITTED version — so running this against a dirty tree
# silently DELETES your uncommitted work. That happened on the first run of
# the review-round fixes: two edits vanished mid-harness and only the
# post-restore baseline going red revealed it. The baseline check at the end
# is not decoration; if it is RED, the harness ate something.
#
# Run from the repo root.
set -u
cd "$(git rev-parse --show-toplevel)" || exit 1

HOOK=conexus/hooks/scripts/agent-dispatch-expect.sh
HOOKS_JSON=conexus/hooks/hooks.json
LIB=tests/e2e/lib/expectations.sh
PLUGIN_LIB=conexus/hooks/scripts/expectations.sh
T=tests/hooks/test_agent_dispatch_expect.py

restore() { git checkout -- "$@" 2>/dev/null; }

run() {  # run <label> <expect-red|expect-green> <pytest-args...>
    local label="$1" want="$2"; shift 2
    local out rc
    out="$(uv run pytest -q -p no:randomly "$@" 2>&1)"; rc=$?
    if [[ "$want" == "expect-red" ]]; then
        if [[ $rc -ne 0 ]]; then
            echo "MUTATION OK   [$label] -> RED: $(grep -Eo '[0-9]+ failed' <<<"$out" | head -1)"
        else
            echo "MUTATION FAIL [$label] -> stayed GREEN (the pin is vacuous)"
        fi
    else
        if [[ $rc -eq 0 ]]; then echo "BASELINE OK   [$label] -> GREEN"
        else echo "BASELINE FAIL [$label] -> RED"; echo "$out" | tail -5; fi
    fi
}

echo "== baseline =="
run "baseline" expect-green "$T"

echo
echo "== M1: hook not registered on Agent PreToolUse =="
python3 - <<'PY'
import json, pathlib
p = pathlib.Path("conexus/hooks/hooks.json")
d = json.loads(p.read_text())
d["hooks"]["PreToolUse"] = [
    e for e in d["hooks"]["PreToolUse"]
    if not any("agent-dispatch-expect.sh" in h.get("command", "") for h in e.get("hooks", []))
]
p.write_text(json.dumps(d, indent=2) + "\n")
PY
run "M1 registration removed" expect-red "$T::TestPluginWiring::test_registered_on_agent_pretooluse"
restore "$HOOKS_JSON"

echo
echo "== M2: key on a literal name instead of subagent_type (the pre-fix defect) =="
python3 - <<'PY'
import pathlib
p = pathlib.Path("conexus/hooks/scripts/agent-dispatch-expect.sh")
s = p.read_text().replace(
    'expectations_expect "$SESSION_ID" "$SUBAGENT_TYPE" "$DISPATCH_MODE" "$DISPATCH_ID"',
    'expectations_expect "$SESSION_ID" "teammate-1" "$DISPATCH_MODE" "$DISPATCH_ID"')
p.write_text(s)
PY
run "M2 unpairable key" expect-red "$T::TestPairsWithSubagentStart" "$T::TestSameTypeDispatchedTwice"
restore "$HOOK"

echo
echo "== M3: fail-CLOSED on the write path (deny + nonzero on bad input) =="
python3 - <<'PY'
import pathlib
p = pathlib.Path("conexus/hooks/scripts/agent-dispatch-expect.sh")
s = p.read_text().replace(
    '[[ -n "$SESSION_ID" && -n "$SUBAGENT_TYPE" ]] || exit 0',
    '[[ -n "$SESSION_ID" && -n "$SUBAGENT_TYPE" ]] || { echo \'{"hookSpecificOutput":{"permissionDecision":"deny"}}\'; exit 2; }')
p.write_text(s)
PY
run "M3 fail-closed" expect-red "$T::TestFailOpen"
restore "$HOOK"

echo
echo "== M3b: fail-CLOSED at the tool-name gate (the path M3's inputs REACH) =="
python3 - <<'PY'
import pathlib
p = pathlib.Path("conexus/hooks/scripts/agent-dispatch-expect.sh")
s = p.read_text().replace(
    '[[ "$TOOL_NAME" == "Agent" || "$TOOL_NAME" == "Task" ]] || exit 0',
    '[[ "$TOOL_NAME" == "Agent" || "$TOOL_NAME" == "Task" ]] || { echo DENY; exit 2; }')
p.write_text(s)
PY
run "M3b fail-closed (reached path)" expect-red "$T::TestFailOpen"
restore "$HOOK"

echo
echo "== M3c: revert the empty-field parse to IFS=tab (the collapse bug) =="
python3 - <<'PY'
import pathlib
p = pathlib.Path("conexus/hooks/scripts/agent-dispatch-expect.sh")
s = p.read_text()
s = s.replace("IFS=$'\\x1f' read -r", "IFS=$'\\t' read -r")
s = s.replace('print("\\x1f".join(', 'print("\\t".join(')
p.write_text(s)
PY
run "M3c IFS collapse" expect-red "$T::TestFailOpen::test_empty_field_does_not_shift_the_parse"
restore "$HOOK"

echo
echo "== M4: ignore run_in_background (mark everything background) =="
python3 - <<'PY'
import pathlib
p = pathlib.Path("conexus/hooks/scripts/agent-dispatch-expect.sh")
s = p.read_text().replace(
    'bg = ti.get("run_in_background", True)', 'bg = True')
p.write_text(s)
PY
run "M4 no sync/bg discrimination" expect-red "$T::TestSyncVsBackground"
restore "$HOOK"

echo
echo "== M5: restore the unrecognised free pass (the pre-houpu defect) =="
# Was "recogniser back to morphology-only". That mutation edited two awk
# lines containing `named[id]`, which nexus-houpu DELETED — the replaces
# silently matched nothing and the mutation stopped mutating. A mutation
# that applies no diff proves the suite is green about nothing, so this one
# ASSERTS it landed (see the vacuous-gate doctrine in AGENTS.md).
#
# The equivalent defect under type keying is the free pass itself: skip any
# START whose type has no EXPECT row instead of naming it. That is exactly
# what hid a half-working dispatch hook before houpu.
python3 - <<'PY'
import pathlib, sys
PAIRS = [
    # expectations_undeclared: skip a START whose type was never declared
    ("                if (t in e) recognized++",
     "                if (t in e) recognized++; else continue"),
    # expectations_census: same free pass in the per-agent view
    ("                    if (ty in expect) recognized++",
     "                    if (ty in expect) recognized++; else continue"),
]
for f in ("tests/e2e/lib/expectations.sh", "conexus/hooks/scripts/expectations.sh"):
    p = pathlib.Path(f)
    s = p.read_text()
    for needle, repl in PAIRS:
        if needle not in s:
            sys.exit(f"M5 mutation did not apply: needle absent from {f}:\n"
                     f"  {needle!r}\n"
                     "the mutation would have been vacuous; fix the needle.")
        s = s.replace(needle, repl, 1)
    p.write_text(s)
PY
run "M5 unrecognised free pass" expect-red \
    "tests/hooks/test_subagent_stop_hook.py::TestNamedBackgroundDispatchAt2_1_251::test_named_background_dispatch_undeclared_without_expect_row" \
    "tests/hooks/test_subagent_stop_hook.py::TestNamedBackgroundDispatchAt2_1_251::test_census_names_a_start_whose_type_was_never_declared"
restore "$LIB" "$PLUGIN_LIB"

echo
echo "== M6: drop the tool_use_id 5th field =="
python3 - <<'PY'
import pathlib
p = pathlib.Path("conexus/hooks/scripts/agent-dispatch-expect.sh")
s = p.read_text().replace(
    'expectations_expect "$SESSION_ID" "$SUBAGENT_TYPE" "$DISPATCH_MODE" "$DISPATCH_ID"',
    'expectations_expect "$SESSION_ID" "$SUBAGENT_TYPE" "$DISPATCH_MODE"')
p.write_text(s)
PY
run "M6 no dispatch id" expect-red "$T::TestWritesTheRow::test_row_carries_the_dispatch_tool_use_id" "$T::TestIdempotence"
restore "$HOOK"

echo
echo "== M7: N-of-type credit back to set membership =="
python3 - <<'PY'
import pathlib
for f in ("tests/e2e/lib/expectations.sh", "conexus/hooks/scripts/expectations.sh"):
    p = pathlib.Path(f)
    s = p.read_text()
    s = s.replace("if (credit[t] > 0) { credit[t]--; continue }   # N-of-type",
                  "if (t in e) { continue }")
    s = s.replace('if (credit[ty] > 0) { credit[ty]--; d = "declared" }',
                  'if (ty in expect) { d = "declared" }')
    p.write_text(s)
PY
run "M7 set membership" expect-red "$T::TestSameTypeDispatchedTwice::test_partial_mechanization_leaves_a_deficit"
restore "$LIB" "$PLUGIN_LIB"

echo
echo "== M8: plugin lib copy drifts from the reference =="
printf '\n# drift\n' >> "$PLUGIN_LIB"
run "M8 parity drift" expect-red "$T::TestPluginWiring::test_shellib_parity_with_reference"
restore "$PLUGIN_LIB"

echo
echo "== M9: ledger entry removed =="
# The target test returns early once the marketplace pin is >= 7.0.0 (the
# hook shipped there; re-drift is the generic drift-ledger tests' job), so
# past that pin this mutation has NO subject: removing an entry that is not
# there and expecting red is a vacuous pin, not a gate. Skip it out loud
# rather than let it print "stayed GREEN" every run after a release.
M9_PIN="$(python3 -c 'import json;print(json.load(open(".claude-plugin/marketplace.json"))["plugins"][0]["source"]["ref"])')"
if python3 -c 'import sys;v=tuple(int(x) for x in sys.argv[1].lstrip("v").split("."));sys.exit(0 if v >= (7,0,0) else 1)' "$M9_PIN"; then
    echo "M9 SKIPPED: pin $M9_PIN >= v7.0.0 — the hook is in the pinned tag and test_declared_in_pending_release_ledger returns early by design; nothing to mutate"
else
python3 - <<'PY'
import pathlib, re
p = pathlib.Path("conexus/PENDING_RELEASE.md")
s = p.read_text()
s = re.sub(r"- `conexus/hooks/scripts/agent-dispatch-expect\.sh`.*?(?=\n- `)", "", s, flags=re.S)
p.write_text(s)
PY
run "M9 undeclared drift" expect-red "$T::TestPluginWiring::test_declared_in_pending_release_ledger"
restore conexus/PENDING_RELEASE.md
fi

echo
echo "== M10: reader-side dispatch_id dedup removed (review finding 1) =="
python3 - <<'PY'
import pathlib
for f in ("tests/e2e/lib/expectations.sh", "conexus/hooks/scripts/expectations.sh"):
    p = pathlib.Path(f)
    s = p.read_text().replace(
        '$2 == "EXPECT" && $5 != "" && ($5 in dseen) { next }', "")
    p.write_text(s)
PY
run "M10 no dedup by dispatch id" expect-red "$T::TestIdempotence::test_duplicate_rows_do_not_inflate_the_credit_pool"
restore "$LIB" "$PLUGIN_LIB"

echo
echo "== M11: stale-lockdir reaping removed (review finding 3) =="
python3 - <<'PY'
import pathlib, re
p = pathlib.Path("conexus/hooks/scripts/agent-dispatch-expect.sh")
s = p.read_text()
s = re.sub(r'if \[\[ -d "\$LOCKDIR" \]\].*?\nfi\n', "", s, flags=re.S, count=1)
p.write_text(s)
PY
run "M11 no stale-lock reaping" expect-red "$T::TestIdempotence::test_stale_lockdir_is_reaped"
restore "$HOOK"

echo
echo "== restored: post-mutation baseline =="
run "post-restore baseline" expect-green "$T"
git status --porcelain
