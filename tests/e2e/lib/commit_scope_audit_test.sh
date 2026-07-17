#!/usr/bin/env bash
# tests/e2e/lib/commit_scope_audit_test.sh — synthetic-foreign-file
# acceptance test for commit_scope_audit.sh (RDR-184 P0.3, nexus-ccs9v.3).
#
# Self-provisioning: builds its OWN throwaway git repo under a tmpdir and
# makes commits inside it. Never touches this repo's real history. Run
# directly with bash: `bash tests/e2e/lib/commit_scope_audit_test.sh`.
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUDIT="$HERE/commit_scope_audit.sh"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/commit_scope_audit_test.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT

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

REPO="$WORKDIR/throwaway-repo"
mkdir -p "$REPO"
git -C "$REPO" init -q -b main
git -C "$REPO" config user.email "test@example.invalid"
git -C "$REPO" config user.name "commit_scope_audit_test"

mkdir -p "$REPO/src" "$REPO/secrets"

# Commit 1: in-scope only (src/).
echo "hello" >"$REPO/src/a.txt"
git -C "$REPO" add src/a.txt
git -C "$REPO" commit -q -m "in-scope commit 1"
c1="$(git -C "$REPO" rev-parse HEAD)"

# Commit 2: the synthetic foreign file — touches src/ (in-scope) AND
# secrets/ (out-of-scope) in the SAME commit.
echo "world" >"$REPO/src/b.txt"
echo "leaked" >"$REPO/secrets/leak.txt"
git -C "$REPO" add src/b.txt secrets/leak.txt
git -C "$REPO" commit -q -m "foreign-file commit"
c2="$(git -C "$REPO" rev-parse HEAD)"

# Commit 3: back to in-scope only.
echo "again" >"$REPO/src/c.txt"
git -C "$REPO" add src/c.txt
git -C "$REPO" commit -q -m "in-scope commit 3"
c3="$(git -C "$REPO" rev-parse HEAD)"

# ── Test A: full history (c3 = HEAD, root commit auto-included via ──────
# ── "<ref>" single-rev form), allowlist = src/ only -> must flag c2 only ─
echo "Test A: synthetic foreign file is flagged, and only that commit"
OUT="$(cd "$REPO" && bash "$AUDIT" "${c3}" "src" 2>&1)"
RC=$?

if [[ $RC -ne 0 ]]; then
    ok "exit code is nonzero (1 expected) when a foreign file is present: got $RC"
else
    bad "exit code was 0 despite a foreign file being present"
fi

if echo "$OUT" | grep -q "FOREIGN FILE(S) DETECTED in ${c2}"; then
    ok "flags the correct commit (${c2}) as containing the foreign file"
else
    bad "did not flag the foreign-file commit ${c2}; output was:\n$OUT"
fi

if echo "$OUT" | grep -q "secrets/leak.txt"; then
    ok "names the actual foreign file (secrets/leak.txt) in the output"
else
    bad "foreign file path secrets/leak.txt missing from output"
fi

if echo "$OUT" | grep -q "FOREIGN FILE(S) DETECTED in ${c1}"; then
    bad "incorrectly flagged the clean commit ${c1}"
else
    ok "clean commit ${c1} is NOT flagged"
fi

if echo "$OUT" | grep -q "FOREIGN FILE(S) DETECTED in ${c3}"; then
    bad "incorrectly flagged the clean commit ${c3}"
else
    ok "clean commit ${c3} is NOT flagged"
fi

# ── Test B: a fully in-scope range must exit 0 (non-vacuous positive) ────
echo "Test B: clean sub-range (c1 only) exits 0, no flags"
OUT_CLEAN="$(cd "$REPO" && bash "$AUDIT" "${c1}" "src" 2>&1)"
RC_CLEAN=$?
if [[ $RC_CLEAN -eq 0 ]]; then
    ok "exit code 0 for a fully in-scope range"
else
    bad "expected exit 0 for a clean range, got $RC_CLEAN"
fi
if echo "$OUT_CLEAN" | grep -q "FOREIGN FILE"; then
    bad "clean range unexpectedly reports a foreign file"
else
    ok "no FOREIGN FILE marker for the clean range"
fi

# ── Test C: usage error (missing args) exits 2 ───────────────────────────
echo "Test C: usage error"
bash "$AUDIT" >/dev/null 2>&1
RC_USAGE=$?
if [[ $RC_USAGE -eq 2 ]]; then
    ok "missing-arguments invocation exits 2"
else
    bad "expected exit 2 for missing arguments, got $RC_USAGE"
fi

echo
echo "commit_scope_audit_test.sh: ${PASS} passed, ${FAIL} failed"
[[ "$FAIL" -eq 0 ]]
