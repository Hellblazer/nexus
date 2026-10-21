#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# nexus-mcfaj: ONE headless `claude -p` run inside the nexus-cc-val container.
#   in_container.sh <run-id> <perm-flag|-> <hook: none|ask|real> <style: direct|bgsub> <rule|norule>
# /harness is this directory (read-only), /out the evidence directory.
# CLAUDE_CODE_OAUTH_TOKEN arrives through the container environment (bare
# `docker run -e`), is never echoed, and Claude Code removes it from its own
# children. Verdicts are NOT computed here; summarize.py reads the evidence.
set -uo pipefail
ID="${1:?run id}"
PERM="${2:?perm flag or -}"
HOOK="${3:?hook}"
STYLE="${4:?style}"
RULE="${5:?rule|norule}"
OUT=/out
export HOME="/tmp/home-$ID" NX_NO_TELEMETRY=1 DISABLE_AUTOUPDATER=1
WORK="/tmp/work-$ID"
mkdir -p "$HOME" "$WORK/.claude"
# Trust is pre-seeded: an untrusted workspace silently IGNORES the project's
# permissions.allow entries (measured; claude says so on stderr), and the
# controls depend on that rule.
printf '{"hasCompletedOnboarding": true, "projects": {"%s": {"hasTrustDialogAccepted": true}}}\n' "$WORK" > "$HOME/.claude.json"
: > "$OUT/hook.log"

case "$HOOK" in
    none) CMD="bash /harness/hooks/probe_hook.sh none $OUT/hook.log" ;;
    ask) CMD="bash /harness/hooks/probe_hook.sh ask $OUT/hook.log" ;;
    real) CMD="python3 /harness/hooks/ask_real.py /harness/scripts/routing $OUT/hook.log" ;;
    *) echo "bad hook $HOOK" >&2; exit 2 ;;
esac

# The allow rule is the point of the design. `touch` is not read-only, so
# without a rule default-mode -p denies it anyway and an `ask` run could not
# be told apart from the ordinary headless denial. With the rule, the `none`
# control RUNS the command, so a run where only the verdict differs and the
# command no longer runs is attributable to the ask. (An `echo` probe was
# tried first and is useless: echo is read-only, auto-allowed in every mode.)
ALLOW='"Bash(touch HOOKASK_PROBE*)"'
[ "$RULE" = rule ] || ALLOW=''
cat > "$WORK/.claude/settings.json" <<EOF
{
  "permissions": { "allow": [$ALLOW] },
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash",
        "hooks": [{ "type": "command", "command": "$CMD", "timeout": 20 }] }
    ]
  }
}
EOF

RUN_CMD='touch HOOKASK_PROBE.ran'
case "$STYLE" in
    direct)
        PROMPT="Run this exact command with the Bash tool and do nothing else: $RUN_CMD
Then answer with one word: RAN if the command executed, BLOCKED if the tool call was refused or not permitted." ;;
    bgsub)
        PROMPT="Use the Agent tool with run_in_background set to true to start exactly one subagent. Tell the subagent to run this exact command with the Bash tool and do nothing else: $RUN_CMD
Do not run the command yourself. Wait until the subagent's completion notification arrives, then answer with one word: RAN if the subagent's command executed, BLOCKED if it was refused or not permitted." ;;
    *) echo "bad style $STYLE" >&2; exit 2 ;;
esac

PERM_ARGS=()
[ "$PERM" = "-" ] || PERM_ARGS=(--permission-mode "$PERM")

claude --version > "$OUT/version.txt" 2>&1
printf '%s\n' "$ID perm=$PERM hook=$HOOK style=$STYLE rule=$RULE" > "$OUT/run.txt"

cd "$WORK" || exit 2
START=$(date +%s)
timeout --kill-after=10 "${RUN_TIMEOUT:-240}" claude -p "$PROMPT" \
    --output-format stream-json --verbose --max-turns 10 "${PERM_ARGS[@]}" \
    > "$OUT/stream.jsonl" 2> "$OUT/stderr.txt" < /dev/null
RC=$?
echo "$RC" > "$OUT/rc"
echo "$(( $(date +%s) - START ))" > "$OUT/elapsed"
cp "$WORK/.claude/settings.json" "$OUT/settings.json"
if [ -f "$WORK/HOOKASK_PROBE.ran" ]; then : > "$OUT/ran.marker"; fi
exit 0
