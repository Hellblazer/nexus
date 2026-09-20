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
#
# RDR-215 bead nexus-q02nx.21 deleted the twelve bash hook scripts this
# harness used to mutate (agent-dispatch-expect.sh, expectations.sh among
# them) once their hooks.json entries were re-declared to mcp_tools. Every
# mutation below that had a mechanical Python analogue was re-pointed at the
# port (src/nexus/hooks/agent_dispatch_expect.py, already-ported
# src/nexus/hooks/expectations.py); the ones that did not are recorded as
# REMOVED, with the reason, rather than silently dropped. See the M1/M3c/M9
# comments below for exactly which three and why.
set -u
cd "$(git rev-parse --show-toplevel)" || exit 1

HOOK=src/nexus/hooks/agent_dispatch_expect.py
PY_LIB=src/nexus/hooks/expectations.py
T=tests/hooks/test_agent_dispatch_expect.py

# ⛔ REAL GUARD, not just the warning above: refuse outright on a dirty tree
# instead of trusting the comment to be read. Every mutation below is
# reverted with `git checkout --`, which restores the COMMITTED version of
# whatever it touches — so if any of these paths already carries
# uncommitted work, running this harness discards it silently. That
# happened once already (see the header). Checking only the paths this
# harness actually mutates (not the whole tree) lets it run in a checkout
# that has unrelated uncommitted work elsewhere.
MUTATED_PATHS=("$HOOK" "$PY_LIB")
DIRTY="$(git status --porcelain -- "${MUTATED_PATHS[@]}")"
if [[ -n "$DIRTY" ]]; then
    echo "REFUSING TO RUN: uncommitted changes in a file this harness mutates and"
    echo "reverts with 'git checkout --'. Running now would silently DELETE that"
    echo "work. Commit or stash it first, then re-run."
    echo "$DIRTY"
    exit 1
fi

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
echo "== M1: REMOVED (RDR-215 bead nexus-q02nx.21) =="
# M1 mutated hooks.json to drop the PreToolUse registration and asserted
# TestPluginWiring::test_registered_on_agent_pretooluse went red. That test
# and its whole class were deleted (not adapted) in this same bead: the
# bash command string it matched on no longer exists (the entry is now the
# hook_agent_dispatch_expect mcp_tool with no "command" field at all), and
# the wiring invariant it protected does not have a natural home in this
# file's Python-port test suite the way the write-path mutations below do.
# Nothing replaces this mutation. The registration itself is still asserted
# generically by tests/hooks/test_verification_integration.py-style
# hooks.json structure checks and by the plugin's own dead-wire sweeps, but
# neither is a mutation-falsified pin for THIS specific entry.

echo
echo "== M2: key on a literal name instead of subagent_type (the pre-fix defect) =="
python3 - <<'PY'
import pathlib
p = pathlib.Path("src/nexus/hooks/agent_dispatch_expect.py")
s = p.read_text().replace(
    "                _exp.expectations_expect(\n"
    "                    session_id, subagent_type, dispatch_mode, dispatch_id\n"
    "                )",
    "                _exp.expectations_expect(\n"
    '                    session_id, "teammate-1", dispatch_mode, dispatch_id\n'
    "                )")
p.write_text(s)
PY
run "M2 unpairable key" expect-red "$T::TestPairsWithSubagentStart" "$T::TestSameTypeDispatchedTwice"
restore "$HOOK"

echo
echo "== M3: fail-CLOSED on the write path (deny + nonzero on bad input) =="
python3 - <<'PY'
import pathlib
p = pathlib.Path("src/nexus/hooks/agent_dispatch_expect.py")
s = p.read_text().replace(
    "    if not session_id:\n"
    "        _skip(\n"
    '            "agent-dispatch-expect: empty/unparseable session_id — EXPECT row NOT "\n'
    '            f"written for this dispatch (tool_use_id={shown_id})"\n'
    "        )\n"
    "        return HookResult()",
    "    if not session_id:\n"
    "        _skip(\n"
    '            "agent-dispatch-expect: empty/unparseable session_id — EXPECT row NOT "\n'
    '            f"written for this dispatch (tool_use_id={shown_id})"\n'
    "        )\n"
    '        return HookResult(stdout=\'{"decision": "deny"}\', exit_code=2)')
p.write_text(s)
PY
run "M3 fail-closed" expect-red "$T::TestFailOpen"
restore "$HOOK"

echo
echo "== M3b: fail-CLOSED at the tool-name gate (the path M3's inputs REACH) =="
python3 - <<'PY'
import pathlib
p = pathlib.Path("src/nexus/hooks/agent_dispatch_expect.py")
s = p.read_text().replace(
    "    if tool_name not in _DISPATCH_TOOLS:\n"
    "        _skip(\n"
    '            f"agent-dispatch-expect: tool_name \'{tool_name}\' is not Agent/Task — "\n'
    '            f"EXPECT row NOT written for this dispatch (tool_use_id={shown_id})"\n'
    "        )\n"
    "        return HookResult()",
    "    if tool_name not in _DISPATCH_TOOLS:\n"
    "        _skip(\n"
    '            f"agent-dispatch-expect: tool_name \'{tool_name}\' is not Agent/Task — "\n'
    '            f"EXPECT row NOT written for this dispatch (tool_use_id={shown_id})"\n'
    "        )\n"
    '        return HookResult(stdout="DENY", exit_code=2)')
p.write_text(s)
PY
run "M3b fail-closed (reached path)" expect-red "$T::TestFailOpen"
restore "$HOOK"

echo
echo "== M3c: REMOVED (RDR-215 bead nexus-q02nx.21) =="
# M3c reverted the bash script's field decode from its \x1f delimiter back
# to the tab-collapsing IFS=$'\t' read idiom -- a bash parameter-parsing
# defect class (empty fields collapse because tab is IFS whitespace) that
# has no Python analogue at all: the port reads tool_input as a dict
# (tool_input.get("subagent_type")), never positionally, so there is no
# delimiter and nothing to revert. The BEHAVIOUR this pinned (an empty
# subagent_type must not shift a later field into its slot) is still
# covered directly -- see test_empty_field_does_not_shift_the_parse and
# test_empty_session_id_does_not_shift_the_parse in this file -- but there
# is no way to INJECT the bash's specific defect into code that was never
# shaped to have it, so this mutation has nothing left to falsify.

echo
echo "== M4: ignore run_in_background (mark everything background) =="
python3 - <<'PY'
import pathlib
p = pathlib.Path("src/nexus/hooks/agent_dispatch_expect.py")
s = p.read_text().replace(
    '    if isinstance(value, str):\n'
    '        return value.strip().lower() not in ("false", "0", "no", "")\n'
    '    return bool(value)',
    '    return True')
p.write_text(s)
PY
run "M4 no sync/bg discrimination" expect-red "$T::TestSyncVsBackground"
restore "$HOOK"

echo
echo "== M5: restore the unrecognised free pass (the pre-houpu defect) =="
# Was "recogniser back to morphology-only", then (RDR-215 bead nexus-q02nx.9)
# the same defect re-expressed in the two bash copies' awk. Bead
# nexus-q02nx.14 deleted tests/e2e/lib/expectations.sh, the reference those
# needles targeted, so this mutates the PORT (src/nexus/hooks/expectations.py)
# instead. The real expressions, read from that file rather than guessed:
# expectations_undeclared and expectations_census each compute `recognized`
# from `agent_type in expect_types` (undeclared) / `expect_names` (census)
# immediately before consulting credit -- the free pass is skipping straight
# to the next agent when that membership check fails, instead of falling
# through to name the deficit.
#
# The bash-sourcing pins this used to key on
# (TestNamedBackgroundDispatchAt2_1_251's
# test_named_background_dispatch_undeclared_without_expect_row /
# test_census_names_a_start_whose_type_was_never_declared) exercise
# tests/e2e/lib/expectations.sh via _run_undeclared/_run_census and so
# cannot see a mutation applied only to the Python port -- TestFreePass-
# RemovedInPythonPort's two tests call expectations_undeclared /
# expectations_census directly and are what this mutation must turn red.
python3 - <<'PY'
import pathlib, sys
TARGET = "src/nexus/hooks/expectations.py"
PAIRS = [
    # expectations_undeclared: skip a START whose type was never declared
    ("        if agent_type in expect_types:\n"
     "            recognized += 1\n"
     "        if credit.get(agent_type, 0) > 0:",
     "        if agent_type in expect_types:\n"
     "            recognized += 1\n"
     "        else:\n"
     "            continue\n"
     "        if credit.get(agent_type, 0) > 0:"),
    # expectations_census: same free pass in the per-agent view
    ("            if agent_type in expect_names:\n"
     "                recognized += 1\n"
     "            if credit.get(agent_type, 0) > 0:",
     "            if agent_type in expect_names:\n"
     "                recognized += 1\n"
     "            else:\n"
     "                continue\n"
     "            if credit.get(agent_type, 0) > 0:"),
]
p = pathlib.Path(TARGET)
s = p.read_text()
for needle, repl in PAIRS:
    if needle not in s:
        sys.exit(f"M5 mutation did not apply: needle absent from {TARGET}:\n"
                 f"  {needle!r}\n"
                 "the mutation would have been vacuous; fix the needle.")
    s = s.replace(needle, repl, 1)
p.write_text(s)
PY
run "M5 unrecognised free pass" expect-red \
    "tests/hooks/test_subagent_stop_hook.py::TestFreePassRemovedInPythonPort::test_undeclared_names_a_start_with_no_expect_row" \
    "tests/hooks/test_subagent_stop_hook.py::TestFreePassRemovedInPythonPort::test_census_names_a_start_with_no_expect_row"
restore "$PY_LIB"

echo
echo "== M6: drop the tool_use_id 5th field =="
python3 - <<'PY'
import pathlib
p = pathlib.Path("src/nexus/hooks/agent_dispatch_expect.py")
s = p.read_text().replace(
    "                _exp.expectations_expect(\n"
    "                    session_id, subagent_type, dispatch_mode, dispatch_id\n"
    "                )",
    "                _exp.expectations_expect(\n"
    "                    session_id, subagent_type, dispatch_mode\n"
    "                )")
p.write_text(s)
PY
run "M6 no dispatch id" expect-red "$T::TestWritesTheRow::test_row_carries_the_dispatch_tool_use_id" "$T::TestIdempotence"
restore "$HOOK"

echo
echo "== M7: N-of-type credit back to set membership =="
# Same bead-nexus-q02nx.14 retarget as M5: the awk needles that used to hit
# both bash copies no longer have a subject once tests/e2e/lib/expectations.sh
# is deleted, so this mutates src/nexus/hooks/expectations.py's N-of-type
# credit check directly. The real expressions, read from that file: both
# expectations_undeclared and expectations_census gate the
# declared/undeclared verdict on `credit.get(agent_type, 0) > 0` (decrementing
# on each hit) — set membership drops the decrement, so a second START of an
# already-declared type is wrongly waved through instead of counted as a
# deficit.
python3 - <<'PY'
import pathlib, sys
TARGET = "src/nexus/hooks/expectations.py"
PAIRS = [
    # expectations_undeclared: N-of-type credit collapsed to set membership
    ('        if credit.get(agent_type, 0) > 0:\n'
     '            credit[agent_type] -= 1\n'
     '            continue\n'
     '        lines.append(f"UNDECLARED\\t{agent_id}\\t{agent_type}")',
     '        if agent_type in expect_types:\n'
     '            continue\n'
     '        lines.append(f"UNDECLARED\\t{agent_id}\\t{agent_type}")'),
    # expectations_census: same collapse in the per-agent view
    ('            if credit.get(agent_type, 0) > 0:\n'
     '                credit[agent_type] -= 1\n'
     '                declared = "declared"\n'
     '            else:',
     '            if agent_type in expect_names:\n'
     '                declared = "declared"\n'
     '            else:'),
]
p = pathlib.Path(TARGET)
s = p.read_text()
for needle, repl in PAIRS:
    if needle not in s:
        sys.exit(f"M7 mutation did not apply: needle absent from {TARGET}:\n"
                 f"  {needle!r}\n"
                 "the mutation would have been vacuous; fix the needle.")
    s = s.replace(needle, repl, 1)
p.write_text(s)
PY
run "M7 set membership" expect-red \
    "$T::TestSameTypeDispatchedTwice::test_partial_mechanization_leaves_a_deficit_python_port"
restore "$PY_LIB"

echo
echo "== M8: REMOVED (RDR-215 bead nexus-q02nx.14) =="
# M8 asserted the two bash copies stayed byte-identical. Its subject —
# tests/e2e/lib/expectations.sh, the reference side of that comparison — no
# longer exists once bead .14 landed, and the parity tests it targeted
# (test_agent_dispatch_expect.py::TestPluginWiring::test_shellib_parity_with_reference,
# test_subagent_stop_hook.py::TestPluginWiring::test_shellib_parity_with_reference)
# were deleted rather than adapted: a byte-parity test with one side gone
# either errors on a missing file or passes vacuously, and vacuous is worse
# than absent. Nothing replaces this mutation; there is nothing left to
# falsify.

echo
echo "== M9: REMOVED (RDR-215 bead nexus-q02nx.21) =="
# M9 mutated conexus/PENDING_RELEASE.md to drop the agent-dispatch-expect.sh
# declaration line and asserted
# TestPluginWiring::test_declared_in_pending_release_ledger went red. That
# test already self-skipped once the marketplace pin passed v7.0.0 (the
# hook ships in the pinned tag; the drift ledger's own generic tests own
# re-drift from there), and this bead's client pin is 7.54.0, well past
# that boundary -- so this mutation was already a permanent no-op before
# TestPluginWiring was deleted in this same bead. Nothing replaces it.

echo
echo "== M10: reader-side dispatch_id dedup removed (review finding 1) =="
# The target test writes the duplicate EXPECT row directly to the ledger
# file (bypassing the write-side lock and _already_written entirely -- its
# own docstring says so: "holds regardless of whether the lock ever leaks
# one"), so the mechanism it falsifies is the READER's dedup in
# expectations_census, not the writer's in agent_dispatch_expect.py.
python3 - <<'PY'
import pathlib, sys
TARGET = "src/nexus/hooks/expectations.py"
needle = (
    '        if verb == "EXPECT":\n'
    '            dispatch_id = row[4] if len(row) > 4 else ""\n'
    '            if dispatch_id and dispatch_id in seen_dispatch:\n'
    '                continue\n'
    '            if dispatch_id:\n'
    '                seen_dispatch.add(dispatch_id)\n'
)
repl = (
    '        if verb == "EXPECT":\n'
    '            dispatch_id = row[4] if len(row) > 4 else ""\n'
)
p = pathlib.Path(TARGET)
s = p.read_text()
if needle not in s:
    sys.exit(f"M10 mutation did not apply: needle absent from {TARGET}")
s = s.replace(needle, repl, 1)
p.write_text(s)
PY
run "M10 no dedup by dispatch id" expect-red "$T::TestIdempotence::test_duplicate_rows_do_not_inflate_the_credit_pool"
restore "$PY_LIB"

echo
echo "== M11: stale-lockdir reaping removed (review finding 3) =="
python3 - <<'PY'
import pathlib
p = pathlib.Path("src/nexus/hooks/agent_dispatch_expect.py")
s = p.read_text().replace(
    '    try:\n'
    '        if os.path.isdir(lockdir) and (time.time() - os.stat(lockdir).st_mtime) > 60:\n'
    '            os.rmdir(lockdir)\n'
    '    except OSError:\n'
    '        pass\n',
    '')
p.write_text(s)
PY
run "M11 no stale-lock reaping" expect-red "$T::TestIdempotence::test_stale_lockdir_is_reaped"
restore "$HOOK"

echo
echo "== restored: post-mutation baseline =="
run "post-restore baseline" expect-green "$T"
git status --porcelain
