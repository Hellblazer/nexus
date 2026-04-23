---
title: "RDR-092: Plan Match-Text from Dimensional Identity"
id: RDR-092
type: Feature
status: accepted
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-22
accepted_date: 2026-04-23
related_issues: []
related_tests: [test_plan_match.py, test_plan_library.py, test_plan_session_cache.py]
related: [RDR-078, RDR-080, RDR-091]
---

# RDR-092: Plan Match-Text from Dimensional Identity

## Problem Statement

The plan matcher embeds the author's prose `query` string into `plans__session` at session start (`src/nexus/plans/session_cache.py:178`), then cosine-searches incoming questions against those embeddings. The author's prose is doing double duty: it labels the plan for humans, and it is the entire matching surface for the embedder.

That surface is over-broad. MiniLM-384 encodes an anchor like *"How does plan-match-first retrieval work in RDR-078?"* as a vector whose nearest-neighborhood in this project's embedding space is dominated by one token: *retrieval*. "plan", "work", and "How does" are generic connective tissue. "RDR-078" barely moves the vector because the model never saw the token. The result is one plan acting as an attractor for any incoming question that orbits the same general region.

Empirical evidence from the live `plan_library` on this repository (2026-04-22 snapshot):

- *"How does plan-match-first retrieval work in RDR-078?"* has `match_count = 139`. The top-5 matcher has returned this plan 139 times across all incoming questions, counting paraphrases, sibling questions, and test probes.
- *"just a random hello query"* probed against the current library lands plan #38 at rank 1 with cos=0.341, 6x above the unrelated-text background floor (~0.05). A nonsense probe can dominate a genuinely unrelated plan, and the dominant plan gets its counter bumped anyway.
- The sum of `match_count` across the top ten plans is 662. Direct `plan_search` by hand over the same corpus: 37 calls. The matcher is the dominant consumer; it consumes an over-broad surface.

The RDR-078 dimensional identity columns (`verb`, `scope`, `dimensions`, `scope_tags`, `name`) already exist on every plan in the schema (`plan_library.py:74-104`). They encode the plan's typed identity cleanly. They are not in the matching path.

#### Gap 1: T1 cosine path embeds raw prose

`src/nexus/plans/matcher.py:162` embeds the raw `query` string into `plans__session` at session start and cosine-matches at runtime. The dimensional identity columns already populated on every row never enter the embedding. Scope-aware re-ranking (RDR-091) is a post-filter over the cosine result, not a change to the embedding surface. Result: the T1 lane inherits the attractor-breadth problem above.

#### Gap 2: T2 FTS5 fallback indexes the same raw prose

`plan_library.py::search_plans` keyword-matches over `query + tags + project` via the `plans_fts` virtual table. Same prose column, same attractor surface, different retrieval math. Both matcher lanes key off the same under-discriminative payload.

#### Gap 3: Dimensional columns empty in production

R3 verified 0/52 live-DB plans have populated `verb` / `name` / `dimensions`. Three upstream causes:

1. Legacy builtin seed at `src/nexus/commands/catalog.py:107` calls `db.save_plan(query, plan_json, tags)` without dimensional arguments.
2. Grown-plan path at `src/nexus/mcp/core.py:2397` passes only `scope`, `scope_tags`, `tags` (all 5 grown plans non-dimensional).
3. The RDR-078 scoped loader at `src/nexus/plans/seed_loader.py:158` is dimensional-correct but its 9 would-be rows are absent from the live DB — loader never re-ran after landing.

Even a perfect embedding synthesiser can only project from what exists; populating the columns is the precondition for every downstream fix.

#### Gap 4: Loader failures are silent

The scoped loader's error path (`_seed_plan_templates` around `catalog.py:131`) logs via `_log.warning` only. A misrouted `plugin_root` or missing YAMLs produces no user-visible failure during `nx catalog setup`; the empty global tier state from R3 + R5 persists indefinitely. The close-time `nx doctor --check-plan-library` surface + fail-loud setup raise are the observability lever.

## Context

### Technical Environment

- `plans` table (`plan_library.py:74`): `query TEXT NOT NULL` is the raw author string. Dimensional columns (`verb`, `scope`, `dimensions`, `scope_tags`, `name`) were added by RDR-078's `_add_plan_dimensional_identity` migration (4.4.0) and are populated on every new save via `save_plan`.
- `plans__session` ChromaDB collection: created at session start by `PlanSessionCache`; upserts one document per plan with `document=query`, embedding function `LocalEmbeddingFunction` (MiniLM-L6-v2 ONNX, 384 dims). Cosine similarity drives `plan_match` top-N results.
- `plans_fts` FTS5 virtual table (`plan_library.py:114`): index columns `query, tags, project`. The fallback path searches over the same prose.
- Matcher integration: `plan_match` (`matcher.py`) returns `Match(plan_id, confidence)` pairs. `increment_match_metrics` (`plan_library.py:360`) bumps `match_count` on every returned candidate.
- Post-match filters: RDR-091 added `_scope_fit` re-ranking with `_SCOPE_FIT_WEIGHT = 0.15` and specificity tie-breakers. These operate on the cosine result, not on the embedding input.

### Research Findings

All four targets executed 2026-04-22. Full findings in T2: `nx memory get --project nexus_rdr --title 092-research-{1..4}`.

**R1: MiniLM-384 ablation (T2: `092-research-1`).** The author's prose is mostly noise in the embedding space. Stripping "RDR-078" from the 144-count anchor costs 0.097 cosine (10%); dropping "How does ... work" costs 0.058 (6%); dropping both costs 0.18 (18%). The single word "retrieval" retains 0.51 of the baseline signal, one token carrying half. Assumption A1 confirmed.

**R2: Top-5 composition (T2: `092-research-2`).** Plan #38 (the 144-count row) lands in top-5 for 4 of 10 synthetic probes, including "just a random hello query" at rank 1 (cos=0.341, 6x background floor). The plan's anchor vector is a strong enough attractor to dominate unrelated probes. Aside: the Tokyo row ("What is the weather in Tokyo right now?") matches only its own stored anchor (cos=1.000, all others ≤ 0.052). Its 24 `match_count` hits are re-runs of the same literal probe, not false positives on some other plan. Tokyo is an isolated self-match row, not attractor-breadth. (A separate blog-draft `blog/post-6-nx-plugin-configuration-that-composes.md` mischaracterizes this row and will be corrected; that correction is not part of RDR-092's scope.)

**R3: Dimensional coverage in production (T2: `092-research-3`). CRITICAL.** Queried the live `memory.db` plans table: 0/52 plans have populated `verb`, `name`, or `dimensions`. Only 13/52 have `scope_tags` (from the RDR-091 backfill). Total `match_count` across the library is 802; total `use_count` is **zero**. Upstream causes:
- Legacy builtin seed at `src/nexus/commands/catalog.py:107` calls `db.save_plan(query, plan_json, tags)` without dimensional arguments.
- Grown-plan path at `src/nexus/mcp/core.py:2397` passes only `scope`, `scope_tags`, `tags`. All 5 grown plans are non-dimensional.
- The RDR-078 scoped loader at `src/nexus/plans/seed_loader.py:158` DOES pass full dimensional columns, but its 9 would-be rows (analyze-default, debug-default, etc.) are absent from the live DB. Loader tested in isolation and works; either it never ran, or errors were swallowed.

Assumption A2 **falsified**. The proposed solution as originally scoped (synthesize from existing dimensional columns) cannot work because the columns are empty. The RDR must pivot.

**R4: Empirical effect of alternative anchors (T2: `092-research-4`).** Tested 5 anchor candidates for plan #38 against the same 10-probe set:
- Raw (today): 4/10 landings, mean rank 16.2.
- Strip-prose: 4/10, mean 13.8. Canonicalization alone captures most of the improvement.
- Synth word form: 4/10, mean 16.9.
- Synth terse: 4/10, mean 17.4.
- Synth JSON-style: 3/10, mean 25.0. Fewer landings, but "banana bread recipe" newly enters top-5 via shared tokens like "dims=" and "scope=". Synthesis can create new attractors, not just narrower ones.

Assumption A3 partially falsified. Synthesis improves precision marginally, but the real bottleneck is MiniLM-384 + a 52-plan library: near-random probes will always find SOMETHING in top-5. Higher-leverage alternatives:
- Enforce a `min_confidence` threshold (currently supported but not wired to a floor for cosine hits; a 0.5 floor would cut most spurious top-5 landings at query time).
- Grow the library (more genuine near-neighbors per probe).
- Upgrade the embedder (Voyage in `plans__session` would widen on-topic vs off-topic separation).

### Pivot implied by R3 + R4

The RDR as originally scoped proposed a Phase-1 synthesizer at `session_cache._upsert_row` plus a Phase-2 FTS5 column. R3 says there's nothing to synthesize from. R4 says even if there were, the effect size is modest and a `min_confidence` floor plus library growth would dominate.

Scope pivoted to upstream-first (RDR-092B). The Proposed Solution and Implementation Plan sections below now reflect that pivot: Phase 0 populates dimensional columns across all three save paths; Phase 1 does the synthesis as originally proposed; Phase 2 adds a `min_confidence` floor as independent leverage.

**R5: Loader health check (T2: `092-research-5`).** Ran `load_all_tiers` in isolation against the current source tree. Result: 9 rows would insert cleanly (0 errors, 0 skipped). The loader code is healthy. The empty-column state on the live DB is a deployment gap: `nx catalog setup` was last run before the RDR-078 scoped loader landed. Re-running it today would produce the 9 dimensional rows. This reframes Phase 0c: the fail-loud instrumentation is still worth adding as a future-proofing safeguard, but the immediate action is "re-run `nx catalog setup` once Phase 0a lands." Phase 0d (backfill) is still required because `get_plan_by_dimensions` at `seed_loader.py:150` skips only existing-by-dimensions, not existing-by-query; legacy rows sit in the DB forever unless explicitly backfilled.

**R6: Grown-plan inference feasibility (T2: `092-research-6`).** At `core.py:2397`, three-tier inference is feasible with low misclassification risk:
- Tier 1: if the caller passed `dimensions={"verb": "..."}` into `nx_answer` (which the docstring explicitly encourages), propagate that value directly. Zero inference cost.
- Tier 2: otherwise, infer from `plan_json.steps` operator sequence (`compare` → analyze; `extract`+`rank` → analyze; `traverse`+`search`+`summarize` → research; default `search`+`summarize` → research).
- Tier 3: name from kebab-case of the question's first 3-5 content words. Non-unique, acceptable for `scope=personal`.

Note: `purposes.yml` defines link-traversal purposes, not verbs. Verbs live in `dimensions.yml`. Verb routing for grown plans must come from caller-supplied dims or plan_json shape.

**R7: Legacy templates vs YAML builtins (T2: `092-research-7`).** Enumerated the 5 legacy `_PLAN_TEMPLATES` rows. Disposition:
- Migrate 3 to YAML (distinct shapes not covered by existing builtins): "find documents by author" (author-scoped search), "trace citation chain" (citation-traversal), "search within content type" (type-scoped). Each becomes a dimensional template in `nx/plans/builtin/*.yml`.
- Retire 2 as redundant: "trace provenance chain" (overlaps research-default), "compare documents across corpora" (overlaps analyze-default). Drop them entirely.

Net result: `_PLAN_TEMPLATES` array shrinks from 5 to 0; YAML builtin count grows from 9 to 12. The comment at `catalog.py:91` about dimensions=NULL being load-bearing gets removed along with the `_PLAN_TEMPLATES` block.

**R8: Backfill heuristic hit rate (T2: `092-research-8`).** Tested a 13-rule verb-from-stem dictionary against all 52 live plans. Coverage: 31/52 (60%) get a verb with non-zero score; 21/52 (40%) score 0. Main failure modes: "What papers cover X" (ART project queries) need an explicit papers-cover rule; generic wh-questions need a research-default fallback; "research design review" lifecycle phrases need handling because "research" appears as a noun.

With a 20-rule enriched dictionary plus a safe default (`verb=research, strategy=backfill-default` for unmatched wh-questions), expected coverage is >85% confident + 15% low-confidence fallback. Phase 0d backfill rows flagged:
- Non-zero score: `tags += ",backfill"`.
- Zero-score wh-fallback: `tags += ",backfill-low-conf"`.

`nx plan repair` can prioritize low-confidence rows for manual review.

**R9: min_confidence calibration (T2: `092-research-9`).** Tested 6 thresholds against 5 legit + 5 noise probes. Threshold 0.5 is the clean sweet spot: 100% legit recall, 100% noise rejection. 0.3 lets "weather forecast" (cos=0.474 against plan #46) through; 0.6 cuts too many legit matches (recall drops to 60%). Confirms Phase 2's proposed default of 0.5. Caveats: small probe set, MiniLM-specific; recommend re-calibration at Phase 5 validation with a larger probe corpus.

**R10: Phase 1 synthesizer form (T2: `092-research-10`). Revises Phase 1.** Compared three match-text forms against the 9 YAML builtins:
- Form A (raw description): noise floor 0.025, legit signal 0.340.
- Form B (pure synth, e.g. "analyze default scope global"): noise floor 0.014 (44% better), but legit signal drops and legit-verb matches get worse. The rich description carries most of the discriminative signal against natural-language questions; pure-synth collapses that neighborhood.
- Form C (hybrid, description + dimensional suffix): noise floor 0.021 (16% better than raw), legit signal 0.348 (slightly better than raw), zero regression on verb accuracy vs raw.

Conclusion: **Phase 1 should use Form C (hybrid), not pure-synth.** The hybrid form keeps the description as the primary signal and uses dimensional tokens as a suffix boost. This resolves the A4 "no new attractor" risk for synthesis (pure-synth violated it; hybrid does not).

Concrete synthesizer:
```python
def _synthesize_match_text(row):
    query = (row.get('query') or '').strip()
    verb = (row.get('verb') or '').strip()
    name = (row.get('name') or '').strip()
    scope = (row.get('scope_tags') or row.get('scope') or '').strip()
    if not verb and not name:
        return query
    suffix_parts = [p for p in [verb, name] if p]
    if scope:
        suffix_parts.append(f"scope {scope}")
    return f"{query}. {' '.join(suffix_parts)}".strip('. ')
```

Also: the "analyze: tradeoffs" probe mismatches `document-default` in all three forms. That is a description-quality issue in `nx/plans/builtin/document-default.yml`, fixable independently.

**R11: Engineering cost (T2: `092-research-11`).** Source delta ~420 LOC across 9 Python files; 120 LOC of 3 new YAML builtins; 425 LOC of 17 new tests across 7 existing test files; 3 docs files. Wall-clock per phase 19-30 hours focused; 29-45 hours with a 1.5x migration risk multiplier. Revised downward after the Phase 2 gate finding: Phase 2 is no longer new `min_confidence` wiring (the parameter already exists at 0.40 per RDR-079) but a smaller constant revision or a per-call override, 1-2 hours not the originally-estimated 2-3. Adjusted total: ~28-44 hours. Each phase can ship as its own PR. Natural ship order: Phase 0 first now (Phase 2's leverage depends on the Option A/B/C choice and on the size of the Phase 5 probe set), then 1 → 2 → 3 → 4 → 5. If Option B is chosen for Phase 2, ship it after Phase 0 so the measured effect is attributable.

### Critical Assumptions

- **A1: MiniLM-384's topical generalization is the source of attractor breadth.** Assumed based on the empirical `match_count` distribution and the known behavior of sentence-BERT models on short prose. Needs finding R1 above.
- **A2: Dimensional columns are populated densely enough to be the primary match surface.** Needs finding R3. If coverage is thin, the migration path is longer.
- **A3: Typed match-text does not itself become a different kind of over-broad attractor.** Needs finding R4. A synthesized string like `"analyze scope=rdr__* dims={...}"` could be either sharper or just differently attractive.
- **A4: No user-facing regression in `plan_search`.** `plan_search` renders the `query` column as-is for humans. The RDR does not change that; it changes only what gets embedded into `plans__session`.

## Proposed Solution

The pivot implied by research R3 is upstream-first. Phase 0 populates the dimensional columns across all three save paths and via a backfill; Phase 1 does the match-text synthesis originally proposed; Phase 2 adds a `min_confidence` floor as a complementary hardening. All three phases can ship independently if needed.

### Phase 0: populate dimensional columns across all paths

The root cause of the empty columns is three save paths that do not carry dimensional information. Fix each.

#### 0.1 Legacy builtin seed

`src/nexus/commands/catalog.py:107` iterates `_PLAN_TEMPLATES` and calls `db.save_plan(query, plan_json, tags)` only. The 5 legacy builtins land with NULL `verb`/`name`/`dimensions`.

Two options:
- **Option 1 (preferred):** retire `_PLAN_TEMPLATES` and migrate the 5 legacy templates into `nx/plans/builtin/*.yml` files with full dimensional frontmatter. The `seed_loader.load_all_tiers` path already knows how to load them. One legacy-catalog-by-author template; one trace-citation; one trace-provenance; one compare-across-corpora; one find-by-type. Each becomes a YAML with `verb`, `scope`, `strategy`, `dimensions`, `plan_json`.
- **Option 2 (minimal):** keep `_PLAN_TEMPLATES` but add `verb`, `name`, `dimensions` dict entries to each template, and pass them through on `save_plan`. Simpler diff; keeps two template surfaces alive.

Pick Option 1. Fewer surfaces, one loader, dimensional from the source.

#### 0.2 Grown-plan save path

`src/nexus/mcp/core.py:2397` (`grow_plan` inside `nx_answer`) currently passes only `scope`, `scope_tags`, and `tags`. Per R6's three-tier verb-inference cascade:

1. **Caller-supplied dims (tier 1).** If the caller passed `dimensions={"verb": "..."}` into `nx_answer` (verb skills are documented to do this at `core.py:2100-2103`), propagate that value directly. Zero inference cost.
2. **plan_json shape inference (tier 2).** Otherwise, inspect `best.plan_json.steps` operator sequence: `compare` present → verb=analyze; `extract` + `rank` → verb=analyze; `traverse` + `search` + `summarize` → verb=research; flat `search` + `summarize` → verb=research; `debug`-related ops → verb=debug.
3. **Safe fallback (tier 3).** Default `verb=research` for unmatched shapes.

Also:
- `name`: derive from the question. First 3-5 content words, kebab-cased ("plan-match-first-retrieval"). Acceptable for grown plans; human-readable but not globally unique.
- `dimensions`: `{"verb": <tier-resolved>, "scope": "personal", "strategy": "grown"}`.

Clarification: `purposes.yml` resolves link-type sets for the `traverse` operator (`find-implementations`, `reference-chain`, etc., consumed via `resolve_purpose` at `core.py:1636-1655`). It does NOT supply verbs and `nx_answer` does not take a `purpose` parameter. Verb routing for grown plans comes entirely from caller-supplied dims or the plan_json-shape heuristic above.

#### 0.3 Verify `seed_loader` actually runs

R3 showed that `load_all_tiers` works in isolation (would insert 9 dimensional rows from `nx/plans/builtin/*.yml`) but the live DB has 0 such rows. The `nx catalog setup` path at `catalog.py:120` does call `load_all_tiers`, and errors are logged via `_log.warning("rdr078_seed_load_error", ...)` (line 133). Two hypotheses:
- Errors WERE raised per-tier and logged, but `seeded += len(result.inserted)` masked the zero-count case. Likely, since `catalog.py:131-137` does not differentiate "no files found" from "errors swallowed all of them".
- `nx catalog setup` never ran on this machine after the loader landed.

Instrumentation: change `catalog.py:131-137` to raise on tier load errors, or at minimum emit a user-visible warning when `load_all_tiers` produces zero rows. Add a `nx doctor --check-plan-library` subcommand that asserts at least 9 dimensional builtins are present.

#### 0.4 Backfill migration for existing rows

Some number of legacy plans will always predate the loader wiring. Add a migration that, for any plan without a populated `verb`/`dimensions`:
- Heuristically infer `verb` from the query stem (maps `"how does"` → investigate, `"what tradeoffs"` → analyze, `"implement"` → develop, etc.). A small dictionary; documented.
- Set `name` from a kebab-case of the query's content nouns.
- Set `dimensions` as `{"verb": <inferred>, "scope": <inferred or "global">, "strategy": "backfill"}`.
- Flag the row with `tags += ",backfill"` so future audits can see which rows have inferred (vs authored) dimensions.

Idempotent: re-running produces the same output. Authored rows (verb already set) are untouched.

### Phase 1: synthesize `match_text` from dimensional columns (hybrid form per R10)

Once Phase 0 populates the columns, this is the Phase 1 work. R10 showed that pure synthesis (dimensional tokens only) hurts legit matching because it strips the rich description's semantic overlap with natural-language questions. The hybrid form (description + dimensional suffix) improves both noise rejection and legit matching vs raw description.

In `PlanSessionCache._upsert_row` (`src/nexus/plans/session_cache.py:177`), replace `document = row['query']` with:

```python
def _synthesize_match_text(row: dict[str, Any]) -> str:
    query = (row.get("query") or "").strip()
    verb = (row.get("verb") or "").strip()
    name = (row.get("name") or "").strip()
    scope = (row.get("scope_tags") or row.get("scope") or "").strip()
    if not verb and not name:
        return query  # fallback: pre-RDR-078 rows keep raw query
    suffix_parts = [p for p in [verb, name] if p]
    if scope:
        suffix_parts.append(f"scope {scope}")
    return f"{query}. {' '.join(suffix_parts)}".strip(". ")
```

R10 measurements against the 9 YAML builtins:
- Noise floor (mean cosine from noise probes to plans): 0.025 raw → 0.021 hybrid.
- Legit signal (mean cosine from legit probes to correct-verb plan): 0.340 raw → 0.348 hybrid.
- Verb accuracy: zero regression vs raw across the probe set.

Word-prefix format ("scope", "dims") rather than JSON syntax avoids the token-contamination risk R4 identified with the earlier pure-synth draft.

The raw `query` column is unchanged; `plan_search`, `plan_list`, and the `plan_match` MCP tool's display still render it as the human-facing label.

### Phase 2: raise the `min_confidence` floor from 0.40 to 0.50

Correction to an earlier draft: `min_confidence` is already a wired parameter.
- `src/nexus/plans/matcher.py:132` has `min_confidence: float = 0.40` as the parameter default.
- `src/nexus/plans/matcher.py:200-201` drops below-threshold candidates before `increment_match_metrics` runs.
- `src/nexus/mcp/core.py:1726` has `_PLAN_MATCH_MIN_CONFIDENCE: float = 0.40` as the module-level constant.
- `src/nexus/mcp/core.py:2151` passes that constant into `plan_match` from `nx_answer`.

RDR-079 set the 0.40 default as F1-optimal under its calibration. That prior decision is live in the codebase. Phase 2 is NOT new wiring; it is a proposal to revise the constant from 0.40 to 0.50 based on R9's re-measurement, which tested 6 thresholds against 5 legit + 5 noise probes (T2: `092-research-9`):

| threshold | legit recall | noise reject |
|---|---|---|
| 0.3 | 100% | 60% |
| 0.4 | 100% | 80% |
| **0.5** | **100%** | **100%** |
| 0.6 | 60% | 100% |
| 0.7 | 40% | 100% |

R9 shows 0.5 is clean against that probe set but the sample is small (5+5). The threshold revision therefore carries non-trivial risk: raising from 0.40 to 0.50 may silently reduce recall on real traffic in ways the R9 probe set did not surface.

Resolution paths (pick in Phase 2 PR review):
- **Option A (conservative):** keep `_PLAN_MATCH_MIN_CONFIDENCE = 0.40` as the default. Expose `min_confidence` as a per-call parameter on `nx_answer` so verb skills can pin 0.50 where they want precision. Documents R9 as the precision preset. Risk: default behavior unchanged; 4/10 random-hello-style landings continue.
- **Option B (bold):** raise the constant to 0.50. Ship a regression test that verifies the legit probe set still finds matches. Acknowledge the R9 sample size in commit message and defer full calibration to Phase 5 validation against a larger probe corpus.
- **Option C (deferred):** leave the constant alone in Phase 2 and defer the threshold change to after Phase 5 validation. Phase 2 shrinks to "document the existing 0.40 behavior" and becomes a docs-only PR.

FTS5 fallback hits keep the existing sentinel behavior (`confidence=None` passes through; the threshold only applies to scored cosine hits). This is unchanged by any option.

Phase dependency: Phase 2 is logically independent of Phase 0 and Phase 1 in code, but its MEASURED effect is only comparable to the R2 baseline if the rest of the library state is unchanged. If Phase 2 ships before Phase 0, record a baseline snapshot of R2 metrics in the Phase 2 PR so Phase 5 can separate Phase 2's contribution from Phase 0+1.

### Phase 3: FTS5 path `match_text` column

Extend `plans_fts` to include a `match_text` column so the fallback path benefits from the same surface change:
- Schema migration `_add_plan_match_text`: add `match_text TEXT DEFAULT ''` to `plans`, extend `plans_fts`, re-populate on first session start via the synthesizer.
- `save_plan` writes both `query` (human label) and `match_text` (synthesized from its inputs).
- `search_plans` FTS5 query targets `match_text, tags, project`.

### Phase 4: documentation

Update `docs/plan-authoring-guide.md` and `docs/plan-centric-retrieval.md`:
- Authors must populate `verb`, `scope`, `dimensions` in plan YAML; `query` is a display label, not the matching surface.
- The matcher's confidence floor and its calibration are documented.
- The grown-plan inference rules are documented so users understand what ends up in T2.

## Alternatives Considered

### A. Canonicalize prose at save time

Strip `"How does"`, `"RDR-NNN"`, question marks, etc. from `query` and embed the result. Smaller win than the dimensional approach: still prose-shaped, still depends on what tokens the author happens to write, still loses to MiniLM's topical generalization.

### B. Multi-anchor per plan

Store 3–5 paraphrases per plan; embed each; take max cosine. Requires a new table and explicit authoring or generation of paraphrases. Higher authoring cost; higher coverage ceiling. Deferred until we see whether dimensional embedding is enough.

### C. HyDE (hypothetical document embedding)

Embed a synthetic answer passage, not the question. Incoming questions align when they look for the same answer. Requires a stronger embedder than MiniLM-384 to pay off, and adds an inference step at save time. Deferred.

### D. Do nothing; ship RDR-090 benchmark first

RDR-090 will measure plan-first vs naive RAG on a fixed corpus. That benchmark will tell us whether the matching surface is the actual bottleneck or whether other factors (plan coverage, operator quality) dominate. Valid to defer this RDR until RDR-090 lands. Not preferred because: (1) the empirical `match_count` distribution already points at the problem, (2) the fix is small and localized, (3) RDR-090 will be more useful with a sharper matcher.

### E. Keep the current embedding, add a re-ranker

Train or use a cross-encoder post-ranker over the cosine top-K. More complex, costs more per match, harder to reason about. Only worth doing once the base embedding surface is aligned with the plan's typed identity.

## Trade-offs

- **Pro: alignment with typed plan identity.** The matching surface matches the plan's declared purpose (verb, dims, scope) rather than the author's happenstance prose. The dimensional columns earn their keep.
- **Pro: sharper attractors in the best case.** A plan saved with `verb=analyze dims={question-type: comparison, intent: tradeoffs, corpus: rdr}` produces a more specific embedding neighborhood than a free-text question. R4 shows the effect is modest on its own; it compounds with the confidence floor.
- **Pro: confidence floor is independent leverage.** Phase 2 can ship before Phase 0 if needed and will reduce spurious top-5 landings immediately. The floor survives whether or not match_text synthesis ever ships.
- **Pro: local, no external cost.** Embedding stays MiniLM ONNX; no model change, no API spend.
- **Pro: preserves display layer.** `plan_search` still shows human-readable anchors. Authors still write what the plan is for.
- **Con: upstream scope is bigger than the original RDR assumed.** Phase 0 touches three save paths (legacy seed, grown-plan, loader instrumentation) plus a backfill migration. Substantially more surface area than the original "session_cache one-line change".
- **Con: backfill heuristics are lossy.** Inferring `verb` from query stems via a dictionary will misclassify edge cases. Flagging with `tags += ",backfill"` preserves auditability but doesn't prevent bad dimensions. Mitigated by `nx plan repair` for manual correction.
- **Con: risk of new attractor shape from synthesis.** R4 showed a JSON-style match_text creates its own attractor via tokens like "dims=". The word-prefix format proposed here should be less vulnerable but needs calibration in Phase 5 validation.
- **Con: two surfaces to maintain.** `query` for display, `match_text` for matching. Small but real ongoing cost in authoring and migration code.
- **Con: legacy template migration loses Git blame continuity.** Retiring `_PLAN_TEMPLATES` and creating 5 new YAML files means the git history no longer points at a single authoritative legacy-template ancestor. Acceptable for 5 small templates; note in commit message.

## Implementation Plan

### Prerequisites

- RDR-091 (scope-aware matching) already landed.
- R3 finding (`092-research-3`) documents that dimensional columns are empty in production; this RDR's Phase 0 addresses that directly.

### Phase 0a: migrate legacy `_PLAN_TEMPLATES` into YAML

- Retire `_PLAN_TEMPLATES` from `src/nexus/commands/catalog.py`. Add 5 new YAML files under `nx/plans/builtin/` (one per legacy template) with full `verb`, `scope`, `strategy`, `dimensions`, `plan_json` frontmatter.
- Remove the `for tmpl in _PLAN_TEMPLATES:` loop in `catalog.py:107-115`. The `load_all_tiers` call below it already handles loading.
- Regression gate: `nx catalog setup` on a fresh DB produces at least 9 dimensional rows (4 existing + 5 migrated, minus any overlap). Test: `test_catalog_cli.py::test_setup_produces_dimensional_rows`.

### Phase 0b: grown-plan path passes dimensional args

- In `src/nexus/mcp/core.py:2397`, add `verb=<tier-resolved>`, `name=<kebab-case first words>`, `dimensions=<synthesized JSON>` arguments to the `save_plan` call.
- Verb resolution follows R6's three-tier cascade: caller-supplied `dimensions["verb"]` (if present in the `dimensions` argument to `nx_answer`) → `plan_json.steps` operator-shape inference (`compare` → analyze, `extract`+`rank` → analyze, `traverse`+`search`+`summarize` → research, flat → research) → fallback to `verb=research`.
- Name: kebab-case of the question's first 3-5 content words.
- Dimensions: `{"verb": <resolved>, "scope": "personal", "strategy": "grown"}`.
- No dependency on `purposes.yml` (that file resolves link-type sets for `traverse`, not verbs).
- Unit tests: `test_nx_answer.py::test_grown_plan_has_dimensional_columns`, `test_nx_answer.py::test_grown_plan_verb_inference_from_plan_json`.

### Phase 0c: loader instrumentation

- Modify `catalog.py:120-137`: emit a user-visible warning when `load_all_tiers` returns zero rows for any tier. Fail loudly if the global tier returns zero (meaning the plugin builtins are not loadable).
- Add `nx doctor --check-plan-library`: reports plans missing dimensional columns; flags the count; returns non-zero exit if the global-tier builtin count is below a minimum.
- Test: `test_doctor.py::test_check_plan_library_flags_missing_dimensions`.

### Phase 0d: backfill migration for existing rows

- New migration `_backfill_plan_dimensions`: heuristically infers `verb`/`name`/`dimensions` for rows where all three are empty. Maps question-stems to verbs via a small documented dictionary.
- Flags backfilled rows via `tags += ",backfill"` (idempotent).
- Test: `test_migrations.py::test_backfill_plan_dimensions_infers_verb`, `test_backfill_plan_dimensions_idempotent`.

### Phase 1: synthesizer + upsert path

- Add `_synthesize_match_text` helper in `session_cache.py`.
- Change `_upsert_row` to call it; `documents=[match_text]` instead of `documents=[query]`.
- Unit tests: `test_plan_session_cache.py` covers (a) plans with full dimensional columns (synthesized), (b) plans with only `query` (fallback to raw), (c) empty plans (rejected as today).

### Phase 2: revise the existing `min_confidence` floor

Note: `min_confidence` is already a wired parameter in `matcher.py:132` (default 0.40) and `_PLAN_MATCH_MIN_CONFIDENCE` at `core.py:1726` (also 0.40, RDR-079 calibration). Phase 2 is NOT new wiring. Pick a resolution per the Proposed Solution options A/B/C, then:

- **Option A**: expose `min_confidence` as a per-call parameter on `nx_answer` so verb skills can pin 0.50 for precision-first calls. No change to the default constant.
- **Option B**: change `_PLAN_MATCH_MIN_CONFIDENCE` from 0.40 to 0.50 at `core.py:1726`. Ship a regression test that verifies legit probes still find matches at the new threshold. Acknowledge the R9 sample-size risk in commit message.
- **Option C**: docs only. Document the existing 0.40 behavior and defer the threshold change to post-Phase-5 validation.

If Option B ships, also record a snapshot of R2 metrics against the current library IMMEDIATELY BEFORE the constant change so Phase 5 can separate Phase 2's effect from Phase 0+1.

Tests (pick per option):
- Option A: `test_nx_answer.py::test_min_confidence_override_tightens_match_rejection`.
- Option B: `test_plan_match.py::test_min_confidence_at_050_rejects_random_probe`, `test_plan_match.py::test_min_confidence_at_050_accepts_legit_probe`.
- Option C: no new tests; docs-only PR.

### Phase 3: FTS5 `match_text` column

- Migration `_add_plan_match_text_column`: adds `match_text TEXT DEFAULT ''` to `plans`; extends `plans_fts` to include `match_text`; backfills existing rows via the synthesizer.
- `save_plan` computes and writes `match_text` alongside `query`.
- `search_plans` FTS5 query targets `match_text, tags, project`.
- Test: `test_plan_library.py::test_search_plans_uses_match_text`.

### Phase 4: documentation

- `docs/plan-authoring-guide.md`: note that authored YAML must populate `verb`/`scope`/`dimensions`; `query` is a display label.
- `docs/plan-centric-retrieval.md`: update the "how plans match" paragraph to reflect synthesized match_text and the confidence floor.
- `docs/nexus-cli.md`: add `nx doctor --check-plan-library` and note `nx plan repair`.

### Phase 5: validation + measurement

- Re-run the R1 ablation and R2 top-5 probe post-Phase-0-through-3. Document delta: how many of the 10 probes still land plan #38 in top-5? Does "just a random hello query" still rank 1 on any plan?
- Dispatch `nx:substantive-critic` against the new matcher behavior on 20 representative queries.
- Update `092-research-*` T2 entries with post-change measurements, or record `092-validation-*` follow-ups.

### Day-2 operations

- `nx doctor --check-plan-library`: report plans missing dimensional columns and flag low-dimensional coverage. Phase 0c.
- `nx plan repair`: command to re-run the backfill heuristic and re-synthesize `match_text`. Idempotent, safe to run.

## Test Plan

Phase 0a (legacy migration):
- `test_catalog_cli.py::test_setup_produces_dimensional_rows`: `nx catalog setup` on a fresh DB produces at least 9 dimensional rows across the global tier.
- `test_catalog_cli.py::test_legacy_templates_no_longer_ingested_non_dimensionally`: the `_PLAN_TEMPLATES` path is gone; the 5 legacy shapes come from YAML with full columns.

Phase 0b (grown-plan):
- `test_nx_answer.py::test_grown_plan_has_dimensional_columns`: after a grown-plan save, the row has populated `verb`, `name`, and `dimensions`.

Phase 0c (instrumentation):
- `test_doctor.py::test_check_plan_library_flags_missing_dimensions`: synthetic DB with empty-dim rows produces a non-zero exit.
- `test_catalog_cli.py::test_setup_warns_on_zero_global_tier`: a plugin_root with no builtin YAML files triggers the visible warning.

Phase 0d (backfill):
- `test_migrations.py::test_backfill_plan_dimensions_infers_verb`: the heuristic maps "how does X work?" to `verb=investigate` (or equivalent mapped value).
- `test_migrations.py::test_backfill_plan_dimensions_idempotent`: running twice yields identical state.
- `test_migrations.py::test_backfill_preserves_authored_verbs`: rows with an authored `verb` are not modified.

Phase 1 (synthesizer):
- `test_plan_session_cache.py::test_synthesize_with_dimensions`: full dimensional row yields expected match-text shape.
- `test_plan_session_cache.py::test_synthesize_fallback_to_query`: row without dimensional columns returns raw query.
- `test_plan_match.py::test_match_by_dimensions_not_prose`: two plans with identical `query` prose but different dimensions rank differently for dimensionally-specific incoming questions.

Phase 2 (min_confidence):
- `test_plan_match.py::test_min_confidence_filters_noise`: a probe with max cosine below the floor returns no matches.
- `test_plan_match.py::test_min_confidence_does_not_affect_fts5_sentinel`: FTS5 fallback path still returns with `confidence=None`.

Phase 3 (FTS5):
- `test_plan_library.py::test_search_plans_uses_match_text`: FTS5 query matches a synthesized term but not the raw query for a plan whose `query` is deliberately unrelated to its dimensions.
- `test_migrations.py::test_match_text_column_migration_idempotent`.

Canary regression (Phase 5):
- `test_plan_match.py::test_random_probe_rank1_regresses`: "just a random hello query" no longer lands any plan at rank 1 above the confidence floor.
- `test_plan_match.py::test_144_row_narrows`: the R2 landings count for plan #38 drops below a documented target (relative, not absolute; recorded as telemetry, not a hard gate).

## Validation

Finalization gate requires:

1. Phase 0a through 0d landed. `nx doctor --check-plan-library` on a fresh install reports ≥ 9 dimensional rows and flags no missing dim columns.
2. Phase 1 + 2 landed. The R2 probe set re-run shows:
   - Plan #38 no longer ranks 1 on "just a random hello query" (the probe either returns no match above the confidence floor, or ranks on a more appropriate plan).
   - The R2 landings count for plan #38 drops relative to pre-change baseline.
3. No regression on `test_plan_match.py`, `test_plan_library.py`, or `test_nx_answer.py`.
4. `substantive-critic` review of the matcher on 20 representative queries shows no new false negatives (specialized plans still selected for specialized questions).
5. T2 research entries `092-research-*` have companion `092-validation-*` entries recording the post-change measurements.

Measurement stays qualitative pre-RDR-090 (describe the deltas against the R2 probe set). Re-evaluated quantitatively once RDR-090's benchmark is available.

## Finalization Gate

- [ ] All research findings recorded via `/nx:rdr-research add 092`.
- [ ] Three-layer gate passes via `/nx:rdr-gate 092`.
- [ ] Critic critique resolved.
- [ ] Canary measurements documented.

## References

- RDR-078: Plan dimensional identity (`verb`, `scope`, `dimensions`).
- RDR-080: Retrieval layer consolidation (nx_answer wiring).
- RDR-091: Scope-aware plan matching (post-cosine re-ranking).
- `src/nexus/plans/session_cache.py:178`: current `document=query` upsert.
- `src/nexus/plans/matcher.py:162`: two-path matcher, cosine + FTS5.
- `src/nexus/db/t2/plan_library.py:74-104`: schema with dimensional columns.
- Post 6 draft, matcher-returns table, 2026-04-22: empirical evidence for the attractor-breadth problem.

## Revision History

- 2026-04-22: draft created.
- 2026-04-22: research findings R1-R4 recorded. R3 blocks the RDR as originally scoped (dimensional columns empty in production). R4 shows synthesis is lower-leverage than a `min_confidence` floor plus library growth.
- 2026-04-22: scope pivoted to upstream-first (RDR-092B). Phase 0 populates dimensional columns across all three save paths. Phase 1 does the match_text synthesis as originally proposed. Phase 2 adds a `min_confidence` floor as independent leverage. Phase 3 extends FTS5. Phases can ship independently; Phase 2 is the cheapest immediate win.
- 2026-04-23: R5-R8 recorded, refining the Phase 0 plan. R5 reframes the upstream root cause as a deployment gap (loader is healthy; just never re-run post-landing). R6 validates three-tier grown-plan inference (caller-dims → plan_json-shape → name-from-question). R7 disposes the 5 legacy templates: migrate 3 to YAML, retire 2 as redundant with existing builtins; net +3 YAML builtins. R8 tests the backfill heuristic: 60% confident coverage on a 13-rule dictionary, 85% expected with a 20-rule enriched version plus wh-question fallback, low-conf rows flagged via `tags += ",backfill-low-conf"`.
- 2026-04-23: R9-R11 recorded. R9 calibrates `min_confidence=0.5` as the clean sweet spot (100% legit recall + 100% noise rejection against the 5+5 probe set). R10 revises Phase 1 synthesizer form from pure-synth to hybrid (description + dimensional suffix); pure-synth collapsed legit signal while hybrid improves both noise rejection and legit matching with zero verb-accuracy regressions. R11 estimates 29-45 engineering hours with migration risk multiplier; each phase is a contained PR.
- 2026-04-23: gate attempt 1 BLOCKED (T2: `092-gate-latest`). Critic flagged 2 critical factual errors. Fixed in place: (1) Phase 2 rewritten to reflect that `min_confidence` already exists at 0.40 in `matcher.py:132` and `core.py:1726:2151` from RDR-079; Phase 2 is now a constant-revision decision (Option A per-call override / Option B global 0.40→0.50 / Option C docs-only) rather than new wiring. (2) Phase 0.2 and Phase 0b rewritten to remove the false "purpose from purposes.yml IS the verb" claim; verb routing uses R6's three-tier cascade (caller dims → plan_json shape → research fallback) with no dependency on `purposes.yml` (which resolves link-type sets for the `traverse` operator only). Ship order revised: Phase 0 first, then 1 → 2 → 3 → 4 → 5. R11 cost estimate adjusted downward ~1-2 hours for Phase 2. Tokyo-row aside clarified to reference the blog-draft correction out-of-scope. Ready for re-gate.
