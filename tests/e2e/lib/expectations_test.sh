#!/usr/bin/env bash
# tests/e2e/lib/expectations_test.sh — unit-level shell tests for
# expectations.sh (RDR-184 P1.1, nexus-ccs9v.7). Self-provisioning: private
# XDG_STATE_HOME in a throwaway tmpdir, no ambient state, no dependency on
# any other harness. Run directly: `bash tests/e2e/lib/expectations_test.sh`.
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/expectations_test.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT
export XDG_STATE_HOME="$WORKDIR/state"

# shellcheck source=./expectations.sh disable=SC1091
source "$HERE/expectations.sh"

PASS=0
FAIL=0

ok() {
    echo "  [ok] $1"
    PASS=$((PASS + 1))
}
bad() {
    echo "  [FAIL] $1"
    FAIL=$((FAIL + 1))
}

SID="sess-1e9c9a90"

# ── Test 1: file path shape + private parent dir ─────────────────────────
echo "Test 1: expectations_file path + private parent dir"
f="$(expectations_file "$SID")"
if [[ "$f" == "$WORKDIR/state/nexus/orchestration/$SID.expectations" ]]; then
    ok "path honors XDG_STATE_HOME and the <session_id>.expectations shape"
else
    bad "unexpected path: $f"
fi
if [[ -d "$WORKDIR/state/nexus/orchestration" ]]; then
    ok "parent dir created on first use"
    perms="$(stat -f '%Lp' "$WORKDIR/state/nexus/orchestration" 2>/dev/null || stat -c '%a' "$WORKDIR/state/nexus/orchestration")"
    if [[ "$perms" == "700" ]]; then
        ok "parent dir is 0700 (private)"
    else
        bad "parent dir perms are $perms, expected 700"
    fi
else
    bad "parent dir not created"
fi

# ── Test 2: EXPECT write — exact TSV row, file 0600, append-only ─────────
echo "Test 2: EXPECT write is an exact appended TSV row"
if expectations_expect "$SID" "worker-a" "background"; then
    ok "expectations_expect accepted a valid name+mode"
else
    bad "expectations_expect refused a valid name+mode"
fi
row="$(tail -1 "$f")"
IFS=$'\t' read -r ts verb name mode <<<"$row"
if [[ "$verb" == "EXPECT" && "$name" == "worker-a" && "$mode" == "background" ]]; then
    ok "row fields exact: EXPECT / worker-a / background"
else
    bad "row fields wrong: verb='$verb' name='$name' mode='$mode' (raw: $row)"
fi
if [[ "$ts" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]]; then
    ok "timestamp is ISO-8601 UTC"
else
    bad "timestamp malformed: '$ts'"
fi
perms="$(stat -f '%Lp' "$f" 2>/dev/null || stat -c '%a' "$f")"
if [[ "$perms" == "600" ]]; then
    ok "expectations file is 0600"
else
    bad "expectations file perms are $perms, expected 600"
fi
expectations_expect "$SID" "worker-b" "sync"
if [[ "$(wc -l <"$f" | tr -d ' ')" == "2" ]]; then
    ok "second write appended (2 rows, first row untouched)"
else
    bad "expected 2 rows, got $(wc -l <"$f")"
fi

# ── Test 2b: racing first-writers never lose rows ────────────────────────
# The creation path must be one O_APPEND|O_CREAT open. The pre-review
# check-then-truncate version silently wiped earlier racers' rows when
# several writers all observed "file doesn't exist" (critic finding,
# reproduced pre-fix with a widened window). HONEST SCOPE: the truncate
# race window in the exact old code is sub-millisecond and this test does
# NOT deterministically hit it (verified: old code passes unwidened 3/3;
# a 0-0.2s widening reds it 39/40-lost) — bash builtins cannot be
# PATH-shadow-paused, so no deterministic choreography exists. The fix is
# correct BY CONSTRUCTION (single atomic open, no truncate step to race);
# this test guards O_APPEND line-grain atomicity and catches any future
# variant with a widened create window.
echo "Test 2b: concurrent first-writers to a fresh session file lose nothing"
RACE_SID="race-session"
racef="$(expectations_file "$RACE_SID")"
rm -f "$racef"
for i in $(seq 1 40); do
    ( source "$HERE/expectations.sh"; expectations_expect "$RACE_SID" "racer-$i" "background" ) &
done
wait
rows="$(wc -l <"$racef" | tr -d ' ')"
if [[ "$rows" == "40" ]]; then
    ok "40 concurrent first-writes -> 40 rows (no create-race row loss)"
else
    bad "expected 40 rows after concurrent first-writes, got $rows (create-race lost rows)"
fi
malformed="$(awk -F'\t' 'NF != 4 || $2 != "EXPECT"' "$racef" | wc -l | tr -d ' ')"
if [[ "$malformed" == "0" ]]; then
    ok "all 40 rows intact TSV (O_APPEND line-grain atomicity held)"
else
    bad "$malformed malformed/interleaved rows under concurrent append"
fi
rm -f "$racef"

# ── Test 3: EXPECT input validation — reject, write nothing ──────────────
echo "Test 3: EXPECT rejects malformed input without writing"
before="$(wc -l <"$f" | tr -d ' ')"
if expectations_expect "$SID" "worker-a" "later" 2>/dev/null; then
    bad "invalid mode 'later' was accepted"
else
    ok "invalid mode rejected"
fi
if expectations_expect "$SID" "" "background" 2>/dev/null; then
    bad "empty name was accepted"
else
    ok "empty name rejected"
fi
if expectations_expect "$SID" "$(printf 'evil\tname')" "background" 2>/dev/null; then
    bad "tab-bearing name was accepted (would corrupt the TSV)"
else
    ok "tab-bearing name rejected"
fi
if expectations_expect "$SID" "bad name" "background" 2>/dev/null; then
    bad "space-bearing name was accepted (Agent-tool names are [A-Za-z0-9_-])"
else
    ok "space-bearing name rejected"
fi
after="$(wc -l <"$f" | tr -d ' ')"
if [[ "$before" == "$after" ]]; then
    ok "no rows written by any rejected call"
else
    bad "rejected calls still appended rows ($before -> $after)"
fi

# ── Test 4: consult rule — named background teammate OWES ────────────────
# Scenario-27 morphology (bd nexus-ccs9v.7 determination): a NAMED agent
# arrives at the stop hook with agent_type == <name> and
# agent_id == "a<name>-<hash>".
echo "Test 4: named background teammate owes a report"
if expectations_owes_report "$SID" "aworker-a-6f59dab8bbb14864" "worker-a"; then
    ok "EXPECT(background) + named morphology -> owes"
else
    bad "named background teammate not recognized as owing"
fi

# ── Test 5: sync EXPECT row never owes ───────────────────────────────────
echo "Test 5: sync dispatch never owes (false-block immunity by construction)"
if expectations_owes_report "$SID" "aworker-b-1234567890abcdef" "worker-b"; then
    bad "EXPECT(sync) agent reported as owing — sync must be unblockable"
else
    ok "EXPECT(sync) -> never owes"
fi

# ── Test 6: no EXPECT row never owes ─────────────────────────────────────
echo "Test 6: un-expected agent never owes"
if expectations_owes_report "$SID" "astranger-aaaaaaaaaaaaaaaa" "stranger"; then
    bad "agent with no EXPECT row reported as owing"
else
    ok "no EXPECT row -> never owes"
fi

# ── Test 7: subagent_type collision immunity via morphology ──────────────
# An UNNAMED dispatch (sync Task or background) has agent_id "a<hash>" (no
# "a<name>-" prefix) and agent_type = the real subagent_type. Even if an
# EXPECT(background) row exists for a name equal to that subagent_type,
# the morphology check must refuse the match — otherwise every sync
# general-purpose Task would be blockable the moment any background
# general-purpose teammate was expected.
echo "Test 7: unnamed agent never matches an EXPECT row for its subagent_type"
expectations_expect "$SID" "general-purpose" "background"
if expectations_owes_report "$SID" "a16b397f79df79c42" "general-purpose"; then
    bad "unnamed agent (agent_id 'a<hash>') matched via bare agent_type — morphology check missing"
else
    ok "unnamed morphology (no 'a<name>-' prefix) -> never owes, even on name collision"
fi

# ── Test 8: name mismatch / prefix-substring immunity ────────────────────
echo "Test 8: exact-name matching only (no prefix/substring bleed)"
expectations_expect "$SID" "probe" "background"
if expectations_owes_report "$SID" "aprobeB-1234567890abcdef" "probeB"; then
    bad "EXPECT 'probe' matched agent named 'probeB' (substring bleed)"
else
    ok "EXPECT 'probe' does not match agent 'probeB'"
fi
if expectations_owes_report "$SID" "aprobe-1234567890abcdef" "probe"; then
    ok "EXPECT 'probe' still matches agent 'probe' exactly"
else
    bad "exact-name match broken for 'probe'"
fi

# ── Test 9: fail-open — missing file never owes, never errors ────────────
echo "Test 9: missing expectations file fails OPEN"
if expectations_owes_report "no-such-session" "aworker-a-6f59dab8bbb14864" "worker-a"; then
    bad "missing file reported an agent as owing (fail-closed — would brick every stop)"
else
    ok "missing file -> never owes (fail-open)"
fi

# ── Test 10: BLOCKED once-guard ──────────────────────────────────────────
echo "Test 10: BLOCKED once-guard is exact-id, write-once semantics"
if expectations_already_blocked "$SID" "aworker-a-6f59dab8bbb14864"; then
    bad "agent reported blocked before any BLOCKED row"
else
    ok "not blocked before marking"
fi
expectations_mark_blocked "$SID" "aworker-a-6f59dab8bbb14864"
if expectations_already_blocked "$SID" "aworker-a-6f59dab8bbb14864"; then
    ok "blocked after marking"
else
    bad "BLOCKED row not detected after marking"
fi
# Prefix-substring immunity on ids, BOTH containment directions: a marked
# short id must not cover a longer query, and a marked long id must not
# cover a shorter query (a substring/index-style matcher passes one
# direction and fails the other — mutation-verified).
expectations_mark_blocked "$SID" "aX-1"
if expectations_already_blocked "$SID" "aX-12"; then
    bad "BLOCKED 'aX-1' matched query 'aX-12' (substring bleed, short-marked direction)"
else
    ok "BLOCKED id matching is exact (marked aX-1 does not cover query aX-12)"
fi
expectations_mark_blocked "$SID" "aY-12"
if expectations_already_blocked "$SID" "aY-1"; then
    bad "BLOCKED 'aY-12' matched query 'aY-1' (substring bleed, long-marked direction)"
else
    ok "BLOCKED id matching is exact (marked aY-12 does not cover query aY-1)"
fi

# ── Test 10b: mark_blocked rejects TSV-corrupting agent_id ───────────────
echo "Test 10b: mark_blocked rejects tab/newline agent_id without writing"
before="$(wc -l <"$f" | tr -d ' ')"
if expectations_mark_blocked "$SID" "$(printf 'evil\tid')" 2>/dev/null; then
    bad "tab-bearing agent_id was accepted (would misalign the BLOCKED row and defeat once-guard)"
else
    ok "tab-bearing agent_id rejected"
fi
after="$(wc -l <"$f" | tr -d ' ')"
if [[ "$before" == "$after" ]]; then
    ok "no row written by the rejected mark_blocked call"
else
    bad "rejected mark_blocked still appended a row"
fi

# ── Test 10c: glob-metachar agent_type can never owe ─────────────────────
# The morphology gate interpolates agent_type into a glob pattern. Even if
# a FOREIGN writer plants an EXPECT row with a metachar name (impossible
# through expectations_expect, but the file is just a file), the consult
# rule must refuse it: agent_type outside the name charset never owes.
echo "Test 10c: glob-metachar agent_type refused even with a matching foreign row"
printf '%s\tEXPECT\t*\tbackground\n' "2026-07-17T00:00:00Z" >>"$f"
if expectations_owes_report "$SID" "a*-deadbeef" "*"; then
    bad "agent_type '*' owed via a foreign EXPECT row (charset gate missing — glob interpolation live)"
else
    ok "agent_type outside the name charset never owes, even with a foreign '*' row present"
fi

# ── Test 10d: session_id path traversal refused ──────────────────────────
# session_id lands in a filesystem path; a traversal-bearing id must never
# escape the private 0700 orchestration dir (critic finding, reproduced:
# '../../evil' wrote evil.expectations two levels up pre-guard).
echo "Test 10d: traversal-bearing session_id refused on every surface"
if expectations_expect "../../evil" "worker-a" "background" 2>/dev/null; then
    bad "expectations_expect accepted session_id '../../evil'"
else
    ok "write path rejects traversal session_id loudly"
fi
if [[ -e "$WORKDIR/state/evil.expectations" || -e "$WORKDIR/evil.expectations" ]]; then
    bad "traversal session_id escaped the orchestration dir"
else
    ok "nothing written outside the orchestration dir"
fi
if expectations_owes_report "../../evil" "aworker-a-6f59dab8bbb14864" "worker-a"; then
    bad "consult path reported owes for a traversal session_id (must fail open)"
else
    ok "consult path fails open on traversal session_id"
fi

# ── Test 11: START stamp row (non-load-bearing backfill) ─────────────────
echo "Test 11: START row records agent_id + agent_type verbatim"
expectations_start "$SID" "aworker-a-6f59dab8bbb14864" "worker-a"
row="$(tail -1 "$f")"
IFS=$'\t' read -r ts verb aid atype <<<"$row"
if [[ "$verb" == "START" && "$aid" == "aworker-a-6f59dab8bbb14864" && "$atype" == "worker-a" ]]; then
    ok "START row fields exact"
else
    bad "START row wrong: verb='$verb' aid='$aid' atype='$atype'"
fi

# ── Test 12: a malformed line costs one entry, not the session ───────────
echo "Test 12: junk line in the file does not break the consult rule"
echo "this is not a tsv row at all" >>"$f"
if expectations_owes_report "$SID" "aworker-a-6f59dab8bbb14864" "worker-a"; then
    ok "consult rule still works with a junk line present"
else
    bad "one junk line broke the consult rule (fail-closed per line, should be per entry)"
fi

# ── Test 13: sweep removes only stale files ──────────────────────────────
echo "Test 13: sweep removes >7d-old files, keeps fresh ones"
oldf="$(expectations_file "old-session")"
: >"$oldf"
touch -t 202601010000 "$oldf"
freshf="$(expectations_file "$SID")"
expectations_sweep
if [[ ! -e "$oldf" ]]; then
    ok "stale (>7d) expectations file swept"
else
    bad "stale file survived the sweep"
fi
if [[ -e "$freshf" ]]; then
    ok "fresh expectations file kept"
else
    bad "sweep deleted a fresh file"
fi

echo ""
echo "expectations_test.sh: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]] || exit 1
exit 0
