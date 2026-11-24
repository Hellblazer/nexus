# SPDX-License-Identifier: AGPL-3.0-or-later
"""The managed ``pre-push`` stanza (RDR-252 "Every repository" item 4, bead nexus-igea9).

What the stanza does, for each ref a push updates (git sends ``<local ref> <local sha> <remote ref> <remote sha>`` lines
on standard input):

* a branch the CI board does not watch (anything but :data:`nexus.ci_wait.BOARD_BRANCHES`), a delete, and a push made while
  ``NX_PUSH_VIA_SCRIPT`` is set are left alone. ``scripts/git-push-develop.sh`` sets that variable on its own ``git push``: it
  has already run ``nx ci check-hold`` and writes its own push record, so the hook stands down rather than record it twice.
  A box with no ``nx`` on PATH is left alone too.
* ``nx ci check-hold --remote <remote> --branch <branch> --tip <sha>`` decides, bounded to 20 seconds (``NX_PRE_PUSH_GATE_S``). Exit 13
  refuses: the hook shows the command's lines and exits 13, and the push is not made and not recorded. Any other failure
  (a base that cannot be derived, a crash, a timeout) holds nobody, as the command's own unreadable-board cases do. A
  ``NX_PUSH_RED_FIX`` reason is passed on as ``--red-fix``; when the command prints ``PUSH_RED_FIX_APPLIED`` the reason goes into
  the push record too.
* Git has no post-push hook, so once every ref is allowed the stanza starts one detached job per ref: it polls
  ``git ls-remote`` until the remote branch shows the pushed tip, for at most ten minutes (``NX_PRE_PUSH_WAIT_S``), and then calls
  ``nx ci record-push``. A push another hook refuses, or a network that never comes back, ends the job quietly after the
  wait: it logs one line to ``<nexus config dir>/pre-push.log`` and exits 0. The job holds no file descriptor the push has,
  so it never holds the push, and it outlives the hook process.

Where it sits. The stanza is inserted right after the hook's shebang, ahead of any existing body, because a body that ends in
``exec`` or ``exit`` (a beads shim does) would otherwise never reach a stanza appended after it. It saves the ref lines it read
and re-feeds them to the rest of the hook, so a later body still sees git's standard input. A hook whose interpreter is not
a POSIX shell is left alone (:func:`is_shell_hook`).

Decisions of record (nexus-igea9, Sam's design of RDR-252 item 4 and the dispatch that built this):

* The hook can refuse pushes, so ``nx upgrade`` installs it only in a catalog-registered repo whose derived base
  (``<repo>-<branch>``, :mod:`nexus.ci_base`) is served by a live group the local identity belongs to
  (:mod:`nexus.pre_push_hook.serving`). An explicit ``nx hooks install --pre-push`` installs it anywhere. The refresh rewrites
  a stanza that is already there, in any repo, as it does for the other hooks.
* The branches are the adapter's: the stanza is built from ``BOARD_BRANCHES``, so widening the adapter's one branch list is
  one change here too.
"""
from __future__ import annotations

from nexus._git_hooks_meta import PRE_PUSH_HOOK, SENTINEL_BEGIN, SENTINEL_END
from nexus.ci_wait import BOARD_BRANCHES

__all__ = ["PRE_PUSH_HOOK", "STANZA", "VIA_SCRIPT_ENV", "insert_stanza", "is_shell_hook"]

#: The environment variable ``scripts/git-push-develop.sh`` sets on its own ``git push``.
VIA_SCRIPT_ENV = "NX_PUSH_VIA_SCRIPT"

_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh"})

_BODY = r"""
# RDR-252 / nexus-igea9. Hold-your-pushes check and push record; see nexus/pre_push_hook/stanza.py.
_nx_pp_run() {
  _nx_l="$1"; _nx_o="$2"; shift 2
  "$@" > "$_nx_o" 2>&1 &
  _nx_p=$!
  ( sleep "$_nx_l"; kill -TERM "$_nx_p" ) > /dev/null 2>&1 &
  _nx_w=$!
  wait "$_nx_p"; _nx_c=$?
  kill "$_nx_w" > /dev/null 2>&1
  return "$_nx_c"
}
_nx_pp_job() {
  # $1 remote, $2 branch, $3 tip, $4 old tip (all zeros: a new branch), $5 red-fix reason, $6 commit count
  _nx_log="${NEXUS_CONFIG_DIR:-$HOME/.config/nexus}/pre-push.log"
  mkdir -p "$(dirname "$_nx_log")" 2> /dev/null
  _nx_size="$(wc -c < "$_nx_log" 2> /dev/null || echo 0)"
  if [ "$_nx_size" -gt 1048576 ]; then
    mv -f "$_nx_log" "$_nx_log.1" 2> /dev/null || :
  fi
  nohup sh -c '
    echo "=== pre-push record $2 $3 $(date +%Y-%m-%dT%H:%M:%S%z) ==="
    _end=$(( $(date +%s) + ${NX_PRE_PUSH_WAIT_S:-600} ))
    while :; do
      _seen="$(git ls-remote "$1" "refs/heads/$2" 2> /dev/null | cut -f1)"
      if [ "$_seen" = "$3" ]; then break; fi
      if [ "$(date +%s)" -ge "$_end" ]; then
        echo "gave up: $1 never showed $2 at $3 within ${NX_PRE_PUSH_WAIT_S:-600}s (the push did not land, or the remote is unreachable)"
        exit 0
      fi
      sleep "${NX_PRE_PUSH_POLL_S:-5}"
    done
    _fix="$5"; _n="$6"; _old="$4"
    case "$_old" in *[!0]*) ;; *) _old="" ;; esac
    set -- ci record-push --remote "$1" --branch "$2" --from-sha "$_old" --to-sha "$3"
    [ -z "$_n" ] || set -- "$@" --count "$_n"
    [ -z "$_fix" ] || set -- "$@" --red-fix "$_fix"
    exec nx "$@"
  ' nx-pre-push "$1" "$2" "$3" "$4" "$5" "$6" < /dev/null >> "$_nx_log" 2>&1 &
}
_nx_pre_push() {
  [ -z "${NX_PUSH_VIA_SCRIPT:-}" ] || return 0
  command -v nx > /dev/null 2>&1 || return 0
  _nx_remote="$1"
  _nx_in="$(mktemp "${TMPDIR:-/tmp}/nx-pre-push.XXXXXX" 2> /dev/null)" || return 0
  cat > "$_nx_in"
  _nx_lines="$(cat "$_nx_in")"
  exec < "$_nx_in"
  rm -f "$_nx_in"
  _nx_fixed=" "
  while read -r _nx_lref _nx_lsha _nx_rref _nx_rsha; do
    case "$_nx_lsha" in *[!0]*) ;; *) continue ;; esac
    case "$_nx_rref" in refs/heads/*) _nx_branch="${_nx_rref#refs/heads/}" ;; *) continue ;; esac
    case " @BRANCHES@ " in *" $_nx_branch "*) ;; *) continue ;; esac
    _nx_out="$(mktemp "${TMPDIR:-/tmp}/nx-pre-push-out.XXXXXX" 2> /dev/null)" || continue
    set -- ci check-hold --remote "$_nx_remote" --branch "$_nx_branch" --tip "$_nx_lsha"
    [ -z "${NX_PUSH_RED_FIX:-}" ] || set -- "$@" --red-fix "$NX_PUSH_RED_FIX"
    _nx_c=0
    _nx_pp_run "${NX_PRE_PUSH_GATE_S:-20}" "$_nx_out" nx "$@" < /dev/null || _nx_c=$?
    if [ "$_nx_c" -eq 13 ]; then
      cat "$_nx_out" >&2
      rm -f "$_nx_out"
      exit 13
    elif [ "$_nx_c" -ne 0 ]; then
      echo "PUSH_GATE_UNCHECKED the pre-push hold check did not run (exit $_nx_c); the hold was not applied" >&2
    else
      cat "$_nx_out" >&2
      if grep -q '^PUSH_RED_FIX_APPLIED ' "$_nx_out"; then _nx_fixed="$_nx_fixed$_nx_branch "; fi
    fi
    rm -f "$_nx_out"
  done << NX_PRE_PUSH_EOF
$_nx_lines
NX_PRE_PUSH_EOF
  while read -r _nx_lref _nx_lsha _nx_rref _nx_rsha; do
    case "$_nx_lsha" in *[!0]*) ;; *) continue ;; esac
    case "$_nx_rref" in refs/heads/*) _nx_branch="${_nx_rref#refs/heads/}" ;; *) continue ;; esac
    case " @BRANCHES@ " in *" $_nx_branch "*) ;; *) continue ;; esac
    _nx_fix=""
    case "$_nx_fixed" in *" $_nx_branch "*) _nx_fix="${NX_PUSH_RED_FIX:-}" ;; esac
    _nx_n="$(git rev-list --count "$_nx_rsha..$_nx_lsha" 2> /dev/null)" || _nx_n=""
    case "$_nx_rsha" in *[!0]*) ;; *) _nx_n="" ;; esac
    _nx_pp_job "$_nx_remote" "$_nx_branch" "$_nx_lsha" "$_nx_rsha" "$_nx_fix" "$_nx_n"
  done << NX_PRE_PUSH_EOF
$_nx_lines
NX_PRE_PUSH_EOF
  return 0
}
_nx_pre_push "$@"
"""

STANZA = (SENTINEL_BEGIN + _BODY.replace("@BRANCHES@", " ".join(sorted(BOARD_BRANCHES))).rstrip("\n") + "\n" + SENTINEL_END)


def is_shell_hook(content: str) -> bool:
    """Whether a stanza of shell can be put in *content*: no shebang line (git runs it with the shell), or one naming a POSIX
    shell directly or through ``env``."""
    first = content.split("\n", 1)[0]
    if not first.startswith("#!"):
        return True
    words = first[2:].split()
    if not words:
        return True
    prog = words[0].rsplit("/", 1)[-1]
    if prog == "env":
        prog = next((w.rsplit("/", 1)[-1] for w in words[1:] if not w.startswith("-")), "")
    return prog in _SHELLS


def insert_stanza(content: str) -> str:
    """*content* with the stanza right after its shebang line (at the top when it has none). The stanza goes ahead of the
    existing body so a body that ends in ``exec`` or ``exit`` still runs after it."""
    if content.startswith("#!"):
        first, _, rest = content.partition("\n")
        return f"{first}\n{STANZA}\n{rest}"
    return f"{STANZA}\n{content}"
