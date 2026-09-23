#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Push the local integration branch only when every commit in the outbound
# range is one the caller vouches for (nexus-9wxu6).
#
# Why: several sessions share one primary checkout and commit as the same
# git user, so the author field cannot tell a peer's unpushed commit from
# the caller's own. On 2026-09-07 four pushes carried a peer's commits
# because the origin/develop..develop read was printed beside the push or
# skipped on a retry. This script makes the read and the push one step,
# with the vouch list as the only thing the caller supplies: the outbound
# range must equal the vouched set exactly, or nothing is pushed.
#
# Usage: git-push-develop.sh <sha> [<sha> ...]
#   Each argument is a commit the caller made (any revision that resolves
#   to a commit; short SHAs are fine). With no arguments the script only
#   reports; a non-empty range is refused, never pushed.
#
#   A vouched MERGE commit also vouches for everything it merges in (the
#   commits reachable from its second and later parents that are not yet on
#   the remote branch). That is the release back-merge and the plugin-cut
#   back-merge: the caller made the merge, so main's release-only commits
#   ride it. Commits under the merge on its FIRST-parent line (a peer's
#   unpushed work on the local branch) are never covered by it.
#
# Environment:
#   NX_PUSH_REMOTE   remote name (default origin)
#   NX_PUSH_BRANCH   branch name (default develop)
#   NX_PUSH_SOURCE   the local revision to push (default refs/heads/$branch).
#                    Set it to HEAD from a detached worktree based on
#                    $remote/$branch when the local branch is a peer's
#                    in-flight tree: the range is then $remote/$branch..HEAD
#                    and the local branch is left alone.
#   NX_PUSH_ALLOWED_PATHS
#                    REQUIRED when the range is non-empty: space-separated
#                    path prefixes / globs every outbound commit must stay
#                    inside. See "Scope audit" below.
#   NX_PUSH_SKIP_SCOPE_AUDIT
#                    Set to a REASON string to skip the scope audit. Logged
#                    in the output; there is no silent skip.
#   NX_PUSH_SKIP_LOCK
#                    Set to a REASON string to push without taking the
#                    lock/ci-develop-push tuple-space lock. Logged in the
#                    output; there is no silent skip. Same shape as
#                    NX_PUSH_SKIP_SCOPE_AUDIT above.
#
# Push lock (nexus-agctp). Rule 7 (check `gh run list` before pushing) is a
# poll: the gap between looking and pushing is where two sessions collide,
# and a careful session that keeps checking yields indefinitely to a
# careless one that does not. The tuple space's lock/<resource> template
# (RDR-211) is exactly the primitive this needs: `take.enabled`, one row
# per resource, a 900s max lease that expires on its own so a dead session
# cannot wedge the queue.
#
# Scope: claim immediately before `git push`, release immediately after,
# whether the push succeeded or failed. This is deliberately NOT held
# through the pushed sha's CI run. The lock answers WHO GOES NEXT; the
# verdict rule (rule 7) answers WHEN -- a session that wants to hold its
# place while waiting on its own CI verdict does that itself, directly
# with `nx tuple in` / `nx tuple release` on this same subspace, before
# and after its wait -- this script's job is only to serialize the push
# call itself, never a whole review-and-wait workflow. Holding the lock
# for the duration of a CI run from INSIDE this script would need a lease
# that outlives the script's own process (a background renewal loop), and
# the template's 900s lease cap is already shorter than most CI runs, so
# that scope would need machinery this push helper has no business owning.
# Claim-push-release is the simple, correct-sized answer.
#
# Failure policy: if the tuple space cannot be reached, this refuses
# rather than pushing unguarded or wedging every push -- same shape as the
# scope audit's NX_PUSH_SKIP_SCOPE_AUDIT escape above.
#
# Scope visibility (review finding 1). Mutual exclusion only holds if every
# pusher resolves the SAME tuple-space service and tenant -- two sessions
# each correctly serialized against a DIFFERENT service would never see
# each other's claim at all. So every lock outcome line (claimed, released,
# held, unreachable) names the resolved endpoint and tenant this invocation
# used, read via the SAME `nx` this script already calls (`nx config get
# service_url`/`mint_tenant`, or `nx daemon service status` for a local
# supervisor lease when neither is configured) -- never a bash-side re-parse
# of config.yml or a lease file. A miss at every step prints "(unresolvable)"
# / "(unknown)" rather than aborting: this is a diagnostic label, never a gate.
#
# Installed nx only (review finding 2). `uv run` and an activated venv both
# prepend a checkout's OWN `.venv/bin` to PATH ahead of the installed
# generation (`~/.local/bin/nx`, normally first on PATH otherwise), so a bare
# `nx` there resolves to a DEV-CHECKOUT editable install. That install trips
# the nexus-a2qhz production-write guard on every real tuple-space write --
# indistinguishable from a genuinely unreachable tuple space
# (PUSH_REFUSED_LOCK_UNREACHABLE), which teaches operators to reach for
# NX_PUSH_SKIP_LOCK for the wrong reason. So this script walks every `nx` on
# PATH (`command -v -a nx`) and uses the first one that is NOT under a
# `.venv/` directory and NOT under this checkout's own toplevel; if none
# qualifies it refuses with a distinct, accurate message
# (PUSH_REFUSED_LOCK_DEV_CHECKOUT_NX) instead of the generic unreachable one.
#
# Scope audit (nexus-bbriq). Vouching is by SHA, so it answers "did you make
# this commit" and says nothing about WHAT IS IN IT. On 2026-09-17 a peer had
# staged a 740-line docs/rdr/rdr-212-*.md draft in the shared index; an accept
# commit ran `git add <two paths>` then a BARE `git commit`, which commits the
# whole index, so the peer's draft rode 0249b0c98 through this script to
# origin/develop. Every check above passed, correctly: the sha really was the
# caller's.
#
# So before pushing, every outbound commit's file set is audited against
# NX_PUSH_ALLOWED_PATHS via tests/e2e/lib/commit_scope_audit.sh, and a file
# outside it refuses the push. The variable is REQUIRED rather than optional
# because an audit with no allowlist passes everything, and a gate that
# skip-passes when its input is absent is the vacuous-gate class this repo
# already pays for elsewhere (nexus-moht0). On a refusal with the variable
# unset, the script PRINTS the exact line to paste, computed from the range --
# so the cost is reading a file list you should have read anyway, which is the
# step that did not happen in the incident.
#
# What this does NOT catch, stated because the gap is not obvious: a commit
# that names a pathspec it was entitled to name, but whose content came from
# another session's uncommitted edit of that same file. `git commit -- <path>`
# commits the WORKING TREE version of <path>, so on a co-edited file it
# quietly carries the other session's text (measured 2026-09-18). The path is
# in the allowlist, so this audit passes it. The commit-time guard is where
# that one has to be caught.
#
# Exit codes (each failure prints a PUSH_REFUSED_* line first):
#   0  PUSH_OK n=<count> tip=<sha>, or PUSH_NOOP when nothing is outbound
#      and nothing was vouched
#   2  PUSH_REFUSED_FOREIGN     the range holds commits nobody vouched for
#   3  PUSH_REFUSED_STALE_VOUCH a vouched commit is not in the range (already
#                               pushed, rebased away, or on another branch)
#   4  PUSH_REFUSED_DIVERGED    the remote branch is not an ancestor of the
#                               local one; rebase or merge first
#   5  PUSH_REFUSED_BAD_SHA     an argument does not resolve to a commit
#   6  PUSH_REFUSED_NO_BRANCH   the local or remote-tracking branch is missing
#   7  PUSH_REFUSED_SCOPE       an outbound commit touches a file outside
#                               NX_PUSH_ALLOWED_PATHS, or that variable is
#                               unset while the range is non-empty
#   8  PUSH_REFUSED_LOCK_HELD   lock/ci-develop-push is held by another
#                               claimant; the refusal names the holder and
#                               the lease expiry
#   9  PUSH_REFUSED_LOCK_UNREACHABLE
#                               the tuple space could not be reached to
#                               claim the lock, and NX_PUSH_SKIP_LOCK is
#                               unset
#  10  PUSH_REFUSED_LOCK_DEV_CHECKOUT_NX
#                               only a dev-checkout/venv `nx` is on PATH;
#                               the installed generation was not found, and
#                               NX_PUSH_SKIP_LOCK is unset
#  11  PUSH_LOCK_RELEASE_FAILED a claim was taken (nx tuple in succeeded)
#                               but its claim id could not be parsed from
#                               the response -- the claim is left for its
#                               900s lease to self-expire, on the record

set -euo pipefail

# Claimant identity for the push lock (nexus-agctp): the active Claude
# session id, plus this host and this process's own pid, so a refusal
# names something a human can act on. This mirrors two of
# nexus.session.resolve_active_session_id's tiers (NX_SESSION_ID /
# CLAUDE_CODE_SESSION_ID, then the ~/.config/nexus/current_session flat
# file) rather than shelling out to it: this script's cwd is not always
# the nexus checkout (a detached worktree, or -- in the test suite below
# -- a throwaway fixture repo), so `uv run python -c "from nexus.session
# import ..."` would fail to resolve the package there. The two tiers
# reproduced here are the ones documented as stable in AGENTS.md; a miss
# falls back to "unknown", the same fallback resolve_active_session_id's
# own callers already substitute.
#
# NX_SESSION_ID / CLAUDE_CODE_SESSION_ID are preferred OVER the flat file
# and tried first: the file is machine-wide and last-writer-wins, so on a
# shared box it can name a DIFFERENT session than the one actually
# running this push (a second top-level Claude Code session overwrites
# it unconditionally on its own SessionStart). The two env vars are
# per-process and cannot be clobbered by a sibling session. When
# resolution still falls through to the file (or finds nothing at all),
# the claimant string says so -- "(current_session file)" or
# "(unresolved)" -- so a PUSH_REFUSED_LOCK_HELD naming this claimant
# tells its reader the identity came from the weaker source, not the
# session that actually holds the claim.
_lock_session_source="env"
_lock_session="${NX_SESSION_ID:-${CLAUDE_CODE_SESSION_ID:-}}"
if [[ -z "$_lock_session" ]]; then
  _lock_session_source="current_session file"
  _cfg_dir="${NEXUS_CONFIG_DIR:-$HOME/.config/nexus}"
  if [[ -r "$_cfg_dir/current_session" ]]; then
    _lock_session="$(cat "$_cfg_dir/current_session" 2>/dev/null || true)"
  fi
fi
if [[ -z "$_lock_session" ]]; then
  _lock_session="unknown"
  _lock_session_source="unresolved"
fi
_lock_host="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo unknown-host)"
if [[ "$_lock_session_source" == "env" ]]; then
  _lock_claimant="${_lock_session}@${_lock_host}#$$"
else
  _lock_claimant="${_lock_session}@${_lock_host}#$$ (${_lock_session_source})"
fi
_lock_subspace="lock/ci-develop-push"
_lock_resource="ci-develop-push"
# Set ONLY from this invocation's OWN successful `nx tuple in` claim (see
# the `_lock_claim_id=` assignment below, inside the lock-acquisition
# block) -- never from a peer's claim id. So the release below can only
# ever release a claim THIS process made; it cannot touch a lock another
# session holds, whether this invocation never claimed at all (stays
# empty) or was refused because a peer already held it (also stays
# empty, since the failed `in` branch never assigns it).
_lock_claim_id=""
# The installed `nx` this invocation uses for every lock call (resolved
# below, inside the lock-acquisition block, before the first `nx tuple`
# call -- see "Installed nx only" in the header). Declared here, empty,
# so the release trap below never trips `set -u` on an unset var when the
# lock was skipped entirely (NX_PUSH_SKIP_LOCK) and this never gets set.
_nx_bin=""
# "endpoint=... tenant=..." this invocation resolved the lock's tuple
# space against (see "Scope visibility" in the header) -- resolved once,
# alongside `_nx_bin`, and reused on every outcome line so two sessions
# with a still-crossed lock can be told apart by what each actually
# points at.
_lock_scope_desc=""

# Endpoint + tenant this invocation resolved (review finding 1). Reads
# ONLY through `nx` itself (never a bash-side re-parse of config.yml or a
# lease file), in the same priority nexus.db.service_endpoint.
# resolve_service_endpoint documents: an explicit NX_SERVICE_URL/HOST/PORT
# override this process already has in its own environment (no call
# needed), else the persisted config.yml `service_url` credential, else a
# local supervisor's live lease. Every read is individually guarded so a
# resolution failure degrades to "(unresolvable)"/"(unknown)" and NEVER
# aborts the script under `set -e` -- this is a diagnostic label, not a
# gate, and must never be able to orphan a lock or block a push by itself.
_lock_describe_scope() {
  local nxbin="$1" endpoint="" tenant="" cfg status host port
  if [[ -n "${NX_SERVICE_URL:-}" ]]; then
    endpoint="$NX_SERVICE_URL"
  else
    cfg="$("$nxbin" config get service_url --show 2>/dev/null || true)"
    if [[ -n "$cfg" && "$cfg" != "service_url: not set" ]]; then
      endpoint="$cfg"
    elif [[ -n "${NX_SERVICE_HOST:-}" && -n "${NX_SERVICE_PORT:-}" ]]; then
      endpoint="${NX_SERVICE_HOST}:${NX_SERVICE_PORT}"
    else
      status="$("$nxbin" daemon service status --json 2>/dev/null || true)"
      host=""
      port=""
      if [[ -n "$status" ]]; then
        host="$(printf '%s' "$status" | python3 -c 'import json,sys
d = json.load(sys.stdin)
print(d.get("host") or "")' 2>/dev/null || true)"
        port="$(printf '%s' "$status" | python3 -c 'import json,sys
d = json.load(sys.stdin)
print(d.get("port") or "")' 2>/dev/null || true)"
      fi
      if [[ -n "$host" && -n "$port" ]]; then
        endpoint="${host}:${port}"
      fi
    fi
  fi
  [[ -z "$endpoint" ]] && endpoint="(unresolvable)"

  tenant="$("$nxbin" config get mint_tenant --show 2>/dev/null || true)"
  if [[ -z "$tenant" || "$tenant" == "mint_tenant: not set" ]]; then
    tenant="(unknown)"
  fi

  printf 'endpoint=%s tenant=%s' "$endpoint" "$tenant"
}

# Released on every exit path (success, any refusal, or a signal) so a
# claim taken right before `git push` never outlives this process. A safe
# no-op before the lock is ever claimed, since _lock_claim_id starts empty
# (including when the lock was skipped, or when nx-resolution or the
# reachability probe refused before any claim was attempted).
_release_push_lock() {
  if [[ -n "$_lock_claim_id" ]]; then
    local out
    if ! out="$("$_nx_bin" tuple release "$_lock_claim_id" --claimant "$_lock_claimant" 2>&1)"; then
      echo "PUSH_LOCK_RELEASE_FAILED could not release $_lock_subspace claim $_lock_claim_id ($_lock_scope_desc): $out" >&2
      echo "Its 900s lease will expire on its own; no action needed unless a push is waiting right now." >&2
    else
      echo "PUSH_LOCK_RELEASED $_lock_subspace ($_lock_scope_desc) claimant=$_lock_claimant" >&2
    fi
  fi
}
trap _release_push_lock EXIT

remote="${NX_PUSH_REMOTE:-origin}"
branch="${NX_PUSH_BRANCH:-develop}"
source="${NX_PUSH_SOURCE:-refs/heads/$branch}"

git rev-parse --show-toplevel >/dev/null

git fetch -q "$remote"

# Resolve the tip ONCE. Everything below (ancestry, range, push) uses this
# sha, never the moving ref, so a peer commit landing on the branch between
# the range read and the push cannot ride it.
if ! tip="$(git rev-parse -q --verify "$source^{commit}")"; then
  echo "PUSH_REFUSED_NO_BRANCH source $source does not resolve to a commit"
  exit 6
fi
if ! git rev-parse -q --verify "refs/remotes/$remote/$branch^{commit}" >/dev/null; then
  echo "PUSH_REFUSED_NO_BRANCH $remote/$branch does not exist after fetch"
  exit 6
fi

if ! git merge-base --is-ancestor "refs/remotes/$remote/$branch" "$tip"; then
  echo "PUSH_REFUSED_DIVERGED $remote/$branch is not an ancestor of $source; rebase or merge first"
  exit 4
fi

range=()
while IFS= read -r sha; do
  [[ -n "$sha" ]] && range+=("$sha")
done < <(git rev-list "refs/remotes/$remote/$branch..$tip")

vouched=()
for arg in "$@"; do
  if ! full="$(git rev-parse -q --verify "$arg^{commit}")"; then
    echo "PUSH_REFUSED_BAD_SHA $arg does not resolve to a commit"
    exit 5
  fi
  vouched+=("$full")
  # A merge's non-first parents: what it merges in is covered by vouching it.
  while IFS= read -r parent; do
    [[ -z "$parent" ]] && continue
    while IFS= read -r merged; do
      [[ -n "$merged" ]] && vouched+=("$merged")
    done < <(git rev-list "$parent" "^refs/remotes/$remote/$branch")
  done < <(git rev-list --parents -n 1 "$full" | cut -d' ' -f3-  | tr ' ' '\n')
done

if [[ ${#range[@]} -eq 0 && ${#vouched[@]} -eq 0 ]]; then
  echo "PUSH_NOOP $source is already at $remote/$branch"
  exit 0
fi

contains() {
  local needle="$1"; shift
  local x
  for x in "$@"; do [[ "$x" == "$needle" ]] && return 0; done
  return 1
}

foreign=()
for sha in "${range[@]+"${range[@]}"}"; do
  contains "$sha" "${vouched[@]+"${vouched[@]}"}" || foreign+=("$sha")
done
stale=()
for sha in "${vouched[@]+"${vouched[@]}"}"; do
  contains "$sha" "${range[@]+"${range[@]}"}" || stale+=("$sha")
done

if [[ ${#foreign[@]} -gt 0 ]]; then
  echo "PUSH_REFUSED_FOREIGN ${#foreign[@]} of ${#range[@]} outbound commit(s) on $source are not vouched:"
  for sha in "${foreign[@]}"; do
    git log -1 --format='  %h %an %s' "$sha"
  done
  echo "Vouch only for commits this session made. A peer's commit is theirs to push; do not add its sha to unblock yourself."
  exit 2
fi

if [[ ${#stale[@]} -gt 0 ]]; then
  echo "PUSH_REFUSED_STALE_VOUCH ${#stale[@]} vouched commit(s) are not in $remote/$branch..$source:"
  for sha in "${stale[@]}"; do
    git log -1 --format='  %h %s' "$sha"
  done
  echo "Re-read the range: the commit was already pushed, rebased away, or is on another branch."
  exit 3
fi

# True when the allowlist matches every file, so the audit would prove
# nothing. `commit_scope_audit.sh` matches with `case "$file" in $spec)`, so
# a bare `*` allows everything -- which satisfies a REQUIRED variable while
# producing the identical vacuous pass the requirement exists to prevent, and
# unlike NX_PUSH_SKIP_SCOPE_AUDIT it leaves no trace saying so. Refusing it
# and naming the skip keeps exactly one escape, and keeps it visible.
_wildcard_allowlist() {
  # `read -ra`, NOT `for spec in $1`: an unquoted expansion of a value that
  # IS a glob expands it against the cwd, so `*` would arrive as the local
  # file list and never match the case below. That is the same defect this
  # script already fixed once for NX_PUSH_ALLOWED_PATHS, reintroduced in the
  # check written to catch its abuse (measured 2026-09-18).
  # Same newline fold as the audit call site below: `read` stops at the
  # first newline, so a multi-line allowlist whose wildcard sits on a later
  # line would slip past this check entirely. Both readers must see the same
  # value or the check guards a different string from the one that is used.
  local -a specs=()
  read -r -a specs <<< "$(printf '%s' "$1" | tr '\n' ' ')"
  local spec
  for spec in "${specs[@]+"${specs[@]}"}"; do
    case "$spec" in
    "*" | "**" | "." | "./" | "/" | "*/*") return 0 ;;
    esac
  done
  return 1
}

# Per-commit file union for the outbound range.
#
# `git diff-tree` accepts at most TWO tree-ish arguments; anything after the
# second is read as a PATH FILTER, not a further commit. Passing the whole
# range in one call was silently wrong and got worse with size: measured
# 2026-09-18 against a three-commit fixture, one sha printed its own file,
# two shas printed the DIFF BETWEEN them (dropping the first commit's file),
# and three shas printed NOTHING. This repo batches related work into one
# push by convention, so the common case was the empty one -- and the empty
# case handed the caller a pasteable `NX_PUSH_ALLOWED_PATHS=''`, hiding a
# foreign file from the exact review step this refusal exists to force.
_outbound_files() {
  local sha
  for sha in "${range[@]}"; do
    git -c core.quotePath=false diff-tree --no-commit-id --name-only -r --root "$sha"
  done | sort -u
}

# ── Scope audit (nexus-bbriq) ────────────────────────────────────────────
# Runs after the vouch checks, so its output is about commits already
# confirmed to be the caller's, and before the push, so a refusal costs
# nothing.
# Resolved from THIS SCRIPT's own location, never from the pushed repo's
# toplevel: the audit is this repo's sibling tool, and keying it on the
# working tree would look correct in the primary checkout (where they
# coincide) and break anywhere else.
_script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
audit="$_script_dir/../tests/e2e/lib/commit_scope_audit.sh"
if [[ -n "${NX_PUSH_SKIP_SCOPE_AUDIT:-}" ]]; then
  # stderr, not stdout: stdout is the machine-readable contract. Still
  # visible to a human, which is the whole point of naming the skip.
  echo "PUSH_SCOPE_AUDIT_SKIPPED reason=${NX_PUSH_SKIP_SCOPE_AUDIT}" >&2
elif [[ ! -r "$audit" ]]; then
  # Not a silent pass: the audit is part of the gate, so its absence is
  # reported and refused rather than shrugged off.
  echo "PUSH_REFUSED_SCOPE the scope audit is missing or unreadable: $audit"
  echo "Restore it, or set NX_PUSH_SKIP_SCOPE_AUDIT='<reason>' to proceed on the record."
  exit 7
elif [[ -z "${NX_PUSH_ALLOWED_PATHS:-}" ]]; then
  echo "PUSH_REFUSED_SCOPE NX_PUSH_ALLOWED_PATHS is unset and ${#range[@]} commit(s) are outbound."
  echo "Vouching is by sha: it proves you MADE the commit, not what is IN it (nexus-bbriq)."
  echo "These are the files the outbound commits touch. Read them, then re-run with:"
  echo
  _outbound_files | sed 's/^/    /'
  echo
  # The suggestion is ALWAYS the literal file list, never coarsened to
  # directory prefixes. An earlier version coarsened past 8 files "so a
  # prefix list you keep in your shell covers most pushes" -- which handed
  # the caller a ready-to-paste `docs/rdr/` allowlist that passes the very
  # foreign file this audit exists to catch, collapsing the commit-time and
  # push-time layers into one correlated failure from a single habit. A long
  # list is not a usability problem to smooth over; it is the signal that the
  # push is too broad to eyeball.
  files="$(_outbound_files)"
  suggestion="$(printf '%s\n' "$files" | tr '\n' ' ')"
  echo "    NX_PUSH_ALLOWED_PATHS='${suggestion% }' $0 $*"
  exit 7
# `read -ra` splits on IFS WITHOUT glob-expanding, which an unquoted
# ${NX_PUSH_ALLOWED_PATHS} would do: a pathspec like `docs/*` or `*` is a
# pattern the AUDIT must receive verbatim, and letting this shell expand it
# against the cwd first would silently narrow the allowlist to whatever
# happens to exist here.
#
# Newlines are folded to spaces first because `read` stops at the FIRST one.
# A caller pasting a multi-line list got an allowlist of its first entry and
# a FOREIGN FILE(S) DETECTED verdict naming the other five -- their own
# files, in their own commit. It fails closed, so nothing unsafe shipped,
# but "you are smuggling files" is a bad way to say "your variable had a
# newline in it", and it cost a peer session a cycle (nexus-01, 2026-09-19).
# Folding is not a widening: a newline is a separator here either way, and
# treating it as one can only ever ADD entries the caller wrote down.
elif _wildcard_allowlist "${NX_PUSH_ALLOWED_PATHS}"; then
  echo "PUSH_REFUSED_SCOPE NX_PUSH_ALLOWED_PATHS is '${NX_PUSH_ALLOWED_PATHS}', which matches every file."
  echo "An allowlist that audits nothing satisfies the letter of this gate and produces a clean"
  echo "PUSH_OK, indistinguishable afterwards from a genuinely scoped push. The named skip is the"
  echo "honest way to say the same thing, and it says so out loud:"
  echo
  echo "    NX_PUSH_SKIP_SCOPE_AUDIT='<reason>' $0 $*"
  exit 7
elif read -r -a _allowed_paths <<< "$(printf '%s' "${NX_PUSH_ALLOWED_PATHS}" | tr '\n' ' ')" &&
     ! _audit_out="$(bash "$audit" "refs/remotes/$remote/$branch..$tip" "${_allowed_paths[@]}" 2>&1)"; then
  # The audit's per-commit listing is captured, not streamed: this script's
  # stdout is a machine-readable contract (PUSH_OK n=<n> tip=<sha>) that
  # callers and tests parse, so a clean audit must add nothing to it.
  printf '%s\n' "$_audit_out"
  echo "PUSH_REFUSED_SCOPE an outbound commit touches a file outside NX_PUSH_ALLOWED_PATHS (see above)."
  echo "A foreign file in your commit is the nexus-bbriq class: a peer's staged work swept in by a"
  echo "whole-index commit. Do not widen the allowlist to unblock yourself — check what you committed."
  exit 7
fi

# ── Push lock (nexus-agctp) ──────────────────────────────────────────────
# Runs after every other gate, right before the push itself: a refusal
# above this point is about the commits, not about who else is pushing,
# and costs nothing extra by happening first.
if [[ -n "${NX_PUSH_SKIP_LOCK:-}" ]]; then
  echo "PUSH_LOCK_SKIPPED reason=${NX_PUSH_SKIP_LOCK}" >&2
else
  # Resolve the INSTALLED nx generation deliberately (review finding 2;
  # see "Installed nx only" in the header): the first `nx` on PATH that is
  # neither under a `.venv/` directory nor under this checkout's own
  # toplevel. Walks $PATH by hand rather than `command -v -a` -- bash's
  # `command` builtin has NO `-a` flag (that is a zsh-ism; under bash it
  # is a hard "invalid option" error, confirmed directly), so that call
  # would have found nothing on every real bash and refused every push.
  _repo_toplevel="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  _nx_path_ifs="$IFS"
  IFS=':' read -r -a _nx_path_dirs <<< "$PATH"
  IFS="$_nx_path_ifs"
  for _nx_dir in "${_nx_path_dirs[@]+"${_nx_path_dirs[@]}"}"; do
    [[ -z "$_nx_dir" ]] && continue
    _nx_candidate="$_nx_dir/nx"
    [[ -x "$_nx_candidate" ]] || continue
    _nx_cand_dir="$(cd -- "$_nx_dir" 2>/dev/null && pwd -P)" || continue
    _nx_resolved="$_nx_cand_dir/nx"
    case "$_nx_resolved" in
      */.venv/*) continue ;;
    esac
    if [[ -n "$_repo_toplevel" && "$_nx_resolved" == "$_repo_toplevel"/* ]]; then
      continue
    fi
    _nx_bin="$_nx_candidate"
    break
  done

  if [[ -z "$_nx_bin" ]]; then
    echo "PUSH_REFUSED_LOCK_DEV_CHECKOUT_NX only a dev-checkout/venv nx is on PATH; the installed generation was not found."
    echo "The nexus-a2qhz production-write guard refuses every real tuple-space write from a dev-checkout CLI, which otherwise"
    echo "reads identically to an unreachable tuple space -- refusing outright here instead so the two are never confused."
    echo "Fix PATH so the installed generation resolves first (avoid 'uv run' / an activated venv for this script), or"
    echo "reinstall it: scripts/reinstall-tool.sh."
    echo "Set NX_PUSH_SKIP_LOCK='<reason>' to push without the lock, on the record."
    exit 10
  fi

  _lock_scope_desc="$(_lock_describe_scope "$_nx_bin")"

  # `out` is idempotent (id_from: keys) and doubles as the reachability
  # probe: it always succeeds against a live tuple space, whether the
  # resource row already exists, is free, or is expired (the lock flag
  # resets an expired row to available rather than leaving it dead).
  if ! _lock_out_msg="$("$_nx_bin" tuple out "$_lock_subspace" --key "resource=$_lock_resource" 2>&1)"; then
    echo "PUSH_REFUSED_LOCK_UNREACHABLE could not reach the tuple space to claim $_lock_subspace ($_lock_scope_desc):"
    echo "$_lock_out_msg"
    echo "Set NX_PUSH_SKIP_LOCK='<reason>' to push without the lock, on the record."
    exit 9
  fi

  if _lock_in_json="$("$_nx_bin" tuple in "$_lock_subspace" --pattern "resource=$_lock_resource" \
       --claimant "$_lock_claimant" --lease-s 900 --timeout-s 0 --json 2>/dev/null)"; then
    # The claim id extraction is its OWN guarded step (ship-blocker,
    # code-review): a bare `x="$(cmd)"` assignment aborts the WHOLE
    # script under `set -e` the instant cmd's exit status is nonzero --
    # skipping past the point where `_lock_claim_id` would be set, so the
    # EXIT trap finds it empty and releases nothing, even though `nx
    # tuple in` just succeeded and a real claim now lives server-side.
    # Wrapping this in `if` is what keeps that failure from ever
    # bypassing the trap: `set -e` does not fire inside an `if` test.
    if _lock_claim_parse_out="$(printf '%s' "$_lock_in_json" | python3 -c 'import json,sys
d = json.load(sys.stdin)
cid = d.get("claim_id") or ""
if not cid:
    raise SystemExit(1)
print(cid)' 2>&1)"; then
      _lock_claim_id="$_lock_claim_parse_out"
      echo "PUSH_LOCK_CLAIMED $_lock_subspace ($_lock_scope_desc) claimant=$_lock_claimant" >&2
    else
      # A live claim already exists under $_lock_claimant -- it cannot be
      # released without its claim id, which this branch could not parse.
      # Never orphan it SILENTLY: name the claimant and subspace, and
      # exit nonzero rather than proceeding to push in an unknown state.
      # The 900s lease is the actual bound on how long this wedges the
      # queue for everyone else.
      echo "PUSH_LOCK_RELEASE_FAILED nx tuple in for $_lock_subspace ($_lock_scope_desc) as $_lock_claimant SUCCEEDED but its claim id could not be parsed:"
      echo "$_lock_claim_parse_out"
      echo "raw response: $_lock_in_json"
      echo "A live claim now exists under this claimant and cannot be released without its claim id -- it will self-expire from its 900s lease."
      exit 11
    fi
  else
    # The `out` above just proved the tuple space is reachable, so a
    # failed claim here means the row is held by someone else (or, more
    # rarely, was consumed/re-raced between the two calls) -- read it
    # without claiming to name who, and until when.
    if _lock_rows_json="$("$_nx_bin" tuple rd "$_lock_subspace" --pattern "resource=$_lock_resource" --json 2>/dev/null)"; then
      # Guarded the same way as the claim-id parse above (code-review,
      # same class): a python failure here must degrade to a clean
      # PUSH_REFUSED_LOCK_HELD, never a raw traceback that skips it.
      if ! _lock_holder="$(printf '%s' "$_lock_rows_json" | python3 -c 'import json,sys
rows = json.load(sys.stdin)
if rows:
    r = rows[0]
    print("claimant=%s lease_until=%s claim_state=%s" % (r.get("claimant"), r.get("lease_until"), r.get("claim_state")))
else:
    print("no row found -- the lock may have been released between the claim attempt and this read")' 2>&1)"; then
        _lock_holder="(a row exists but its details could not be parsed: $_lock_holder)"
      fi
    else
      _lock_holder="(could not read the lock row to name the holder -- the tuple space may have become unreachable)"
    fi
    echo "PUSH_REFUSED_LOCK_HELD $_lock_subspace ($_lock_scope_desc) is not available: $_lock_holder"
    echo "Wait for the lease to lapse or the holder to release it, then retry."
    echo "Set NX_PUSH_SKIP_LOCK='<reason>' to push without the lock, on the record."
    exit 8
  fi
fi

git push -q "$remote" "$tip:refs/heads/$branch"
echo "PUSH_OK n=${#range[@]} tip=$tip"
