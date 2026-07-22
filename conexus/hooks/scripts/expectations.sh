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
# FORMAT — one file per orchestrator session, append-only TSV, three verbs:
#   <iso-utc-ts> TAB EXPECT  TAB <name>     TAB <background|sync>
#   <iso-utc-ts> TAB START   TAB <agent_id> TAB <agent_type>
#   <iso-utc-ts> TAB BLOCKED TAB <agent_id>
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

# expectations_expect <session_id> <name> <mode> — the ORCHESTRATOR write
# path. MUST be called BEFORE the Agent dispatch (write-before-dispatch is
# the load-bearing ordering — see header). mode is background|sync; only
# background rows ever cause an agent to owe a report.
expectations_expect() {
    local sid="$1" name="${2:-}" mode="${3:-}"
    if [[ -z "$sid" || -z "$name" || -z "$mode" ]]; then
        echo "expectations_expect: ERROR — usage: expectations_expect <session_id> <name> <mode>" >&2
        return 2
    fi
    # Agent-tool name charset ([A-Za-z0-9_-]); also keeps the TSV intact.
    if [[ ! "$name" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]]; then
        echo "expectations_expect: ERROR — invalid name '$name' (must match Agent-tool name charset)" >&2
        return 2
    fi
    if [[ "$mode" != "background" && "$mode" != "sync" ]]; then
        echo "expectations_expect: ERROR — mode must be 'background' or 'sync', got '$mode'" >&2
        return 2
    fi
    local file
    file="$(expectations_file "$sid")" || return 2
    _expectations_append "$file" "$(_expectations_ts)"$'\tEXPECT\t'"$name"$'\t'"$mode"
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
# consult rule (v2, post-determination). Returns 0 iff the stopping agent
# owes a completion report:
#   EXPECT row exists with mode=background and name == agent_type
#   AND agent_id has the named-agent morphology "a<name>-..." for that
#   exact name (kills the subagent_type-collision false-block class).
# Everything else — sync rows, unknown agents, unnamed morphology,
# missing/unreadable file — returns 1 (fail-open, never block).
expectations_owes_report() {
    local sid="$1" agent_id="${2:-}" agent_type="${3:-}"
    [[ -n "$sid" && -n "$agent_id" && -n "$agent_type" ]] || return 1
    # An agent_type outside the name charset can never equal a stored
    # EXPECT name, so it can never owe. Checking it HERE (not just at
    # write time) makes that an enforced invariant rather than an
    # implicit cross-function one — and keeps the glob below literal
    # (no caller-supplied metacharacters in the pattern).
    [[ "$agent_type" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]] || return 1
    # Named-agent morphology gate: agent_id must be "a<agent_type>-...".
    [[ "$agent_id" == "a${agent_type}-"?* ]] || return 1
    local file
    file="$(expectations_file "$sid" 2>/dev/null)" || return 1
    [[ -r "$file" ]] || return 1
    awk -F'\t' -v n="$agent_type" \
        '$2 == "EXPECT" && $3 == n && $4 == "background" { found = 1 } END { exit !found }' \
        "$file" 2>/dev/null
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
expectations_undeclared() {
    local sid="$1"
    [[ -n "$sid" ]] || return 0
    local file
    file="$(expectations_file "$sid" 2>/dev/null)" || return 0
    [[ -r "$file" ]] || return 0
    awk -F'\t' '
        $2 == "START" && index($3, "a" $4 "-") == 1 && length($3) > length($4) + 2 { s[$3] = $4 }
        $2 == "EXPECT" { e[$3] = 1 }
        END { for (id in s) if (!(s[id] in e)) print "UNDECLARED\t" id "\t" s[id] }
    ' "$file" 2>/dev/null
    return 0
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
# Exact-duplicate lines (the nexus-3h0u6 doubling, legacy files) are
# dropped; legitimate repeats (same verb, different timestamp — e.g. one
# REPORTED per round) are kept in ROWS counts. Missing/unreadable file =>
# no output, exit 0 (fail-open, like every consult surface here).
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
        { rows[$2]++ }
        $2 == "EXPECT"  { expect[$3] = 1 }
        $2 == "START" && index($3, "a" $4 "-") == 1 && length($3) > length($4) + 2 {
            if (!($3 in listed)) { order[++n] = $3; listed[$3] = 1 }
            name[$3] = $4
        }
        $2 == "START"   { started[$4] = 1 }
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
                if (name[id] == "") {
                    print "AGENT\t" id "\t-\t" t "\tno-start"
                    nostart++
                } else {
                    d = (name[id] in expect) ? "declared" : "undeclared"
                    print "AGENT\t" id "\t" name[id] "\t" t "\t" d
                    if (d == "undeclared") undeclared++
                }
                cls[t]++
            }
            ens = 0
            for (nm in expect) if (!(nm in started)) { print "EXPECTED_NO_START\t" nm; ens++ }
            printf "ROWS\texpect=%d start=%d reported=%d blocked=%d wouldblock=%d\n", \
                rows["EXPECT"], rows["START"], rows["REPORTED"], rows["BLOCKED"], rows["WOULDBLOCK"]
            printf "CLASSIFIED\treported=%d blocked_resolved=%d (immediate=%d later=%d) blocked_unresolved=%d wouldblock=%d no_terminal=%d undeclared=%d no_start=%d expected_no_start=%d\n", \
                cls["REPORTED"], cls["BLOCKED_RESOLVED"], res_immediate, res_later, \
                cls["BLOCKED_UNRESOLVED"], \
                cls["WOULDBLOCK"], cls["NO_TERMINAL"], undeclared, nostart, ens
        }
    ' "$file" 2>/dev/null
    return 0
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
