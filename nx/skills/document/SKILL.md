---
name: document
description: Use when authoring or auditing documentation against existing coverage — tries the document plan library first (search prose follow_links=cites + search code → documentation-for traversal → compare), falls through to /nx:query if nothing matches
effort: medium
---

# document

Pure verb skill. One-shot: `plan_match` with `verb=document` →
`plan_run` → return final.

## Flow

```
plan_match(
    intent=<caller's phrasing>,
    dimensions={verb: "document"},
    min_confidence=0.85,
    n=1,
)
→ if match: plan_run(match, bindings={area: <topic>, limit: 10})
→ else: /nx:query <caller's intent>
```

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

- **Using `document` for new writing.** The plan surveys existing
  coverage and flags gaps; actually authoring new prose belongs to
  the user + the caller's downstream tool.
- **Confusing with `research`.** `research` walks from a concept
  to implementing modules (design-first); `document` walks from
  an area to its documentation coverage (coverage-first). Different
  traversal purposes.

See `/nx:plan-first` and `docs/plan-authoring-guide.md`.
