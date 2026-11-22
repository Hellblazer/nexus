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
# Usage: git-push-develop.sh [flags] <sha> [<sha> ...]
#   Each argument is a commit the caller made (any revision that resolves
#   to a commit; short SHAs are fine). With no arguments the script only
#   reports; a non-empty range is refused, never pushed.
#
#   The script is callable with NO environment-variable prefix: every
#   setting below has a command-line flag, so a push is one command that
#   starts with the script path (a permission rule that matches on the
#   leading command is defeated by `FOO=bar cmd`). A typical batch push
#   from a detached worktree:
#
#     scripts/git-push-develop.sh --source HEAD --allowed-paths-from-range <sha> [<sha> ...]
#       -> prints the files your commits touch and a 12-hex digest, exits 7
#     scripts/git-push-develop.sh --source HEAD --allowed-paths-from-range=<digest> <sha> [<sha> ...]
#       -> pushes, only if the digest still matches the outbound range
#
#   Flags (each also takes the `--flag=value` form; a value may not begin
#   with `--`; a literal `--` ends flag parsing, so everything after it is
#   a sha). A flag WINS over its environment twin when both are set; the
#   environment forms keep working unchanged.
#     --source <ref>              NX_PUSH_SOURCE      (HEAD for a detached worktree)
#     --remote <name>             NX_PUSH_REMOTE
#     --branch <name>             NX_PUSH_BRANCH
#     --allowed-paths <paths>     NX_PUSH_ALLOWED_PATHS. ONE value: a single path
#                                 or a space-separated list, quoted. Repeatable;
#                                 the values accumulate. It takes one value, not
#                                 "everything up to the next flag", because a
#                                 path and a sha are indistinguishable by shape.
#     --allowed-paths-from <file> The same list read from a file (whitespace and
#                                 newlines separate entries). Repeatable, and
#                                 accumulates with --allowed-paths.
#     --allowed-paths-from-range[=<digest>]
#                                 A two-step confirm. The allowlist is the union of every
#                                 path touched by every outbound commit (per commit, the same
#                                 `git diff-tree -r --root` listing the scope audit uses, so a
#                                 rename lists both paths). Step 1, no digest: nothing is
#                                 pushed; the list and a digest of it (first 12 hex of the
#                                 sha256 of the sorted, newline-joined list) are printed
#                                 with the pasteable step-2 command, exit 7. Step 2,
#                                 `=<digest>`: pushes only when the digest matches the
#                                 current outbound range, else refuses and reprints, so a
#                                 file added to a commit after you read the list changes the
#                                 digest and stops the push. The audit cannot catch a foreign
#                                 file swept into your commit (nexus-bbriq) because the list
#                                 is derived from that commit; the check is YOUR READ of the
#                                 printed list. A confirmed push says so on stdout
#                                 (PUSH_SCOPE_CONFIRMED_RANGE). Use --allowed-paths when you
#                                 can name the files. Cannot be combined with
#                                 --allowed-paths[-from].
#     --skip-scope-audit <reason> NX_PUSH_SKIP_SCOPE_AUDIT
#     --skip-lock <reason>        NX_PUSH_SKIP_LOCK
#     --batch-minutes <n>         NX_PUSH_BATCH_MINUTES
#     --tenant-check <0|1>        NX_PUSH_TENANT_CHECK (0 skips the tenant hint)
#     --skip-scrub <reason>       NX_PUSH_SKIP_SCRUB (push past the scrub gate, on the record)
#     --red-fix <reason>          NX_PUSH_RED_FIX (push to develop while a red it did not cause is open and you are
#                                 neither its owner nor a suspect's pusher; see "Hold your pushes" below)
#     --proof <file>              A scripts/watched_pytest.py --status file proving a heavy check leg green on this tip's
#                                 tree (see "Check gate" below). Repeatable; each is checked, none is a skip.
#     --skip-<check> <reason>     Override ONE check of the check gate, on the record. <check> is file-size, changelog,
#                                 context-budget, wire-ledger, cloud-leg, plugin-ledger, docs-readers, write-seam, or
#                                 check-gate (the whole gate, for a gate that cannot start). Flags only, with no
#                                 environment twin: an exported variable from an earlier session overrides nothing.
#   A bad flag exits 64 with PUSH_REFUSED_USAGE before anything is fetched.
#
#   A vouched MERGE commit also vouches for everything it merges in (the
#   commits reachable from its second and later parents that are not yet on
#   the remote branch). That is the release back-merge: the caller made
#   the merge, so main's release-only commits ride it. Commits under the merge on its FIRST-parent line (a peer's
#   unpushed work on the local branch) are never covered by it.
#
# Environment (each has the flag twin above; the flag wins when both are set):
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
#   NX_PUSH_BATCH_MINUTES
#                    Window for the batching hint (default 30; 0 turns it off).
#                    nexus-cvtxu: develop CI runs one run at a time and a newer
#                    push replaces the pending one, so pushes queue behind each
#                    other (2026-10-21: 62 pushes, push-to-verdict p50 25 / p90
#                    41 min with runs ~15). After a push, when this worktree's
#                    previous push was inside the window, a PUSH_BATCH_HINT line
#                    on stderr asks the session to batch (Sam, 2026-10-21). A
#                    hint, never a refusal; stdout keeps its PUSH_OK contract.
#
# After PUSH_OK, two PUSH_WAIT lines on stderr name the ways to be woken when the
# push's CI completes (nexus-1d6pz.11.4): the tuple_subscribe call for a session
# with the channel, and `nx ci wait <tip>` to run as a background command for a
# session without it. The channel line names the tip because the board's notice
# for a --done row carries the sha it reports in its meta (nexus-1d6pz.11.3). A
# notice for another sha is not to be ignored on that alone: in a push window the
# beads close on the window tip's result, which is often a peer's sha.
#
# Push record (RDR-252 Step 2, nexus-igea9.2). After PUSH_OK on a push to develop,
# on every path including --skip-lock, the script writes one tuple to
# board/ci/nexus-develop--push (key topic=nexus-develop--push, nonce push-<tip>):
# dims from=<identity id, or "unknown"> kind=push sha=<tip>, body {"base": <the
# remote develop tip the push replaced>, "n": <commits>}. RDR-252's owner stamper
# links base to tip to order develop's tips and maps the record's session stamp to
# the pushing identity; it never reads from. The stamp is added by `nx tuple out`
# once RDR-253 Step 3's client resolver is installed; until then the record is
# unstamped and serves the chain only. The push lock is released before the record
# is written. With --skip-lock the installed nx is resolved for this write as the
# lock path does; if none resolves, no record is written (PUSH_RECORD_SKIPPED).
# The write is `nx ci record-push --base nexus-<branch> ...` (nexus-igea9 audit S3): nx
# holds the logic, so another repo's push script can call it. from is the pusher's
# identity id, else "unknown"; a failed stamped write is retried once as a minimal
# unstamped record (base, tip, from) so the chain keeps its link. Its one status line,
# when not PUSH_RECORD_OK, goes in the push summary: PUSH_RECORD_FALLBACK or
# PUSH_RECORD_FAILED (also an nx without the command, a crash, or a write past
# NX_PUSH_RECORD_TIMEOUT_S, default 20 s), and PUSH_RECORD_SKIPPED, each on stdout after
# PUSH_OK and on stderr. The record never fails the push: the exit code is the push's.
# A push that overrode the develop-red hold with --red-fix has its reason in the record
# body as "red_fix", so the board shows every escape. A push to any other branch writes
# no record.
#
# Hold your pushes (RDR-252, nexus-igea9). A push to develop while a red is open on
# board/ci/nexus-develop--owner is refused (PUSH_REFUSED_DEVELOP_RED, exit 13) unless
# the pusher owns the red or pushed one of its suspects (`nx ci check-hold` decides);
# --red-fix '<reason>' pushes past it, on the record (PUSH_RED_FIX on stdout and the
# record body). A failed read of the red is one PUSH_GATE_UNCHECKED line on stderr and
# the push goes on. The check runs after the scope audit and the scrub gate, before
# the lock.
#
# Check gate (nexus-igea9, Sam 2026-11-22). Five develop reds in one day came from a check that was not run, or was run on
# the working tree instead of the pushed tree. A push to develop now runs the checks its diff reaches against the TIP's
# tree (a detached worktree of the tip, removed on every exit path; scripts/push_check_gate.py holds the logic, and its
# docstring the table): always scripts/file_size_ratchet.py check and scripts/changelog_fold.py check; the wire-pairing check
# when the diff touches what that script treats as wire (or the ledger); tests/scripts/test_cloud_leg.py for docs/; the plugin
# release-drift ledger test for conexus/, sn/, tool-spill/, prose-edit/ and marketplace.json. Two heavy legs are not run here
# but need a PROOF (--proof <file>: the status file of scripts/watched_pytest.py, stamped with the tree it ran on): the
# docs-readers selection when docs/ changes, the write-seam behavioural gate when ci.yml's write-seam path filter matches. A red
# or a missing proof refuses the push (PUSH_REFUSED_<CHECK>, exit 14) with the command to reproduce it; --skip-<check> '<reason>'
# overrides one, and the reason goes on stdout (PUSH_CHECK_SKIPPED) and into the push record body. A gate that cannot start is
# PUSH_REFUSED_CHECK_GATE_ERROR (exit 15), never a silent pass. The gate is armed by the REMOTE branch (it carries
# scripts/file_size_ratchet.py), so a push cannot disarm it by deleting the script. It runs after the hold check and BEFORE
# the lock, so a slow check never holds the lock. Develop only.
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
#  12  PUSH_REFUSED_SCRUB       the tip's tree or an outbound commit message
#                               carries a personal home path, tailnet hostname,
#                               chat link or session trailer, or the allowlist
#                               widens the remote's (scripts/scrub_check.py
#                               check --rev <tip> --base <remote branch>)
#  13  PUSH_REFUSED_DEVELOP_RED develop is red and this push is from neither the
#                               red's owner nor the pusher of one of its suspects
#                               (--red-fix '<reason>' pushes past it, on the record)
#  14  PUSH_REFUSED_<CHECK>    a check the outbound diff selects is red against the tip's tree, or a heavy leg has no
#                               proof (PUSH_REFUSED_FILE_SIZE, _CHANGELOG, PUSH_REFUSED_CONTEXT_BUDGET, _WIRE_LEDGER,
#                               _CLOUD_LEG, _PLUGIN_LEDGER, _DOCS_READERS, _WRITE_SEAM), each with the command to reproduce it
#  15  PUSH_REFUSED_CHECK_GATE_ERROR
#                               the check gate could not run (no usable Python, a temp directory it could not make, a
#                               worktree it could not add); --skip-check-gate '<reason>' pushes past it, on the record
#  64  PUSH_REFUSED_USAGE      a flag is unknown, lacks its value, or flags that
#                               cannot combine were combined; nothing was fetched
#  11  PUSH_LOCK_RELEASE_FAILED a claim was taken (nx tuple in succeeded)
#                               but its claim id could not be parsed from
#                               the response -- the claim is left for its
#                               900s lease to self-expire, on the record

set -euo pipefail

_script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# `installed_nx_resolve` / `installed_nx_describe_scope`: the installed-nx walk
# and the endpoint label.
# shellcheck source=./lib/installed-nx.sh
source "$_script_dir/lib/installed-nx.sh"

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

# Endpoint + tenant this invocation resolved (review finding 1): see
# installed_nx_describe_scope in scripts/lib/installed-nx.sh. A diagnostic
# label, never a gate.
_lock_describe_scope() {
  installed_nx_describe_scope "$@"
}

# Released on every exit path (success, any refusal, or a signal) so a
# claim taken right before `git push` never outlives this process. A safe
# no-op before the lock is ever claimed, since _lock_claim_id starts empty
# (including when the lock was skipped, or when nx-resolution or the
# reachability probe refused before any claim was attempted).
_record_tmp=""   # the push record's temp file while its write runs; the EXIT trap removes it if the script dies mid-write
_check_workdir=""   # the check gate's temp directory (the tip's tree lives under it); the EXIT trap removes it and its worktree entry
_check_cleanup() {
  [[ -n "$_check_workdir" && -d "$_check_workdir" ]] || return 0
  local tree gitdir
  for tree in "$_check_workdir"/pc-*/tree; do
    [[ -f "$tree/.git" ]] || continue
    gitdir="$(sed -n 's/^gitdir: //p' "$tree/.git" 2>/dev/null || true)"
    # The worktree entry this gate made, and only that one: `git worktree prune` would also drop a peer's missing one.
    if [[ -n "$gitdir" && "$gitdir" == */worktrees/* ]]; then rm -rf "$gitdir"; fi
  done
  rm -rf "$_check_workdir"
  _check_workdir=""
}
_release_push_lock() {
  [[ -n "$_record_tmp" ]] && rm -f "$_record_tmp"
  _check_cleanup
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

# ── Command-line flags ───────────────────────────────────────────────────
# Each flag assigns the NX_PUSH_* variable it is the twin of, so everything
# below reads one variable and a flag wins over the environment by plain
# assignment. Parsed before the first remote call: a typo costs nothing.
_usage_refuse() {
  echo "PUSH_REFUSED_USAGE $*"
  echo "Flags: --source --remote --branch --allowed-paths --allowed-paths-from --allowed-paths-from-range[=<digest>] --skip-scope-audit --skip-lock --batch-minutes --tenant-check --skip-scrub --red-fix --proof --skip-<check> (see the header of $0)."
  exit 64
}
_vouch_args=()
_echo_flags=()          # flags worth repeating in a pasteable suggestion
_range_allowlist=0
_range_digest=""
_flag_allowed=""
_flag_allowed_given=0
_check_proofs=()        # --proof files, in order
# The check skips are set by their flags and by nothing else (no NX_PUSH_SKIP_<CHECK> twin: Sam's ruling asked for flags), so a
# same-named variable inherited from the environment is dropped before the flags are read.
for _skip_key in FILE_SIZE CHANGELOG CONTEXT_BUDGET WIRE_LEDGER CLOUD_LEG PLUGIN_LEDGER DOCS_READERS WRITE_SEAM CHECK_GATE; do
  unset "_CHECK_SKIP_${_skip_key}"
done
while [[ $# -gt 0 ]]; do
  _arg="$1"
  case "$_arg" in
  --)
    shift
    _vouch_args+=("$@")
    break
    ;;
  --?*)
    _name="$_arg"
    _val=""
    _has_val=0
    if [[ "$_arg" == --*=* ]]; then
      _name="${_arg%%=*}"
      _val="${_arg#*=}"
      _has_val=1
    fi
    case "$_name" in
    --allowed-paths-from-range)
      if (( _has_val )); then
        [[ "$_val" =~ ^[0-9a-f]{12}$ ]] || _usage_refuse "$_name=<digest> takes the 12 hex digits the bare form printed, got '$_val'"
        _range_digest="$_val"
      fi
      _range_allowlist=1
      shift
      continue
      ;;
    --source | --remote | --branch | --allowed-paths | --allowed-paths-from \
      | --skip-scope-audit | --skip-lock | --batch-minutes | --tenant-check | --skip-scrub | --red-fix \
      | --proof | --skip-file-size | --skip-changelog | --skip-context-budget | --skip-wire-ledger | --skip-cloud-leg \
      | --skip-plugin-ledger | --skip-docs-readers | --skip-write-seam | --skip-check-gate) ;;
    *) _usage_refuse "unknown flag $_name" ;;
    esac
    if (( _has_val )); then
      shift
    else
      [[ $# -ge 2 ]] || _usage_refuse "$_name needs a value"
      _val="$2"
      [[ "$_val" == --* ]] && _usage_refuse "$_name needs a value, got '$_val'"
      shift 2
    fi
    [[ -n "$_val" ]] || _usage_refuse "$_name needs a non-empty value"
    case "$_name" in
    --source) NX_PUSH_SOURCE="$_val"; _echo_flags+=("$_name" "$_val") ;;
    --remote) NX_PUSH_REMOTE="$_val"; _echo_flags+=("$_name" "$_val") ;;
    --branch) NX_PUSH_BRANCH="$_val"; _echo_flags+=("$_name" "$_val") ;;
    --skip-lock) NX_PUSH_SKIP_LOCK="$_val"; _echo_flags+=("$_name" "$_val") ;;
    --skip-scope-audit) NX_PUSH_SKIP_SCOPE_AUDIT="$_val" ;;
    --skip-scrub) NX_PUSH_SKIP_SCRUB="$_val"; _echo_flags+=("$_name" "$_val") ;;
    --red-fix) NX_PUSH_RED_FIX="$_val"; _echo_flags+=("$_name" "$_val") ;;
    --proof)
      [[ -r "$_val" && -f "$_val" ]] || _usage_refuse "--proof: cannot read file '$_val'"
      _check_proofs+=("$_val")
      _echo_flags+=("$_name" "$_val")
      ;;
    --skip-file-size | --skip-changelog | --skip-context-budget | --skip-wire-ledger | --skip-cloud-leg \
      | --skip-plugin-ledger | --skip-docs-readers | --skip-write-seam | --skip-check-gate)
      _skip_key="${_name#--skip-}"
      _skip_key="$(printf '%s' "${_skip_key//-/_}" | tr '[:lower:]' '[:upper:]')"
      printf -v "_CHECK_SKIP_${_skip_key}" '%s' "$_val"
      ;;
    --batch-minutes) NX_PUSH_BATCH_MINUTES="$_val" ;;
    --tenant-check)
      [[ "$_val" == "0" || "$_val" == "1" ]] || _usage_refuse "--tenant-check takes 0 or 1, got '$_val'"
      NX_PUSH_TENANT_CHECK="$_val"
      ;;
    --allowed-paths)
      _flag_allowed+="${_flag_allowed:+ }$_val"
      _flag_allowed_given=1
      ;;
    --allowed-paths-from)
      [[ -r "$_val" && -f "$_val" ]] || _usage_refuse "--allowed-paths-from: cannot read file '$_val'"
      _from_text="$(tr '\r\n' '  ' < "$_val")"
      [[ -n "${_from_text//[[:space:]]/}" ]] || _usage_refuse "--allowed-paths-from: '$_val' lists no paths"
      _flag_allowed+="${_flag_allowed:+ }$_from_text"
      _flag_allowed_given=1
      ;;
    esac
    ;;
  *)
    _vouch_args+=("$_arg")
    shift
    ;;
  esac
done
if (( _range_allowlist && _flag_allowed_given )); then
  _usage_refuse "--allowed-paths-from-range cannot be combined with --allowed-paths or --allowed-paths-from"
fi
if (( _flag_allowed_given || _range_allowlist )) && [[ -n "${NX_PUSH_ALLOWED_PATHS:-}" ]]; then
  echo "PUSH_NOTE the allowlist flag wins: the NX_PUSH_ALLOWED_PATHS environment value is ignored." >&2
fi
(( _flag_allowed_given )) && NX_PUSH_ALLOWED_PATHS="$_flag_allowed"
set -- "${_vouch_args[@]+"${_vouch_args[@]}"}"
# The flags to repeat in a pasteable line: only the ones that change which
# push this is, never an allowlist or a skip reason.
_echo_flag_text=""
if [[ ${#_echo_flags[@]} -gt 0 ]]; then
  _echo_flag_text="$(printf '%q ' "${_echo_flags[@]}")"
fi

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
  done | LC_ALL=C sort -u
}

# First 12 hex of the sha256 of stdin. shasum on macOS, sha256sum elsewhere.
_digest12() {
  local out
  if command -v sha256sum >/dev/null 2>&1; then
    out="$(sha256sum)"
  else
    out="$(shasum -a 256)"
  fi
  printf '%s' "${out:0:12}"
}

# ── Scope audit (nexus-bbriq) ────────────────────────────────────────────
# Runs after the vouch checks, so its output is about commits already
# confirmed to be the caller's, and before the push, so a refusal costs
# nothing.
# Resolved from THIS SCRIPT's own location, never from the pushed repo's
# toplevel: the audit is this repo's sibling tool, and keying it on the
# working tree would look correct in the primary checkout (where they
# coincide) and break anywhere else.
audit="$_script_dir/../tests/e2e/lib/commit_scope_audit.sh"

# --allowed-paths-from-range: the two-step confirm. The list comes from the
# same per-commit diff-tree call the audit uses (a rename lists BOTH paths; a
# `git log --name-only` list collapses it to the new one and the audit then
# refuses the old). The digest binds the confirming call to the list the
# caller was shown: a file added to a commit between the two steps changes it.
# The array is filled here, not word-split from a string, so a path with a
# space in it reaches the audit whole. Not computed when the audit is skipped.
_allowed_paths=()
_vouch_text="$*"   # a function's own $* is its arguments, not the script's
_range_show() {
  {
    echo "PUSH_REFUSED_SCOPE $1"
    echo "n=${#_allowed_paths[@]} files touched by $remote/$branch..$source (digest $_range_have):"
    printf '    %s\n' "${_allowed_paths[@]}"
    _merges="$(git rev-list --merges --count "$remote/$branch..$source" 2>/dev/null || echo 0)"
    if [[ "$_merges" != "0" ]]; then
      echo
      echo "WARNING: $_merges merge commit(s) in the range: the list above does not show what a merge"
      echo "itself changes (a conflict resolution or a file only the merge adds). Read those with"
      echo "    git show --first-parent -m --stat <merge-sha>"
    fi
    echo
    echo "Read the list. If it is exactly what you meant to push, confirm it:"
    echo
    echo "    $0 ${_echo_flag_text}--allowed-paths-from-range=$_range_have $_vouch_text"
    echo
    echo "The audit cannot catch a file that is in your commit by mistake (nexus-bbriq): the list"
    echo "is derived from your commits, so your read of it is the check."
  }
  exit 7
}
if (( _range_allowlist )) && [[ -z "${NX_PUSH_SKIP_SCOPE_AUDIT:-}" ]]; then
  while IFS= read -r _p; do
    [[ -n "$_p" ]] && _allowed_paths+=("$_p")
  done < <(_outbound_files)
  if [[ ${#_allowed_paths[@]} -eq 0 ]]; then
    echo "PUSH_REFUSED_SCOPE --allowed-paths-from-range found no files in $remote/$branch..$source (merge commits only?); there is nothing to allowlist."
    echo "Name the files with --allowed-paths, or skip on the record with --skip-scope-audit '<reason>'."
    exit 7
  fi
  _range_have="$(printf '%s\n' "${_allowed_paths[@]}" | _digest12)"
  if [[ -z "$_range_digest" ]]; then
    _range_show "--allowed-paths-from-range prints the list first; nothing was pushed."
  elif [[ "$_range_digest" != "$_range_have" ]]; then
    _range_show "the digest $_range_digest does not match the outbound range (a commit changed, or the range moved, since you read the list); nothing was pushed."
  fi
  echo "PUSH_SCOPE_CONFIRMED_RANGE n=${#_allowed_paths[@]} digest=$_range_have (the allowlist was the printed range list, confirmed by digest; not hand-scoped)"
fi

if [[ -n "${NX_PUSH_SKIP_SCOPE_AUDIT:-}" ]]; then
  # stderr, not stdout: stdout is the machine-readable contract. Still
  # visible to a human, which is the whole point of naming the skip.
  echo "PUSH_SCOPE_AUDIT_SKIPPED reason=${NX_PUSH_SKIP_SCOPE_AUDIT}" >&2
elif [[ ! -r "$audit" ]]; then
  # Not a silent pass: the audit is part of the gate, so its absence is
  # reported and refused rather than shrugged off.
  echo "PUSH_REFUSED_SCOPE the scope audit is missing or unreadable: $audit"
  echo "Restore it, or pass --skip-scope-audit '<reason>' (env NX_PUSH_SKIP_SCOPE_AUDIT) to proceed on the record."
  exit 7
elif (( ! _range_allowlist )) && [[ -z "${NX_PUSH_ALLOWED_PATHS:-}" ]]; then
  echo "PUSH_REFUSED_SCOPE no allowlist (--allowed-paths, NX_PUSH_ALLOWED_PATHS) and ${#range[@]} commit(s) are outbound."
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
  echo "    $0 ${_echo_flag_text}--allowed-paths '${suggestion% }' $*"
  echo
  echo "Or take the list as is: this prints it with a digest, and you confirm with that digest:"
  echo
  echo "    $0 ${_echo_flag_text}--allowed-paths-from-range $*"
  exit 7
# The env allowlist is split with `read -ra`, WITHOUT glob-expanding, which an
# unquoted ${NX_PUSH_ALLOWED_PATHS} would do: a pathspec like `docs/*` or `*` is a
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
elif (( ! _range_allowlist )) && _wildcard_allowlist "${NX_PUSH_ALLOWED_PATHS}"; then
  echo "PUSH_REFUSED_SCOPE the allowlist '${NX_PUSH_ALLOWED_PATHS}' matches every file."
  echo "An allowlist that audits nothing satisfies the letter of this gate and produces a clean"
  echo "PUSH_OK, indistinguishable afterwards from a genuinely scoped push. The named skip is the"
  echo "honest way to say the same thing, and it says so out loud:"
  echo
  echo "    $0 ${_echo_flag_text}--skip-scope-audit '<reason>' $*"
  exit 7
elif { (( _range_allowlist )) || read -r -a _allowed_paths <<< "$(printf '%s' "${NX_PUSH_ALLOWED_PATHS}" | tr '\n' ' ')"; } &&
     ! _audit_out="$(bash "$audit" "refs/remotes/$remote/$branch..$tip" "${_allowed_paths[@]}" 2>&1)"; then
  # The audit's per-commit listing is captured, not streamed: this script's
  # stdout is a machine-readable contract (PUSH_OK n=<n> tip=<sha>) that
  # callers and tests parse, so a clean audit must add nothing to it.
  printf '%s\n' "$_audit_out"
  echo "PUSH_REFUSED_SCOPE an outbound commit touches a file outside the allowlist (see above)."
  echo "A foreign file in your commit is the nexus-bbriq class: a peer's staged work swept in by a"
  echo "whole-index commit. Do not widen the allowlist to unblock yourself — check what you committed."
  exit 7
fi

# ── Scrub gate (nexus-3ceda.7) ───────────────────────────────────────────
# Sam ruled the repo stays scrubbed (ask nexus-scrubbed, 2026-11-09): no
# personal home path, tailnet hostname, chat link or session trailer in a
# committed file beyond what tests/fixtures/scrub-allowlist.txt grandfathers.
# The check reads the TIP's files and allowlist (--rev), not the working
# tree, so a dirty checkout neither hides nor invents a hit; with --base it
# also scans the outbound commit messages and refuses an allowlist that
# widens the remote branch's. CI's lint bucket and docs lane run the file
# check; this catches it before the push, and sub-second, so it runs before
# the tenant hint. A checkout without the script (an older branch) or a host
# without python3 skips it with a PUSH_SCRUB_SKIPPED line on stderr.
# --skip-scrub '<reason>' (env NX_PUSH_SKIP_SCRUB) pushes past it, on the record.
_scrub_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -n "${NX_PUSH_SKIP_SCRUB:-}" ]]; then
  echo "PUSH_SCRUB_SKIPPED reason=${NX_PUSH_SKIP_SCRUB}" >&2
elif [[ -z "$_scrub_root" || ! -f "$_scrub_root/scripts/scrub_check.py" ]]; then
  echo "PUSH_SCRUB_SKIPPED reason=no scripts/scrub_check.py in this checkout" >&2
elif ! command -v python3 >/dev/null 2>&1; then
  echo "PUSH_SCRUB_SKIPPED reason=no python3 on PATH" >&2
elif ! _scrub_out="$(python3 "$_scrub_root/scripts/scrub_check.py" --root "$_scrub_root" check \
      --rev "$tip" --base "refs/remotes/$remote/$branch" 2>&1)"; then
  printf '%s\n' "$_scrub_out"
  echo "PUSH_REFUSED_SCRUB the tip $tip's tree, its outbound commit messages or its allowlist carry a personal path,"
  echo "hostname, chat link or session trailer (see above). Use a placeholder (/Users/you/..., ~, <host>); a"
  echo "legitimate one: python3 scripts/scrub_check.py update-exceptions --allow-raise, then push with"
  echo "--skip-scrub '<reason>'."
  exit 12
fi

# ── Tenant ceiling hint (nexus-g37hn) ───────────────────────────────────
# Develop went red twice on 2026-10-21/22 on pytest-gate's storage-touch fan-in
# because a pushed test minted a tenant over tests/storage_touch_ceiling.toml
# (tolerance 0). When the pushed range changes a test module and the tip is the
# checkout's HEAD, scripts/tenant_delta.py counts the tests that mint a tenant
# at the tip and at the remote branch and prints the difference as
# PUSH_TENANT_HINT lines on stderr. A hint, never a refusal: the count has a 30 s
# budget, and a count it could not take (over budget, or a module the merge base
# cannot collect) prints a "WARNING:" line and the push goes on.
# NX_PUSH_TENANT_CHECK=0 skips it.
_tenant_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
# The changed names are captured first, not piped to `grep -q`: under pipefail a
# grep that exits on its first match SIGPIPEs git diff and reads as no match.
_tenant_changed="$(git diff --name-only "refs/remotes/$remote/$branch" "$tip" -- tests 2>/dev/null || true)"
if [[ "${NX_PUSH_TENANT_CHECK:-1}" != "0" && -n "$_tenant_root" && -f "$_tenant_root/scripts/tenant_delta.py" ]] \
   && [[ "$tip" == "$(git rev-parse -q --verify HEAD 2>/dev/null || true)" ]] \
   && [[ "$_tenant_changed" == *.py* ]] \
   && command -v uv >/dev/null 2>&1; then
  _tenant_out="$(cd "$_tenant_root" && uv run --no-sync --quiet python scripts/tenant_delta.py \
        --base "refs/remotes/$remote/$branch" --quiet-if-zero --budget-s 30 2>&1 || true)"
  if [[ -n "$_tenant_out" ]]; then
    while IFS= read -r _tenant_line; do
      echo "PUSH_TENANT_HINT ${_tenant_line:0:400}" >&2
    done <<<"$_tenant_out"
  fi
fi

# ── Hold your pushes (RDR-252, nexus-igea9) ─────────────────────────────
# While a develop red is open, only its owner and the pusher of one of its suspects push to develop: anyone
# else's push lands inside the red's window and hides or confuses its cause (24 pushes landed inside 13 red
# windows in the 2026-11-22 audit; the owner's "Hold your pushes" line was display only). `nx ci check-hold`
# holds the logic (so any repo's push script can call it; this script is its first caller and passes the board
# base, nexus-<branch>, explicitly): it reads the open red on board/ci/nexus-develop--owner and the suspects'
# push records. A refusal is PUSH_REFUSED_DEVELOP_RED on stdout and exit 13, which this script passes on.
# --red-fix '<reason>' (env NX_PUSH_RED_FIX) pushes past it, on the record: the command prints one
# PUSH_RED_FIX_APPLIED line (PUSH_RED_FIX_UNNEEDED when nothing held the push), and this script passes the
# reason to `nx ci record-push`, which writes it into the push record body. A check that cannot be made (no installed nx, a read that failed, an nx that has no such
# command or crashes, or one past NX_PUSH_GATE_TIMEOUT_S, default 20 s) is one PUSH_GATE_UNCHECKED line on
# stderr and the push goes on: the gate holds a push for a red it can name, not for its own fault.
# Develop only.
# _bounded SECONDS OUTFILE CMD...: run CMD with its output in OUTFILE; its exit code, or 124 once it has
# run SECONDS (TERM, then KILL a second later). macOS ships no timeout(1).
_bounded() {
  local limit="$1" outfile="$2" pid ticks=0
  shift 2
  "$@" >"$outfile" 2>&1 &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    if (( ticks >= limit * 10 )); then
      kill -TERM "$pid" 2>/dev/null || true
      sleep 1
      kill -KILL "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      return 124
    fi
    sleep 0.1
    ticks=$(( ticks + 1 ))
  done
  wait "$pid"
}

_red_fix_escape=""   # the --red-fix reason once the hold was actually overridden; the push record carries it (Sam, 2026-11-22)
_gate_nx="${_nx_bin:-}"
if [[ "$branch" == "develop" ]]; then
  [[ -n "$_gate_nx" ]] || _gate_nx="$(installed_nx_resolve || true)"
  if [[ -z "$_gate_nx" ]]; then
    echo "PUSH_GATE_UNCHECKED nx is not installed on PATH (only a dev-checkout or venv copy, or none); the develop-red hold was not checked" >&2
  else
    _gate_limit="${NX_PUSH_GATE_TIMEOUT_S:-20}"
    [[ "$_gate_limit" =~ ^[0-9]+$ ]] && _gate_limit=$(( 10#$_gate_limit )) || _gate_limit=20
    (( _gate_limit > 0 )) || _gate_limit=20
    _gate_code=0
    _gate_file="$(mktemp "${TMPDIR:-/tmp}/push-gate.XXXXXX" 2>/dev/null || true)"
    if [[ -z "$_gate_file" ]]; then
      echo "PUSH_GATE_UNCHECKED no temp file; the develop-red hold was not checked" >&2
    else
      _record_tmp="$_gate_file"
      _gate_args=(ci check-hold --base "nexus-$branch" --tip "$tip")
      [[ -z "${NX_PUSH_RED_FIX:-}" ]] || _gate_args+=(--red-fix "$NX_PUSH_RED_FIX")
      _bounded "$_gate_limit" "$_gate_file" "$_gate_nx" "${_gate_args[@]}" || _gate_code=$?
      if (( _gate_code == 0 || _gate_code == 13 )); then
        # The command's own lines: PUSH_GATE_UNCHECKED is a warning (stderr), everything else is the push summary.
        while IFS= read -r _gate_line; do
          [[ -n "${_gate_line//[[:space:]]/}" ]] || continue
          if [[ "$_gate_line" == PUSH_GATE_UNCHECKED* ]]; then
            echo "$_gate_line" >&2
          else
            echo "$_gate_line"
            # The marker nx ci check-hold prints for an override (nexus.ci_push_gate.RED_FIX_APPLIED), matched as a line
            # prefix and nothing else: the reason after it is free text. The flag nothing needed prints another marker.
            if [[ "$_gate_line" == "PUSH_RED_FIX_APPLIED "* ]]; then _red_fix_escape="${NX_PUSH_RED_FIX:-}"; fi
          fi
        done < "$_gate_file"
      elif (( _gate_code == 124 )); then
        echo "PUSH_GATE_UNCHECKED the develop-red check ran past ${_gate_limit}s and was stopped; the hold was not checked" >&2
      else
        _gate_last="$(grep -v '^[[:space:]]*$' "$_gate_file" | tail -n 1 || true)"
        echo "PUSH_GATE_UNCHECKED the develop-red check failed (exit $_gate_code: ${_gate_last:0:300}); the hold was not checked" >&2
      fi
      rm -f "$_gate_file"
      _record_tmp=""
      if (( _gate_code == 13 )) && [[ -z "$_red_fix_escape" ]]; then exit 13; fi
    fi
  fi
fi

# ── Check gate (nexus-igea9) ─────────────────────────────────────────────
# See "Check gate" in the header. The logic is scripts/push_check_gate.py, found beside this script (this script's own
# checkout, like the scope audit's helper); it reads the TIP's tree, which it materialises under a temp directory this
# script owns, so the EXIT trap can remove the tree even when the gate is killed. It needs Python 3.11 or newer: NX_PUSH_CHECK_PYTHON
# when set (and then it must work, no fallback), else this checkout's .venv, else python3.13/3.12/3.11/python3.
# stdout carries the refusal and the PUSH_CHECK_SKIPPED lines (this script's summary); the rest is on stderr.
_check_skip_records=()   # "check=reason" for each check overridden by name; the push record carries them
_check_proof_records=()  # "leg=tree12,started,seconds,host" for each proof accepted; the push record carries them
_check_python_ok() {
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}
_check_python_find() {
  local cand top
  if [[ -n "${NX_PUSH_CHECK_PYTHON:-}" ]]; then
    _check_python_ok "$NX_PUSH_CHECK_PYTHON" && { printf '%s' "$NX_PUSH_CHECK_PYTHON"; return 0; }
    return 1
  fi
  top="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  for cand in "${top:+$top/.venv/bin/python}" python3.13 python3.12 python3.11 python3; do
    [[ -n "$cand" ]] || continue
    if [[ "$cand" == */* ]]; then [[ -x "$cand" ]] || continue; else command -v "$cand" >/dev/null 2>&1 || continue; fi
    _check_python_ok "$cand" && { printf '%s' "$cand"; return 0; }
  done
  return 1
}
_check_gate_error() {
  echo "PUSH_REFUSED_CHECK_GATE_ERROR the check gate could not run: $*"
  echo "  override, on the record: --skip-check-gate '<reason>'"
  exit 15
}
if [[ "$branch" == "develop" ]]; then
  _check_gate_script="$_script_dir/push_check_gate.py"
  if [[ -n "${_CHECK_SKIP_CHECK_GATE:-}" ]]; then
    echo "PUSH_CHECK_SKIPPED check-gate reason=${_CHECK_SKIP_CHECK_GATE}"
    _check_skip_records+=("check-gate=${_CHECK_SKIP_CHECK_GATE}")
  else
    [[ -f "$_check_gate_script" ]] || _check_gate_error "$_check_gate_script is missing from this checkout"
    _check_py="$(_check_python_find || true)"
    [[ -n "$_check_py" ]] || _check_gate_error "no Python 3.11 or newer (tried NX_PUSH_CHECK_PYTHON, this checkout's .venv, python3.13 to python3); run uv sync, or set NX_PUSH_CHECK_PYTHON"
    _check_workdir="$(mktemp -d "${TMPDIR:-/tmp}/push-check-gate.XXXXXX" 2>/dev/null || true)"
    [[ -n "$_check_workdir" && -d "$_check_workdir" ]] || { _check_workdir=""; _check_gate_error "no temp directory for the tip's tree (TMPDIR=${TMPDIR:-/tmp})"; }
    _check_args=(--repo "$_scrub_root" --base "refs/remotes/$remote/$branch" --tip "$tip" --workdir "$_check_workdir" --python "$_check_py")
    for _check_proof in "${_check_proofs[@]+"${_check_proofs[@]}"}"; do _check_args+=(--proof "$_check_proof"); done
    for _check_name in file-size changelog context-budget wire-ledger cloud-leg plugin-ledger docs-readers write-seam; do
      _check_var="_CHECK_SKIP_$(printf '%s' "${_check_name//-/_}" | tr '[:lower:]' '[:upper:]')"
      [[ -z "${!_check_var:-}" ]] || _check_args+=(--skip "$_check_name=${!_check_var}")
    done
    # The re-push line a refusal for a missing proof prints: this command, with the proofs it lacks added before the shas.
    _repush_prefix="scripts/git-push-develop.sh ${_echo_flag_text}"
    if (( _range_allowlist )) && [[ -n "$_range_digest" ]]; then
      _repush_prefix+="--allowed-paths-from-range=$_range_digest "
    elif [[ -n "${NX_PUSH_ALLOWED_PATHS:-}" ]]; then
      _repush_prefix+="--allowed-paths $(printf '%q' "$NX_PUSH_ALLOWED_PATHS") "
    fi
    _check_args+=(--repush-prefix "${_repush_prefix% }" --repush-suffix "$_vouch_text")
    _check_out="$_check_workdir/gate.out"
    _check_code=0
    "$_check_py" "$_check_gate_script" "${_check_args[@]}" >"$_check_out" || _check_code=$?
    while IFS= read -r _check_line; do
      echo "$_check_line"
      # The line push_check_gate.py prints for an override: "PUSH_CHECK_SKIPPED <check> reason=<free text>".
      if [[ "$_check_line" == "PUSH_CHECK_SKIPPED "* ]]; then
        _check_rest="${_check_line#PUSH_CHECK_SKIPPED }"
        _check_skip_records+=("${_check_rest%% reason=*}=${_check_rest#* reason=}")
      elif [[ "$_check_line" == "PUSH_PROOF_ACCEPTED "* ]]; then
        # "PUSH_PROOF_ACCEPTED <leg> tree=<12> started_at=<iso> elapsed_s=<n> host=<h> file=<f>": no field holds a space.
        read -r _ _pp_leg _pp_tree _pp_start _pp_secs _pp_host _ <<<"$_check_line"
        _pp_secs="${_pp_secs#elapsed_s=}"
        [[ "$_pp_secs" =~ ^[0-9]+$ ]] || _pp_secs=0
        _check_proof_records+=("${_pp_leg}=${_pp_tree#tree=},${_pp_start#started_at=},${_pp_secs},${_pp_host#host=}")
      fi
    done < "$_check_out"
    _check_cleanup
    case "$_check_code" in
    0) ;;
    14 | 15) exit "$_check_code" ;;
    *) echo "PUSH_REFUSED_CHECK_GATE_ERROR the check gate failed (exit $_check_code) before it could judge the tip" ; echo "  override, on the record: --skip-check-gate '<reason>'"; exit 15 ;;
    esac
  fi
fi

# ── Push lock (nexus-agctp) ──────────────────────────────────────────────
# Runs after every other gate, right before the push itself: a refusal
# above this point is about the commits, not about who else is pushing,
# and costs nothing extra by happening first.
#
# Resolve the INSTALLED nx generation deliberately (review finding 2; see
# "Installed nx only" in the header and scripts/lib/installed-nx.sh).
if [[ -n "${NX_PUSH_SKIP_LOCK:-}" ]]; then
  echo "PUSH_LOCK_SKIPPED reason=${NX_PUSH_SKIP_LOCK}" >&2
else
  _nx_bin="$(installed_nx_resolve || true)"
  if [[ -z "$_nx_bin" ]]; then
    echo "PUSH_REFUSED_LOCK_DEV_CHECKOUT_NX only a dev-checkout/venv nx is on PATH; the installed generation was not found."
    echo "The nexus-a2qhz production-write guard refuses every real tuple-space write from a dev-checkout CLI, which otherwise"
    echo "reads identically to an unreachable tuple space -- refusing outright here instead so the two are never confused."
    echo "Fix PATH so the installed generation resolves first (avoid 'uv run' / an activated venv for this script), or"
    echo "reinstall it: scripts/reinstall-tool.sh."
    echo "Pass --skip-lock '<reason>' (env NX_PUSH_SKIP_LOCK) to push without the lock, on the record."
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
    echo "Pass --skip-lock '<reason>' (env NX_PUSH_SKIP_LOCK) to push without the lock, on the record."
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
    echo "Pass --skip-lock '<reason>' (env NX_PUSH_SKIP_LOCK) to push without the lock, on the record."
    exit 8
  fi
fi

# The push record's base (RDR-252 Step 2): develop's remote tip as the fetch above left it, read just
# before the push. Under the lock no other push lands in between, so it is the tip this push replaces. A
# push that lands while the lock is skipped, or one whose range already holds a peer's newer tip, can make
# it differ; the stamper then sees a gap in the chain, which the RDR handles ("Develop's state from commit
# order").
_record_base="$(git rev-parse -q --verify "refs/remotes/$remote/$branch^{commit}" 2>/dev/null || true)"

git push -q "$remote" "$tip:refs/heads/$branch"
echo "PUSH_OK n=${#range[@]} tip=$tip"

# The lock is released now, before the push record, so a slow tuple space while the record is written
# never keeps other sessions' pushes waiting. The EXIT trap then finds no claim and does nothing.
_release_push_lock
_lock_claim_id=""

# ── Push record (RDR-252 Step 2, nexus-igea9.2) ─────────────────────────
# One tuple per push to develop, on every path that reaches PUSH_OK (--skip-lock included), written by
# `nx ci record-push` (the logic is nx's, so any repo's push script can call it; this script is its first caller and
# passes the base, nexus-<branch>, explicitly). RDR-252's owner stamper reads the record: its body's base linked to
# the tip orders develop's tips, and the record's session stamp maps the push to the pushing identity; it never reads
# `from`, a label (the identity id, or "unknown"). The command writes the stamped record, retries once as a minimal
# unstamped one when that fails, and prints one status line: PUSH_RECORD_OK (stamped; nothing is added to the summary),
# PUSH_RECORD_UNSTAMPED, PUSH_RECORD_FALLBACK or PUSH_RECORD_FAILED (each goes in the push summary on stdout, after
# PUSH_OK, and on stderr: nexus-igea9 audit S3, 2 of 9 pushes in an evening had no record and nobody saw it). A skipped
# record (no installed nx) is PUSH_RECORD_SKIPPED, the same way. An nx with no `ci record-push` (exit 2, "No such
# command") gets the legacy `nx tuple out` write, reported as PUSH_RECORD_LEGACY; one that crashes or runs past
# NX_PUSH_RECORD_TIMEOUT_S seconds (default 20) is PUSH_RECORD_FAILED. Nothing here can fail the push that just
# landed: the exit code stays the push's. A push that overrode the develop-red hold passes its --red-fix reason to the
# command, which writes it into the record body.
_record_report() {
  echo "$1"
  echo "$1" >&2
}

# A mixed fleet: an installed nx that predates `nx ci record-push` answers "No such command" (exit 2). The record is then
# written the way this script wrote it before the command existed, with `nx tuple out` (from=unknown, body base and n), so
# the chain keeps its link. It is one bounded attempt, reported as PUSH_RECORD_LEGACY (written) or PUSH_RECORD_FAILED. An nx
# too old for the record is too old for `nx ci check-hold` too, so no --red-fix override reaches this path.
_write_legacy_record() {
  local nxbin="$1" limit="$2" outfile code=0 last
  outfile="$(mktemp "${TMPDIR:-/tmp}/push-record.XXXXXX")" || { _record_report "PUSH_RECORD_FAILED the installed CLI lacks 'nx ci record-push' and no temp file for the legacy write (the push stands)"; return 0; }
  _record_tmp="$outfile"
  _bounded "$limit" "$outfile" "$nxbin" tuple out "board/ci/nexus-$branch--push" --key "topic=nexus-$branch--push" \
    --nonce "push-$tip" --dim "from=unknown" --dim "kind=push" --dim "sha=$tip" \
    --body "{\"base\": \"${_record_base}\", \"n\": ${#range[@]}}" || code=$?
  last="$(grep -v '^[[:space:]]*$' "$outfile" | tail -n 1 || true)"
  rm -f "$outfile"
  _record_tmp=""
  if (( code == 124 )); then
    _record_report "PUSH_RECORD_FAILED the installed CLI lacks 'nx ci record-push' and the legacy 'nx tuple out' write ran past ${limit}s and was stopped (the push stands)"
  elif (( code != 0 )); then
    _record_report "PUSH_RECORD_FAILED the installed CLI lacks 'nx ci record-push' and the legacy 'nx tuple out' write failed too (exit $code: ${last:0:300}) (the push stands, no record was written)"
  else
    _record_report "PUSH_RECORD_LEGACY the installed CLI lacks 'nx ci record-push'; the record was written with 'nx tuple out' (from=unknown, no retry or stamp check): upgrade nx (scripts/reinstall-tool.sh) so the record names its pusher"
  fi
}

_write_push_record() {
  [[ "$branch" == "develop" ]] || return 0
  local nxbin="${_nx_bin:-}" limit="${NX_PUSH_RECORD_TIMEOUT_S:-20}" outfile code=0 last status legacy=0
  [[ "$limit" =~ ^[0-9]+$ ]] && limit=$(( 10#$limit )) || limit=20   # base 10: a leading zero (08) is not octal
  (( limit > 0 )) || limit=20
  # The lock path already resolved the installed nx; --skip-lock resolves it here, the same way.
  [[ -n "$nxbin" ]] || nxbin="$(installed_nx_resolve || true)"
  if [[ -z "$nxbin" ]]; then
    _record_report "PUSH_RECORD_SKIPPED: nx is not installed on PATH (only a dev-checkout or venv copy, or none); the push stands"
    return 0
  fi
  outfile="$(mktemp "${TMPDIR:-/tmp}/push-record.XXXXXX")" || { _record_report "PUSH_RECORD_FAILED no temp file (the push stands)"; return 0; }
  _record_tmp="$outfile"
  local args=(ci record-push --base "nexus-$branch" --from-sha "$_record_base" --to-sha "$tip" --count "${#range[@]}")
  [[ -z "$_red_fix_escape" ]] || args+=(--red-fix "$_red_fix_escape")
  local extra_args=() entry
  for entry in "${_check_skip_records[@]+"${_check_skip_records[@]}"}"; do extra_args+=(--skip "$entry"); done
  for entry in "${_check_proof_records[@]+"${_check_proof_records[@]}"}"; do extra_args+=(--proof-info "$entry"); done
  _bounded "$limit" "$outfile" "$nxbin" "${args[@]}" "${extra_args[@]+"${extra_args[@]}"}" || code=$?
  # An installed nx that predates `--skip` and `--proof-info` answers "No such option" (exit 2): write the record without them
  # rather than lose it, and say so, since the board then lacks them (the push summary above still names them).
  if (( code == 2 && ${#extra_args[@]} > 0 )) && grep -q "No such option" "$outfile"; then
    echo "PUSH_RECORD_NOTE the installed CLI lacks 'nx ci record-push --skip' or '--proof-info': the record was written without the check overrides and proofs (upgrade nx: scripts/reinstall-tool.sh)" >&2
    code=0
    _bounded "$limit" "$outfile" "$nxbin" "${args[@]}" || code=$?
  fi
  # The last non-empty line: nx prints its own log lines first, its status or error last.
  last="$(grep -v '^[[:space:]]*$' "$outfile" | tail -n 1 || true)"
  status="$(grep '^PUSH_RECORD_' "$outfile" | tail -n 1 || true)"
  # Exit 2 is click's usage error: an old nx ("No such command") and a refused base look alike but for the words.
  if (( code == 2 )) && grep -q "No such command" "$outfile"; then legacy=1; fi
  rm -f "$outfile"
  _record_tmp=""
  if (( legacy )); then
    _write_legacy_record "$nxbin" "$limit"
  elif (( code == 124 )); then
    _record_report "PUSH_RECORD_FAILED the write ran past ${limit}s and was stopped (the push stands)"
  elif (( code != 0 )) || [[ -z "$status" ]]; then
    _record_report "PUSH_RECORD_FAILED exit $code: ${last:0:400} (the push stands)"
  elif [[ "$status" != PUSH_RECORD_OK* ]]; then
    _record_report "${status:0:600}"
  fi
  return 0
}
_write_push_record || true

# nexus-1d6pz.11.4: the two ways to be woken when this push's CI completes, on
# stderr (stdout keeps its PUSH_OK contract). A session with the channel subscribes
# to the --done topic; one without runs nx ci wait as a background command, whose
# exit is the wake and whose one line is the result.
echo "PUSH_WAIT channel: tuple_subscribe(\"board/ci/nexus-${branch}--done\"); a wake for this push carries sha=\"$tip\"" >&2
echo "PUSH_WAIT no channel: run as a background command: nx ci wait $tip --branch $branch" >&2

# nexus-cvtxu: the batching hint (see NX_PUSH_BATCH_MINUTES above). The stamp lives
# in this worktree's own git dir, and a session works in one worktree, so the gap
# is this session's. Best effort: nothing here can fail the push that just landed.
_batch_minutes="${NX_PUSH_BATCH_MINUTES:-30}"
[[ "$_batch_minutes" =~ ^[0-9]+$ ]] || _batch_minutes=30
_batch_window_s=$(( _batch_minutes * 60 ))
_batch_gitdir="$(git rev-parse --git-dir 2>/dev/null || true)"
_batch_stamp="${_batch_gitdir:-.}/nexus-last-develop-push"
_batch_now="$(date +%s)"
if [[ -n "$_batch_gitdir" && $_batch_window_s -gt 0 && -f "$_batch_stamp" ]]; then
  _batch_prev="$(cat "$_batch_stamp" 2>/dev/null || true)"
  if [[ "$_batch_prev" =~ ^[0-9]+$ ]] && (( _batch_now - _batch_prev < _batch_window_s )); then
    echo "PUSH_BATCH_HINT this worktree pushed to $branch $(( (_batch_now - _batch_prev) / 60 )) min ago; develop CI runs one run at a time, so each push waits for the one before it. Hold commits and push them together (AGENTS.md rule 8, nexus-cvtxu)." >&2
  fi
fi
[[ -n "$_batch_gitdir" ]] && { printf '%s\n' "$_batch_now" > "$_batch_stamp"; } 2>/dev/null || true
