# RDR-078 Implementation Plan: Plan-Centric Retrieval

**RDR**: [RDR-078 — Plan-Centric Retrieval](../rdr/rdr-078-unified-context-graph-and-retrieval.md) (accepted 2026-04-15, revision 8)
**Plan author**: strategic-planner
**Plan date**: 2026-04-14
**Out-of-scope reminder**: lifecycle ops (`nx plan promote`, `nx plan audit`, RDR-close hooks, `search(purpose=)` propagation) are deferred to RDR-079. This plan ships the foundational layer only.

## Executive Summary

RDR-078 ships in **six phases** (Phase 4 fans out into 4a-4e). Every phase is additive on top of shipped substrate (RDR-041 T1 HTTP server, RDR-042 plan library + `/nx:query`, RDR-070 HDBSCAN taxonomies, RDR-077 projection quality). The architectural glue: a dimensional plan template registry, semantic plan matching via T1 cosine, a `traverse` plan operator over the existing catalog typed-link graph, scenario seeds, and plan-first agent priming.

The quality lever is **Phase 3** (typed-graph traversal). The efficiency lever is **Phase 1** (semantic plan matching, ~40% compute reduction at ≥0.85 confidence). The shipping-velocity lever is **Phase 6** (git-tracked YAML scopes).

## Phase Map

| Phase | Theme | Beads |
|---|---|---|
| 1 | `plan_match` + `plan_run` MCP tools, T1 cache populator (no schema change yet) | P1 |
| 2 | Domain-scoped retrieval steps (plan-step `scope` field) | P2 |
| 3 | `traverse` operator + `Catalog.graph_many()` + purpose registry + 2 link-type docs | P3 |
| 4a | Plan template schema + validator + canonical dimension JSON | P4a |
| 4b | Five scenario template seeds (`scope:global`) | P4b |
| 4c | T2 migration: dimensional identity + currying + metrics columns | **P4c (first to land — schema migration)** |
| 4d | Four meta-seed plans (`verb:plan-author`, `verb:plan-promote`, two `verb:plan-inspect`) | P4d |
| 4e | `docs/plan-authoring-guide.md` | P4e |
| 5 | Skills (`nx:plan-first` gate + 5 verb skills + 3 plan-mgmt skills) + SessionStart/SubagentStart hook edits + 8 agent-md edits | P5 |
| 6 | Scoped plan loader (4 YAML tiers) + CI schema-validation hook | P6 |

## Sequencing Decisions

The natural ordering is **4c → 1 → (2, 4a in parallel) → 3 → (4b, 4d, 4e in parallel) → 5 → 6**. Rationale captured in each bead.

### Why 4c (T2 schema migration) lands first, not Phase 1

Phase 1 (`plan_match`) reads the new `dimensions`, `verb`, `scope`, `default_bindings`, `parent_dims`, `name`, and metrics columns. Phase 4a's `plan_save` writes them. Both depend on the schema. Migrating the table first means every subsequent bead writes against the live schema with no rebase friction. Existing RDR-042 rows migrate in place (`verb=NULL, scope='personal', dimensions=NULL`) per RDR §Phase 4c, so this is non-breaking.

**Alternative considered**: ship Phase 1 against the unchanged `plans` table and migrate later. Rejected — would require rewriting `plan_match` and `Match.from_plan_row` once the dimensional columns land, doubling the test surface.

### Why Phase 2 and Phase 4a parallelize

Phase 2 (scope field on plan steps) is a runner-side change — accepts new arg, dispatches scoped retrieval. Phase 4a (template schema + validator) defines what loaders accept on disk. They share no code surface; Phase 4a's schema must *describe* the Phase 2 scope field, but the schema doc is authored from the RDR — both teams read the same spec. Both depend on 4c (need `dimensions` column to define identity), then proceed independently.

### Why Phase 3 depends on Phase 2

Phase 3's purpose registry resolves to link-type lists; the resolved set is forwarded into the `traverse` step. Phase 3 contract tests must verify `traverse → search(scope=...)` composition end-to-end, which requires the Phase 2 scope plumbing live. Without Phase 2, Phase 3 tests would have to stub the downstream step.

### Why Phase 4b/4d/4e fan out after Phase 3

The five scenario seeds (4b) and four meta-seeds (4d) all reference `traverse` steps with `purpose:` arguments. Authoring them requires Phase 3 to be live (otherwise loader rejects unknown tool). The authoring guide (4e) documents the schema (4a) plus the scope (Phase 2) plus the traverse operator (Phase 3). All three can be written in parallel by different drafters once their inputs are stable.

### Why Phase 5 depends on Phases 1, 3, 4b being mergeable

The 9 skills' template body is `plan_match(intent, dimensions={verb:<v>}, n=1) → plan_run(match, bindings)`. Each skill needs at least one matching plan in the library — five verb skills need five scenario seeds (4b); three plan-mgmt skills need three meta-seeds (4d). Without these, the skills fall through to `/nx:query` 100% of the time, defeating the demo.

The hook edits could in principle land before the skills, but the SessionStart "## Plan Library" block lists the scenario names — wrong listing if seeds aren't shipped.

### Why Phase 6 depends on Phase 4c (not Phase 4a/b)

The scoped loader's idempotency mechanism is `INSERT … ON CONFLICT(project, dimensions) DO UPDATE`, which requires the UNIQUE INDEX shipped in Phase 4c. The schema validator (Phase 4a) is also a dependency, but 4a depends on 4c, so the chain transits through 4c.

## Critical Path

`P4c → P1 → P3 → P4b → P5`

(P2, P4a parallelize against P1; P4d, P4e parallelize against P4b; P6 enters after P4c is live and after P4a is mergeable.)

## Parallelization Opportunities

| Parallel set | Trigger | Beads |
|---|---|---|
| After P4c lands | Phase 1, Phase 2, Phase 4a all unblocked | P1, P2, P4a |
| After P3 lands | Phase 4b, Phase 4d, Phase 4e all unblocked | P4b, P4d, P4e |
| Phase 6 has weak dependency on P4a only (schema validator) | Can start once P4a + P4c are live | P6 |

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| T1 server unavailable at SessionStart | Phase 1 spec mandates FTS5 fallback via `Match.from_plan_row(confidence=None)`. Tested per SC-1 fallback case. |
| Cross-embedding cosine sneaks in | Phase 1 ships `PlanRunEmbeddingDomainError` runtime guard + typed `traverse` signature (SC-10). Pinned by unit test `test_plan_runner_rejects_cross_embedding_step`. |
| Metrics counter sites missed | Phase 1 + Phase 4c integration test asserts `match_count` and `success_count` increment on a complete `plan_match → plan_run` round-trip. |
| Scenario seeds collide with RDR-042 builtin templates | Phase 4b seed loop uses `INSERT … ON CONFLICT(project, dimensions) DO UPDATE`. RDR-042 rows have `dimensions=NULL`, so no conflict — they continue working through legacy FTS5 path. |
| Skills outpace seeds (5 skills ship, only 3 seeds available) | Phase 5 bead explicitly depends on 4b *and* 4d. CI: skill files reference verbs that must exist in seed YAML. |
| Schema CI in Phase 6 catches no errors because tests don't cover the loader | Phase 6 test plan includes loader-from-malformed-yaml round-trip (SC-15). |

## Per-Bead Detail

Each bead below maps to one phase deliverable. Beads include: file touchpoints, test cases, dependencies, and a reminder to use `mcp__plugin_nx_sequential-thinking__sequentialthinking` for the design-of-DAG tasks (Phase 1 runner contract, Phase 3 traverse merge invariants, Phase 4a canonical-JSON dedup).

### Epic — RDR-078 Plan-Centric Retrieval

Tracks all phase beads. Closes when SC-1..SC-19 all pass and the ART end-to-end demo (SC-8) clears `min_confidence` on the warm-library run.

### P4c — T2 schema migration (dimensional identity + currying + metrics)

**Lands first.** Migration adds: `verb TEXT`, `scope TEXT`, `dimensions TEXT`, `default_bindings TEXT`, `parent_dims TEXT`, `name TEXT`, `use_count INTEGER`, `last_used TEXT`, `match_count INTEGER`, `match_conf_sum REAL`, `success_count INTEGER`, `failure_count INTEGER`, plus indexes on `verb`, `scope`, `(verb, scope)`, and the load-bearing `UNIQUE INDEX idx_plans_project_dimensions ON plans(project, dimensions) WHERE dimensions IS NOT NULL`.

**Files**:
- `src/nexus/db/migrations.py` — register new `Migration` per RDR-076 pattern; idempotent ALTER TABLE + CREATE INDEX.
- `src/nexus/db/t2/plan_library.py` — accept new columns in CRUD methods; existing RDR-042 callers continue working with NULL columns.
- `src/nexus/plans/schema.py` — **new file** — `canonical_dimensions_json(dim_map: dict) -> str` (sorted, lowercased keys).

**Tests** (`tests/test_plan_library_migration.py`):
1. `test_migration_idempotent` — apply migration twice, no error, schema unchanged.
2. `test_legacy_rows_load` — pre-migration row reads back with `verb=None, dimensions=None`.
3. `test_unique_dimensions_constraint` — INSERT with duplicate `(project, dimensions)` raises IntegrityError.
4. `test_canonical_dimensions_json_stable` — `{"verb":"r","scope":"g"}` and `{"scope":"g","verb":"r"}` produce identical strings.
5. `test_metrics_default_zero` — new row has all counters at 0.

### P1 — `plan_match` + `plan_run` MCP tools + T1 cache populator

**Files**:
- `src/nexus/mcp/core.py` — register `plan_match` and `plan_run` tools.
- `src/nexus/plans/match.py` — **new file** — `Match` dataclass, `Match.from_plan_row()` constructor.
- `src/nexus/plans/runner.py` — **new file** — deterministic step dispatch, `$var` and `$stepN.<field>` resolution, `PlanRunBindingError`, `PlanRunStepRefError`, `PlanRunEmbeddingDomainError`.
- `src/nexus/db/t1.py` — extend with `plans__session` collection bootstrap helper.
- `src/nexus/db/t2/plan_library.py` — commit hook on `plan_save`: also upsert to T1 `plans__session`.

**Tests** (`tests/test_plan_match.py`, `tests/test_plan_run.py`):
1. `test_plan_match_returns_high_confidence_above_threshold` — paraphrased intent matches saved description with cosine ≥ PQ-2 calibrated value.
2. `test_plan_match_t1_unavailable_falls_back_to_fts5` — kill T1, get `Match` with `confidence=None` from `plan_search` path.
3. `test_plan_run_resolves_var_and_stepref` — `$concept` and `$step1.tumblers` substitute correctly.
4. `test_plan_run_rejects_missing_required_binding` — `PlanRunBindingError(missing=[...])`.
5. `test_plan_runner_rejects_cross_embedding_step` — step with `scope.taxonomy_domain=prose` dispatched to a code corpus → `PlanRunEmbeddingDomainError` (SC-10).
6. `test_plan_save_visible_within_session` — save, then `plan_match` from same process resolves it.

**Reminder**: use `mcp__plugin_nx_sequential-thinking__sequentialthinking` to design the `$stepN.<field>` resolution contract — multi-hop reference resolution interacts with T1 scratch tagging (RDR-041 pattern, tag `plan_run,step-N`).

### P2 — Domain-scoped retrieval steps

**Files**:
- `src/nexus/plans/runner.py` — extend step dispatch to forward `step.scope.taxonomy_domain` and `step.scope.topic` to `search()` / `query()`.
- `src/nexus/search_engine.py` — accept `taxonomy_domain` and per-domain topic; route to correct corpus subset.

**Tests** (`tests/test_plan_step_scope.py`):
1. `test_scope_prose_routes_to_prose_corpora` — step with `taxonomy_domain=prose` only hits `knowledge__*`, `docs__*`, `rdr__*`, `paper__*`.
2. `test_scope_code_routes_to_code_corpora` — only `code__*`.
3. `test_topic_filter_applied` — `topic=` arg forwarded into the corpus-scoped search where-clause.
4. `test_no_cross_embedding_cosine_computed` — assert no call path computes cosine across model boundaries (greppable + runtime guard from P1).

### P3 — `traverse` operator + `Catalog.graph_many()` + purpose registry + link-type docs

**Files**:
- `src/nexus/catalog/catalog.py` — add `Catalog.graph_many(seeds, depth, link_types, direction) -> {nodes, edges}` per RDR §Phase 3.
- `src/nexus/plans/runner.py` — register `traverse` step kind; dispatch to `graph_many()` (list seeds) or `graph()` (single seed).
- `src/nexus/plans/purposes.py` — **new file** — `purposes_resolve(name, project, scope) -> list[str]`; loads `nx/plans/purposes.yml`; warn-and-drop unknown link types per RDR.
- `nx/plans/purposes.yml` — **new file** — starter purpose set per RDR §Phase 3.
- `docs/catalog-link-types.md` — **new file** — directionality, source, typical traversal shape per link type.
- `docs/catalog-purposes.md` — **new file** — purpose registry reference.

**Tests** (`tests/test_catalog_graph_many.py`, `tests/test_traverse_step.py`, `tests/test_purposes_resolve.py`):
1. `test_graph_many_node_dedup` — two seeds reaching same node merge with `seed_origin: list[str]` populated, first-seen wins.
2. `test_graph_many_edge_dedup` — `(from, to, link_type)` triple dedup across seed traversals.
3. `test_graph_many_max_nodes_cap` — merged frontier respects `_MAX_GRAPH_NODES = 500`, short-circuits.
4. `test_traverse_seeds_from_step_ref` — `seeds: $step1.tumblers` resolves from prior retrieval step output.
5. `test_purpose_resolves_to_link_types` — `purpose:find-implementations` → `[implements, implements-heuristic]`.
6. `test_purpose_unknown_link_type_warn_and_drop` — registry references future link type; resolver returns subset, logs `purpose_unknown_link_type` warning.
7. `test_traverse_returns_collections_usable_as_subtree` — return shape `collections` feeds downstream `search(subtree=...)` (SC-5 end-to-end).

**Reminder**: use sequential thinking to design the `graph_many` merge invariants — node-key vs edge-key dedup, cap-across-merged-frontier, seed-origin metadata semantics.

### P4a — Plan template schema + validator + canonical dimension JSON

**Files**:
- `src/nexus/plans/schema.py` — **extend P4c's file** — `validate_plan_template(yaml_dict) -> None`, raises named errors per RDR §Phase 4a.
- `src/nexus/plans/__init__.py` — re-export `Match`, `validate_plan_template`, `canonical_dimensions_json`.

**Tests** (`tests/test_plan_template_schema.py`):
1. `test_schema_accepts_full_template` — RDR §Phase 4a example loads cleanly.
2. `test_schema_rejects_missing_required_dimension` — no `verb` → named error.
3. `test_schema_rejects_link_types_and_purpose_together` — both specified → validation error (SC-16).
4. `test_canonical_dimensions_dedup_collision` — two plans with identical canonical maps → loader rejects later, names both sources.
5. `test_unregistered_dimension_warns_lenient` — unknown dim warns but loads (default mode, SC-19).

### P4b — Five scenario template seeds

**Files**:
- `nx/plans/builtin/research-default.yml` — `verb:research, scope:global, strategy:default`.
- `nx/plans/builtin/review-default.yml` — `verb:review, ...`.
- `nx/plans/builtin/analyze-default.yml` — `verb:analyze, ...`.
- `nx/plans/builtin/debug-default.yml` — `verb:debug, ...` (intentionally flat — no `traverse`).
- `nx/plans/builtin/document-default.yml` — `verb:document, ...`.
- `src/nexus/commands/catalog.py` — extend `nx catalog setup` to load builtin scenarios via P6 loader (or via direct seed call until P6 lands).

**Tests** (`tests/test_scenario_seeds.py`):
1. `test_all_five_seeds_load` — `nx catalog setup` writes 5 rows with correct dimensions.
2. `test_seeds_idempotent` — second `setup` doesn't duplicate (UNIQUE constraint).
3. `test_four_seeds_use_traverse` — research, review, analyze, document each have ≥1 `tool: traverse` step (SC-6).
4. `test_debug_seed_is_flat` — debug has zero `traverse` steps (SC-6, intentional per RDR §Phase 4b).
5. `test_seed_descriptions_match_paraphrase_set` — paraphrase queries resolve to correct seed via `plan_match` ≥ threshold.

### P4d — Four meta-seed plans

**Files**:
- `nx/plans/builtin/plan-author-default.yml` — `verb:plan-author, strategy:default`.
- `nx/plans/builtin/plan-promote-propose.yml` — `verb:plan-promote, strategy:propose`.
- `nx/plans/builtin/plan-inspect-default.yml` — `verb:plan-inspect, strategy:default`.
- `nx/plans/builtin/plan-inspect-dimensions.yml` — `verb:plan-inspect, strategy:dimensions`.

**Tests** (`tests/test_meta_seeds.py`):
1. `test_all_four_meta_seeds_load_idempotent` — re-setup is no-op (SC-13).
2. `test_meta_seed_descriptions_resolve` — `plan_match("how do I write a plan")` returns `verb:plan-author` ≥ threshold.
3. `test_plan_promote_dag_runs` — execute meta-seed against empty metrics, returns empty shortlist without error.

### P4e — `docs/plan-authoring-guide.md`

**Files**:
- `docs/plan-authoring-guide.md` — **new file** — covers templating vocabulary (template/description/intent/bindings/scope), schema reference, what makes a good description, binding naming conventions, four-axis `plan_match` contract, lifecycle (personal → rdr → project → repo → global), `verb:plan-author` pointer.
- Catalog-indexed automatically via `nx catalog setup` (it's under `docs/`).

**Tests** (`tests/test_plan_authoring_guide.py`):
1. `test_guide_exists_and_indexed` — file present; catalog row created on `nx catalog setup`.
2. `test_guide_resolves_via_plan_match` — `plan_match("what makes a good plan description")` returns the guide ≥ 0.80 confidence (SC-17).

### P5 — Skills + hook edits + per-agent prompts

**Files**:
- `nx/skills/nx-plan-first.md` — **new** — gate skill, dispatches `plan_match` first.
- `nx/skills/nx-research.md`, `nx-review.md`, `nx-analyze.md`, `nx-debug.md`, `nx-document.md` — **new** — five verb skills, shared template body.
- `nx/skills/nx-plan-author.md`, `nx-plan-inspect.md`, `nx-plan-promote.md` — **new** — three plan-mgmt skills.
- `nx/hooks/scripts/session_start_hook.py` — **edit** — populate T1 `plans__session` (SQL per RDR §Phase 5); inject `## Plan Library` block.
- `nx/hooks/scripts/subagent-start.sh` — **edit** — inject plan-match-first preamble for the 8 retrieval-shaped agents.
- `nx/agents/strategic-planner.md`, `architect-planner.md`, `code-review-expert.md`, `substantive-critic.md`, `deep-analyst.md`, `deep-research-synthesizer.md`, `debugger.md`, `plan-auditor.md` — **edit** — opening instruction citing `plan_match`-first pattern.

**Tests** (`tests/test_plan_first_skills.py`, `tests/test_session_start_hook.py`, `tests/test_plugin_structure.py` extension):
1. `test_all_nine_skills_present_and_well_formed` — plugin structure tests pass for the 9 new skill files.
2. `test_session_start_populates_plans_session` — after hook runs, `COUNT(t1.plans__session) == COUNT(t2.plans WHERE outcome='success' AND ttl-honest)` (SC-2).
3. `test_session_start_injects_plan_library_block` — output contains `## Plan Library` with all five scenario names (SC-7).
4. `test_subagent_start_preamble_present_for_eight_agents` — grep on hook output for each of 8 agent names (SC-7).
5. `test_each_agent_md_cites_plan_match_first` — grep on each `nx/agents/<name>.md` (SC-7 — survives hook trimming).

### P6 — Scoped plan loader (4 YAML tiers) + CI schema validation

**Files**:
- `src/nexus/plans/loader.py` — **new file** — scans `nx/plans/builtin/*.yml`, `docs/rdr/<slug>/plans.yml` (only `accepted`/`closed`), `.nexus/plans/*.yml`, optional `_repo.yml`. Validates per P4a, dedups via `(project, dimensions)` UNIQUE.
- `src/nexus/commands/catalog.py` — `nx catalog setup` invokes loader.
- `.github/workflows/plan-schema-check.yml` — **new** — CI check on PR for plan schema validity (SC-15).

**Tests** (`tests/test_scoped_plan_loader.py`):
1. `test_loader_loads_all_four_tiers` — temp project with all 4 tiers populated; `setup` writes correct row count with correct scope tags (SC-14).
2. `test_loader_skips_draft_rdr_plans` — RDR with `status: draft` → its `plans.yml` is not loaded (SC-14).
3. `test_loader_idempotent_via_unique_index` — re-run = no new rows (SC-14).
4. `test_loader_path_scope_mismatch_logs_warning_path_wins` — YAML in `.nexus/plans/` declares `scope:global` → warn, store with `scope:project` (SC-14).
5. `test_malformed_yaml_logs_named_error_skips` — broken YAML → named error, other plans still load (SC-14).
6. `test_ci_schema_check_fails_on_invalid_plan` — invoking the CI script with bad YAML returns non-zero (SC-15).

## Acceptance — Closing the Epic

The epic closes when:

- All phase beads are `closed`.
- All SC-1..SC-19 verified — checklist run as part of close gate.
- ART end-to-end demo (SC-8) recorded: cold-library run → `/nx:query` planner → plan saved; warm-library run → `plan_match` ≥ threshold → `plan_run` succeeds, includes ≥1 `traverse` step traversing typed links from RDR to implementing code.
- Zero regressions on `plan_save` / `plan_search` / `/nx:query` / `search()` / `query()` (SC-9).
- Cross-embedding boundary not crossed anywhere (SC-10): `test_plan_runner_rejects_cross_embedding_step` green; greppable invariant on `plan_run`.

## Continuation State

Persist progress to T2 memory after each phase merges:

```
mcp__plugin_nx_nexus__memory_put(
    project="nexus",
    title="rdr-078-impl-state.md",
    content="<phase status, blocker notes, calibration values for PQ-2/PQ-20>"
)
```

PQ-2 (match threshold), PQ-20 (specificity bonus weight), and PQ-14 (scope precedence weight) need empirical calibration during P1 — record the chosen values in this file before claiming SC-1 met.

## References

- RDR-078 file: `/Users/hal.hildebrand/git/nexus/docs/rdr/rdr-078-unified-context-graph-and-retrieval.md`
- RDR-042 plan library: `src/nexus/db/t2/plan_library.py`
- Catalog graph: `src/nexus/catalog/catalog.py:1440` (`Catalog.graph()`)
- T1 server: `src/nexus/session.py:220-277`, RDR-041 PPID inheritance
- Migration registry: `src/nexus/db/migrations.py`
- MCP server: `src/nexus/mcp/core.py`
- Hooks: `nx/hooks/scripts/session_start_hook.py`, `nx/hooks/scripts/subagent-start.sh`
- Agents: `nx/agents/{strategic-planner,architect-planner,code-review-expert,substantive-critic,deep-analyst,deep-research-synthesizer,debugger,plan-auditor}.md`
