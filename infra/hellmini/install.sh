#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# infra/hellmini/install.sh [--force] [user ...]   (nexus-xmj1r)
#
# Install the versioned runner hooks under infra/hellmini/hooks/<user>/ into
# /Volumes/Bulk/<user>/actions-runner/hooks/ on hellmini, as that user.
# Run it ON hellmini; it needs passwordless sudo (the hooks are mode 0700 and
# owned by the runner user).
#
# Per file:
#   - live file absent:        install it (mode 700, or 755 for wait-for-host.sh).
#   - live file identical:     nothing to do.
#   - live file differs:       print the diff and REFUSE unless --force. With
#                              --force, first copy the live file to
#                              <file>.bak-<timestamp> (owner and mode kept), then
#                              overwrite it, keeping the live file's mode.
# Every file is checked before any is written, so a refusal changes nothing.
# With no --force this is also the drift check: exit 0 means live == repo.
#
# Exit: 0 ok, 1 a live file differs (no --force), 2 usage or a missing directory.
#
# Test hooks (tests/scripts/test_hellmini_infra.py): HELLMINI_ROOT replaces
# /Volumes/Bulk; HELLMINI_RUN_AS, when set, is a command called as
# "$HELLMINI_RUN_AS <user> <cmd...>" in place of "sudo -n -u <user> <cmd...>".

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${HELLMINI_ROOT:-/Volumes/Bulk}"
USERS_ALL=(ghci ghrunner)

force=0
users=()
for arg in "$@"; do
  case "$arg" in
    --force) force=1 ;;
    -h|--help) sed -n '2,23p' "${BASH_SOURCE[0]}"; exit 0 ;;
    -*) echo "install.sh: unknown option: $arg" >&2; exit 2 ;;
    *) users+=("$arg") ;;
  esac
done
[ ${#users[@]} -gt 0 ] || users=("${USERS_ALL[@]}")

as_user() {
  local u=$1; shift
  if [ -n "${HELLMINI_RUN_AS:-}" ]; then "$HELLMINI_RUN_AS" "$u" "$@"; else sudo -n -u "$u" "$@"; fi
}

default_mode() { if [ "$1" = wait-for-host.sh ]; then echo 755; else echo 700; fi; }

live_mode() { as_user "$1" stat -c %a "$2" 2>/dev/null || as_user "$1" stat -f %Lp "$2"; }

# Pass 1: plan. Fill the parallel arrays; print diffs; count differing files.
plan_user=(); plan_src=(); plan_dest=(); plan_action=()
differ=0
difftmp="$(mktemp)"
trap 'rm -f "$difftmp"' EXIT
for u in "${users[@]}"; do
  src_dir="$HERE/hooks/$u"
  hooks_dir="$ROOT/$u/actions-runner/hooks"
  if [ ! -d "$src_dir" ]; then echo "install.sh: no repo hooks for user '$u' ($src_dir)" >&2; exit 2; fi
  if ! as_user "$u" test -d "$hooks_dir"; then
    echo "install.sh: $hooks_dir does not exist (is the runner installed for $u?)" >&2; exit 2
  fi
  for src in "$src_dir"/*.sh; do
    f="$(basename "$src")"
    dest="$hooks_dir/$f"
    if ! as_user "$u" test -e "$dest"; then
      action=install
    elif diff -u <(as_user "$u" cat "$dest") "$src" >"$difftmp" 2>&1; then
      action=same
    else
      action=differs
      differ=$((differ + 1))
      echo "--- $u/$f: live differs from repo (diff live -> repo):"
      cat "$difftmp"
    fi
    plan_user+=("$u"); plan_src+=("$src"); plan_dest+=("$dest"); plan_action+=("$action")
  done
done

if [ "$differ" -gt 0 ] && [ "$force" -ne 1 ]; then
  echo "install.sh: $differ live file(s) differ from the repo copy; nothing written. Re-run with --force to overwrite (a backup is made first)." >&2
  exit 1
fi

# Pass 2: apply.
ts="$(date +%Y-%m-%d-%H%M%S)"
for i in "${!plan_user[@]}"; do
  u="${plan_user[$i]}"; src="${plan_src[$i]}"; dest="${plan_dest[$i]}"; action="${plan_action[$i]}"
  f="$(basename "$src")"
  case "$action" in
    same) echo "unchanged: $u/$f" ;;
    install|differs)
      if [ "$action" = differs ]; then
        mode="$(live_mode "$u" "$dest")"
        as_user "$u" cp -p "$dest" "$dest.bak-$ts"
        echo "backup: $dest.bak-$ts"
      else
        mode="$(default_mode "$f")"
      fi
      tmp="$dest.new.$$"
      as_user "$u" sh -c 'cat > "$1"' _ "$tmp" < "$src"
      as_user "$u" chmod "$mode" "$tmp"
      as_user "$u" mv -f "$tmp" "$dest"
      echo "installed ($mode): $u/$f"
      ;;
  esac
done
