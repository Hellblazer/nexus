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

# ── Test D: unicode filename in an in-scope commit is NOT a false ───────
# ── positive (RDR-184 P0 review M1: core.quotePath quoting/octal-escape) ─
# Under the default `core.quotePath=true`, `git diff-tree --name-only`
# quotes and octal-escapes non-ASCII bytes (e.g. `src/café.txt` becomes
# the literal string `"src/caf\303\251.txt"`), which no longer matches a
# plain-text allowlist pathspec via prefix/glob comparison. Reproduced
# live during review against this repo's own default git config.
echo "Test D: unicode filename does not false-positive as OUTSIDE ALLOWLIST"
printf 'bonjour\n' >"$REPO/src/café.txt"
git -C "$REPO" add src/café.txt
git -C "$REPO" commit -q -m "unicode filename, in-scope"
c4="$(git -C "$REPO" rev-parse HEAD)"

OUT_UNICODE="$(cd "$REPO" && bash "$AUDIT" "${c3}..${c4}" "src" 2>&1)"
RC_UNICODE=$?
if [[ $RC_UNICODE -eq 0 ]]; then
    ok "exit code 0 for an in-scope commit containing a unicode filename"
else
    bad "expected exit 0 for a unicode-filename in-scope commit, got $RC_UNICODE: $OUT_UNICODE"
fi
if echo "$OUT_UNICODE" | grep -q "OUTSIDE ALLOWLIST"; then
    bad "unicode filename src/café.txt was misclassified as OUTSIDE ALLOWLIST (core.quotePath regression): $OUT_UNICODE"
else
    ok "unicode filename src/café.txt is correctly recognized as in-scope (no quoting false-positive)"
fi
if echo "$OUT_UNICODE" | grep -q "café.txt"; then
    ok "the unquoted, correctly-decoded filename appears in the report"
else
    bad "expected to see the plain (unquoted) filename café.txt in the output: $OUT_UNICODE"
fi

# ── Test E: merge commits are counted and reported, never silently ──────
# ── invisible (RDR-184 P0 review M2) ─────────────────────────────────────
echo "Test E: merge commits are skipped from per-file audit but counted and reported"
git -C "$REPO" checkout -q -b side "$c1"
echo "side work" >"$REPO/src/side.txt"
git -C "$REPO" add src/side.txt
git -C "$REPO" commit -q -m "side branch commit"
side_head="$(git -C "$REPO" rev-parse HEAD)"
git -C "$REPO" checkout -q main
git -C "$REPO" merge -q --no-ff -m "merge side into main" "$side_head"
merge_c="$(git -C "$REPO" rev-parse HEAD)"

OUT_MERGE="$(cd "$REPO" && bash "$AUDIT" "${merge_c}" "src" 2>&1)"
if echo "$OUT_MERGE" | grep -q "\[merge commit -- not audited\]"; then
    ok "merge commit ${merge_c} is explicitly labeled as not-audited (not silently blank)"
else
    bad "merge commit ${merge_c} was not labeled as skipped: $OUT_MERGE"
fi
if echo "$OUT_MERGE" | grep -qE "^1 merge commit\(s\) skipped -- not audited$"; then
    ok "summary line reports exactly 1 merge commit skipped"
else
    bad "expected a '1 merge commit(s) skipped -- not audited' summary line, got: $OUT_MERGE"
fi
# The clean full-history range (Test A/B, no merges) must report 0, never
# omit the line entirely -- a reader must never have to infer "0" from
# absence.
OUT_NOMERGE="$(cd "$REPO" && bash "$AUDIT" "${c1}" "src" 2>&1)"
if echo "$OUT_NOMERGE" | grep -qE "^0 merge commit\(s\) skipped -- not audited$"; then
    ok "a merge-free range explicitly reports 0 merge commits skipped (never silently omitted)"
else
    bad "expected '0 merge commit(s) skipped -- not audited' on a merge-free range, got: $OUT_NOMERGE"
fi

# ── Test F: bash < 4 fails loud with a clear message (RDR-184 P0 review ─
# ── M3) rather than a bare "mapfile: command not found" ─────────────────
# macOS ships /bin/bash 3.2 (GPLv3-avoidance freeze) as the OS default,
# distinct from a Homebrew-installed bash 4+ that may or may not be ahead
# of it on PATH -- exercised directly here rather than assumed.
echo "Test F: bash < 4 fails loud with a clear message, not a bare syntax error"
if [[ -x /bin/bash ]] && ! /bin/bash -c '(( BASH_VERSINFO[0] >= 4 ))' 2>/dev/null; then
    OUT_OLDBASH="$(/bin/bash "$AUDIT" "${c1}" "src" 2>&1)"
    RC_OLDBASH=$?
    if [[ $RC_OLDBASH -eq 1 ]]; then
        ok "bash 3.2 invocation exits 1 (the guard's own exit code), not a bash parse crash"
    else
        bad "expected exit 1 from the bash-version guard under /bin/bash 3.2, got $RC_OLDBASH: $OUT_OLDBASH"
    fi
    if echo "$OUT_OLDBASH" | grep -q "requires bash >= 4"; then
        ok "clear 'requires bash >= 4' message shown under bash 3.2"
    else
        bad "expected a clear 'requires bash >= 4' message under bash 3.2, got: $OUT_OLDBASH"
    fi
    if echo "$OUT_OLDBASH" | grep -qi "command not found\|syntax error"; then
        bad "raw bash4+-syntax failure leaked under bash 3.2 (guard did not fire before mapfile): $OUT_OLDBASH"
    else
        ok "no raw 'command not found'/syntax-error leak under bash 3.2 -- the guard fired first"
    fi
else
    echo "  [skip] /bin/bash on this host is already bash 4+ (not the stock macOS 3.2) -- guard cannot be exercised live here"
fi

echo
echo "commit_scope_audit_test.sh: ${PASS} passed, ${FAIL} failed"
[[ "$FAIL" -eq 0 ]]
