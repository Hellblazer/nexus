---
name: sequential-thinking
description: Use when approaching complex problems requiring structured reasoning — debugging, analysis, design decisions, or any investigation where you need to form hypotheses, gather evidence, and iterate. Self-contained replacement for the external sequential-thinking MCP server.
---

# Sequential Thinking

Structured hypothesis-driven reasoning for complex problems. Uses `nx thought add` as the tool call mechanism — each call returns the **full accumulated chain** from T2 storage, so the complete investigation context is always in the tool result and survives context compaction.

## How to Use

For each thought, make a Bash tool call:

```bash
nx thought add "**Thought N of ~T** [flags]
<content>
nextThoughtNeeded: true|false"
```

The command appends the thought to session-scoped T2 storage and returns the complete chain. Even after compaction, the next `nx thought add` re-surfaces all previous thoughts automatically — identical to the sequential-thinking MCP server's behaviour.

To see the current chain without adding a thought:
```bash
nx thought show
```

To close a completed chain:
```bash
nx thought close
```

## Thought Format

Every thought must open with a header declaring its position and metadata:

```
**Thought N of ~T** [flags]
<content>
nextThoughtNeeded: true|false
```

Where:
- `N` = current thought number (starts at 1, can exceed initial estimate)
- `~T` = current estimate of total thoughts needed (adjust freely as understanding grows)
- `[flags]` = optional inline metadata (see below)

**Required fields** (always state explicitly):

| Field | What it means |
|-------|---------------|
| `nextThoughtNeeded` | `true` if more thinking needed; `false` only when fully done |
| `thoughtNumber` (`N`) | The sequence position |
| `totalThoughts` (`~T`) | Current best estimate of total thoughts needed |

**Optional flags** (include only when applicable):

| Flag | When to use |
|------|-------------|
| `[REVISION of Thought N]` | When reconsidering or correcting a prior thought |
| `[BRANCH from Thought N — branchId]` | When exploring an alternative path |
| `[needsMoreThoughts]` | When reaching what seemed like the end but more is needed |

## Example Session

```bash
nx thought add "**Thought 1 of ~5**
Frame: Why is the frecency score doubling after re-indexing?
nextThoughtNeeded: true"
```
```
Chain: 20260225-143022  (1 thought)
════════════════════════════════════════════════════
**Thought 1 of ~5**
Frame: Why is the frecency score doubling after re-indexing?
nextThoughtNeeded: true
════════════════════════════════════════════════════
Next: nx thought add "**Thought 2 of ~5** ..."
```

```bash
nx thought add "**Thought 2 of ~5**
Hypothesis: reindex() adds a record without clearing the old one.
nextThoughtNeeded: true"
```

*[... compaction fires here — conversation context compressed ...]*

```bash
nx thought add "**Thought 3 of ~6** [needsMoreThoughts]
Evidence: upsert() merges hit_count additively on conflict. Partial support.
Revising to focus on upsert merge strategy.
nextThoughtNeeded: true"
```
```
Chain: 20260225-143022  (3 thoughts)
════════════════════════════════════════════════════
**Thought 1 of ~5**
Frame: Why is the frecency score doubling after re-indexing?
nextThoughtNeeded: true

**Thought 2 of ~5**
Hypothesis: reindex() adds a record without clearing the old one.
nextThoughtNeeded: true

**Thought 3 of ~6** [needsMoreThoughts]
Evidence: upsert() merges hit_count additively on conflict. Partial support.
Revising to focus on upsert merge strategy.
nextThoughtNeeded: true
════════════════════════════════════════════════════
Next: nx thought add "**Thought 4 of ~6** ..."
```

All three thoughts are present after compaction because they came from T2, not from the conversation context.

## Rules

- **Always use `nx thought add`** — never write thoughts only in conversation text; the tool call is what makes them compaction-resilient
- **Never skip to conclusion** — work through each thought explicitly
- **One hypothesis at a time** — don't hedge with "it could be X or Y"
- **Evidence before evaluation** — gather first, evaluate after
- **State `nextThoughtNeeded` explicitly** — makes continuation unambiguous
- **Adjust `~T` freely** — it's an estimate; update it when scope changes
- **Name revisions** — `[REVISION of Thought N]` makes branching visible
- **Close when done** — run `nx thought close` when `nextThoughtNeeded: false`

## When to Use

- Debugging: form hypothesis about root cause before reading code
- Architecture decisions: evaluate trade-offs systematically
- Performance analysis: hypothesize bottleneck, measure, iterate
- Feature design: explore alternatives before committing
- Any investigation where you might be wrong on first instinct

## Context Compaction

No special action needed. The chain is stored in T2 (external to Claude's context window). Each `nx thought add` retrieves and returns the full chain regardless of what has been compacted. The mechanism is identical to how the sequential-thinking MCP server works — state lives outside the conversation.

For deliberate compaction with custom instructions:
```
/compact preserve the active sequential thinking chain in full detail
```
