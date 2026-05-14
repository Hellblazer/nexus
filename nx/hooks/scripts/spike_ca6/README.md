# RDR-111 CA-6 spike (nexus-1h26) — payload capture for 4 inferred hook types

Logging-only hooks for the four hook types whose payloads are marked
`[inferred]` in RDR-111 §RF-1: `SubagentStop`, `UserPromptSubmit`,
`PreCompact`, `Notification`. The bridge for these four types cannot
ship until the empirical payloads are reconciled with the RF-1
inventory.

## How to run

1. Pick a scratch Claude Code session. The capture dir defaults to
   `~/.config/nexus/spike-ca6/`; override with
   `NX_SPIKE_CAPTURE_DIR=$(pwd)/spike-out`.

2. Add the four hooks to that session's `~/.claude/settings.json`
   (replace `<NEXUS>` with the repo path):

   ```json
   {
     "hooks": {
       "SubagentStop": [
         { "hooks": [{ "type": "command",
           "command": "python3 <NEXUS>/nx/hooks/scripts/spike_ca6/log_payload.py SubagentStop" }] }
       ],
       "UserPromptSubmit": [
         { "hooks": [{ "type": "command",
           "command": "python3 <NEXUS>/nx/hooks/scripts/spike_ca6/log_payload.py UserPromptSubmit" }] }
       ],
       "PreCompact": [
         { "hooks": [{ "type": "command",
           "command": "python3 <NEXUS>/nx/hooks/scripts/spike_ca6/log_payload.py PreCompact" }] }
       ],
       "Notification": [
         { "hooks": [{ "type": "command",
           "command": "python3 <NEXUS>/nx/hooks/scripts/spike_ca6/log_payload.py Notification" }] }
       ]
     }
   }
   ```

3. Trigger each hook type at least once in a live session. Two need
   explicit setup or they will not fire within the spike budget:

   - **`SubagentStop`** — `Agent({subagent_type: "general-purpose", prompt: "say hi", description: "spike trigger"})` and wait for return.
   - **`UserPromptSubmit`** — fires on every user turn; one prompt is enough.
   - **`PreCompact`** — `/compact` only fires the hook if the session
     has enough context to actually compact. A scratch session with only
     the three triggers above is below threshold and `/compact` will
     no-op. Pre-fill the context: paste a large block of text (5-10 K
     tokens) or run several tool-heavy prompts, then `/compact`. If
     `PreCompact.jsonl` stays empty, the session was under threshold;
     add more context and try again.
   - **`Notification`** — wait-for-idle is unreliable inside the spike
     budget. The reliable path is a permission prompt: set
     `"permissions": {"defaultMode": "default"}` (NOT auto-approve),
     then issue a Bash tool call that requires approval, e.g.
     `Bash(command="echo notification-trigger")`. Claude Code escalates
     to a permission prompt, which fires the Notification hook.
     **Watch for**: any `Bash(*)` (or similar pre-approval) in your
     user-global `~/.claude/settings.json` short-circuits the prompt
     and silently defeats this trigger. Temporarily comment out the
     allow rule for the duration of the spike (restore it after).
     A clean `$HOME` would break OAuth auth — `.credentials.json`
     lives under `~/.claude/` and the temp home has none.

4. After each trigger, check `$NX_SPIKE_CAPTURE_DIR/<type>.jsonl`. The
   `payload` field is the verbatim stdin JSON Claude Code piped to the
   hook.

## Reconcile with RDR-111 §RF-1

For each of the four types, compare the captured `payload` against
the RF-1 inventory entry. Outcomes:

- **Field-for-field match** — flip the RF-1 row from `[inferred]` to
  verified; bridge script can be authored against the documented
  shape.
- **Field discrepancy that does not change semantics** — update RF-1
  with the verified shape, note the discrepancy.
- **Field discrepancy that changes bridge mapping** — pause and flag.
  Bridge for that hook type cannot ship until reconciled. Decide
  whether the issue is an RF-1 schema mistake or a bridge-mapping
  question.

## Persist to T2

One memo per type:

```
mcp__plugin_nx_nexus__memory_put project=nexus_rdr
  title=111-research-CA-6-payloads-SubagentStop
  content=<json + analysis>
  tags=rdr-111,spike,CA-6,SubagentStop
  ttl=0
```

Repeat for the other three types.

## When the spike is complete

- Four T2 memos exist: `111-research-CA-6-payloads-{SubagentStop,UserPromptSubmit,PreCompact,Notification}`.
- RDR-111 §RF-1 has no remaining `[inferred]` rows for these four types.
- The bead `nexus-1h26` is closed.
- This directory (`nx/hooks/scripts/spike_ca6/`) can be deleted once
  the bead is closed; the scaffolding is throwaway by design.
