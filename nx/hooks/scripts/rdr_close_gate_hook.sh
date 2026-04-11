#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# PreToolUse hook: gate RDR frontmatter closes to the /nx:rdr-close slash command.
#
# Blocks Edit/Write tool calls that set `status: closed` on `docs/rdr/rdr-*.md`
# (or equivalent RDR directory from `.nexus.yml`) unless a matching
# `rdr-close-active-<id>` T1 scratch marker is present. The marker is written
# ONLY by the `/nx:rdr-close` command preamble at the successful end of Pass 2
# of the Problem Statement Replay (see `nx/commands/rdr-close.md`). The slash
# command is the sole path to producing the marker.
#
# Without the marker, an Edit/Write that flips frontmatter `status: closed`
# is a manual-walkthrough attempt from an assistant context that silently
# skips:
#
# - Step 1.5 Two-Pass Problem Statement Replay preamble (file:line pointer
#   validation against every enumerated Gap — the structural gate)
# - Step 1.75 fresh critic dispatch against the close-time state (NOT a
#   reused verdict from an earlier phase — a fresh critic read)
# - Step 3 user-confirmation bead gate
#
# These are the load-bearing checks the rdr-close skill exists to perform.
# Bypassing them is a silent scope reduction of the gate exercise — exactly
# the failure mode RDR-066 and RDR-069 were designed to prevent, applied to
# their own close flow.
#
# Historical context: this hook was shipped after two instances of the
# failure mode were caught in a single session (RDR-069 Phase 4c, then
# RDR-066 Phase 5c, both auto-closed via manual walkthrough in an assistant
# context). A memory-based fix would only have helped the single assistant
# instance; this hook is structural enforcement at the tool-use boundary and
# applies to any user of the plugin. See
# `nexus_rdr/066-validation-5c-real-self-close` for the rollback record.
#
# No set -e/-u/-o pipefail — this hook must NEVER fail with a shell error.
# Every code path produces valid JSON on stdout and exits 0.

# ---------------------------------------------------------------------------
# Helpers — PreToolUse hooks use hookSpecificOutput, NOT decision/reason
# ---------------------------------------------------------------------------

allow() {
    printf '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}\n'
    exit 0
}

deny() {
    local reason="$1"
    local reason_json
    reason_json=$(printf '%s' "$reason" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" 2>/dev/null || printf '"%s"' "$reason")
    printf '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "reason": %s}}\n' "$reason_json"
    exit 0
}

# ---------------------------------------------------------------------------
# Read stdin
# ---------------------------------------------------------------------------

STDIN=$(cat 2>/dev/null || true)
if [[ -z "$STDIN" ]]; then
    allow
fi

# ---------------------------------------------------------------------------
# Fast no-op: check tool_name (Edit or Write only)
# ---------------------------------------------------------------------------

TOOL_NAME=$(printf '%s' "$STDIN" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('tool_name', ''))
except Exception:
    print('')
" 2>/dev/null || true)

if [[ "$TOOL_NAME" != "Edit" && "$TOOL_NAME" != "Write" ]]; then
    allow
fi

# ---------------------------------------------------------------------------
# Extract file_path + new content (the string that will be written)
# ---------------------------------------------------------------------------

FILE_PATH=$(printf '%s' "$STDIN" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('tool_input', {}).get('file_path', ''))
except Exception:
    print('')
" 2>/dev/null || true)

if [[ -z "$FILE_PATH" ]]; then
    allow
fi

# For Edit: the 'new_string' field. For Write: the 'content' field.
NEW_CONTENT=$(printf '%s' "$STDIN" | python3 -c "
import json, sys
try:
    ti = json.load(sys.stdin).get('tool_input', {})
    # Edit tool uses new_string; Write tool uses content
    print(ti.get('new_string', ti.get('content', '')))
except Exception:
    print('')
" 2>/dev/null || true)

# ---------------------------------------------------------------------------
# Fast no-op: not an RDR markdown file
# ---------------------------------------------------------------------------
# Match the conventional docs/rdr/rdr-NNN-*.md path. If .nexus.yml specifies
# a non-default rdr_paths entry, we still match on the filename pattern
# (`rdr-NNN-*.md`) which is stable across path conventions.

case "$FILE_PATH" in
    */rdr-[0-9]*.md|*/rdr-[0-9]*-*.md)
        ;;
    *)
        allow
        ;;
esac

# Extract RDR number from the filename (leading zeros preserved so NNN
# matches the T2 title/tag format the preamble writes).
RDR_NUM=$(basename "$FILE_PATH" | python3 -c "
import sys, re
m = re.match(r'rdr-(\d+)', sys.stdin.read().strip())
print(m.group(1) if m else '')
" 2>/dev/null || true)

if [[ -z "$RDR_NUM" ]]; then
    allow
fi

# ---------------------------------------------------------------------------
# Fast no-op: new content does NOT set status: closed
# ---------------------------------------------------------------------------
# Only gate transitions TO `status: closed`. Edits that change body text,
# add revision history entries, or set any other frontmatter field are not
# subject to the gate — only the status-closed flip is.

if ! printf '%s' "$NEW_CONTENT" | grep -qE '^status:[[:space:]]*closed\b'; then
    allow
fi

# ---------------------------------------------------------------------------
# Check for the rdr-close-active marker in T1 scratch
# ---------------------------------------------------------------------------
# The marker is written by the /nx:rdr-close preamble (nx/commands/rdr-close.md
# around lines 270-277) on successful Pass 2 completion. Format:
#
#   nx scratch put <rdr_id_label> --tags rdr-close-active,rdr-<rdr_id_label>
#
# `nx scratch list` has no filter flag — it emits one line per entry with
# tags in the second field. We grep for an entry whose tag list contains
# BOTH `rdr-close-active` AND `rdr-<RDR_NUM>` tags. The preamble writes the
# two tags comma-separated in either order, and `nx scratch list` prints
# them as `tag1,tag2,...` without spaces.
#
# Leading-zero normalization: the preamble uses `rdr_id_label` which is the
# first numeric match from the filename (e.g. "066" for rdr-066-foo.md, but
# could be "66" if the RDR is named without zero-padding). Check both forms.

# Give `nx scratch list` a short timeout — hooks must be fast.
MARKER_OUTPUT=$(timeout 5 nx scratch list 2>/dev/null || true)

# Strip leading zeros for the unpadded variant (066 → 66). Keep padded form too.
RDR_NUM_UNPADDED=$(printf '%s' "$RDR_NUM" | sed 's/^0*//')
[[ -z "$RDR_NUM_UNPADDED" ]] && RDR_NUM_UNPADDED="0"

# Match: a line whose tag list contains both `rdr-close-active` and
# `rdr-<RDR_NUM>` (either padded or unpadded form, since the preamble uses
# the raw t2_key which may omit leading zeros).
if printf '%s' "$MARKER_OUTPUT" | grep -qE "rdr-close-active.*rdr-(${RDR_NUM}|${RDR_NUM_UNPADDED})(,|$|[[:space:]])|rdr-(${RDR_NUM}|${RDR_NUM_UNPADDED})(,|$|[[:space:]]).*rdr-close-active"; then
    allow
fi

# ---------------------------------------------------------------------------
# Deny: construct a precise, actionable message
# ---------------------------------------------------------------------------

REASON=$(cat <<EOF
BLOCKED by rdr_close_gate_hook: attempting to set \`status: closed\` on
$FILE_PATH but no \`rdr-close-active-$RDR_NUM\` T1 scratch marker exists.

The marker is written by the \`/nx:rdr-close\` command preamble ONLY when
the slash command is invoked and Pass 2 of the Problem Statement Replay
gate succeeds. Its absence means the close flow was NOT initiated via
\`/nx:rdr-close\` — you are editing the RDR frontmatter directly, which
silently skips:

  - Step 1.5 Two-Pass Problem Statement Replay preamble (file:line pointer
    validation against every enumerated Gap in \`## Problem Statement\`)
  - Step 1.75 fresh substantive-critic dispatch against the close-time
    state (NOT a reused verdict from an earlier phase — a fresh read)
  - Step 3 user-confirmation bead gate

These are the load-bearing checks the rdr-close skill exists to perform.
Reusing a prior critic verdict from pre-close review is not equivalent —
a fresh dispatch at close time reads the state at the moment of close,
not the pre-close state.

To close this RDR properly, invoke the slash command from the main
conversation:

  /nx:rdr-close $RDR_NUM --reason implemented

The preamble will guide you through Pass 1 (gap enumeration) and Pass 2
(pointer validation), write the marker, and then the skill body will
run Steps 1.75 through 6.

If you are an AI assistant walking an RDR close flow manually: STOP.
Hand the close off to the user with "ready for /nx:rdr-close <id>" and
do not edit the frontmatter directly. This gate enforces that handoff
structurally rather than relying on agent discipline.

This error prevents the silent-scope-reduction pattern that RDR-066 and
RDR-069 exist to catch, applied to their own close flow. Two instances
of the pattern were caught in a single session before this hook shipped
(RDR-069 Phase 4c and RDR-066 Phase 5c, both auto-closed via manual
walkthrough). See \`nexus_rdr/066-validation-5c-real-self-close\` for the
rollback record.
EOF
)

deny "$REASON"
