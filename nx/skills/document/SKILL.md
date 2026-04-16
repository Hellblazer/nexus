---
name: document
description: Use when authoring or auditing documentation against existing coverage — tries the document plan library first (search prose follow_links=cites + search code → documentation-for traversal → compare), falls through to /nx:query if nothing matches
effort: medium
---

# document

Pure verb skill. Routes through `nx_answer` with `dimensions={verb: "document"}`
so the plan-match gate narrows to document templates and the full trunk runs
in one tool call.

## Flow

```
mcp__plugin_nx_nexus__nx_answer(
    question=<caller's phrasing>,
    dimensions={"verb": "document"},
    context=<area + limit — as JSON string if needed>,
)
```

`nx_answer` handles match → run → record. Plan-miss falls through to an
inline `claude -p` planner.

## Required bindings

- `area` — the subject area being documented or audited for
  doc-coverage gaps.

## Optional bindings

- `limit` — per-corpus result cap (`default_bindings` → 10).

## Typical intent shapes

- "audit doc coverage for the taxonomy pipeline"
- "what existing RDRs should I cite when documenting X?"
- "find doc-coverage gaps in the auth module"

## Anti-patterns

- **Calling `plan_match` directly instead of `nx_answer`.** You lose
  the record step and the miss-path inline-planner fallback.
- **Using `document` for new writing.** The plan surveys existing
  coverage and flags gaps; actually authoring new prose belongs to
  the user + the caller's downstream tool.
- **Confusing with `research`.** `research` walks from a concept
  to implementing modules (design-first); `document` walks from
  an area to its documentation coverage (coverage-first). Different
  traversal purposes.

See `/nx:plan-first` and `docs/plan-authoring-guide.md`.
