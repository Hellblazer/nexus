# SPDX-License-Identifier: AGPL-3.0-or-later
"""The managed ``pre-push`` stanza (RDR-252 "Every repository" item 4, bead nexus-igea9).

What the stanza does, for each ref a push updates (git sends ``<local ref> <local sha> <remote ref> <remote sha>`` lines
on standard input):

* a branch the CI board does not watch (anything but :data:`nexus.ci_wait.BOARD_BRANCHES`), a delete, and a push made while
  ``NX_PUSH_VIA_SCRIPT`` is set are left alone. ``scripts/git-push-develop.sh`` sets that variable on its own ``git push``: it
  has already run ``nx ci check-hold`` and writes its own push record, so the hook stands down rather than record it twice.
  The ``nx`` it calls is the first one on PATH that is neither under a ``.venv`` directory nor under the repository (the
  push script's rule: a dev-checkout ``nx`` is refused by the production-write guard); a box with none is left alone.
* ``nx ci check-hold --remote <remote> --branch <branch> --tip <sha>`` decides, each call stopped after
  ``NX_PUSH_GATE_TIMEOUT_S`` seconds (the push script's name for it; ``NX_PRE_PUSH_GATE_S`` is read as an alias; default 20).
  Exit 13 refuses: the hook shows the command's lines, adds one line naming ``NX_PUSH_RED_FIX='<reason>' git push``, and exits
  13; the push is not made and not recorded. Any other failure (a base that cannot be derived, a crash, a timeout) holds
  nobody, as the command's own unreadable-board cases do. A ``NX_PUSH_RED_FIX`` reason is passed on as ``--red-fix``; when
  the command prints ``PUSH_RED_FIX_APPLIED`` the reason goes into the push record too.
* Git has no post-push hook, so once every ref is allowed the stanza starts one detached job per ref. The job first waits for
  the ``git push`` that ran the hook (its parent pid) to exit, then asks ``git ls-remote`` whether the remote branch shows the
  pushed tip, every ``NX_PRE_PUSH_POLL_S`` seconds for at most ``NX_PRE_PUSH_GRACE_S`` (default 30) after the push exited, and
  then calls ``nx ci record-push``. The whole job is bounded by ``NX_PRE_PUSH_WAIT_S`` (ten minutes) and every ``ls-remote``
  and the record call get a watchdog, so a remote that never answers cannot keep it alive; ``GIT_TERMINAL_PROMPT=0`` keeps it
  from asking on the terminal. A push another hook refused, or a remote that never shows the tip, ends the job quietly: one
  line in ``<nexus config dir>/pre-push.log`` (rotated at 1 MiB) and exit 0. The job holds no file descriptor the push has,
  so it never holds the push, and it outlives the hook process.

Where it sits. The stanza is inserted right after the hook's shebang, ahead of any existing body, because a body that ends in
``exec`` or ``exit`` (a beads shim does) would otherwise never reach a stanza appended after it. It saves the ref lines it read
and re-feeds them to the rest of the hook, so a later body still sees git's standard input. A hook whose interpreter is not
a POSIX shell is left alone (:func:`is_shell_hook`). Install and removal are exact inverses (:func:`insert_stanza`,
:func:`remove_stanza`): removing the stanza gives back the bytes that were there, and a file nexus created is removed whole.

Decisions of record (nexus-igea9, Sam's design of RDR-252 item 4 and the dispatch that built this):

* The hook can refuse pushes, so ``nx upgrade`` installs it only in a catalog-registered repo whose derived base
  (``<repo>-<branch>``, :mod:`nexus.ci_base`) is served by a live group the local identity belongs to
  (:mod:`nexus.pre_push_hook.serving`), and only when the hooks directory sits inside that repo (a shared
  ``core.hooksPath`` would arm every repo that uses it). An explicit ``nx hooks install --pre-push`` installs it anywhere. The
  refresh rewrites a stanza that is already there, in any repo, as it does for the other hooks.
* The branches are the adapter's: the stanza is built from ``BOARD_BRANCHES``, so widening the adapter's one branch list is
  one change here too.
"""
from __future__ import annotations

import re

from nexus._git_hooks_meta import PRE_PUSH_HOOK, SENTINEL_BEGIN, SENTINEL_END
from nexus.ci_wait import BOARD_BRANCHES

__all__ = ["CREATED_NOTE", "PRE_PUSH_HOOK", "STANZA", "VIA_SCRIPT_ENV", "create_content", "insert_stanza", "is_shell_hook", "remove_stanza"]

#: The environment variable ``scripts/git-push-develop.sh`` sets on its own ``git push``.
VIA_SCRIPT_ENV = "NX_PUSH_VIA_SCRIPT"

#: The line that marks a file nexus created, so removing the stanza can tell it from a hook that was there before.
CREATED_NOTE = "# created for the managed pre-push stanza; removed with it"

_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh"})

_BODY = r"""
# RDR-252 / nexus-igea9. The hold check, then the push record. See nexus/pre_push_hook/stanza.py.
_nx_pp_run() {
  _nx_l="$1"; _nx_o="$2"; shift 2
  "$@" > "$_nx_o" 2>&1 &
  _nx_p=$!
  ( sleep "$_nx_l"; kill -TERM "$_nx_p" ) > /dev/null 2>&1 &
  _nx_w=$!
  _nx_c=0
  { wait "$_nx_p"; } 2> /dev/null || _nx_c=$?
  kill "$_nx_w" > /dev/null 2>&1 || :
  { wait "$_nx_w"; } 2> /dev/null || :
  return "$_nx_c"
}
_nx_pp_job() {
  # $1 remote, $2 branch, $3 tip, $4 old tip (all zeros: a new branch), $5 red-fix reason, $6 commit count, $7 the push's pid
  _nx_log="${NEXUS_CONFIG_DIR:-$HOME/.config/nexus}/pre-push.log"
  mkdir -p "$(dirname "$_nx_log")" 2> /dev/null || :
  if [ -f "$_nx_log" ]; then
    _nx_size="$(wc -c < "$_nx_log" 2> /dev/null || echo 0)"
    if [ "$(( _nx_size + 0 ))" -gt 1048576 ]; then mv -f "$_nx_log" "$_nx_log.1" 2> /dev/null || :; fi
  fi
  ( : >> "$_nx_log" ) 2> /dev/null || _nx_log=/dev/null
  (
    trap '' HUP
    export GIT_TERMINAL_PROMPT=0
    _j_end=$(( $(date +%s) + ${NX_PRE_PUSH_WAIT_S:-600} ))
    echo "=== pre-push record $2 $3 $(date +%Y-%m-%dT%H:%M:%S%z) ==="
    while kill -0 "$7" 2> /dev/null; do
      if [ "$(date +%s)" -ge "$_j_end" ]; then echo "gave up: the push never finished"; exit 0; fi
      sleep "${NX_PRE_PUSH_POLL_S:-5}"
    done
    _j_grace=$(( $(date +%s) + ${NX_PRE_PUSH_GRACE_S:-30} ))
    while :; do
      _j_left=$(( _j_end - $(date +%s) ))
      if [ "$_j_left" -le 0 ]; then echo "gave up: $1 never showed $2 at $3 within ${NX_PRE_PUSH_WAIT_S:-600}s"; exit 0; fi
      if [ "$_j_left" -gt 30 ]; then _j_left=30; fi
      _j_out="$(mktemp "${TMPDIR:-/tmp}/nx-pre-push-ls.XXXXXX" 2> /dev/null)" || exit 0
      _nx_pp_run "$_j_left" "$_j_out" git ls-remote "$1" "refs/heads/$2" || :
      _j_seen="$(cut -f1 < "$_j_out" 2> /dev/null | head -n 1)"
      rm -f "$_j_out"
      if [ "$_j_seen" = "$3" ]; then break; fi
      if [ "$(date +%s)" -ge "$_j_grace" ]; then echo "gave up: $1 does not show $2 at $3 (the push did not land, or the remote is unreachable)"; exit 0; fi
      sleep "${NX_PRE_PUSH_POLL_S:-5}"
    done
    _j_fix="$5"; _j_n="$6"; _j_old="$4"
    case "$_j_old" in *[!0]*) ;; *) _j_old="" ;; esac
    set -- ci record-push --remote "$1" --branch "$2" --from-sha "$_j_old" --to-sha "$3"
    if [ -n "$_j_n" ]; then set -- "$@" --count "$_j_n"; fi
    if [ -n "$_j_fix" ]; then set -- "$@" --red-fix "$_j_fix"; fi
    _j_out="$(mktemp "${TMPDIR:-/tmp}/nx-pre-push-rec.XXXXXX" 2> /dev/null)" || exit 0
    _nx_pp_run 120 "$_j_out" "$_nx_bin" "$@" || :
    cat "$_j_out" 2> /dev/null
    rm -f "$_j_out"
  ) < /dev/null >> "$_nx_log" 2>&1 &
}
_nx_pre_push() {
  if [ -n "${NX_PUSH_VIA_SCRIPT:-}" ]; then return 0; fi
  _nx_ppid="$PPID"
  _nx_top="$(git rev-parse --show-toplevel 2> /dev/null || :)"
  if [ -n "$_nx_top" ]; then _nx_top="$(cd "$_nx_top" 2> /dev/null || exit 1; pwd -P)" || _nx_top=""; fi
  _nx_bin=""
  _nx_oifs="$IFS"
  IFS=:
  for _nx_d in $PATH; do
    if [ -z "$_nx_d" ] || [ ! -x "$_nx_d/nx" ]; then continue; fi
    _nx_r="$(cd "$_nx_d" 2> /dev/null || exit 1; pwd -P)" || continue
    case "$_nx_r" in */.venv/*) continue ;; esac
    if [ -n "$_nx_top" ]; then case "$_nx_r/" in "$_nx_top"/*) continue ;; esac; fi
    _nx_bin="$_nx_d/nx"
    break
  done
  IFS="$_nx_oifs"
  if [ -z "$_nx_bin" ]; then return 0; fi
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
    if [ -n "${NX_PUSH_RED_FIX:-}" ]; then set -- "$@" --red-fix "$NX_PUSH_RED_FIX"; fi
    _nx_c=0
    _nx_pp_run "${NX_PUSH_GATE_TIMEOUT_S:-${NX_PRE_PUSH_GATE_S:-20}}" "$_nx_out" "$_nx_bin" "$@" < /dev/null || _nx_c=$?
    if [ "$_nx_c" -eq 13 ]; then
      cat "$_nx_out" >&2
      rm -f "$_nx_out"
      echo "To push past the hold, on the record: NX_PUSH_RED_FIX='<reason>' git push ..." >&2
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
    _nx_pp_job "$_nx_remote" "$_nx_branch" "$_nx_lsha" "$_nx_rsha" "$_nx_fix" "$_nx_n" "$_nx_ppid"
  done << NX_PRE_PUSH_EOF
$_nx_lines
NX_PRE_PUSH_EOF
  return 0
}
_nx_pre_push "$@"
"""

STANZA = (SENTINEL_BEGIN + _BODY.replace("@BRANCHES@", " ".join(sorted(BOARD_BRANCHES))).rstrip("\n") + "\n" + SENTINEL_END)

_BLOCK = rf"{re.escape(SENTINEL_BEGIN)}.*?{re.escape(SENTINEL_END)}"
# install is `first\n<stanza>\nrest`, `first\n<stanza>` (a shebang with no newline) or `<stanza>\nrest` (no shebang); each has an exact inverse
_AFTER_SHEBANG = re.compile(rf"\A(?P<first>#![^\n]*)\n(?P<note>{re.escape(CREATED_NOTE)}\n)?{_BLOCK}(?:\n(?P<rest>.*)\Z|\Z)", re.DOTALL)
_BARE = re.compile(rf"\A{_BLOCK}(?:\n(?P<rest>.*)\Z|\Z)", re.DOTALL)


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


def create_content() -> str:
    """The file nexus writes when there is no pre-push hook yet."""
    return f"#!/bin/sh\n{CREATED_NOTE}\n{STANZA}\n"


def insert_stanza(content: str) -> str:
    """*content* with the stanza right after its shebang line (at the top when it has none). The stanza goes ahead of the
    existing body so a body that ends in ``exec`` or ``exit`` still runs after it. :func:`remove_stanza` undoes this exactly."""
    if content.startswith("#!"):
        first, sep, rest = content.partition("\n")
        return f"{first}\n{STANZA}\n{rest}" if sep else f"{first}\n{STANZA}"
    return f"{STANZA}\n{content}"


def remove_stanza(content: str) -> str | None:
    """*content* as it was before the stanza went in, or None when nothing but nexus's own creation (:func:`create_content`)
    is left, so the caller removes the file. A stanza that is not where :func:`insert_stanza` puts it (a hand edit) is taken
    out by the caller's general rule instead: this returns *content* unchanged then."""
    m = _AFTER_SHEBANG.match(content)
    if m is not None:
        rest = m.group("rest")
        if m.group("note") is not None and not rest:
            return None
        return f"{m.group('first')}\n{rest}" if rest is not None else m.group("first")
    m = _BARE.match(content)
    if m is not None:
        return m.group("rest") or ""
    return content
