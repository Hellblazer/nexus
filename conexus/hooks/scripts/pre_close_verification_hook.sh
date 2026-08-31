#!/bin/bash
# PreToolUse close verification hook on bd close/done.
# nexus-4av2n: BLOCKS a close when the bead(s) being closed have no
# review-completed marker (bead-id-anchored, entry-level match) -- it used to
# be advisory-only and stamped verification=passed unconditionally, which is
# the false-record shape the no-silent-fallbacks doctrine exists to prevent.
# Exit 0 always with hookSpecificOutput JSON.
# SPDX-License-Identifier: AGPL-3.0-or-later

# No set -e/-u/-o pipefail — this hook must NEVER fail.
# Every code path must produce valid JSON on stdout and exit 0.

# ---------------------------------------------------------------------------
# Helpers — PreToolUse uses hookSpecificOutput, NOT decision/reason
# ---------------------------------------------------------------------------

allow() {
    if [[ -n "${1:-}" ]]; then
        local escaped
        escaped=$(printf '%s' "$1" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" 2>/dev/null || printf '"%s"' "$1")
        printf '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "additionalContext": %s}}\n' "$escaped"
    else
        printf '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}\n'
    fi
    exit 0
}

# deny <reason> — mirrors conexus/hooks/scripts/routing/_lib.py's
# deny_envelope shape so both the bash-native and python-native hook
# surfaces read identically to the model (permissionDecisionReason) and the
# user transcript (systemMessage, first line of reason).
deny() {
    local reason="${1:-No reason provided.}"
    local escaped summary summary_escaped
    escaped=$(printf '%s' "$reason" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" 2>/dev/null || printf '"%s"' "$reason")
    summary=$(printf '%s' "$reason" | head -1)
    summary_escaped=$(printf '%s' "$summary" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" 2>/dev/null || printf '"%s"' "$summary")
    printf '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": %s, "reason": %s}, "systemMessage": %s}\n' \
        "$escaped" "$escaped" "$summary_escaped"
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
# Export the harness-provided session_id for every `nx` CLI call this hook
# makes (nexus-36q84). `nx scratch list` resolves its active session via
# nexus.session.resolve_active_session_id(), whose lowest-priority fallback
# is the machine-wide ~/.config/nexus/current_session flat file — clobbered
# unconditionally by ANY second top-level Claude Code session's SessionStart
# hook. This hook runs detached from any live nx-mcp process and cannot rely
# on env-var inheritance from a parent session, so it reads session_id
# directly out of its own stdin JSON payload (present on every hook
# invocation per the standard hook contract) and forces NX_SESSION_ID to it
# — the highest-priority tier in the resolution chain — so `nx scratch list`
# below always resolves to the session that is actually running this hook,
# never a sibling session's clobbered pointer.
# ---------------------------------------------------------------------------

HOOK_SESSION_ID=$(printf '%s' "$STDIN" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('session_id', ''))
except Exception:
    print('')
" 2>/dev/null || true)

if [[ -n "$HOOK_SESSION_ID" ]]; then
    export NX_SESSION_ID="$HOOK_SESSION_ID"
    # nexus-6a19f / nexus-f7xyq: since f7xyq, `nx scratch list` fails loud
    # (T1ServerNotFoundError) for an explicit NX_SESSION_ID with no live T1
    # lease -- which this hook's own forced NX_SESSION_ID above routinely
    # triggers (this hook runs detached, with no lease published for the
    # transcript session it read from stdin). Pre-f7xyq that case silently
    # fell through to the shared CLI-dedicated scope, which is exactly
    # where this hook's review-completed markers live -- so opt back into
    # that fallback explicitly rather than losing the advisory signal.
    export NX_T1_ALLOW_SHARED_FALLBACK=1
fi

# ---------------------------------------------------------------------------
# Fast no-op: check tool_name
# ---------------------------------------------------------------------------

TOOL_NAME=$(printf '%s' "$STDIN" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('tool_name', ''))
except Exception:
    print('')
" 2>/dev/null || true)

if [[ "$TOOL_NAME" != "Bash" ]]; then
    allow
fi

# ---------------------------------------------------------------------------
# Fast no-op: check if command matches bd close/done/create
# ---------------------------------------------------------------------------

TOOL_INPUT=$(printf '%s' "$STDIN" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('tool_input', {}).get('command', ''))
except Exception:
    print('')
" 2>/dev/null || true)

# nexus-4av2n round 2 Important-4 (code-review-expert): the prior blanket
# `grep -qE '\bbd[[:space:]]+(close|done|create)\b'` matched the pattern
# ANYWHERE in the raw command string -- including inside an unrelated
# quoted argument, e.g. `git commit -m "docs: bd close workflow notes"`.
# Harmless under the old advisory-only hook; now that a match can DENY the
# Bash call outright, a false match has real cost. Tokenize per shell
# segment (same &&/;/|/then/do splitting the push-gate already uses) and
# require literal `bd close|done|create` as the FIRST TWO tokens of a
# segment -- a quoted string that merely CONTAINS those words never
# tokenizes that way.
# nexus-cr4lp F2: skip leading NAME=VALUE env-assignment tokens before
# requiring the literal 'bd' verb -- `NX_REVIEW_GATE_OVERRIDE=1 bd close
# nexus-x` pre-fix tokenized as tokens[0]=='NX_REVIEW_GATE_OVERRIDE=1', so
# has_close_or_done stayed False and the WHOLE hook fast-no-op'd below
# (the whole coverage check silently never ran -- B2, T2 nexus/guard-
# evidence-cluster-root-cause-2026-08-18). Also detects the SAME inline
# assignment here (one tokenisation pass) so OVERRIDE below can honor it
# even though this hook's own os.environ never observes a shell-inline
# `NAME=VALUE cmd` assignment (that scoping belongs to the child process
# the shell would spawn, not this hook).
BD_VERBS=$(printf '%s' "$TOOL_INPUT" | python3 -c "
import json, re, shlex, sys
cmd = sys.stdin.read()
segments = re.split(r'(?:&&|\|\||;|\s\|\s|\bthen\b|\bdo\b)', cmd)
has_create = False
has_close_or_done = False
inline_override = False
env_assign_re = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
for seg in segments:
    try:
        variants = [shlex.split(seg, posix=True)]
    except ValueError:
        # nexus-2e874: never silently skip a segment shlex rejects for
        # unbalanced quoting -- that direction made the verb never match,
        # so the whole hook fast-no-op'd and a stray quote in a --reason
        # value fully bypassed the close gate. Degrade to TWO rough
        # variants (quote-as-space keeps boundary-glued quotes splitting;
        # quote-removed keeps a quote INSIDE the verb from fracturing it,
        # e.g. b\"d close) -- a match in either counts. Same posture as
        # the BEAD_IDS_JSON raw-scan fallback below.
        variants = [
            seg.replace('\"', ' ').replace(\"'\", ' ').split(),
            seg.replace('\"', '').replace(\"'\", '').split(),
        ]
    for tokens in variants:
        i = 0
        while i < len(tokens) and env_assign_re.match(tokens[i]):
            if tokens[i] == 'NX_REVIEW_GATE_OVERRIDE=1':
                inline_override = True
            i += 1
        rest = tokens[i:]
        if len(rest) >= 2 and rest[0] == 'bd':
            if rest[1] == 'create':
                has_create = True
            elif rest[1] in ('close', 'done'):
                has_close_or_done = True
print(json.dumps({
    'has_create': has_create,
    'has_close_or_done': has_close_or_done,
    'inline_override': inline_override,
}))
" 2>/dev/null || echo '{"has_create": false, "has_close_or_done": false, "inline_override": false}')

HAS_CREATE=$(printf '%s' "$BD_VERBS" | python3 -c "import json,sys; print(json.load(sys.stdin).get('has_create', False))" 2>/dev/null || echo False)
HAS_CLOSE_OR_DONE=$(printf '%s' "$BD_VERBS" | python3 -c "import json,sys; print(json.load(sys.stdin).get('has_close_or_done', False))" 2>/dev/null || echo False)
INLINE_OVERRIDE=$(printf '%s' "$BD_VERBS" | python3 -c "import json,sys; print(json.load(sys.stdin).get('inline_override', False))" 2>/dev/null || echo False)

if [[ "$HAS_CREATE" != "True" && "$HAS_CLOSE_OR_DONE" != "True" ]]; then
    allow
fi

# ---------------------------------------------------------------------------
# bd create branch — commitment-metadata enforcement during active RDR close
# (RDR-065 Gap 3). Audit log: /tmp/nexus-rdr065-bd-create-audit.log (NOT .beads/).
# ---------------------------------------------------------------------------

if [[ "$HAS_CREATE" == "True" ]]; then
    AUDIT_LOG="/tmp/nexus-rdr065-bd-create-audit.log"

    # Look up active-close marker via T1 scratch. The two-pass preamble tags
    # entries with `rdr-close-active,rdr-NNN` so the rdr id rides along on the
    # tag line. We avoid `nx scratch search` here because that is semantic, not
    # exact-tag — list+grep is the only reliable form.
    ACTIVE_CLOSE_RDR=""
    if command -v nx &>/dev/null; then
        ACTIVE_CLOSE_RDR=$(nx scratch list 2>/dev/null \
            | grep -E '\brdr-close-active\b' \
            | grep -oE '\brdr-[0-9]+\b' \
            | head -1 \
            | sed -E 's/^rdr-//')
    fi
    # Stripped form for numeric matching (065 → 65). The HA-3 regex below uses
    # 0* so it accepts either padded or unpadded forms in the bead text.
    ACTIVE_CLOSE_INT=$(printf '%s' "$ACTIVE_CLOSE_RDR" | sed -E 's/^0+//')
    [[ -z "$ACTIVE_CLOSE_INT" && -n "$ACTIVE_CLOSE_RDR" ]] && ACTIVE_CLOSE_INT="$ACTIVE_CLOSE_RDR"

    # Pull agent_id / agent_type from the original hook stdin (subagent attribution).
    AGENT_ID=$(printf '%s' "$STDIN" | python3 -c "
import json, sys
try: print(json.load(sys.stdin).get('agent_id', ''))
except Exception: print('')
" 2>/dev/null || true)
    AGENT_TYPE=$(printf '%s' "$STDIN" | python3 -c "
import json, sys
try: print(json.load(sys.stdin).get('agent_type', ''))
except Exception: print('')
" 2>/dev/null || true)

    audit_line() {
        local decision="$1" missing_json="${2:-[]}"
        local ts cmd_excerpt
        ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        cmd_excerpt=$(printf '%s' "$TOOL_INPUT" | head -c 200 | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" 2>/dev/null || printf '"%s"' "$TOOL_INPUT")
        printf '{"ts":"%s","agent_id":"%s","agent_type":"%s","decision":"%s","rdr":"%s","missing":%s,"cmd":%s}\n' \
            "$ts" "$AGENT_ID" "$AGENT_TYPE" "$decision" "$ACTIVE_CLOSE_RDR" "$missing_json" "$cmd_excerpt" \
            >> "$AUDIT_LOG" 2>/dev/null || true
    }

    if [[ -z "$ACTIVE_CLOSE_RDR" ]]; then
        audit_line "allow-no-active-close"
        allow
    fi

    # Robust title/description extraction via shlex.
    PARSED=$(printf '%s' "$TOOL_INPUT" | python3 -c "
import sys, shlex
title, desc = '', ''
try:
    tokens = shlex.split(sys.stdin.read())
    for i, t in enumerate(tokens):
        if t == '--title' and i + 1 < len(tokens):
            title = tokens[i + 1]
        elif t.startswith('--title='):
            title = t.split('=', 1)[1]
        elif t == '--description' and i + 1 < len(tokens):
            desc = tokens[i + 1]
        elif t.startswith('--description='):
            desc = t.split('=', 1)[1]
except Exception:
    pass
print(title)
print('---NXSEP---')
print(desc)
" 2>/dev/null || true)
    TITLE_VAL=$(printf '%s' "$PARSED" | sed -n '1,/---NXSEP---/p' | sed '$d')
    DESC_VAL=$(printf '%s' "$PARSED" | awk '/---NXSEP---/{flag=1; next} flag')
    COMBINED="${TITLE_VAL} ${DESC_VAL}"

    # HA-3 scoped detection: does the bead reference the active RDR ID?
    RDR_MENTIONED=false
    if printf '%s' "$COMBINED" | grep -qiE "(^|[^0-9])0*${ACTIVE_CLOSE_INT}([^0-9]|\$)|RDR-0*${ACTIVE_CLOSE_INT}|rdr-0*${ACTIVE_CLOSE_INT}"; then
        RDR_MENTIONED=true
    fi

    if [[ "$RDR_MENTIONED" == "false" ]]; then
        audit_line "allow-advisory"
        allow "RDR close active for RDR-${ACTIVE_CLOSE_RDR} — if this bead is a follow-up, add reopens_rdr/sprint/drift_condition metadata to the description."
    fi

    # RDR is referenced — require commitment markers.
    MISSING=()
    printf '%s' "$COMBINED" | grep -qi 'reopens_rdr' || MISSING+=("reopens_rdr")
    printf '%s' "$COMBINED" | grep -qiE 'sprint|due' || MISSING+=("sprint or due")
    printf '%s' "$COMBINED" | grep -qi 'drift_condition' || MISSING+=("drift_condition")

    if [[ ${#MISSING[@]} -eq 0 ]]; then
        audit_line "allow-complete"
        allow
    fi

    # Build missing-list JSON for audit
    MISSING_JSON=$(printf '%s\n' "${MISSING[@]}" | python3 -c "import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))" 2>/dev/null || printf '[]')
    audit_line "deny" "$MISSING_JSON"

    MISSING_DISPLAY=$(printf -- '- %s\n' "${MISSING[@]}")
    REASON=$(printf 'Follow-up bead for RDR-%s is missing required commitment metadata.\nMissing fields:\n%s\nAdd these to the --description, e.g.:\n  reopens_rdr: %s\n  sprint: implementation-2026-04\n  drift_condition: <what drift looks like>' \
        "$ACTIVE_CLOSE_RDR" "$MISSING_DISPLAY" "$ACTIVE_CLOSE_RDR")
    REASON_JSON=$(printf '%s' "$REASON" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" 2>/dev/null || printf '"%s"' "$REASON")
    printf '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "reason": %s}}\n' "$REASON_JSON"
    exit 0
fi

# ---------------------------------------------------------------------------
# Read verification config
# ---------------------------------------------------------------------------

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/../../.." 2>/dev/null && pwd)}"
CONFIG=$(python3 "$PLUGIN_ROOT/hooks/scripts/read_verification_config.py" 2>/dev/null || true)

if [[ -z "$CONFIG" ]]; then
    allow
fi

ON_CLOSE=$(printf '%s' "$CONFIG" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('on_close', False))
except Exception:
    print('False')
" 2>/dev/null || echo "False")

if [[ "$ON_CLOSE" != "True" ]]; then
    allow
fi

# ---------------------------------------------------------------------------
# Resolve bead id(s) — nexus-4av2n datum (i), Hal 2026-08-03: a naive
# "first token after close/done" capture is defeated by
# `for b in nexus-x nexus-y; do bd close $b; done` -- it looks up the LITERAL
# string "$b", which never matches a real marker, producing a false
# no-marker verdict for beads that WERE reviewed. A PreToolUse hook only
# ever sees the pre-expansion command text (there is no post-expansion
# text to read short of actually executing the command), so instead of
# capturing "the token after close", scan the WHOLE command string for
# every literal nexus-<id>-shaped token. In the loop-variable shape those
# real ids sit in the `for b in ...` list even though the close call itself
# is `$b` -- this recovers them without executing anything. A command with
# NO literal bead id anywhere (e.g. `bd close $(cat f)`) is genuinely
# indeterminate and is handled below by allowing WITHOUT stamping
# verification (never a false "passed").
# ---------------------------------------------------------------------------

# nexus-cr4lp F3: the blanket whole-string regex scan above (pre-fix) also
# harvested a bead id that appeared ONLY inside a --reason/--description/
# --notes/-m OPTION VALUE elsewhere in the same compound command -- e.g.
# `bd close nexus-wixar --reason="... Residue: nexus-lemv5 ..."` harvested
# nexus-lemv5 as a REQUIRED-coverage id even though it is prose, not a
# close target (T2 nexus/guard-evidence-cluster-root-cause-2026-08-18 LEG
# D1). Fixed: per-segment shlex tokenisation (same &&/;/|/then/do split the
# push-gate hook uses), skip the VALUE token of any of those four flags
# (both `--flag value` and `--flag=value` forms -- the latter is ONE shlex
# token, skipped whole), scan everything else. The `for b in ...; do bd
# close $b; done` loop-variable recovery (datum i) still works: those ids
# sit in the `for ... in` segment as bare positional tokens, never a flag
# value, so they are never skipped.
BEAD_IDS_JSON=$(printf '%s' "$TOOL_INPUT" | python3 -c "
import json, re, shlex, sys
cmd = sys.stdin.read()
VALUE_FLAGS = {'--reason', '--description', '--notes', '-m'}
BEAD_RE = re.compile(r'\bnexus-[a-z0-9]+\b', re.IGNORECASE)
segments = re.split(r'(?:&&|\|\||;|\s\|\s|\bthen\b|\bdo\b)', cmd)
seen, ids = set(), []

def _scan_text(text):
    for m in BEAD_RE.finditer(text):
        tok = m.group(0).lower()
        if tok not in seen:
            seen.add(tok)
            ids.append(tok)

for seg in segments:
    try:
        tokens = shlex.split(seg, posix=True)
    except ValueError:
        # Malformed quoting for this segment -- fall back to a raw scan
        # rather than losing the segment's ids entirely.
        _scan_text(seg)
        continue
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        bare = tok.split('=', 1)[0]
        if bare in VALUE_FLAGS:
            if '=' in tok:
                # --reason=value form: flag and value are ONE token; skip
                # it whole, do not scan.
                i += 1
                continue
            # --reason value form: skip the flag token AND its separate
            # value token.
            i += 2
            continue
        _scan_text(tok)
        i += 1

print(json.dumps(ids))
" 2>/dev/null || echo '[]')

if [[ -z "$BEAD_IDS_JSON" || "$BEAD_IDS_JSON" == "[]" ]]; then
    allow "INDETERMINATE: no literal bead id (nexus-*) found anywhere in this bd close/done command — cannot check a review marker statically, so verification is NOT stamped. Prefer literal ids over shell variables so the review gate can verify coverage."
fi

ALL_IDS_SPACE=$(printf '%s' "$BEAD_IDS_JSON" | python3 -c "import json,sys; print(' '.join(json.load(sys.stdin)))" 2>/dev/null || true)

# nexus-cr4lp F2: OVERRIDE is honored from the hook's own process env OR
# an inline `NX_REVIEW_GATE_OVERRIDE=1` prefix in the command text itself
# (INLINE_OVERRIDE, detected above alongside the bd-verb tokenisation) --
# the latter is otherwise invisible to `${NX_REVIEW_GATE_OVERRIDE:-}`
# entirely, since a shell-inline assignment scopes to the child process
# the shell would spawn, never to this hook's own environment.
OVERRIDE=0
if [[ "${NX_REVIEW_GATE_OVERRIDE:-}" == "1" || "$INLINE_OVERRIDE" == "True" ]]; then
    OVERRIDE=1
fi

# nexus-cr4lp F2: every override use is auditable -- a logged routing
# event, via the same JSONL sink the python routing hooks share
# (`_lib.log_routing_event`, `conexus/hooks/scripts/routing/_lib.py`).
# This hook is not itself a `routing/` rule, so it has no such
# integration pre-fix; imported here on the override path only (never on
# the hot fast-no-op path above) so this adds no extra `nx` spawn and no
# cost to the common case.
_log_override_escape() {
    local ids="$1"
    # Resolve the sibling `routing/_lib.py` off THIS SCRIPT's own real
    # location, never off CLAUDE_PLUGIN_ROOT: tests deliberately fake that
    # var to redirect the read_verification_config.py lookup earlier in
    # this file, and following it here would silently miss the import
    # (caught below, so the miss would otherwise be invisible).
    local script_dir
    script_dir="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
    NX_HOOK_ROUTING_DIR="$script_dir/routing" NX_HOOK_LOG_IDS="$ids" NX_HOOK_LOG_CMD="$TOOL_INPUT" \
        python3 -c "
import os, sys
sys.path.insert(0, os.environ.get('NX_HOOK_ROUTING_DIR', ''))
try:
    import _lib
    _lib.log_routing_event(
        rule='pre_close_verification_hook', outcome='escape', tool_name='Bash',
        command_fragment=os.environ.get('NX_HOOK_LOG_CMD', ''),
        escape_reason='NX_REVIEW_GATE_OVERRIDE=1: ' + os.environ.get('NX_HOOK_LOG_IDS', ''),
    )
except Exception:
    pass
" 2>/dev/null || true
}

_stamp_ids() {
    # _stamp_ids <space-separated ids> <state> <reason> — best-effort bd
    # set-state for each given id. Never called with any id on the deny
    # path (a denied close must never acquire ANY verification record —
    # that IS the false-record fix). nexus-4av2n round 2 Significant-d
    # (both reviewers): a failed stamp is now LOUD (stderr, never stdout —
    # PreToolUse stdout must stay pure JSON) instead of silently swallowed,
    # so a broken `bd` binary at close time is at least observable in hook
    # debug logs rather than producing an audit record nobody can trust
    # AND nobody was told is missing.
    local ids="$1" state="$2" reason="$3"
    [[ -z "$ids" ]] && return 0
    if ! command -v bd &>/dev/null; then
        echo "WARNING: bd not found on PATH — cannot stamp verification=$state for: $ids" >&2
        return 0
    fi
    local bid
    for bid in $ids; do
        if ! bd set-state "$bid" "verification=$state" --reason "$reason" 2>/dev/null; then
            echo "WARNING: bd set-state $bid verification=$state FAILED — audit record not written" >&2
        fi
    done
}

# ---------------------------------------------------------------------------
# Coverage check, T1-ONLY (nexus-fgekf, 2026-08-30 — retires the T2 memory
# leg; Sam 2026-08-27: "t2 markers should not really exist... let's not
# ensconce a workaround as a thing").
#
# HISTORY, so the leg is not re-added without re-reading it: the dual-source
# shape (nexus-4av2n round 2 Critical-1) existed because a marker written
# via the MCP `scratch` tool was INVISIBLE to this hook's detached CLI
# `nx scratch list` whenever the CLI's T1 lease was stale — the CLI then
# silently resolved the pooled fallback scope (nexus-6a19f) instead of the
# session's. BOTH halves of that divergence are since fixed: nexus-d76vc
# (2026-08-07) re-leases the MCP scope onto the new session id on
# /clear//resume, and nexus-f7xyq made a dead lease under an explicit
# session id FAIL LOUD (T1ServerNotFoundError) instead of silently reading
# the pooled scope. Measured on a live session 2026-08-30 (nexus-fgekf
# probe): an MCP-written scratch entry — including one written by a
# subagent — is immediately visible to `nx scratch list`.
#
# WHY T1-only is the DESIGN, not just the simplification: T1 is
# session-scoped and ephemeral, so a T1 marker cannot outlive the session
# that wrote it — the marker and the close are the same session's work. A
# durable T2 marker can satisfy a close in a much later session, for a
# review nobody in that session performed. Every property that makes T2
# right for knowledge makes it wrong for an attestation.
#
# Per-id final status is one of:
#   covered   -- T1 found an entry-anchored review-completed marker naming
#                the complete reviewer set.
#   incomplete-- a marker exists but does not name the full set (nexus-e3mak).
#   missing   -- T1 was reachable and holds no marker. A real absence --
#                denies.
#   uncertain -- T1 unreachable (nx missing, or `nx scratch list` failed —
#                post-f7xyq that includes a dead CLI lease failing loud).
#                A capability gap: warns + stamps unverified, never
#                silently denies or silently passes (the same posture the
#                dual-source shape used for total unreachability).
#   deadline  -- the T1 read blew the wall-clock budget (DEADLINE_SECONDS,
#                3.5s default, under the 5s PreToolUse ceiling —
#                nexus-4av2n round 3). Denies: "ran out of time" is
#                indistinguishable from "would have found nothing".
#
# WRITE-SIDE CONTRACT (documented here + PENDING_RELEASE.md): a marker is
# visible to this gate iff it exists in THIS SESSION's T1 scratch
# (`nx scratch put` / the MCP `scratch` tool — the scopes converge) with
# content containing `review-completed`, the bead id, and both reviewer
# names. T2 memory markers are NOT consulted (nexus-fgekf).
#
# Deny-on-absence STAYS: an allow-when-T1-empty fallback would re-vacuate
# the gate for the 2026-07-31 no-markers-anywhere case.
# ---------------------------------------------------------------------------

export NX_HOOK_BEAD_IDS_JSON="$BEAD_IDS_JSON"
COVERAGE_RESULT=$(python3 -c "
import json, os, re, shutil, subprocess, time

bead_ids = json.loads(os.environ.get('NX_HOOK_BEAD_IDS_JSON', '[]'))

# nexus-4av2n round 3: wall-clock deadline for the WHOLE coverage phase,
# overridable via NX_CLOSE_GATE_DEADLINE_SECONDS (test seam, mirrors the
# push-gate's NX_PUSH_GATE_DEADLINE_SECONDS) so tests can trip it fast and
# deterministically with a slow stub nx rather than waiting out 3.5s.
DEADLINE_SECONDS = float(os.environ.get('NX_CLOSE_GATE_DEADLINE_SECONDS', '3.5') or '3.5')
_start = time.monotonic()

def _deadline_exceeded():
    return (time.monotonic() - _start) >= DEADLINE_SECONDS

def _clamp_timeout(floor=0.5, ceiling=15.0):
    remaining = max(0.0, DEADLINE_SECONDS - (time.monotonic() - _start))
    return max(floor, min(ceiling, remaining))

_HEADER_RE = re.compile(r'^\[[0-9a-fA-F]+\]\s+(.*?)\s{2,}flagged=', re.IGNORECASE)

def _tags(tags_line):
    m = _HEADER_RE.match(tags_line)
    body = m.group(1) if m else tags_line
    return [t.strip().lower() for t in body.split(',') if t.strip()]

#: nexus-e3mak: a marker counts as coverage only when it NAMES THE COMPLETE
#: required reviewer set. The gate's own remedy calls them out --
#: 'Run the stacked reviewers (code-review-expert + substantive-critic)' --
#: and its text says the critic is never optional, but the match was on the
#: literal string 'review-completed' plus the bead id, so ANY entry carrying
#: those satisfied it.
#:
#: 2026-08-26 (RG-C, nexus-utpuw.23): the dispatched code-review-expert wrote
#: a handoff note for its sibling beginning
#:     review-completed bead=nexus-utpuw.23 (RG-C reviewer 1/2: ...)
#: It was honest -- it said 'reviewer 1/2' -- and the gate would have passed
#: the close with the substantive-critic never dispatched. Same pathology as
#: nexus-1f98p: a guard whose evidence is produced by the party it checks.
#:
#: POSITIVE NAMING, no grace path for the older marker form. Plugins load
#: from an immutable pinned tag, so there is no mixed-version population to
#: keep working: when the pin advances, this hook and the remedy text it
#: prints ship together. A 'names some but not all' carve-out was drafted and
#: DISCARDED -- the incident marker names reviewers by COUNT ('1/2'), not by
#: agent type, so a rule keyed on partial naming would not have caught the
#: very incident this bead is about.
#:
#: This raises the bar; it does not make the gate unforgeable. A reviewer can
#: still name both. Nothing available today is independent AND bead-keyed:
#: T1/T2 rows do carry an 'agent' column, but NOTHING in production sets
#: NX_AGENT (only tests do) and CONTEXT_PROTOCOL merely INSTRUCTS agents to
#: pass agent= -- so refusing on a self-declared author would rest the gate
#: on a claim by the party it gates, which is this bead one level down.
_REQUIRED_REVIEWERS = ('code-review-expert', 'substantive-critic')

def _named_reviewers(text):
    low = (text or '').lower()
    return [r for r in _REQUIRED_REVIEWERS if r in low]

def _names_full_set(text):
    return len(_named_reviewers(text)) == len(_REQUIRED_REVIEWERS)

def _t1_covers(bead_id, tags_line, content_line):
    tags = _tags(tags_line)
    if 'review-completed' not in tags:
        return False
    if bead_id in tags:
        return True
    pat = re.compile(r'(?<![A-Za-z0-9-])' + re.escape(bead_id) + r'(?![A-Za-z0-9-])', re.IGNORECASE)
    return bool(pat.search(content_line))

def _parse_entries(raw):
    lines = raw.splitlines()
    entries = []
    i = 0
    while i < len(lines):
        if lines[i].startswith('['):
            header = lines[i]
            content = lines[i + 1] if i + 1 < len(lines) else ''
            entries.append((header, content))
            i += 2
        else:
            i += 1
    return entries

# nexus-fgekf: T2 memory is NOT consulted -- see the header block. The
# _t2_covers/_t2_lookup pair and the per-id 'nx memory search' spawns are
# deliberately GONE, not dormant. (No backticks in THIS comment: it lives
# inside the double-quoted python3 -c string, where a backtick is live
# bash command substitution -- the first draft of this very comment
# EXECUTED nx memory search from inside the program that exists to prove
# nothing calls it.)
t1_reachable = False
t1_timed_out = False
t1_entries = []
if shutil.which('nx'):
    try:
        r = subprocess.run(['nx', 'scratch', 'list'], capture_output=True, text=True, timeout=_clamp_timeout())
        if r.returncode == 0:
            t1_reachable = True
            t1_entries = _parse_entries(r.stdout)
    except subprocess.TimeoutExpired:
        # Time budget, not capability: deny-on-indeterminate (round 3
        # doctrine) — distinct from rc!=0/nx-missing, which is a
        # capability gap (post-f7xyq that includes a dead CLI lease
        # failing loud).
        t1_timed_out = True
    except Exception:
        pass

status = {}
seen_names = {}
for bid in bead_ids:
    _t1_hits = [(t, c) for t, c in t1_entries if _t1_covers(bid, t, c)]
    if _t1_hits:
        _txt = ' '.join(t + ' ' + c for t, c in _t1_hits)
        if _names_full_set(_txt):
            status[bid] = 'covered'
            continue
        seen_names[bid] = _named_reviewers(_txt)
        status[bid] = 'incomplete'
        continue
    if t1_timed_out or _deadline_exceeded():
        status[bid] = 'deadline'
    elif not t1_reachable:
        status[bid] = 'uncertain'
    else:
        status[bid] = 'missing'

print(json.dumps({'t1_reachable': t1_reachable, 'status': status, 'deadline_seconds': DEADLINE_SECONDS, 'seen_names': seen_names}))
" 2>/dev/null || echo '{"t1_reachable": false, "status": {}, "deadline_seconds": 3.5}')

_ids_with_status() {
    printf '%s' "$COVERAGE_RESULT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(' '.join(k for k, v in d.get('status', {}).items() if v == '$1'))
" 2>/dev/null || true
}

COVERED_SPACE=$(_ids_with_status covered)
MISSING_SPACE=$(_ids_with_status missing)
UNCERTAIN_SPACE=$(_ids_with_status uncertain)
DEADLINE_SPACE=$(_ids_with_status deadline)
INCOMPLETE_SPACE=$(_ids_with_status incomplete)
DEADLINE_SECONDS_DISPLAY=$(printf '%s' "$COVERAGE_RESULT" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('deadline_seconds', 3.5))
except Exception:
    print(3.5)
" 2>/dev/null || echo "3.5")

# OVERRIDE wins over ALL THREE non-covered states uniformly (missing,
# uncertain, OR deadline) -- a deliberate operator override is a single
# audit signal and must not be silently absorbed into "unverified" just
# because the underlying reason happened to be a capability gap or a
# self-imposed time budget rather than a real absence. Checked FIRST,
# before the deny path below.
if [[ "$OVERRIDE" -eq 1 && ( -n "$MISSING_SPACE" || -n "$UNCERTAIN_SPACE" || -n "$DEADLINE_SPACE" || -n "$INCOMPLETE_SPACE" ) ]]; then
    _stamp_ids "$COVERED_SPACE" "passed" "review-completed marker verified at close"
    NOT_COVERED_SPACE="$MISSING_SPACE $UNCERTAIN_SPACE $DEADLINE_SPACE $INCOMPLETE_SPACE"
    _stamp_ids "$NOT_COVERED_SPACE" "overridden" "NX_REVIEW_GATE_OVERRIDE=1; no confirmed review-completed coverage in T1 scratch for: $NOT_COVERED_SPACE"
    # nexus-cr4lp F2: every override use is auditable, logged BEFORE the
    # allow (env-sourced OR inline-command-text-sourced -- both routes
    # through the same OVERRIDE flag, same log call).
    _log_override_escape "$NOT_COVERED_SPACE"
    allow "OVERRIDE (NX_REVIEW_GATE_OVERRIDE=1): no confirmed review-completed coverage in T1 scratch for $NOT_COVERED_SPACE — closing anyway. Stamped verification=overridden for those ids. This bypass is deliberate and audited."
fi

if [[ -n "$MISSING_SPACE" || -n "$DEADLINE_SPACE" || -n "$INCOMPLETE_SPACE" ]]; then
    # DENY — no _stamp_ids call on this path for ANY id, covered or not. A
    # blocked close must never acquire ANY verification record; that is
    # the false-record fix this bead exists for (datum 2). nexus-4av2n
    # round 3: a genuine absence (MISSING) and a deadline-exceeded
    # indeterminate (DEADLINE) both deny, composed via python for a clean
    # conditional message rather than bash string-splicing.
    export NX_HOOK_MISSING_SPACE="$MISSING_SPACE"
    export NX_HOOK_DEADLINE_SPACE="$DEADLINE_SPACE"
    export NX_HOOK_INCOMPLETE_SPACE="$INCOMPLETE_SPACE"
    export NX_HOOK_DEADLINE_SECONDS_DISPLAY="$DEADLINE_SECONDS_DISPLAY"
    DENY_MSG=$(python3 -c "
import os
missing = os.environ.get('NX_HOOK_MISSING_SPACE', '').split()
deadline_ids = os.environ.get('NX_HOOK_DEADLINE_SPACE', '').split()
incomplete = os.environ.get('NX_HOOK_INCOMPLETE_SPACE', '').split()
deadline_seconds = os.environ.get('NX_HOOK_DEADLINE_SECONDS_DISPLAY', '3.5')
lines = []
if missing:
    lines.append('Close blocked: no review-completed marker found in T1 scratch for: ' + ' '.join(missing) + '. (T2 memory markers are no longer consulted — nexus-fgekf: an attestation must come from the closing session, not a durable store.)')
if incomplete:
    lines.append(
        'Close blocked: a review-completed marker exists but does NOT name the full required reviewer set for: '
        + ' '.join(incomplete) + '. A marker must name BOTH code-review-expert AND substantive-critic '
        '(nexus-e3mak: a reviewer left a handoff note saying \'reviewer 1/2\' and it satisfied this gate '
        'with the critic never dispatched).'
    )
if deadline_ids:
    lines.append(
        f\"Close blocked: coverage could not be VERIFIED within the hook's {deadline_seconds}s wall-clock \"
        f\"budget for: \" + ' '.join(deadline_ids) + ' (not confirmed missing -- just unchecked; the hook '
        'stops itself deterministically rather than risk the harness PreToolUse timeout killing it '
        'mid-check, nexus-4av2n round 3).'
    )
lines.append(
    'Remedy: Run the marker write as a SEPARATE tool call -- this deny aborts the '
    'ENTIRE command, including any marker write bundled ahead of it '
    '(nexus-cr4lp F4). Run the stacked reviewers (code-review-expert + substantive-critic), then write the'
)
lines.append('marker to T1 scratch (this session; the MCP scratch tool and the CLI converge):')
lines.append('  nx scratch put \"review-completed: <bead-id> reviewers=code-review-expert,substantive-critic\" --tags \"review-completed,<bead-id>\"')
lines.append('The marker MUST name both reviewers; naming one (or neither) is refused. T2 memory markers do not satisfy this gate (nexus-fgekf).')
lines.append('then re-run this close.')
lines.append('Deliberate override (audited): set NX_REVIEW_GATE_OVERRIDE=1 and re-run.')
print(chr(10).join(lines))
" 2>/dev/null || echo "Close blocked: coverage could not be verified in T1 scratch. Set NX_REVIEW_GATE_OVERRIDE=1 to override.")
    deny "$DENY_MSG"
fi

if [[ -n "$UNCERTAIN_SPACE" ]]; then
    _stamp_ids "$COVERED_SPACE" "passed" "review-completed marker verified at close"
    _stamp_ids "$UNCERTAIN_SPACE" "unverified" "T1 scratch unreachable at close time (capability gap, not a time-budget issue)"
    allow "WARNING: could not verify review-completed coverage in T1 scratch for $UNCERTAIN_SPACE — T1 unreachable (nx missing or 'nx scratch list' failed; post-nexus-f7xyq that includes a dead CLI T1 lease failing loud — check 'nx doctor --check-t1'). Closing anyway (a broken verification path must not brick every bead close) but stamped verification=unverified for those ids, NOT passed. If review truly happened this is a capability gap, not a review gap. To silence this deliberately, set NX_REVIEW_GATE_OVERRIDE=1."
fi

_stamp_ids "$COVERED_SPACE" "passed" "review-completed marker verified at close"
allow "Review completed for $ALL_IDS_SPACE."
