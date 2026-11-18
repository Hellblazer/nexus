#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# The weekly sandbox run for scripts/claude-restart-sessions (Sam, 2026-11-18).
#
# Why: claude-restart-sessions keys its sandbox pass on the script, the harness, tmux and the
# Claude Code version. Claude Code updates every couple of days, so the pass went stale that
# often and every restart was refused until someone ran the sandbox by hand. The restart now
# only warns on another Claude Code version; this job keeps the pass current so the warning
# stays rare, and its last result is named in that warning.
#
# It needs what tests/e2e/restart-sessions/run.sh needs: a native Claude Code install, tmux, the
# operator's nexus config and running service, and the harness's automation token in the
# login keychain (item nexus-automation-oauth-token; `claude setup-token` makes one). No CI
# runner has those, and the pass record is local state keyed to this machine's Claude Code and
# tmux, so it runs as a launchd job on the laptop, not as a CI schedule.
#
# Usage:
#   scripts/restart-sandbox-weekly.sh               run the sandbox now (what the job runs)
#   scripts/restart-sandbox-weekly.sh --install     write the launchd job and load it
#   scripts/restart-sandbox-weekly.sh --uninstall   unload the job and remove its plist
#   scripts/restart-sandbox-weekly.sh --print-plist print the job's plist and exit
# After --install, `launchctl kickstart gui/$(id -u)/com.nexus.restart-sandbox-weekly` runs it
# once under launchd, which shows whether the keychain token and the volume are reachable there.
#
# The job runs Sundays at 03:30 local time (at the next wake if the laptop is asleep) from the
# primary checkout (NX_REPO, default ~/git/nexus), the copy ~/.local/bin/claude-restart-sessions
# links to, so the pass it records is for the script restarts actually run. Every invocation
# appends to ~/.local/state/claude-restart-sessions/sandbox-weekly.log and records its outcome
# beside it: a run in sandbox-weekly-last.json, status (passed, failed, timeout, not-recorded or
# not-run), the reason, the start and end, the exit code, the Claude Code version, and whether a
# new pass was recorded (sandbox-pass.json's passed_at changed; the harness can exit 0 without
# recording one, for instance when Claude Code updates mid-run); a skip (lock held, restart in
# flight) in sandbox-weekly-skip.json only, so it never hides the last real result. A failure
# also goes to stderr, which launchd keeps
# in sandbox-weekly.launchd.err. A failed run leaves the previous pass in place.
#
# A run takes about 17 minutes. It starts real Claude Code sessions (the harness's default model,
# sonnet) in a private tmux server and a scratch HOME. Nothing is written to the operator's
# ~/.claude (the harness fails its own run otherwise), but the sessions do use the operator's
# nexus service (mail, T1 leases) as the operator. The run is killed after NX_SANDBOX_LIMIT_S
# (default 5400 s): TERM to the harness, which cleans up, then KILL to its process group 120 s
# later. A run while another holds the lock exits 0 at once; so does a run while a real
# restart's log was written in the last 30 minutes. TMPDIR is NX_SANDBOX_TMPDIR, default
# /Volumes/SanHell/tmp/nx.noindex (where every nexus test run on Sam's Mac keeps its temp); the
# run waits up to NX_SANDBOX_VOLUME_WAIT_S (default 300 s) for it, then fails loudly instead of
# falling back to the internal disk.
set -uo pipefail

repo="${NX_REPO:-$HOME/git/nexus}"
state="$HOME/.local/state/claude-restart-sessions"
label="com.nexus.restart-sandbox-weekly"
agents="${NX_LAUNCH_AGENTS:-$HOME/Library/LaunchAgents}"
plist="$agents/$label.plist"
self="$repo/scripts/restart-sandbox-weekly.sh"
restart_log="$HOME/.claude/restart-sessions.log"
pass_record="$state/sandbox-pass.json"

print_plist() {
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$self</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>NX_REPO</key>
    <string>$repo</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key><integer>0</integer>
    <key>Hour</key><integer>3</integer>
    <key>Minute</key><integer>30</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>$state/sandbox-weekly.launchd.out</string>
  <key>StandardErrorPath</key>
  <string>$state/sandbox-weekly.launchd.err</string>
  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
EOF
}

case "${1:-}" in
  --print-plist)
    print_plist
    exit 0
    ;;
  --install)
    [ -f "$self" ] || { echo "restart-sandbox-weekly: $self not found; set NX_REPO to the primary checkout" >&2; exit 2; }
    mkdir -p "$agents" "$state" || exit 2
    print_plist > "$plist.tmp" && mv "$plist.tmp" "$plist" || exit 2
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null
    # A bootstrap straight after a bootout can fail while launchd is still tearing the old job
    # down (I/O error 5), so it is tried again once.
    if ! launchctl bootstrap "gui/$(id -u)" "$plist"; then
      sleep 2
      if ! launchctl bootstrap "gui/$(id -u)" "$plist"; then
        echo "restart-sandbox-weekly: launchctl bootstrap failed for $plist" >&2
        exit 1
      fi
    fi
    echo "installed $plist (Sundays 03:30); runs $self"
    echo "run it once under launchd now: launchctl kickstart gui/$(id -u)/$label"
    exit 0
    ;;
  --uninstall)
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null
    rm -f "$plist"
    echo "removed $plist"
    exit 0
    ;;
  "") ;;
  *)
    sed -n '4,/^set -uo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac

mkdir -p "$state" || exit 2
log="$state/sandbox-weekly.log"
last="$state/sandbox-weekly-last.json"
# launchd starts jobs with a bare PATH; claude, nx, uv and tmux live in these.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
limit="${NX_SANDBOX_LIMIT_S:-5400}"
started=""
stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }
note() { printf '%s %s\n' "$(stamp)" "$*" >> "$log"; }
# Record the outcome of this invocation: status, exit code, reason, whether a pass was recorded.
# A skip goes to sandbox-weekly-skip.json, never over the last real run's record, so the
# restart's warning keeps naming that run's result. Each record carries the Claude Code version
# it ran under, which the warning can compare with the one running at restart.
record() {
  local status="$1" code="$2" reason="$3" recorded="${4:-false}" out="$last"
  [ "$status" = skipped ] && out="$state/sandbox-weekly-skip.json"
  reason="${reason//\\/\\\\}"; reason="${reason//\"/\\\"}"
  printf '{"status": "%s", "exit": %d, "reason": "%s", "recorded": %s, "claude_version": "%s", "started": "%s", "ended": "%s"}\n' \
    "$status" "$code" "$reason" "$recorded" "$claude_at_start" "${started:-$(stamp)}" "$(stamp)" > "$out.tmp" && mv "$out.tmp" "$out"
}
fail() {  # fail CODE STATUS REASON: a failure goes to the log, the last-run record and stderr
  note "FAIL: $3"
  record "$2" "$1" "$3"
  echo "restart-sandbox-weekly: $3" >&2
  exit "$1"
}
passed_at() { sed -n 's/.*"passed_at": *"\([^"]*\)".*/\1/p' "$pass_record" 2>/dev/null; }

started="$(stamp)"
# The version the run is tested on, read once at the start: a Claude Code update during the
# run must not be recorded as tested (the harness refuses to record a pass in that case too).
claude_at_start="$(claude --version 2>/dev/null | awk 'NR==1 {print $1}')"
# Timeout markers left by a wrapper that was killed outright (its pid is in the name).
for marker in "$state"/sandbox-weekly.timed-out.*; do
  [ -e "$marker" ] || continue
  kill -0 "${marker##*.}" 2>/dev/null || rm -f "$marker"
done
tmp="${NX_SANDBOX_TMPDIR:-/Volumes/SanHell/tmp/nx.noindex}"
waited=0
while [ ! -d "$tmp" ] && [ "$waited" -lt "${NX_SANDBOX_VOLUME_WAIT_S:-300}" ]; do
  sleep 5
  waited=$((waited + 5))
done
[ -d "$tmp" ] || fail 2 not-run "TMPDIR $tmp is missing (is the volume mounted?); not run"
export TMPDIR="$tmp"

# The lock is a symlink whose target is the holder's pid, made by one atomic ln -s, so it never
# exists without its pid. A holder counts as live only while that pid runs this script; a lock
# older than the run limit plus 15 minutes is stale whoever holds it (a hung or recycled pid).
lock="$state/sandbox-weekly.lock"
holder_live() {  # holder_live PID
  local cmd
  cmd="$(ps -p "$1" -o command= 2>/dev/null)" || return 1
  case "$cmd" in *restart-sandbox-weekly*) return 0 ;; *) return 1 ;; esac
}
lock_age() {  # seconds since the lock was made
  echo $(( $(date +%s) - $(stat -f %m "$lock" 2>/dev/null || stat -c %Y "$lock" 2>/dev/null || date +%s) ))
}
if ! ln -s "$$" "$lock" 2>/dev/null; then
  holder="$(readlink "$lock" 2>/dev/null)"
  if [ -n "$holder" ] && holder_live "$holder" && [ "$(lock_age)" -le $((limit + 900)) ]; then
    note "SKIP: another weekly run (pid $holder) holds $lock"
    record skipped 0 "another weekly run (pid $holder) holds the lock"
    exit 0
  fi
  # Take a stale lock over by renaming it to a name only this process uses (one rename wins),
  # then check it is the lock judged stale; if a peer took it over first, put that one back.
  aside="$lock.stale.$$"
  if ! mv "$lock" "$aside" 2>/dev/null || [ "$(readlink "$aside")" != "$holder" ]; then
    [ -L "$aside" ] && { [ -e "$lock" ] || [ -L "$lock" ] || mv "$aside" "$lock"; }
    rm -f "$aside"
    note "SKIP: lost the takeover of $lock to another run"
    record skipped 0 "lost the lock takeover to another run"
    exit 0
  fi
  rm -f "$aside"
  if [ -n "$holder" ] && holder_live "$holder"; then
    note "lock held for $(lock_age) s by pid $holder, past the limit; stopping it"
    kill -TERM -- "-$holder" 2>/dev/null || kill -TERM "$holder" 2>/dev/null
  fi
  note "lock $lock held by a stale holder (${holder:-none}); taking it over"
  if ! ln -s "$$" "$lock" 2>/dev/null; then
    note "SKIP: lost the race for $lock"
    record skipped 0 "lost the race for the lock"
    exit 0
  fi
fi

child=""
watchdog=""
cleanup() {
  [ -n "$watchdog" ] && kill "$watchdog" 2>/dev/null
  [ "$(readlink "$lock" 2>/dev/null)" = "$$" ] && rm -f "$lock"
}
trap cleanup EXIT
# A TERM, INT or HUP to this wrapper is passed to the harness, which cleans up its sessions,
# and the wrapper waits for it; the lock is released only once the harness has gone.
forward() {
  note "signal received; stopping the harness (pid $child)"
  [ -n "$child" ] && kill -TERM "$child" 2>/dev/null
  [ -n "$child" ] && wait "$child" 2>/dev/null
  record failed 143 "stopped by a signal before the run finished"
  exit 143
}
trap forward TERM INT HUP

# A real restart in the last 30 minutes may still be settling; the sandbox's sessions would
# compete with it for the operator's service. Next week's run (or a hand run) picks it up.
if [ -n "$(find "$restart_log" -mmin -30 2>/dev/null)" ]; then
  note "SKIP: $restart_log was written in the last 30 minutes (a restart may be in flight)"
  record skipped 0 "a restart's log was written in the last 30 minutes"
  exit 0
fi

before="$(passed_at)"
note "START: $repo/tests/e2e/restart-sessions/run.sh (TMPDIR $TMPDIR, limit ${limit} s)"
set -m  # the harness gets its own process group, so the watchdog can stop all of it
"$repo/tests/e2e/restart-sessions/run.sh" >> "$log" 2>&1 &
child=$!
set +m
timed_out="$state/sandbox-weekly.timed-out.$$"
# The watchdog keeps no output descriptor open (its sleep would otherwise hold a caller's pipe
# open after the wrapper ends) and kills its own sleep when it is stopped.
(
  exec >/dev/null 2>&1
  nap() { sleep "$1" & s=$!; wait "$s"; }
  trap 'kill "$s" 2>/dev/null; exit 0' TERM
  nap "$limit"
  touch "$timed_out"
  kill -TERM "$child" 2>/dev/null
  nap 120
  kill -KILL -- "-$child" 2>/dev/null
) &
watchdog=$!
wait "$child"
code=$?
kill "$watchdog" 2>/dev/null
watchdog=""
note "END: exit $code"
after="$(passed_at)"
if [ -e "$timed_out" ]; then
  rm -f "$timed_out"
  fail 124 timeout "the harness ran past ${limit} s and was stopped"
fi
if [ "$code" -ne 0 ]; then
  fail "$code" failed "the harness exited $code; see $log"
fi
if [ -z "$after" ] || [ "$after" = "$before" ]; then
  fail 3 not-recorded "the harness exited 0 but recorded no new pass (see 'pass NOT recorded' in $log)"
fi
record passed 0 "recorded a new pass at $after" true
exit 0
