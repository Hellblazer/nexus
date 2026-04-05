---
title: "RDR-052 Post-Mortem: Catalog-First Query Routing"
rdr: RDR-052
status: closed
close_reason: implemented
closed_date: 2026-04-05
---

# Post-Mortem: RDR-052 Catalog-First Query Routing

## Outcome

**Implemented.** All 13 success criteria met. Epic nexus-zr3u closed (13/13 beads). PR #127.

## What Was Built

- Enhanced `query` MCP tool with 5 catalog-aware params: `author`, `content_type`, `subtree`, `follow_links`, `depth`
- Tumbler hierarchy infrastructure: `depth`, `ancestors()`, `lca()`, `descendants()`, `resolve_chunk()`
- Plan library TTL: `ttl` column, expiry enforcement in `search_plans()`/`list_plans()`, `plan_save(ttl=)` through MCP
- 5 builtin plan templates seeded idempotently at `nx catalog setup`
- Three-path dispatch in `/nx:query` skill: single query → template match → planner
- Query-planner scoped to exception path (extract/compare/generate only)
- Routing diagnostic header in query responses
- Subtree depth guard (document-level addresses rejected with helpful error)
- 252 new test cases across 6 test files

## What Went Well

1. **TDD discipline held.** Tests written before implementation for all Python code. Caught the content_type-only routing bug (FTS5 requires a query string) during test execution, not in production.
2. **Bead tracking worked.** 13 beads across 4 phases with dependency tracking. Critical path was clear: `.1 → .2 → .4 → .5 → .9 → .12 → .13`. Review gates at each phase boundary caught real issues.
3. **Three rounds of substantive critique found real bugs.** Code review found redundant index, hasattr dead code. GST critique found non-functional Path 2 templates (wrong key name), partial plan caching, dual routing logic. Xanadu critique identified span instability and permanence violation — led to RDR-053.

## What Went Wrong

1. **Template schema mismatch shipped.** `_PLAN_TEMPLATES` used `"op"` key; the executor expected `"operation"`. Path 2 was entirely non-functional through two code reviews — caught only by the GST substantive critique. Root cause: no test for template execution path. Fix: added template retrieval tests, rewrote templates with correct schema.
2. **Partial plans cached.** Plans with `outcome="partial"` were saved and re-served for 30 days. Fix: gate cache on `outcome="success"` only.
3. **Didn't use writing-nx-skills skill.** Rewrote `query/SKILL.md` without invoking the skill that documents CI-enforced section requirements. Broke CI (`test_plugin_structure.py`). Fix: restored required sections, updated the skill to be clearer about CI enforcement.
4. **`idx_documents_tumbler` removed then re-added.** Phase 1 code review said "PK covers it" — incorrect for LIKE prefix queries used by `descendants()`. Caught by substantive critique. The reviewer was reasoning about exact-match queries; the new code uses prefix queries.

## Divergences from Spec

- **Routing order changed.** Spec proposed sequential (Path 1 → 2 → 3). GST critique identified that analytical signal words should pre-empt the greedy catalog probe. Implemented as simultaneous evaluation with a routing table (catalog handles × analytical signals).
- **Auto-cache scope narrowed.** Spec said "auto-cache on success." Implementation: only cache `outcome="success"`, not partial. Spec's OQ-2 proposed "only 2+ step plans" — implemented.
- **Follow-links interleaving.** Spec left OQ-3 open. Implementation: interleaved (linked collections merged, ranked by distance). Documented in docstring.

## Lessons

1. **Template schemas must be tested end-to-end.** The key mismatch (`"op"` vs `"operation"`) was invisible to structural review because the templates are JSON strings stored as plan data, not code. Only execution testing catches schema mismatches.
2. **Use the process skills.** `writing-nx-skills` exists specifically to prevent the CI failures I caused. The skill system only works if it's invoked.
3. **Substantive critique from multiple lenses finds different bugs.** Code review (correctness), GST (boundaries/feedback), Xanadu (fidelity/principles) — each caught issues the others missed. Three passes is not overkill for architectural changes.
4. **Reviewer reasoning must match the code path.** "PK covers it" is true for exact-match queries but false for LIKE prefix queries. The reviewer analyzed the wrong query pattern.

## Follow-On

- **RDR-053** (draft): Xanadu Fidelity — tumbler arithmetic (`__lt__`, distance, spans_overlap) and content-addressed spans (`hash:{content_hash}` format). Addresses the three Nelson departures documented in the `Catalog` class docstring.
