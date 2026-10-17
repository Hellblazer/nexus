#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# infra/hellmini/install-watchdog.sh [--force]   (nexus-ey1me)
#
# Install the runner watchdog under infra/hellmini/watchdog/ on hellmini:
#   actions-runner-watchdog             -> /usr/local/libexec/      (root:wheel 755)
#   local.actions-runner-watchdog.plist -> /Library/LaunchDaemons/  (root:wheel 644)
# Run it ON hellmini; it needs passwordless sudo.
#
# Same semantics as install.sh, per file:
#   - live file absent:        install it.
#   - live file identical:     nothing to do (but a wrong mode or owner counts as drift).
#   - live file differs:       print the diff and REFUSE unless --force. With
#                              --force, first copy the live file to
#                              <file>.bak-<timestamp>, then overwrite it.
# Every file is checked before any is written, so a refusal changes nothing.
# With no --force this is also the drift check: exit 0 means live == repo.
#
# After the plist is installed or changed, the daemon is reloaded:
# `launchctl bootout system/local.actions-runner-watchdog` (not-loaded is
# ignored), then `launchctl bootstrap system <plist>`. A script-only change
# needs no reload: launchd starts the script fresh every 60 s.
#
# Exit: 0 ok, 1 live differs (no --force), 2 usage or a missing directory,
#       3 launchctl bootstrap failed.
#
# Dry run off the host: HELLMINI_WD_ROOT is a prefix put in front of the two
# absolute destination paths (launchctl is then skipped unless HELLMINI_LAUNCHCTL
# names a stand-in); HELLMINI_RUN_AS, when set, is called as
# "$HELLMINI_RUN_AS root <cmd...>" in place of "sudo -n <cmd...>";
# HELLMINI_WD_OWNER (default root:wheel) is the owner:group to apply and expect;
# HELLMINI_RETRY_SLEEP (default 1) is the pause between bootstrap retries.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$HERE/watchdog"
PREFIX="${HELLMINI_WD_ROOT:-}"
OWNER="${HELLMINI_WD_OWNER:-root:wheel}"
LABEL="local.actions-runner-watchdog"
SCRIPT_NAME="actions-runner-watchdog"
PLIST_NAME="$LABEL.plist"
SCRIPT_DEST="$PREFIX/usr/local/libexec/$SCRIPT_NAME"
PLIST_DEST="$PREFIX/Library/LaunchDaemons/$PLIST_NAME"

if [ -n "${HELLMINI_LAUNCHCTL:-}" ]; then LAUNCHCTL="$HELLMINI_LAUNCHCTL"
elif [ -z "$PREFIX" ]; then LAUNCHCTL=launchctl
else LAUNCHCTL=""; fi

force=0
for arg in "$@"; do
  case "$arg" in
    --force) force=1 ;;
    -h|--help) sed -n '2,33p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "install-watchdog.sh: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

as_root() {
  if [ -n "${HELLMINI_RUN_AS:-}" ]; then "$HELLMINI_RUN_AS" root "$@"; else sudo -n "$@"; fi
}

# "<octal mode> <owner>:<group>" of a live file; GNU stat first, BSD stat second.
live_meta() {
  as_root stat -c '%a %U:%G' "$1" 2>/dev/null || as_root stat -f '%Lp %Su:%Sg' "$1"
}

names=("$SCRIPT_NAME" "$PLIST_NAME")
dests=("$SCRIPT_DEST" "$PLIST_DEST")
modes=(755 644)
actions=()

# Pass 1: plan. Print diffs; count files that drift.
differ=0
difftmp="$(mktemp)"
trap 'rm -f "$difftmp"' EXIT
for i in "${!names[@]}"; do
  n="${names[$i]}"; src="$SRC_DIR/$n"; dest="${dests[$i]}"
  if [ ! -f "$src" ]; then echo "install-watchdog.sh: missing repo file $src" >&2; exit 2; fi
  # /usr/local/libexec may be absent on a fresh host and is created below;
  # /Library/LaunchDaemons is part of macOS, so its absence is an error.
  if [ "$n" = "$PLIST_NAME" ] && ! as_root test -d "$(dirname "$dest")"; then
    echo "install-watchdog.sh: $(dirname "$dest") does not exist" >&2; exit 2
  fi
  if ! as_root test -e "$dest"; then
    actions+=(install)
  elif ! diff -u <(as_root cat "$dest") "$src" >"$difftmp" 2>&1; then
    actions+=(differs)
    differ=$((differ + 1))
    echo "--- $n: live differs from repo (diff live -> repo):"
    cat "$difftmp"
  elif [ "$(live_meta "$dest")" != "${modes[$i]} $OWNER" ]; then
    actions+=(meta)
    differ=$((differ + 1))
    echo "--- $n: content matches but live is '$(live_meta "$dest")', want '${modes[$i]} $OWNER'"
  else
    actions+=(same)
  fi
done

if [ "$differ" -gt 0 ] && [ "$force" -ne 1 ]; then
  echo "install-watchdog.sh: $differ live file(s) differ from the repo copy; nothing written. Re-run with --force to overwrite (a backup is made first)." >&2
  exit 1
fi

# Pass 2: apply.
ts="$(date +%Y-%m-%d-%H%M%S)"
reload=0
for i in "${!names[@]}"; do
  n="${names[$i]}"; src="$SRC_DIR/$n"; dest="${dests[$i]}"; mode="${modes[$i]}"
  case "${actions[$i]}" in
    same) echo "unchanged: $n" ;;
    meta)
      as_root chmod "$mode" "$dest"
      as_root chown "$OWNER" "$dest"
      echo "fixed mode/owner ($mode $OWNER): $n"
      ;;
    install|differs)
      if [ "${actions[$i]}" = differs ]; then
        as_root cp -p "$dest" "$dest.bak-$ts"
        echo "backup: $dest.bak-$ts"
      fi
      as_root mkdir -p "$(dirname "$dest")"
      tmp="$dest.new.$$"
      as_root sh -c 'cat > "$1"' _ "$tmp" < "$src"
      as_root chmod "$mode" "$tmp"
      as_root chown "$OWNER" "$tmp"
      as_root mv -f "$tmp" "$dest"
      echo "installed ($mode $OWNER): $n"
      if [ "$n" = "$PLIST_NAME" ]; then reload=1; fi
      ;;
  esac
done

if [ "$reload" -eq 1 ]; then
  if [ -z "$LAUNCHCTL" ]; then
    echo "launchctl skipped (HELLMINI_WD_ROOT set, no HELLMINI_LAUNCHCTL)"
  else
    as_root "$LAUNCHCTL" bootout "system/$LABEL" >/dev/null 2>&1 || true
    # bootstrap can race the teardown that bootout starts; retry briefly.
    ok=0
    for _ in 1 2 3 4 5; do
      if as_root "$LAUNCHCTL" bootstrap system "$PLIST_DEST"; then ok=1; break; fi
      sleep "${HELLMINI_RETRY_SLEEP:-1}"
    done
    if [ "$ok" -ne 1 ]; then
      echo "install-watchdog.sh: launchctl bootstrap system $PLIST_DEST failed" >&2
      exit 3
    fi
    echo "loaded: system/$LABEL"
  fi
fi
