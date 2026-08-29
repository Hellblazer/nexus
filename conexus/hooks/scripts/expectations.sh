#!/usr/bin/env bash
# tests/e2e/lib/expectations.sh — RDR-184 P1.1 expectations file (bead
# nexus-ccs9v.7): the session-scoped, orchestrator-written ground truth for
# "which agents owe a completion report". Sourced (like lock.sh), never
# executed. Design note: T2 nexus/design-rdr184-p7-expectations-file.
#
# WHY THIS FILE EXISTS (Gap 1, idle-without-report): the SubagentStop hook
# needs to know whether the stopping agent is a background teammate that
# owes the orchestrator a report. Empirical determination (cc-validation
# scenario 27, bd nexus-ccs9v.7 2026-07-17): NEITHER the SubagentStart nor
# the SubagentStop payload carries a background-vs-sync discriminator, and
# neither carries a dedicated name field. Therefore the ONLY ground truth
# is the orchestrator declaring its intent — written BEFORE the dispatch
# call (never after: a fast-stopping teammate can fire SubagentStop before
# a post-dispatch write lands).
#
# PAYLOAD SHAPE (scenario 27, re-measured 2026-08-29 against CC 2.1.251,
# bead nexus-houpu). EVERY dispatch — named background teammate included —
# reaches SubagentStart as:
#   agent_id   == an OPAQUE "a<hex>" handle (e.g. "aeb1c1b56623244ae")
#   agent_type == the dispatch's subagent_type, VERBATIM
# The dispatch NAME appears nowhere in the payload, in any field. The
# earlier "a<name>-<hash>" agent_id encoding is GONE; nothing in this
# file may key on it, and nothing below does.
#
# TYPE KEYING — THE ONLY KEYING (nexus-qc4p1 / nexus-nu7fo, measured
# 2026-07-31; nexus-houpu, re-measured 2026-08-29). The one value BOTH
# sides of the ledger can know is the agent TYPE:
#   PreToolUse(Agent).tool_input.subagent_type == SubagentStart.agent_type
# (verified identical, same session_id, for general-purpose x2 + Explore
# in one turn, and again for a NAMED background dispatch at 2.1.251).
# So EXPECT rows written by the PreToolUse hook (agent-dispatch-expect.sh)
# are keyed on subagent_type, and EVERY reader below — the consult rule,
# the declaration audit, the census — pairs a START row to an EXPECT row
# by (agent_type, N-of-type credit), with EXPECT rows deduped by their
# dispatch_id. There is no second key and no morphology fallback.
#
# WHY THE MORPHOLOGY BRANCH IS GONE (nexus-houpu). It was already
# unreachable when nexus-qc4p1 widened these readers: the Agent tool takes
# NO `name` parameter (tool_input is exactly description / prompt /
# subagent_type / run_in_background), so `recognized` was structurally
# pinned at 0 across 25 consecutive dispatches and the type key was added
# ALONGSIDE it rather than replacing it. CC 2.1.251 removed the encoding
# outright, so the branch could no longer fire even in principle. Keeping
# a dead disjunct is not free: it kept three doc blocks and two audit
# surfaces describing a key that decides nothing, and it made "recognised
# by morphology" look like a live fallback that could excuse a missing
# EXPECT row. Deleted, and `recognized` demoted from a GATE to a
# diagnostic tally on the SUMMARY line.
#
# WHY N-OF-TYPE AND NOT AN ORDINAL. Two dispatches of the same
# subagent_type in one turn are indistinguishable at pairing time, and an
# ordinal cannot fix that: the PreToolUse payload's per-call `tool_use_id`
# is ABSENT from the SubagentStart payload, and `prompt_id` — present in
# both — is the TURN id, identical across every dispatch in the message
# (measured, same probe). There is no per-instance key in either
# direction, so an ordinal invented at write time would be exactly as
# unpairable as the name was. The audit therefore matches N EXPECT rows
# of a type against N STARTs of that type (first-appearance order,
# credit-consuming) and reports the DEFICIT.
#
# FORMAT — one file per orchestrator session, append-only TSV, core verbs:
#   <iso-utc-ts> TAB EXPECT   TAB <name>     TAB <background|sync> [TAB <dispatch_id>]
#   <iso-utc-ts> TAB START    TAB <agent_id> TAB <agent_type>
#   <iso-utc-ts> TAB BLOCKED  TAB <agent_id> [TAB <cause>]
#   <iso-utc-ts> TAB CONSUMED TAB <agent_id> TAB <agent_type>
# (BLOCKED's optional 4th field, nexus-plycy: a cause such as
# "lock-exhausted" when the block was NOT a verified credit decision —
# see expectations_mark_blocked / EXPECTATIONS_OWES_CAUSE. Every
# exact-field BLOCKED reader keys on $2/$3 only, so this is inert to
# them, same convention as REPORTED's <strength>.)
# (REPORTED and WOULDBLOCK are stop-hook-emitted terminal verbs written by
# subagent-stop.sh, not by this file — documented at that script instead.)
# The EXPECT row's optional 5th field is the dispatching tool_use_id. No
# reader interprets it; it exists so (a) the writing hook is idempotent
# under double registration and (b) two same-type dispatches inside one
# second are not collapsed by the exact-duplicate-line filter, which
# would silently under-count the N-of-type deficit.
# No JSON: writers are bash one-liners and LLM-authored echos; a malformed
# LINE costs one entry, a malformed json file would cost the session.
# Reads are awk EXACT-FIELD comparisons only — no globs, no regexes over
# caller-supplied values (the morphology prefix check that was the sole
# exception is deleted, nexus-houpu), which is what keeps caller
# metacharacters inert regardless of the charset gate. No locks:
# single-host, append-only, line-grain, and creation itself is one
# O_APPEND|O_CREAT open (no check-then-truncate window).
#
# FAILURE DIRECTION (fixed in advance): every consult helper fails OPEN —
# a missing, unreadable, or junk-bearing file must never block a stop.
# The file is an enabling allowlist, not a gate on everything.

# _expectations_dir — resolve+create the private state dir, echo its path.
_expectations_dir() {
    local dir="${XDG_STATE_HOME:-$HOME/.local/state}/nexus/orchestration"
    mkdir -p "$dir" 2>/dev/null
    chmod 700 "$dir" 2>/dev/null
    printf '%s\n' "$dir"
}

# _expectations_ts — one timestamp shape everywhere (ISO-8601 UTC).
_expectations_ts() {
    date -u +%Y-%m-%dT%H:%M:%SZ
}

# _expectations_append <file> <row> — one atomic O_APPEND|O_CREAT open
# under a private umask. No check-then-truncate: two callers racing the
# FIRST-EVER write to a session's file must not be able to wipe each
# other's row (the append-only no-locks safety claim has to hold at
# creation time too, not just once the file exists).
_expectations_append() {
    local file="$1" row="$2"
    (umask 077; printf '%s\n' "$row" >>"$file")
}

# expectations_file <session_id> — echo the per-session file path.
# session_id is interpolated into a filesystem path, so it gets the same
# defensive charset treatment as name: a traversal-bearing id (../../x)
# must never escape the private 0700 dir. Framework session ids are
# UUID-shaped; the charset is deliberately wider but path-safe.
expectations_file() {
    local sid="$1"
    if [[ -z "$sid" ]]; then
        echo "expectations_file: ERROR — session_id is required" >&2
        return 2
    fi
    if [[ ! "$sid" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$ ]]; then
        echo "expectations_file: ERROR — invalid session_id '$sid' (path-safe charset only)" >&2
        return 2
    fi
    printf '%s/%s.expectations\n' "$(_expectations_dir)" "$sid"
}

# expectations_expect <session_id> <name> <mode> [dispatch_id] — the
# ORCHESTRATOR write path. MUST be called BEFORE the Agent dispatch
# (write-before-dispatch is the load-bearing ordering — see header). mode
# is background|sync; only background rows ever cause an agent to owe a
# report. <name> is a SUBAGENT TYPE when written by the PreToolUse hook
# (the only key both sides of the ledger can know — see TYPE KEYING).
# <dispatch_id> is optional and uninterpreted (see FORMAT).
expectations_expect() {
    local sid="$1" name="${2:-}" mode="${3:-}" dispatch_id="${4:-}"
    if [[ -z "$sid" || -z "$name" || -z "$mode" ]]; then
        echo "expectations_expect: ERROR — usage: expectations_expect <session_id> <name> <mode> [dispatch_id]" >&2
        return 2
    fi
    # Name charset. ':' is admitted (nexus-qc4p1) because subagent_type is
    # now a legal name and plugin-qualified types carry it
    # ('conexus:code-review-expert'). It is inert in both readers: awk
    # compares fields exactly, and the one glob below is a quoted literal
    # where ':' is not a metacharacter. Tab/newline stay excluded, which is
    # what keeps the TSV intact.
    if [[ ! "$name" =~ ^[A-Za-z0-9][A-Za-z0-9_:-]{0,63}$ ]]; then
        echo "expectations_expect: ERROR — invalid name '$name' (must match the agent-type charset)" >&2
        return 2
    fi
    if [[ "$mode" != "background" && "$mode" != "sync" ]]; then
        echo "expectations_expect: ERROR — mode must be 'background' or 'sync', got '$mode'" >&2
        return 2
    fi
    if [[ "$dispatch_id" == *$'\t'* || "$dispatch_id" == *$'\n'* ]]; then
        echo "expectations_expect: ERROR — tab/newline in dispatch_id" >&2
        return 2
    fi
    local file row
    file="$(expectations_file "$sid")" || return 2
    row="$(_expectations_ts)"$'\tEXPECT\t'"$name"$'\t'"$mode"
    [[ -n "$dispatch_id" ]] && row+=$'\t'"$dispatch_id"
    _expectations_append "$file" "$row"
}

# expectations_start <session_id> <agent_id> <agent_type> — the
# SubagentStart-hook stamp. Non-load-bearing backfill (the payload cannot
# classify background-ness — scenario 27); records the framework-assigned
# agent_id for cross-checks and the .16 retro audit.
expectations_start() {
    local sid="$1" agent_id="${2:-}" agent_type="${3:-}"
    if [[ -z "$sid" || -z "$agent_id" || -z "$agent_type" ]]; then
        echo "expectations_start: ERROR — usage: expectations_start <session_id> <agent_id> <agent_type>" >&2
        return 2
    fi
    if [[ "$agent_id" == *$'\t'* || "$agent_type" == *$'\t'* || "$agent_id" == *$'\n'* || "$agent_type" == *$'\n'* ]]; then
        echo "expectations_start: ERROR — tab/newline in agent_id/agent_type" >&2
        return 2
    fi
    local file
    file="$(expectations_file "$sid")" || return 2
    _expectations_append "$file" "$(_expectations_ts)"$'\tSTART\t'"$agent_id"$'\t'"$agent_type"
}

# _expectations_claim_credit <file> <type_enc> <agent_id> <credit> <spent>
# [owner...] — ATOMICALLY claim one unit of this type's background credit.
# Returns 0 iff THIS call claimed a unit; 1 iff every unit was already
# spoken for. nexus-7z7rj ROUND 3 — see WHY A LOCK CANNOT CARRY THIS
# INVARIANT above expectations_owes_report.
#
# One unit of credit == one slot symlink "<file>.credit.<type>.<n>" for n
# in 1..credit. symlink(2) is atomic and fails with EEXIST, so for any
# given n EXACTLY ONE racer on the host can ever create it, no matter how
# many are running, what order they arrive in, or whether any lock is held
# — the kernel, not a lock protocol, is what bounds the count. The link's
# TARGET is the claiming agent_id, so the claim and the ownership stamp
# are the SAME atomic operation (a mkdir-then-write-owner pair would leave
# a window where a claimed slot is anonymous). `readlink` therefore both
# audits who holds each unit and lets a re-entering agent recognize its
# own claim without consulting the ledger.
#
# RECONCILIATION: slots are derived state; the CONSUMED rows are the
# durable record. A ledger written by a build that predates slots (or one
# mid-upgrade) has rows and no slots, so before claiming we re-create one
# slot per already-recorded CONSUMED row, in file order, stamped with that
# row's own agent_id. Idempotent (every `ln -s` onto an existing name just
# fails), safe under concurrency (same reason the claim is), and it keeps
# a live session's accounting continuous across a plugin update instead of
# handing every in-flight type a fresh, fully-unspent pool.
_expectations_claim_credit() {
    local file="$1" type_enc="$2" agent_id="$3" credit="$4" spent="$5"
    shift 5
    local base="${file}.credit.${type_enc}" i owner
    i=0
    for owner in "$@"; do
        i=$((i + 1))
        ln -s "$owner" "${base}.${i}" 2>/dev/null
    done
    # Slots 1..spent are accounted for by the rows just reconciled, so the
    # first candidate is spent+1; the caller only reaches here when
    # credit > spent, so this loop always runs at least once. Walking
    # upward (rather than from 1) keeps the common case one `ln` exec.
    for ((i = spent + 1; i <= credit; i++)); do
        if ln -s "$agent_id" "${base}.${i}" 2>/dev/null; then
            return 0
        fi
        # Already claimed. If it is OUR claim, this is a re-entry after a
        # crash between the claim and its CONSUMED row: still owes, and
        # must not consume a second unit.
        [[ "$(readlink "${base}.${i}" 2>/dev/null)" == "$agent_id" ]] && return 0
    done
    return 1
}

# expectations_owes_report <session_id> <agent_id> <agent_type> — the
# consult rule (v4, nexus-rkigh + nexus-bk974 fix round on top of v3's
# nexus-hbr4x widening). Returns 0 iff the stopping agent owes a
# completion report, via a HYBRID rule, TYPE-KEYED (no named-agent
# morphology requirement):
#
#   1. UNMIXED-NESS GATE (nexus-rkigh, checked FIRST, unconditional): if
#      ANY EXPECT row for agent_type in this session has mode != background
#      (i.e. a sync EXPECT of this type exists anywhere in the ledger,
#      regardless of when it was written relative to this stop), the type
#      is MIXED and this call returns "does not owe" — full stop, no
#      exception, not even for an agent_id that already holds its own
#      CONSUMED row for the type (see WHY THE HYBRID below for why that
#      asymmetry is intentional).
#   2. CONSUMED-VERB SETTLEMENT (nexus-hbr4x v3, unchanged, applies only
#      when the gate above found the type UNMIXED): unspent background
#      credit for agent_type = (#EXPECT rows with mode=background and
#      name==agent_type) - (#CONSUMED rows for that type belonging to
#      OTHER agent_ids). If unspent > 0, or this agent_id already holds
#      its own CONSUMED row for that type (idempotent re-check on a later
#      stop of the SAME agent, e.g. the stop_hook_active round-trip), the
#      agent owes; a CONSUMED row is appended (once per agent_id) to spend
#      the credit.
# Everything else — no credit left, mixed type, unknown agent_type,
# missing/unreadable file — returns 1 (fail-open, never block).
#
# WHY THE HYBRID (nexus-rkigh, P1 CRITICAL from Wave-1 round-1 critique,
# T2 nexus/wave1-critique-round1-2026-08-03 [21378], orchestrator-decided
# and surfaced to Hal 2026-08-03). v3's pure CONSUMED-verb settlement
# consumed credit in STOP order, not dispatch/background-ness order:
# burndown exit criterion 1 ("in a session mixing sync+background of ONE
# type, no sync agent is blocked") was only a FAVORABLE-order outcome, not
# a satisfied invariant — if the sync-shaped stop fired before the true
# background stop, the sync agent was wrongly blocked AND the real
# background agent was later waved through unrecorded, silently
# reproducing the original Finding-0 defect behind an apparently-working
# guard. The fix gates on UNMIXED-ness first: a type with any sync EXPECT
# row is treated as globally undecidable for this session and NEVER
# flagged, by ANY agent_id of that type, in EITHER stop order —
# order-INDEPENDENT, zero false-block by construction, satisfying exit
# criterion 1 literally. This is deliberately the plan's own named
# fallback option (ii), "no-mixed-type interim", but applied PER-TYPE
# instead of session-wide: a session can still mechanize detection for
# every OTHER type that stays unmixed, only the collided type goes dark.
# ACCEPTED PRICE: a genuinely owing background agent of a mixed type is
# never flagged either — pure detection miss, not a false negative masked
# as success (nothing claims the mixed type was checked). This is why the
# self-idempotency check in step 2 does NOT override the mixed gate: an
# agent that picked up a CONSUMED row before its type became mixed (a
# later dispatch in the same session added a sync EXPECT of the same
# type) is not special-cased back to "owes" — the gate is evaluated fresh,
# against the CURRENT full ledger, on every call, so mixed-ness introduced
# mid-session retroactively silences the type for all subsequent queries,
# consistent with "never fires for a mixed type" rather than "fires until
# the first sync EXPECT shows up".
#
# WHY TYPE-KEYING AT ALL, NOT MORPHOLOGY (nexus-hbr4x, design T1 d40a5b53,
# 2026-08-03, STEP 0 re-verified against subagent-stop.sh's own payload
# parse): neither SubagentStart nor SubagentStop carries a
# background-vs-sync discriminator or a name field, so the OLD
# named-agent-morphology gate ("a<name>-...") was UNREACHABLE from the
# Agent tool (no name parameter) — every real dispatch arrived unnamed,
# the morphology check always failed, and owes_report returned 1 for
# every stop across 18/18 and 19/19 measured sessions. The SubagentStop
# guard had literally never fired (nexus-hbr4x finding 0); dropping the
# morphology requirement and keying on agent_type alone is what makes it
# fire at all. See nexus-rkigh above for how the resulting false-block
# class (introduced by dropping morphology) is now closed. As of CC
# 2.1.251 (nexus-houpu) the "a<name>-" encoding does not exist in the
# payload at all, so this function's keying is not merely the better
# choice — it is the only one the wire still supports. This function
# needed no edit for that change; the audit surfaces below did.
#
# LOCKING (nexus-bk974, P2 SIGNIFICANT, same critique round): the
# read-decide-append below is wrapped in the SAME bounded mkdir lockdir
# pattern as agent-dispatch-expect.sh's _expect_if_absent (nexus-3h0u6
# precedent) — without it, concurrent same-type stops racing the last
# unit of credit could each observe unspent credit and each append
# CONSUMED, over-crediting relative to N-of-type intent (this repo's own
# CLAUDE.md explicitly encourages parallel same-type dispatch fleets, so
# this is not a hypothetical).
#
# BUDGET TUNABLE + DEBUG-VISIBLE DEGRADATION (nexus-bk974/nexus-4b8sz, CI
# red 2026-08-07 run 31215840624 shard 1): the try count was a hardcoded
# literal (10 x 0.1s ~= 1s), so a loaded CI runner that legitimately needs
# longer than 1s to cycle the lock had no way to widen the budget short of
# editing this file, and — worse — degrading to the fail-open unlocked
# path was completely SILENT, indistinguishable from the lock never being
# contended at all. Two independent fixes: (1) NX_EXPECT_LOCK_TRIES
# overrides the try count (still x 0.1s sleep each), default 10 when
# unset OR non-numeric (a bad env value must never break the hook — it
# falls back to the production default, never fails loud here), coerced
# through base-10 arithmetic (`10#$tries`) so a leading-zero value like
# "008" is read as decimal 8 rather than crashing bash's octal literal
# parser (008/009 have no valid octal digit), and clamped to a 600-try
# (~60s) ceiling so a runaway env value cannot hang the hook indefinitely;
# (2) when the loop exhausts without acquiring, one stderr line is emitted
# — stderr only (stdout may be parsed by callers), and it is a diagnostic
# breadcrumb only, never a ledger row (the file format's writers are
# exactly EXPECT/START/BLOCKED/CONSUMED, per FORMAT above). CAVEAT
# (substantive-critic, matching the honest precedent in
# pre_close_verification_hook.sh::_stamp_ids): subagent-stop.sh exits 0 on
# every path (block is signaled via stdout JSON, not exit 2), and the
# documented hook contract only feeds exit-0 stderr back to the operator
# under `claude --debug` — this warning is observable in hook debug logs,
# not a normal-session operator-facing signal.
#
# THE CONTRACT (nexus-7z7rj, fix round on top of 4b8sz — CI red
# 2026-08-08, PR #1445 run 31244456036 shard 1/4: blocked=5 vs credit=4
# with NX_EXPECT_LOCK_TRIES already at 200). 4b8sz's "fail-open degrades
# to the pre-lock racy read" was DOCUMENTED as deliberate but was not
# actually gated on lock possession at all: `held` only controlled
# whether `rmdir` ran at the end — the awk verdict computation and the
# CONSUMED append below ran UNCONDITIONALLY, lock or no lock. So a caller
# that exhausted its try budget executed the exact same read-decide-write
# as a lock HOLDER, concurrently and unsynchronized with whichever other
# caller currently held (or had also given up on) the lock. Widening the
# try budget (4b8sz) narrows the window this can happen in but cannot
# close it — any nonzero probability of simultaneous exhaustion still
# eventually reproduces the over-block under enough contention, which is
# exactly what recurred at 200 tries.
#
# CHOSEN FIX: decision-only-under-lock (bead's option (a)). The verdict
# read AND the CONSUMED append are now INSIDE `if [[ -n "$held" ]]` —
# a caller that never acquires the lock does not consult the ledger at
# all, so it cannot observe a stale credit count and cannot append a
# CONSUMED row racing another writer. This makes "CONSUMED rows for a
# type can never exceed that type's credit" a mechanical invariant
# (single-writer-at-a-time, enforced by mkdir's atomicity) instead of a
# probabilistic one that degrades under load. This half of the fix is
# unchanged by the round-2 revision below.
#
# ROUND 2 (nexus-plycy, substantive-critic Critical on the round-1 fix,
# same day): round 1 shipped a fixed "does not owe" default on lock
# exhaustion, gated behind a SESSION-scoped lockdir (one lockdir for
# every agent_type in the session). The critic found that combination
# unsafe: the pre-existing 1-minute mtime-based stale-lock reap (below)
# meant an orphaned/killed lock holder left the lockdir looking "fresh"
# for up to 60s, during which EVERY subsequent stop of ANY type in the
# session would exhaust its budget and silently return "does not owe"
# with zero ledger consultation — the guard going fully dark
# session-wide for up to a minute, inverting its failure direction from
# safe (over-block) to unsafe (silent miss), exactly the RDR-184 class
# it exists to catch. Empirically reachable, not hypothetical: this
# session's own NX_EXPECT_LOCK_HOLD_DELAY_S test run orphaned a
# lock-spin process at PPID 1. Worse, the round-1 justification for
# picking "does not owe" over "block" (avoiding false accusations of
# unrelated, cross-type agents sharing the session lockdir) was optimizing
# for the WRONG contention shape: the empirical bug that motivated
# nexus-7z7rj in the first place was SAME-type contention (8 racers, one
# type, credit=4) — exactly the case a fixed "does not owe" default
# handles worst, silently waving through a genuinely-owing racer under
# real mass same-type dispatch (which CLAUDE.md explicitly encourages).
#
# FIX: two changes, both required together.
#
# (1) PER-TYPE LOCKDIR. The lockdir is now keyed by agent_type, not just
# session: "${file}.owes.<encoded-type>.lock". This removes the
# cross-type contention that motivated round 1's unsafe default — an
# agent's stop now only ever waits on SAME-type racers (or a stale
# SAME-type lock), never on an unrelated type's dispatch/stop traffic
# sharing the session file. ENCODING: agent_type's charset (see the
# guard below) permits `:` (colon-qualified types like
# "conexus:developer"), which is filesystem-legal on POSIX but avoided
# here on purpose, for the same reason this repo already avoids raw
# colons in T3 collection names (see AGENTS.md's collection-naming
# table) — every literal `:` in agent_type is replaced with `__`
# (double underscore) before it is used as a path component. This is
# lossy in theory (a type containing a literal "__" could collide with
# a colon-bearing type that encodes to the same string) but no observed
# agent_type in this repo's conventions uses "__", and a collision's
# worst case is merely "two types share a lockdir" — a throughput/
# false-cross-type-wait regression, not a correctness bug, since the
# decision-only-under-lock invariant from CHOSEN FIX above holds
# regardless of which lockdir a given type happens to use.
#
# (2) FIXED DEFAULT ON EXHAUSTION FLIPS TO BLOCK. With per-type locking,
# exhaustion can now only mean same-type contention or a same-type stale
# lock — in both cases the guard's usual bias applies again: over-
# blocking is the safe, explicable direction (nothing is silently
# missed), so exhaustion now returns 0 (owes) rather than round 1's 1
# (does not owe). Decision-only-under-lock is KEPT for the CONSUMED
# append specifically: an exhausted caller still never writes a CONSUMED
# row (never spend credit you didn't verify under the lock), so "CONSUMED
# rows for a type never exceed that type's credit" remains the same
# mechanical invariant as CHOSEN FIX above — only the BLOCK decision
# itself, not the ledger write, happens unconditionally on exhaustion.
# This means an exhaustion-forced block is now DISCLOSED, not silent:
# the global EXPECTATIONS_OWES_CAUSE is set to "lock-exhausted" (reset to
# empty at the top of every call) so the caller (subagent-stop.sh) can
# both (a) fold a distinguishing note into the block's JSON `reason` so
# an over-blocked agent's operator can tell it apart from a verified
# credit-backed block, and (b) pass it through to
# expectations_mark_blocked's optional 4th field so the ledger itself
# records the cause for later audit — never a bare, unexplained block.
#
# RESIDUAL (honest, bounded): a stale SAME-type lock still over-blocks
# stops of THAT type for up to the existing ~60s reap window (the
# "Bounded lock" comment below; unchanged by this fix), then the reap
# clears it and normal locked operation resumes. This is the accepted
# price — bounded, single-type, self-healing, and always disclosed via
# EXPECTATIONS_OWES_CAUSE/the BLOCKED row's cause field — traded against
# the alternative (round 1's silent, session-wide, undisclosed miss
# window) which is categorically worse for a guard whose entire purpose
# is not missing undeclared dispatches.
#
# WHY A LOCK CANNOT CARRY THIS INVARIANT (nexus-7z7rj ROUND 3, the third
# recurrence — CI red 2026-08-08 PR #1445 run 31244456036, then the
# round-2 fix's own "mechanical" ceiling VIOLATED in a local full-parallel
# run 2026-08-09 at develop f145f3de: `assert len(consumed_rows) <=
# credit` -> `assert 5 <= 4`, five CONSUMED rows stamped the same second).
#
# ROOT CAUSE (deterministically reproduced, not inferred — see
# test_owes_report_credit_claim_survives_a_lock_that_grants_no_exclusion):
# the stale-lock reap below deletes a lock it does not own, and no amount
# of care in the critical section can fix that from outside. The reap is
# three separate steps — test `-d`, run `find`, then `rmdir` — and the
# lock can change hands BETWEEN them:
#
#   B: [[ -d lockdir ]]            -> true  (A holds it)
#   A: rmdir lockdir               -> A releases
#   B: find lockdir -mmin -1       -> absent, so B judges it "stale"
#   C: mkdir lockdir               -> C acquires legitimately
#   B: rmdir lockdir               -> B DELETES C'S LIVE LOCK
#   B: mkdir lockdir               -> B "acquires"; B and C now BOTH
#                                     inside the critical section
#
# Both then read the same credit state and both append CONSUMED. That is
# the observed double-spend, and the window is exactly a fork+exec, which
# is why it only ever surfaced under heavy parallel load and why rounds 1
# and 2 (which widened the retry budget, then made the decision
# atomic-under-the-lock) each narrowed it without closing it: both rounds
# reasoned about the critical section, while the defect is in the code
# that runs BEFORE the lock is ever acquired.
#
# This is not a bug in the reap's threshold or its ordering — it is
# unfixable in kind. Safely stealing a name-based lock requires an atomic
# compare-and-delete ("remove this lock only if it is still the same lock
# I inspected"), and POSIX offers no such operation on a path. Every
# variant of check-then-rmdir has the same window. The alternatives are
# therefore only: (a) never reap, and accept that one SIGKILLed holder
# wedges that type for the rest of the session, or (b) stop making
# correctness depend on the lock at all.
#
# CHOSEN FIX: (b). The unit of credit is now claimed with an ATOMIC
# FILESYSTEM OPERATION rather than decided by reading a count under mutual
# exclusion — see _expectations_claim_credit above. `credit > spent` is
# still computed, and still gates whether a claim is attempted, but it is
# no longer what makes the accounting exact: a racer that passes the
# read-derived gate still consumes nothing unless it wins a slot symlink,
# and for each of the `credit` slot names exactly one racer on the host
# can ever win. So "CONSUMED rows for a type never exceed that type's
# credit" holds even if the lock grants NO exclusion whatsoever — under a
# stolen lock, a leaked lock, a disabled lock, or an exhausted budget.
# The permanent test asserts exactly that (the lock is switched off and
# the invariant is required to survive), so the property cannot regress
# back into being load-dependent.
#
# The lock below is KEPT, deliberately, and is now purely an efficiency
# and log-noise measure (it keeps same-type racers from doing redundant
# awk passes and from racing onto the same slot name): every safety claim
# round 2 made — decision-only-under-lock, exhaustion blocks with a
# disclosed cause and writes no ledger row — is preserved unchanged and
# still true. What changed is that none of them is load-bearing for the
# ceiling any more. The reap's steal is now merely wasteful rather than
# incorrect; it is tracked for removal separately rather than being
# ripped out here, because deleting it without a replacement self-heal
# would trade a harmless inefficiency for a session-long wedge.
#
# ON THE DEFERRED HALF (why over-counting was never "just bookkeeping"):
# a CONSUMED row is a debit against a fungible per-type pool, so a
# spurious row is not a cosmetic accounting error — it silently absorbs
# the credit of the NEXT genuine background dispatch of that type, and
# that agent then stops UNBLOCKED with no BLOCKED row and no disclosed
# cause. The over-blocking at the moment of the race is safe; the missing
# block one dispatch later is exactly the RDR-184 failure this guard
# exists to catch. That is why "narrow the window" was never an adequate
# answer here, and why the fix had to make a spurious debit impossible to
# write rather than merely unlikely.
#
# ORPHANED CREDIT SLOTS — WHAT TERMINATES THIS HOOK, AND WHERE CAN IT BE
# WHEN THAT HAPPENS? (nexus-ols6a, substantive-critic Critical on round 3.)
#
# This is the question none of rounds 1, 2, or 3 asked, and every round
# was caught by it. Each round moved the unsafe window somewhere its own
# test could not see: round 1 the critical section, round 2 the pre-lock
# reap, round 3 the gap between claiming a slot and writing its row. The
# answer is in conexus/hooks/hooks.json: SubagentStop runs with a
# 10-SECOND TIMEOUT. The lock budget is 20s in CI
# (NX_EXPECT_LOCK_TRIES=200) and clamps at 60s. So the harness SIGKILLs
# this hook as a ROUTINE, LOAD-CORRELATED EVENT, in precisely the
# contention regime the whole credit mechanism exists for. An external
# kill here is scheduled, not rare.
#
# WHAT THAT COSTS, AND HOW ROUND 3 MADE IT WORSE BEFORE MAKING IT BETTER.
# Pre-round-3, a kill between the credit read and the CONSUMED append
# wrote nothing: that stop went unguarded (one miss) but the pool was
# untouched, so a later agent still got the unit. Post-round-3 the claim
# lands BEFORE the row, so the same kill costs that stop AND burns a
# credit permanently — a SECOND future agent also slips. Round 3 doubled
# the damage of a timeout kill, and made it less observable too: the old
# spurious debit was a CONSUMED row a census could read, the new one is a
# symlink no audit surface looks at. "A spurious debit can no longer be
# written" was true of ROWS and false of DEBITS — the debit unit moved
# from rows to slots. Recording that plainly because it is structurally
# the same overclaim as rounds 1 and 2 ("mechanical invariant"), and the
# pattern is the actual lesson here.
#
# THE FIX: an orphan is DETECTABLE, so it must never be silent. When the
# claim is lost, re-read the ledger — if every slot is taken while the
# rows still say this type has unspent credit, that state is provably
# inconsistent and the only explanation is a claimant killed between its
# `ln -s` and its row. Block, with cause=credit-slot-orphan, instead of
# returning "does not owe". That keeps the guard's usual bias (over-block
# is explicable; a silent miss is the thing this whole subsystem exists
# to prevent) and it converts round 3's new window from a doubled silent
# miss into a disclosed block. Deliberately NOT done: reclaiming orphaned
# slots after a bounded age. That is exactly the check-then-act shape
# whose unfixability is documented above, and it would reintroduce the
# stolen-lock class in a new costume.
#
# TEST-ONLY CONTENTION SEAM: NX_EXPECT_LOCK_HOLD_DELAY_S, when set to a
# bare non-negative decimal (e.g. "0.3"), sleeps that many seconds AFTER
# acquiring the lock and BEFORE the read-decide-write below. This exists
# so a test can deterministically WIDEN the critical section and force
# other racers to exhaust a small try budget — reproducing the exhaustion
# race on demand instead of hoping CI happens to be loaded enough (see
# tests/hooks/test_subagent_stop_hook.py). A non-numeric or unset value
# is a no-op; this must never fire in production (nothing sets it outside
# the test harness).
expectations_owes_report() {
    local sid="$1" agent_id="${2:-}" agent_type="${3:-}"
    # nexus-plycy: side-channel for the caller to distinguish a
    # verified-under-lock decision from an exhaustion-forced one. Reset
    # on EVERY call (including every early-return path below) so a
    # caller never reads a stale value from a previous invocation. Never
    # `local` — it must escape to the sourcing script (subagent-stop.sh).
    EXPECTATIONS_OWES_CAUSE=""
    [[ -n "$sid" && -n "$agent_id" && -n "$agent_type" ]] || return 1
    # An agent_type outside the name charset can never equal a stored
    # EXPECT name, so it can never owe. Checking it HERE (not just at
    # write time) makes that an enforced invariant rather than an
    # implicit cross-function one — and keeps the awk comparisons below
    # literal (no caller-supplied metacharacters). Charset kept in
    # lockstep with expectations_expect, ':' included, or the invariant
    # this comment claims would stop being true.
    [[ "$agent_type" =~ ^[A-Za-z0-9][A-Za-z0-9_:-]{0,63}$ ]] || return 1
    # agent_id is interpolated into an appended TSV row below; tab/newline
    # would misalign it, the same hazard expectations_start and
    # expectations_mark_blocked guard against.
    if [[ "$agent_id" == *$'\t'* || "$agent_id" == *$'\n'* ]]; then
        return 1
    fi
    local file
    file="$(expectations_file "$sid" 2>/dev/null)" || return 1
    [[ -r "$file" ]] || return 1

    # Bounded lock (nexus-bk974), PER AGENT_TYPE (nexus-plycy round 2 —
    # see THE CONTRACT above for why session-scoped locking was unsafe).
    # ':' is encoded to '__' before use as a path component (see THE
    # CONTRACT); every other permitted charset character is filesystem-
    # safe as-is. A lockdir left behind by a killed hook would otherwise
    # cost every later same-type stop the full budget forever, so reap
    # one that has sat idle past the critical section's realistic worst
    # case before contending for it — same reap-then-contend shape as
    # agent-dispatch-expect.sh's LOCKDIR.
    local type_enc="${agent_type//:/__}"
    local lockdir="${file}.owes.${type_enc}.lock" held=""
    # TEST-ONLY SEAM (nexus-7z7rj round 3): NX_EXPECT_LOCK_DISABLE=1 skips
    # the lock entirely — no reap, no acquire, no release — so the suite
    # can run real concurrent racers against a critical section with ZERO
    # mutual exclusion and require the credit ceiling to hold anyway. That
    # is the strongest available adversary for the atomic-claim invariant,
    # and it is deterministic, unlike hoping a runner is loaded enough to
    # reproduce a stolen lock. Never set outside the test harness; setting
    # it in production costs only redundant work, never the ceiling.
    if [[ "${NX_EXPECT_LOCK_DISABLE:-}" == "1" ]]; then
        held=1
    else
    if [[ -d "$lockdir" ]] && [[ -z "$(find "$lockdir" -maxdepth 0 -mmin -1 2>/dev/null)" ]]; then
        rmdir "$lockdir" 2>/dev/null
    fi
    local tries="${NX_EXPECT_LOCK_TRIES:-10}"
    [[ "$tries" =~ ^[0-9]+$ ]] || tries=10
    tries=$((10#$tries))
    (( tries > 600 )) && tries=600
    local _i
    for ((_i = 0; _i < tries; _i++)); do
        if mkdir "$lockdir" 2>/dev/null; then
            held=1
            break
        fi
        sleep 0.1
    done
    if [[ -z "$held" ]]; then
        # nexus-plycy: per-type locking means exhaustion can only mean
        # same-type contention or a same-type stale lock (see THE
        # CONTRACT above) — the safe direction is to block, DISCLOSED via
        # EXPECTATIONS_OWES_CAUSE, and WITHOUT touching the ledger (no
        # CONSUMED row: never spend credit that was never verified).
        echo "expectations: owes-report lock budget exhausted (${tries} tries) for type '${agent_type}'; no credit consulted, fixed default = owes (BLOCK, cause=lock-exhausted; fail-open direction per nexus-bk974/nexus-4b8sz/nexus-7z7rj/nexus-plycy)" >&2
        EXPECTATIONS_OWES_CAUSE="lock-exhausted"
        return 0
    fi
    fi

    # nexus-7z7rj test seam: deterministically widen the critical section
    # so a test can force other racers to exhaust their try budget instead
    # of relying on incidental CI load. No-op unless explicitly set by a
    # test harness.
    if [[ "${NX_EXPECT_LOCK_HOLD_DELAY_S:-}" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
        sleep "$NX_EXPECT_LOCK_HOLD_DELAY_S"
    fi

    # The "new" verdict now carries the numbers the atomic claim needs
    # (credit, spent, and the already-consuming agent_ids in file order,
    # for slot reconciliation) as extra tab-separated fields. The verdict
    # TOKEN is still the first field and still exactly self|new|no, so the
    # case below reads the same as it always has.
    local verdict_line verdict
    local -a vfields=()
    verdict_line="$(awk -F'\t' -v id="$agent_id" -v ty="$agent_type" '
        # Dedupe EXPECT by dispatch_id, exactly as expectations_undeclared
        # and expectations_census already do ~200 lines below. The dispatch
        # hook takes a bounded lock whose reap can be stolen (nexus-ma5tg,
        # the same TOCTOU this function was just fixed for), so a double
        # registration of ONE dispatch is reachable. There it inflates this
        # type-s credit pool, which over-blocks — safe, but it also made the
        # census and this guard disagree about the same ledger. Now they
        # settle it identically.
        $2 == "EXPECT" && $5 != "" && ($5 in dseen) { next }
        $2 == "EXPECT" { if ($5 != "") dseen[$5] = 1 }
        $2 == "EXPECT" && $3 == ty {
            if ($4 == "background") { credit++ } else { mixed = 1 }
        }
        $2 == "CONSUMED" && $4 == ty {
            if ($3 == id) { self = 1 } else { owner[++spent] = $3 }
        }
        END {
            # nexus-rkigh: the unmixed-ness gate is checked FIRST and is
            # unconditional — it overrides even a pre-existing self
            # CONSUMED row (see the WHY THE HYBRID comment above the
            # function for why that asymmetry is intentional).
            if (mixed) { print "no"; exit }
            if (self) { print "self"; exit }
            if (credit > spent) {
                # +0 forces both to render as numbers. An UNSET awk var
                # prints as the empty string, and because tab is IFS
                # whitespace, the read -a below would silently DROP that
                # trailing empty field and shift the owner list by one.
                # (No apostrophes in this block: the whole program is a
                # single-quoted shell word.)
                line = "new\t" (credit + 0) "\t" (spent + 0)
                for (i = 1; i <= spent; i++) { line = line "\t" owner[i] }
                print line
                exit
            }
            print "no"
        }
    ' "$file" 2>/dev/null)"
    IFS=$'\t' read -r -a vfields <<<"$verdict_line"
    # `read -a` on empty input (awk failed, unreadable file) leaves a
    # ZERO-element array, and ${arr[0]} is fatal under `set -u` — this
    # library is sourced by callers that set it. Default, then fall
    # through to the fail-open `*)` branch below.
    verdict="${vfields[0]:-}"
    case "$verdict" in
        self)
            rmdir "$lockdir" 2>/dev/null
            return 0
            ;;
        new)
            # TEST-ONLY SEAM (nexus-ols6a): widen READ -> CLAIM so a test can
            # force every racer to decide from the same credit state. Without
            # it, switching the lock off removes exclusion but NOT
            # simultaneity, so the falsifier's detection still rides thread
            # interleaving — measured as a 1-in-12 FALSE PASS against a
            # deliberately broken claim. No-op unless set by the harness.
            if [[ "${NX_EXPECT_CLAIM_DELAY_S:-}" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
                sleep "$NX_EXPECT_CLAIM_DELAY_S"
            fi
            # The read above says a unit LOOKS unspent; the claim below is
            # what actually spends it, and is the only thing that decides.
            if _expectations_claim_credit "$file" "$type_enc" "$agent_id" \
                "${vfields[1]:-0}" "${vfields[2]:-0}" "${vfields[@]:3}"; then
                # TEST-ONLY SEAM (nexus-ols6a): widen CLAIM -> APPEND, the one
                # window where an external kill orphans a claimed slot. This
                # is NOT hypothetical: conexus/hooks/hooks.json gives
                # SubagentStop a 10-SECOND timeout while the lock budget is
                # 20s in CI (NX_EXPECT_LOCK_TRIES=200) and clamps at 60s, so
                # the harness SIGKILLs this hook as a ROUTINE, load-correlated
                # event — in exactly the contention regime this code lives in.
                if [[ "${NX_EXPECT_APPEND_DELAY_S:-}" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
                    sleep "$NX_EXPECT_APPEND_DELAY_S"
                fi
                _expectations_append "$file" \
                    "$(_expectations_ts)"$'\tCONSUMED\t'"$agent_id"$'\t'"$agent_type"
                rmdir "$lockdir" 2>/dev/null
                return 0
            fi
            # LOST THE CLAIM — two very different worlds, and telling them
            # apart is what nexus-ols6a is about (see ORPHANED CREDIT SLOTS
            # above the function).
            #
            #   (a) LEGITIMATE: another racer took the last unit between our
            #       read and our claim. The pool really is dry. Do not owe.
            #   (b) ORPHAN: a previous claimant was killed between its `ln -s`
            #       and its CONSUMED row (the 10s-timeout window above), so a
            #       slot is spoken for by an agent that left no ledger record.
            #
            # They are distinguishable by RE-READING: in (a) the winners'
            # CONSUMED rows account for the whole pool; in (b) every slot is
            # taken while the rows still say credit remains — a provably
            # inconsistent state. Re-read twice with a short pause, because a
            # winner that has claimed but not yet appended looks exactly like
            # an orphan for those few microseconds and would otherwise make
            # this cause fire routinely under contention (a cause that cries
            # wolf is one operators learn to ignore). This is a READ retry,
            # NOT a bounded-age reclaim: nothing is ever deleted or stolen,
            # so it cannot reintroduce the check-then-act shape that caused
            # this bug in the first place.
            local recheck fresh_credit fresh_spent
            for recheck in 1 2; do
                read -r fresh_credit fresh_spent <<<"$(awk -F'\t' -v ty="$agent_type" '
                    $2 == "EXPECT" && $5 != "" && ($5 in dseen) { next }
                    $2 == "EXPECT" { if ($5 != "") dseen[$5] = 1 }
                    $2 == "EXPECT" && $3 == ty && $4 == "background" { credit++ }
                    $2 == "CONSUMED" && $4 == ty { spent++ }
                    END { printf "%d %d\n", credit + 0, spent + 0 }
                ' "$file" 2>/dev/null)"
                (( ${fresh_credit:-0} > ${fresh_spent:-0} )) || break
                [[ "$recheck" == "2" ]] && break
                sleep 0.1
            done
            if (( ${fresh_credit:-0} > ${fresh_spent:-0} )); then
                # Provably inconsistent: no slot left, yet the ledger says
                # this type still has unspent credit. FAIL LOUD in the
                # guard's usual safe direction (block, disclosed) rather
                # than silently waving this stop through — the silent
                # version is what let one killed hook cost TWO unguarded
                # stops instead of one.
                echo "expectations: credit-slot orphan for type '${agent_type}' — every credit slot is claimed but the ledger records only ${fresh_spent} of ${fresh_credit} spent, so a claimant was killed between its slot claim and its CONSUMED row (SubagentStop has a 10s hook timeout). Blocking this stop rather than silently passing it (cause=credit-slot-orphan; nexus-ols6a)" >&2
                EXPECTATIONS_OWES_CAUSE="credit-slot-orphan"
                rmdir "$lockdir" 2>/dev/null
                return 0
            fi
            rmdir "$lockdir" 2>/dev/null
            return 1
            ;;
        *)
            rmdir "$lockdir" 2>/dev/null
            return 1
            ;;
    esac
}

# expectations_mark_blocked <session_id> <agent_id> [cause] — record that
# the stop hook has blocked this agent once. Pairs with
# expectations_already_blocked for the block-at-most-once guard (belt to
# stop_hook_active's braces). <cause> (optional 4th TSV field, nexus-plycy
# — same inert-4th-field convention as REPORTED's <strength>, see
# _stamp_resolution_if_reported in subagent-stop.sh) records WHY the
# block happened when it was not a verified credit decision, e.g.
# "lock-exhausted" (from EXPECTATIONS_OWES_CAUSE) — every exact-field
# BLOCKED reader in this file keys on $2/$3 only, so a 4th field is
# always safely ignored by them. Omitted/empty writes the plain 3-field
# row, unchanged from before this parameter existed.
expectations_mark_blocked() {
    local sid="$1" agent_id="${2:-}" cause="${3:-}"
    if [[ -z "$sid" || -z "$agent_id" ]]; then
        echo "expectations_mark_blocked: ERROR — usage: expectations_mark_blocked <session_id> <agent_id> [cause]" >&2
        return 2
    fi
    # Same guard as expectations_start: a tab/newline here would misalign
    # the BLOCKED row, and expectations_already_blocked's exact-field
    # match would then never fire — silently defeating block-at-most-once.
    if [[ "$agent_id" == *$'\t'* || "$agent_id" == *$'\n'* || "$cause" == *$'\t'* || "$cause" == *$'\n'* ]]; then
        echo "expectations_mark_blocked: ERROR — tab/newline in agent_id/cause" >&2
        return 2
    fi
    local file
    file="$(expectations_file "$sid")" || return 2
    if [[ -n "$cause" ]]; then
        _expectations_append "$file" "$(_expectations_ts)"$'\tBLOCKED\t'"$agent_id"$'\t'"$cause"
    else
        _expectations_append "$file" "$(_expectations_ts)"$'\tBLOCKED\t'"$agent_id"
    fi
}

# expectations_already_blocked <session_id> <agent_id> — 0 iff a BLOCKED
# row exists for this exact agent_id. Missing file => 1 (not blocked yet),
# which composes with owes_report's fail-open into "never block".
expectations_already_blocked() {
    local sid="$1" agent_id="${2:-}"
    [[ -n "$sid" && -n "$agent_id" ]] || return 1
    local file
    file="$(expectations_file "$sid" 2>/dev/null)" || return 1
    [[ -r "$file" ]] || return 1
    awk -F'\t' -v id="$agent_id" \
        '$2 == "BLOCKED" && $3 == id { found = 1 } END { exit !found }' \
        "$file" 2>/dev/null
}

# expectations_undeclared <session_id> — the declaration-completeness
# retro audit (RDR-184 .16; query hardened out of markdown after the
# Phase-2 critique proved the unfiltered version false-flagged every
# sync dispatch). Prints one "UNDECLARED\t<agent_id>\t<agent_type>" line
# per START row whose agent_type has no unspent EXPECT credit left. An
# EXPECT row of EITHER mode supplies credit — a deliberately-declared
# sync dispatch stays audit-clean. Missing/unreadable file => rc 3 (see
# NO-LEDGER EXIT CODE below).
#
# EVERY START IS EVALUATED (nexus-houpu, 2026-08-29). There is no longer
# an unrecognised class that gets a free pass. The previous rule skipped
# any START that matched neither the "a<name>-" morphology nor an EXPECT
# name, on the reasoning that flagging it would false-accuse ordinary
# anonymous SYNC dispatches — true while EXPECT rows were hand-written
# for named background teammates only. It stopped being true at
# nexus-qc4p1: agent-dispatch-expect.sh is a PreToolUse(Agent) hook that
# writes an EXPECT row for EVERY dispatch it sees, sync ones included
# (mode="sync"), gated only by NX_ORCH_STOP_GUARD — the same gate that
# controls the START stamp, so the two cannot be asymmetrically off. A
# START with no EXPECT of its type therefore means exactly one thing: the
# declaration machinery did not see that dispatch. That is the deficit
# this audit exists to report, so it is now reported per dispatch instead
# of being silently dropped.
#
# WHAT REPLACED THE MORPHOLOGY BLIND SPOT (bead nexus-mk3tw, superseded
# here). mk3tw's finding was that a background teammate named only in the
# PROMPT text never acquired the "a<name>-<hash>" agent_id encoding this
# audit keyed on, so N such dispatches with 0 EXPECT rows reported
# "undeclared=0" — a false all-clear (reproduced 2026-07-25, 4-then-6
# dispatches). Its fix could not recover per-dispatch detection and so
# settled for an honest aggregate: a SUMMARY line plus a BLINDSPOT hard
# failure whenever recognized==0. Type keying recovers the per-dispatch
# detection mk3tw could not, and CC 2.1.251 removed the encoding
# entirely, so that fixture now produces four UNDECLARED lines and rc 2 —
# a NAMED deficit, which is strictly more than the aggregate disclosure
# it replaces. The SUMMARY line is kept verbatim in shape:
#   SUMMARY\tchecked=<N> recognized=<M> unrecognized=<N-M> undeclared=<K>
# `checked` is every unique START'd agent_id; `recognized` is now a
# DIAGNOSTIC TALLY of the STARTs whose type appears among EXPECT names,
# not a gate on whether a START is evaluated. With the dispatch hook live
# they coincide; when it is inert, recognized=0 while undeclared=checked,
# and BOTH readings say the same thing.
#
# DISPATCHES THE HOOK CANNOT SEE ARE NAMED, NOT EXCUSED. The EXPECT row is
# written by the PreToolUse hook on the Agent|Task tools only
# (agent-dispatch-expect.sh), while SubagentStart fires for every
# subagent. A START that reached SubagentStart by another door — a
# Workflow-tool agent, an agent a hook itself spawned — has no EXPECT row
# of its type and IS reported UNDECLARED (rc=2). That is the intent: an
# audit that silently skipped what it could not pair is the free pass
# this function used to give, and it hid a half-working dispatch hook.
# The remedy is at dispatch time — `expectations_expect` by hand, keyed on
# the subagent type, for exactly the dispatch the hook cannot see (see
# conexus/skills/orchestration/SKILL.md) — never a skip in the audit.
#
# UNDECLARED-DEFICIT EXIT CODE (nexus-suuja): a genuine undeclared>0
# result was previously rc-invisible — the same exit 0 as a fully clean
# run — so a caller keying on rc alone (not parsing SUMMARY/UNDECLARED
# lines) could not tell "nothing to report" from "N recognized
# dispatches never got a report". This function exits 2 when
# undeclared>0, checked strictly AFTER the BLINDSPOT check below, which
# still takes priority and still exits 1. (Under the current rule the two
# are mutually exclusive — undeclared>0 requires at least one START,
# BLINDSPOT requires none — but the precedence is kept explicit so the rc
# contract does not depend on that happening to stay true.)
#
# NO-LEDGER EXIT CODE (nexus-ahl9v): shakedown falsification F3 proved a
# MISTYPED session id audits as rc=0 "clean" — indistinguishable from a
# genuinely clean session, because the absent/unreadable-file path below
# was folded into the same fail-open `return 0` the live stop-guard
# callers rely on (expectations_owes_report et al., none of which call
# this function). Audits need the opposite of fail-open: absence of a
# ledger is absence of EVIDENCE, not evidence of cleanliness. This
# function now prints a "NOTE" line to stderr and exits 3 whenever it has
# no readable ledger file to audit (empty/invalid session_id, or a
# session_id with no ledger written yet) — a distinct rc a caller can
# branch on, instead of a bare 0 a caller cannot tell apart from clean.
# Fail-open callers are unaffected: only THIS function's contract widens;
# expectations_owes_report/_already_blocked/_mark_blocked/_sweep (the
# actual stop-guard hook's calls, see subagent-stop.sh) are untouched and
# stay fail-open on a missing file. RC CONTRACT:
#   0 = clean (no undeclared, not a blindspot, ledger was readable)
#   1 = BLINDSPOT — the ledger declared dispatches (EXPECT rows exist)
#       but recorded ZERO STARTs, so the audit walked nothing. An audit
#       that saw no dispatch at all is not a clean audit (nexus-mk3tw's
#       "not evidence of compliance" rule, re-aimed at the one shape that
#       can still produce it now that every START is evaluated: an inert
#       or unregistered SubagentStart stamp).
#   2 = undeclared>0 — a real declaration-completeness deficit
#   3 = no ledger file for this session — nothing was checkable, NOT
#       evidence of cleanliness (nexus-ahl9v)
# See AGENTS.md's ledger usage snippet for the caller-facing summary (it
# states this same contract).
#
# N-OF-TYPE MATCHING (nexus-qc4p1, unchanged): credit is consumed in
# first-appearance order, so three STARTs of a type with two EXPECT rows
# yield exactly one UNDECLARED line — a PARTIAL mechanization failure
# stays visible instead of being absorbed by set membership. EXPECT rows
# are deduped by dispatch_id first (see the awk comment below), because a
# double registration inflates the credit pool and would MASK exactly the
# undeclared START this audit is looking for.
expectations_undeclared() {
    local sid="$1"
    local file=""
    if [[ -n "$sid" ]]; then
        file="$(expectations_file "$sid" 2>/dev/null)" || file=""
    fi
    if [[ -z "$file" ]] || [[ ! -r "$file" ]]; then
        echo "NOTE — no ledger file for session '${sid}': either this session dispatched no agents (legitimately nothing to audit) or the session id is wrong — cross-check with a known-good sid via expectations_census; absence is not evidence of cleanliness (nexus-8dr2u)" >&2
        return 3
    fi
    awk -F'\t' '
        # Pairing is decided in END, not on the START line: an EXPECT
        # row for the type may be anywhere in the file, so a single pass
        # cannot classify a START row as it arrives.
        $2 == "START" {
            if (!($3 in allstart)) {
                allstart[$3] = 1; checked++
                order[++n] = $3; stype[$3] = $4
            }
        }
        # Dedupe by dispatch_id: the writing hook takes a bounded lock, so a
        # double registration that outlasts the budget can append the SAME
        # dispatch twice. A duplicate EXPECT row is not the harmless nuisance
        # a duplicate START row is — it inflates the credit pool and MASKS an
        # undeclared start. The reader can settle it unambiguously, so it does.
        $2 == "EXPECT" && $5 != "" && ($5 in dseen) { next }
        $2 == "EXPECT" { if ($5 != "") dseen[$5] = 1; e[$3] = 1; credit[$3]++
                         expect_total++ }
        END {
            undeclared = 0
            for (i = 1; i <= n; i++) {
                id = order[i]; t = stype[id]
                # recognized is a TALLY, not a gate (nexus-houpu): it
                # records how many STARTs had an EXPECT row of their type
                # anywhere in the ledger, which is what distinguishes an
                # inert dispatch hook from a partially-missed one. Every
                # START is evaluated either way — there is no longer an
                # unrecognised class that skips the credit check.
                if (t in e) recognized++
                if (credit[t] > 0) { credit[t]--; continue }   # N-of-type
                print "UNDECLARED\t" id "\t" t
                undeclared++
            }
            unrecognized = checked - recognized
            printf "SUMMARY\tchecked=%d recognized=%d unrecognized=%d undeclared=%d\n", \
                checked, recognized, unrecognized, undeclared
            # The one false-clean shape left: the ledger declares dispatches
            # and records no START at all, so the walk above examined
            # nothing. Zero UNDECLARED lines there means "nothing was
            # checkable", never "compliant" (nexus-mk3tw, re-aimed).
            if (checked == 0 && expect_total > 0) {
                print "BLINDSPOT\tledger holds " expect_total " EXPECT row(s) and ZERO START rows - the audit walked nothing, so undeclared=" undeclared " is NOT evidence of compliance (check the SubagentStart stamp is registered and NX_ORCH_STOP_GUARD is not off)"
                exit 1
            }
            if (undeclared > 0) exit 2
        }
    ' "$file" 2>/dev/null
    return $?
}

# expectations_census <session_id> — the scripted census (nexus-hybv1: the
# hand-count method under-reported a real BLOCKED as 0 on bfbfa2fe and
# over-reported resolved blocks as failures on b819e8f3; .19 and every
# later success-criteria measurement derives counts from THIS, never by
# hand). Output (TSV):
#   AGENT <agent_id> <name> <terminal> <declared|undeclared>
#       terminal: REPORTED | BLOCKED_RESOLVED (BLOCKED then a later
#       REPORTED — the guard nudged a report out, i.e. it WORKED) |
#       BLOCKED_UNRESOLVED (no report ever recorded after the block) |
#       WOULDBLOCK | NO_TERMINAL (started, owes nothing recorded)
#   EXPECTED_NO_START <name>   — declared but no named START row
#   ROWS expect=N start=N reported=N blocked=N wouldblock=N
#   CLASSIFIED reported=N blocked_resolved=N blocked_unresolved=N \
#       wouldblock=N no_terminal=N undeclared=N expected_no_start=N
#   BLINDSPOT checked=N recognized=M unrecognized=N-M
# Exact-duplicate lines (the nexus-3h0u6 doubling, legacy files) are
# dropped; legitimate repeats (same verb, different timestamp — e.g. one
# REPORTED per round) are kept in ROWS counts. Missing/unreadable file =>
# no output, exit 0 (fail-open, like every consult surface here).
#
# EVERY STARTED AGENT IS LISTED (nexus-houpu, 2026-08-29), matching
# expectations_undeclared's rule above — see its header for why the
# unrecognised free pass had to go. Two consequences here specifically:
#   - The per-agent view no longer suppresses a STARTed id that pairs
#     with no EXPECT row. It prints with its REAL agent_type and a
#     declared/undeclared verdict, decided by N-OF-TYPE credit in
#     first-appearance order.
#   - The companion special case is gone with it: an unrecognised id that
#     happened to carry a terminal verb used to be re-labelled as if it
#     had no START row at all (name "-", declaration "no-start"), which
#     misreported a dispatch the ledger had plainly seen start. "no-start"
#     now means only what it says — a BLOCKED/REPORTED/WOULDBLOCK row for
#     an id with no START row (review 21032 Critical 2 / nexus-0s0o1).
# EXPECTED_NO_START compares COUNTS (a type with 2 EXPECT rows and 1
# START is still short one), unchanged.
#
# BLIND-SPOT DISCLOSURE + EXIT CODE (bead nexus-mk3tw, superseded in
# mechanism, kept in intent — the SAME rule as expectations_undeclared).
# mk3tw reproduced expect=0 start=6 reading as clean because the per-agent
# view keyed on an agent_id encoding that no dispatch carried. That
# fixture now lists all 6 as undeclared (undeclared=6 in CLASSIFIED), and
# expectations_undeclared exits 2 on it: a deficit, disclosed per dispatch.
# The BLINDSPOT line stays as the aggregate tally. This function is the
# REPORT, not the verdict: it exits 1 only for the one shape neither
# surface can see into — EXPECT rows present and ZERO STARTs walked — and
# 0 otherwise. It never exits 2; the rc=2 deficit contract belongs to
# expectations_undeclared alone, so a caller reading both gets one answer.
expectations_census() {
    local sid="$1"
    [[ -n "$sid" ]] || return 0
    local file
    file="$(expectations_file "$sid" 2>/dev/null)" || return 0
    [[ -r "$file" ]] || return 0
    awk -F'\t' '
        # Classification is LAST-STATE-WINS in row order (review 21032
        # Critical 1: a one-way-sticky REPORTED masked a LATER unresolved
        # BLOCKED — the exact defect class this function exists to kill):
        #   BLOCKED    -> BLOCKED_UNRESOLVED (unconditionally supersedes)
        #   REPORTED   -> BLOCKED_RESOLVED if currently blocked, else REPORTED
        #   WOULDBLOCK -> WOULDBLOCK (observe-mode soft block, supersedes)
        # Any terminal verb also seeds the per-agent list (review 21032
        # Critical 2: a BLOCKED/REPORTED id with no START row — hooks are
        # independent invocations with no ordering guarantee — must not
        # vanish from the per-agent view; it prints with name "-" and
        # declaration "no-start"). NOTE term[] reads auto-vivify keys;
        # printing iterates order[] only, so stray keys are inert.
        seen[$0]++ { next }                     # 3h0u6 exact-duplicate rows
        # Dedupe by dispatch_id BEFORE the ROWS tally, so a re-registered
        # dispatch does not inflate `expect=N` either — same placement as the
        # exact-duplicate filter above. See the identical guard in
        # expectations_undeclared for why a duplicate EXPECT row is worse
        # than a duplicate START row.
        $2 == "EXPECT" && $5 != "" && ($5 in dseen) { next }
        { rows[$2]++ }
        # expectrows[] is the immutable per-type total; credit[] is the
        # copy the AGENT walk consumes for N-of-type matching.
        $2 == "EXPECT"  { if ($5 != "") dseen[$5] = 1
                          expect[$3] = 1; expectrows[$3]++; credit[$3]++ }
        # Recognition (and therefore listing) is decided in END: an EXPECT
        # row for a START row s agent_type may appear anywhere in the file.
        $2 == "START" {
            if (!($3 in allstart)) {
                allstart[$3] = 1; checked++
                stype[$3] = $4; startcount[$4]++
                if (!($3 in listed)) { order[++n] = $3; listed[$3] = 1 }
            }
        }
        $2 == "REPORTED" || $2 == "BLOCKED" || $2 == "WOULDBLOCK" {
            if (!($3 in listed)) { order[++n] = $3; listed[$3] = 1 }
        }
        $2 == "REPORTED" {
            # Resolution strength (critique 2026-07-22): a REPORTED row that
            # RESOLVES a block carries "immediate" (the block round-trip
            # itself produced the report — the guard demonstrably worked) or
            # "later" (a subsequent stop found a report that may have arrived
            # for unrelated reasons) in field 4. Both fold into
            # BLOCKED_RESOLVED per-agent; the split is surfaced in CLASSIFIED
            # so guard-effectiveness claims can weight them honestly. Rows
            # with no field 4 (primary-path REPORTED, pre-split ledgers)
            # count as immediate when they resolve.
            if (term[$3] == "BLOCKED_UNRESOLVED") {
                term[$3] = "BLOCKED_RESOLVED"
                if ($4 == "later") res_later++; else res_immediate++
            } else if (term[$3] != "BLOCKED_RESOLVED") {
                term[$3] = "REPORTED"
            }
        }
        $2 == "BLOCKED"    { term[$3] = "BLOCKED_UNRESOLVED" }
        $2 == "WOULDBLOCK" { term[$3] = "WOULDBLOCK" }
        END {
            for (i = 1; i <= n; i++) {
                id = order[i]
                t = (term[id] == "") ? "NO_TERMINAL" : term[id]
                ty = stype[id]
                if (ty == "") {
                    # No START row for this id at all — it reached the
                    # ledger only through a terminal verb (review 21032
                    # Critical 2 / nexus-0s0o1). This is the ONLY route to
                    # "no-start"; an id that did start is never relabelled
                    # into it (nexus-houpu).
                    print "AGENT\t" id "\t-\t" t "\tno-start"
                    nostart++
                } else {
                    # recognized is a TALLY of STARTs whose type was
                    # declared somewhere in the ledger, not a gate on
                    # listing (nexus-houpu). Every STARTed id is listed and
                    # gets a verdict; recognized==0 with checked>0 means
                    # not one dispatch was declared, which is what the
                    # BLINDSPOT line below reports.
                    if (ty in expect) recognized++
                    if (credit[ty] > 0) { credit[ty]--; d = "declared" }
                    else               { d = "undeclared"; undeclared++ }
                    print "AGENT\t" id "\t" ty "\t" t "\t" d
                }
                cls[t]++
            }
            ens = 0
            # A declared type with FEWER starts than EXPECT rows: the
            # dispatch was declared and never arrived (or arrived fewer
            # times than declared). Counted per NAME, not per surplus unit.
            for (nm in expect) if (startcount[nm] < expectrows[nm]) { print "EXPECTED_NO_START\t" nm; ens++ }
            printf "ROWS\texpect=%d start=%d reported=%d blocked=%d wouldblock=%d\n", \
                rows["EXPECT"], rows["START"], rows["REPORTED"], rows["BLOCKED"], rows["WOULDBLOCK"]
            printf "CLASSIFIED\treported=%d blocked_resolved=%d (immediate=%d later=%d) blocked_unresolved=%d wouldblock=%d no_terminal=%d undeclared=%d no_start=%d expected_no_start=%d\n", \
                cls["REPORTED"], cls["BLOCKED_RESOLVED"], res_immediate, res_later, \
                cls["BLOCKED_UNRESOLVED"], \
                cls["WOULDBLOCK"], cls["NO_TERMINAL"], undeclared, nostart, ens
            unrecognized = checked - recognized
            printf "BLINDSPOT\tchecked=%d recognized=%d unrecognized=%d\n", checked, recognized, unrecognized
            # ONE blind-spot rule, shared with expectations_undeclared: the
            # ledger declared dispatches and the walk examined no START. An
            # all-undeclared session is a DEFICIT (undeclared=N above; rc=2
            # from expectations_undeclared), never a blind spot -- the two
            # surfaces must not answer differently on the same ledger.
            if (checked == 0 && rows["EXPECT"] > 0) exit 1
        }
    ' "$file" 2>/dev/null
    return $?
}

# expectations_last_terminal <session_id> <agent_id> — echo the LAST
# terminal verb (REPORTED|BLOCKED|WOULDBLOCK) recorded for agent_id, or
# nothing. Consumed by the stop hook''s resolution stamp to avoid
# appending consecutive duplicate REPORTED rows on every re-stop of a
# resolved agent (review 21032 finding 3 — ROWS counts stay meaningful).
expectations_last_terminal() {
    local sid="$1" agent_id="${2:-}"
    [[ -n "$sid" && -n "$agent_id" ]] || return 0
    local file
    file="$(expectations_file "$sid" 2>/dev/null)" || return 0
    [[ -r "$file" ]] || return 0
    awk -F'\t' -v id="$agent_id" \
        '($2 == "REPORTED" || $2 == "BLOCKED" || $2 == "WOULDBLOCK") && $3 == id { v = $2 } END { if (v != "") print v }' \
        "$file" 2>/dev/null
    return 0
}

# expectations_sweep — best-effort reap of expectations files older than 7
# days (no session-directory tie; the lifespan orphan-reaper precedent).
# Safe to call from any hook entry; never fails the caller.
# HONEST RESIDUAL: reaping is by mtime, which only refreshes on WRITE. A
# session idle >7 days with a background dispatch still pending can have
# its file swept by an unrelated session's hook-entry sweep — the guard
# then degrades fail-OPEN for that dispatch (never blocks, never breaks).
# Accepted: 7 days is generous vs teammate lifespans; flagged for the .16
# retro audit rather than complicated here.
# _expectations_archive_dir — durable home for archived ledgers, sibling of
# the live directory. nexus-4bqre.1: the census joins agent ids to START
# rows for agent-type attribution, and expectations_sweep deletes ledgers
# older than 7 days, so an un-archived ledger takes its lifecycle rows with
# it. (Agent TYPE itself is recoverable without this — agent-<id>.meta.json
# carries agentType at 99 pct coverage and is never swept — so the archive
# is belt-and-braces for the census and the sole source only for EXPECT and
# CONSUMED lifecycle rows, which meta.json does not carry.)
_expectations_archive_dir() {
    local dir="${XDG_STATE_HOME:-$HOME/.local/state}/nexus/orchestration-archive"
    mkdir -p "$dir" 2>/dev/null
    chmod 700 "$dir" 2>/dev/null
    printf '%s\n' "$dir"
}

# expectations_archive — copy live ledgers into the archive before the sweep
# can reap them. Idempotent and append-only: a file is copied when it is
# absent from the archive or when the live copy is NEWER (ledgers grow by
# append, so a same-session re-run must refresh rather than skip). Archived
# files are never deleted or truncated here, so a ledger that has already
# been swept upstream stays put. Never fails the caller — this runs on a
# hook path and an archive failure must not break a subagent stop.
#
# MUST be called BEFORE expectations_sweep in any entry point that sweeps;
# calling it after would archive whatever survived the reap, which is the
# one thing the archive exists to prevent.
expectations_archive() {
    local src dst f base
    src="$(_expectations_dir)"
    dst="$(_expectations_archive_dir)"
    [[ -d "$src" && -d "$dst" ]] || return 0
    for f in "$src"/*.expectations; do
        [[ -f "$f" ]] || continue
        base="${f##*/}"
        if [[ ! -e "$dst/$base" || "$f" -nt "$dst/$base" ]]; then
            cp -p "$f" "$dst/$base" 2>/dev/null || true
        fi
    done
    return 0
}

expectations_sweep() {
    local dir
    dir="$(_expectations_dir)"
    find "$dir" -maxdepth 1 -name '*.expectations' -type f -mtime +7 -delete 2>/dev/null
    # nexus-7z7rj round 3: the credit-slot symlinks are derived state that
    # lives beside the ledger under a longer name, so the reap above never
    # matched them.
    #
    # CORRECTION (nexus-ols6a review): an earlier version of this comment
    # claimed deleting a slot whose ledger still exists is "impossible"
    # here. That is FALSE — a ledger's mtime is refreshed by every append,
    # so a session active for more than 7 days keeps its ledger while its
    # day-0 slots age past the floor and are deleted. The real reason this
    # is safe is RECONCILIATION: any deleted slot that still has a backing
    # CONSUMED row is re-created (indices 1..spent) on the next `new`
    # verdict, so a deleted row-backed slot cannot become a second spend.
    # Slots above `spent` are either orphans (freeing them is the only
    # reclaim that exists) or the caller's own, which the `self` branch
    # intercepts first. Stating the actual reason because a maintainer who
    # trusted the word "impossible" could make a change that is NOT safe.
    find "$dir" -maxdepth 1 -name '*.expectations.credit.*' -type l -mtime +7 -delete 2>/dev/null
    return 0
}

# expectations_reconcile <session_id> <stop_payload_json> — cross-check the
# ledger's outstanding background STARTs against the harness's OWN
# background-task ground truth, now available in Stop/SubagentStop hook
# input (CC 2.1.145: `background_tasks`, `session_crons`). Bead nexus-2v0v7
# (epic nexus-qkbo7).
#
# THE GAP THIS CLOSES. Every consult surface above answers "did THIS agent
# report" from the ledger alone. None of them can see an agent that never
# fired SubagentStop at all — a hook crash, an OOM kill, or a harness-level
# SIGKILL between dispatch and stop all leave a START row with no terminal
# row (REPORTED/BLOCKED/WOULDBLOCK), indistinguishable from "still
# legitimately running" from the ledger's point of view alone. The
# harness's own background-task list is independent ground truth: a task
# the harness no longer tracks, while the ledger still calls it
# outstanding, is a silent death the ledger alone could never detect.
#
# SCHEMA CAUTION — READ BEFORE TRUSTING THIS BEYOND WARN-ONLY (written
# ahead of full schema confirmation by a companion research pass;
# nexus-2v0v7's relay flagged this explicitly):
#   - The exact per-task field names inside `background_tasks` are NOT YET
#     independently verified against a live payload. This function reads
#     the first present of a small CANDIDATE FIELD LIST (below) per task;
#     if the real schema uses a different key, every task degrades to
#     "unidentifiable" rather than crashing or false-matching.
#   - `background_tasks` may be a MIXED population (background Bash
#     processes alongside background Agent dispatches) — there is no
#     confirmed discriminator field to separate them, so this function
#     does not attempt to filter by task kind. A harness whose background
#     tasks are ALL non-agent (e.g. only Bash jobs) would make every
#     outstanding agent START look STRANDED, every stop, which is a real
#     false-positive risk this caveat exists to name rather than hide.
#   - Because of the two points above, treat STRANDED output as a lead to
#     investigate, not a confirmed verdict, until the schema is verified.
#   - `background_tasks` absent, null, not a list, or the whole payload
#     unparseable => nothing to reconcile, rc 0, no output — unconditionally,
#     on every pre-2.1.145 harness (zero behavior change).
#
# CANDIDATE FIELD LIST (ordered, first hit per task wins): agent_id, id,
# task_id, taskId, subagent_id. A bare string array entry is taken as the
# id verbatim. Revisit this list (and the two caveats above) the moment the
# schema is independently confirmed.
#
# OUTPUT (stdout, TSV-ish, same convention as expectations_undeclared):
#   STRANDED\t<agent_id>\t<agent_type>   — ledger says outstanding (STARTed,
#       no REPORTED/BLOCKED/WOULDBLOCK row yet); the harness's
#       background_tasks list at Stop time carries no matching id. The new
#       detection class: a silent death the ledger alone could never see
#       (subject to the SCHEMA CAUTION above).
#   UNDECLARED_TASK\t<harness_id>        — the harness tracks this
#       background task, but no ledger START row was ever matched to it.
#       Corroborates (does not replace) expectations_undeclared's rc=2
#       class.
#   SUMMARY\toutstanding=<N> harness_tasks=<M> unidentified=<U> stranded=<S> undeclared_tasks=<T>
#
# RC CONTRACT (adds rc=4 to the 0/1/2/3 vocabulary expectations_undeclared
# owns — see that function's header; the numbers below are NOT a
# reinterpretation of that vocabulary except where explicitly marked
# reused):
#   0 = clean. Either no harness background-task data was present (nothing
#       to reconcile, silently skipped) or data was present and every
#       outstanding START correlated to a live harness task with no extra
#       harness-only tasks.
#   2 = REUSED, deliberately, from expectations_undeclared's vocabulary: at
#       least one UNDECLARED_TASK line and zero STRANDED lines. This is
#       corroborating evidence for the SAME "undeclared dispatch" deficit
#       that function already reports, not a new class, so it reuses that
#       function's exit code rather than inventing a new number for the
#       same idea.
#   4 = NEW: at least one STRANDED line — the ledger and the harness
#       disagree about whether an agent is still alive. Takes priority over
#       rc=2 when both conditions hold: a silent death is a strictly more
#       actionable finding than an undeclared corroboration.
# Never blocks and never mutates the ledger: this function only prints and
# returns an rc. The caller (a Stop-hook site) is responsible for treating
# rc=4 as WARN-ONLY — see stop_verification_hook.sh.
expectations_reconcile() {
    local sid="$1" payload="${2:-}"
    [[ -n "$sid" && -n "$payload" ]] || return 0
    local file
    file="$(expectations_file "$sid" 2>/dev/null)" || return 0
    [[ -r "$file" ]] || return 0

    # PRESENT/ABSENT marker on line 1 distinguishes "no background_tasks
    # key at all" (older harness, or the field genuinely does not apply)
    # from "background_tasks is an empty list" (harness currently tracks
    # zero tasks) — both print nothing further from python, so without the
    # marker they would be indistinguishable from bash and the "absent
    # fields => no-op" contract could not be honored.
    #
    # Routed through a FILE, deliberately, not a `$(...)` capture: command
    # substitution strips ALL trailing newlines, so a batch whose LAST N
    # task entries are unidentifiable (each prints an empty line) would
    # silently lose those trailing empty lines and undercount `unidentified`
    # in the SUMMARY below. Measured, not theoretical (nexus-2v0v7 manual
    # verification: a single trailing unidentifiable entry vanished
    # entirely). Reading the file back with `tail`/`head` preserves every
    # line, blank or not.
    local py_file marker
    py_file="$(mktemp 2>/dev/null)" || return 0
    printf '%s' "$payload" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
if not isinstance(d, dict):
    d = {}
tasks = d.get("background_tasks")
if not isinstance(tasks, list):
    print("ABSENT")
    sys.exit(0)
print("PRESENT")
candidates = ("agent_id", "id", "task_id", "taskId", "subagent_id")
scrub = str.maketrans({"\t": " ", "\n": " ", "\r": " "})
for t in tasks:
    ident = ""
    if isinstance(t, dict):
        for k in candidates:
            v = t.get(k)
            if v:
                ident = str(v)
                break
    elif isinstance(t, str):
        ident = t
    print(ident.translate(scrub) if ident else "")
' >"$py_file" 2>/dev/null
    marker="$(head -n 1 "$py_file" 2>/dev/null)"
    if [[ "$marker" != "PRESENT" ]]; then
        rm -f "$py_file" 2>/dev/null
        return 0
    fi

    # Single combined stream, not awk's classic two-file `NR == FNR` array-
    # load idiom: that idiom silently breaks when the FIRST file has zero
    # records (exactly the "background_tasks: []" case this function must
    # handle) — NR stays 0 through an empty first file, so the SECOND
    # file's first line also makes NR==FNR true, and NR/FNR then stay in
    # lockstep for the rest of that file too, so the whole ledger is
    # mis-read as harness ids instead of being parsed as ledger rows.
    # Measured, not theoretical (nexus-2v0v7 manual verification). A single
    # stream with an explicit mode-flip sentinel has no such edge case.
    local combined_file sentinel="___EXPECTATIONS_RECONCILE_SENTINEL___"
    combined_file="$(mktemp 2>/dev/null)" || { rm -f "$py_file" 2>/dev/null; return 0; }
    {
        tail -n +2 "$py_file"
        printf '%s\n' "$sentinel"
        cat "$file"
    } >"$combined_file"
    rm -f "$py_file" 2>/dev/null

    local rc
    awk -F'\t' -v sentinel="$sentinel" '
        mode == 0 && $0 == sentinel { mode = 1; next }
        mode == 0 {
            if ($0 != "") { hids[$0] = 1; htotal++ } else { unident++ }
            next
        }
        $2 == "START" {
            if (!($3 in allstart)) { allstart[$3] = 1; stype[$3] = $4; order[++n] = $3 }
        }
        $2 == "REPORTED" || $2 == "BLOCKED" || $2 == "WOULDBLOCK" { term[$3] = 1 }
        END {
            outstanding = 0; stranded = 0
            for (i = 1; i <= n; i++) {
                id = order[i]
                if (!(id in term)) {
                    outstanding++
                    if (!(id in hids)) { print "STRANDED\t" id "\t" stype[id]; stranded++ }
                }
            }
            undecl = 0
            for (h in hids) { if (!(h in allstart)) { print "UNDECLARED_TASK\t" h; undecl++ } }
            printf "SUMMARY\toutstanding=%d harness_tasks=%d unidentified=%d stranded=%d undeclared_tasks=%d\n", \
                outstanding, (htotal + 0) + (unident + 0), unident + 0, stranded, undecl
            if (stranded > 0) exit 4
            if (undecl > 0) exit 2
            exit 0
        }
    ' "$combined_file"
    rc=$?
    rm -f "$combined_file" 2>/dev/null
    return $rc
}
