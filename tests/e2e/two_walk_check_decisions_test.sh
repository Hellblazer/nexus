#!/usr/bin/env bash
# Behavioural test of two-walk-check.sh's three decisions (nexus-9a6io round 4):
# how walk 1 is pinned, what the final line says, and the start-count guard.
# The script itself boots an engine three times; these decisions do not. This
# extracts the functions from the REAL script (never a copy) and drives them
# with fixed numbers.
#
# Prints "two_walk_check_decisions_test.sh: N passed, M failed";
# tests/scripts/test_shell_suite_wiring.py holds the floor.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/two-walk-check.sh"
NAME="two_walk_check_decisions_test.sh"
PASS=0
FAIL=0

TMP="$(mktemp -d "${TMPDIR:-/tmp}/twowalk-decisions-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

: > "$TMP/fn.sh"
for fn in _walk1_mode _final_sentinel; do
  sed -n "/^${fn}() {/,/^}/p" "$SCRIPT" >> "$TMP/fn.sh"
done
sed -n '/^_starts_grew_by_one() /p' "$SCRIPT" >> "$TMP/fn.sh"
for fn in _walk1_mode _final_sentinel _starts_grew_by_one; do
  grep -q "^${fn}() " "$TMP/fn.sh" || { echo "[FAIL] $fn not found in $SCRIPT"; echo "$NAME: 0 passed, 1 failed"; exit 1; }
done
if [ "$(wc -l < "$TMP/fn.sh")" -lt 13 ]; then
  echo "[FAIL] decision functions not found in $SCRIPT (extracted $(wc -l < "$TMP/fn.sh") lines)"
  echo "$NAME: 0 passed, 1 failed"
  exit 1
fi

ok()   { PASS=$((PASS + 1)); echo "[ok]   $1"; }
fail() { FAIL=$((FAIL + 1)); echo "[FAIL] $1"; }

# expect_out <label> <want> <command...>
expect_out() {
  local label="$1" want="$2" out; shift 2
  # shellcheck disable=SC1091
  out="$(source "$TMP/fn.sh"; "$@")"
  if [ "$out" = "$want" ]; then ok "$label"; else fail "$label: want '$want', got '$out'"; fi
}

expect_out "walk 1 that added 3 rows -> pinned as recorded 3" "recorded 3" _walk1_mode 491 494
expect_out "walk 1 that added 1 row -> recorded 1" "recorded 1" _walk1_mode 10 11
expect_out "walk 1 that added none -> noop (never an unpinned walk)" "noop" _walk1_mode 494 494
expect_out "a table that shrank -> negative, which the script turns into a failure" "negative" _walk1_mode 494 490

# The sentinel: only a walk that applied something may end plain PASSED.
expect_out "recorded walk 1 -> plain PASSED" "TWO-WALK CHECK PASSED" _final_sentinel recorded
out="$(source "$TMP/fn.sh"; _final_sentinel noop)"
case "$out" in
  "TWO-WALK CHECK PASSED"?*"NOT exercised)") ok "a no-op walk 1 -> PASSED with the NOT exercised suffix" ;;
  *) fail "a no-op walk 1 must carry the suffix, got '$out'" ;;
esac
if [ "$out" = "TWO-WALK CHECK PASSED" ]; then fail "a no-op walk 1 ended the plain sentinel"; else ok "a no-op walk 1 does not end the plain sentinel"; fi

# The start-count guard: exactly one new schema_migration_start.
guard() {
  local label="$1" want="$2" before="$3" after="$4" rc
  # shellcheck disable=SC1091
  ( source "$TMP/fn.sh"; _starts_grew_by_one "$before" "$after" ); rc=$?
  if [ "$rc" = "$want" ]; then ok "$label"; else fail "$label: want rc=$want, got rc=$rc"; fi
}
guard "log grew by one start -> ok" 0 3 4
guard "log did not grow (a boot that logged nothing: the slice would re-read the last one) -> refused" 1 3 3
guard "log grew by two -> refused" 1 3 5
guard "log shrank -> refused" 1 3 2

# Wiring: the script calls them where the decision matters.
wired() {
  local label="$1" pattern="$2"
  if grep -Eq -- "$pattern" "$SCRIPT"; then ok "$label"; else fail "$label: /$pattern/ not found in $SCRIPT"; fi
}
wired "walk 1 pinned with --expect-recorded" '--expect-recorded "\$\{WALK1_PLAN#recorded \}"'
wired "the final line comes from _final_sentinel" '^_final_sentinel "\$WALK1_WORD"$'
wired "a shrinking table is a failure" '\*\) _fail "public.databasechangelog shrank'
wired "both walks assert one new start" '_starts_grew_by_one "\$STARTS_BEFORE_1"'

echo "$NAME: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
