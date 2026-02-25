---
name: sequential-thinking
description: Use when approaching complex problems requiring structured reasoning — debugging, analysis, design decisions, or any investigation where you need to form hypotheses, gather evidence, and iterate. Self-contained replacement for the external sequential-thinking MCP server.
---

# Sequential Thinking

Structured hypothesis-driven reasoning for complex problems. Each thought is explicitly numbered, tracked, and annotated — mirroring the discipline of the sequential-thinking MCP server without requiring the external dependency.

## Thought Format

Every thought must open with a header that declares its position and metadata:

```
**Thought N of ~T** [flags]
<content>
```

Where:
- `N` = current thought number (starts at 1, can exceed initial estimate)
- `~T` = current estimate of total thoughts needed (adjust up/down as understanding grows)
- `[flags]` = optional metadata (see below)

**Required fields** (always state, even if values seem obvious):

| Field | What it means |
|-------|---------------|
| `nextThoughtNeeded` | `true` if more thinking is needed; `false` only when fully done |
| `thoughtNumber` | The sequence position of this thought |
| `totalThoughts` | Your current best estimate of total thoughts needed |

**Optional flags** (include only when applicable):

| Flag | When to use |
|------|-------------|
| `[REVISION of Thought N]` | When reconsidering or correcting a prior thought |
| `[BRANCH from Thought N — branchId]` | When exploring an alternative path |
| `[needsMoreThoughts]` | When you reach what seemed like the end but realize more is needed |

## The Pattern

**Thought 1 of ~5** — Frame the problem
State the question clearly. What are you trying to determine?
`nextThoughtNeeded: true`

**Thought 2 of ~5** — Form initial hypothesis
State your best current hypothesis. Be specific.
`nextThoughtNeeded: true`

**Thought 3 of ~5** — Identify evidence needed
What would confirm or refute this hypothesis? What do you need to check?
`nextThoughtNeeded: true`

**Thought 4 of ~5** — Gather evidence
Actually check: read code, run commands, search, inspect logs.
`nextThoughtNeeded: true`

**Thought 5 of ~5** — Evaluate and conclude
Does the evidence support the hypothesis? State clearly: supported / partially supported / refuted.
- If **supported**: derive next steps or conclusion. Set `nextThoughtNeeded: false`.
- If **refuted**: state why, form a revised hypothesis. Add `[REVISION of Thought 2]` to next thought. Update `~T` if needed.
- If **partially**: identify the gap. Add more thoughts.
`nextThoughtNeeded: true/false`

## Example with Revision

```
**Thought 1 of ~4**
Question: Why is the frecency score for file X unusually high after re-indexing?
nextThoughtNeeded: true

**Thought 2 of ~4**
Hypothesis: The re-indexing path adds a new frecency record without clearing the old one, causing accumulation.
nextThoughtNeeded: true

**Thought 3 of ~4**
Evidence needed: Check the reindex() method for DELETE-before-INSERT vs raw INSERT behavior.
nextThoughtNeeded: true

**Thought 4 of ~6** [needsMoreThoughts]
Evidence: reindex() calls upsert(), not delete+insert. frecency is additive in upsert logic.
This partially supports the hypothesis but the root is in upsert, not a missing delete.
Revising to focus on the upsert merge strategy. Need to check how hit_count accumulates.
nextThoughtNeeded: true

**Thought 5 of ~6** [REVISION of Thought 2]
Revised hypothesis: upsert() merges hit_count additively on conflict, so re-indexing doubles the score.
nextThoughtNeeded: true

**Thought 6 of ~6**
Evidence: Confirmed — upsert uses ON CONFLICT DO UPDATE SET hit_count = hit_count + excluded.hit_count.
Conclusion: Replace with hit_count = excluded.hit_count on re-index, or reset before re-indexing.
nextThoughtNeeded: false
```

## Rules

- **Never skip to conclusion** — work through each thought explicitly
- **One hypothesis at a time** — don't hedge with multiple "it could be X or Y"
- **Evidence before evaluation** — gather first, evaluate after
- **State nextThoughtNeeded explicitly** — make continuation/completion unambiguous
- **Adjust totalThoughts freely** — it's an estimate; update it when scope changes
- **Name revisions** — `[REVISION of Thought N]` makes branching visible
- **Use branches sparingly** — only when genuinely exploring an alternative path, not just adding a thought
- **Stop when done** — set `nextThoughtNeeded: false` only when you have a clear, satisfactory conclusion

## When to Use

- Debugging: form hypothesis about root cause before reading code
- Architecture decisions: evaluate trade-offs systematically
- Performance analysis: hypothesize bottleneck, measure, iterate
- Feature design: explore alternatives before committing
- Any investigation where you might be wrong on first instinct

## Surviving Context Compaction

Active thought chains are lost when context is compacted. Two mechanisms protect them:

**During compaction (preferred):** If you know compaction is coming, run:
```
/compact preserve the active sequential thinking chain in full detail
```
The compaction LLM receives this as an instruction and keeps the chain verbatim in the summary.

**After automatic compaction:** The PreCompact hook saves the chain to T2 before compaction fires. The chain is re-injected automatically on your next message (via the UserPromptSubmit hook) and on the next session start.
