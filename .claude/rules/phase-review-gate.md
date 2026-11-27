---
paths:
  - "docs/rdr/**"
  - "conexus/skills/phase-review-gate/**"
  - "src/nexus/hooks/subagent_start.py"
---

## Phase boundary gate: why nexus keeps it

The plugin's SubagentStart block tells every agent that closes a phase-review bead to walk the `phase-review-gate` skill's checklist first, and ends on a neutral sentence: a dropped item surfaces phases later. The nexus incident behind it lives here, not in the plugin's text: RDR-112 Phase 1 (nexus-52lb) lost days when the T3 daemon drop surfaced three phases later.

When you close a phase-review bead in this repository, list every numbered item of the phase in the RDR and write `Item N = <closing bead id>` or `Item N = none` with a one-line reason for each. Do not close while an item has neither. Nothing enforces this; skipping it is the silent-scope-reduction failure mode (`docs/rdr/AGENTS.md`).
