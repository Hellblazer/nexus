#!/usr/bin/env bash
# scripts/lib/bare_mvnw_lint_test.sh — repo-wide guard (bead nexus-c00dw):
# every REAL ./mvnw or `mvn ` invocation in scripts/, tests/e2e/,
# conexus/skills/ must be routed through the single-builder lease
# (scripts/lib/build-lease.sh), directly or via scripts/mvnw-leased.sh /
# scripts/build-gate-jar.sh — never a bare call that can collide with a
# concurrent build of the same service/target. Run directly with bash:
#   bash scripts/lib/bare_mvnw_lint_test.sh
#
# MECHANISM. Sweep *.sh and *.md under the three trees (a `.py` file's
# error-message string containing "mvn" is not an invocation, so `.py` is
# out of scope) for lines shaped like `./mvnw` or a word-boundary `mvn `,
# excluding plain comment lines (`^\s*#`). Every remaining candidate must
# be either:
#   1. inside a file listed in DOC_ONLY_FILES (only ever mentions the
#      strings in prose/comments/echoed help text/test-fixture content,
#      never a real shell-executed invocation), or
#   2. inside a file listed in SELF_GUARDED_FILES (acquires the lease
#      itself, at/near the top of the script, before any real work — ANY
#      ./mvnw/mvn line inside it is inherently guarded), or
#   3. matched by one entry in ALLOWLIST below: an explicit (file,
#      line-substring, reason) tuple for an invocation that lives outside
#      a self-guarded file but is nonetheless wrapped by an explicit
#      build_lease_acquire/build_lease_release pair around it (verified by
#      hand at review time — this lint checks PRESENCE of the invocation
#      line, not the wrapping itself).
# Anything else is a bare, unguarded invocation and FAILS loudly with
# file:line.
#
# NON-VACUITY (nexus-moht0 doctrine): if the sweep finds ZERO candidate
# lines at all, that is reported as a FAILURE, not a clean pass — it means
# the grep pattern itself broke (e.g. after a directory rename), not that
# the repo has no mvn/mvnw invocations left to guard.

set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"

PASS=0
FAIL=0
ok() { echo "  [ok] $1"; PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }

# Files that only ever mention ./mvnw or mvn in prose, comments, echoed
# help/log text, or test-fixture (heredoc stub) content -- never as a real
# invocation this file's own shell interpreter would execute. Excluded
# from the sweep entirely; there is nothing to allowlist line-by-line
# because nothing here is actually run.
#
# NON-VACUITY (review finding round 3): every entry here is asserted, at
# run time, to still contain at least one raw pattern hit -- see
# _assert_exemptions_live below. An entry with ZERO hits is a SILENT
# exemption doing nothing (scripts/rdr152-sandbox/up.sh and its README.md
# were exactly this after the round-2 fix eliminated their only bare
# invocations -- caught by this assertion and removed, not just by hand).
DOC_ONLY_FILES=(
    "scripts/lib/build-lease.sh"
    "scripts/lib/build-lease_test.sh"
    "scripts/mvnw-leased_test.sh"
    "conexus/skills/orchestration/SKILL.md"
    "conexus/skills/composition-probe/SKILL.md"
    "scripts/validate/integration-stack.sh"
)

# Files fully exempt: they acquire the build lease themselves (at/near the
# top of the script, before any real work), so ANY ./mvnw or mvn line
# inside them is inherently guarded, regardless of line-distance from the
# acquire call.
SELF_GUARDED_FILES=(
    "scripts/mvnw-leased.sh"
    "scripts/build-gate-jar.sh"
)

# Explicit allowlist: file|line-substring|reason. A hit is allowed when its
# file matches AND its line contains the substring.
ALLOWLIST=(
    'tests/e2e/gc-ab/run-ab.sh|-c "./mvnw|host-side build_lease_acquire/build_lease_release wraps this docker run call (build_variant())'
    'tests/e2e/migration-rehearsal/run.sh|-c "./mvnw|host-side build_lease_acquire/build_lease_release wraps this docker run call'
    'conexus/skills/cli-controller/SKILL.md|mvn clean install|generic tmux-driving example, not scoped to nexus service/ (see inline nx-mvnw-allowlist comment at the site)'
)

# _assert_exemptions_live -- non-vacuity for the exemption lists
# themselves (review finding round 3): a DOC_ONLY_FILES or
# SELF_GUARDED_FILES entry whose file no longer contains ANY raw pattern
# hit, or an ALLOWLIST entry whose substring no longer appears in its
# named file, is exempting nothing -- dead weight at best, and a silent
# false sense of coverage if a real invocation is later reintroduced under
# the same path without anyone noticing the exemption never re-engaged.
# Checked against the UNFILTERED pattern (comments included) since
# DOC_ONLY_FILES entries are legitimately expected to have comment-only
# hits.
_assert_exemptions_live() {
    local pattern='(\./mvnw\b|(^|[^w])mvn[[:space:]])'
    local f live=1
    for f in "${DOC_ONLY_FILES[@]}"; do
        if [[ ! -f "$f" ]]; then
            bad "DOC_ONLY_FILES entry '$f' does not exist -- stale exemption, remove it"
            live=0
        elif ! grep -qE "$pattern" -- "$f"; then
            bad "DOC_ONLY_FILES entry '$f' has ZERO ./mvnw or mvn mentions left -- stale exemption exempting nothing, remove it"
            live=0
        fi
    done
    for f in "${SELF_GUARDED_FILES[@]}"; do
        if [[ ! -f "$f" ]]; then
            bad "SELF_GUARDED_FILES entry '$f' does not exist -- stale exemption, remove it"
            live=0
        elif ! grep -qE "$pattern" -- "$f"; then
            bad "SELF_GUARDED_FILES entry '$f' has ZERO ./mvnw or mvn mentions left -- stale exemption exempting nothing, remove it"
            live=0
        fi
    done
    local entry ef rest esub
    for entry in "${ALLOWLIST[@]}"; do
        ef="${entry%%|*}"
        rest="${entry#*|}"
        esub="${rest%%|*}"
        if [[ ! -f "$ef" ]]; then
            bad "ALLOWLIST entry for '$ef' does not exist -- stale exemption, remove it"
            live=0
        elif ! grep -qF -- "$esub" "$ef"; then
            bad "ALLOWLIST entry for '$ef' (substring: $esub) no longer matches anything in that file -- stale exemption, remove it"
            live=0
        fi
    done
    [[ $live -eq 1 ]] && ok "every DOC_ONLY_FILES / SELF_GUARDED_FILES / ALLOWLIST exemption still has a live hit to exempt (no silent dead entries)"
}

_is_doc_only() {
    local f="$1" d
    for d in "${DOC_ONLY_FILES[@]}"; do [[ "$f" == "$d" ]] && return 0; done
    return 1
}
_is_self_guarded() {
    local f="$1" d
    for d in "${SELF_GUARDED_FILES[@]}"; do [[ "$f" == "$d" ]] && return 0; done
    return 1
}
_is_allowlisted() {
    local f="$1" line="$2" entry ef rest esub
    for entry in "${ALLOWLIST[@]}"; do
        ef="${entry%%|*}"
        rest="${entry#*|}"
        esub="${rest%%|*}"
        if [[ "$f" == "$ef" && "$line" == *"$esub"* ]]; then
            return 0
        fi
    done
    return 1
}

cd "$REPO_ROOT" || exit 1

_assert_exemptions_live

checked=0
violations=0
while IFS=: read -r file lineno content; do
    [[ -z "$file" ]] && continue
    # Plain comment line (leading whitespace then #) is never an
    # invocation -- filtered before it even becomes a candidate.
    [[ "$content" =~ ^[[:space:]]*# ]] && continue
    checked=$((checked + 1))
    _is_doc_only "$file" && continue
    _is_self_guarded "$file" && continue
    if _is_allowlisted "$file" "$content"; then
        continue
    fi
    echo "  [FAIL] unguarded invocation: $file:$lineno: $content"
    violations=$((violations + 1))
done < <(
    grep -rnE --include='*.sh' --include='*.md' \
        --exclude='bare_mvnw_lint_test.sh' \
        '(\./mvnw\b|(^|[^w])mvn[[:space:]])' \
        -- scripts tests/e2e conexus/skills 2>/dev/null
)

if [[ $checked -eq 0 ]]; then
    bad "non-vacuity: the sweep found ZERO candidate lines across scripts/, tests/e2e/, conexus/skills/ -- the grep pattern itself is broken, not evidence of a clean repo (nexus-moht0 doctrine)"
elif [[ $violations -eq 0 ]]; then
    ok "swept $checked candidate ./mvnw / mvn line(s) across scripts/, tests/e2e/, conexus/skills/ -- zero unguarded invocations"
else
    bad "$violations unguarded invocation(s) found above -- route each through scripts/mvnw-leased.sh (or scripts/build-gate-jar.sh), or add an ALLOWLIST entry in this test naming why it is safe"
fi

echo
echo "bare_mvnw_lint_test.sh: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
