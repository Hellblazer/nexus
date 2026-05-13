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

3. Trigger each hook type at least once in a live session:

   - **`SubagentStop`** — `Agent({subagent_type: "general-purpose", prompt: "say hi", description: "spike trigger"})` and wait for return.
   - **`UserPromptSubmit`** — fires on every user turn; one prompt is enough.
   - **`PreCompact`** — type `/compact` in the session.
   - **`Notification`** — wait for an idle-prompt notification, OR force one via a request that triggers a permission prompt.

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
