---
id: RDR-024
title: "RDR Process Guardrails: Prevent Implementation Before Gate/Accept"
type: enhancement
status: draft
priority: P2
created: 2026-03-07
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

Add pre-checks at three points in the workflow to catch implementation attempts
on ungated/unaccepted RDRs:

1. **Brainstorming-gate skill** — after design approval, before invoking
   strategic-planning, check if the work references an RDR. If so, verify the
   RDR is in `accepted` status.

2. **Strategic-planner agent** — when creating an implementation plan that
   references an RDR, verify the RDR is `accepted` before proceeding.

3. **Bead creation hook** — when `bd create` references an RDR (in description
   or notes), warn if the RDR is not yet accepted.

## Proposed Solution

### Guardrail 1: Brainstorming-gate skill amendment

Add a section to the brainstorming-gate skill (`nx/skills/brainstorming-gate/SKILL.md`)
between "Present design" and "Save design doc" steps:

```
6a. **RDR status check** — If this work is tracked by an RDR:
    - Run: `nx memory get --project {repo}_rdr --title {id}`
    - If status is not "accepted": STOP. Print:
      "RDR-NNN is still {status}. Run /rdr-gate NNN and /rdr-accept NNN
       before planning implementation."
    - If no RDR exists for this work, proceed normally.
```

### Guardrail 2: Strategic-planner agent pre-check

Add to the strategic-planner agent system prompt (`nx/agents/strategic-planner.md`)
a relay validation step:

```
Before creating an implementation plan, check if the relay references an RDR:
- If an RDR ID is mentioned (e.g., "RDR-023"), verify its status via
  `nx memory get --project {repo}_rdr --title {id}`
- If status is not "accepted", report: "Cannot create implementation plan —
  RDR-NNN is {status}. Gate and accept the RDR first."
- If no RDR is referenced, proceed normally.
```

### Guardrail 3: Bead context hook enhancement

Extend the existing `bead_context_hook.py` (`nx/hooks/scripts/bead_context_hook.py`)
to detect RDR references in newly created beads:

```python
# After bead creation, check if description/notes mention an RDR
rdr_refs = re.findall(r'RDR-(\d+)', bead_description)
for rdr_id in rdr_refs:
    status = get_rdr_status(rdr_id)  # via nx memory get
    if status not in ('accepted', 'closed'):
        print(f"Warning: Bead references RDR-{rdr_id} (status: {status}). "
              f"Consider running /rdr-gate {rdr_id} before implementation.")
```

## Open Questions

**Q1**: Should the guardrails be hard blocks (prevent proceeding) or soft
warnings (print warning but allow override)? Hard blocks are safer but may
frustrate when the RDR lifecycle is intentionally skipped for trivial changes.

**Q2**: Should there be an `--override-rdr-check` flag for cases where the
user deliberately wants to implement before accepting (e.g., prototyping)?

**Q3**: How should the guardrails detect which RDR is associated with the
current work? Options: (a) explicit RDR ID in the relay/bead, (b) keyword
matching against RDR titles, (c) user must specify.

## Success Criteria

- [ ] Brainstorming-gate warns when referenced RDR is not accepted
- [ ] Strategic-planner refuses to create plans for unaccepted RDRs
- [ ] Bead creation warns when referencing unaccepted RDRs
- [ ] Override mechanism exists for intentional process bypasses
- [ ] No false positives for work not tracked by an RDR
