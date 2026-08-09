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

# ── Test 7: unnamed dispatch matches by agent_type (morphology dropped) ──
# STALE-ASSERTION FIX (nexus-a4nun / nexus-3zu8g, 2026-08-09): this test
# originally asserted that the named-agent morphology gate ("a<name>-...")
# must refuse an unnamed dispatch's match against an EXPECT row for its
# bare subagent_type. That gate was deliberately REMOVED at nexus-hbr4x
# (design T1 d40a5b53, 2026-08-03, see expectations_owes_report's own
# header comment "WHY TYPE-KEYING AT ALL, NOT MORPHOLOGY"): neither
# SubagentStart nor SubagentStop carries a name field (the Agent tool has
# no name parameter), so the morphology check was UNREACHABLE from every
# real dispatch and owes_report returned 1 (never owes) across 18/18 and
# 19/19 measured sessions — the guard had literally never fired. Dropping
# morphology and keying on agent_type alone (gated by the per-type
# UNMIXED-ness check, nexus-rkigh) is what makes it fire at all; the
# accepted price is documented in owes_report's "ACCEPTED PRICE" comment.
# An UNNAMED dispatch (agent_id "a<hash>", no "a<name>-" prefix) now
# correctly MATCHES an EXPECT(background) row for its bare agent_type when
# no sync EXPECT of that type exists (unmixed) — this is the intended
# current contract, not a regression.
echo "Test 7: unnamed dispatch matches an EXPECT row for its subagent_type (morphology dropped, nexus-hbr4x)"
expectations_expect "$SID" "general-purpose" "background"
if expectations_owes_report "$SID" "a16b397f79df79c42" "general-purpose"; then
    ok "unnamed dispatch (agent_id 'a<hash>') matches via bare agent_type -> owes (per-type consult, nexus-hbr4x)"
else
    bad "unnamed dispatch failed to match its EXPECT row via agent_type — per-type consult regressed"
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

# ── Test 12b: undeclared-dispatch audit (the .16 retro query) ────────────
# The Phase-2 critique (Critical-1) proved the original markdown-embedded
# awk false-flagged every ordinary SYNC dispatch (no morphology filter,
# untested). The audit is now a tested function: flag ONLY named-
# morphology START rows (agent_id == "a<agent_type>-<hash>") that have
# no EXPECT row for that name; an EXPECT row of EITHER mode suppresses
# (a deliberately-declared named-sync dispatch stays audit-clean).
echo "Test 12b: undeclared audit flags only undeclared NAMED dispatches"
AUD_SID="audit-session"
audf="$(expectations_file "$AUD_SID")"
rm -f "$audf"
# sync unnamed dispatch: START row with subagent_type + hash id -> never flagged
expectations_start "$AUD_SID" "a16b397f79df79c42" "general-purpose"
# named background teammate, DECLARED -> not flagged
expectations_expect "$AUD_SID" "declared-bg" "background"
expectations_start "$AUD_SID" "adeclared-bg-1234567890abcdef" "declared-bg"
# named dispatch declared SYNC -> suppressed
expectations_expect "$AUD_SID" "declared-sync" "sync"
expectations_start "$AUD_SID" "adeclared-sync-1234567890abcd" "declared-sync"
# named teammate, UNDECLARED -> the one true positive
expectations_start "$AUD_SID" "arogue-bg-1234567890abcdef" "rogue-bg"
out="$(expectations_undeclared "$AUD_SID")"
if [[ "$(printf '%s\n' "$out" | grep -c "UNDECLARED")" == "1" ]]; then
    ok "exactly one UNDECLARED line for four mixed dispatches"
else
    bad "expected exactly 1 UNDECLARED, got: $out"
fi
if printf '%s\n' "$out" | grep -q $'UNDECLARED\tarogue-bg-1234567890abcdef\trogue-bg'; then
    ok "the undeclared named teammate is the one flagged"
else
    bad "wrong/missing UNDECLARED line: $out"
fi
if printf '%s\n' "$out" | grep -qE "general-purpose|declared-bg|declared-sync"; then
    bad "audit flagged a sync or declared dispatch (cry-wolf class): $out"
else
    ok "sync-unnamed and both declared dispatches not flagged"
fi
# nexus-ahl9v: a session with no ledger file is NOT evidence of
# cleanliness -- it must be distinguishable (rc=3 + stderr NOTE) from a
# genuinely clean session (rc=0), not collapsed into the same fail-open
# 0 the live stop-guard callers use.
no_ledger_err="$(expectations_undeclared "no-such-audit-session" 2>&1 1>/dev/null)"
no_ledger_rc=$?
if [[ "$no_ledger_rc" == "3" ]]; then
    ok "missing file: audit exits 3 (no-ledger, distinct from clean)"
else
    bad "missing file: expected rc=3, got rc=$no_ledger_rc"
fi
if printf '%s\n' "$no_ledger_err" | grep -q "NOTE"; then
    ok "missing file: NOTE line printed to stderr"
else
    bad "missing file: no NOTE line on stderr: $no_ledger_err"
fi
rm -f "$audf"

# ── Test 13: census — scripted counts (nexus-hybv1) ──────────────────────
echo "Test 13: expectations_census classification + dedup"
CSID="sess-census-1"
cf="$(expectations_file "$CSID")"
{
    # reviewer: clean report. critic: blocked then resolved (post-block
    # REPORTED stamp, no strength field = immediate). ghost: blocked,
    # never resolved. rogue: named START with no EXPECT (undeclared).
    # phantom: EXPECT with no START. flaky: REPORTED then a LATER
    # unresolved BLOCKED (review 21032 Critical 1 — last state must win).
    # noshow: BLOCKED with no START row at all (review 21032 Critical 2 /
    # nexus-0s0o1 — must still appear per-agent). slowres: blocked then
    # resolved with strength "later" (weak causal evidence, split out).
    # Every hook-written row doubled — the nexus-3h0u6 legacy shape.
    printf '2026-07-19T15:27:00Z\tEXPECT\treviewer-x\tbackground\n'
    printf '2026-07-19T15:27:00Z\tEXPECT\tcritic-x\tbackground\n'
    printf '2026-07-19T15:27:00Z\tEXPECT\tghost-x\tbackground\n'
    printf '2026-07-19T15:27:00Z\tEXPECT\tphantom-x\tbackground\n'
    printf '2026-07-19T15:27:00Z\tEXPECT\tflaky-x\tbackground\n'
    printf '2026-07-19T15:27:00Z\tEXPECT\tslowres-x\tbackground\n'
    printf '2026-07-19T15:27:17Z\tSTART\tareviewer-x-1b8b\treviewer-x\n'
    printf '2026-07-19T15:27:17Z\tSTART\tareviewer-x-1b8b\treviewer-x\n'
    printf '2026-07-19T15:27:31Z\tSTART\tacritic-x-efef\tcritic-x\n'
    printf '2026-07-19T15:27:31Z\tSTART\tacritic-x-efef\tcritic-x\n'
    printf '2026-07-19T15:27:40Z\tSTART\taghost-x-aaaa\tghost-x\n'
    printf '2026-07-19T15:27:50Z\tSTART\tarogue-x-bbbb\trogue-x\n'
    printf '2026-07-19T15:27:55Z\tSTART\taflaky-x-dddd\tflaky-x\n'
    printf '2026-07-19T15:27:58Z\tSTART\taslowres-x-eeee\tslowres-x\n'
    printf '2026-07-19T15:30:00Z\tREPORTED\taflaky-x-dddd\n'
    printf '2026-07-19T15:33:49Z\tBLOCKED\tacritic-x-efef\n'
    printf '2026-07-19T15:33:49Z\tBLOCKED\tacritic-x-efef\n'
    printf '2026-07-19T15:33:55Z\tBLOCKED\taghost-x-aaaa\n'
    printf '2026-07-19T15:33:58Z\tBLOCKED\tanoshow-x-cccc\n'
    printf '2026-07-19T15:34:02Z\tBLOCKED\taslowres-x-eeee\n'
    printf '2026-07-19T15:34:12Z\tREPORTED\tareviewer-x-1b8b\n'
    printf '2026-07-19T15:34:12Z\tREPORTED\tareviewer-x-1b8b\n'
    printf '2026-07-19T15:34:20Z\tREPORTED\tacritic-x-efef\n'
    printf '2026-07-19T15:40:00Z\tBLOCKED\taflaky-x-dddd\n'
    printf '2026-07-19T15:41:00Z\tREPORTED\taslowres-x-eeee\tlater\n'
} >"$cf"
census="$(expectations_census "$CSID")"
if grep -q $'AGENT\tareviewer-x-1b8b\treviewer-x\tREPORTED\tdeclared' <<<"$census"; then
    ok "clean reporter classified REPORTED"
else
    bad "reviewer classification wrong: $census"
fi
if grep -q $'AGENT\tacritic-x-efef\tcritic-x\tBLOCKED_RESOLVED\tdeclared' <<<"$census"; then
    ok "blocked-then-reported classified BLOCKED_RESOLVED (guard success)"
else
    bad "critic classification wrong"
fi
if grep -q $'AGENT\taghost-x-aaaa\tghost-x\tBLOCKED_UNRESOLVED\tdeclared' <<<"$census"; then
    ok "bare block classified BLOCKED_UNRESOLVED"
else
    bad "ghost classification wrong"
fi
if grep -q $'AGENT\tarogue-x-bbbb\trogue-x\tNO_TERMINAL\tundeclared' <<<"$census"; then
    ok "undeclared named START flagged undeclared"
else
    bad "rogue classification wrong"
fi
if grep -q $'EXPECTED_NO_START\tphantom-x' <<<"$census"; then
    ok "EXPECT with no START surfaced"
else
    bad "phantom missing from census"
fi
if grep -q $'AGENT\taflaky-x-dddd\tflaky-x\tBLOCKED_UNRESOLVED\tdeclared' <<<"$census"; then
    ok "REPORTED-then-later-BLOCKED classifies BLOCKED_UNRESOLVED (last state wins — review 21032 C1)"
else
    bad "flaky classification wrong: $(grep aflaky <<<"$census")"
fi
if grep -q $'AGENT\tanoshow-x-cccc\t-\tBLOCKED_UNRESOLVED\tno-start' <<<"$census"; then
    ok "BLOCKED with no START row still appears per-agent (review 21032 C2 / nexus-0s0o1)"
else
    bad "noshow missing/wrong: $(grep anoshow <<<"$census")"
fi
if grep -q $'AGENT\taslowres-x-eeee\tslowres-x\tBLOCKED_RESOLVED\tdeclared' <<<"$census"; then
    ok "later-strength resolution still folds to BLOCKED_RESOLVED per-agent"
else
    bad "slowres classification wrong"
fi
# Doubled rows must count ONCE: blocked=5 (critic, ghost, noshow, slowres,
# flaky), not 6 (critic doubled).
if grep -q 'ROWS	expect=6 start=6 reported=4 blocked=5 wouldblock=0' <<<"$census"; then
    ok "3h0u6-doubled rows deduplicated in ROWS counts"
else
    bad "ROWS counts wrong: $(grep ROWS <<<"$census")"
fi
if grep -q 'CLASSIFIED	reported=1 blocked_resolved=2 (immediate=1 later=1) blocked_unresolved=3 wouldblock=0 no_terminal=1 undeclared=1 no_start=1 expected_no_start=1' <<<"$census"; then
    ok "CLASSIFIED summary exact (incl. immediate/later resolution split)"
else
    bad "CLASSIFIED summary wrong: $(grep CLASSIFIED <<<"$census")"
fi
if [[ -z "$(expectations_census "no-such-census-session")" ]]; then
    ok "missing file: census emits nothing, exit 0 (fail-open)"
else
    bad "census produced output for a missing file"
fi
rm -f "$cf"

# ── Test 14: sweep removes only stale files ──────────────────────────────
echo "Test 14: sweep removes >7d-old files, keeps fresh ones"
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

# ── Test 15: Gap-1 blind spot to prompt-named dispatches (nexus-mk3tw) ───
# Reproduces the false-clean reported live 2026-07-24/25 (bead nexus-mk3tw):
# background teammates named only inside the Agent-tool PROMPT text (there
# is no name= field in the dispatch schema for the framework to thread into
# the hook payload — see the header comment above expectations_undeclared)
# arrive at SubagentStart with agent_type == the bare subagent_type and
# agent_id == "a<hash>" — structurally IDENTICAL to an anonymous sync
# dispatch. Both the declaration-completeness audit and the census key
# EXCLUSIVELY on the "a<agent_type>-<hash>" named morphology, so every one
# of these dispatches was invisible to both, and a session with N such
# dispatches and 0 EXPECT rows reported "undeclared=0" — a false all-clear.
echo "Test 15: Gap-1 guard blind spot to prompt-named (unnamed-morphology) dispatches"

# ── frozen copy of the PRE-mk3tw logic (verbatim from the code these two
# functions replaced) — the permanent "old guard" baseline. Kept as an
# inline snapshot rather than a git-history lookup so this regression proof
# does not silently stop proving anything once this fix is committed.
_pre_mk3tw_undeclared() {
    local file="$1"
    awk -F'\t' '
        $2 == "START" && index($3, "a" $4 "-") == 1 && length($3) > length($4) + 2 { s[$3] = $4 }
        $2 == "EXPECT" { e[$3] = 1 }
        END { for (id in s) if (!(s[id] in e)) print "UNDECLARED\t" id "\t" s[id] }
    ' "$file"
}
_pre_mk3tw_census_undeclared_count() {
    local file="$1"
    awk -F'\t' '
        seen[$0]++ { next }
        { rows[$2]++ }
        $2 == "EXPECT"  { expect[$3] = 1 }
        $2 == "START" && index($3, "a" $4 "-") == 1 && length($3) > length($4) + 2 {
            if (!($3 in listed)) { order[++n] = $3; listed[$3] = 1 }
            name[$3] = $4
        }
        $2 == "START"   { started[$4] = 1 }
        END {
            undeclared = 0
            for (i = 1; i <= n; i++) {
                id = order[i]
                if (name[id] != "" && !(name[id] in expect)) undeclared++
            }
            printf "undeclared=%d start=%d\n", undeclared, rows["START"]
        }
    ' "$file"
}

BSID="mk3tw-blindspot"
bsf="$(expectations_file "$BSID")"
rm -f "$bsf"
# 4 prompt-named background dispatches (the bead's first-sighting shape),
# zero EXPECT rows (the mechanism was never invoked / never existed) —
# every START row has UNNAMED morphology: agent_id "a<hash>" with no
# "a<agent_type>-" prefix.
{
    printf '2026-07-25T10:00:00Z\tSTART\ta1a2b3c4d5e6f708\tcode-review-expert\n'
    printf '2026-07-25T10:00:05Z\tSTART\ta9f8e7d6c5b4a302\tsubstantive-critic\n'
    printf '2026-07-25T10:00:10Z\tSTART\ta1122334455667788\tcode-review-expert\n'
    printf '2026-07-25T10:00:15Z\tSTART\taffeeddccbbaa9988\ttest-validator\n'
} >"$bsf"

# ── baseline: the OLD guard passes when it should fail ───────────────────
old_undeclared_out="$(_pre_mk3tw_undeclared "$bsf")"
old_census_line="$(_pre_mk3tw_census_undeclared_count "$bsf")"
if [[ -z "$old_undeclared_out" ]]; then
    ok "BASELINE (old guard): 4 undeclared named-background dispatches produce ZERO UNDECLARED output (the bug, reproduced)"
else
    bad "BASELINE assumption broken — old guard unexpectedly flagged something: $old_undeclared_out"
fi
if [[ "$old_census_line" == "undeclared=0 start=4" ]]; then
    ok "BASELINE (old guard): census-style count reads undeclared=0 despite start=4 (the false-clean, reproduced)"
else
    bad "BASELINE assumption broken — old census-style count: $old_census_line"
fi

# ── fixed guard: same fixture, must NOT read as clean ─────────────────────
new_undeclared_out="$(expectations_undeclared "$BSID")"
new_undeclared_rc=$?
if [[ "$new_undeclared_rc" -ne 0 ]]; then
    ok "FIXED expectations_undeclared: hard-fails (exit $new_undeclared_rc) instead of silently returning 0"
else
    bad "FIXED expectations_undeclared: still exits 0 on a 0-of-4-recognised session"
fi
if grep -q $'SUMMARY\tchecked=4 recognized=0 unrecognized=4 undeclared=0' <<<"$new_undeclared_out"; then
    ok "FIXED expectations_undeclared: SUMMARY distinguishes checked=4 from recognized=0"
else
    bad "FIXED expectations_undeclared: SUMMARY line missing/wrong: $new_undeclared_out"
fi
if grep -q "^BLINDSPOT" <<<"$new_undeclared_out"; then
    ok "FIXED expectations_undeclared: emits an explicit BLINDSPOT line"
else
    bad "FIXED expectations_undeclared: no BLINDSPOT line: $new_undeclared_out"
fi

new_census_out="$(expectations_census "$BSID")"
new_census_rc=$?
if [[ "$new_census_rc" -ne 0 ]]; then
    ok "FIXED expectations_census: hard-fails (exit $new_census_rc) instead of silently returning 0"
else
    bad "FIXED expectations_census: still exits 0 on a 0-of-4-recognised session"
fi
if grep -q $'BLINDSPOT\tchecked=4 recognized=0 unrecognized=4' <<<"$new_census_out"; then
    ok "FIXED expectations_census: BLINDSPOT line reports checked=4 recognized=0 unrecognized=4"
else
    bad "FIXED expectations_census: BLINDSPOT line missing/wrong: $(grep BLINDSPOT <<<"$new_census_out")"
fi
if grep -q 'CLASSIFIED	reported=0 blocked_resolved=0 (immediate=0 later=0) blocked_unresolved=0 wouldblock=0 no_terminal=0 undeclared=0 no_start=0 expected_no_start=0' <<<"$new_census_out"; then
    ok "FIXED expectations_census: CLASSIFIED line unchanged in shape (undeclared=0 alone, without BLINDSPOT, would still read as clean)"
else
    bad "FIXED expectations_census: CLASSIFIED line format regressed: $(grep CLASSIFIED <<<"$new_census_out")"
fi
rm -f "$bsf"

# ── control: mixed session (some recognized) must NOT blindspot-fail ────
# Guards against the fix over-firing: one named+declared dispatch, one
# named+undeclared dispatch (still caught by the pre-existing UNDECLARED
# logic), and two unrecognized (blind-spotted) dispatches. recognized=2 >
# 0, so this must NOT read as BLINDSPOT (rc=1, "recognised NOTHING") even
# though unrecognized > 0.
#
# STALE-ASSERTION FIX (nexus-a4nun / nexus-3zu8g, 2026-08-09): this test
# originally asserted rc==0 ("does not hard-fail"), written before the
# UNDECLARED-DEFICIT exit code (nexus-suuja, see expectations_undeclared's
# header) widened the rc contract to 0=clean / 1=BLINDSPOT / 2=undeclared
# deficit / 3=no-ledger. This fixture deliberately includes a genuine
# undeclared dispatch (rogue-bg — asserted below), so under the CURRENT
# contract the correct, intended result is rc=2 (a real deficit, not a
# blindspot false-clean) — rc==0 would mean "nothing to report", which is
# false here. The guard this CONTROL exists to prove — recognized>0 must
# not fall into the rc=1 BLINDSPOT branch — is captured by asserting
# rc==2 specifically (rc=1 would also make this check fail).
MSID="mk3tw-mixed"
msf="$(expectations_file "$MSID")"
rm -f "$msf"
{
    printf '2026-07-25T10:00:00Z\tEXPECT\tdeclared-bg\tbackground\n'
    printf '2026-07-25T10:00:05Z\tSTART\tadeclared-bg-1234567890abcdef\tdeclared-bg\n'
    printf '2026-07-25T10:00:10Z\tSTART\tarogue-bg-abcdef1234567890\trogue-bg\n'
    printf '2026-07-25T10:00:15Z\tSTART\ta1a2b3c4d5e6f708\tcode-review-expert\n'
    printf '2026-07-25T10:00:20Z\tSTART\ta9f8e7d6c5b4a302\tsubstantive-critic\n'
} >"$msf"
mixed_undeclared_out="$(expectations_undeclared "$MSID")"
mixed_undeclared_rc=$?
if [[ "$mixed_undeclared_rc" -eq 2 ]]; then
    ok "CONTROL: mixed session (recognized=2 > 0) reports the genuine undeclared-deficit (rc=2, nexus-suuja), not BLINDSPOT (rc=1)"
else
    bad "CONTROL: mixed session with 2 recognized dispatches returned rc=$mixed_undeclared_rc, expected rc=2 (undeclared-deficit)"
fi
if grep -q $'UNDECLARED\tarogue-bg-abcdef1234567890\trogue-bg' <<<"$mixed_undeclared_out"; then
    ok "CONTROL: the genuinely undeclared named dispatch is still caught"
else
    bad "CONTROL: undeclared named dispatch not flagged: $mixed_undeclared_out"
fi
if grep -q $'SUMMARY\tchecked=4 recognized=2 unrecognized=2 undeclared=1' <<<"$mixed_undeclared_out"; then
    ok "CONTROL: SUMMARY counts exact (checked=4 recognized=2 unrecognized=2 undeclared=1)"
else
    bad "CONTROL: SUMMARY counts wrong: $mixed_undeclared_out"
fi
rm -f "$msf"

echo ""
echo "expectations_test.sh: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]] || exit 1
exit 0
