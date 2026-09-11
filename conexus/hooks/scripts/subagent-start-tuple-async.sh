#!/bin/bash
# SubagentStart async projection entry — RDR-205 Phase 2 Step 3 (bead
# nexus-em75s.11). Writes the ledger START tuple. A SIBLING of
# subagent-start.sh / subagent-start-stamp.sh in hooks.json's
# "SubagentStart" hooks array — never a child of either (a child that
# inherits a blocking hook's fds holds the whole dispatch, CA 4).
#
# INERT-SAFE BY CONSTRUCTION, not by trusting `async: true`. The actual
# work (endpoint/lease resolution + one urllib POST — nexus-em75s.12
# review fix replaced the original curl invocation, which put the
# bearer in that process's argv) runs in a detached background subshell
# whose stdin/stdout/stderr are ALL redirected to /dev/null before
# backgrounding; this script then exits immediately. Research 5
# (RDR-205, Claude Code 2.1.266) measured that exact shape at 18ms
# regardless of whether the harness honors `async: true` on this event
# — so even an installed harness that silently ignored the `async` key
# on a hooks.json entry (treating it as an ordinary blocking hook)
# would still see this script's own fds close in tens of milliseconds,
# never the seconds a POST round trip against a down/rate-limited
# engine could otherwise cost.
#
# NO HOOK MINTS ANYTHING: all resolution/posting logic lives in the
# stdlib-only sibling tuple_ledger_project.py (never nexus.db.data_token,
# never a mint call). This script's own stdout/stderr/exit code are never
# read by the harness on an async entry; tuple_ledger_project.py logs its
# own skip/failure reasons to a file beside the session's expectations
# ledger.

PAYLOAD="$(cat)"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

(
    printf '%s' "$PAYLOAD" | python3 "$HERE/tuple_ledger_project.py" start
) </dev/null >/dev/null 2>&1 &
disown 2>/dev/null || true

exit 0
