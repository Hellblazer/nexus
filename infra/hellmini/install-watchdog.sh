#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# infra/hellmini/install-watchdog.sh [--force]   (nexus-ey1me)
#
# Install the runner watchdog under infra/hellmini/watchdog/ on hellmini:
#   actions-runner-watchdog             -> /usr/local/libexec/      (root:wheel 755)
#   local.actions-runner-watchdog.plist -> /Library/LaunchDaemons/  (root:wheel 644)
#   (state and log directory)           -> /var/db/actions-runner-watchdog
#                                          (root:wheel 700; the script keeps its
#                                          state and log here, off the runner
#                                          volume whose parent the CI user owns)
# Run it ON hellmini; it needs passwordless sudo. It refuses to run unless
# /Volumes/Bulk/ghrunner exists (or HELLMINI_WD_ROOT is set), so a stray run on
# another Mac installs nothing.
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
# The state directory is checked like a file: absent is created, and a wrong
# mode or owner counts as drift (--force repairs it). A symlink directly inside
# it also counts as drift (the watchdog never makes one; --force removes it, and
# never follows it).
#
# After the plist is installed or changed, the daemon is reloaded:
# `launchctl bootout system/local.actions-runner-watchdog` (not-loaded is
# ignored), `launchctl enable system/...` (a daemon someone disabled with
# `launchctl disable` will not bootstrap otherwise), then
# `launchctl bootstrap system <plist>`. Otherwise, if anything was written (or
# --force was given) and the daemon is not loaded, it is enabled and
# bootstrapped. A script-only change to a loaded daemon needs no reload: launchd
# starts the script fresh every 60 s.
#
# Exit: 0 ok, 1 live differs (no --force), 2 usage, a missing directory or not
#       hellmini, 3 launchctl bootstrap failed.
#
# Dry run off the host: HELLMINI_WD_ROOT (must be non-empty if set) is a prefix
# put in front of the absolute destination paths (launchctl is then skipped
# unless HELLMINI_LAUNCHCTL names a stand-in); HELLMINI_WD_HOSTMARK (default
# /Volumes/Bulk/ghrunner) is the directory whose presence marks hellmini;
# HELLMINI_RUN_AS, when set, is called as
# "$HELLMINI_RUN_AS root <cmd...>" in place of "sudo -n <cmd...>";
# HELLMINI_WD_OWNER (default root:wheel) is the owner:group to apply and expect;
# HELLMINI_RETRY_SLEEP (default 1) is the pause between bootstrap retries.

set -euo pipefail

case "${1:-}" in -h|--help) sed -n '2,49p' "${BASH_SOURCE[0]}"; exit 0 ;; esac

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$HERE/watchdog"
HOSTMARK="${HELLMINI_WD_HOSTMARK:-/Volumes/Bulk/ghrunner}"
# Default-safe: with no prefix this installs into the real /usr/local, /Library
# and /var/db, so it must be hellmini. An EMPTY prefix is refused too, since it
# would aim a launchctl stand-in (or the real thing) at real paths.
if [ "${HELLMINI_WD_ROOT+set}" = set ]; then
  if [ -z "$HELLMINI_WD_ROOT" ]; then
    echo "install-watchdog.sh: HELLMINI_WD_ROOT is set but empty; unset it or give a directory" >&2
    exit 2
  fi
  PREFIX="$HELLMINI_WD_ROOT"
else
  if [ ! -d "$HOSTMARK" ]; then
    echo "install-watchdog.sh: $HOSTMARK does not exist, so this is not hellmini; refusing (set HELLMINI_WD_ROOT for a prefixed dry run)" >&2
    exit 2
  fi
  PREFIX=""
fi
OWNER="${HELLMINI_WD_OWNER:-root:wheel}"
LABEL="local.actions-runner-watchdog"
SCRIPT_NAME="actions-runner-watchdog"
PLIST_NAME="$LABEL.plist"
SCRIPT_DEST="$PREFIX/usr/local/libexec/$SCRIPT_NAME"
PLIST_DEST="$PREFIX/Library/LaunchDaemons/$PLIST_NAME"
# Must equal the default STATE_DIR in watchdog/actions-runner-watchdog (a test pins it).
STATE_DEST="$PREFIX/var/db/actions-runner-watchdog"
STATE_MODE=700

if [ -n "${HELLMINI_LAUNCHCTL:-}" ]; then LAUNCHCTL="$HELLMINI_LAUNCHCTL"
elif [ -z "$PREFIX" ]; then LAUNCHCTL=launchctl
else LAUNCHCTL=""; fi

force=0
for arg in "$@"; do
  case "$arg" in
    --force) force=1 ;;
    -h|--help) sed -n '2,49p' "${BASH_SOURCE[0]}"; exit 0 ;;
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

# The state directory: created if absent, drift if its mode or owner is wrong.
# It is root-only on purpose (see actions-runner-watchdog); a symlink or a
# non-directory in its place is refused outright.
if as_root test -L "$STATE_DEST" || { as_root test -e "$STATE_DEST" && ! as_root test -d "$STATE_DEST"; }; then
  echo "install-watchdog.sh: $STATE_DEST exists but is not a plain directory; refusing" >&2; exit 2
fi
if ! as_root test -e "$STATE_DEST"; then
  state_action=install
elif [ "$(live_meta "$STATE_DEST")" != "$STATE_MODE $OWNER" ]; then
  state_action=meta
  differ=$((differ + 1))
  echo "--- state dir: live is '$(live_meta "$STATE_DEST")', want '$STATE_MODE $OWNER'"
else
  state_action=same
fi
state_links=""
if as_root test -d "$STATE_DEST"; then
  state_links="$(as_root find "$STATE_DEST" -maxdepth 1 -type l)"
  if [ -n "$state_links" ]; then
    differ=$((differ + 1))
    echo "--- state dir: contains symlink(s), which the watchdog never creates:"
    echo "$state_links"
  fi
fi

if [ "$differ" -gt 0 ] && [ "$force" -ne 1 ]; then
  echo "install-watchdog.sh: $differ live item(s) differ from the repo copy; nothing written. Re-run with --force to overwrite (a backup is made first)." >&2
  exit 1
fi

# Pass 2: apply.
ts="$(date +%Y-%m-%d-%H%M%S)"
reload=0
changed=0
case "$state_action" in
  same) echo "unchanged: state dir" ;;
  install)
    # shellcheck disable=SC2016 # $1 is expanded by the inner sh, on purpose
    as_root sh -c 'umask 077; mkdir -p "$1"' _ "$STATE_DEST"
    as_root chmod "$STATE_MODE" "$STATE_DEST"
    as_root chown "$OWNER" "$STATE_DEST"
    echo "created ($STATE_MODE $OWNER): state dir $STATE_DEST"
    changed=1
    ;;
  meta)
    as_root chmod "$STATE_MODE" "$STATE_DEST"
    as_root chown "$OWNER" "$STATE_DEST"
    echo "fixed mode/owner ($STATE_MODE $OWNER): state dir"
    changed=1
    ;;
esac
if [ -n "$state_links" ]; then
  # -type l matches the link itself; rm removes it without following it.
  as_root find "$STATE_DEST" -maxdepth 1 -type l -exec rm -f {} +
  echo "removed symlink(s) from the state dir"
  changed=1
fi
for i in "${!names[@]}"; do
  n="${names[$i]}"; src="$SRC_DIR/$n"; dest="${dests[$i]}"; mode="${modes[$i]}"
  [ "${actions[$i]}" = same ] || changed=1
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
      # shellcheck disable=SC2016 # $1 is expanded by the inner sh, on purpose
      as_root sh -c 'cat > "$1"' _ "$tmp" < "$src"
      as_root chmod "$mode" "$tmp"
      as_root chown "$OWNER" "$tmp"
      as_root mv -f "$tmp" "$dest"
      echo "installed ($mode $OWNER): $n"
      if [ "$n" = "$PLIST_NAME" ]; then reload=1; fi
      ;;
  esac
done

# Load the daemon when the plist was just installed, or when something was
# written (or --force given) and it is not loaded: a plist that is unchanged but
# whose daemon is disabled or booted out still needs to come up.
need_load=0
if [ "$reload" -eq 1 ]; then
  need_load=1
elif [ -n "$LAUNCHCTL" ] && ! as_root "$LAUNCHCTL" print "system/$LABEL" >/dev/null 2>&1; then
  if [ "$changed" -eq 1 ] || [ "$force" -eq 1 ]; then
    need_load=1
  else
    echo "note: system/$LABEL is not loaded; re-run with --force to enable and load it" >&2
  fi
fi

if [ "$need_load" -eq 1 ]; then
  if [ -z "$LAUNCHCTL" ]; then
    echo "launchctl skipped (HELLMINI_WD_ROOT set, no HELLMINI_LAUNCHCTL)"
  else
    as_root "$LAUNCHCTL" bootout "system/$LABEL" >/dev/null 2>&1 || true
    # A daemon disabled with `launchctl disable` refuses to bootstrap.
    as_root "$LAUNCHCTL" enable "system/$LABEL" || echo "install-watchdog.sh: launchctl enable system/$LABEL failed (continuing)" >&2
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
