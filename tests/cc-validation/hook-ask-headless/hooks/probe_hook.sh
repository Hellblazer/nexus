#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Minimal PreToolUse probe for the nexus-mcfaj headless-ask measurement.
#   probe_hook.sh <ask|none> <log-file>
# Acts only on a Bash call whose payload carries HOOKASK_PROBE; any other
# call passes through silently. Logs one line per probe call (the log path
# is an argument because a PreToolUse command hook does not inherit the
# harness environment). `ask` emits permissionDecision "ask"; `none` emits
# NOTHING, which is the control: same hook, same log line, no decision.
VERDICT="${1:?verdict}"
LOG="${2:?log}"
INPUT="$(cat)"
case "$INPUT" in *HOOKASK_PROBE*) ;; *) exit 0 ;; esac
SUB=main
case "$INPUT" in *'"agent_id"'*) SUB=subagent ;; esac
printf '%s %s fired caller=%s\n' "$(date +%s)" "$VERDICT" "$SUB" >> "$LOG"
[ "$VERDICT" = ask ] || exit 0
printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"hook-ask-headless probe (nexus-mcfaj)"}}'
