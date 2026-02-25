---
name: sequential-thinking
description: Use when approaching complex problems requiring structured reasoning — debugging, analysis, design decisions, or any investigation where you need to form hypotheses, gather evidence, and iterate. Self-contained replacement for the external sequential-thinking MCP server.
---

# Sequential Thinking

Structured hypothesis-driven reasoning for complex problems. Use this pattern when the answer isn't obvious and requires evidence-gathering and iteration.

## The Pattern

Work through the problem in explicit numbered thoughts:

**Thought 1 — Frame the problem**
State the question clearly. What are you trying to determine?

**Thought 2 — Form initial hypothesis**
State your best current hypothesis. Be specific.

**Thought 3 — Identify evidence needed**
What would confirm or refute this hypothesis? What do you need to check?

**Thought 4 — Gather evidence**
Actually check: read code, run commands, search, inspect logs.

**Thought 5 — Evaluate**
Does the evidence support the hypothesis? State clearly: supported / partially supported / refuted.

**Thought 6+ — Iterate or conclude**
- If **refuted**: state why, form a revised hypothesis, return to Thought 2
- If **supported**: derive next steps or conclusion
- If **partially**: identify the gap, gather more evidence

## Rules

- **Never skip to conclusion** — work through each thought explicitly
- **One hypothesis at a time** — don't hedge with multiple "it could be X or Y"
- **Evidence before evaluation** — gather first, evaluate after
- **Name revisions** — "Revised hypothesis (after Thought 5):" makes branching visible
- **Stop when done** — don't continue past a clear conclusion

## When to Use

- Debugging: form hypothesis about root cause before reading code
- Architecture decisions: evaluate trade-offs systematically
- Performance analysis: hypothesize bottleneck, measure, iterate
- Feature design: explore alternatives before committing
- Any investigation where you might be wrong on first instinct
