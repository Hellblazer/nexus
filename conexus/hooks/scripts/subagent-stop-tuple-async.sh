#!/bin/bash
# SubagentStop async projection entry — RDR-205 Phase 2 Step 3 (bead
# nexus-em75s.11). Writes the ledger REPORT tuple. A SIBLING of
# subagent-stop.sh in hooks.json's "SubagentStop" hooks array — never a
# child of it (CA 4; see subagent-start-tuple-async.sh for the full
# inert-safe rationale, identical here).
#
# The report tuple needs no cooperation from the stopping agent: it is
# keyed on the same harness-issued agent_id the SubagentStart payload
# carried, which subagent-start.sh already injected into that agent's
# own context as its claimant id (RDR-205 "Identity and addressing").
# This hook writes it independently from the Stop payload's own
# agent_id/agent_type fields.

PAYLOAD="$(cat)"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

(
    printf '%s' "$PAYLOAD" | python3 "$HERE/tuple_ledger_project.py" report
) </dev/null >/dev/null 2>&1 &
disown 2>/dev/null || true

exit 0
