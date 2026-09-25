#!/usr/bin/env bash
# tests/e2e/run_sh_credentials_test.sh — RDR-219 P2.1b (nexus-wauo1.11):
# structural regression test proving tests/e2e/run.sh has migrated onto
# `claude_credentials.py run --` / `status`, and no longer touches
# `.credentials.json` (the operator's own interactive login file — RDR-219
# rule 1 forbids a harness from using it).
#
# Deliberately structural, not behavioral: exercising the real launch path
# means starting a real tmux server and, for the full picture, a real paid
# Claude Code session — that's this bead's own PROOF
# (`tests/e2e/run.sh --scenario 01`), run once by hand, not from a test
# loop. This test greps run.sh's own source for the wiring that PROOF run
# depends on, so a regression is caught for free on every run.
#
# Self-provisioning, no ambient state: reads only the checked-in run.sh.
# Run directly:
#   bash tests/e2e/run_sh_credentials_test.sh
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$HERE/run.sh"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

if [[ ! -f "$TARGET" ]]; then
    echo "FAIL: $TARGET not found — nothing to check" >&2
    exit 1
fi

# 1. The tmux SERVER start (the `new-session` call that actually spawns a
#    fresh server when none is running on the private socket) is wrapped by
#    claude_credentials.py `run --`, so the server's own inherited
#    environment — not the invoking shell's — carries the token (RDR-219
#    tmux trap, bead item 6). CRED_TOOL must both point at
#    lib/claude_credentials.py and be the thing that wraps the actual
#    `tmux -L ... new-session` call — checked separately so a rename of
#    either half still fails this test.
if grep -qE '^CRED_TOOL="\$SCRIPT_DIR/lib/claude_credentials\.py"' "$TARGET" \
    && grep -qE '"\$CRED_TOOL"\s+run\s+--\s+tmux\s+.*-L.*new-session' "$TARGET"; then
    pass "tmux server start (new-session) routes through claude_credentials.py run -- on a -L socket"
else
    fail "no CRED_TOOL=.../claude_credentials.py + '\$CRED_TOOL run -- tmux -L ... new-session' pair in run.sh"
fi

# 2. Every OTHER run.sh-level tmux invocation (kill-session, send-keys)
#    routes through lib.sh's existing _tmux() wrapper (NX_TMUX_SOCKET) —
#    never a bare `tmux kill-session`/`tmux send-keys` against the shared
#    default socket, so this harness never joins a server that was already
#    running (the exact failure mode item 6 warns never reaches the pane).
if grep -qE '^\s*tmux (kill-session|send-keys)\b' "$TARGET"; then
    fail "run.sh still issues a bare 'tmux kill-session'/'tmux send-keys' (bypasses the private socket)"
else
    pass "run.sh's own kill-session/send-keys calls route through _tmux (private socket)"
fi

# 3. The prerequisite gate FUNCTIONALLY calls claude_credentials.py status
#    (via CRED_TOOL) instead of testing for a cached
#    .claude-auth/.credentials.json file — matched on the actual
#    invocation, not merely a mention of "status" in a doc comment
#    (the header prerequisites comment names the same command as an
#    example, which would make a substring match on "status" alone
#    vacuous).
if grep -qE '"\$CRED_TOOL"\s+status\b' "$TARGET"; then
    pass "prerequisite gate calls \$CRED_TOOL status"
else
    fail "no '\$CRED_TOOL status' call in run.sh"
fi

# 4. No OPERATIONAL read or write of a `.credentials.json` path remains —
#    that file is the operator's own interactive login (RDR-219 rule 1: a
#    harness must never touch it). Prose mentioning the filename (e.g. to
#    explain what was removed and why) is fine and expected; what must be
#    absent is a `cp`/redirect/`-f` test actually touching such a path, and
#    the now-dead AUTH_DIR variable that only ever pointed at one.
if grep -qE '(cp\s+|>\s*"?\$?\{?TEST_HOME|-f\s+"?\$)[^#]*\.credentials\.json' "$TARGET"; then
    fail "an operational .credentials.json read/write remains in run.sh"
elif grep -q '^AUTH_DIR=' "$TARGET"; then
    fail "AUTH_DIR variable (pointed only at .claude-auth/.credentials.json) still declared in run.sh"
else
    pass "no operational .credentials.json read/write and no AUTH_DIR remain in run.sh"
fi

# 5. The oauthAccount seed copy (claude.json from .claude-auth/) is gone —
#    Phase 0 (T2 nexus_rdr/219-research-9) confirmed the A1 launch shape
#    passes without it ("Both .claude.json variants passed, onboarding-only
#    and onboarding plus oauthAccount").
if grep -qE 'cp\s+"?\$?\{?AUTH_DIR[^#]*claude\.json' "$TARGET"; then
    fail "AUTH_DIR/claude.json seed copy still present in run.sh"
else
    pass "no AUTH_DIR/claude.json seed copy remains in run.sh"
fi

# 6. RDR-219 Phase 2 Step 2 (bead item 2, mechanism 9): the caller's own
#    ANTHROPIC_API_KEY value must never be written into $TEST_HOME/.env.test
#    -- it already reaches the tmux SERVER's environment (and so every pane)
#    through the `$CRED_TOOL run -- tmux ... new-session` call at check 1,
#    which execs with a copy of the caller's own os.environ. Writing the
#    literal value into .env.test as well would put a plaintext credential
#    on disk for no operational reason. A defensive `unset ANTHROPIC_API_KEY`
#    line (written when the caller's env does NOT carry a key, so a stale
#    export from an earlier .env.test can't linger) is fine and expected.
if grep -qE 'export ANTHROPIC_API_KEY=\\?"\$ANTHROPIC_API_KEY\\?"' "$TARGET"; then
    fail "run.sh still writes the ANTHROPIC_API_KEY value into .env.test"
else
    pass "no plaintext ANTHROPIC_API_KEY value is written into .env.test"
fi

# 7. cleanup() tears down the private-socket tmux SERVER, not merely the
#    session -- the server started at check 1 carries
#    CLAUDE_CODE_OAUTH_TOKEN in its own environment (the tmux trap: env is
#    fixed at server start), so kill-session alone leaves the token-bearing
#    server running after the harness exits (nexus-wauo1.19). Matched
#    inside cleanup()'s own body, routed through _tmux (private socket
#    only, never a bare `tmux kill-server` against the shared default
#    socket -- that's the same hazard check 2 already guards for
#    kill-session/send-keys).
cleanup_body="$(awk '/^cleanup\(\) \{/,/^\}/' "$TARGET")"
if [[ -z "$cleanup_body" ]]; then
    fail "no cleanup() function found in run.sh to check for a tmux server teardown"
elif grep -qE '_tmux kill-server\b' <<<"$cleanup_body"; then
    pass "cleanup() kills the private-socket tmux server (_tmux kill-server), not just the session"
else
    fail "cleanup() never calls '_tmux kill-server' -- the token-bearing tmux server on \$NX_TMUX_SOCKET outlives the run"
fi
if grep -qE '^\s*tmux kill-server\b' <<<"$cleanup_body"; then
    fail "cleanup() issues a bare 'tmux kill-server' (bypasses the private socket) instead of '_tmux kill-server'"
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
