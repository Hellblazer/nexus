---
name: document
description: Use when authoring or auditing documentation against existing coverage — tries the document plan library first (search prose follow_links=cites + search code → documentation-for traversal → compare), falls through to /nx:query if nothing matches
effort: medium
---

# document

**You MUST call `nx_answer` for documentation-coverage questions. Direct
`search` calls against docs corpora skip the cross-reference traversal
that produces coverage gaps — you will miss the structure the skill
exists to surface.**

## The call

```
mcp__plugin_nx_nexus__nx_answer(
    question=<caller's phrasing>,
    dimensions={"verb": "document"},
    context=<area + limit — as JSON string if needed>,
)
```

One tool call. `nx_answer` handles match → run → record. Operator chains
inside document-verb plans (extract + compare) bundle into a single
`claude -p` subprocess — substantially faster than per-step isolation.
Plan-miss falls through to an inline `claude -p` planner.

## Required bindings

- `area` — the subject area being documented or audited for
  doc-coverage gaps.

## Optional bindings

- `limit` — per-corpus result cap (`default_bindings` → 10).

## Typical intent shapes

- "audit doc coverage for the taxonomy pipeline"
- "what existing RDRs should I cite when documenting X?"
- "find doc-coverage gaps in the auth module"

## When direct `search` is fine

A simple "where is X documented" lookup — one corpus, one keyword
query — is fine via `mcp__plugin_nx_nexus__search`. This skill earns
its cost when the question requires a cites-graph walk (finding
undocumented areas) or multi-corpus alignment (code + docs coverage
comparison).

## Anti-patterns (do not do any of these)

- **Calling `search` directly for a coverage-gap question.** Coverage
  questions need the cites-graph traversal (docs linking to code,
  code linking to RDRs). `search` returns top-K chunks; it doesn't
  walk the graph. For coverage, you need `nx_answer`.
- **Calling `plan_match` directly instead of `nx_answer`.** You lose
  the record step, the miss-path inline-planner fallback, and the
  use_count telemetry on the matched plan.
- **Using `document` for new writing.** The plan surveys existing
  coverage and flags gaps; actually authoring new prose belongs to
  the user + the caller's downstream tool.
- **Confusing with `research`.** `research` walks from a concept
  to implementing modules (design-first); `document` walks from
  an area to its documentation coverage (coverage-first). Different
  traversal purposes.

See `/nx:plan-first` and `docs/plan-authoring-guide.md`.
