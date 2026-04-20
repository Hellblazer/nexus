#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# E2E sandbox for the T1-UUID-keying fix (fix/t1-session-uuid-keying).
#
# Exercises the SessionStart/SessionEnd hook lifecycle against real chroma
# servers in an isolated NEXUS_CONFIG_DIR, so we can see the bug fix
# working without touching the user's real ~/.config/nexus/.
#
# Verifies, end-to-end:
#   1. Two distinct UUIDs → two distinct session files → two distinct chroma
#      servers on different ports (the original bug fix)
#   2. Same UUID twice → adopts the existing record, no second server
#      (subagent inheritance)
#   3. NEXUS_SKIP_T1=1 → no chroma server started (claude_dispatch path)
#   4. Legacy numeric-stem session files are swept on first SessionStart
#      (migration from the broken PID-keyed scheme)
#   5. SessionEnd reaps the session file + tmpdir + chroma server
#
# Usage:
#   ./scripts/sandbox-t1-uuid.sh
#
# Exit code: 0 on full pass, 1 on any check failure.

set -euo pipefail

# ── Setup ────────────────────────────────────────────────────────────────────

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SANDBOX_DIR="$(mktemp -d "/tmp/nx-sandbox-XXXXXX")"
SESSIONS_DIR="$SANDBOX_DIR/sessions"
PASS=0
FAIL=0

cleanup() {
  echo
  echo "── cleanup ──"
  # Kill any chroma servers we may have spawned
  pgrep -f "chroma run --host 127.0.0.1.*nx_t1_" 2>/dev/null | while read -r pid; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  sleep 0.5
  pgrep -f "chroma run --host 127.0.0.1.*nx_t1_" 2>/dev/null | while read -r pid; do
    kill -KILL "$pid" 2>/dev/null || true
  done
  rm -rf "$SANDBOX_DIR"
  echo "removed $SANDBOX_DIR"
  if [ "$FAIL" -gt 0 ]; then
    echo
    echo "❌ $FAIL check(s) failed (passes: $PASS)"
    exit 1
  else
    echo
    echo "✅ all $PASS check(s) passed"
  fi
}
trap cleanup EXIT

check() {
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "  ✓ $desc"
    PASS=$((PASS + 1))
  else
    echo "  ✗ $desc"
    echo "    command: $*"
    FAIL=$((FAIL + 1))
  fi
}

# Run nx commands with the development copy of conexus (the branch under test)
# and the isolated config dir.
nx_dev() {
  NEXUS_CONFIG_DIR="$SANDBOX_DIR" uv run --quiet --project "$REPO_ROOT" -- nx "$@"
}

# Invoke the SessionStart hook the same way Claude Code does: pipe a JSON
# payload containing a session_id on stdin.
session_start() {
  local uuid="$1"
  echo "{\"session_id\": \"$uuid\"}" | NEXUS_CONFIG_DIR="$SANDBOX_DIR" uv run --quiet --project "$REPO_ROOT" -- nx hook session-start >/dev/null
}

# Variant: SessionStart with NEXUS_SKIP_T1 set — for testing claude_dispatch path
session_start_skip_t1() {
  local uuid="$1"
  echo "{\"session_id\": \"$uuid\"}" | NEXUS_CONFIG_DIR="$SANDBOX_DIR" NEXUS_SKIP_T1=1 \
    uv run --quiet --project "$REPO_ROOT" -- nx hook session-start >/dev/null
}

session_end() {
  local uuid="${1:-}"
  if [ -n "$uuid" ]; then
    NEXUS_CONFIG_DIR="$SANDBOX_DIR" NX_SESSION_ID="$uuid" \
      uv run --quiet --project "$REPO_ROOT" -- nx hook session-end >/dev/null
  else
    NEXUS_CONFIG_DIR="$SANDBOX_DIR" \
      uv run --quiet --project "$REPO_ROOT" -- nx hook session-end >/dev/null
  fi
}

# ── Sandbox banner ────────────────────────────────────────────────────────────

echo "T1 UUID-keying E2E sandbox"
echo "  branch:        $(git -C "$REPO_ROOT" branch --show-current)"
echo "  sandbox dir:   $SANDBOX_DIR"
echo "  sessions dir:  $SESSIONS_DIR"
echo

# ── Test 1: Two distinct UUIDs → two distinct session files + servers ────────

echo "── Test 1: distinct UUIDs get distinct T1 servers (the original bug) ──"

UUID_A="11111111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
UUID_B="22222222-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

session_start "$UUID_A"
session_start "$UUID_B"

check "UUID_A session file present" test -f "$SESSIONS_DIR/$UUID_A.session"
check "UUID_B session file present" test -f "$SESSIONS_DIR/$UUID_B.session"
check "no numeric-stem session files leaked" \
  bash -c "! ls -1 $SESSIONS_DIR/*.session 2>/dev/null | xargs -n1 basename 2>/dev/null | grep -qE '^[0-9]+\.session$'"

PORT_A=$(jq -r .server_port "$SESSIONS_DIR/$UUID_A.session")
PORT_B=$(jq -r .server_port "$SESSIONS_DIR/$UUID_B.session")
PID_A=$(jq -r .server_pid "$SESSIONS_DIR/$UUID_A.session")
PID_B=$(jq -r .server_pid "$SESSIONS_DIR/$UUID_B.session")

check "UUID_A and UUID_B got distinct ports" test "$PORT_A" != "$PORT_B"
check "UUID_A and UUID_B got distinct server PIDs" test "$PID_A" != "$PID_B"
check "UUID_A's chroma server is alive" kill -0 "$PID_A"
check "UUID_B's chroma server is alive" kill -0 "$PID_B"

echo "    UUID_A → port $PORT_A, pid $PID_A"
echo "    UUID_B → port $PORT_B, pid $PID_B"

# ── Test 2: Re-running with same UUID adopts existing record ─────────────────

echo
echo "── Test 2: same UUID adopts existing record (subagent inheritance) ──"

session_start "$UUID_A"  # second invocation, same UUID

PORT_A_AGAIN=$(jq -r .server_port "$SESSIONS_DIR/$UUID_A.session")
PID_A_AGAIN=$(jq -r .server_pid "$SESSIONS_DIR/$UUID_A.session")

check "UUID_A's port unchanged after re-invoke" test "$PORT_A" = "$PORT_A_AGAIN"
check "UUID_A's pid unchanged after re-invoke" test "$PID_A" = "$PID_A_AGAIN"
check "UUID_A's server still the same process" kill -0 "$PID_A"

# ── Test 3: NEXUS_SKIP_T1 → no chroma server started ─────────────────────────

echo
echo "── Test 3: NEXUS_SKIP_T1=1 skips server start (claude_dispatch path) ──"

UUID_SKIP="33333333-cccc-cccc-cccc-cccccccccccc"
CHROMA_COUNT_BEFORE_SKIP=$(pgrep -f 'chroma run --host 127.0.0.1.*nx_t1_' 2>/dev/null | wc -l | tr -d ' ')
session_start_skip_t1 "$UUID_SKIP"
CHROMA_COUNT_AFTER_SKIP=$(pgrep -f 'chroma run --host 127.0.0.1.*nx_t1_' 2>/dev/null | wc -l | tr -d ' ')

check "no session file written for skip-T1 session" \
  bash -c "! test -f $SESSIONS_DIR/$UUID_SKIP.session"
check "no extra chroma server spawned for skip-T1 session" \
  test "$CHROMA_COUNT_AFTER_SKIP" -eq "$CHROMA_COUNT_BEFORE_SKIP"

# ── Test 3b: nested session_start does NOT stomp current_session ─────────────
# Bug-fix coverage for the "operator subprocess overwrites parent's
# current_session pointer" issue. With NX_SESSION_ID inherited (the way
# claude_dispatch sets it), the nested SessionStart must preserve the
# parent's pointer so shell-side `nx scratch` etc. still find the
# parent's T1 record after the operator returns.

echo
echo "── Test 3b: nested SessionStart preserves parent's current_session ──"

# Capture the parent's current_session before the nested call.
PARENT_CURRENT=$(cat "$SANDBOX_DIR/current_session" 2>/dev/null)
NESTED_UUID="55555555-eeee-eeee-eeee-eeeeeeeeeeee"

echo "{\"session_id\": \"$NESTED_UUID\"}" \
  | NEXUS_CONFIG_DIR="$SANDBOX_DIR" \
    NX_SESSION_ID="$PARENT_CURRENT" \
    NEXUS_SKIP_T1=1 \
    uv run --quiet --project "$REPO_ROOT" -- nx hook session-start >/dev/null

CURRENT_AFTER_NESTED=$(cat "$SANDBOX_DIR/current_session" 2>/dev/null)

check "current_session unchanged after nested SessionStart" \
  test "$PARENT_CURRENT" = "$CURRENT_AFTER_NESTED"
check "current_session still points at parent UUID, not nested UUID" \
  test "$CURRENT_AFTER_NESTED" != "$NESTED_UUID"

# ── Test 4: Legacy numeric-stem files swept on next SessionStart ─────────────

echo
echo "── Test 4: legacy {pid}.session migration ──"

# Drop a fresh-looking legacy file masquerading as the old format
LEGACY_FILE="$SESSIONS_DIR/99999.session"
cat > "$LEGACY_FILE" <<EOF
{"session_id": "legacy-uuid", "server_host": "127.0.0.1", "server_port": 65000, "server_pid": 1, "created_at": $(date +%s), "tmpdir": ""}
EOF
check "legacy numeric-stem file present before sweep" test -f "$LEGACY_FILE"

# Trigger sweep via a fresh SessionStart (always runs sweep_stale_sessions first)
UUID_SWEEP="44444444-dddd-dddd-dddd-dddddddddddd"
session_start "$UUID_SWEEP"

check "legacy numeric-stem file removed by sweep" \
  bash -c "! test -f $LEGACY_FILE"
check "UUID-keyed file from this sweep run survives" \
  test -f "$SESSIONS_DIR/$UUID_SWEEP.session"
check "previous UUID-keyed files (A, B) survive sweep" \
  bash -c "test -f $SESSIONS_DIR/$UUID_A.session && test -f $SESSIONS_DIR/$UUID_B.session"

# ── Test 5: SessionEnd reaps server + file + tmpdir ──────────────────────────

echo
echo "── Test 5: SessionEnd cleanup ──"

TMPDIR_A=$(jq -r .tmpdir "$SESSIONS_DIR/$UUID_A.session")
session_end "$UUID_A"

check "UUID_A session file removed by session_end" \
  bash -c "! test -f $SESSIONS_DIR/$UUID_A.session"
check "UUID_A tmpdir removed by session_end" \
  bash -c "! test -d $TMPDIR_A"

# Server process needs a moment to die (SIGTERM → SIGKILL with 3s grace)
sleep 1
check "UUID_A's chroma server reaped" \
  bash -c "! kill -0 $PID_A 2>/dev/null"

# UUID_B (we never ended it) should still be running
check "UUID_B unaffected by UUID_A's session_end" \
  test -f "$SESSIONS_DIR/$UUID_B.session"
check "UUID_B's chroma server still alive" kill -0 "$PID_B"

# ── Done ──────────────────────────────────────────────────────────────────────

echo
echo "── inventory ──"
echo "  session files remaining: $(ls $SESSIONS_DIR/*.session 2>/dev/null | wc -l | tr -d ' ')"
echo "  chroma processes alive:  $(pgrep -f 'chroma run --host 127.0.0.1.*nx_t1_' | wc -l | tr -d ' ')"
