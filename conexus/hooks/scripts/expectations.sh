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
# NAME MORPHOLOGY (scenario 27, verified on live sessions): a NAMED agent
# reaches the hooks with agent_type == <name> and agent_id ==
# "a<name>-<hash>"; an UNNAMED agent has agent_type == <subagent_type> and
# agent_id == "a<hash>" (no "a<name>-" prefix). Consequences baked in
# below:
#   - The consult rule requires BOTH agent_type == EXPECTed name AND the
#     "a<name>-" agent_id prefix, so a sync Task whose subagent_type
#     happens to equal an expected name can never be blocked.
#   - Only NAMED background dispatches are enforceable. The dispatch
#     convention therefore requires a name on every background teammate;
#     an unnamed background dispatch simply falls outside the guard
#     (fail-open), it does not break anything.
#
# TYPE KEYING (nexus-qc4p1 / nexus-nu7fo, measured 2026-07-31 against the
# live Claude Code hook payloads). The morphology above is unreachable in
# the harness this repo actually runs in: its Agent tool takes NO `name`
# parameter (tool_input is exactly description / prompt / subagent_type /
# run_in_background), so every dispatch arrives UNNAMED — agent_id
# "a<hash>", agent_type == subagent_type — and `recognized` was
# structurally pinned at 0 across 25 consecutive dispatches. The one
# value BOTH sides of the ledger can know is the agent TYPE:
#   PreToolUse(Agent).tool_input.subagent_type == SubagentStart.agent_type
# (verified identical, same session_id, for general-purpose x2 + Explore
# in one turn). So EXPECT rows written by the PreToolUse hook
# (agent-dispatch-expect.sh) are keyed on subagent_type, and the audit
# surfaces below recognise a START row by EITHER key: the legacy
# "a<name>-" morphology OR its agent_type matching an EXPECT name.
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
#   <iso-utc-ts> TAB BLOCKED  TAB <agent_id>
#   <iso-utc-ts> TAB CONSUMED TAB <agent_id> TAB <agent_type>
# (REPORTED and WOULDBLOCK are stop-hook-emitted terminal verbs written by
# subagent-stop.sh, not by this file — documented at that script instead.)
# The EXPECT row's optional 5th field is the dispatching tool_use_id. No
# reader interprets it; it exists so (a) the writing hook is idempotent
# under double registration and (b) two same-type dispatches inside one
# second are not collapsed by the exact-duplicate-line filter, which
# would silently under-count the N-of-type deficit.
# No JSON: writers are bash one-liners and LLM-authored echos; a malformed
# LINE costs one entry, a malformed json file would cost the session.
# Reads are awk exact-field comparisons plus one quoted-literal glob
# prefix check (the morphology gate — quoting keeps caller metacharacters
# inert, and the charset gate above it enforces that). No locks:
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
# class (introduced by dropping morphology) is now closed.
#
# LOCKING (nexus-bk974, P2 SIGNIFICANT, same critique round): the
# read-decide-append below is wrapped in the SAME bounded mkdir lockdir
# pattern as agent-dispatch-expect.sh's _expect_if_absent (nexus-3h0u6
# precedent) — without it, concurrent same-type stops racing the last
# unit of credit could each observe unspent credit and each append
# CONSUMED, over-crediting relative to N-of-type intent (this repo's own
# CLAUDE.md explicitly encourages parallel same-type dispatch fleets, so
# this is not a hypothetical). Fail-open on lock timeout: never block a
# stop because of lock contention — an unheld window just degrades this
# one call back to the pre-lock racy read, it never denies or defers the
# stop itself.
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
# before proceeding unlocked — stderr only (stdout may be parsed by
# callers), and it is a diagnostic breadcrumb only, never a ledger row
# (the file format's writers are exactly EXPECT/START/BLOCKED/CONSUMED,
# per FORMAT above). CAVEAT (substantive-critic, matching the honest
# precedent in pre_close_verification_hook.sh::_stamp_ids): subagent-
# stop.sh exits 0 on every path (block is signaled via stdout JSON, not
# exit 2), and the documented hook contract only feeds exit-0 stderr back
# to the operator under `claude --debug` — this warning is observable in
# hook debug logs, not a normal-session operator-facing signal.
expectations_owes_report() {
    local sid="$1" agent_id="${2:-}" agent_type="${3:-}"
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

    # Bounded lock (nexus-bk974). A lockdir left behind by a killed hook
    # would otherwise cost every later stop of this session the full
    # budget forever, so reap one that has sat idle past the critical
    # section's realistic worst case before contending for it — same
    # reap-then-contend shape as agent-dispatch-expect.sh's LOCKDIR.
    local lockdir="${file}.owes.lock" held=""
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
        echo "expectations: owes-report lock budget exhausted (${tries} tries); proceeding unlocked (fail-open, nexus-bk974/nexus-4b8sz)" >&2
    fi

    local verdict
    verdict="$(awk -F'\t' -v id="$agent_id" -v ty="$agent_type" '
        $2 == "EXPECT" && $3 == ty {
            if ($4 == "background") { credit++ } else { mixed = 1 }
        }
        $2 == "CONSUMED" && $4 == ty {
            if ($3 == id) { self = 1 } else { spent++ }
        }
        END {
            # nexus-rkigh: the unmixed-ness gate is checked FIRST and is
            # unconditional — it overrides even a pre-existing self
            # CONSUMED row (see the WHY THE HYBRID comment above the
            # function for why that asymmetry is intentional).
            if (mixed) { print "no"; exit }
            if (self) { print "self"; exit }
            if (credit > spent) { print "new"; exit }
            print "no"
        }
    ' "$file" 2>/dev/null)"
    case "$verdict" in
        self)
            [[ -n "$held" ]] && rmdir "$lockdir" 2>/dev/null
            return 0
            ;;
        new)
            _expectations_append "$file" \
                "$(_expectations_ts)"$'\tCONSUMED\t'"$agent_id"$'\t'"$agent_type"
            [[ -n "$held" ]] && rmdir "$lockdir" 2>/dev/null
            return 0
            ;;
        *)
            [[ -n "$held" ]] && rmdir "$lockdir" 2>/dev/null
            return 1
            ;;
    esac
}

# expectations_mark_blocked <session_id> <agent_id> — record that the stop
# hook has blocked this agent once. Pairs with expectations_already_blocked
# for the block-at-most-once guard (belt to stop_hook_active's braces).
expectations_mark_blocked() {
    local sid="$1" agent_id="${2:-}"
    if [[ -z "$sid" || -z "$agent_id" ]]; then
        echo "expectations_mark_blocked: ERROR — usage: expectations_mark_blocked <session_id> <agent_id>" >&2
        return 2
    fi
    # Same guard as expectations_start: a tab/newline here would misalign
    # the BLOCKED row, and expectations_already_blocked's exact-field
    # match would then never fire — silently defeating block-at-most-once.
    if [[ "$agent_id" == *$'\t'* || "$agent_id" == *$'\n'* ]]; then
        echo "expectations_mark_blocked: ERROR — tab/newline in agent_id" >&2
        return 2
    fi
    local file
    file="$(expectations_file "$sid")" || return 2
    _expectations_append "$file" "$(_expectations_ts)"$'\tBLOCKED\t'"$agent_id"
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
# per NAMED-morphology START row (agent_id == "a<agent_type>-<hash>")
# that has NO EXPECT row for that name. An EXPECT row of EITHER mode
# suppresses — a deliberately-declared named-sync dispatch stays
# audit-clean. Unnamed (sync-shaped) dispatches are never flagged: their
# START rows lack the morphology, and finding 4 already proves sync
# dispatches cannot idle-without-report. Missing/unreadable file => no
# output, exit 0 (fail-open, like every consult surface here).
#
# BLIND-SPOT DISCLOSURE (bead nexus-mk3tw): a "prompt-named" dispatch —
# a unique name embedded in the Agent-tool PROMPT text rather than in an
# actual name= field the framework threads into the hook payload — never
# acquires the "a<name>-<hash>" morphology this audit keys on. Its START
# row arrives with agent_type == the bare subagent_type and agent_id ==
# "a<hash>" — structurally IDENTICAL to an ordinary anonymous sync
# dispatch. There is no field in the payload that tells these two apart
# (scenario 27); the guard cannot recover the orchestrator's intent from
# data that was never sent. Silently reporting "no UNDECLARED lines" in
# that case (the pre-mk3tw behavior) reads as "all clear" when it is
# really "recognised nothing" — confirmed reproduced 2026-07-25,
# 4-then-6 named background dispatches / 0 EXPECT rows / undeclared=0.
#
# The fix does not (cannot) recover per-dispatch detection. It makes the
# aggregate honest: every call now ALSO prints one
#   SUMMARY\tchecked=<N> recognized=<M> unrecognized=<N-M> undeclared=<K>
# line, where checked = every unique START'd agent_id regardless of
# morphology, recognized = the subset with named morphology (the only
# ones this audit can classify at all), unrecognized = the rest (could
# be ordinary sync dispatches OR blind-spotted prompt-named background
# ones — indistinguishable from here, by construction).
#
# HARD FAILURE (never a silent pass): when checked > 0 and recognized ==
# 0 — the guard saw dispatches but its filters matched NONE of them —
# that is the exact false-clean shape from the bead. The function still
# prints every line (UNDECLARED rows if any, plus SUMMARY) but returns
# exit 1 instead of 0, and prints a BLINDSPOT line calling it out by
# name. A caller that only greps for "UNDECLARED" and ignores the exit
# code repeats the original mistake; the exit code is the part that
# cannot be misread as silence.
#
# UNDECLARED-DEFICIT EXIT CODE (nexus-suuja): a genuine undeclared>0
# result was previously rc-invisible — the same exit 0 as a fully clean
# run — so a caller keying on rc alone (not parsing SUMMARY/UNDECLARED
# lines) could not tell "nothing to report" from "N recognized
# dispatches never got a report". This function now exits 2 when
# undeclared>0, checked strictly AFTER the recognized==0 BLINDSPOT check
# above, which still takes priority and still exits 1. RC CONTRACT:
#   0 = clean (no undeclared, not a blindspot)
#   1 = recognized==0 blindspot — false-clean, not a pass
#   2 = undeclared>0 — a real declaration-completeness deficit
# See AGENTS.md's ledger usage snippet for the caller-facing summary.
#
# WIDENED RECOGNISER (nexus-qc4p1): recognized now means "pairable by
# EITHER key" — legacy name-morphology, OR the START row's agent_type
# appearing among EXPECT names. The second key is what the PreToolUse
# dispatch hook writes, and it is the only one reachable in a harness
# whose Agent tool has no name parameter (see TYPE KEYING in the header).
# Matching is N-OF-TYPE and credit-consuming in first-appearance order:
# three STARTs of a type with two EXPECT rows yield exactly one
# UNDECLARED line, so a PARTIAL mechanization failure is visible instead
# of being absorbed by set membership. The BLINDSPOT hard failure is
# unchanged and still fires when nothing at all was checkable — which is
# precisely what an uninstalled/inert dispatch hook looks like.
expectations_undeclared() {
    local sid="$1"
    [[ -n "$sid" ]] || return 0
    local file
    file="$(expectations_file "$sid" 2>/dev/null)" || return 0
    [[ -r "$file" ]] || return 0
    awk -F'\t' '
        # Recognition is decided in END, not on the START line: an EXPECT
        # row for the type may be anywhere in the file, so a single pass
        # cannot classify a START row as it arrives.
        $2 == "START" {
            if (!($3 in allstart)) {
                allstart[$3] = 1; checked++
                order[++n] = $3; stype[$3] = $4
                named[$3] = (index($3, "a" $4 "-") == 1 && length($3) > length($4) + 2)
            }
        }
        # Dedupe by dispatch_id: the writing hook takes a bounded lock, so a
        # double registration that outlasts the budget can append the SAME
        # dispatch twice. A duplicate EXPECT row is not the harmless nuisance
        # a duplicate START row is — it inflates the credit pool and MASKS an
        # undeclared start. The reader can settle it unambiguously, so it does.
        $2 == "EXPECT" && $5 != "" && ($5 in dseen) { next }
        $2 == "EXPECT" { if ($5 != "") dseen[$5] = 1; e[$3] = 1; credit[$3]++ }
        END {
            undeclared = 0
            for (i = 1; i <= n; i++) {
                id = order[i]; t = stype[id]
                # Recognised by EITHER key: legacy name-morphology, or the
                # agent TYPE appearing among EXPECT names (the key the
                # PreToolUse hook writes). Unrecognised ids are neither
                # counted nor flagged — the pre-existing contract, so an
                # ordinary undeclared sync dispatch is never false-flagged.
                if (!named[id] && !(t in e)) continue
                recognized++
                if (credit[t] > 0) { credit[t]--; continue }   # N-of-type
                print "UNDECLARED\t" id "\t" t
                undeclared++
            }
            unrecognized = checked - recognized
            printf "SUMMARY\tchecked=%d recognized=%d unrecognized=%d undeclared=%d\n", \
                checked, recognized, unrecognized, undeclared
            if (checked > 0 && recognized == 0) {
                print "BLINDSPOT\tguard recognised 0 of " checked " dispatched agent(s) by name-morphology or agent-type - undeclared=" undeclared " here is NOT evidence of compliance, it means nothing was checkable"
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
# BLIND-SPOT DISCLOSURE + HARD FAILURE (bead nexus-mk3tw — same defect
# class as expectations_undeclared above, see its header for the full
# mechanism writeup: a "prompt-named" background dispatch never acquires
# the "a<name>-<hash>" morphology this census's per-agent view keys on,
# so it is structurally indistinguishable from an anonymous sync
# dispatch. Reproduced 2026-07-25: expect=0 start=6, and
# "undeclared=0"/"no_terminal=0" read as clean while the census had in
# fact classified NONE of the 6 by name — a guard reporting undeclared=0
# when it recognised 0 of N dispatches is worse than reporting nothing.
#
# checked = every unique agent_id that ever got a START row, regardless
# of morphology. recognized = the subset the AGENT/CLASSIFIED lines can
# say anything about at all. When checked > 0 and recognized == 0 this
# function still prints every line it would normally print, but ALSO
# exits 1 instead of 0 — the exit code is the part a caller cannot
# mistake for a clean run even if it only skims stdout.
#
# WIDENED RECOGNISER (nexus-qc4p1), matching expectations_undeclared:
# recognized = named morphology OR agent_type present among EXPECT names,
# and declared/undeclared is decided by N-OF-TYPE credit in
# first-appearance order rather than set membership. EXPECTED_NO_START
# likewise compares COUNTS (a type with 2 EXPECT rows and 1 START is
# still short one). Agents pairable by neither key stay unlisted — the
# pre-existing contract that keeps ordinary sync dispatches from
# false-flagging — but they are still counted in checked, so the
# BLINDSPOT line and its exit 1 still see them.
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
                named[$3] = (index($3, "a" $4 "-") == 1 && length($3) > length($4) + 2)
                if (!($3 in listed)) { order[++n] = $3; listed[$3] = 1 }
            }
        }
        $2 == "REPORTED" || $2 == "BLOCKED" || $2 == "WOULDBLOCK" {
            if (!($3 in listed)) { order[++n] = $3; listed[$3] = 1 }
            # Explicit set, NOT "id in term": term[] is READ in the END
            # loop, and an awk read auto-vivifies the key, so membership
            # in term[] is worthless there (the caveat two comments up).
            hasterm[$3] = 1
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
                if ((id in allstart) && !named[id] && !(ty in expect)) {
                    # Started, but pairable by neither key. Listing it would
                    # false-flag every ordinary sync dispatch as undeclared,
                    # which is the pre-mk3tw over-firing this audit already
                    # rejected once. It still counts in BLINDSPOT/checked.
                    if (!(id in hasterm)) continue
                    ty = ""
                }
                if (ty == "") {
                    print "AGENT\t" id "\t-\t" t "\tno-start"
                    nostart++
                } else {
                    recognized++
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
            if (checked > 0 && recognized == 0) exit 1
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
expectations_sweep() {
    local dir
    dir="$(_expectations_dir)"
    find "$dir" -maxdepth 1 -name '*.expectations' -type f -mtime +7 -delete 2>/dev/null
    return 0
}
