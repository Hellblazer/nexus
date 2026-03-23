---
id: RDR-024
title: "RDR Process Guardrails: Prevent Implementation Before Gate/Accept"
type: enhancement
status: closed
priority: P2
created: 2026-03-07
accepted_date: 2026-03-07
closed_date: 2026-03-07
close_reason: implemented
reviewed_by: self
related_issues: ["RDR-023"]
---

# RDR-024: RDR Process Guardrails

## Problem Statement

During RDR-023 (Agent Tool Permissions), implementation was completed before
the formal RDR lifecycle was followed. The sequence was: create draft → design
→ implement → retroactively gate → retroactively accept → close. The correct
sequence requires gate and accept before implementation begins.

This happened because the brainstorming-gate skill transitions directly to
strategic-planning after design approval, with no check for whether the work
is tracked by an RDR that needs to pass through the gate/accept lifecycle first.

**Root cause**: No automated checkpoint exists between "design approved" and
"implementation begins" that verifies RDR status when an RDR exists.

## Scope

Add soft-warning pre-checks at three points in the workflow to catch
implementation attempts on ungated/unaccepted RDRs:

1. **Brainstorming-gate skill** — after design approval, before invoking
   strategic-planning, scan for RDR references and check status.

2. **Strategic-planner agent** — when creating an implementation plan,
   scan relay for RDR references and warn if not accepted.

3. **Bead creation hook** — when `bd create` description mentions an RDR,
   warn if the RDR is not yet accepted.

## Research Findings

### Finding 1: Brainstorming-gate has clear insertion point

The skill checklist (steps 5-7) provides a natural insertion between "Present
design / User approves" and "Write design doc / Invoke strategic-planning".
A new step 6a fits cleanly. The relay template already has structured fields
(Task, Bead, Input Artifacts) that can be scanned for RDR references.

### Finding 2: Strategic-planner relay reception is extensible

The agent already validates 5 relay fields in its Relay Reception section
(lines 20-36). Adding an RDR status check as step 6 is consistent with the
existing pattern. This is the most reliable guardrail because it runs after
brainstorming-gate, providing defense-in-depth.

### Finding 3: Bead hook is feasible but should stay simple

The existing `bead_context_hook.py` is 36 lines with no subprocess
infrastructure. Adding `nx memory get` as a subprocess call adds 1-2s latency
to every `bd create`. **Recommendation**: regex-only warning (detect `RDR-\d+`
pattern, warn to check status) without actually querying T2. This is zero
latency and still surfaces the reminder.

### Finding 4: Skills and prompts are advisory — soft warnings are the ceiling

Claude Code does not enforce skill instructions as hard gates. A skill saying
"STOP" can be overridden by the user or ignored by the model. The only true
hard block mechanism is a PreToolUse hook returning "deny". For skills and
agent prompts, soft warnings (print message, ask user to confirm) are the
realistic enforcement level.

### Finding 5: Regex RDR detection is sufficient

RDR IDs follow pattern `RDR-NNN` which is trivially matchable with regex
`r'RDR-(\d+)'`. In the RDR-023 session, the relay included "RDR-023" in the
task description and design doc filename. Convention already exists — no new
detection mechanism needed.

## Proposed Solution

### Guardrail 1: Brainstorming-gate skill amendment

Add a section to the brainstorming-gate skill (`nx/skills/brainstorming-gate/SKILL.md`)
between "Present design" and "Save design doc" steps. Uses passive detection
(regex scan) rather than requiring prior knowledge of an RDR:

```
6a. **RDR status check** — Scan the relay task, bead description, and user
    request for the pattern `RDR-\d+`. For each match:
    - Run: `nx memory get --project {repo}_rdr --title NNN`
    - If status is not "accepted" or "closed": warn the user:
      "RDR-NNN is still {status}. Run /nx:rdr-gate NNN and /nx:rdr-accept NNN
       before planning implementation."
    - If the lookup fails or returns no result, warn and proceed (fail-open).
    - If no RDR pattern is found, proceed normally.
```

### Guardrail 2: Strategic-planner agent pre-check

Add to the strategic-planner agent system prompt (`nx/agents/strategic-planner.md`)
relay validation step 6, after existing field checks:

```
6. **RDR status check** — Scan the relay Task field and Input Artifacts for
   the pattern `RDR-\d+`. For each match:
   - Run: `nx memory get --project {repo}_rdr --title NNN`
   - If status is not "accepted" or "closed", warn: "RDR-NNN is {status}.
     Consider running /nx:rdr-gate NNN and /nx:rdr-accept NNN first."
   - If the lookup fails, warn and proceed (fail-open).
   - If no RDR pattern found, proceed normally.
```

### Guardrail 3: Bead context hook enhancement

Extend `bead_context_hook.py` (`nx/hooks/scripts/bead_context_hook.py`) to
detect RDR references. **Regex-only, no T2 lookup** (zero latency):

```python
import re

# After bead creation, scan description for RDR references
rdr_refs = re.findall(r'RDR-(\d+)', command)
if rdr_refs:
    rdr_list = ', '.join(f'RDR-{r}' for r in rdr_refs)
    result = {
        "message": f"Bead references {rdr_list}. "
                   f"Verify RDR status before implementation: "
                   f"/nx:rdr-show {rdr_refs[0]}"
    }
    print(json.dumps(result))
```

## Resolved Questions

**Q1** (hard blocks vs soft warnings): **Soft warnings only.** Skills and agent
prompts are advisory — Claude Code cannot enforce hard blocks at this layer.
The user can always say "proceed anyway." This is acceptable: the goal is to
make the agent aware of the RDR status so it flags the issue, not to prevent
all possible bypasses.

**Q2** (override flag): **Not needed.** Since all guardrails are soft warnings,
there is nothing to override. The user simply proceeds if they choose to.

**Q3** (RDR detection mechanism): **Regex `RDR-(\d+)` on relay fields and bead
descriptions.** This pattern is already convention. No keyword matching or user
specification needed.

## Design Notes

**Fail-open policy**: All three guardrails fail open. If `nx memory get` is
unavailable, returns no result, or the T2 project name is wrong, the guardrail
warns and proceeds rather than blocking. Advisory checks should never break
workflows.

**T2 storage convention**: Guardrails 1 and 2 assume RDR status is stored in T2
under `nx memory get --project {repo}_rdr --title NNN`. This is the convention
used by `/nx:rdr-gate` and `/nx:rdr-accept`. If the T2 record doesn't exist, the
guardrail falls back to "warn and proceed."

## Success Criteria

- [ ] Brainstorming-gate scans for `RDR-\d+` and warns when status is not accepted
- [ ] Strategic-planner scans relay for `RDR-\d+` and warns when status is not accepted
- [ ] Bead creation hook detects `RDR-\d+` in description and prints reminder
- [ ] All guardrails fail open (no workflow breakage when T2 is unavailable)
- [ ] No false positives for work not tracked by an RDR
