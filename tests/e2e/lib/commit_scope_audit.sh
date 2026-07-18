#!/usr/bin/env bash
# tests/e2e/lib/commit_scope_audit.sh — Gap-4 commit-scope audit helper
# (RDR-184 P0.3, nexus-ccs9v.3).
#
# Placement note: this isn't itself an e2e test, but the RDR-184 phased
# plan groups it with the Phase-0 repo-local tooling (alongside
# tests/e2e/lib/lock.sh) rather than inventing a new top-level `scripts/`
# entry for one script — it lives next to its Phase-0 sibling where anyone
# auditing that work will already be looking.
#
# Purpose: retro-checkable tripwire for the live git-index incident
# (finding 6: one agent's staged-but-unreviewed work got swept into
# another actor's whole-index commit). Given a commit range and an
# allowlist of pathspecs, lists the file set touched by every commit in
# range and flags LOUD any commit that touches a file outside the
# allowlist — the "foreign file snuck into a commit" class.
#
# I/O contract:
#   input  = a git ref-range (e.g. "HEAD~5..", "abc123..def456") OR
#            "--since=<date>" (e.g. "--since=2026-07-15"), plus one or
#            more allowed pathspecs (directory prefixes or glob patterns).
#   output = per-commit file listing on stdout, each foreign file marked
#            inline and summarized with a "!!! FOREIGN FILE(S)" line.
#   exit   = 0 if every commit stayed within the allowlist; 1 if any
#            commit touched a file outside it; 2 on usage error.
#
# Implementation note: per-commit file sets are resolved via
# `git -c core.quotePath=false diff-tree --no-commit-id --name-only -r
# --root`, which is the same underlying diff machinery `git log --stat`
# uses to compute its per-commit file list — chosen over parsing
# `--stat`'s text/number columns directly, which is fragile (rename
# arrows, truncated stat bars) for what is fundamentally a "list the
# files" query. `core.quotePath=false` is forced on the invocation
# (RDR-184 P0 review M1) rather than relying on ambient repo/global git
# config: under the default `core.quotePath=true`, `--name-only` quotes
# and octal-escapes any non-ASCII byte in a path (e.g. `src/café.txt`
# becomes the literal string `"src/caf\303\251.txt"`), which no longer
# matches a plain-text allowlist pathspec via prefix/glob comparison and
# was verified to produce a false "OUTSIDE ALLOWLIST" flag on an
# otherwise in-scope file. Quoting can only ever push a path further
# from matching (adds characters), never closer, so this was
# exclusively a false-positive risk, never a way to dodge detection —
# still worth forcing off since a tripwire that cries wolf gets ignored.
#
# Merge commits: `git diff-tree` without `-m`/`-c` produces zero files
# for a merge commit (git's default behavior for `log`/`diff-tree`
# without `-m`) — this repo's release process uses REAL merge commits
# (`gh pr merge --merge`, never `--squash`), so merges are a normal part
# of history here, not a hypothetical. Rather than silently reporting
# "0 files, nothing to flag" for a merge (indistinguishable from "this
# commit legitimately touched nothing outside the allowlist"), every run
# explicitly counts and reports how many merge commits in range were
# skipped (RDR-184 P0 review M2), so a clean report never reads as
# "exhaustively checked" when it wasn't.
set -euo pipefail

# RDR-184 P0 review M3: `mapfile` (below) is bash 4.0+ only and silently
# does not exist on stock macOS /bin/bash 3.2 (the OS-shipped default on
# this repo's own stated primary dev platform, still frozen at 3.2 for
# GPLv3-avoidance reasons) — fail loud with a clear message rather than a
# bare "mapfile: command not found" if invoked under an old bash.
if ((BASH_VERSINFO[0] < 4)); then
    echo "commit_scope_audit.sh: requires bash >= 4 (found ${BASH_VERSION}); on macOS, run via Homebrew bash (e.g. /opt/homebrew/bin/bash), not the OS-shipped /bin/bash 3.2" >&2
    exit 1
fi

usage() {
    cat >&2 <<'EOF'
Usage: commit_scope_audit.sh <range> <pathspec> [<pathspec> ...]

  <range>      A git rev-range (e.g. "HEAD~5..", "abc123..def456") OR
               "--since=<date>" (e.g. "--since=2026-07-15") to select
               commits by date instead of an explicit rev-range.
  <pathspec>   One or more allowed path prefixes / patterns. A commit
               is IN SCOPE only if every file it touches matches at
               least one pathspec (prefix match on a directory, exact
               match, or a shell glob pattern).

Exit status: 0 if every commit in range stayed within the allowlist;
             1 if any commit touched a file outside it (flagged inline);
             2 on a usage error (bad arguments, not a git work tree).
EOF
}

if [[ $# -lt 2 ]]; then
    usage
    exit 2
fi

RANGE="$1"
shift
PATHSPECS=("$@")

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "commit_scope_audit: not inside a git work tree" >&2
    exit 2
fi

_matches_allowlist() {
    local file="$1" spec norm
    for spec in "${PATHSPECS[@]}"; do
        norm="${spec%/}"
        if [[ "$file" == "$spec" || "$file" == "$norm"/* ]]; then
            return 0
        fi
        # shellcheck disable=SC2254 # deliberately unquoted: spec is a glob pattern here
        case "$file" in
        $spec)
            return 0
            ;;
        esac
    done
    return 1
}

declare -a commits=()
if [[ "$RANGE" == --since=* ]]; then
    while IFS= read -r c; do
        commits+=("$c")
    done < <(git log --since="${RANGE#--since=}" --reverse --format='%H')
else
    while IFS= read -r c; do
        commits+=("$c")
    done < <(git log --reverse --format='%H' "$RANGE")
fi

if [[ ${#commits[@]} -eq 0 ]]; then
    echo "commit_scope_audit: no commits in range '$RANGE'" >&2
    exit 0
fi

exit_code=0
merge_skipped=0
for c in "${commits[@]}"; do
    subject="$(git log -1 --format='%s' "$c")"

    read -r -a parent_words <<<"$(git log -1 --format='%P' "$c")"
    if [[ ${#parent_words[@]} -gt 1 ]]; then
        echo "commit ${c}  ${subject}  [merge commit -- not audited]"
        merge_skipped=$((merge_skipped + 1))
        echo
        continue
    fi

    echo "commit ${c}  ${subject}"

    mapfile -t files < <(git -c core.quotePath=false diff-tree --no-commit-id --name-only -r --root "$c")
    foreign=()
    for f in "${files[@]}"; do
        [[ -z "$f" ]] && continue
        if _matches_allowlist "$f"; then
            echo "    $f"
        else
            echo "    $f   <-- OUTSIDE ALLOWLIST"
            foreign+=("$f")
        fi
    done

    if [[ ${#foreign[@]} -gt 0 ]]; then
        echo "  !!! FOREIGN FILE(S) DETECTED in ${c}: ${foreign[*]}"
        exit_code=1
    fi
    echo
done

echo "${merge_skipped} merge commit(s) skipped -- not audited"

exit "$exit_code"
