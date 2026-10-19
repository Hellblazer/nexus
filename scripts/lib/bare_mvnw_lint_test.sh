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
# UNSHARE (nexus-k6zxp). A gate-jar cache hit or store leaves
# service/target's jar hard-linked to a read-only cache entry, and the jar
# plugin rewrites an existing jar IN PLACE. So every invocation that reaches the
# jar -- a package/verify/install/deploy/integration-test phase, or a *:jar /
# *:shade / *jar-plugin* / *shade-plugin* goal, on the invocation line or its
# backslash continuations -- must be preceded, earlier in the same file, by a
# non-comment gate_jar_cache_unshare call. scripts/mvnw-leased.sh (which unshares
# conditionally on the goals it is given) and a generic example outside the
# service tree are the only exceptions, listed in UNSHARE_EXEMPT_FILES.
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
    "scripts/lib/gate-jar-cache_test.sh"
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
    "tests/e2e/local-service-gate.sh"   # build_lease_acquire_wait service at line 508, before the ./mvnw at line 532 (nexus-q1upi)
)

# Explicit allowlist: file|line-substring|reason. A hit is allowed when its
# file matches AND its line contains the substring.
ALLOWLIST=(
    'tests/e2e/gc-ab/run-ab.sh|-c "./mvnw|host-side build_lease_acquire/build_lease_release wraps this docker run call (build_variant())'
    'conexus/skills/cli-controller/SKILL.md|mvn clean install|generic tmux-driving example, not scoped to nexus service/ (see inline nx-mvnw-allowlist comment at the site)'
)

# Files whose ./mvnw or mvn lines need no gate_jar_cache_unshare before them:
# the wrapper that unshares on its own (it only knows the goals at run time) and
# a generic tmux-driving example that is not about service/target. Asserted live
# like the other exemption lists.
UNSHARE_EXEMPT_FILES=(
    "scripts/mvnw-leased.sh"
    "conexus/skills/cli-controller/SKILL.md"
)

# The goals that reach the jar (see UNSHARE above).
JAR_GOAL_REGEX='(^|[[:space:]"=])(package|verify|install|deploy|pre-integration-test|integration-test|post-integration-test)([[:space:]"]|$)|:jar([[:space:]"]|$)|:shade([[:space:]"]|$)|jar-plugin|shade-plugin'

# _logical_line <file> <lineno> -- the line plus its backslash continuations.
_logical_line() {
    local file="$1" n="$2" line out=""
    while IFS= read -r line; do
        out="$out $line"
        [[ "$line" == *\\ ]] || break
    done < <(sed -n "${n},\$p" "$file")
    printf '%s' "$out"
}

# _unshare_precedes <file> <lineno> -- a non-comment gate_jar_cache_unshare call
# sits on an earlier line of <file>.
_unshare_precedes() {
    local file="$1" n="$2"
    # grep -c reads all its input (no SIGPIPE to the sed under pipefail).
    # Comments (whole-line and trailing) are stripped first: a mention in prose is
    # not a call. The call must have an argument.
    [[ "$(sed -n "1,$((n - 1))p" "$file" | grep -vE '^[[:space:]]*#' | sed -E 's/[[:space:]]#.*$//' | grep -cE '(^|[[:space:];&|(])gate_jar_cache_unshare[[:space:]]+[^[:space:]]')" -gt 0 ]]
}

_is_unshare_exempt() {
    local f="$1" d
    for d in "${UNSHARE_EXEMPT_FILES[@]}"; do [[ "$f" == "$d" ]] && return 0; done
    return 1
}

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
    for f in "${UNSHARE_EXEMPT_FILES[@]}"; do
        if [[ ! -f "$f" ]]; then
            bad "UNSHARE_EXEMPT_FILES entry '$f' does not exist -- stale exemption, remove it"
            live=0
        elif ! grep -qE "$pattern" -- "$f"; then
            bad "UNSHARE_EXEMPT_FILES entry '$f' has ZERO ./mvnw or mvn mentions left -- stale exemption, remove it"
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
jar_reaching=0
unshare_violations=0
while IFS=: read -r file lineno content; do
    [[ -z "$file" ]] && continue
    # Plain comment line (leading whitespace then #) is never an
    # invocation -- filtered before it even becomes a candidate.
    [[ "$content" =~ ^[[:space:]]*# ]] && continue
    checked=$((checked + 1))
    _is_doc_only "$file" && continue
    # nexus-k6zxp: a lease-guarded invocation that reaches the jar must also
    # have unshared it. Checked for every non-doc invocation, self-guarded and
    # allowlisted ones included, not just the unguarded ones.
    if ! _is_unshare_exempt "$file" && [[ "$(_logical_line "$file" "$lineno")" =~ $JAR_GOAL_REGEX ]]; then
        jar_reaching=$((jar_reaching + 1))
        if ! _unshare_precedes "$file" "$lineno"; then
            echo "  [FAIL] jar-reaching invocation with no gate_jar_cache_unshare before it: $file:$lineno: $content"
            unshare_violations=$((unshare_violations + 1))
        fi
    fi
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

# nexus-k6zxp: the unshare rule, with its own non-vacuity floor. The repo has at
# least these jar-reaching invocations outside the exempt files: build-gate-jar.sh,
# local-service-gate.sh and gc-ab/run-ab.sh; fewer means the goal regex or the
# sweep broke, not that the repo got safer.
if [[ $unshare_violations -gt 0 ]]; then
    bad "$unshare_violations jar-reaching invocation(s) not preceded by gate_jar_cache_unshare -- source scripts/lib/gate-jar-cache.sh and call gate_jar_cache_unshare <service/target jar> before it (a link to the read-only cache entry fails the jar plugin, or is rewritten through by root)"
elif [[ $jar_reaching -lt 3 ]]; then
    bad "non-vacuity: only $jar_reaching jar-reaching invocation(s) swept (expected >= 3: build-gate-jar.sh, local-service-gate.sh, gc-ab/run-ab.sh) -- the goal regex or the sweep is broken"
else
    ok "swept $jar_reaching jar-reaching ./mvnw / mvn invocation(s) -- every one is preceded by gate_jar_cache_unshare"
fi

echo
echo "bare_mvnw_lint_test.sh: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
