# RDR-039 Phase 2: PostCompact & StopFailure Hooks — Implementation Plan

**Epic**: nexus-opkj (RDR-039)
**Feature bead**: nexus-q411 (Phase 2)
**Branch**: `feature/nexus-q411-phase2-hooks`

## Overview

Add two new hook event handlers to the nx plugin. PostCompact re-injects
in-progress bead state and scratch entries after compaction. StopFailure
logs API failure context to beads memory for observability.

## Dependency Graph

```
nexus-hiwa (2a: PostCompact script)  ─┐
                                       ├─→ nexus-fy7l (2c: Register in hooks.json)
nexus-d0ia (2b: StopFailure script)  ─┘         │
                                                  ▼
                                       nexus-ilwk (2d: Validate hooks)
```

2a and 2b are independent — can be implemented in parallel.
2c depends on both. 2d depends on 2c.

## Phase 1: Create PostCompact hook script (nexus-hiwa)

**File**: `nx/hooks/scripts/post_compact_hook.sh`

**Behavior**:
1. Show in-progress beads: `bd list --status=in_progress --limit=5`
2. Show T1 scratch entries: `nx scratch list | head -5`
3. Remind about `bd prime` if `.beads/` exists
4. Total output ≤ 20 lines

**Stdin JSON** (from Claude Code):
```json
{
  "session_id": "...",
  "transcript_path": "...",
  "cwd": "...",
  "hook_event_name": "PostCompact",
  "trigger": "manual|auto",
  "compact_summary": "..."
}
```

**Note**: We do NOT re-run `nx hook session-start` or `bd prime` here because
SessionStart already fires on compact events (`matcher: "startup|resume|clear|compact"`).
PostCompact adds only what SessionStart doesn't cover: in-progress bead context
and scratch entries that were evicted from the conversation window.

**Design pattern**: Match `subagent-start.sh` — bash, guard with `command -v`,
cap output lines, exit 0 always.

**Test**: `tests/hooks/test_post_compact_hook.py`
- Mock bd/nx commands, verify output ≤ 20 lines
- Verify output contains bead section when beads exist
- Verify output is minimal when no beads/scratch

**Success criteria**:
- [ ] Script at `nx/hooks/scripts/post_compact_hook.sh`, executable
- [ ] Output ≤ 20 lines in all cases
- [ ] Tests pass

## Phase 2: Create StopFailure hook script (nexus-d0ia)

**File**: `nx/hooks/scripts/stop_failure_hook.py`

**Behavior**:
1. Read stdin JSON, extract `error` field
2. Map to known types: `rate_limit`, `authentication_failed`, `billing_error`,
   `invalid_request`, `server_error`, `max_output_tokens`, `unknown`
3. Log via `bd remember "stop-failure-{type}: {details} at {iso-timestamp}"`
4. For `rate_limit` only: `bd create --title="Rate limit hit..." --type=bug --priority=1`
5. Never raise — catch all exceptions, exit 0 always (output ignored anyway)

**Stdin JSON** (from Claude Code):
```json
{
  "session_id": "...",
  "transcript_path": "...",
  "cwd": "...",
  "hook_event_name": "StopFailure",
  "error": "rate_limit",
  "error_details": "...",
  "last_assistant_message": "..."
}
```

**Design pattern**: Match `session_start_hook.py` — Python, `run_command` helper,
graceful error handling, structlog-style debug to stderr.

**No `bd dolt push`** — overkill for a failure hook. Next session's `bd prime` syncs.

**Test**: `tests/hooks/test_stop_failure_hook.py`
- Test all 7 failure types with mock stdin
- Verify `bd remember` called with correct format
- Verify `bd create` called only for `rate_limit`
- Verify graceful handling when `bd` not on PATH

**Success criteria**:
- [ ] Script at `nx/hooks/scripts/stop_failure_hook.py`
- [ ] Handles all 7 failure types
- [ ] `rate_limit` creates blocker bead
- [ ] Never raises unhandled exception
- [ ] Tests pass

## Phase 3: Register hooks in hooks.json (nexus-fy7l)

**File**: `nx/hooks/hooks.json`

**Add two entries**:
```json
"PostCompact": [
  {
    "matcher": "",
    "hooks": [
      {
        "type": "command",
        "command": "bash $CLAUDE_PLUGIN_ROOT/hooks/scripts/post_compact_hook.sh",
        "timeout": 10
      }
    ]
  }
],
"StopFailure": [
  {
    "matcher": "",
    "hooks": [
      {
        "type": "command",
        "command": "python3 $CLAUDE_PLUGIN_ROOT/hooks/scripts/stop_failure_hook.py",
        "timeout": 5
      }
    ]
  }
]
```

**Validation**: `python3 -c "import json; json.load(open('nx/hooks/hooks.json'))"`

**Success criteria**:
- [ ] JSON valid after edits
- [ ] PostCompact entry with 10s timeout
- [ ] StopFailure entry with 5s timeout

## Phase 4: Validate (nexus-ilwk)

**Manual validation**:
1. PostCompact: Run `/compact` with active beads → verify hook fires, output ≤ 20 lines
2. StopFailure: Check `bd memories stop-failure` after next API error

**Success criteria**:
- [ ] PostCompact fires on `/compact`
- [ ] PostCompact output contains in-progress beads
- [ ] StopFailure creates bd memory entry on API failure
