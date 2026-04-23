# Changelog

All notable changes to Nexus are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [4.10.0] - 2026-04-23

Ships operator bundling and the plan-use telemetry wiring. Contiguous runs of ≥2 operator steps (extract / rank / compare / summarize / generate) now collapse into a single `claude -p` subprocess instead of one spawn per step. Measured wins on real corpora: **-55%** on a 2-op `extract → summarize` chain, **-28%** on the blog-post Arcaneum tradeoffs plan (with materially better ranking quality), **-72%** on a 4-op cross-repo compositional query (192s → 54s, matched synthesis quality). Default is on (`bundle_operators=True`); the one-line escape hatch `bundle_operators=False` recovers per-step isolation for debugging. Separately, `plans.use_count` / `success_count` / `failure_count` finally populate — the `PlanLibrary.increment_run_*` methods existed since RDR-078 but had zero callers until this release. Skill wording for the five verb skills (`/nx:research`, `/nx:review`, `/nx:analyze`, `/nx:debug`, `/nx:document`) and `/nx:query` shifts from descriptive ("Routes through nx_answer") to imperative ("You MUST call nx_answer") to close the 6,537:0 audit deficit between direct `search` calls and `nx_answer` invocations.

### Added

- **Operator-bundle execution** (`src/nexus/plans/bundle.py`, new 500 LOC module). `segment_steps` walks a plan and emits `OperatorBundleSlice` markers for ≥2 contiguous operator runs; `compose_bundle_prompt` produces a single composite prompt describing all N steps with a plan-index → bundle-local-position map for cross-references; `dispatch_bundle` issues one `claude_dispatch` for the whole bundle. `BUNDLEABLE_OPERATORS` is the authoritative eligibility set with documented criteria (pure / cost-bounded / failure-meaningful-at-bundle-granularity). `MAX_BUNDLE_PROMPT_CHARS=200_000` guards against oversized composite prompts with per-step fallback. `DEFERRED_REF_KEY` is the shared sentinel for intra-bundle `$stepN.field` references.
- **`bundle_operators: bool = True` kwarg on `plan_run`** (`src/nexus/plans/runner.py`). New default collapses eligible segments via the bundle path; `False` flattens back to per-step dispatch. The runner routes through `segment_steps` as the sole bundle-boundary detector (no duplicate inline logic).
- **`supports_bundling` attribute on dispatchers** (`src/nexus/plans/runner.py::_default_dispatcher`). `plan_run` gates bundling on `getattr(dispatch, "supports_bundling", False)` instead of an identity check — decorators and wrappers either inherit or set their own marker, so timing wrappers and retry decorators don't silently disable bundling.
- **Deferred step-reference handling** (`src/nexus/plans/runner.py::_resolve_value`). Intra-bundle `$stepN.field` refs return a sentinel dict instead of raising; `compose_bundle_prompt` renders them as "STEP M output" prose. Covers the 7 of 20 bundle-eligible plans in the live library that chain between operators.
- **Parallel-branch source attribution** (`src/nexus/plans/bundle.py::OperatorBundleStep.source_collections`). Bundled operator steps whose inputs came from auto-hydration carry the pre-hydration `collections` arg into the prompt as a `source:` line, so the LLM can attribute parallel branches (e.g. Arcaneum extracts vs Nexus extracts in a cross-repo compare) to their originating corpora.
- **Plan-use telemetry wiring** (`src/nexus/mcp/core.py::_nx_answer_record_outcome`, `_plan_run` wrapping). `increment_run_started` fires before execution, `increment_run_outcome(success=bool)` fires after. Guarded on `best.plan_id > 0` to skip synthetic inline-planner matches. Errors swallowed locally so telemetry can never break the user-facing flow. Tests cover success-path, synthetic-match skip, and exception → `success=False` recording.
- **`scripts/bundle_sandbox_probe.py`** — reproducible live benchmark harness. `--runs N` (default 3) collects N samples per configuration and reports mean / stddev / min / max. `--dry-run` previews the composite prompt without spending API credit.
- **`scripts/rdr092_replay.py`** — plan-match validation harness. Clones `memory.db`, builds a fresh T1 cache, replays every plan's stored anchor + 10 synthetic noise probes; reports self-match rank distribution, confidence histogram, attractor counts, and noise-floor rejection rate.

### Changed

- **Verb skills (`research` / `review` / `analyze` / `debug` / `document`) and `/nx:query`** — wording flipped from descriptive to imperative. Each skill now has a "When direct `search` is fine" carve-out for single-corpus keyword lookups, and anti-patterns cite *composition* (not a blanket "analytical") as the deciding factor. The `using-nx-skills` common-mistakes table adds three mappings from bad `search(analytical-question)` shapes to correct `nx_answer` shapes (full `mcp__plugin_nx_nexus__` prefix throughout — no short-form syntax).
- **`_hydrate_operator_args` is now synchronous** (`src/nexus/plans/runner.py`). Previously declared `async def` with no `await` expressions. Removed the coroutine allocation per operator step and the misleading async contract.

### Fixed

- **`_DEFERRED_REF_KEY` is defined once** (`src/nexus/plans/bundle.py`), imported by `runner.py`. Previously duplicated across both modules with identical strings; a rename on one side would have silently corrupted bundle prompts.

## [4.9.13] - 2026-04-23

Ships RDR-092 (Plan Match-Text from Dimensional Identity) in full, plus the wheel-packaging deployment fix it surfaced. The plan library's T1 cosine cache and T2 FTS5 lane now both key off a hybrid `match_text` payload (`<description>. <verb> <name> scope <scope>`) instead of the raw `query` column. The R3-class state that motivated the RDR — 0/52 live plans with populated `verb` / `name` / `dimensions` — drains to 0 non-dimensional rows on `nx upgrade`. Phase 5 empirical validation confirmed both RDR targets: plan-#38 rank-1 attractor landings 4/10 → 1/10, noise probes above the 0.40 confidence floor 1/10 → 0/3. Version jumps 4.9.11 → 4.9.13 so both gated migrations (4.9.12 backfill and 4.9.13 match_text column) run on existing installs. Rolls up #266 / #267 / #268 / #269.

### Added

- **Hybrid `match_text` synthesiser** (`src/nexus/db/t2/plan_library.py::_synthesize_match_text`, `src/nexus/plans/session_cache.py`, RDR-092). Single source of truth that produces the description + dimensional-suffix string both lanes embed. `session_cache._synthesize_match_text` is a thin dict-unpacking adapter around the shared kwargs-based implementation. Shape: `"<description>. <verb> <name> scope <scope>"` when verb AND name are populated; raw description fallback when not. R10 validated the hybrid at zero verb-accuracy regression vs raw description.
- **`plans.match_text` column + `plans_fts` rebuild** (`src/nexus/db/t2/plan_library.py`, migration `_add_plan_match_text_column` at 4.9.13). Fresh installs get the column + FTS shape directly from `_PLANS_SCHEMA_SQL`; existing DBs pick it up via the migration, which drops + recreates `plans_fts` (FTS5 has no `ALTER COLUMN`) after backfilling `match_text` from existing verb/name/scope. Drop-before-backfill ordering avoids `database disk image is malformed` on external-content FTS updates. The column guard also checks `plans_fts` presence so an interrupted upgrade between `ALTER TABLE` and the FTS rebuild recovers on retry instead of silently landing empty FTS payloads.
- **Three-tier verb cascade on grown plans** (`src/nexus/mcp/core.py::_infer_grown_plan_verb`, `_infer_grown_plan_name`, RDR-092 Phase 0b). `nx_answer`'s ad-hoc grow path now populates `verb` / `name` / `dimensions` via (1) caller-supplied `dimensions["verb"]`, (2) operator-shape inference from `plan_json.steps` (compare → analyze; extract+rank → analyze; traverse+search+summarize → research), (3) `research` fallback. Name is kebab-case from the first 3-5 content tokens of the question with stop-words dropped.
- **`_backfill_plan_dimensions` migration at 4.9.12** (`src/nexus/db/migrations.py`, RDR-092 Phase 0d). Retroactively populates `verb` / `name` / `dimensions` / `scope` on every row with `dimensions IS NULL`. 29-stem verb dictionary (research / analyze / review / debug / document families) + wh-fallback catches edge cases. Within-loop collisions resolve via a deterministic row-id strategy suffix against an in-memory `claimed` set — reruns produce byte-identical identities so a rerun is a no-op. Low-confidence wh-fallback rows are tagged `backfill-low-conf` for operator review via `nx plan repair`.
- **`nx doctor --check-plan-library`** (`src/nexus/commands/doctor.py`, RDR-092 Phase 0c.2). Buckets plan rows into authored / backfilled / non-dimensional and reports the global-tier builtin count. Exits 1 when the builtin count falls below 9 (the pre-Phase-0a floor — partial installs on older plugin versions still pass). Non-dimensional rows surface a `nx plan repair` hint.
- **`nx plan repair` subcommand** (`src/nexus/commands/plan.py`, RDR-092 Phase 0d.2). Re-runs the backfill heuristic + lists `backfill-low-conf` rows with their inferred verb and original query text so operators can hand-correct edge cases. Idempotent.
- **Fail-loud loader in `nx catalog setup`** (`src/nexus/commands/catalog.py::_seed_plan_templates`, RDR-092 Phase 0c.1). Empty global tier during setup now raises `click.ClickException` instead of silently returning zero rows. Per-tier schema errors fan out to stderr via `click.echo`.
- **Three new YAML builtin plans** (`nx/plans/builtin/`, RDR-092 Phase 0a). `find-by-author.yml`, `citation-traversal.yml`, `type-scoped-search.yml` replace the three migrated shapes from the retired legacy `_PLAN_TEMPLATES` array. Each declares full `verb` / `scope` / `strategy` dimensions.
- **Per-call `min_confidence` override on `nx_answer`** (`src/nexus/mcp/core.py`, RDR-092 Phase 2 Option A). New `min_confidence: float | None` kwarg threads through both `plan_match` and the hit helper; default `None` preserves the RDR-079 P5 calibration (0.40). Verb skills that validated a stricter floor (0.50 per R9's 5+5 probe corpus) pin it per-call without moving the global knob. Bounds-checked: values outside `[0.0, 1.0]` fail loud.
- **Canary regression tests for the attractor behaviour** (`tests/test_plan_match.py::TestRdr092Canaries`). Locks in that random-token probes can't land rank-1 above the floor; positive-case companion for dimensional probes; telemetry-only concentration-ratio report.

### Changed

- **Retired the legacy `_PLAN_TEMPLATES` array** (`src/nexus/commands/catalog.py`, RDR-092 Phase 0a). The 5 hard-coded legacy templates were the upstream source of R3's 0/52 non-dimensional state. Three migrated to YAML; two (provenance, multi-corpus-compare) retired as redundant with `research-default` and `analyze-default`. Builtin count went 9 → 12 net.
- **Packaging: `nx/plans/` ships as wheel package data** (`pyproject.toml` `[tool.hatch.build.targets.wheel.force-include]`, `src/nexus/commands/catalog.py::_resolve_plugin_root`, nexus-b9f3). The wheel build now copies `nx/plans/` into `nexus/_resources/plans/`. `_resolve_plugin_root` resolves via `importlib.resources.files('nexus') / '_resources'` first (works for both wheel and editable installs via a `src/nexus/_resources/plans` symlink), with repo-root and legacy `__file__` walk as fallbacks. Before this fix, installed CLIs couldn't find the YAMLs from any cwd outside the nexus repo — the Phase 0c fail-loud guard exposed the pre-existing gap.
- **Docs: `docs/plan-authoring-guide.md`, `docs/plan-centric-retrieval.md`, `docs/cli-reference.md`** (RDR-092 Phase 4). New "match_text synthesis" section, builtin-templates table bumped 9 → 12, retrieval-trunk diagram updated for the `min_confidence` override. cli-reference documents `nx doctor --check-plan-library` and the new `nx plan` command group.
- **Test suite tightening** (`tests/test_bib_enricher.py`, `tests/test_doctor_search.py`, `tests/test_phase3_structured_chash.py`, #269). Patched `time.sleep` in the 429-backoff test (35s → 0.05s), mocked `claude_dispatch` in two `nx_answer` structured-envelope tests (84s → 0.72s combined), marked `test_flag_invokes_probe` as `@pytest.mark.slow` (122s deselected by default; opt-in via `-m slow`). Full unit suite: 12:31 → 8:02.

### Fixed

- **RDR-092 Problem Statement retrofit** (`docs/rdr/rdr-092-plan-match-text-from-dimensional-identity.md`). Added four `#### Gap N:` structured headings so the RDR-065 close gate's pointer-replay validation runs in one pass instead of blocking on malformed-new. Content unchanged; headings organise the existing prose.

## [4.9.11] - 2026-04-23

Plugin-side hardening. Shifts the `#### Gap N:` structural requirement in RDR Problem Statements from `/nx:rdr-close` (detected at close time) to `/nx:rdr-gate` (detected at gate time, before accept) so authors meet the rule before it bites. Closes the surprise-at-close pattern that bit RDR-091 on 2026-04-22: the RDR accepted cleanly without gap headings, then failed the close preamble, and the gap structure had to be retrofitted after the fact.

### Added

- **`/nx:rdr-gate` Layer 1 gap-structure check** (`nx/commands/rdr-gate.md`, `nx/skills/rdr-gate/SKILL.md`, `nexus-4qpb`). For RDRs with `id >= 65`, the preamble now validates the `## Problem Statement` section contains at least one `#### Gap N: <title>` heading. Missing headings emit a BLOCKED outcome with the same error and regex the close skill uses; the assistant stops at Layer 1 without running the assumption audit or AI critique. Legacy RDRs (`id < 65`) are grandfathered. `--skip-gaps` bypasses the check for rare RDRs where the structure does not fit; the override is recorded in the gate audit trail.

### Changed

- **`/nx:rdr-create` template and SKILL both reinforce the gap convention** (`nx/resources/rdr/TEMPLATE.md`, `nx/skills/rdr-create/SKILL.md`). Template comment now explicitly names both gate enforcement points (`/nx:rdr-gate` and `/nx:rdr-close`) so authors see "missing gaps will block the gate" at drafting time, not just at close. The Gap 1 + Gap 2 placeholders remain in place.

### Fixed

- **RDR-091 retrofitted with the gap heading it was drafted without** (`docs/rdr/rdr-091-scope-aware-plan-matching.md`). The RDR was drafted and accepted before gate-time enforcement landed, so the close skill flagged the missing structure after all the Phase 2a–2d work had already shipped. Added `#### Gap 1: plan_match silently drops scope_preference, so specialized plans lose to generic ones on scoped questions` to the Problem Statement and flipped frontmatter `status: accepted` → `status: closed` with `closed_pointers: Gap1=src/nexus/plans/matcher.py:77` for audit trail.

## [4.9.10] - 2026-04-23

Post-4.9.9 hardening — five GitHub issues filed on 2026-04-23 from the 4.9.9 shakeout (#249–#253), all observability or UX fixes to the boundaries between the catalog, taxonomy, and storage tiers. No user-facing behaviour changes on the `store_put` / `taxonomy-assign` / `split` paths; they just leave evidence now when something goes sideways. Rolls up as PR #254.

### Added

- **`nx catalog verify`** (GitHub #249, `src/nexus/commands/catalog.py`, `src/nexus/db/t3.py`). Reconciliation sweep that cross-checks every catalog tumbler against its target T3 collection to surface ghost tumblers — entries in the catalog whose `doc_id` has no matching row in ChromaDB. #244 closed the new-ghost source on the 4.9.9 put path, but latent ghosts from 4.9.7 / 4.9.8 installs still survived — discovered only when a user hit `store_get`. `verify` groups tumblers by `physical_collection` and batches `col.get(ids=[...], include=[])` at the 300-id cap for an ANN-less presence check. Missing collections (deleted, renamed) count every tumbler as a ghost. `--heal` walks ghosts interactively (`[d]rop tumbler / [p]rint put-cmd template / [s]kip / [q]uit`), `--json` emits a machine-readable map, `--collection NAME` scopes the sweep. Tumblers without `meta.doc_id` are unverifiable and skipped rather than reported as false positives. New T3 primitive `existing_ids(collection, ids)` backs the check (paginated, `include=[]`, missing collection → empty set). (nexus-23l3)
- **`nx taxonomy status` surfaces recent post-store hook failures** (GitHub #251, `src/nexus/db/migrations.py`, `src/nexus/mcp_infra.py`, `src/nexus/commands/taxonomy_cmd.py`). New `hook_failures` T2 table (`id`, `doc_id`, `collection`, `hook_name`, `error`, `occurred_at`) captures every exception `fire_post_store_hooks` catches — previously visible only in structlog output. `status` emits an `Action: N post-store hook failure(s) in the last 24h` line matching the v4.9.9 #239 / #243 pattern, so a silently-dropped `taxonomy_assign_hook` write (missing centroids, Chroma timeout) becomes actionable instead of a silent log smudge. Drop-and-warn policy preserved — the T3 row stays source of truth and the hook exception is never raised; this just makes the drops queryable. New `_record_hook_failure()` persist helper is best-effort (inner `try/except` falls through to a debug log on failure so a broken T2 never masks the primary hook exception). Read path is also best-effort — pre-4.9.10 DBs stay silent rather than blowing up. Migration registered at 4.9.10 and activates automatically with this release. (nexus-4dkf)
- **`nx doctor --check-taxonomy`** (GitHub #252, `src/nexus/commands/doctor.py`). Verifies the `topic_links ≡ projection-assignment` invariant that #240 maintained via single-caller discipline (`_persist_assignments` was the only caller of `refresh_projection_links`). Drift SQL finds topics with `assigned_by='projection'` rows but no entry in `topic_links`; exits 0 with count when the invariant holds, exits 1 with up to 10 named topics and a `Fix: nx taxonomy project --backfill --persist` hint on drift. Closes the detection gap without refactoring the domain layer — a future caller that writes projection assignments through `assign_topic` directly, or a test fixture that seeds rows, will be caught before the materialized view goes stale in production. (nexus-xhf1)
- **`nx taxonomy split` prints a next-step hint pointing at `label`** (GitHub #250, `src/nexus/commands/taxonomy_cmd.py`). `split_topic` creates children with `review_status='pending'` and n-gram labels; users previously got no signal that the documented `split → label → review` workflow still needed the labeler run. After a non-zero split, echo `Action: N new sub-topics have n-gram labels. Run nx taxonomy label -c <coll>` — collection scope resolves from `--collection` or the parent topic row so the hint stays precise even when the user resolves the topic by label alone. Matches the status-hint pattern v4.9.9 adopted. No hint on no-op splits (`child_count=0`). (nexus-ir2g)

### Fixed

- **MCP `store_put` silently swallowed `_catalog_store_hook` failures** (GitHub #253, `src/nexus/mcp/core.py`). A bare `try/except: pass` around the catalog-registration call in the MCP `store_put` wrapper hid every hook exception — schema-lock contention, corrupt catalog db, import errors — producing the inverse-of-#244 orphan shape: T3 row present, no catalog tumbler, nothing surfaces. Matches the `fire_post_store_hooks` pattern now: `catalog_store_hook_failed` warning with `doc_id`, `collection`, and `exc_info=True`. Policy stays non-fatal — T3 is source of truth — but failures are now observable. `nx catalog verify` (#249 above) can detect the inverse orphan shape from the T3 side if it becomes a recurring issue. (nexus-tsyg)

## [4.9.9] - 2026-04-22

Rolls up the kxez (GitHub #243 + #241) and gwhy (#238 / #239 / #240 + review-response) taxonomy fixes that were merged to `main` after the v4.9.8 tag was cut; they did not ship in the 4.9.8 PyPI artifact. Plus two bugs found during the 4.9.8 shakeout, plus three small `nx catalog` CLI polish items that had been sitting as open beads since 2026-04-19 (below).

### Added

- **`nx catalog links --resolve`** (`src/nexus/commands/catalog.py`, bead `nexus-i63n` rolled into `nexus-iojz`). Default output is raw tumblers (`1.17.14 → 1.1.107`), which is unreadable for external audiences and still clunky when project-hacking. `--resolve` renders each endpoint inline as `<title-or-path> (<tumbler>)` using the existing `catalog.resolve()` entry lookup: prefer `title`, fall back to `file_path`, then bare tumbler.
- **`nx catalog links --unique-targets`** (`src/nexus/commands/catalog.py`, bead `nexus-x6eu` rolled into `nexus-iojz`). Collapses edges that point at the same `file_path` via different owner tumblers (the shape re-indexing after owner-rename produces). Stable first-seen-wins; fails open to bare tumbler if the target does not resolve. Workaround for the deeper re-index dedup gap, not a replacement for it.
- **`nx catalog stats` now includes a topics block** (`src/nexus/commands/catalog.py`, bead `nexus-1n0t` rolled into `nexus-iojz`). Previously reported owners / documents / links / by_type / by_link_type; the catalog's third layer (`CatalogTaxonomy`) was silent. New section shows total assignments, distinct topics assigned, and projection breakdown by source collection. `--json` carries the same data under a top-level `taxonomy` key. Skipped quietly when T2 is absent or carries no topic rows.

### Fixed

- **`store_put` silently dropped oversized documents, leaving catalog ghosts** (GitHub #244, `src/nexus/db/t3.py`, `src/nexus/errors.py`). `_write_batch` dropped-and-warned any document over the 16384-byte ChromaDB Cloud cap and returned normally. `put()` then returned the computed deterministic `doc_id`, and the MCP `store_put` wrapper went on to register that `doc_id` in the nexus-catalog. Result: a catalog entry with `physical_collection` + `doc_id` pointing at a row that was never written (three reproduced cases in the filing session). New `PutOversizedError` is raised on the put path (`fail_on_oversized=True`); indexer batch paths keep the existing drop-and-warn behaviour because a chunker upstream was already supposed to guard against this and a pipeline-wide raise is worse than dropping one record. The caller of `put()` now sees the error and skips catalog registration, so no ghost is created. Workaround for existing ghosts: re-run `store_put` with the same `title` and in-budget content, since `doc_id` is deterministic (`sha256(collection:title)[:16]`); the live row attaches to the existing catalog tumbler. (nexus-akof)
- **Inline planner generated `query(..., author='arcaneum')` filters that returned zero** (bead `nexus-sgrg`, `src/nexus/mcp/core.py`). The LLM planner saw the `author=""` parameter on the `query` tool and emitted `author=<repo-name>` as a plausible scope filter. The catalog's `author` column is rarely populated for RDR / docs collections, so the filter almost always matches nothing, extract gets an empty input, and generate honestly reports "no synthesis produced." `_PLANNER_TOOL_REFERENCE` now documents that `author=` is rarely populated and biases callers toward `corpus=<collection>` for project scoping. (nexus-sgrg)
- **`nx taxonomy label` silently skipped split sub-topics** (GitHub #243, `src/nexus/commands/taxonomy_cmd.py`, `src/nexus/db/t2/catalog_taxonomy.py`). The pre-check and the `--all` relabel path both used `get_topics()` which returns only root topics (`parent_id IS NULL`). After a `nx taxonomy split`, `status` correctly reported the new children as pending, but `label` reported "No topics to label" and the documented `split → label → review` workflow silently stalled with cryptic n-gram labels on the sub-topics. New `get_all_topics()` helper returns roots + children; both the pre-check and the batch relabeler now use it. (nexus-kxez)
- **`nx taxonomy label` auto-accepted topics** (GitHub #241 Item 3, `src/nexus/commands/taxonomy_cmd.py`, `src/nexus/db/t2/catalog_taxonomy.py`). `rename_topic` sets `review_status='accepted'` as a side effect (correct for the interactive `review` rename path where the human is acknowledging the label). The batch LLM relabeler was also calling it, short-circuiting the documented `pending → review → accepted` flow so `status` showed topics as "accepted" with no human ever having seen them. New `update_topic_label()` helper updates the label without touching status; `relabel_topics` now uses it. After this fix, `label` leaves topics `pending`, and `review` is the step that transitions them to `accepted`. (nexus-kxez)
- **`nx taxonomy list` hid collection membership** (GitHub #241 Item 1, `src/nexus/commands/taxonomy_cmd.py`). The flat topic dump with no collection column made it impossible to tell which topic belonged to which collection on multi-collection setups. Root rows now carry a `[collection]` prefix, consistent with the format `hubs` and `links` already use. (nexus-kxez)
- **`nx taxonomy project --help` threshold resolution text was stale** (GitHub #241 Item 2, `src/nexus/commands/taxonomy_cmd.py`). Docstring said "fallback to prefix default → 0.70" but actual defaults are per-prefix (0.70 code, 0.50 knowledge, 0.55 docs / rdr). Replaced with a pointer to the `--threshold` option help for the real table. (nexus-kxez)
- **`nx taxonomy project <src>` narrowed targets vs `--backfill` for the same source** (GitHub #238, `src/nexus/commands/taxonomy_cmd.py`). Single-source tried `list_sibling_collections` (same-hash, different-prefix) first and fell back to all-collections only when siblings were empty; `--backfill` always used the full set. For multi-repo families (e.g. three `docs__<repo>-<hash>` from distinct projects), single-source targeted fewer collections than `--backfill` on the same source. Dropped the sibling-first heuristic. Default is now every collection with topics minus the source, matching `--backfill`. Use `--against` to scope explicitly when the default is too wide. (nexus-gwhy)
- **`nx taxonomy status` didn't surface zero-projection collections** (GitHub #239, `src/nexus/commands/taxonomy_cmd.py`, `src/nexus/db/t2/catalog_taxonomy.py`). A collection fresh from `discover` has own-collection topics but no cross-collection projection data; previously the status default output showed the collection as healthy and the gap was only visible via `audit -c <coll>`. Status now queries projection-assignment counts per collection, flags rows with topics but zero projection inline as `[no projection]`, and surfaces an `Action:` hint naming the concrete `project --persist` invocation. New `get_projection_counts_by_collection()` T2 helper backs the query. (nexus-gwhy)
- **`nx taxonomy rename` silently transitioned topics to `accepted`** (code-review finding M-1, `src/nexus/commands/taxonomy_cmd.py`). The standalone `rename` command called `rename_topic`, which atomically renames + accepts. Correct for the interactive `review` rename path, but surprising for a user fixing a typo on a still-pending topic. New `--no-accept` flag uses `update_topic_label` instead; default behaviour preserved (typing a new label is an acknowledgement). (nexus-gwhy)
- **`refresh_projection_links` released the taxonomy lock between its aggregate SELECT and per-pair upserts** (code-review finding C-1, `src/nexus/db/t2/catalog_taxonomy.py`). A concurrent `assign_topic` or `taxonomy_assign_hook` firing in the gap could change `topic_assignments` between the two phases, yielding stale `link_count` values. Hoisted both phases into a single `with self._lock:` block. (nexus-gwhy)
- **`nx taxonomy status` missing-projection `Action:` count could under-report under `-n`** (code-review finding C-2, `src/nexus/commands/taxonomy_cmd.py`). The count was computed from the already-truncated `rows` slice rather than the full `all_topics` universe, so `status -n 5` on a 20-collection install named at most the top-5 by `doc_count`. Count is now computed from the unfiltered universe. (nexus-gwhy)
- **`nx taxonomy links` stayed stale after `project --persist`** (GitHub #240, `src/nexus/commands/taxonomy_cmd.py`, `src/nexus/db/t2/catalog_taxonomy.py`). `project --persist` wrote per-chunk projection rows to `topic_assignments` but never updated `topic_links`, so `links` (which reads the cache) disagreed with `hubs` (which queries live). New `refresh_projection_links()` T2 method aggregates per-chunk projection rows into canonical `(from_topic_id, to_topic_id, count)` pairs and upserts them into `topic_links` with `'projection'` merged into any existing `link_types` set (so catalog-derived `cites` / `implements` survive). Called at the end of `_persist_assignments`, so single-source `project --persist` and `--backfill --persist` both leave `links` in sync with `hubs`. (nexus-gwhy)

## [4.9.8] - 2026-04-22

### Added

- **`operator_compare` gains two-sided mode** (`src/nexus/mcp/core.py`). Plans that need to compare extractions from two separate corpora (a cross-corpus DAG like "how does project A frame X vs how does project B frame X") can now pass `items_a` / `items_b` / `label_a` / `label_b` as independent args. The resolver substitutes each `$stepN.<field>` reference at top level, which the previous one-sided `items` arg could not do (the no-inline-interpolation rule means references embedded in a `focus` string stay literal). The two-sided prompt asks for shared axes, divergent decisions, side-only axes, and philosophy difference. One-sided `items` mode preserved unchanged; list / dict values in any of the `items*` args are now JSON-serialized before prompt interpolation so the LLM sees clean JSON instead of Python repr. Live cross-corpus run against Arcaneum + Nexus RDR corpora produced a clean synthesis (shared-axes table, divergent-decisions table, philosophy paragraph) with zero training-data fill-in. (nexus-km5i)

### Fixed

- **`catalog_search` rejected `content_type` as a sole filter** (`src/nexus/mcp/catalog.py`). The structured-filter trigger condition (`if owner or corpus or file_path or (author and not query)`) omitted `content_type`, so a sole `content_type` value fell through to the FTS5 path which requires a free-text query. Documented behaviour was wrong for callers asking "show me everything of type prose" without a search term. Added `content_type` to the trigger; pre-existing structured-filter SQL already handled `content_type = ?` correctly. (nexus-3o3t)
- **`store_list --docs=true` showed `?` for chunk count on every entry** (`src/nexus/mcp/core.py`). Per-doc `chunk_count` was read from chunk metadata, but `store_put` doesn't set that field — only the PDF indexer does. The dedup pass now derives the chunk count per content-hash; the page-count column is omitted entirely when no document carries one (was always `?p` for non-PDF entries). (nexus-3o3t)
- **`store_get` failed silently when given a title** (`src/nexus/mcp/core.py`). `store_list` displays titles; `store_put`/`search` return hashes; the MCP tool docstring promised hashes but the natural `list → get` copy-paste flow was broken. Now: try the input as a hash first; if not found and it doesn't look like a 16-char hex hash, fall back to `find_ids_by_title()`; on multi-match, list candidate hashes; on miss, error message names both the hash and title paths. Also surfaces the 16-char content-hash in the `--docs=true` listing so a hash is always within copy-paste reach. (nexus-3o3t)

### Changed

- **`T1Database.get` and `_reconnect` emit structured diagnostic logs** (`src/nexus/db/t1.py`). A user-reported `scratch put` → `scratch get` round-trip failure (`Not found` on the freshly-returned UUID) couldn't be reproduced in isolation. Added `t1_get_miss` (with requested_id, session_id, client_type, dead) and `t1_reconnect_to_different_server` (with prior + new session_id and host:port) so the next occurrence carries enough context to diagnose. No behaviour change. (nexus-3o3t)

## [4.9.6] - 2026-04-22

### Added

- **RDR-091: Scope-Aware Plan Matching.** `nx_answer`'s `scope` parameter was accepted and documented but silently ignored on the library-match path: when `plan_match` hit a saved plan, the plan's own corpus arg (if any) was used verbatim, so scoped calls could end up searching unrelated corpora. Phase 1 (`src/nexus/plans/runner.py`) now injects the caller's scope into retrieval step args when the plan does not pin a corpus. Phase 2 (`src/nexus/plans/matcher.py`, `src/nexus/plans/scope.py`) adds a `plans.scope_tags` column (4.8.0 migration), inference from retrieval-step `corpus` / `collection` args, and a scope-conflict filter + scope-fit boost + specificity tie-break in the matcher. `plan_save` MCP tool gains an optional `scope_tags` kwarg; `plan_search` output now surfaces the stored scope tag. Score formula is multiplicative per RDR spec: `final_score = base_confidence * (1 + scope_fit_weight * scope_fit)` with `scope_fit_weight = 0.15`. Empty `scope_preference` is a hard no-op (no boost, no filter). See `docs/plan-authoring-guide.md` §`scope_tags` and `docs/plan-centric-retrieval.md` §Scope-aware matching. (nexus-zs1d, nexus-x6pr, nexus-bgs7, nexus-svcg, nexus-jvma)

### Fixed

- **`"all"` corpus sentinel was inferred as a concrete scope tag**, filtering the seven builtin plans that use `corpus: all` out of every scoped `nx_answer` call. `_infer_scope_tags` now skips the sentinel alongside `$var` placeholders. A 4.8.1 rewash migration cleans up rows contaminated by the pre-fix backfill. (`src/nexus/plans/scope.py`, `src/nexus/db/migrations.py`, nexus-dfok)
- **`scope_tags` backfill overwrote explicit values on every process start.** The migration now guards `WHERE scope_tags = ''` so plans authored with `save_plan(scope_tags='rdr__arcaneum')` survive across MCP server / CLI restarts. (`src/nexus/db/migrations.py`, nexus-dfok)
- **Grown plans from scoped `nx_answer` calls were always agnostic.** `_infer_scope_tags` cannot see the runtime `_nx_scope` corpus injection (that lives in bindings, not `plan_json`). The grown-plan save path now passes `scope_tags=scope` explicitly so each grown plan is anchored to the retrieval space that produced it. (`src/nexus/mcp/core.py`, nexus-dfok)
- **Case-sensitive `scope_tags` prefix match surprised callers passing the ChromaDB-conventional lowercase scope** when real collections carried mixed case (`code__Delos-5af9bfe0` alongside `knowledge__delos`). `_scope_fit` now folds both sides before comparison; `_HASH_SUFFIX_RE` accepts upper-hex too. Stored values preserve original case; only the compare is case-folded. (`src/nexus/plans/matcher.py`, `src/nexus/plans/scope.py`, nexus-yi7m)

### Changed

- **`Match` dataclass gains a `scope_tags: str = ""` field** populated from the new column. All existing `Match(...)` callers keep working because the field is defaulted. (`src/nexus/plans/match.py`)
- **Scope-tag helpers (`_normalize_scope_string`, `_infer_scope_tags`, `_SCOPE_AGNOSTIC_SENTINELS`) moved out of `plan_library.py`** into a standalone `src/nexus/plans/scope.py` module, breaking a `migrations -> plan_library -> migrations` circular import path. Re-exported from `plan_library` for backward compatibility. (code-review follow-up)

### Docs

- **`docs/plan-authoring-guide.md`** gains a `scope_tags (matcher routing)` section covering inference vs explicit, normalization contract, matching semantics, multi-corpus bridging plans, interaction with grown plans, and authoring guidance. Clearly distinguishes `scope_tags` (matcher routing) from the `scope` dimension (publication tier).
- **`docs/plan-centric-retrieval.md`** gains a `Scope-aware matching` section covering filter / boost / tie-break mechanics, zero-candidate fallback to the inline planner, and prefix semantics (bidirectional `startswith`, intersect rules for multi-corpus plans). Quotes the final `scope_fit_weight=0.15` value.
- **`docs/rdr/rdr-091-scope-aware-plan-matching.md`** is the design record. `implementation_notes:` frontmatter records the picked weight, the multiplicative-formula correction history, and the critic follow-up.

## [4.9.5] - 2026-04-21

### Changed

- **`nx doctor` `Local collections` line now reports the empty-collection count** (`src/nexus/health.py`). After deleting every doc from a collection, `nx collection list` continues to show the collection at `0 chunks` because the empty collection is intentionally retained — it preserves the embedding-model binding so the next `store_put` doesn't have to re-derive it. The absence of a doctor signal made this look like a leak. Output now reads `Local collections: N collections (including K empty), <size> on disk`; the `(including K empty)` clause is omitted when `K == 0`. Pure transparency — no behavior change. (nexus-obp2)

## [4.9.4] - 2026-04-20

### Fixed

- **`nx store delete` left the catalog entry visible until the next `nx catalog gc`** (`src/nexus/commands/store.py`). The MCP `catalog_links` tool already filtered deleted-endpoint links immediately, so the eventual-consistency gap surprised users who expected delete to be atomic. After the T3 delete succeeds, look up each doc by `meta.doc_id`, tombstone the catalog row, and remove it from SQLite. Best-effort: silently skips when the catalog is uninitialised. (nexus-43pq)
- **`nx scratch get` rejected the 8-char prefix that `nx scratch list` printed** (`src/nexus/commands/scratch.py`). `delete` already accepted the prefix; `get` required the full UUID, breaking the natural `list → get <prefix>` copy-paste flow. Extracted `_resolve_entry_id()` from `delete_cmd` and reused it in `get`. Ambiguous prefixes still error with the candidate count. (nexus-43pq)
- **`nx collection info` reported the cloud Voyage model name in local mode** (`src/nexus/commands/collection.py`). `embedding_model_for_collection()` always returns the Voyage tag (its docstring even says "callers in local mode bypass this"); collection info forgot to. Now branches on `is_local_mode()` and reports `<minilm-or-bge> (local)` when local. Prevents callers from trusting the collection's self-reported model and reindexing with an incompatible embedder. (nexus-43pq)
- **`nx search` printed `:0:<content>` for results without a `source_path`** (`src/nexus/formatters.py`). The `path:line:content` format gracefully degrades to empty path + line 0 for `knowledge__*` / `docs__*` entries that aren't file-backed. `format_plain` now falls back to the MCP-style `[distance] title\n  snippet` format when no source path is present. (nexus-43pq)
- **`nx doctor` printed `Fix:` under passing (`✓`) checks** (`src/nexus/health.py`). The `Embedding model: all-MiniLM-L6-v2 (384d)` line carried `Fix: Upgrade: pip install conexus[local]…` even though nothing was broken. Renamed the prefix to `Suggest:` for `r.ok=True` results; `Fix:` is reserved for actual failures. (nexus-43pq)
- **`nx doctor` reported `T1 sessions: N session file(s), no orphans detected` even when sessions belonged to dead Claude Code instances** (`src/nexus/health.py`). The orphan check only inspects whether the chroma server PID is alive; long-lived chroma servers from prior conversations are technically "live" by that definition. Output now lists each session file with `(pid <N> alive, age <H>m/h)` so the state is transparent — and the failure message clarifies that "orphan" means the chroma pid is dead. (nexus-43pq)

### Changed

- **`nx store --help` tagline** updated from `(ChromaDB Cloud + Voyage AI)` to `(local ChromaDB or Cloud + Voyage AI)`. Local mode is the zero-config default; the prior tagline misled fresh installs. (nexus-43pq)
- **MCP `scratch` list / search** display now uses the same 8-character UUID prefix as the `nx scratch list` CLI (`src/nexus/mcp/core.py`). Previously the MCP surface showed 12 chars while the CLI showed 8, complicating copy-paste between the two. (nexus-43pq)

## [4.9.3] - 2026-04-20

### Fixed

- **Nested operator subprocesses stomped the parent's `current_session` flat file** (`src/nexus/hooks.py`, `src/nexus/operators/dispatch.py`, `src/nexus/db/t1.py`). After a parent Claude session ran any tool that fired `claude_dispatch` (operator_summarize, operator_generate, etc.), the subprocess's `SessionStart` hook unconditionally rewrote `~/.config/nexus/current_session` with its own transient UUID. The subprocess wrote no on-disk session record (skip-T1 path), so the parent's shell-side `nx scratch` / `nx memory` then resolved to a ghost UUID, found no record, and silently fell back to EphemeralClient for the rest of the conversation. Three coordinated changes resolve it: `claude_dispatch` exports `NX_SESSION_ID=<parent-uuid>` in subprocess env (populates the discriminator); `session_start` honours `NX_SESSION_ID` by preferring it as the resolved `session_id` and skipping the `write_claude_session_id()` call (preserves the parent pointer); `T1Database.__init__` short-circuits to EphemeralClient under `NEXUS_SKIP_T1` without searching for a session record (otherwise the operator would inadvertently connect to the parent's T1 server). Stateless-operator semantics preserved; cross-conversation T1 contamination eliminated.

### Added

- **Plugin↔CLI version drift detection at MCP server startup** (`src/nexus/mcp_infra.py`, `nx/.mcp.json`). The plugin and CLI ship from one `pyproject.toml` (CI enforces marketplace.json parity) but the user runs two separate update commands — `uv tool upgrade conexus` and `/plugin update nx@nexus-plugins`. After drift, the plugin's hooks may invoke flags the CLI no longer recognises. Extended `check_version_compatibility()` (already called from each MCP server's `main()` for CLI ↔ T2 schema drift) with a second case: read `${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json`'s `version` field, compare against `importlib.metadata.version("conexus")`, log `plugin_cli_version_mismatch` warning on minor or major divergence with the actionable update hint for the lagging side. Patch-level drift ignored (within-minor releases are wire-compatible). Never blocks startup. The MCP server is the natural single binding point — `nx-mcp` and `nx-mcp-catalog` are conexus entry points; plugin/CLI coupling runs entirely through that surface. Modelled on JupyterLab/VSCode's runtime-recheck-on-every-load pattern. `nx/.mcp.json` gains an explicit `env: {"CLAUDE_PLUGIN_ROOT": "${CLAUDE_PLUGIN_ROOT}"}` block so the spawned MCP server sees the variable as an env var (not just a path-substitution token).

## [4.9.2] - 2026-04-20

### Fixed

- **T1 scratch was bound to the terminal session, not the Claude conversation** (`src/nexus/session.py`, `src/nexus/hooks.py`, `src/nexus/db/t1.py`). Two `claude` invocations in the same shell shared one T1 server because `session_start` walked the PPID chain looking for "the ancestor session file" and landed on the login shell. The Claude conversation UUID was already arriving via the SessionStart hook payload but was stored only *inside* each session record, never as the *filename*. Fix: drop the PPID walk; key session files on `{session_id}.session`; resolve the UUID via the existing `current_session` flat file (subagent inheritance) or `NX_SESSION_ID` env var (opt-in). Subagents within one conversation continue to share the parent's T1; two parallel conversations now correctly get distinct T1s. Migration: numeric-stem session files (legacy PID-keyed) are swept unconditionally on first new-code SessionStart.
- **T1 chroma server killed immediately after spawn (regression from 4.9.1)** (`src/nexus/session.py`). The `atexit.register(stop_t1_server, proc.pid)` added in 4.9.1 as a "defence-in-depth fallback" was wrong: it ran inside the short-lived `nx hook session-start` process, which exits within seconds of spawning chroma. atexit then killed every chroma server right after spawn — production T1 silently fell back to EphemeralClient on every Claude conversation since 4.9.1 shipped. Fix: remove the atexit registration. The chroma server is meant to outlive the hook process; cleanup belongs to the SessionEnd hook (kept). Ungraceful exits leak the server until the next SessionStart's `sweep_stale_sessions` reaps it — the pre-4.9.1 design.
- **`nx doctor` did not detect missing Node.js / `npx`** (`src/nexus/health.py`). The plugin's `sequential-thinking` and `context7` MCP servers are spawned via `npx -y …` and silently fail without `npx` on PATH. Added a non-fatal `npx (Node.js, plugin-only)` line to `_check_tools()` with `nodejs.org` install hints. CLI users without the plugin keep `exit 0`.

### Added

- **`NEXUS_SKIP_T1` env var** (`src/nexus/hooks.py`, `src/nexus/operators/dispatch.py`). Opt-out for callers that don't want a T1 chroma server spun up for short-lived `claude -p` invocations. `claude_dispatch` (every operator + the taxonomy labeler) now exports it in the subprocess env, so per-call chroma startup overhead drops to zero. T1 client falls back to EphemeralClient when no server record is found — correct semantics for stateless operator subprocesses.
- **`scripts/sandbox-t1-uuid.sh`** — E2E shell harness that exercises the full SessionStart/SessionEnd lifecycle against real chroma servers in an isolated `NEXUS_CONFIG_DIR`. 20 checks cover: distinct UUIDs → distinct servers, subagent inheritance via same UUID, `NEXUS_SKIP_T1` honoured, legacy migration sweep, SessionEnd cleanup. Caught the 4.9.1 atexit regression.

## [4.9.1] - 2026-04-20

### Fixed

- **T1 ChromaDB child process leak** (`src/nexus/session.py`). The `SessionEnd` plugin hook removed in v1.10.1 with the reasoning *"T1 server stops with process tree; hook was a no-op"* was load-bearing — chroma is intentionally spawned with `start_new_session=True` (so `safe_killpg` reaches its multiprocessing workers and avoids POSIX-named-semaphore exhaustion; beads `nexus-dc57` / `nexus-ze2a`), which also detaches it from the terminal's process group, so OS-level reaping never collects it. Symptom on the maintainer's machine: 43 leaked `chroma run …nx_t1_*` processes accreting over 2+ days, plus 51 orphan tmp dirs in `/var/folders/.../T/nx_t1_*`. The plugin-side hook re-registration ships in the `nx` plugin (see `nx/CHANGELOG.md`). The `nexus` Python package adds an `atexit.register(stop_t1_server, pid)` fallback in `start_t1_server` for cases where the hook can't fire (harness teardown cancels the hook, OOM, terminal SIGHUP swallowed by the new-session boundary). Idempotent against already-dead PIDs. Two new regression tests pin both halves of the fix.

## [4.9.0] - 2026-04-19

### Added

- **`nx index --debug-timing` — prose and PDF file paths now instrumented** (nexus-7niu extension). The scaffold PR (shipped earlier this release cycle) covered code files; this follow-up extends the same `StageTimers`-via-`IndexContext` wiring to the other two per-file paths the `nx index repo` loop exercises. `prose_indexer.index_prose_file` wraps the markdown / line-based chunker, the CCE / local embed call, and the T3 upsert + chash dual-write + taxonomy-assign block. `_index_pdf_file` (the repo-loop PDF wrapper in `indexer.py`) gets the same three-stage decomposition with a local `contextlib.nullcontext` fallback so the `stage_timers=None` fast path stays zero-overhead. The `_run_index` prose-file and PDF-file loops now build a fresh `StageTimers` per file when the CLI's `on_stage_timers` callback is installed, matching the code-file loop's pattern exactly. Three new tests in `tests/test_indexer.py` pin the callback contract for each of the three paths (code / prose / PDF); `tests/test_stage_timers.py` primitive coverage unchanged. Remaining un-instrumented sites per the bead's design doc — `doc_indexer.batch_index_markdowns` (RDR ingestion, batch-shaped rather than per-file) and `pipeline_stages.uploader_loop` (streaming PDF pipeline with decoupled extract/chunk/upload threads) — require separate design because their shapes don't map cleanly onto the per-file `StageTimers` contract.
- **`nx index --debug-timing` per-stage intra-file timing breakdown** (nexus-7niu scaffold, vatx Gap 4b). Operators investigating the 89–95 s per-file stalls surfaced by the parent bead (nexus-vatx) could see an aggregate retry summary (Gap 4a) but not a decomposition of "was voyage slow, was chromadb slow, or was chunking choked on a giant file?" This PR ships the primitive + the first instrumented call site. A new `StageTimers` dataclass in `src/nexus/stage_timers.py` accumulates four buckets (chunking / embed / upload / retry) with a `.stage(name)` context manager that snapshots `nexus.retry.get_retry_stats()` before and after to correctly attribute backoff sleep to the retry bucket rather than the embed bucket. The `code_indexer.index_code_file` hot path is now wrapped in three `ctx.stage_timers.stage(...)` blocks (silent no-ops when the ctx field is `None` — zero overhead on normal runs). `_run_index` and `index_repository` gain an `on_stage_timers: Callable[[Path, StageTimers], None] | None` parameter; when the CLI passes `--debug-timing` it collects per-file timers and renders an end-of-run breakdown to stderr (`[debug-timing] per-stage totals across N files: chunking_s … embed_s … upload_s … retry_s …` with percentages). Prose, PDF, and pipeline-stages uploader sites will be instrumented in follow-up PRs — the scaffold keeps the scope of this change reviewable. 11 primitive tests + 2 indexer-integration tests + 3 CLI tests (16 total) pin accumulation, retry-delta attribution, aggregate/report formatting, and CLI wiring.
- **`nx doctor --check-quotas` pre-flight diagnostic** (nexus-c590, Gap 5 of nexus-vatx split off at release). Emits a three-section report: ChromaDB Cloud free-tier limits (drawn from `nexus.db.chroma_quotas.QUOTAS` — `MAX_QUERY_RESULTS`, `MAX_RECORDS_PER_WRITE`, `MAX_CONCURRENT_*`, `MAX_DOCUMENT_BYTES`, and neighbours) plus a live reachability probe of the cloud tenant; Voyage AI per-model token + dimension caps (`voyage-3`, `voyage-code-3`, `voyage-context-3`) with `VOYAGE_API_KEY` presence check; and a cumulative retry-accumulator summary pulled from `nexus.retry.get_retry_stats()` so any transient-error backoffs observed in the current process surface alongside the static limits. Exits 1 when the cloud tenant is unreachable in cloud mode (actionable fail), 0 in local mode or on a healthy cloud connection. `--json` returns a structured `{chromadb, voyage, retry}` dict for dashboards / CI gates. Six new tests in `tests/test_doctor_cmd.py::TestCheckQuotas` cover reachable + unreachable + local-mode + no-voyage-key + nonzero-retry + JSON-schema paths.

### Fixed

- **`nx collection health` chunk count now comes from T3, not the catalog** (nexus-39zi). The old report computed ``chunk_count`` as ``SELECT SUM(chunk_count) FROM catalog.documents WHERE physical_collection = ?``, which silently drifted to 0 on 129/143 production collections because the catalog's ``chunk_count`` column is only written by the paths that register through the catalog — direct ``store_put``, cloud-side operations, and tenants that predate the column leave it untouched. The 2026-04-18 live shakeout surfaced the drift: ``nx collection list`` showed ``code__ART-8c2e74c0`` with 63 077 chunks while ``nx collection health --format=json`` reported 0. Health now calls ``coll.count()`` on the live T3 collection (the same source ``nx collection list`` uses) so the two commands cannot disagree. A new `chunk_count_fn` parameter on `compute_collection_health` makes the source injectable for tests; the existing `catalog_stats_fn` still owns `last_indexed` and `orphan_count` (catalog-side properties). Four new tests pin the precedence rule, the exact drift case from the live shakeout, backward-compat for legacy callers, and the removal of ``chunk_count`` from ``_default_catalog_stats_fn``'s return. Same PR also folds the `_default_chash_coverage_fn` to use the public `ChashIndex.count_for_collection()` method instead of reaching into `_lock` (carryover from the review-remediation sweep that missed this site).

## [4.8.0] - 2026-04-18

### Added

- **`nx index` end-of-run retry-time summary** (nexus-vatx Gap 4a). Process-local counters in `nexus.retry` track aggregate voyage + chroma backoff seconds and retry counts; `nx index repo` resets them on start and emits `Transient-error backoff: Xs total (voyage … , chroma … )` at the end when any retry fired. Silent on clean runs so the normal output stays tidy. New `get_retry_stats()` / `reset_retry_stats()` public API. Three new tests pin counter accumulation across the voyage and chroma paths plus reset semantics.
- **`nx index` periodic ETA line** (nexus-vatx Gap 3). A new background ticker in `nx index repo` emits `[eta] N/total files · C chunks · Xs/file avg · ~M min remaining` to stderr every 60 s, independent of stdout's TTY state. Tqdm's built-in bar suppresses itself when stdout is redirected (CI logs, `nohup`, `tail -f`), leaving operators with no pace signal — the ticker fills that gap. Lifecycle: starts on `on_start` when the total file count is known, stops in a `finally` block so a mid-run exception still reaps the daemon thread. The first tick before any file completes renders `pending` rather than dividing by zero. Three formatter tests + three ticker-lifecycle tests pin the behaviour.
- **`nx index` post-processing phase markers** (nexus-vatx Gap 2). After the per-file `[N/N]` progress bar finishes, the pipeline keeps running for several seconds to minutes — RDR discovery, misclassified-chunk pruning, deleted-file pruning, pipeline-version stamping, and catalog registration. Previously the operator saw silence and could not tell hung from busy. A new `on_phase` callback threaded through `index_repository` → `_run_index` emits `[post] <phase>…` / `[post] <phase> done (Xs)` lines to stderr for each phase, bookended by `[post] Post-processing complete (Xs)`. The `nx index` CLI wires the callback to `click.echo(..., err=True)` so markers are visible even when stdout is redirected to a file. Four new tests in `tests/test_indexer.py` pin the phase surface.

### Changed

- **Voyage AI retries are now visible to operators** (nexus-vatx Gap 1). Every `voyageai.Client(...)` construction in the tree now passes `max_retries=0` instead of the SDK's tenacity-based `max_retries=3`, and `_voyage_with_retry` is the sole retry authority. The retry predicate is extended from `APIConnectionError | TryAgain` to also cover `RateLimitError`, `ServiceUnavailableError`, `ServerError`, and `Timeout`. Each retry decision emits a WARN-level structlog line (`voyage_transient_error_retry` with `attempt`, `delay`, `error_type`, `error`) — previously voyageai's internal tenacity swallowed rate-limit backoffs, producing the 89–95 s per-file stalls with no log explanation that surfaced during the 2026-04-17 Delos re-index. Touches `db/t3.py`, `doc_indexer.py`, `indexer.py`, `scoring.py`. Six new tests in `tests/test_voyage_retry.py` pin the extended predicate, WARN-line contents, and per-error-class retry behaviour.

### Fixed

- **Review-remediation: Important + Suggestion findings.** Sweep of the remaining post-v4.7.0 review findings after the three Criticals landed. Grouped by reviewer scope:
  - **Indexing observability:** `_run_index` post-processing block is now wrapped in `try/finally` so the `[post] Post-processing complete …` marker fires even when a prune / catalog hook raises (Reviewer A/I-1); the marker includes `(interrupted: <ExcType>)` when abnormal. `nx index repo` emits the retry-time summary (`Transient-error backoff: Xs total …`) from a `finally` block so it's visible on exception paths (Reviewer A/I-2). `_ETATicker.start()` now clears the stop event inside the lock and refuses double-start to eliminate a thread-leak hazard on concurrent start/stop (Reviewer A/I-3 + S-4). `_format_eta` renders `done` instead of `~1 min remaining` when every file is complete (Reviewer A/S-1). `test_voyage_retry.py` now resets retry accumulators via an autouse fixture so assertion failures can't leak module state between tests (Reviewer A/I-4). The retry accumulator's record-before-sleep semantic is now documented (Reviewer A/S-2).
  - **Collection management:** `collection_audit.compute_chash_coverage` no longer reaches into `ChashIndex._lock` from outside the class — new public methods `ChashIndex.count_for_collection(name)` and `ChashIndex.doc_ids_present_in_collection(collection, ids)` provide locked alternatives, and the coverage pass uses a single `ChashIndex` open for both the count and missing-sample queries (Reviewer B/I-1, B/S-3, C/I-4). `indexer._catalog_hook` captures `source_mtime` with `stat()` BEFORE reading file bytes for the content hash so a concurrent write produces a stored mtime *older* than the indexed content (safe direction — future staleness checks fire correctly) instead of the reverse (Reviewer B/I-3). A parallel TOCTOU window exists in `doc_indexer._catalog_markdown_hook` and `pipeline_stages._catalog_pdf_hook` that requires threading mtime from the content-read entry point — comment annotations added; full fix deferred to a follow-up bead.
  - **Search / taxonomy / util:** `_CHILD_MARKERS` in `doc/resolvers.py` now uses trailing-boundary suffixes (`-calibration-` and `-calibration.`) so a primary RDR whose title starts with `calibration-` (e.g. `rdr-200-calibration-free-inference.md`) is no longer misclassified as a child artifact (Reviewer C/I-5). Stale `os.killpg(os.getpgid(pid), …)` comments in `session.py` now reference the canonical `safe_killpg` helper (Reviewer C/I-1). `_iter_count_matches` in `doc/ref_scanner.py` gained comments documenting plain-integer-before-k-shorthand iteration order, and `_extract_count_near`'s docstring pins the equidistant tie-break (Reviewer C/I-2 + I-3). `sample_live_distances` and the `--live` histogram probe now log at DEBUG on exception instead of silently returning empty, so a quota-exceeded or timeout path is observable in structured logs (Reviewer B/S-1 + C/S-2). Comment additions for the `merge_candidates` symmetric-pair averaging tradeoff (Reviewer C/S-4) and the `test_silent_zero_end_to_end_real_engine` `CliRunner.mix_stderr` contract (Reviewer C/S-1).

  Test additions: `test_pid_zero_returns_false_without_signalling`, `test_negative_pid_returns_false_without_signalling`, `test_rename_preserves_source_mtime_across_jsonl_rebuild`, `TestRenameCascadeFailureModes` (t2 + catalog), `test_primary_with_calibration_prefix_in_title_is_not_misclassified`, `test_equidistant_counts_tie_break_is_stable`, autouse `_reset_retry_stats_on_entry` fixture.
- **Review-remediation: `safe_killpg` rejects non-positive pids.** The `pid=0` path routed `os.getpgid(0)` to the *caller's* own pgid and then `os.killpg(own_pgid, SIGKILL)` — a truncated or zero-byte mineru pidfile that parsed as 0 would have self-terminated the running `nx` CLI. The helper now guards `pid <= 0` explicitly (in addition to the pre-existing `isinstance(pid, int)` mock guard) and emits a debug log on the skip. Two new regression tests (`test_pid_zero_returns_false_without_signalling`, `test_negative_pid_returns_false_without_signalling`) pin that `os.killpg` is never invoked on a non-positive pid.
- **Review-remediation: `Catalog.rename_collection` preserves `source_mtime` in JSONL.** The rename SELECT was fetching 12 columns (omitting `source_mtime`, column 13) and appending a JSONL record without the field. JSONL is the rebuild source of truth, so `Catalog.rebuild()` silently reset `source_mtime` to `0.0` for every renamed document — breaking stale-source detection (RDR-087 Phase 3.4) until the next re-index restamped the column. Fixed by adding `source_mtime` to the SELECT and the record dict. A new test (`test_rename_preserves_source_mtime_across_jsonl_rebuild`) seeds a document with a known mtime, renames the collection, rebuilds the catalog from JSONL, and asserts the mtime round-trips.
- **Review-remediation: explicit failure-mode tests for the rename cascade.** `nx collection rename` is intentionally fail-open — T3 renames first, then T2 and catalog are attempted independently so a partial failure leaves the system in a divergent but recoverable state with a stderr `warn:` line. That contract was documented in code comments but not pinned by tests. Added `TestRenameCascadeFailureModes` with two cases: T2 raises → exit 0 with `T2 cascade failed` on stderr + T3 still renamed; catalog raises → exit 0 with `catalog cascade failed` on stderr + T3 still renamed.
- **`nx taxonomy validate-refs` proximity false-positives on bullet lists and multi-count paragraphs** (nexus-7ay). Each markdown list item (`-`, `*`, `+`, ordered) is now its own count-binding scope — a count claim in one bullet no longer leaks into every sibling, which previously produced a single OK line followed by a cascade of spurious Drift lines. Within a prose paragraph that names more than one collection, each reference now binds to the textually nearest chunk-count claim instead of always the first one encountered. Seven new tests in `tests/test_ref_scanner.py` pin the expected proximity semantics.

## [4.7.0] - 2026-04-18

### Added

- **RDR-086 Phase 1 — T2 `chash_index` primitive + dual-write + cascade + reconciliation.**
  New `ChashIndex` domain store answers "which `(collection, doc_id)` holds this chunk hash?" in ~50 µs via a SQLite lookup, replacing the ~13-min serial-ChromaDB-filter alternative. Compound PK `(chash, physical_collection)` so the same chunk text can legitimately live in multiple collections (e.g. `knowledge__delos` and `knowledge__delos_docling`). Populated via best-effort dual-write at seven T3 upsert sites — `code_indexer`, `prose_indexer`, three `doc_indexer` paths, `pipeline_stages.uploader_loop`, and `indexer._index_pdf_file`. `nx collection backfill-hash [--all]` reconciles gaps with a tqdm progress bar (TTY-auto-detect via `disable=None`). `nx collection delete` cascade now also purges `chash_index` rows. 5 sub-phase beads: `nexus-l2k`, `nexus-4qm`, `nexus-ppl`, `nexus-r9b`, `nexus-jfi`.
- **RDR-086 Phase 2 — `Catalog.resolve_chash` collection-agnostic resolver** (nexus-9a8). Given a bare hex, `chash:<hex>`, or `chash:<hex>:<start>-<end>` input: look up T2 rows, self-heal stale rows (collection no longer exists in T3 → delete the row on access), tie-break by `prefer_collection` → newest `created_at` → deterministic name sort (newest wins so re-indexing into `_docling` variants supersedes the original), delegate to `resolve_span` for chunk text + metadata. On T2 miss, parallel ChromaDB fallback (10× concurrency matching `MAX_CONCURRENT_READS`, 30 s wall-clock deadline, one warning per process). New `ChunkRef` TypedDict documents the return shape.
- **RDR-086 Phase 3 — `chunk_text_hash` on structured returns** (nexus-d3h). `search(structured=True)` and `query(structured=True)` now include a `chunk_text_hash` list aligned with `ids`, plus a new per-result `chunk_collections` list so consumers that need per-chunk origin (e.g. `nx_answer`'s envelope) get the right collection for every hit, not just the top dedup'd one. `nx_answer(structured=True)` is a new opt-in kwarg returning `{final_text, chunks, plan_id, step_count}` where each chunk carries `{id, chash, collection, distance}`. Single-step guard path produces the same envelope with one `query()` round-trip (previously two).
- **RDR-086 Phase 4 — `nx doc` consumers on `resolve_chash`** (nexus-6iz). `nx doc check-grounding --fail-ungrounded` — exit 1 on any unresolved `chash:` span with file:line error output. `nx doc check-extensions` resolves chash → Chroma-scoped `doc_id` before calling `chunk_grounded_in` (caller-side fix; the taxonomy signature is unchanged), removing the RDR-083 v1 inertness case. `nx doc render --expand-citations` appends a `## Citations` footnote block with chunk text (truncated at 500 chars); unresolvable hashes render as `[unresolved chash: <first 8>…]`.
- **RDR-086 Phase 5 — `nx doc cite` authoring CLI** (nexus-3dk). Compose `search(structured=True)` with Phase 3's `chunk_text_hash` surface, resolve the top hash via `Catalog.resolve_chash` to fetch the excerpt, emit a paste-ready `[excerpt](chash:<hex>)` markdown link or the full `--json` schema `{candidates, query, threshold_met}`. Empty-index short-circuit exits 2 with a "run `nx collection backfill-hash --all`" hint instead of a 30 s fallback timeout. Tied candidates within 0.01 distance are surfaced in JSON; stdout picks first + notes `# N candidates tied (see --json)`.
- **`ChashIndex.delete_stale(chash, collection)`** and **`ChashIndex.is_empty()`** — locked public methods the self-healing read and fresh-install guard use instead of touching `.conn` directly.

### Changed

- **`nexus.config.nexus_config_dir()`** is now the single source of truth for every path under `~/.config/nexus`. Twenty sites that previously hard-coded `Path.home() / ".config" / "nexus" / ...` now route through it. Covers T2 database, catalog JSONL, sessions, checkpoints, pipeline buffer, ripgrep cache, MinerU PID + output root, context cache, git-hook registry, index log, doctor registry lookup. `NEXUS_CONFIG_DIR` was previously documented as an override but silently ignored at the load-bearing T2 path — meaning a "sandbox" run could overwrite the user's production `memory.db`. 22 new isolation tests in `tests/test_config_dir_isolation.py` verify each surface.
- **`search_engine.search_cross_corpus` per-collection `n_results`** is now capped at `QUOTAS.MAX_QUERY_RESULTS` (300). Without this, a large `offset` fed into `fetch_n = offset + limit` multiplied by `mult` (up to 4×) produced per-collection query values that punched through the ChromaDB Cloud cap.
- **`_prune_stale_chunks` and the three `doc_indexer` stale-chunk delete sites** now batch `col.delete(ids=...)` at `MAX_RECORDS_PER_WRITE=300`. A single unbounded delete violated the Cloud quota on re-indexes that dropped >300 chunks.
- **`CatalogDB` SQLite connection** sets `busy_timeout=5000` + `journal_mode=WAL` to match the five T2 domain stores. Without these, cross-process catalog writes during indexing raced CLI reads and raised `OperationalError: database is locked` immediately.
- **`search_by_tag` LIKE pattern** escapes `%` and `_` metacharacters. Bound parameters block SQL injection but not glob matching — a tag like `"rdr_078"` previously matched `"rdrX078"`.
- **`merge_topics`** uses `INSERT ... ON CONFLICT DO UPDATE` to preserve the higher-similarity projection row when source and target both carry assignments for the same `doc_id`. The previous `INSERT OR IGNORE` silently discarded higher-similarity data.
- **`nx_answer(structured=True)` single-step guard** synthesizes the result summary from the structured envelope instead of calling `query()` twice. Halves the T3 round-trips for the single-step path.
- **`reset_singletons()`** now also resets the T1 plan-match cache. Previously, tests that injected a fresh T1 saw stale plan embeddings from prior test state.
- **`plan_match`** evicts stale T1 rows when `library.get_plan` returns `None`. Prevents accumulating ghost embeddings after T2 plan deletes.
- **`frecency.batch_frecency`** uses a unique `|||nxcommit|||` sentinel around timestamps instead of the fragile `"COMMIT "` prefix. A file literally named `"COMMIT something"` could previously corrupt all subsequent scores in its commit.
- **`_flag_contradictions`** caps the O(n²) pairwise check at 30 indices per collection. A knowledge corpus with many near-duplicate chunks used to dominate search-engine latency.
- **Exporter import path** validates every embedding's byte-size matches the first record's (and that the first is a multiple of 4 for float32). Malformed `.nxexp` files now fail fast at the boundary instead of raising a cryptic ChromaDB dim-mismatch error deep in upsert.
- **`nx upgrade --dry-run`** no longer writes. Previously called `bootstrap_version()` which creates base tables and seeds `_nexus_version` — legitimate writes in non-dry-run but wrong for dry-run.
- **`nx upgrade` T3 steps** are now tracked in a `_nexus_t3_steps` table. A failed T3 step is retried on the next upgrade invocation; previously the overall version advanced on T2 success and the failed T3 step never retried.
- **`nx doctor --check-schema`** additionally verifies `memory_fts` and the `idx_chash_index_collection` index. Opens with `journal_mode=WAL` to avoid immediate lock errors during concurrent MCP writes.
- **`console` activity route** uses a bounded `collections.deque` instead of reading the whole JSONL into memory — keeps the async event loop unblocked on large catalogs.
- **`Catalog._ensure_consistent`** caches the JSONL max-mtime and skips the rebuild when nothing has changed. Previously re-parsed the full corpus on every `Catalog()` construction.
- **T1 access tracking** coalesces per-row `col.update` calls into a single batched call, dropping N serial HTTP round-trips per search.
- **`LocalEmbeddingFunction`** guards lazy init with a lock so concurrent callers don't both download the fastembed model.
- **`CatalogTaxonomy.detect_hubs`** replaces N per-hub `SELECT DISTINCT source_collection` queries with one grouped query + a Python dict lookup.
- **CLI exit codes** standardized across `commands/doc.py` (16 sites) and `commands/taxonomy_cmd.py` (4 sites) — `raise click.exceptions.Exit(N)` instead of `sys.exit(N)` so the Click error pipeline fires.
- **`nx mineru` output root** moves from world-writable `/tmp/mineru-output` to `$XDG_RUNTIME_DIR/nexus-mineru` or `$NEXUS_CONFIG_DIR/mineru-output` (mode 0o700). Path is recorded in the PID file and cleaned up on `nx mineru stop` so extracted PDF artifacts don't linger.
- **Console + mineru PID files** use `os.open(..., 0o600)` instead of `path.write_text()` (default umask 0o644). `nx console` also probes existing PID files on startup and refuses to start over a live server.
- **Activity-route `_event_summary`** routes the output `kind` through a frozenset whitelist as a defensive XSS hardening for the `<tr class="event-row {{ e.kind }}">` template.

### Fixed

- **RDR-086 review #1 (Critical) — `resolve_chash` self-heal bypassed `ChashIndex._lock`.** Self-healing `DELETE` now goes through the new `ChashIndex.delete_stale` method so concurrent `upsert` / `delete_collection` callers can't race the same SQLite connection.
- **RDR-086 review #2 (Critical) — `_fallback_chash_scan` future leak on early exit.** `ThreadPoolExecutor` now uses explicit manual lifecycle with `shutdown(wait=False, cancel_futures=True)` in a `finally` block so the 30-second deadline is a real deadline, not a ceiling bounded by the slowest in-flight probe.
- **Storage review C-1 (Critical) — migration race in `_upgrade_done`.** `apply_pending` now holds `_upgrade_lock` for the entire bootstrap + migrate sequence and only marks the path done on success. The previous shape reserved the slot before running migrations and relied on a try/except discard — leaving a window where a concurrent caller could see the path as done and proceed against a half-initialised schema.
- **Indexing review C1 (Critical) — wrong `killpg` target.** PDF-extractor `killpg(proc.pid, SIGKILL)` replaced with `killpg(os.getpgid(proc.pid), SIGKILL)` + `ProcessLookupError` swallow at all three MinerU subprocess kill sites. PID reuse could SIGKILL an unrelated process group under the old idiom.
- **Indexing review C2 (Critical) — `chunking_done` not in `finally`.** `chunker_loop` now wraps its body in try/finally so `_signal_done()` fires on every exit path including exceptions. Previously the orchestrator's `cancel.set()` was the implicit rescue — fragile and skipped the uploader's mark-completed logic.
- **Indexing review C3 (Critical) — parallel CCE tail-batch 429 swallowed.** Every future is now drained before re-raising, recording the first exception and re-raising it after collection. Previously the executor's `__exit__` discarded pending results when the first future's `.result()` raised.
- **Search review I-6 (Important) — `operators/dispatch.py` missing `killpg` on timeout.** `claude -p` now spawns with `start_new_session=True`; the `asyncio.TimeoutError` branch does `killpg(getpgid(pid), SIGKILL)` so children spawned by the planner are reaped, not orphaned.
- **Indexing review I-1 — `_index_pdf_incremental` resume with shrinking chunk count.** Discards the checkpoint when stored `chunks_upserted > total` (e.g. extractor version change re-chunked to fewer units) instead of silently skipping the loop and leaving stale chunks beyond `total`.
- **Indexing review I-2 — MinerU output-layout drift detected.** Raises a clear `RuntimeError` when subprocess exits 0 but the expected `.md` file is missing (layout change after a MinerU upgrade).
- **Storage review I-1 — 6+ lock-bypass sites in `taxonomy_cmd.py` / `migrations.py` backfill.** Every `db.taxonomy.conn.execute(...)` is now wrapped with `with db.taxonomy._lock:` to match the domain-store contract.
- **Storage review I-4 — `T2Database.delete` lock ordering contract documented.** Memory → taxonomy is the required order; the docstring now guards future edits from introducing a reverse-ordered caller that could deadlock.
- **`nx_answer` plan-miss error surfaces the dropped tool names** (e.g. "planner returned only non-dispatchable tools: Bash, grep") instead of a generic "planner failed" string.
- **Pre-existing local-mode RDR crash.** `LocalEmbeddingFunction.__call__` is 1-arg (`texts -> embeddings`) but `doc_indexer.EmbedFn` is 2-arg (`(texts, model) -> (embeddings, model)`). A `_local_embed_fn_tuple` adapter is now wired specifically to the `_discover_and_index_rdrs` call in local mode; code / prose / PDF paths continue to use the raw `LocalEmbeddingFunction` instance directly.
- **CI hang on `test_timeout_kills_process_and_raises`.** Both `operators/dispatch.py::claude_dispatch` and `pdf_extractor.py::_killpg_safe` now guard `killpg(getpgid(proc.pid), …)` behind `isinstance(proc.pid, int)`. `MagicMock` implements `__index__` returning 1, so unit tests that mock the subprocess previously signaled pgid=1 (init / launchd). On macOS this was benign (EPERM caught), but on GitHub ubuntu-latest containers the in-kernel signal delivery would stall deterministically — hanging the matrix pytest step for the full workflow run. Real `subprocess.Popen` / `asyncio.create_subprocess_exec` always yield an int pid; mock fixtures fall through to `proc.kill()` cleanly.

### Docs

- RDR-086 design document lives at `docs/rdr/rdr-086-chash-span-surface.md`.

## [4.6.5] - 2026-04-18

### Fixed

- **nexus-7ne1 — PDF extractor: MinerU-failed fallback returns `fast_result` without replaying `on_page` (silent 0-chunk pathology)** — when the auto-routing PDF extractor decides to use MinerU (`formula_count >= 5`) and MinerU then fails for any reason, the fallback returns the Docling probe pass's `fast_result` — but never replayed the `on_page` callbacks. The streaming pipeline (which only sees pages via `on_page`) got nothing → chunker emitted **0 chunks** for the entire document. The probe pass at `_extract_with_docling(..., enriched=False)` is intentionally invoked without `on_page` so callbacks aren't double-fired if MinerU takes over; the `formula_count < 5` happy path replays callbacks from `fast_result.metadata["page_boundaries"]`, but the `except` branch did not. **Fix**: mirror the replay logic into the `except` branch — every page from `fast_result.text` is re-emitted via `on_page` using stored `page_boundaries`. This bug masqueraded as MinerU brokenness during the 2026-04-17 Delos re-index (13/16 papers reported "0 chunks"); once MinerU succeeded after 4.6.4's `killpg` fix, the latent issue would still re-emerge for any future MinerU failure (transient network error, formula-density OOM, rate limit, etc.). Two regression tests in `tests/test_pdf_extractor.py::TestAutoDetectRouting` cover (1) the multi-page replay path and (2) the `on_page=None` no-callback contract.

## [4.6.4] - 2026-04-18

### Fixed

- **nexus-ze2a (P0) + nexus-dc57 (P1) — POSIX semaphore-leak root cause** — cross-corpus `_multiprocessing.SemLock()` was failing with `[Errno 28] No space left on device` whenever MinerU or orphaned T1 chroma children had accumulated. Both bugs shared a single root: we used `os.kill(pid, SIGTERM/SIGKILL)` on the long-running subprocess head, which did not propagate to its multiprocessing workers or their `resource_tracker`. Workers got orphaned and their POSIX named semaphores were never `sem_unlink()`-ed, eventually exhausting the kernel namespace (`kern.posix.sem.max = 10000`). **Fix**: (1) T1 chroma spawn in `session.py` now uses `start_new_session=True` so chroma plus its workers share one killable process group; (2) `stop_t1_server` uses `os.killpg(os.getpgid(pid), SIGTERM)` → `os.killpg(..., SIGKILL)` so the whole subtree receives the signal; (3) `nx mineru stop` uses the same `killpg` pattern so MinerU workers' `resource_tracker` runs before the group exits. No periodic-restart band-aid — the process-group contract itself was broken. New `nx doctor --check-resources` probes the POSIX semaphore namespace and exits 2 with actionable guidance on `[Errno 28]` pressure (pointing at both sources by name).

## [4.6.3] - 2026-04-17

### Fixed

- **Issue #190 follow-up** — `nx search` (and any cross-corpus query) no longer crashes when a T3 collection's stored embeddings don't match the current embedding model's output dimension. `T3Database.search` now catches `chromadb.errors.InvalidArgumentError` where the message contains `"dimension"`, logs a structured `collection_dimension_mismatch_skipped` warning with the collection name, and continues to the next collection. Non-dimension `InvalidArgumentError` subtypes (malformed `where` clause, bad query args, etc.) still propagate. Unblocks users who upgraded their local embedding model and left older collections around at the previous dimension — they can now search across the healthy collections without manually deleting the stale ones first.

## [4.6.2] - 2026-04-17

### Fixed

- **Issue #190** — `nx search` (and any `T2Database` construction) crashed with `sqlite3.OperationalError: no such column: verb` for anyone whose DB was created before 4.4.0 (RDR-078 dimensional-identity columns). `_PLANS_SCHEMA_SQL` in `db/t2/plan_library.py` created four `CREATE INDEX` statements referencing the `verb` / `scope` / `dimensions` columns inline, so `_create_base_tables` crashed before the 4.4.0 `_add_plan_dimensional_identity` migration had a chance to add those columns. Indexes removed from `_PLANS_SCHEMA_SQL`; the migration already creates all four idempotently (`CREATE INDEX IF NOT EXISTS`), so fresh installs still get them via the migration and upgrading installs get both the columns and the indexes added together. Two regression tests seed a pre-4.4.0 plans table and pin `bootstrap_version` + `apply_pending` behaviour.

## [4.6.1] - 2026-04-17

### Fixed

- **RDR-087 review follow-up** (nexus-yi4b.2.5) — two post-merge nits from the Phase 2 code review:
  - **Typed telemetry accessor on hot path** — `search_engine.search_cross_corpus` and `commands/search_cmd._emit_silent_zero_note` now read the `telemetry.search_enabled` / `telemetry.stderr_silent_zero` opt-outs via `config.get_telemetry_config(cfg=cfg)` instead of raw `cfg.get("telemetry", {}).get(...)`. Malformed `.nexus.yml` values (e.g. `search_enabled: "yes"`) now surface the structured `telemetry_config_malformed` warning on every call, matching the design intent of Phase 2.3's typed accessor. `get_telemetry_config` accepts an optional pre-loaded `cfg` kwarg so the hot path skips the disk re-read.
  - **Schema column rename** (migration 4.6.1) — `search_telemetry.dropped_count` → `kept_count` with value flip (`kept = raw − dropped`). RDR-087 §Proposed Solution specifies `kept_count`; 4.6.0 shipped with `dropped_count`. Idempotent ALTER + UPDATE migration; no-op on already-renamed or missing tables. Fresh installs get `kept_count` directly via `_TELEMETRY_SCHEMA_SQL`. Phase 3 consumers can now rely on spec-aligned column semantics.

## [4.6.0] - 2026-04-17

### Added

- **RDR-087 Phase 2: Telemetry Persistence** (nexus-yi4b.2) — four stacked beads that turn the Phase 1 silent-zero stderr diagnostic into queryable T2 state:
  - **2.1** — `search_telemetry` T2 table + registered migration; `nx doctor --check-schema` recognises it.
  - **2.2** — hot-path `INSERT OR IGNORE` from `search_cross_corpus` writing one row per (query, collection). Failure is swallowed at DEBUG so a telemetry fault never breaks search. Composite PK `(ts, query_hash, collection)` dedupes same-second writers.
  - **2.3** — `telemetry.search_enabled` / `telemetry.stderr_silent_zero` opt-out section in `.nexus.yml`; `TelemetryConfig` dataclass + `get_telemetry_config()` accessor with malformed-value structured warning.
  - **2.4** — `nx doctor --trim-telemetry [--days N]` (default 30d, `click.IntRange(min=1)`); safe on empty tables, missing-DB handled gracefully.

## [4.5.3] - 2026-04-17

### Fixed

- **MCP analytical-tool timeouts** — `claude_dispatch` default raised from 60s → 300s; per-tool defaults raised from 120s → 300s (`operator_extract`, `operator_rank`, `operator_compare`, `operator_summarize`, `operator_generate`, `nx_enrich_beads`, `_nx_answer_plan_miss`) and 120s → 600s (`nx_tidy`, `nx_plan_audit`). The prior 120s ceiling was producing false timeouts on real analytical workloads (observed: `nx_plan_audit` on the RDR-086 accept chain exhausted 120s mid-run). Timeouts now tier by workload: content transforms at 300s, whole-corpus sweeps at 600s. Callers pass an explicit timeout override when their input is known-short. Nine regression tests pin the new defaults via `inspect.signature`.
- **T3 bare-constructor credential fallback** — `chromadb.CloudClient()` invoked without explicit args now falls back through `get_credential()` before failing. Previously scripts that didn't know about the `make_t3()` factory surfaced `ChromaError: Permission denied`.

### Changed

- **RDR-086 scope expanded** — moved from draft to accepted. Original scope ("ship `resolve_chash` primitive + consumers") expanded after ART-instance feedback revealed authors have no CLI/MCP surface for obtaining chash values. New scope owns the primitive end-to-end: authoring (`nx doc cite`), resolution (T2 `chash_index` + global `Catalog.resolve_chash`), and verification (grounded citation coverage). 6 Gaps, 11 research findings, 5 implementation phases; compound PK `(chash, physical_collection)` for collision tolerance; empty-index short-circuit for fresh installs.

## [4.5.2] - 2026-04-17

### Fixed

- **nexus-51j** — `{{rdr:<id>.<field>}}` token resolution now works for projects using the uppercase `RDR-NNN-*.md` filename convention (common practice — makes RDR files visually distinct from other `docs/` content). The shipped `RdrResolver` (RDR-082) used `Path.glob("rdr-{key}-*.md")` which is case-sensitive on Linux/macOS filesystems and silently missed uppercase files. `_fetch` now iterates `*.md` files and matches case-insensitively via `re.IGNORECASE`. Zero-padding handling preserved; mixed-case cohabitation works (same directory with both `rdr-072-*.md` and `RDR-073-*.md` resolves both). Five regression tests in `tests/test_rdr_082_doc_tokens.py::TestRdrResolver` pin the behaviour. Observed on ART (70+ uppercase RDR files) during 2026-04-16 token pilot.

## [4.5.1] - 2026-04-17

### Fixed

- **nexus-lub follow-up** — `nx collection delete` cascade now runs even when the Chroma collection is already absent (discovered during the v4.5.0 live shakeout). The v4.5.0 cascade fix assumed the T3 delete succeeds before the cascade runs; when a user invokes the command to clean up orphan taxonomy state left by a pre-4.5.0 deletion (the recovery case the fix was supposed to unblock), `chromadb.errors.NotFoundError` bubbled out before the cascade, leaving orphans intact. v4.5.1 wraps the T3 delete in `try/except NotFoundError`, prints an informational `note:` to stderr, and proceeds with the taxonomy cleanup. One new regression test exercises the absent-collection path end-to-end via the Click runner.

## [4.5.0] - 2026-04-17

### Added

- **RDR-082 Doc-Build Token Resolution** — new `nx doc render` / `nx doc validate` commands that expand `{{bd:<id>[.field]}}` and `{{rdr:<id>[.field]}}` tokens against authoritative state (bead DB, RDR frontmatter) at build time. Resolver registry is the extension point for future namespaces. Emits `<stem>.rendered.md` sibling; fail-loud on unresolved tokens. Shared `src/nexus/doc/_common.py` fence helpers now used by both 082's tokenizer and RDR-081's `ref_scanner`.
- **RDR-083 Corpus-Evidence Tokens** — `{{nx-anchor:<collection>[|top=N]}}` token plus `nx doc check-grounding` (citation-coverage report) and `nx doc check-extensions` (projection-based author-extension flagger) subcommands. `AnchorResolver` is the first external consumer of RDR-082's registry — plugs in without parser/engine/CLI changes. v1 ships with a documented scope reduction: hash-to-chunk resolution (`resolve_chash`) deferred; `check-extensions` marked `[experimental]` with loud stderr WARNING when its inertness case fires. Deferrals owned by draft RDR-086.
- **RDR-084 Plan Library Growth** — successful ad-hoc plans produced by `nx_answer`'s plan-miss path are now auto-persisted via `save_plan(scope="personal", tags="ad-hoc,grown")`. Paraphrased questions match the grown plan through the plan_match gate instead of re-running the inline planner. New config key `plans.ad_hoc_ttl` (default 30d; set 0 to disable). T1 cosine cache receives `upsert(row)` so matches work without SessionStart re-populate.
- **RDR-085 Glossary-Aware Topic Labeler** — migrates `_generate_labels_batch` off its bespoke subprocess shell-out onto the shipped `claude_dispatch` substrate with schema-enforced output. Project vocabulary from `.nexus.yml#taxonomy.glossary` or `docs/glossary.md` prepended to the labeler prompt eliminates training-prior hallucinations (observed SSMF → "Single Mode Fiber" on ART corpus; live smoke on `rdr__nexus-571b8edd` shows 2/6 topics improved, 0/6 regressed). Supersedes the labeler portion of RDR-081.
- **RDR-086 Chash Span Resolution** (draft) — owner-RDR for RDR-083's deferred work. Proposes `catalog.resolve_chash(chash)` backed by a T2 `chash_index` table populated at indexing time. Unblocks `check-grounding --fail-ungrounded`, `check-extensions` meaningful candidates, and `nx doc render --expand-citations`.

### Fixed

- **nexus-lub** — `nx collection delete` now cascade-purges taxonomy state (`topics`, `topic_assignments`, `topic_links`, `taxonomy_meta`). Prior behaviour left orphan rows so `nx taxonomy status` and hub detection dragged ghost rows across deletions. New `CatalogTaxonomy.purge_collection(name)` method is transactional; delete command reports cleaned-row counts.
- **nexus-9ji** — `nx index pdf --force` now breaks the partial-ingest deadlock. `pipeline_index_pdf` gains `force: bool = False`; when True, `db.delete_pipeline_data(content_hash)` wipes stale pipeline.db state AND `col.delete(where={"content_hash": <hash>})` wipes T3 orphan chunks before `create_pipeline` runs. Both cleanups are pre-flight, so the normal "already running" skip still protects concurrent peers.

## [4.4.1] - 2026-04-16

### Fixed

- **Plugin auto-approval allow list** — added the 11 MCP tools shipped in 4.4.0 that were missing from `nx/hooks/scripts/auto-approve-nx-mcp.sh`: `nx_answer`, `nx_tidy`, `nx_enrich_beads`, `nx_plan_audit`, `traverse`, `store_get_many`, and the 5 operators (`operator_summarize`, `operator_extract`, `operator_rank`, `operator_compare`, `operator_generate`). Without this, every call to any of these tools surfaced a permission prompt instead of running silently. Plugin-only fix — no Python code changed.
- **Subagent-start operators block** — the analytical-operators guidance in `nx/hooks/scripts/subagent-start.sh` still told subagents to dispatch the removed `analytical-operator` agent. Replaced with direct invocations of the 5 `operator_*` MCP tools plus a pointer to `nx_answer` for plan-matched retrieval.
- **`nexus` skill reference** — `SKILL.md` common-operations block and `reference.md` tool catalog were frozen at the 15-tool v4.3.x surface. Added full entries for `nx_answer`, `traverse`, `store_get_many`, the 5 operators, and the 3 hygiene tools (`nx_tidy` / `nx_enrich_beads` / `nx_plan_audit`); corrected core-tool count to 26.
- **Stale "15 agents" reference** in `nx/agents/_shared/README.md` updated to "13 agents (10 active + 3 RDR-080 MCP-tool redirect stubs)".
- **`catalog.py` docstring** — removed lingering `query-planner` reference from `link_query`.

## [4.4.0] - 2026-04-16

### Added

- **RDR-078 Plan-Centric Retrieval** — semantic plan matching (T1 cosine + FTS5 fallback), typed-graph traversal as a first-class plan operator, scenario-plan library, dimensional plan identity. New MCP tools: `plan_match` (internal), `plan_run` (internal), `traverse` (catalog graph walk, depth cap 3), `store_get_many` (batch hydration past the ChromaDB 300-record quota). Migration `4.4.0` adds `plans.verb` / `scope` / `dimensions` / `default_bindings` / `parent_dims` columns + lifetime metrics. 9 builtin YAML scenario templates under `nx/plans/builtin/`. `PlanLibrary.get_plan_by_dimensions()` + `increment_match_metrics()` / `increment_run_started()` / `increment_run_outcome()`. `.github/workflows/plan-schema-check.yml` validates plan YAML on PR.
- **RDR-080 Retrieval Layer Consolidation** — single `nx_answer` MCP tool replaces the `query-planner` + `analytical-operator` agent pair and the inline three-path dispatcher. Trunk: `plan_match` → classify → `plan_run` → record (`nx_answer_runs` table). Plan-miss falls through to an inline `claude -p` planner. `nx_answer` accepts `dimensions={"verb": …}` so verb skills narrow the match to templates of the appropriate verb. Three stub agents (`knowledge-tidier`, `plan-auditor`, `plan-enricher`) shrink to 40-line redirects pointing at `nx_tidy`, `nx_plan_audit`, `nx_enrich_beads`. `pdf-chromadb-processor` agent removed (use `nx index pdf` or `/pdf-process`).
- **RDR-081 Stale-Reference Validator** — `nx taxonomy validate-refs <path>...` scans markdown for `<prefix>__<name>` collection references (default prefixes `docs`, `code`, `knowledge`, `rdr`) and proximate chunk-count claims ("12,900 chunks", "~13k chunks"), compares against current T3 state (`collection_list()` + `count()`), and reports `OK` / `Drift` / `Missing` per reference. Deterministic — pure regex + SQL, no LLM. Respects fenced code blocks (``` ``` ``` and `~~~`) so tutorial snippets don't false-positive. Config-driven whitelist via `.nexus.yml#taxonomy.collection_prefixes`. Exit-code contract: `0` = all OK, `1` = drift (or Missing with `--strict`), `2` = scanner/T3 failure. New module `src/nexus/doc/ref_scanner.py`.
- **5 operator MCP tools** — `operator_extract`, `operator_rank`, `operator_compare`, `operator_summarize`, `operator_generate` (each spawns `claude -p --output-format json --json-schema …`). Default timeout raised to 120s (from 60s) to fit real-corpora workloads.
- **`structured=True` mode** on `search()` and `query()` MCP tools — returns `{ids, tumblers, distances, collections}` dict instead of the human-readable string. Used by the plan runner so `$stepN.ids` / `$stepN.collections` references in plan YAML resolve to real data.
- **`corpus="all"` now means every live prefix** — computed from `get_collection_names()` instead of the hardcoded `"knowledge,code,docs,rdr"`. Projects with only `rdr__*` or custom prefixes no longer miss.
- **Inline-planner prompt is schema-aware** — `_PLANNER_TOOL_REFERENCE` in `nx_answer`'s plan-miss path now carries each tool's signature + output contract + two canonical chain patterns (search → store_get_many → operator; operator auto-hydration). Eliminates the silent `planner_step_dropped` / `missing required argument` failure modes.
- **Operator auto-hydration matches per-tool arg shapes** — the plan runner's `_default_dispatcher` now produces `content` for summarize, `context` for generate, `items` for rank/compare, `inputs` for extract. Previously produced `inputs` for all, which blew up summarize/generate with `missing required argument 'content'`.
- **`T3Database._embedding_fn` honors local mode** — returns `LocalEmbeddingFunction()` (ONNX MiniLM, 384-dim) when `local_mode=True` instead of always VoyageAI. Fixes `"Collection expecting embedding with dimension of 1024, got 384"` on local-mode sandboxes.
- **`nx index rdr` honors `NX_LOCAL`** — `batch_index_markdowns` + `_discover_and_index_rdrs` accept `embed_fn`; the CLI wraps `LocalEmbeddingFunction` with the correct `(texts, model) → (embs, model)` shape and forces Python floats (ChromaDB rejects `np.float32`).
- **`derive_title()` restored with initialism preservation** (`indexer_utils.py`). `my_api_v2.md` → `"My API V2"` instead of empty. `_PRESERVE_UPPER` covers 30+ common technical acronyms (`API`, `RDR`, `MCP`, `CLI`, `LLM`, …).
- **FTS5 special-character sanitization expanded** in `memory_store` to cover `'`, `,`, `;`, `?`, `!`, `#`, `@`, `$`, `%`, `&`, `|`, `\`, `<`, `>`, `[`, `]`, `{`, `}`, `=` (previously raised `OperationalError` on queries with apostrophes, URLs, or CLI flags).
- **Live validation harness** under `scripts/validate/` — 9 suites, 320+ runtime cases exercising every MCP tool, CLI command, hook, skill, and agent in an isolated sandbox. Per-case streaming + per-suite roll-up. LLM suites gated on `NX_VALIDATE_WITH_LLM=1`. Includes runtime exercise of all 43 skills + 13 agents via `claude -p` (suite 09) and full RDR lifecycle e2e (suite 08).

### Fixed

- **`claude_dispatch` now unwraps `structured_output`** from `claude -p --output-format json` wrapper (`src/nexus/operators/dispatch.py`). Before: `nx_tidy` / `nx_enrich_beads` / `nx_plan_audit` and all 5 `operator_*` tools silently returned empty strings because they read schema fields at the top level of claude's result wrapper. Surfaced by the harness's semantic assertions.
- **`Catalog.graph_many()` no longer produces dangling edges** when the node cap fires — edges referencing truncated nodes are filtered out of `merged_edges`; new `graph_many_node_limit_mid_seed` debug log.
- **`get_t1_plan_cache` init-failure short-circuit** via `_PLAN_CACHE_UNAVAILABLE` sentinel — prevents lock contention on the degraded-T1 hot path.
- **`store_get_many`** returns N contents for N input ids without silent truncation at the ChromaDB 300-record quota boundary.
- **Tool-name normalization in the inline planner** — LLM emits `mcp__plugin_nx_nexus__operator_extract`; dispatcher expected bare `extract`. `_TOOL_ALIASES` now maps both forms + the common catalog-tool names (`link_query` → `traverse`, etc.) so steps don't silently drop.
- **`T2Database.save_plan()` facade** accepts and forwards dimensional kwargs (`name`, `verb`, `scope`, `dimensions`, `default_bindings`, `parent_dims`). Previously stripped silently.
- **`_seed_plan_templates()` restores `load_all_tiers()` call** — 9 YAML templates under `nx/plans/builtin/` now actually seed into T2 (the call had been dead code).
- **`PlanLibrary.get_plan_by_dimensions()` restored** — `seed_loader.py` referenced it; method was missing. Seed-directory loads fail at the idempotency check without it.
- **`subprocess_git_toplevel()` removed** from `commands/catalog.py`; replaced with `find_repo_root(Path.cwd())` from `indexer_utils` to eliminate duplicate helpers.
- **CI git-identity fixture** in `tests/test_catalog_graph_many.py` — GitHub runners have no `user.email`/`user.name` configured; tests using `Catalog.init()` need env-scoped git identity.

### Changed

- **All 5 verb skills route through `nx_answer`** — `research`, `review`, `analyze`, `debug`, `document` now call `nx_answer(dimensions={"verb": <skill>})` instead of hand-rolling `plan_match` + `plan_run`. Picks up the record step automatically.
- **10 active agents include a "Retrieval preference (RDR-080)" section** — recommends `nx_answer` for multi-source retrieval; keeps direct `search()`/`query()` appropriate for single-step scoped lookups.

### Docs

- New concept pages: `docs/mcp-vs-agents.md` (RDR-080 boundary rule + stub-agent pattern) and `docs/plan-centric-retrieval.md` (`nx_answer` trunk, plan dimensions, scenario templates).
- Updated: `docs/cli-reference.md` (full `nx taxonomy validate-refs` section), `docs/querying-guide.md` (`nx_answer` trunk + verb skills), `docs/mcp-servers.md` (26-tool catalog broken out by category), `docs/configuration.md` (`taxonomy.collection_prefixes` key), `docs/catalog.md` (knowledge-tidier → `nx_tidy` MCP tool), `docs/memory-and-tasks.md`, `docs/rdr-nexus-integration.md`, `docs/rdr-workflow.md`, `nx/README.md`.
- New RDRs filed: RDR-082 (doc render tokens), RDR-083 (chunk-grounded citations), RDR-084 (plan library growth), RDR-085 (glossary-aware labeler) — all draft, tracked in index.

## [4.3.2] - 2026-04-15

### Added

- **`nx collection rewrite-metadata <coll>`** (load-bearing): paginate a collection, normalise each chunk's metadata via the same `_normalize_for_write` that fronts every live write, write back via `T3Database.update_chunks`. Idempotent. `--source-path PATH` filter, `--dry-run`, `--all`. Operationalises the PR #164 schema rationalisation on already-indexed corpora — `nx index --force` is a silent no-op when the pipeline-state DB still has the content_hash on file, so this command was the only path to retroactively rewrite legacy chunks.
- **`nexus.indexer_utils.detect_git_metadata(path)`** helper — walks up via `find_repo_root` and collects `git_project_name` / `git_branch` / `git_commit_hash` / `git_remote_url`. Returns `{}` outside a git repo so callers can `**`-merge unconditionally.

### Fixed

- **Empty `bib_*` placeholders no longer eat metadata budget** (nexus-2my): `normalize()` drops the four `bib_*` slots together when every value is the placeholder (`0` / `""`); a populated set rides through unchanged. Mirrors the `git_meta`-omitted-when-empty pattern from PR #164.
- **`git_meta` is now populated for `nx index pdf` and `nx index md`**: `_pdf_chunks`, `_markdown_chunks`, `pipeline_stages._build_chunk_metadata` accept a `git_meta` kwarg with auto-detect fallback. Pre-fix, single-file ingest paths emitted no `git_*` keys (the augment lived only in the repo-walk path), so `git_meta` was simply absent on directly-indexed PDFs/markdown. Resolved once at the entrypoint for the streaming pipeline so per-chunk overhead is zero.

### Notes

- Pipeline-state staleness (where `--force` is a silent no-op when `pipeline_buffer` still tracks the content_hash) is tracked separately as a follow-up — not blocking this release because `nx collection rewrite-metadata` is the operator-facing answer.

## [4.3.1] - 2026-04-15

### Fixed

- **T3 metadata schema rationalised (nexus-40t)**: fresh `nx index pdf` ingests on ChromaDB Cloud no longer trip the 32-key `NumMetadataKeys` quota. New `src/nexus/metadata_schema.py` defines the 31 canonical top-level keys actually read by `where=` filters, scoring, and display; every T3 `upsert`/`update` now funnels through `normalize()` + `validate()` at the write boundary. The prior insertion-order-dependent silent-trim heuristic — which dropped newly-enriched `bib_*` fields when total key count crossed 32 — is gone. Violations now raise `MetadataSchemaError` with the full key set.
- **Consolidated `git_*` provenance into a single `git_meta` JSON string** (4 slots → 1). Sub-keys: `project`, `branch`, `commit`, `remote`.
- **Confirmed cargo keys dropped**: `bib_semantic_scholar_id`, `pdf_subject`, `pdf_keywords`, `source_date`, `is_image_pdf`, `has_formulas`, `format`, `extraction_method`, `chunk_type`, `filename`, `file_extension`, `programming_language`, `ast_chunked`, `page_count`, `indexed_at`. All were written by the indexing pipeline but read by no call site.
- **New `content_type` field** (`code` / `pdf` / `markdown` / `prose`) injected by `normalize()` as the canonical routing signal; supersedes the overlapping legacy pair `(store_type, category)`, though both remain in the allowed schema for user-facing back-compat.

### Notes

- **No on-disk backfill** — existing records with >31 metadata keys remain readable. Only new writes are constrained. A dedicated `nx collection rewrite-metadata` command will land in a follow-up to rewrite historical ingests under the canonical schema.

## [4.3.0] - 2026-04-14

### Added

- **Projection quality (RDR-077)**: cross-collection projection now records raw cosine similarity, timestamp, and source collection for every projection assignment. Three new nullable columns on `topic_assignments`: `similarity` (REAL), `assigned_at` (TEXT, ISO-8601), `source_collection` (TEXT). Composite index `idx_topic_assignments_source` supports ICF aggregation and Phase 5/6 hub / audit queries. Migration is idempotent and applied by `nx upgrade` under the existing RDR-076 registry.
- **`nx taxonomy project --use-icf`**: Inverse Collection Frequency weighting. Suppresses hub topics (generic labels that span nearly every source corpus) before the threshold filter and top-K ranking. Stored similarity remains raw cosine — ICF is applied only at query time, never persisted (RDR-077 RF-8 invariant).
- **Per-corpus-type default thresholds** on `nx taxonomy project`: omitting `--threshold` now applies `code__*` 0.70, `knowledge__*` 0.50, `docs__*`/`rdr__*` 0.55. Explicit `--threshold` always wins. Exposed as `nexus.corpus.default_projection_threshold`.
- **`CatalogTaxonomy.compute_icf_map()`**: returns `{topic_id: icf}` where `icf = log2(N_effective / DF)`. Guards: `N_effective < 2` returns `{}`; `DF = N_effective` yields `0.0` (intentional hub suppression); legacy NULL-`source_collection` rows excluded from both numerator and denominator. Per-instance cache via `use_cache=True` + `clear_icf_cache()`. `log2` registered as a deterministic, null-safe SQLite scalar in `CatalogTaxonomy.__init__`.
- **`AssignResult` NamedTuple**: `assign_single()` now returns `AssignResult(topic_id, similarity)` instead of a bare `int`. Callers that only need the topic id use `.topic_id`. Distance → similarity inversion (`1.0 - distance`) happens inside the method.
- **Prefer-higher UPSERT for projection rows**: `assign_topic(assigned_by='projection', ...)` uses `INSERT … ON CONFLICT DO UPDATE SET similarity = MAX(COALESCE(-1.0), excluded)` so re-projection with a lower similarity never overwrites a higher one, and `assigned_at` / `source_collection` refresh only when the incoming match wins. HDBSCAN / centroid / manual rows keep `INSERT OR IGNORE`.
- **`docs/taxonomy-projection-tuning.md`**: operator guide — similarity semantics, ICF rationale, per-corpus-type defaults, calibration loop for new corpora, upsert semantics, staleness detection, troubleshooting.
- **`nx taxonomy hubs`**: generic-pattern hub detector. Flags topics whose projection assignments span `--min-collections N` or more source corpora with ICF `<= --max-icf` and/or labels containing bundled stopword tokens (`assert`, `junit`, `builder`, `class`, `import`, `exception`, `getter`, `setter`, `variable`, `declaration`, `operator`). Output sorted by `chunks × (1 - ICF)` descending. `--warn-stale` compares `MAX(taxonomy_meta.last_discover_at)` across contributing source collections against the hub's latest `assigned_at`; `--explain` shows DF / ICF / matched stopword tokens per row. Advisory — users decide.
- **`detect_hubs()`** on `CatalogTaxonomy` returning `list[HubRow]` with per-row staleness fields (`max_last_discover_at`, `never_discovered_count`, `is_stale`). Never-discovered source collections count as stale (O-3). `DEFAULT_HUB_STOPWORDS` constant exposes the bundled token list.
- **`nx taxonomy audit --collection NAME`**: projection-quality report per collection. Output: total projection assignments originating from the collection, p10 / p50 / p90 of raw cosine similarity (Python-side nearest-rank — SQLite has no `percentile_cont`), count below threshold (re-projection candidates), top receiving topics with ICF, pattern-pollution flags. `--threshold` defaults to the per-corpus-type value from `default_projection_threshold`; `--top-n` caps the receiving-topic list (default 5). Empty-projection case returns a clean "no projection data" message, no stack trace.
- **`audit_collection()`** on `CatalogTaxonomy` returning `AuditReport(collection, total_assignments, p10, p50, p90, below_threshold_count, threshold, top_receiving_hubs, pattern_pollution)`. Helper NamedTuple `AuditHub` carries per-row chunk_count, icf, and matched_stopwords.

### Changed

- `nx taxonomy project --threshold` is now optional (was `0.85`). Omitting it triggers the per-corpus-type default cascade.
- `project_against()` accepts optional `icf_map: dict[int, float] | None`. When supplied, adjusted scores (`sim * icf`) drive both the threshold filter and the top-K ranking; raw cosine is still what lands in `chunk_assignments`.
- `assign_batch(cross_collection=True)` now propagates per-row similarity and `source_collection` into `topic_assignments` (previously the distance was discarded — RDR-077 C-1 audit finding).
- `backfill_projection` (T3 upgrade step) unpacks the new 3-tuple `chunk_assignments` and passes `similarity` + `source_collection=src` through to `assign_topic`.

### Documentation

- `docs/taxonomy.md` — new cross-collection projection section, `project` subcommand added to command table, ICF summary, per-corpus threshold table.
- `docs/cli-reference.md` — `--use-icf` example, `project` row updated with per-corpus defaults + tuning-doc link.
- `docs/storage-tiers.md` — `topic_assignments` row now documents all post-RDR-077 columns and upsert semantics.
- `docs/architecture.md` — Projection quality subsection under Taxonomy linking to the tuning guide; `project` subcommand listed in the `nx taxonomy` CLI table.

## [4.2.2] - 2026-04-14

(Note: v4.2.1 was tagged but never published due to a test failure. v4.2.2 supersedes it and includes all 4.2.1 changes plus the ChromaDB Cloud quota audit and observability improvements found during a live shakeout.)

### Added

- **`nx doctor` PyPI version check**: `_check_cli_version` queries https://pypi.org/pypi/conexus/json (3-second timeout) and reports current vs latest. When behind, suggests `uv tool upgrade conexus`. Network failures are silent (offline-tolerant).
- **`nx upgrade --skip-t3` flag**: skip T3 upgrade steps (e.g., heavy cross-collection projection backfill) for fast T2-only migrations.
- **`backfill_projection` per-collection progress**: prints `[i/N] collection: chunks, matches, attempted (elapsed)` to stderr during the T3 backfill, plus a final summary with total time and actual rows stored. Previously the backfill was silent for many minutes on large repos.
- **`CatalogTaxonomy._paginated_get`** + **`_batched_upsert`** helpers: wrap ChromaDB calls with the 300-record per-call cap (`MAX_QUERY_RESULTS` / `MAX_RECORDS_PER_WRITE`).
- **CLAUDE.md "External Service Limits" section**: documents ChromaDB Cloud + Voyage AI quotas with a reference table. Mandatory consult before any new ChromaDB call.

### Fixed

- **`project_against` paginated `coll.get()`**: `_PAGE = 2000` exceeded the ChromaDB Cloud Get quota of 300, causing `nx taxonomy project` to fail on real cloud collections. Now `_PAGE = 300` with paginated source-collection fetch via `_paginated_get`.
- **4 unbounded `coll.get()` calls** in `catalog_taxonomy.py` (`_discover_cross_links`, `project_against` centroid filter, `rebuild_taxonomy` rebuild + cleanup paths) wrapped in `_paginated_get` to avoid OOM and quota errors at scale.
- **3 `centroid_coll.upsert()` sites** wrapped in `_batched_upsert` (defensive against `MAX_RECORDS_PER_WRITE = 300`).
- **`rebuild_taxonomy` cleanup**: paginated GET + batched DELETE so collections with >300 centroids don't fail rebuild.
- **`nx taxonomy links` invisible cross-collection links**: command queried `compute_topic_links` (catalog-derived only) and ignored the `topic_links` table. Cross-collection projection links written by `_discover_cross_links` and `generate_cooccurrence_links` were invisible. Now displays all rows in `topic_links` with `[collection]` prefix on each topic. New `--refresh` flag re-runs catalog-derived computation explicitly.
- **`backfill_projection` misleading count**: reported "X assignments" using the per-call attempt count, but `INSERT OR IGNORE` deduplicates. Now reports "X stored (Y attempted)" using `COUNT(*) FROM topic_assignments WHERE assigned_by = 'projection'`.
- **Plugin/CLI version mismatch UX**: when the nx plugin is upgraded but the conexus CLI is not, the `nx upgrade --auto` SessionStart hook would print a cryptic Click error. Now prints a helpful message: `nx plugin requires conexus >= 4.2.0 — run: uv tool upgrade conexus`.

## [4.2.0] - 2026-04-14

### Added

- **Idempotent upgrade mechanism** (RDR-076): centralised T2 schema migration registry in `src/nexus/db/migrations.py` with version-gated `Migration(introduced, name, fn)` entries. `apply_pending(conn, current_version)` runs migrations between last-seen version (stored in `_nexus_version` table) and current CLI version. Each migration is idempotent via `PRAGMA table_info()` / `sqlite_master` guards.
- **`nx upgrade` CLI command** with `--dry-run`, `--force`, `--auto` flags for applying pending T2 migrations and T3 upgrade steps.
- **Auto-upgrade on SessionStart**: `nx upgrade --auto` runs as the first SessionStart hook — T2 migrations apply silently on every session start.
- **`T3UpgradeStep` typed interface** for ChromaDB operations (backfills, re-indexing) that require a `T3Database` client.
- **`nx doctor --check-schema`** validates T2 database schema and reports pending migrations.
- **MCP version compatibility check**: synchronous check in MCP `main()` that warns on major/minor version divergence between CLI and stored version.
- **Cross-collection topic projection** (RDR-075): `nx taxonomy project SOURCE` command computes cosine similarity between source chunk embeddings and target collection centroids via normalized matrix multiply. Flags: `--against TARGETS`, `--threshold N` (default 0.85), `--top-k N`, `--persist`, `--backfill`.
- **Automatic cross-collection projection** in `taxonomy_assign_hook`: every `store_put` now projects against foreign collection centroids in addition to same-collection assignment. New rows use `assigned_by='projection'`.
- **Cross-collection topic links**: `_discover_cross_links` (centroid-level similarity at discover time) and `generate_cooccurrence_links` (SQL self-join on shared doc co-assignments) populate the `topic_links` table with `link_types=["projection"]` or `["cooccurrence"]`.
- **`list_sibling_collections()`** in `registry.py` auto-detects related collections from the `{prefix}{name}-{hash8}` naming scheme. Used as the default `--against` target for `nx taxonomy project`.
- **T3 projection backfill**: `T3UpgradeStep("4.2.0", "Backfill cross-collection projection", ...)` runs via `nx upgrade` (not `--auto`) to populate cross-collection assignments and links for existing installs.
- **`cross_collection` parameter** on `assign_single` and `assign_batch` — when True, queries only foreign centroids (`$ne collection_name` filter) for cross-collection projection.
- **Incremental taxonomy assignment during indexing** (RDR-070): `taxonomy_assign_batch` wired into `code_indexer`, `prose_indexer`, `pipeline_stages` uploader, and `doc_indexer`. Chunks assigned to nearest topics immediately after upsert.
- **`indexer_utils`** gitignore/repo-root helpers: `find_repo_root()`, `should_ignore()`, `load_ignore_patterns()`, `is_gitignored()`. PDF batch mode now respects `.nexusignore`.

### Fixed

- RDR close gate heading normalization: `_extract_section` now accepts both `## Problem` and `## Problem Statement` heading variants; gap regex broadened from `^#### Gap \d+:` to `^#{3,5} Gap \d+:` (accepts h3–h5).
- `doctor --check-schema` uses `PRAGMA busy_timeout=2000` to prevent `database is locked` during concurrent upgrades.
- `upsert_topic_links` no longer deletes all rows before inserting — preserves projection links from `_discover_cross_links`.
- `_parse_version` normalizes to 3-component tuples (`(3, 7)` → `(3, 7, 0)`) to avoid unexpected ordering.

### Changed

- `assign_batch` batches all embeddings into a single ChromaDB query (was N individual queries).
- `project_against` paginates source collection fetch (2000-chunk pages) to prevent OOM on large collections.
- `generate_cooccurrence_links` uses a SQL self-join on `topic_assignments` instead of loading the full table into Python memory.
- Domain store `_migrate_*_if_needed()` methods now delegate to the centralised migration registry.

### Docs

- Full `nx upgrade` section added to `docs/cli-reference.md`
- `nx taxonomy project` subcommand documented in `docs/cli-reference.md`
- Migration Registry section added to `docs/architecture.md` replacing old ad-hoc migration paragraph
- Source Layout in `CLAUDE.md` updated with `migrations.py`, `upgrade.py`, and updated descriptions
- Release checklist in `docs/contributing.md` now includes `migrations.py` verification

## [4.1.2] - 2026-04-13

### Fixed

- SubagentStart hook emitted literal `$(...)` bash code instead of the L1 knowledge map — the command substitution was inside a single-quoted heredoc (`<<'NXTOOLS'`) which suppresses expansion
- Added 4 integration tests verifying both SessionStart and SubagentStart hooks actually emit cached context

## [4.1.1] - 2026-04-13

### Fixed

- `nx context show` read the global cache file instead of the per-repo cache, showing stale or wrong content
- Corrected cache path in docs: `~/.config/nexus/context/<repo>-<hash>.txt` (was `context_l1_<repo>-<hash>.txt`)

## [4.1.0] - 2026-04-13

### Added

- **Query sanitizer** (RDR-071): `sanitize_query()` strips LLM prompt contamination (system prompts, tool preambles, chain-of-thought artifacts) from search queries before embedding. Wired into MCP `search` and `query` tools automatically — no user action needed. 24 TDD tests.
- **Progressive context loading** (RDR-072): generates a ~200 token topic map from taxonomy and caches it as a flat file. Injected at session start via the SessionStart hook so agents have project context before the first search query.
- `nx context refresh` CLI command to manually regenerate the context cache
- `nx context show` to display the current cached context
- Auto-refresh hooks: context cache regenerated automatically after `nx taxonomy discover` and `nx index repo`
- Per-repo context cache files (`context_l1_<repo>-<hash>.txt`) for multi-project support

### Fixed

- `reset_singletons` no longer clears module-level hook registrations (affected test isolation)

### Docs

- Added `nx context` section to CLI reference
- Added Context module to architecture.md module map
- Noted query sanitizer in architecture.md Search area description

## [4.0.3] - 2026-04-13

### Changed

- Batch topic labeling: 20 topics per claude -p call (amortizes startup overhead), 4 parallel workers. 654 topics labeled in ~3 minutes vs ~70 minutes sequential.


## [4.0.2] - 2026-04-13

### Changed

- **Hybrid clustering**: MiniBatchKMeans for collections over 5K chunks (O(n) vs HDBSCAN's O(n^2)). Reduces clustering time from 12+ minutes to 2.9 seconds on 63K-chunk collections.
- **Parallel labeling**: 4 concurrent `claude -p` workers for topic labeling. Labels incrementally per collection (crash-safe) instead of batch at end.
- **Progress tracking**: per-phase reporting for `nx taxonomy discover` (fetch milestones at 25/50/75%, embedding source, clustering time, labeling progress with worker count)

### Fixed

- Labeling limit raised from 100 to 1000 per collection (was silently truncating collections with 100+ topics)
- Progress output uses `stdout.buffer.flush()` for immediate display in pipes and redirects

## [4.0.1] - 2026-04-13

### Fixed

- `nx taxonomy` command group was not registered in `cli.py` (included in 4.0.0 squash merge but registration line was missing)
- Added `test_cli_registration.py` to prevent this class of bug: verifies all command modules, taxonomy subcommands, MCP tools, and post-store hooks are properly wired

## [4.0.0] - 2026-04-13

### Added

- **Topic taxonomy** (RDR-070): automatic topic discovery across T3 collections using HDBSCAN clustering on native embeddings (Voyage 1024d on cloud, MiniLM 384d on local). Topics are auto-labeled with Claude Haiku when the `claude` CLI is available. Search results are grouped by topic and boosted for relevance.
- `nx taxonomy discover --all` discovers topics for all eligible T3 collections in one command
- `nx taxonomy status` shows topic health: collections, coverage, review state
- `nx taxonomy review` interactive review: accept, rename, merge, delete, skip
- `nx taxonomy label` batch-relabels topics with Claude Haiku
- `nx taxonomy assign/rename/merge/split/links` manual curation commands
- `nx taxonomy rebuild` full re-cluster with merge strategy preserving operator labels
- Topic boost in search: same-topic results get -0.1 distance, linked-topic -0.05
- Topic grouping: `cluster_by="semantic"` groups results by topic label when >50% assigned
- Topic-scoped search: `search(query="...", topic="Label")` pre-filters to a topic cluster
- Incremental assignment: `store_put` auto-assigns new docs to nearest topic via centroid ANN
- `taxonomy.auto_label` config (default: true) controls Claude Haiku auto-labeling
- `taxonomy.local_exclude_collections` config (default: `["code__*"]`) skips code in local mode (MiniLM clusters poorly on code; cloud Voyage handles it well)
- Live smoke test script: `scripts/smoke-test-taxonomy.py`
- 15 E2E integration tests with real ChromaDB and MiniLM (no mocks)
- `docs/taxonomy.md` dedicated user guide

### Changed

- **Breaking**: `nx taxonomy rebuild` now takes `--collection` instead of `--project`. The old `--project` flag still works with a deprecation notice.
- **Breaking**: `cluster_and_persist()` and `rebuild_taxonomy()` in `nexus.taxonomy` now emit `DeprecationWarning` and return 0. Use `db.taxonomy.discover_topics()` or `nx taxonomy discover`.
- `search()` and `query()` MCP tools now pass taxonomy for topic boost and grouping on all searches
- `discover_for_collection` uses native T3 embeddings instead of re-embedding with MiniLM
- PDF metadata filtering: empty values dropped before ChromaDB upsert to stay under 32-key limit, fixing `git_project_name` loss on PDF chunks

### Fixed

- 30+ bugs found across 5 review rounds (substantive critique, deep review, 4x parallel sweep)
- Connection leak in MCP search when using topic filter
- Orphaned centroids after merge/delete/split operations
- Silent data loss on rebuild when HDBSCAN produces all noise
- Topic boost was writing to `hybrid_score` (overwritten by reranker) instead of `distance`
- Self-merge destroying a topic instead of no-op
- Double `get_assignments_for_docs` call per search
- `review_cmd` crash on EOF/Ctrl-C in interactive prompts
- Cloud quota violation: pagination reduced from 5000 to 250 per request
- Concurrency p95 threshold bumped 4.0x to 5.0x for CI noise tolerance

### Docs

- All user-facing docs source-verified by 6 parallel audit agents
- `docs/taxonomy.md` new dedicated taxonomy guide
- `docs/querying-guide.md` topic-aware search section
- `docs/cli-reference.md` all 12 taxonomy subcommands
- `docs/architecture.md` module map updated (10 missing files added)
- `CLAUDE.md` source layout expanded (18+ files added)
- `docs/configuration.md` 4 missing config keys documented
- BERTopic references removed (never used; sklearn HDBSCAN only)

## [3.9.3] - 2026-04-11

### Fixed

- **Agent model defaults restored to original values**: 3.9.2 downgraded
  agents aggressively; clean eval against ART RDR-073 (no T2 injection)
  proved haiku fails on complex architectural critique and sonnet uses
  more tool calls than opus. All defaults restored to v3.9.1 originals
  (4 opus, 10 sonnet, 2 haiku). The Model Selection tables added in 3.9.2
  are retained — they allow dispatchers to downgrade per-task when the
  task is simple enough, while keeping strong defaults.

  Lesson: initial haiku eval was contaminated by SubagentStart T2
  injection priming agents with the answer. T2 context ≠ cold capability.

## [3.9.3] - 2026-04-11

### Fixed

- **Agent model defaults recalibrated after clean evaluation**:

### Fixed

- **Agent model defaults recalibrated after clean evaluation**: 3.9.2
  set 8 agents to haiku default. Clean testing (no T2 context injection)
  against ART RDR-073 showed haiku fails on complex architectural
  critique — answers the wrong question, can't hold a dimensional thread
  through a 903-line RDR. Six analytical agents restored to sonnet
  default; three mechanical agents remain haiku.

  Agents restored to sonnet: substantive-critic, plan-auditor,
  deep-research-synthesizer, code-review-expert, codebase-deep-analyzer,
  query-planner.

  Agents remaining haiku: plan-enricher, test-validator, knowledge-tidier,
  analytical-operator, pdf-chromadb-processor.

  Finding: initial haiku evaluation was contaminated — SubagentStart hook
  injected T2 context about the exact failure mode being tested, making
  haiku appear capable of analysis it couldn't do cold.

## [3.9.2] - 2026-04-11

### Changed

- **Dynamic model selection for all agents**: agent defaults lowered to
  cheapest model that handles the common case (8 agents → haiku, 4 → sonnet
  from opus). Skills include Model Selection tables with escalation triggers.
  The Agent tool's `model` parameter overrides frontmatter at dispatch time
  (documented priority 2 in Claude Code resolution chain). Opus is now an
  explicit escalation, not a default.

  | Before | After (default) | Escalation |
  |--------|-----------------|------------|
  | 4 opus agents | 0 opus defaults | opus via `model` param when needed |
  | 8 sonnet agents | 4 sonnet defaults | sonnet via `model` param when needed |
  | 2 haiku agents | 12 haiku/sonnet defaults | — |

## [3.9.1] - 2026-04-11

### Fixed

- **rdr-audit canonical prompt v1.2**: added mandatory code-verification
  gate for PARTIAL and SCOPE-REDUCED audit verdicts. The audit read RDR
  text (success criteria checkboxes) but not code, producing false-positive
  SCOPE-REDUCED on RDR-056 when all 4 features had shipped. The gate
  requires Grep spot-checks against the source before any non-CLEAN verdict.

### Changed

- RDR-066 composition probe catch demonstration proven via synthetic test
  (10-dim vs 5-dim mismatch correctly attributed).
- RDR-067 CA-1 verified (prompt generalizes beyond ART), CA-2 partially
  verified (calibration drifts on severity grading).
- RDRs 057, 061, 062 closed as implemented; 065 status corrected; 068
  closed as won't-ship.

## [3.9.0] - 2026-04-11

Minor release: ships RDR-067 (Cross-Project RDR Audit Loop) — Phase 2 of
the 4-RDR silent-scope-reduction remediation. Adds the `nx:rdr-audit`
skill which wraps the proven 2026-04-11 audit pattern as a one-command
feedback loop, five management subcommands with a read-only / print-only
safety split, cross-project incident template, scheduling asset
templates for local cron/launchd, and softens six research-class agents
to honor relay-specified storage targets (T1/T2/T3).

### Added (nx plugin)

- **`nx:rdr-audit` skill** (`nx/skills/rdr-audit/SKILL.md`) — wraps the
  canonical audit prompt from RDR-067 Phase 1a (pinned in T2 at
  `nexus_rdr/067-canonical-prompt-v1`, ttl=0 permanent) as a one-command
  feedback loop. Dispatches the `deep-research-synthesizer` agent with
  the substituted prompt, parses the output, and persists findings to
  T2 `rdr_process/audit-<project>-<date>`. Enforces the Phase 1b
  invariant that transcript mining from `~/.claude/projects/*` is
  non-delegatable (main session must pre-gather excerpts before
  dispatch). Current-project derivation via `git remote` → pwd basename
  → user prompt precedence chain. Skill body owns `memory_put`
  persistence (the subagent returns findings; the skill writes T2).
- **Management subcommands** on `nx:rdr-audit`: `list`, `status`,
  `history`, `schedule`, `unschedule`. Enforces a safety split:
  read-only subcommands (`list`/`status`/`history`) must not mutate OS
  or T2 state; print-only subcommands (`schedule`/`unschedule`) must
  not execute `launchctl load`, `launchctl unload`, crontab edits, or
  plist file writes. Platform install/uninstall commands are printed
  for the user to review and run manually — the skill never performs
  privileged OS changes automatically.
- **`nx:rdr-audit` slash command** (`nx/commands/rdr-audit.md`) —
  preamble derives current project, pre-scopes the evidence layer
  (worktree detection, transcript directory detection), and classifies
  subcommands by safety class before routing to the skill body.
- **Cross-project incident template**
  (`nx/resources/rdr_process/INCIDENT-TEMPLATE.md`) — 6 frontmatter
  fields + 8 required narrative sections for cross-project
  silent-scope-reduction incident filings. Sibling projects file into
  T2 `rdr_process/<project>-incident-<slug>` so audit subagents can
  aggregate across projects.
- **Scheduling asset templates** (`scripts/`) — shell wrapper
  (`scripts/cron-rdr-audit.sh`, chmod +x, strict bash mode, log rotation
  at 10MB), macOS launchd plist template
  (`scripts/launchd/com.nexus.rdr-audit.PROJECT.plist`, monthly
  cadence), Linux crontab template (`scripts/cron/rdr-audit.crontab`,
  `0 3 1 */3 *` true 90-day cadence), and platform READMEs with
  explicit "do not run launchctl load automatically" safety notes.

### Changed (nx plugin)

- **Research-class agents honor relay-specified storage targets**. Six
  agents (`deep-research-synthesizer`, `deep-analyst`,
  `codebase-deep-analyzer`, `architect-planner`, `debugger`,
  `strategic-planner`) previously had hardcoded "MUST store to T3 via
  `store_put`" directives and `<HARD-GATE>` blocks that overrode
  dispatching skills' T2 target requests. Softened to "MUST persist …
  unless the dispatching relay specifies an alternative storage target
  in its Input Artifacts, Deliverable, or Operational Notes section".
  The T3 default is preserved for generic `/nx:research`,
  `/nx:deep-analysis`, `/nx:analyze-code`, etc. invocations (so the
  auto-linker and catalog graph behavior is unchanged). Dispatching
  skills like `nx:rdr-audit` can now redirect findings to T2 without
  fighting the agent's trained pattern.

### Docs

- RDR-067 (`docs/rdr/rdr-067-cross-project-rdr-audit-loop.md`) accepted
  2026-04-11, status `accepted`.

## [3.8.5] - 2026-04-11

Patch release: ships RDR-066 (Composition Smoke Probe at Coordinator
Beads) Phase 1 of the 4-RDR silent-scope-reduction remediation. Adds
plan-enricher coordinator detection and the `nx:composition-probe`
skill. Catches 3/4 historical ART audit incidents at the coordinator
boundary (inter-bead composition failures); the 4th (RDR-036 intra-class
HashMap short-circuit) is out of scope and re-attributed to RDR-068
dimensional contracts.

### Added (nx plugin)

- **Plan-enricher coordinator detection** (`nx/agents/plan-enricher.md`)
  — the enricher now inspects `bd show <id> --json .dependencies` in
  its per-bead walk. When the blocking-dependency count is ≥ 2, the
  bead is tagged `metadata.coordinator=true` via `bd update --metadata`,
  and a `/nx:composition-probe <id>` instruction is appended to the
  enriched bead description. Post-write verification asserts the tag
  actually persisted (CA-4 silent-omission mitigation) — on failure the
  enricher surfaces an explicit WARNING to the user rather than
  silently proceeding.
- **`nx:composition-probe` skill** (`nx/skills/composition-probe/SKILL.md`)
  — new skill fired on coordinator beads (or manually via
  `/nx:composition-probe <id>`). Reads the coordinator bead and its
  dependencies, dispatches a general-purpose subagent with a verbatim
  prompt to generate a 30-50 line composition smoke test, runs it via
  the project-native test runner (py/java/ts auto-detected), and
  reports PASS or FAIL with attribution to the specific failing
  dependency bead. Read-only subagent tool budget (Read + Grep + Glob),
  locked on Phase 1a spike that verified `search_cross_corpus` as a
  hard-case target without Serena symbol resolution needed.

### Fixed (nx plugin)

- **Coordinator convention documentation** in plan-enricher agent
  prompt header — clarifies what a coordinator is, how detection works
  (fallback heuristic, not full method-ownership lookup), and the
  over-tagging / under-tagging trade-offs. Inline references to the
  Phase 1b CA-5b retrospective (3/3 on in-scope historical targets).

### Performance

- Composition probe execution latency (Phase 1a empirical): **1.93s**
  for a 5-test probe against `search_engine.search_cross_corpus`
  (real `EphemeralClient` + ONNX MiniLM, no mocks, no API keys).
  Well under the documented 30-120s budget. Generation latency
  ~8 minutes wall-clock for reading source files and authoring the
  probe on a hard case.

### RDR

- RDR-066 Composition Smoke Probe at Coordinator Beads — Phase 1 of
  the 4-RDR silent-scope-reduction remediation cycle (Phase 0 was
  RDR-069, shipped 3.8.3). Catch ceiling revised from 4/4 to 3/4
  after Phase 1b retrospective found RDR-036 FactualTeacher.query
  HashMap short-circuit was an intra-class failure mode outside
  the probe framework's scope (re-attributes to RDR-068 dimensional
  contracts).

## [3.8.4] - 2026-04-11

Patch release: surgical close-time reindex. The `/nx:rdr-close` skill
was unconditionally walking the entire RDR corpus via `nx index rdr`
on every close, even when the diff was wholly inside the frontmatter
(status / closed_date / close_reason flip). This shipped two fixes
that should have landed together with RDR-069 in 3.8.3.

### Added

- **`nx index rdr <file.md>`** — single-file scoping for the RDR
  indexer. The command now accepts either a repo directory (existing
  behaviour — glob all `docs/rdr/*.md`) or a single `.md` file (new
  behaviour — index just that one file). File-mode resolves the repo
  root from the file path via `git rev-parse --show-toplevel`, falling
  back to the conventional `docs/rdr/<file>.md` layout when git is not
  available. Collection naming is computed from the resolved repo
  root, so file-mode and directory-mode write to the same
  `rdr__{basename}-{hash8}` collection. Rejects non-markdown files
  with a clean error; rejects unresolvable files with guidance to pass
  a directory instead.

### Fixed (nx plugin)

- **`rdr-close` skill unconditional corpus reindex** — Step 4.4 of
  the Implemented flow, Step 5 of the Reverted/Abandoned flow, and
  Step 3 of the Superseded flow all previously ran `nx index rdr`
  (no argument, whole-corpus walk) on every close. For
  frontmatter-only edits this is pure waste: chunk text is unchanged
  so embeddings would not shift. For body-level edits affecting only
  one RDR, it is still wasteful to walk every RDR file in the corpus.
  The skill now specifies: (a) skip the reindex entirely when the
  diff is wholly inside the frontmatter block, with a concrete
  `git diff | grep` recipe the user can run to check; (b) when a
  reindex IS warranted, use the single-file form
  `nx index rdr docs/rdr/rdr-NNN-<slug>.md` so the corpus walk is
  avoided. The whole-corpus form is explicitly called out as NOT
  appropriate at close time.

## [3.8.3] - 2026-04-11

Patch release: ships RDR-069 automatic substantive-critic dispatch at
`/nx:rdr-close`. New Step 1.75 (Automatic Critique) runs the
`substantive-critic` agent on every close and gates `close_reason` on
the critic's verdict category. Addresses ART's documented
silent-scope-reduction failure mode with the only intervention that has
empirical catch evidence (2/2 on ART RDR-073 + RDR-075).

### Added (nx plugin)

- **Step 1.75 Automatic Critique** in `nx/skills/rdr-close/SKILL.md` —
  dispatches `/nx:substantive-critique <rdr-id>` via a fixed-shape
  minimal relay (rdr_id + standard input artifacts only — never
  session-generated summaries, which is the exact rationalization-bias
  failure mode RDR-069 addresses). Parses the canonical `## Verdict`
  block and branches on outcome: `justified` passes through; `partial`
  blocks `close_reason: implemented` without override; `not-justified`
  blocks `close_reason: implemented` without override (while
  `close_reason: reverted` and `close_reason: partial` remain available
  without override as honest-failure-acknowledgment paths — only
  `implemented` requires `--force-implemented`); a fallback path
  counts `### Issue:` headers under `## Critical Issues` /
  `## Significant Issues` when the Verdict block is absent. Scenario 4
  surfaces dispatch timeouts and transport failures to the user —
  neither silently blocks nor silently proceeds.
- **Canonical Verdict block** in `nx/agents/substantive-critic.md`
  Output Format — 5 fields (`outcome`, `confidence`, `critical_count`,
  `significant_count`, `summary`) at the `- **outcome**:` line the
  close-flow parser greps. Fallback parse rule documented inline.
- **`--force-implemented "<reason>"`** flag in `nx/commands/rdr-close.md`
  preamble — escape hatch for false-positive critic blocks. Requires a
  non-empty reason (empty reason → `sys.exit(0)` with usage hint).
  Handles single-quoted, double-quoted, and bare-token reason forms.
  Writes a T2 audit entry at `nexus_rdr/<id>-close-override-<YYYY-MM-DD>`
  capturing `critic_verdict` (or "skipped"), `user_reason`,
  `final_close_reason`, `timestamp`, and `rdr_id`.
- **CA-4 override-rate threshold** documented in RDR-069 Day 2
  Operations — `>20%` override rate in any 30-day window degrades
  Phase 2 dispatch to advisory mode (critic runs, findings surface,
  close is not blocked). Measurement surface: the T2 override audit
  entries above.

### Fixed (nx plugin)

- **`--force` regex collision** in `rdr-close` preamble (plan-auditor
  SIG-1/SIG-2). Both occurrences — the `force = bool(...)` detection
  and the `args_clean` `re.sub(...)` stripping step — now use
  `r'--force(?!-)'` negative lookahead instead of `r'--force'`. A
  `\b`-based fix is explicitly rejected: word-boundary fires between
  `e` and `-` and still matches `--force-implemented`. Verified in
  Python REPL; concrete AC test from the Phase 2 bead passes.

### Performance

- **Critic dispatch latency** (CA-3): median ~111s, range 95-217s
  (n=9 runs on real RDRs during the Phase 0 research arc). Clean RDRs
  take longer than broken ones — the critic cannot short-circuit on
  "no Critical" and must exhaustively confirm. Budget 3-4 minutes for
  a clean close; use `--force-implemented "<reason>"` for
  high-confidence closes where the latency is not warranted.

### RDR

- RDR-069 Automatic Substantive-Critic Dispatch at Close — Phase 0 of
  the 4-RDR silent-scope-reduction remediation cycle (shipped first;
  RDR-066/067/068 are the later layers).

## [3.8.2] - 2026-04-11

Patch release: ships RDR-065 close-time funnel hardening for the nx plugin.
No core CLI changes — all surface area lives in the `nx` Claude Code plugin
(commands, hooks, skill text, RDR template scaffold). The new gates defend
the RDR close ritual against silent scope reduction.

### Added (nx plugin)

- **RDR template scaffold (Gap 4)** — `### Enumerated gaps to close`
  subsection with `#### Gap N: <title>` placeholders. Authors of new RDRs
  scaffold the structure required by the close gate out of the box.
- **Two-pass Problem Statement Replay preamble (Gap 1)** — added to
  `nx/commands/rdr-close.md`. Pass 1 enumerates `#### Gap N:` headings from
  the RDR's Problem Statement and exits cleanly when `--pointers` omitted.
  Pass 2 validates per-gap pointers (key coverage + file existence) and sets
  a T1 scratch `rdr-close-active,rdr-NNN` marker on success. Grandfathering
  is ID-based (`rdr_id_int < 65`), never date-based. Hard blocks all use
  `sys.exit(0)`.
- **`### Step 1.5: Problem Statement Replay`** — new section in
  `nx/skills/rdr-close/SKILL.md` documenting the four preamble outcomes
  (validation passed / Pass 1 enumeration / legacy WARN / hard block) and
  the verbatim user-facing framing prompt.
- **Divergence-language guard PostToolUse hook (Gap 2)** — new
  `nx/hooks/scripts/divergence-language-guard.sh` registered for `Write|Edit`
  matching `docs/rdr/post-mortem/`. Bakes in the LOCKED Rev 4 8-pattern
  regex bank with markdown header / table-row pre-filtering. Advisory
  only — never hard-blocks.
- **`bd create` commitment-metadata enforcement (Gap 3)** — extends
  `nx/hooks/scripts/pre_close_verification_hook.sh`. When an RDR close is
  active and a follow-up `bd create` mentions the active RDR, the hook
  requires `reopens_rdr`, `sprint`/`due`, and `drift_condition` markers in
  title+description. Missing markers → hard deny with reason. Audit log at
  `/tmp/nexus-rdr065-bd-create-audit.log`.

### RDR

- RDR-065 Close-Time Funnel Hardening Against Silent Scope Reduction —
  6 of 10 epic beads closed with this release.

## [3.8.1] - 2026-04-10

Patch release: four bug fixes from a live shakeout of v3.8.0. Every
user-visible feature from RDRs 057 / 061 / 062 / 063 was exercised
end-to-end against the shipped CLI, MCP servers, and SQLite store.
Three real bugs and one doc drift were found and fixed. **3466
non-integration tests passing** (+8 new regression pins), **20
integration tests passing**.

### Fixed

- **RDR-057 `overlap_detected` logic bug** — `T1.promote()` used the
  scratch entry's full first-100-char snippet as an FTS5 MATCH query.
  FTS5 MATCH is implicit-AND, so any scratch content containing even
  one token not present in the candidate returned zero matches — and
  by construction a similar-but-not-identical entry always has at
  least one new token. The feature was effectively unreachable for
  its intended use case. Rewrote the overlap detection to use the
  same two-phase pattern as `MemoryStore.find_overlapping_memories`:
  (1) pull the first 3 non-stopword content tokens as the FTS5
  candidate query, (2) confirm with Jaccard similarity ≥ 0.5 on the
  full non-stopword word sets. Threshold is 0.5 (more permissive than
  `find_overlapping_memories`' 0.7) because `promote()` is advisory —
  the row is written either way — while consolidation uses the higher
  bar for destructive merges. Four regression tests in
  `tests/test_scratch.py` pin the v3.8.0 shakeout failure plus edges
  (subset below threshold, too-short content, unrelated content).

- **`nx memory delete` taxonomy cascade** — deleting memory entries
  (via `--title`, `--all`, or `--id`) left dangling
  `topic_assignments` rows pointing to the deleted `doc_id`. Orphan
  topics surfaced in `nx taxonomy list` and `nx taxonomy show` as
  ghost entries referencing nonexistent docs. Added
  `CatalogTaxonomy.purge_assignments_for_doc(project, title)` that
  deletes matching assignments (scoped by collection) and drops any
  topics whose assignment count reaches zero. `T2Database.delete()`
  calls it after a successful memory row delete — cross-domain
  coordination lives in the facade (RDR-063 Phase 2 boundary).
  When the caller used `--id`, the facade resolves `(project,
  title)` via a direct SELECT on `memory.conn` before the delete so
  the cascade can scope correctly, avoiding the access-count side
  effect of `memory.get(id=...)` on a dying row. Four regression
  tests in `tests/test_taxonomy.py` cover the cascade, empty-topic
  cleanup, cross-project scoping, and delete-by-id.

### Docs

- **`nx catalog link --help` missing `formalizes`** — the built-in
  link types list was out of date; `formalizes` (added in RDR-057)
  was missing. The creation path accepts it correctly; only the help
  text was stale. One-line docstring update in
  `src/nexus/commands/catalog.py`.

- **`docs/mcp-servers.md` `nx catalog link-bulk` command name** —
  `docs/mcp-servers.md` listed `nx catalog link-bulk` as a CLI-only
  demoted tool. The actual command is `nx catalog link-bulk-delete`
  (hidden) and it is a bulk *delete* by filter, not a bulk create.
  Updated the demoted-tools table to use the real name and clarify
  the semantics. CHANGELOG entries for 3.7.0 and 3.8.0 use the
  Python function name `catalog_link_bulk` which is accurate —
  those are intentionally left as historical records.

## [3.8.0] - 2026-04-10

Ships **RDR-063 (T2 domain split)** — the Phase 1/Phase 2 refactor that was
drafted and gate-ready in 3.7.0. T2 is now a four-store package with per-store
`sqlite3.Connection` + `threading.Lock`; cross-domain reads no longer block on
unrelated writes. **3458 non-integration tests passing** (+2 for the new
`test_t2_concurrency.py` suite), **20 integration tests passing**, concurrency
acceptance gates all green.

### Added

- **RDR-063: T2 Domain Split** — `src/nexus/db/t2.py` (1,052 LOC monolith,
  four mixed domains) split into `src/nexus/db/t2/` package with four per-
  domain stores behind a composing `T2Database` facade:
  - `MemoryStore` (`db.memory`) — agent memory, FTS5 search, access tracking,
    heat-weighted TTL, consolidation helpers
  - `PlanLibrary` (`db.plans`) — plan templates, plan search, plan TTL
  - `CatalogTaxonomy` (`db.taxonomy`) — topic clustering, topic assignment
  - `Telemetry` (`db.telemetry`) — relevance log, retention-based expiry
  Each store opens its own `sqlite3.Connection` against the shared SQLite file
  in WAL mode with `busy_timeout=5000`. Reads in one domain are never blocked
  by writes in another. Concurrent writes across domains still serialize at
  SQLite's single-writer WAL lock but `busy_timeout` absorbs brief contention.
  Per-domain migration guards prevent double-`ALTER TABLE` under concurrent
  constructors. Phase 3 (physical file split) is explicitly deferred; requires
  its own RDR.

- **New concurrency test suite**: `tests/test_t2_concurrency.py` — 6 tests
  covering cross-domain parallel writes, same-store serialization, single-
  threaded baseline, memory_search under concurrent write load (acceptance
  gate), memory_get under concurrent write load, and memory_search during
  active `cluster_and_persist` runs. All gates stable across 10+ runs.

- `_is_sqlite_busy` helper in `memory_store.py` uses `exc.sqlite_errorcode`
  for precise SQLITE_BUSY detection (Python 3.12+). Extended codes
  (`SQLITE_BUSY_SNAPSHOT`, `SQLITE_BUSY_RECOVERY`, `SQLITE_BUSY_TIMEOUT`) are
  intentionally NOT swallowed — they indicate distinct failure modes.

### Changed

- **Best-effort access tracking** (behavior change): `memory.search(access="track")`
  and `memory.get()` now run the `access_count`/`last_accessed` UPDATE as a
  best-effort side-effect under a temporary `PRAGMA busy_timeout = 0`. Under
  sustained cross-domain write load, roughly 5–10% of updates fail-fast on
  `SQLITE_BUSY` and are logged at warning as `memory.access_tracking.skipped`.
  The returned row content is unaffected; only the counter update may be
  skipped. This trades counter precision for tail latency stability — the
  pre-refactor behavior would block the caller for up to 5 seconds on the
  busy_timeout. RDR-057 heat-weighted TTL remains approximate under load
  (see [Storage Tiers § Heat-Weighted Expiry](docs/storage-tiers.md#t2----memory-bank)).

- `T2Database` facade is now pure composition. `T2Database.conn` and
  `T2Database._lock` were removed. Callers that reached into the facade's
  raw connection must route through a specific domain store
  (`db.memory.conn`, `db.plans.conn`, etc.). All in-repo call sites migrated;
  no external callers should have depended on these (they were implementation
  details, not advertised API).

### Fixed

- README agent/skill/tool counts: 32 → 33 skills, 24 → 25 MCP tools (main
  README), 32 → 33 skills (nx plugin README), 17 → 16 agents and 32 → 33
  skills (getting-started.md), 17 → 16 agents (historical.md).
- `docs/architecture.md` Telemetry row mislabeled "access tracking" — moved
  to Memory row where it belongs; Telemetry reworded as "Relevance log …
  retention-based expiry".
- `nx/README.md` Hooks table rewritten to match `hooks.json` — removed
  non-existent `bd prime` entries, added missing `PostCompact`,
  `StopFailure`, `Stop`, `PreToolUse` (bd-close gate), and
  `PermissionRequest` (auto-approve MCP) hooks.
- `docs/storage-tiers.md` stale "Upcoming (RDR-063 draft)" blurb replaced
  with the shipped architecture description.
- `docs/rdr/README.md` RDR-063 status row updated from Accepted → Closed.
- `catalog_taxonomy.py::get_topic_docs` — added Phase 3 fragility note
  explaining that the cross-table JOIN depends on single-file architecture
  and will require redesign if Phase 3 proceeds.

### Docs

- **New**: `docs/rdr/post-mortem/063-t2-domain-split.md` — full post-mortem
  covering the 3 drifts (module LOC targets, access-tracking behavior change,
  now-addressed carry-forwards), 3 carry-forward items, and 4 process
  takeaways.
- `docs/architecture.md` — new § T2 Domain Stores section with the domain
  store table and Phase 1 → Phase 2 concurrency model comparison.
- `docs/contributing.md` — new § Adding a T2 Domain Feature with recipes for
  extending an existing store and adding a new domain store.
- `docs/storage-tiers.md` — RDR-063 interaction note under Heat-Weighted
  Expiry explaining best-effort access tracking.
- `docs/memory-and-tasks.md` — access tracking paragraph clarified to reflect
  best-effort semantics under load.
- `CLAUDE.md` — source layout updated from `db/t2.py` to `db/t2/` package.

## [3.7.0] - 2026-04-10

Three accepted RDRs ship together in a single release: RDR-057 (progressive
formalization), RDR-061 (literature-grounded search enhancement), and RDR-062
(MCP interface tiering). RDR-063 (T2 domain split) is drafted and gate-ready
for the next release. Six rounds of parallel multi-agent code review, 55+
findings addressed, 3426 unit tests + 20 integration tests passing.

### Added

- **RDR-062: MCP interface tiering (dual-server split)** — Single 30-tool
  `nexus` MCP server split into `nexus` (15 core tools) +
  `nexus-catalog` (10 catalog tools with short names — no `catalog_` prefix).
  New `nx-mcp-catalog` entry point. Six admin tools demoted to CLI-only:
  `store_delete`, `collection_info`, `collection_verify`, `catalog_unlink`,
  `catalog_link_audit`, `catalog_link_bulk`. Backward-compat shim at
  `nexus.mcp_server` re-exports all 30 functions for existing callers.
- **RDR-057: Progressive formalization across memory tiers**
  - T1 access tracking via ChromaDB metadata (`access_count`, `last_accessed`)
  - `PromotionReport` return type from `T1.promote()` with `new` / `overlap_detected` actions
  - T2 heat-weighted TTL: `effective_ttl = base_ttl * (1 + log(access_count + 1))` — highly-accessed entries survive longer
  - JIT contradiction detection in `search_cross_corpus`: flags same-collection result pairs with different `source_agent` provenance and cosine distance < 0.3 as `[CONTRADICTS ANOTHER RESULT]` in search output. Default-on; opt out via `search.contradiction_check: false`
  - New `formalizes` catalog link type for multi-representation equivalence
- **RDR-061: Literature-grounded search enhancement**
  - `memory_consolidate` MCP tool with `find-overlaps`, `merge`, `flag-stale` actions. Merge has `dry_run` + `confirm_destructive` safety gates. Uses SQLite `with self.conn:` context manager for atomic UPDATE+DELETE; raises `KeyError` if `keep_id` is missing (prevents silent data loss on `expire()` race)
  - Retrieval feedback loop (E2): new T2 `relevance_log` table records `(query, chunk_id, action)` triples when agents act on search results. Session-keyed in-process trace cache in `mcp_infra`. Purged by `T2Database.expire(relevance_log_days=90)`
  - Persistent taxonomy CLI (E5): `nx taxonomy list/rebuild/show` — Ward hierarchical clustering over T2 memory entries, capped vocab + stopword filter. CLI-only by design, no MCP tool
  - Memory consolidation helpers: `find_overlapping_memories`, `merge_memories`, `flag_stale_memories`
- **RDR-063: T2 domain split (drafted)** — Architecture RDR proposing a 3-phase
  refactor of `src/nexus/db/t2.py` into domain modules (memory, plans, catalog
  taxonomy, telemetry) with a facade preserving backward compatibility. Gate-ready.
- **Structured log event contracts** — `expire_complete`, `embedding_fetch_failed`,
  `embedding_fetch_shape_mismatch`, `contradiction_check`,
  `clustering_skipped_partial_failure` with field-level regression tests in
  `test_structlog_events.py`

### Changed

- **`T2Database.expire()`** now takes a `relevance_log_days: int = 90`
  parameter and purges the telemetry table alongside memory TTL expiry.
  Emits structured `expire_complete` log with `memory_deleted`,
  `relevance_log_deleted`, and optional `relevance_log_error` fields.
- **`T2Database.search()`** now takes `access: Literal["track", "silent"]`
  parameter (default `"track"`) replacing the former implicit access-count
  bump. `find_overlapping_memories` passes `access="silent"` to prevent
  consolidation scans from contaminating the staleness signal.
- **`search_cross_corpus()`** shares a single embedding fetch between
  contradiction detection and clustering via the new
  `_fetch_embeddings_for_results` helper. Partial per-collection failures
  now return `(embeddings, failed_indices)` so features process successful
  collections rather than being suppressed whole.
- **Migration guard** — T2 schema migrations run once per process per path
  via module-level `_migrated_paths` set, with the lock held across the
  full check-run-add sequence to prevent concurrent construction races.
  Path is canonicalized via `resolve()` to deduplicate symlinked aliases.

### Fixed

- **R4-1 (critical) merge_memories TOCTOU data loss** — `T2.merge_memories`
  now runs UPDATE + DELETE atomically via `with self.conn:` context manager;
  raises `KeyError` and rolls back when `keep_id` has 0 rowcount
  (prevents `delete_ids` from being destroyed when a concurrent `expire()`
  deletes `keep_id` mid-merge)
- **C2/F2 merge data loss guards** — `merge_memories` raises `ValueError`
  when `keep_id` appears in `delete_ids` (previously silently destroyed
  the kept entry)
- **R3-1 fail-per-collection embedding fetch** — One broken collection
  no longer suppresses contradiction flags or clustering for all other
  collections in a cross-corpus search
- **R4-2 clustering partial-failure observability** — Emits
  `clustering_skipped_partial_failure` warning when clustering is skipped
  due to a failed embedding fetch

### Removed

- **`src/nexus/catalog/llm_linker.py`** — 207-line dormant module (RDR-061
  E3 Phase 2b). Complete and tested but never wired to a call site,
  conflicting with RDR-057 RF-11 ("cheap at write, expensive at query").
  Cut with rationale recorded in RDR-061.
- **Monolithic `mcp_server.py`** — Replaced with a backward-compat shim.
  All tool definitions moved to `src/nexus/mcp/core.py` and
  `src/nexus/mcp/catalog.py`.

### Docs

- Comprehensive user-facing documentation audit: README, `docs/architecture.md`,
  `docs/catalog.md`, `docs/cli-reference.md`, `docs/configuration.md`,
  `docs/memory-and-tasks.md`, `docs/querying-guide.md`, `docs/storage-tiers.md`,
  `nx/README.md`, `nx/agents/_shared/CONTEXT_PROTOCOL.md`,
  `nx/skills/nexus/reference.md` updated for the dual-server architecture,
  new tools, heat-weighted TTL, contradiction detection, consolidation
  workflow, taxonomy CLI, and `formalizes` link type
- Plugin audit: skills, agents, commands, hooks, and plugin config all
  verified for stale MCP tool references (zero remaining)

## [3.6.5] - 2026-04-09

### Fixed
- **Stale lock file cleanup** — Background indexers launched by git hooks (`disown`/`&`) that crash before writing their PID left empty 0-byte lock files in `~/.config/nexus/locks/` that accumulated indefinitely. `_clear_stale_lock()` now uses age-based detection for empty files (>5s = stale). Added `_sweep_stale_locks()` to clean the entire locks directory on each index run.
- **Session lock hardening** — `session.lock` now writes PID for stale detection and clears stale locks before acquiring, using the same defensive pattern as the indexer.

## [3.6.4] - 2026-04-09

### Fixed
- **nx plugin.json version not bumped** — `nx/.claude-plugin/plugin.json` was stuck at 3.2.3 since the nx plugin was created, preventing Claude Code from refreshing the nx plugin cache on new releases. All plugin changes since 3.2.3 were invisible to users until manual cache clearing.
- **Release docs updated** — contributing.md now lists `nx/.claude-plugin/plugin.json` as a required release artifact alongside `sn/.claude-plugin/plugin.json`.

## [3.6.3] - 2026-04-09

### Fixed
- **Phantom Serena tool names eradicated** — `rename_symbol` (not a real JetBrains backend tool) replaced with `jet_brains_rename` in session-start.sh, mcp-inject.sh, and serena-code-nav skill.
- **Wrong MCP prefix** — `mcp__plugin_serena_serena__` corrected to `mcp__plugin_sn_serena__` in serena-code-nav skill and registry.yaml.
- **Sequential thinking prefix** — `mcp__sequential-thinking__` corrected to `mcp__plugin_nx_sequential-thinking__` in 7 skills and 1 command.
- **Phantom tools removed** — `restart_language_server`, `get_current_config`, and `activate_project` references removed from serena-code-nav skill (not exposed in `--context claude-code` MCP mode).

### Changed
- **Backend-agnostic Serena discovery** — mcp-inject.sh SubagentStart hook now uses dual-variant ToolSearch (JetBrains + LSP names) so the sn plugin works regardless of Serena backend. Delegates parameter docs to `initial_instructions`.
- **Generic tool names in skills** — debugging, development, and architecture skills use backend-neutral short names in pseudocode instead of hardcoded `jet_brains_*` names.

## [3.6.2] - 2026-04-08

### Fixed
- **CCE oversized chunk handling** — single chunks exceeding Voyage's 32K token context window are now truncated to ~30K tokens and retried, then degraded to zero vector if still too large. No more infinite retry spam.
- **Recursive CCE batch splitting** — batch halves now recurse through `_embed_one_batch` at all depth levels instead of calling the API directly.
- **Classifier: data files skipped** — `.txt`, `.csv`, `.tsv`, `.dat`, `.log` files are now classified as SKIP (not PROSE). Prevents wasting API calls on non-prose data files.
- **Catalog progress output** — `nx index repo` now shows progress lines during the catalog registration, link generation, and housekeeping phases instead of a silent pause.

## [3.6.1] - 2026-04-08

### Fixed
- **Subagent hook catalog context** — catalog link context (linked RDRs for files in task) now fires for all agent types. Was incorrectly skipped for code-nav and review agents.

## [3.6.0] - 2026-04-08

### Added
- **Catalog path rationalization (RDR-060)** — catalog `file_path` and T3 `source_path` now store relative paths. `resolve_path()` reconstructs absolute paths via `owner.repo_root` or registry fallback.
- **Link-aware search boost** — `query` MCP tool boosts results from documents with `implements` links. `implements-heuristic` links get zero boost (too noisy). Configurable per-type weights.
- **Discovery tools** — `nx catalog orphans`, `coverage`, `suggest-links` for link graph observability.
- **Incremental link generation** — link generators accept `new_tumblers` for O(new_n × m) incremental mode during `nx index repo`. `nx catalog link-generate` for full batch scans.
- **Agent integration** — `nx catalog links-for-file` and `session-summary` surface linked RDRs for code files. Ambient catalog context in subagent-start hook.
- **Catalog housekeeping** — `_run_housekeeping()` tracks `miss_count`, evicts orphans after 2 missed index runs, detects renames via content hash. `nx catalog gc` CLI.
- **`nx doctor --fix-paths [--dry-run]`** — one-time migration of absolute paths to relative (catalog + T3 metadata).
- **`nx:catalog` skill** — agent-friendly catalog manipulation (resolve, link, context, seed).

### Fixed
- **Test catalog isolation** — autouse `conftest.py` fixture prevents integration tests from polluting the user's live catalog.

## [3.5.2] - 2026-04-08

### Fixed
- **Batched ChromaDB deletes** — `--force` reindex failed with quota error when >300 stale chunks needed pruning. All delete paths now batch in 300-record pages.

### Added
- **`S2_API_KEY` support** — Semantic Scholar enrichment (`nx enrich`) now sends `x-api-key` header when set. Authenticated rate: 100 req/s vs 100/5min unauthenticated (50x speedup). Free key at https://www.semanticscholar.org/product/api#api-key

## [3.5.1] - 2026-04-08

### Fixed
- **Hook permissions** — `stop_failure_hook.py` now executable (was 644).
- **Hook robustness** — removed `set -euo pipefail` from all advisory and permission auto-approve hooks. Prevents silent failures under load.
- **Agent frontmatter** — synced 8 agent colors and 2 versions to match registry.yaml source of truth.

### Docs
- README: corrected agent/skill counts (14→16 agents, 28→32 skills).

## [3.5.0] - 2026-04-08

### Added
- **Quality-score reranking** (RDR-055 E2) — `quality_score()` and `apply_quality_boost()` in `scoring.py`. Log-scaled citation signal + exponential age decay, wired into CLI search after hybrid scoring. Dormant until `nx enrich` populates `bib_citation_count` metadata.
- **Shared where-filter module** (`nexus.filters`) — canonical `parse_where()` / `parse_where_str()` replacing duplicated parsers in MCP server and CLI. Strict mode for CLI validation, lenient for MCP.
- **Shared tumbler resolver** (`nexus.catalog.resolve_tumbler`) — canonical implementation replacing duplicated resolvers in MCP server and CLI catalog commands.
- **MCP infrastructure module** (`nexus.mcp_infra`) — singletons, caching, and test injection extracted from `mcp_server.py` (1752 → 1490 lines).
- **`PDFConfig` dataclass** in `config.py` — replaces 4 individual getter functions with a single structured config loader.

### Fixed
- **MCP cluster output** (RDR-056) — `search()` with `cluster_by="semantic"` now preserves cluster-grouped order and renders `── label ──` headers. Previously re-sorted by distance, destroying cluster grouping.
- **Flaky hook test** — `nx catalog sync` wrote "Catalog synced." to stdout, corrupting JSON output in `stop_verification_hook.sh`. Redirected to `/dev/null`.
- **Flaky integration test** — search by UID without metadata filter returned stale documents from prior runs. Added `--where title=` filter.
- **Advisory hooks hardened** — removed `set -euo pipefail` from `stop_verification_hook.sh` and `pre_close_verification_hook.sh` (advisory hooks must never fail).

### Changed
- **Test suite consolidated** — 44,243 → 30,799 lines (30% reduction). `@pytest.mark.parametrize` for redundant variants, 8 files deleted, 50+ files rewritten. All coverage preserved.
- **Corpus model selection** — `embedding_model_for_collection()` and `index_model_for_collection()` consolidated into single `voyage_model_for_collection()` with backward-compatible aliases.
- **Removed trivial wrappers** — `_entry_to_dict()` / `_link_to_dict()` replaced with direct `.to_dict()` calls (21 call sites).

## [3.4.0] - 2026-04-08

### Changed
- **Retire orchestrator agent** (RDR-058) — deleted `nx/agents/orchestrator.md`, removed from registry and model groups. Routing content preserved in `nx/skills/orchestration/reference.md`. Agent count 15 → 14.
- **Orchestration skill** — converted from agent-delegating to standalone reference skill. Points to routing tables and decision framework in `reference.md`.
- **Shared agent docs** — `CONTEXT_PROTOCOL.md` and `RELAY_TEMPLATE.md` updated: "orchestrator" → "caller" terminology.
- **rdr-accept command** — updated "orchestrator" → "caller" in planning chain prohibition.

### Added
- **Plan library integration** (RDR-058) — `using-nx-skills` Process Flow now checks `plan_search` before multi-agent dispatch and saves successful pipelines via `plan_save`.
- **5 pipeline templates** — RDR Chain, Plan-Audit-Implement, Research-Synthesize, Code Review, and Debug patterns stored as permanent T2 plan library entries.
- **Pipeline Pattern Catalog** — new table in `orchestration/reference.md` documenting all 5 standard pipeline patterns with agents, use cases, and prerequisites.
- **Orchestration standalone skill** — added to `registry.yaml` `standalone_skills` section.

### Docs
- README updated: 14 agents, 10 standalone skills, orchestration directory comment.
- RDR-058 accepted.

## [3.3.1] - 2026-04-07

### Fixed
- **RDR-055 code missing from v3.3.0** — `section_type` metadata (classify_section_type, 9 patterns, all 5 indexing paths) was lost during squash merge of PR #131. Cherry-picked from feature branch. `--where section_type!=references` now works.
- **CI failure on Python 3.13** — HNSW ef tests used `_local_db()` without EF override, causing VoyageAI key error on CI.

## [3.3.0] - 2026-04-07

### Added
- **Per-corpus distance thresholds** (RDR-056) — automatic noise filtering calibrated for Voyage AI embeddings. `code=0.45`, `knowledge/docs/rdr=0.65`, `default=0.55`. Configurable via `.nexus.yml` `search.distance_threshold.*`.
- **Multi-probe collection verification** (RDR-056) — `verify --deep` probes 5 documents (was 1), reports `probe_hit_rate`, new `degraded` status for partial failures.
- **HNSW ef tuning** (RDR-056) — local-mode collections created with `hnsw:search_ef=256`. Retroactive fix via `nx doctor --fix`. Cloud SPANN unaffected.
- **Corpus-specific over-fetch** (RDR-056) — knowledge/docs/rdr fetch 4x candidates before threshold filtering (was uniform 2x). Code stays at 2x.
- **Ward hierarchical clustering** (RDR-056) — new `search_clusterer.py` module. Opt-in via `cluster_by="semantic"` on MCP `search()` tool or `search.cluster_by` config key. Deterministic scipy Ward with numpy k-means fallback.
- **Catalog-scoped pre-filtering** (RDR-056) — high-selectivity metadata predicates (<5% match) route through catalog SQLite as `source_path $in` filter, avoiding HNSW/SPANN stalling.
- **Section-type metadata** (RDR-055) — markdown chunks carry `section_type` (abstract, introduction, methods, results, discussion, conclusion, references, acknowledgements, appendix). Filter with `--where section_type!=references`.
- **`T3Database.get_embeddings()`** — embedding post-fetch for clustering pipeline.
- **`Catalog.doc_count()`** — document count for selectivity calculation.

### Fixed
- **`hnsw:space` latent bug** — cloud SPANN collections don't populate `hnsw:space` metadata; `verify_collection_deep` now returns `cosine` directly in cloud mode instead of reading the absent key.
- **Broken status message** — `collection verify --deep` message updated from singular "probe chunk" to multi-probe semantics.

### Changed
- **`search_cross_corpus()` signature** — gains `cluster_by`, `catalog` parameters (both optional, backward compatible).
- **MCP `search()` tool** — gains `cluster_by` parameter.

### Docs
- Updated cli-reference.md, configuration.md, querying-guide.md with all new features.
- New "Search quality features" section in querying-guide.md.
- Plugin skill reference, session hooks, and subagent hooks updated for `cluster_by` and `section_type`.
- CLAUDE.md source layout updated.
- RDR-056 closed (implemented, Phases 1-3).

## [3.2.5] - 2026-04-07

### Fixed
- **Code search embedding mismatch** (RDR-059) — `code__*` collections were indexed with `voyage-code-3` but queried with `voyage-4`, producing random noise (0.038 distance spread). Query model now matches index model for all collection types. No reindexing required.
- **Flaky test determinism** — `_init_git_repo` in hook integration tests now disables GPG signing, eliminating SSH agent warmup race condition.
- **Stop hook pipefail** — replaced `printf | python3` pipe with `sys.argv` argument passing to avoid `set -eo pipefail` race.

### Changed
- **Embedding model routing** — `_embedding_fn()` in `t3.py` now routes via `embedding_model_for_collection()` instead of hardcoding `voyage-4`. Enforces index/query model match invariant.
- **Default fallback model** — unknown collection prefixes now default to `voyage-code-3` (was `voyage-4`) for both index and query.

### Removed
- **voyage-4 from all active code paths** — eradicated from `corpus.py`, `db/t3.py`, and all user-facing documentation. Only remains in historical changelog/RDR/postmortem references and one deliberate stale-data test fixture.
- **Superpowers plugin references** — removed from E2E test harness (`run.sh`, `00_debug_load.sh`).

### Docs
- **RDR-056**: Search Robustness and Result Clustering (17 research findings, 3 rounds)
- **RDR-057**: Progressive Formalization Across Memory Tiers
- **RDR-058**: Pipeline Orchestration and Plan Reuse
- **RDR-059**: Code Search Embedding Model Mismatch (critical bug, fixed)
- Updated CLAUDE.md, architecture.md, storage-tiers.md, repo-indexing.md, configuration.md — all voyage-4 references corrected.

## [3.2.4] - 2026-04-07

### Fixed
- **Chunk boundary overlap** (RDR-054) — wired dead `overlap_chars` into `SemanticMarkdownChunker._split_large_section`, which previously had zero overlap between sub-chunks. Bumped `PDFChunker` default overlap from 15% to 20% (225 → 300 chars). Fixed header duplication bug when overlap exceeds emitted content length. Guarded Python `[-0:]` edge case.

## [3.2.3] - 2026-04-07

### Fixed
- **Pagination completeness** — all list-returning MCP tools and CLI commands now include pagination footers when results are truncated. Tools fixed: `query`, `scratch` (search/list), `plan_search`, `catalog_search`, `catalog_list`, `catalog_link_query`. CLI commands fixed: `nx catalog list`, `nx catalog search`, `nx catalog links`. Docstrings updated to document pagination behavior.

## [3.2.2] - 2026-04-07

### Fixed
- **Plugin audit compliance** — added `nx/.claude-plugin/plugin.json` manifest; fixed 9 agents using non-standard `color` values (only `red`, `blue`, `green`, `yellow`, `purple`, `orange`, `pink`, `cyan` are valid per Claude Code docs).
- **PermissionRequest hooks** — added `sequential-thinking` to nx MCP auto-approve list (was causing CI failure); explicit matchers in hooks.json reverted to wildcard routing (decision logic stays explicit in shell scripts).

## [3.2.1] - 2026-04-07

### Added
- **File-path extraction linker** — `generate_rdr_filepath_links()` scans RDR content for source file paths and creates `implements` links to matching catalog code entries. `created_by="filepath_extractor"`. Wired into the indexer alongside the existing heuristic linker.

### Fixed
- **MCP auto-approve hooks** — replaced wildcard glob patterns with explicit full tool name lists in both nx (28 tools) and sn (27 tools) PermissionRequest hooks.
- **Agent self-seeding** — 5 analysis/research agents now self-seed T1 scratch with `link-context` when dispatched without a skill, so the auto-linker fires regardless of dispatch path.
- **Mandatory T3 persistence** — added `<HARD-GATE>` and Stop Criteria enforcement for `store_put` in deep-research-synthesizer, deep-analyst, debugger, architect-planner, and codebase-deep-analyzer.

## [3.2.0] - 2026-04-06

### Added
- **Auto-linker** — automatic catalog link creation at storage boundaries. When agents store findings via `store_put`, link-context entries seeded in T1 scratch by dispatching skills are read and catalog links are created automatically via `link_if_absent`. `created_by="auto-linker"` distinguishes mechanical links from agent-created and heuristic links.
- New module `src/nexus/catalog/auto_linker.py` with `auto_link()`, `read_link_contexts()`, and `LinkContext` dataclass.
- `_catalog_auto_link()` helper in MCP server, wired after `_catalog_store_hook` in `store_put`.

## [3.1.2] - 2026-04-06

### Added
- **Sub-chunk character ranges** — `chash:<sha256hex>:<start>-<end>` references a character range within a content-addressed chunk. The hash pins the chunk; the range pins the passage within it. Character offsets are inherently stable because the hash guarantees the content hasn't changed.
- **Custom link types** — the CLI `--type` flag now accepts any string, not just the seven built-in types.

### Docs
- **[Xanadu in Nexus](docs/xanadu-in-nexus.md)** — Xanadu lineage, cross-document linkage problem, and how the link graph enables plan-driven agentic search.
- **[Querying Guide](docs/querying-guide.md)** — `nx search` vs `query()` MCP vs `/nx:query` skill with catalog-aware routing and analytical query examples.
- Expanded catalog guide with tumbler addressing, link type guidance, span lifecycle, admin operations, and troubleshooting.

## [3.1.1] - 2026-04-06

### Added
- **chash: span validation at link creation** — `link()` and `link_if_absent()` now verify that `chash:` spans resolve to actual chunks in the document's physical collection before accepting the link. Raises `ValueError` with collection name if the hash doesn't exist. Skipped when `allow_dangling=True`.

### Fixed
- **`backfill-hash` live progress** — per-batch progress on stderr with carriage-return updates. Previously silent until completion.
- **`backfill-hash` ChromaDB quota handling** — chunks with 32+ metadata keys hit the `NumMetadataKeys` quota on update. Now caught per-batch and counted as skipped instead of crashing the entire run.

## [3.1.0] - 2026-04-06

### Added
- **Tumbler comparison operators** (RDR-053) — `__lt__`, `__le__`, `__gt__`, `__ge__` with -1 sentinel padding for cross-depth ordering. Parent tumblers sort before their children (e.g., `1.1.3 < 1.1.3.0`).
- **`Tumbler.spans_overlap()`** — static method for positional span overlap detection using the comparison operators.
- **Content-addressed spans** (RDR-053) — `chunk_text_hash` (SHA-256 of chunk text) added to ChromaDB metadata in all 5 indexers (code, prose markdown, prose non-markdown, doc PDF/markdown, streaming PDF pipeline). Distinct from file-level `content_hash`.
- **`chash:<sha256hex>` span format** — `_SPAN_PATTERN` and all link creation APIs accept content-hash spans alongside legacy positional formats. Content-hash spans survive re-indexing when chunk boundaries are unchanged.
- **`Catalog.resolve_span()`** — resolves `chash:` spans to chunk content via ChromaDB metadata query.
- **`link_audit()` chash verification** — optional `t3` parameter verifies each `chash:` span resolves to an actual chunk in ChromaDB. MCP `catalog_link_audit` tool now performs chash verification automatically.
- **`nx collection backfill-hash`** — backfill `chunk_text_hash` metadata on existing chunks without re-embedding. Also integrated into `nx catalog setup` and `nx catalog backfill` for automatic backfill during onboarding.
- **Querying guide** — new `docs/querying-guide.md` documenting `nx search` vs `query()` MCP vs `/nx:query` skill, catalog-aware routing, three-path dispatch, and analytical query examples.

### Fixed
- `resolve_span_text()` now handles `chash:` spans (was silently returning None for the preferred format).
- `stale_spans` audit excludes `chash:` spans (they survive re-indexing by design) and checks both `from_span` and `to_span`.
- `stale_chash` entries include `reason` field (`missing`, `document_deleted`, `error`) for actionable diagnostics.
- Plan template seeding uses direct SQL instead of fragile FTS search for idempotency.
- TTL migration detection uses `PRAGMA table_info` instead of DDL substring match.
- Stale-span timestamp comparison uses `datetime()` wrapping for safe ISO-8601 comparison.

### Changed
- CI runs only on PRs to main (not every push to every branch), with `concurrency: cancel-in-progress` to prevent run pile-ups. 10-minute job timeout added.

### Docs
- Expanded `docs/catalog.md` with tumbler addressing, link type guidance, span lifecycle, admin operations, and troubleshooting.
- Updated all 7 link-creating agents with `chash:` span format in tool signatures.
- Updated SubagentStart hook, session_start_hook, and CONTEXT_PROTOCOL with catalog-aware guidance.

## [3.0.0] - 2026-04-05

### Added
- **Document catalog with typed link graph** (RDR-049/050/051) — Xanadu-inspired document registry tracking every indexed document and the relationships between them. Tumblers (permanent hierarchical addresses) identify documents; typed links (`cites`, `supersedes`, `implements-heuristic`, `relates`) capture provenance.
  - `nx catalog setup` — one-command onboarding: init + populate from T3 + generate links
  - `nx catalog search` / `show` / `links` — find documents, browse metadata, traverse the link graph
  - `nx catalog link` / `unlink` — create and remove typed relationships
  - MCP tools: `catalog_search`, `catalog_show`, `catalog_list`, `catalog_register`, `catalog_update`, `catalog_link`, `catalog_links`, `catalog_unlink`, `catalog_link_query`, `catalog_link_audit`, `catalog_link_bulk`, `catalog_resolve`, `catalog_stats`
  - All indexing pathways auto-register in catalog (`index repo`, `index pdf`, `index rdr`, `index md`, MCP `store_put`)
  - Citation links from Semantic Scholar references (via `nx enrich`)
  - Code-RDR links auto-generated by title heuristic at index time
  - Span transclusion — links can reference specific line ranges or chunk positions
  - Permanent addressing — tumbler numbers are never reused, even after delete + compact
  - 12 agents/skills wired for catalog link creation and discovery
- **`defrag()`** — safe JSONL compaction that deduplicates overwrites but preserves tombstones. Auto-runs in `sync()`. Use `compact()` for full tombstone purge.

### Fixed
- **Silent data loss & corruption audit** (nexus-s5mf) — 11 bugs across 7 modules where errors were silently swallowed, causing data loss or corruption:
  - **P0**: CCE empty-result no longer falls through to voyage-4 (would corrupt vector space with mixed embedding models)
  - **P0**: Pipeline post-pass failures logged at WARNING and return bool; pipeline data preserved for retry on failure
  - **P0**: `_prune_stale_chunks` separates query/delete error handling; reports stale chunk count on delete failure
  - **P0**: `delete_pipeline_data` gated on all post-passes succeeding
  - **P0**: `git ls-files` failure in git repos raises RuntimeError instead of silently falling back to rglob (which would index .gitignored secrets)
  - **P1**: Catalog `_ensure_consistent` rebuild failure sets `degraded` flag and logs WARNING
  - **P1**: MCP `_get_catalog()` catches `OSError` only (was bare `except Exception`)
  - **P1**: `reindex_cmd` sourceless check paginates (was limit=100)
  - **P1**: T1 `list_entries` and `clear` paginate with limit=300 to avoid ChromaDB truncation
  - **P2**: `collection info` paginates for accurate MAX(indexed_at) timestamp
  - **P2**: T1 reconnect fallback to EphemeralClient logs WARNING about data loss
- **FTS5 dot/asterisk in queries** — `_sanitize_fts5` now quotes tokens containing `.`, `*`, `+`, `/`. Filenames like "types.py" no longer cause syntax errors in search or title resolution.
- **ChromaDB Cloud quota violations** — catalog setup handles rate limits with per-collection progress and timeouts.
- **RDR backfill pagination** — paginates through all chunks (was limited to first page).
- **Deadlock in sync→defrag** — fixed operation ordering.

### Changed
- **Breaking**: `catalog_links` MCP tool now returns `{"nodes": [...], "edges": [...]}` dict instead of flat edge list. Access edges via `result["edges"]`.
- `nx catalog links` command now handles both graph traversal (positional tumbler) and flat filter queries (`--created-by`, `--type`). The old `link-query` command is removed.
- Admin commands (`link-audit`, `link-bulk-delete`, `backfill`) are hidden from `--help` (still accessible).

### Upgrade notes
After upgrading, run `nx catalog setup` to create and populate the catalog. This is optional — everything works without it — but enables catalog search, link traversal, and agent citation queries. `nx doctor` will remind you.

## [2.12.0] - 2026-04-04

### Added
- **Streaming PDF pipeline** (RDR-048) — three-stage concurrent indexing pipeline (extractor → chunker → uploader) connected via a SQLite WAL buffer (`PipelineDB`). Replaces the sequential extract-then-chunk-then-embed pipeline for all PDFs. Pages stream to buffer as they're extracted; chunker processes the stable prefix while extraction continues; uploader pushes embedded chunks to T3 ChromaDB as they become available.
- **Crash recovery** — every page, chunk, and embedding is durably persisted in SQLite before the next stage processes it. Resume from any crash point (extraction, chunking, embedding, upload) with no re-work beyond the in-flight batch. Extraction metadata stored in `pipeline.db` for instant resume without re-extraction.
- **Incremental chunking** — chunker caches page text in memory, reads only new pages from SQLite (`O(new_pages)` not `O(all_pages)`), and holds back the last chunk until extraction completes (boundary may shift). Eliminates the O(pages^2) re-chunking overhead.
- **`--streaming` CLI flag** — `nx index pdf --streaming auto|always|never`. Default `auto` routes all PDFs through the streaming pipeline. `never` falls back to the batch+checkpoint path (RDR-047).
- **`PipelineCancelled` exception** — `on_page` callback raises on cancel, propagating through MinerU's subprocess batch loop for fast abort instead of silently skipping writes.
- **`on_page` streaming callback** on `PDFExtractor.extract()` — fires per page across all three backends (Docling, MinerU, PyMuPDF). Auto-mode Docling probe runs without callback to avoid double-firing; pages replayed from result if Docling wins.
- **Metadata enrichment post-pass** — after upload, queries T3 and enriches all chunks with source_title, source_author, extraction_method, page_count, is_image_pdf, has_formulas, chunk_count. Resolves key names from ExtractionResult (docling_title → pdf_title → filename).
- **table_regions post-pass** (RF-14) — tags chunks on table pages with `chunk_type=table_page` after extraction completes.
- **Stale chunk pruning** — after upload, deletes chunks from previous versions of the same PDF (uses full `content_hash` from metadata, not ID prefix).
- **`nx doctor --clean-pipelines`** — scans `pipeline.db` for orphaned entries (missing source PDF, stale running pipelines) and deletes them with cascade across all three buffer tables.
- **Incremental PDF upsert with checkpoints** (RDR-047) — batch-path crash recovery for the `--streaming never` path. Checkpoints track embed/upsert progress per batch.
- **Parallel CCE embedding** — `ThreadPoolExecutor(4)` with token-bucket rate limiter for Voyage API calls during embedding.
- **`nx config get` dotted-path traversal** — e.g. `nx config get pdf.extractor`.

### Fixed
- **Concurrent pipeline guard** — `create_pipeline` catches `IntegrityError` on concurrent INSERT (two processes indexing same file).
- **Credential resolution in streaming path** — `embed_fn=None` now resolved from `voyage_api_key` credential in the orchestrator, matching batch path behavior. Fast-fail `RuntimeError` when credentials are absent.
- **Auto-mode double `on_page`** — Docling probe no longer fires `on_page`; pages replayed from `page_boundaries` if Docling wins. Prevents `total_pages` showing double the actual count for formula PDFs.
- **Uploader completion guard** — removed early-exit on provisional `chunks_created` counter during incremental chunking (could cause premature completion). Resume path uses durable state.
- **Resume cursor** — counts all embedded chunks (both uploaded and not-yet-uploaded) to avoid re-embedding work on crash recovery.
- **Embedding heartbeat** — embeds in batches of 32 with `update_progress` between, preventing stale-pipeline detection during long embedding calls.
- **Schema migration** — `_migrate_if_needed` adds `extraction_meta` column to existing `pipeline.db` files.

### Docs
- `docs/rdr/rdr-048-streaming-pdf-pipeline.md` — full architecture spec with 16 research findings
- `docs/cli-reference.md` — `--streaming` flag, `--clean-pipelines`, `--clean-checkpoints`
- `docs/architecture.md` — `pipeline_buffer.py`, `pipeline_stages.py`, `checkpoint.py` in module table
- `CLAUDE.md` — new modules in source layout

## [2.11.2] - 2026-04-03

### Fixed
- **Visible progress during PDF extraction** — Docling and MinerU passes now print status to stderr via tqdm-safe `_progress()` helper that clears/refreshes active tqdm bars. Shows "Docling: extracting paper.pdf (formula detection)…" before the minutes-long enriched pass, "Formulas detected (N) — switching to MinerU", and per-page "MinerU: page N/M" during extraction.

## [2.11.1] - 2026-04-03

### Fixed
- **ChromaDB 32-metadata-key limit** — PDF extractors emit ~37 keys; `_write_batch` now strips empty droppable keys (pdf_creator, pdf_producer, etc.) while preserving load-bearing empty strings (expires_at="" for TTL). Hard truncation guard if still over limit.

## [2.11.0] - 2026-04-03

### Added
- **MinerU server-backed PDF extraction** (RDR-046) — `nx mineru start/stop/status` manages a persistent mineru-api server. HTTP client in pdf_extractor with subprocess fallback. Auto-restart on server OOM (2x budget). Dynamic port allocation.
- **Batch PDF indexing** — `nx index pdf --dir <path>` indexes all PDFs in a directory with progress `[i/N]`, timing, error isolation, and summary. Server-absent advisory.
- **`query` MCP tool** — document-level semantic search. Groups results by source document with full metadata (title, year, authors, citations, page count, extraction method). No LLM required.
- **`store_delete` MCP tool** — delete T3 knowledge entries by document ID
- **`memory_delete` MCP tool** — delete T2 memory entries by project and title
- **`search` `where` filter** — metadata filtering on MCP search and query tools. `KEY=VALUE` or `KEY>=VALUE` format, comma-separated. Numeric fields auto-coerced.
- **`store_list` `docs` mode** — document-level view deduplicating chunks by content_hash. Shows title, chunk count, page count, extraction method.
- **`collection_info` peek** — sample entry titles for collection discoverability
- **`scratch` delete action** — delete T1 scratch entries
- **Adaptive page ranges with OOM retry** — multi-page batch failure splits to 1-page retry. Config-driven `pdf.mineru_page_batch`.

### Fixed
- **CCE embedding model consistency** — eliminated voyage-4 fallback. On CCE batch failure, splits in half and retries with same model (voyage-context-3). Prevents embedding model mismatch within collections.
- **T3 list_store pagination** — real `offset` parameter passed to ChromaDB (was capped at 300 in cloud mode)
- **store_list title display** — falls back to `source_title` for PDF-indexed entries
- **`search` param `n` → `limit`** — consistent pagination parameter naming across all tools
- **FTS5 title search** — corrected documentation: memory_search searches title, content, and tags (was incorrectly documented as "title not searchable")
- **Agent tool discoverability** — all tool references use full `mcp__plugin_nx_nexus__` prefix. Fixed `mcp__sequential-thinking__` → `mcp__plugin_nx_sequential-thinking__` in 13 agent files.
- **`reinstall-tool.sh`** — symlinks `mineru-api` to `~/.local/bin` when `[mineru]` extra is present
- **`plan_save` schema** — documented minimal JSON schema in tool docstring

### Changed
- MCP tool count: 12 → 17
- `_CCE_TOKEN_LIMIT`: 32K → 24K (safety margin for academic text token estimation)
- Token estimate in CCE batching: `len//3` → `len//2` (conservative for academic text)

### Docs
- `reference.md` — 17 tools documented with full parameter tables and examples
- All 17 agent `.md` files updated with `nx Tool Reference` block and full tool names
- `CONTEXT_PROTOCOL.md` — full tool names throughout, search options table expanded
- `subagent-start.sh` — full tool names with `Tool:` prefix labels
- RDR-042, 043, 045, 046 closed

## [2.10.8] - 2026-04-02

### Changed
- **MinerU batched subprocess extraction** — large PDFs are now split
  into 5-page batches, each processed in an isolated subprocess. Prevents
  OOM on formula-dense documents (e.g. 108-page Grossberg 1986). GPU/model
  memory is fully reclaimed between batches.

### Docs
- Updated `cli-reference.md` and `architecture.md` with MinerU batching behavior.

## [2.10.7] - 2026-04-02

### Fixed
- **Preserve optional extras during reinstall** — added
  `scripts/reinstall-tool.sh` that reads `uv-receipt.toml` and preserves
  extras like `[mineru]` and `[local]` when reinstalling the CLI tool.
  Previously `uv tool install --reinstall .` silently dropped extras,
  breaking MinerU mid-session. Fixes #122.

## [2.10.6] - 2026-04-02

### Fixed
- **RDR close bead gate** — replaced "advisory only" bead status check
  with a hard gate that requires explicit user confirmation before closing
  an RDR with open or in-progress beads. Previously agents would see open
  beads and proceed to close anyway.

## [2.10.5] - 2026-04-02

### Fixed
- **MinerU extraction output paths** — updated `_extract_with_mineru` to
  match MinerU v2 `do_parse` API (positional output_dir, `pdf_bytes_list`,
  `p_lang_list`). Output directory now uses `pdf_path.name` (with extension)
  instead of `pdf_path.stem`. Tests updated to match.

## [2.10.4] - 2026-04-01

### Removed
- **PostToolUse prompt hook** — `type: "prompt"` is not valid for
  PostToolUse hooks, causing `PostToolUse:Bash hook error` on every
  Bash tool call. Removed entirely; `/nx:debug` remains available
  on demand.

## [2.10.3] - 2026-04-01

### Added
- **PostToolUse prompt hook** for debugger enforcement — detects
  repeated test failures and enforces `/nx:debug` invocation instead
  of manual retry loops.

## [2.10.2] - 2026-04-01

### Fixed
- **Restore skill routing guardrails** — re-added the Skill Directory
  tables, Process Flow graph, Storage Tier Protocol, and Red Flags
  anti-rationalization table to the `using-nx-skills` SessionStart
  injection. These were trimmed in RDR-039 for compactness but their
  removal caused agents to stop invoking specialized skills (debugger,
  architect, etc.).

## [2.10.1] - 2026-04-01

### Fixed
- **Verification hooks now advisory-only** — removed test suite
  execution from both Stop and PreToolUse hooks. Running tests inside
  hooks caused multi-minute delays on routine operations. Both hooks
  now perform fast checks only (uncommitted changes, open beads,
  review markers) and never block.
- **PreToolUse output format** — corrected to use `hookSpecificOutput`
  with `permissionDecision` (PreToolUse protocol), not `decision`/`reason`
  (Stop protocol).
- **Bead ID extraction** — fixed BSD sed compatibility on macOS (`sed -E`
  instead of GNU-only `\+`/`\b`/`\|`).

## [2.10.0] - 2026-04-01

### Added
- **Verification config** (RDR-045) — `_DEFAULTS["verification"]` section
  in `.nexus.yml` with `on_stop`, `on_close`, `test_command`, `lint_command`,
  `test_timeout` keys. New `get_verification_config()` and
  `detect_test_command()` in `config.py` with auto-detection for 7 project
  types (Maven, Gradle, Python, Node, Rust, Make, Go).

## [2.9.2] - 2026-03-31

### Changed
- **Eradicate superpowers references** — removed all live superpowers
  delegation from nx README, preflight check, and 4 skill files.
  nx is now fully self-contained with no superpowers dependency.
- **Move WIP tutorial to branch** — `docs/tutorial/` moved to
  `wip/tutorial` branch, off main.

## [2.9.1] - 2026-03-31

### Added
- **Math-aware PDF extraction (RDR-044)** — three-backend auto-detect
  routing: Docling detects formula regions, MinerU re-extracts math-heavy
  papers with superior LaTeX output, PyMuPDF normalized as terminal fallback.
  - `nx index pdf --extractor [auto|docling|mineru]` CLI option
  - `has_formulas` boolean on all PDF chunks for downstream filtering
  - `formula_count` in extraction metadata
- **Sticky PDF extractor config** — `nx config set pdf.extractor=mineru`
  sets the default globally (`~/.config/nexus/config.yml`) or per-repo
  (`.nexus.yml`). CLI `--extractor` flag overrides config when passed.
- **`nx config set` dotted keys** — `nx config set pdf.extractor=mineru`
  now writes nested YAML config, not just credentials.
- **MinerU optional dependency** — `uv pip install 'conexus[mineru]'`
  installs `mineru[all]` for math-aware extraction. ~2-3 GB model download
  on first run.

### Fixed
- **Missing Docling transitive deps** — added `python-pptx` and
  `opencv-python-headless` to fix Docling import failures on some platforms.

## [2.8.5] - 2026-03-30

### Changed
- **Plan-enricher widened scope (RDR-043)** — reframed from audit-findings
  delivery to general bead enrichment. Execution context (file paths, code
  patterns, constraints, test commands) is now the primary purpose. Audit
  findings incorporated when available, no longer required. "Degraded mode"
  removed.
- **Review gates in plans** — strategic planner includes mandatory
  code-review-expert tasks after implementation phases.

## [2.8.4] - 2026-03-30

### Added
- **Review gates in plans** — strategic planner now includes mandatory
  code-review-expert tasks after implementation phases in every plan.
- **Bib enrichment opt-in** — `nx index pdf --enrich` flag wired through
  CLI (was documented but not implemented). Default is off.

### Fixed
- **Pagination correctness** — `store_list` uses true collection count,
  `search` footer distinguishes "may have more" from "end". Standardized
  footer format across all paged tools. 11 pagination tests added.
- **Empty `--where` values rejected** — `key=` and `key>=` raise clear
  CLI errors instead of passing empty strings to ChromaDB.

## [2.8.3] - 2026-03-30

### Changed
- **Bib enrichment default flipped to opt-in** — `nx index pdf` no longer
  queries Semantic Scholar by default. Pass `--enrich` to enable inline
  metadata lookup. Use `nx enrich <collection>` for deliberate backfill.
- **MCP tool pagination** — `search`, `store_list`, and `memory_search`
  return paged results with `offset` parameter. Standardized footer:
  `--- showing X-Y of Z. next: offset=N` / `(end)`. `store_list` uses
  true collection count. No data lost — agents page through all results.
- **Hook output optimized for AI** — SubagentStart reduced 43% (6.3K→3.6K
  chars), SessionStart reduced 55%. Same information, structured for LLM
  parsing.

### Fixed
- **sn plugin Serena hook** — clarified `jet_brains_*` tools work with any
  LSP backend (not IntelliJ-specific). Added `find_file`, `list_dir`,
  Serena memories. Full MCP-prefixed tool names. Context7 prefixes fixed.
- **`--where` empty values rejected** — `key=` and `key>=` now raise
  `BadParameter` instead of silently passing empty strings to ChromaDB.
- **`store_list` missing collection** — returns "Collection not found"
  instead of misleading "No entries".

## [2.8.2] - 2026-03-30

### Added
- **SessionStart capabilities summary** — main conversation now gets a
  compact overview of `--where` operators, `/nx:query` pipeline, `nx enrich`,
  and plan library MCP tools on every session start.

## [2.8.1] - 2026-03-30

### Fixed
- **T2 plans table migration** — existing `memory.db` files created during
  v2.8.0 before the `project` column was added now auto-migrate on open.
  `ALTER TABLE plans ADD COLUMN project` + FTS5 rebuild runs transparently.

## [2.8.0] - 2026-03-30

### Added
- **Analytical query pipeline** — `/nx:query` skill decomposes complex questions
  into multi-step plans (search → extract → summarize → compare → generate),
  dispatched via new `query-planner` and `analytical-operator` agents. Step
  outputs persist in T1 scratch for cross-dispatch reference. (RDR-042)
- **Bibliographic metadata enrichment** — `nx index pdf` queries Semantic Scholar
  for year, venue, authors, and citation count. Opt-in with `--enrich`.
  Backfill existing collections with `nx enrich <collection>`. (RDR-042)
- **Structured table detection** — PDF chunks on pages containing tables are
  tagged `chunk_type=table_page`. Filter with `--where chunk_type=table_page`.
  Page-level granularity via Docling `TableItem` detection. (RDR-042)
- **`--where` comparison operators** — `nx search --where` now supports `>=`,
  `<=`, `>`, `<`, `!=` in addition to `=`. Known numeric fields (`bib_year`,
  `bib_citation_count`, `page_count`, etc.) are auto-coerced to int. (RDR-042)
- **T2 plan library** — `plans` table with FTS5 search, project scoping, and
  MCP tools (`plan_save`, `plan_search`). Saves successful query execution
  plans for future reuse. (RDR-042)
- **Orchestrator self-correction** — failure relay protocol distinguishes
  ESCALATION sentinels (route to debugger) from incomplete output (retry up
  to 2x with augmented context). (RDR-042)
- **NDCG retrieval smoke test** — synthetic corpus + ground-truth queries in
  `tests/benchmarks/` verify the search pipeline runs end-to-end with ONNX
  MiniLM. (RDR-042)

### Fixed
- **sn plugin Serena hook** — SubagentStart hook now uses full MCP-prefixed
  tool names (`mcp__plugin_sn_serena__*`) so subagents can actually resolve
  and call Serena tools. Previously used short names that subagents couldn't
  find.

## [2.7.1] - 2026-03-28

### Added
- **nx: three-tier storage guidance for all agents** — SubagentStart hook injects
  nx MCP tool signatures (T1 scratch, T2 memory, T3 search/store) into every
  subagent. Non-nx agents (general-purpose, superpowers, etc.) can now use
  T1 scratch for inter-agent communication, read T2 project context, and query
  the T3 knowledge store.

## [2.7.0] - 2026-03-28

### Added
- **sn plugin** — new lightweight Claude Code plugin that bundles Serena and
  Context7 MCP servers with a SubagentStart hook that injects tool usage
  guidance into all subagents. Serena configured with `--context claude-code`
  (minimal tool surface) and `--project-from-cwd` (auto-detect project).
  Install independently: `/plugin install sn@nexus-plugins`.
- **nx: sequential thinking injection** — SubagentStart hook injects usage
  guidance for the sequential thinking MCP tool.

## [2.6.1] - 2026-03-27

### Fixed
- **rdr-accept planning detection** — was matching only `## Implementation Plan`
  with `### Phase` subheadings. Now scans 6 section names (Implementation Plan,
  Approach, Plan, Design, Steps, Execution) and 4 subheading types (Phase, Step,
  Stage, Part, plus numbered `###`). Default flipped from "no" to "yes" — false
  positives are cheap, false negatives skip planning on complex work.

### Docs
- Tutorial video scripts (sections 0-9), companion cheatsheet
- Automated recording pipeline: expect + asciinema + agg + ffmpeg + speed-mapping
- `make` in `docs/tutorial/vhs/` reproduces the full demo video from scratch

## [2.6.0] - 2026-03-26

### Added
- **T1 scratch inter-agent context sharing** (RDR-041) — standardized scratch
  tag vocabulary (`impl`, `checkpoint`, `failed-approach`, `hypothesis`,
  `discovery`, `decision`), sibling context SHOULD for relay-reliant agents
  with relay-over-scratch precedence rule, developer writes failed approaches
  to scratch, code reviewer checks scratch for developer struggles before
  reviewing, debugger checks scratch for predecessor findings.
- **Debugger escalation relay** includes `nx scratch` field for pre-escalation
  failed-approach entries.
- **Re-dispatch developer relay template** with structured nx store/memory
  artifact references from debugger output.
- **Escalation guard** — if developer circuit breaker fires twice for the same
  bead, escalate to human instead of infinite developer→debugger loop.

## [2.5.0] - 2026-03-25

### Added
- **Developer agent circuit breaker** (RDR-040) — after 2 consecutive test
  failures, the developer agent stops and outputs a structured ESCALATION
  report. The parent dispatches the debugger with the failure context. Counter
  tracks test runs (not root causes), resets on green or new invocation.
  Supersedes the advisory "Recommend debugger" escalation trigger.
- **Debugger escalation relay template** in development skill — parent-side
  dispatch instructions with field mapping from escalation report to debugger
  relay.
- **Developer → debugger routing** in orchestration skill — escalation edge
  in routing diagram and quick reference table.

## [2.4.2] - 2026-03-25

### Docs
- **Python 3.14 troubleshooting** — `uv tool update` reuses the existing
  environment's Python, so upgrading under 3.14 doesn't auto-switch to 3.13
  despite the `requires-python` cap. Documented `--force --python 3.13` as
  the fix. Added `head -1 $(which nx)` diagnostic.

## [2.4.1] - 2026-03-24

### Fixed
- **`--collection` flag bypass of `t3_collection_name()`** — `nx index pdf --collection knowledge` now correctly normalizes to `knowledge__knowledge`, matching search conventions. Previously created bare collections invisible to `nx search` with wrong embedding model.
- **`memory promote --collection` same bug** — bare collection names in `nx memory promote` now normalized via `t3_collection_name()`.
- **Updated `--collection` help text** — no longer says "Fully-qualified" since bare names are now accepted and auto-normalized.
- **Updated CCE post-mortem** — linked RDR-040 resolution and documented the `--collection` naming variant.

## [2.4.0] - 2026-03-24

### Added
- **`nx collection reindex <name>`** — delete and re-index a collection from source files with pre-delete safety check, per-type dispatch (code/docs/rdr/knowledge), and post-reindex verification (A4)
- **`collection_list` MCP tool** — list all T3 collections with document counts and models (B2)
- **`collection_info` MCP tool** — detailed collection metadata including index/query models (B3)
- **`collection_verify` MCP tool** — known-document retrieval health probe (B4)
- **Per-chunk progress for pdf/md indexing** — `--monitor` now shows tqdm bar during embedding, not just post-hoc metadata (A5)
- **Retrieval quality unit tests** — assert semantic rank ordering with real ONNX embeddings (A1)
- **Cross-model invariant regression test** — fails if CCE index/query models diverge (A3)

### Fixed
- **Single-chunk CCE model mismatch** — documents with only 1 chunk in CCE collections now use `contextualized_embed()` instead of falling back to `voyage-4`, which produced vectors in an incompatible space (C1)
- **Unpaginated `col.get()` in indexer** — `_prune_deleted_files`, `_prune_misclassified`, and `_run_index_frecency_only` now paginate at 300 records to handle ChromaDB Cloud's hard cap (C2/C3)
- **Mixed-model CCE batches** — partial CCE failure now re-embeds the entire document with voyage-4 for consistency, preventing mixed-space vectors (C4)
- **MCP collection cache race** — `_get_collection_names()` uses atomic tuple assignment to eliminate the window where concurrent threads could see an empty list (C5)
- **`info_cmd` unbounded `col.get()`** — now uses `limit=300` for best-effort timestamp sampling
- **`reindex_cmd` corpus metadata** — derives corpus from collection name instead of storing empty string

### Changed
- **MCP `search` default** — changed from `corpus="knowledge"` to `corpus="knowledge,code,docs"` matching CLI behavior; added `"all"` alias for all corpora including rdr (B1)
- **`collection verify --deep`** — enhanced with known-document probe, distance reporting, and `VerifyResult` dataclass (A2)

### Docs
- Updated CLI reference, architecture docs, MCP tool reference, CLAUDE.md, and nx plugin CHANGELOG for all RDR-040 changes (D1–D6)

### References
- RDR-040: CCE Post-Mortem Gap Closure & MCP Server Enhancement
- Post-mortem: `docs/rdr/post-mortem/cce-query-model-mismatch.md`
- Epic: nexus-5rn1 (16 beads, all closed)
- PR: #118

## [2.3.6] - 2026-03-23

### Fixed
- **Restore voyageai as required dependency** — the `conexus[cloud]` optional extra
  was an unnecessary workaround. Since `requires-python < 3.14` blocks the only
  incompatible Python version, voyageai always works on supported Pythons. Reverted
  to direct `import voyageai` with no guards. Removed the `cloud` extra.

## [2.3.5] - 2026-03-23

### Docs
- **Streamlined getting-started guide** — linear flow from prerequisites through
  install, verify, use, plugin, cloud. Added `nx doctor` verify step, Python 3.14
  workaround, `uv tool update` instructions, and `conexus[cloud]`/`conexus[local]`
  extras documentation.
- **Three-pass substantive critique** — fixed query model docs (CCE collections use
  voyage-context-3 for both index and query), removed "T3 cloud" mislabeling from
  CLI reference, corrected tuning YAML structure (nested subsections, not flat keys),
  fixed local-mode auto-detection docs (either key absent, not both), added missing
  `--on-locked` flag and `NX_VOYAGEAI_READ_TIMEOUT_SECONDS` env var, corrected
  minified code detection description, and replaced all `pip install` with `uv` syntax.
- **Unprefixed skill references** — corrected `/rdr-create` → `/nx:rdr-create` etc.
  across all documentation and RDR files.

### Fixed
- **`is_local_mode()` docstring** — corrected to match implementation (either key
  absent triggers local mode, not both).

## [2.3.4] - 2026-03-23

### Fixed
- **Python 3.14 compatibility** — `voyageai` uses Pydantic v1 compat layer which
  is broken on Python ≥ 3.14. Moved `voyageai` from required to optional dependency
  (`pip install conexus[cloud]`). Capped `requires-python` to `<3.14` so uv/pip
  auto-select Python 3.13. All `import voyageai` sites guarded with clear error
  messages pointing to `conexus[cloud]`.
- **Unprefixed skill references in docs** — all `/rdr-create`, `/rdr-close`, etc.
  corrected to `/nx:rdr-create`, `/nx:rdr-close` across 11 documentation files.

## [2.3.3] - 2026-03-23

### Fixed
- **Python 3.14 compatibility** (partial) — guarded `import voyageai` in `t3.py`
  but missed `retry.py` and other import sites. Superseded by 2.3.4.

## [2.3.2] - 2026-03-22

### Fixed
- **Planning chain bypass prevention** — agents can no longer skip the
  strategic-planner → plan-auditor → plan-enricher chain by creating beads
  directly or compensating when subagents fail. PROHIBITION block added to
  rdr-accept, chain mandatory for multi-phase RDRs.
- **Silent bead content corruption** — `bd update --description "..."` silently
  destroys multi-line markdown (backticks, `$variables`, nested quotes). Replaced
  with Write tool → `--body-file` pattern in plan-enricher agent and skill.
- **Dead T2 idempotency code** — removed Python comparisons against always-None
  `t2_status`; self-healing logic moved to Action section with live `memory_get`.
- **Unbound placeholders** — fixed `{id}`, `{t2_status}`, `{repo_name}`, `{type}`
  leaking from Python into agent instructions; standardized to `<ID>` notation.

### Added
- **Known Pitfalls** section in writing-nx-skills skill — documents the
  `--description` corruption bug so future agent authors use `--body-file`.

## [2.3.1] - 2026-03-22

### Fixed
- **StopFailure hook junk beads** — guarded side effects behind `CLAUDECODE` env var
  so test runs no longer create junk beads and memories via `bd`.

## [2.3.0] - 2026-03-22

### Added
- **PostCompact hook** — re-injects in-progress bead state and T1 scratch entries
  after conversation compaction. Only emits output when there is content to show.
- **StopFailure hook** — logs API failure context to beads memory for observability.
  Creates a blocker bead on rate limits. Handles null `error_details` gracefully.
- **Integration tests in release checklist** — `uv run pytest -m integration` is now
  a required pre-release step in `docs/contributing.md`.

### Fixed
- **Test isolation** — patched `get_credential` in T3/store tests to prevent
  `~/.config/nexus/config.yml` from leaking real credentials into unit tests.
- **PostCompact scratch test** — no longer false-fails on CI when `nx scratch list`
  returns no entries.

### Docs
- RDR-039 closed: all 4 phases implemented.

## [2.2.0] - 2026-03-21

### Changed
- **Plugin hooks cleanup** — removed 5 dead/redundant hook scripts
  (`mcp_health_hook.sh`, `setup.sh`, `bead_context_hook.py`,
  `permission-request-stdin.sh`, `readonly-agent-guard.sh`) and 3 hook events
  (Setup, PostToolUse, PermissionRequest). Hooks reduced from 9 to 5.
- **Orchestrator upgraded** from haiku to sonnet — routing ambiguous requests
  needs reasoning depth.
- **T2 memory dedup** — removed duplicate T2 output from `session_start()`;
  `session_start_hook.py` via `t2_prefix_scan.py` is the single source.

### Fixed
- **rdr_hook.py** — added `closed` status to `_STATUS_ORDER` (was missing,
  caused wrong reconciliation direction), terminal conflicts now warn instead
  of auto-reconciling, fixed `_update_file_status` blank-line accumulation,
  reads `.nexus.yml` for RDR path instead of hardcoding `docs/rdr`.
- **"Task tool" → "Agent tool"** — corrected 19 stale references across skills,
  commands, and relay template.

## [2.1.1] - 2026-03-15

### Fixed
- **Plugin skill references** — all 19 nx plugin files now use fully-qualified
  `/nx:skill-name` form instead of short `/skill-name` which Claude Code cannot
  resolve for plugin-namespaced skills. Affected agents, commands, hooks, skills,
  and README.

## [2.1.0] - 2026-03-15

### Added
- **Local T3 backend** (RDR-038) — zero-config semantic search using ChromaDB
  `PersistentClient` + bundled ONNX MiniLM embeddings. `pip install conexus &&
  nx index repo . && nx search "query"` works with no API keys.
- `is_local_mode()` auto-detection: activates local mode when cloud credentials
  are absent. Force with `NX_LOCAL=1` or `NX_LOCAL=0`.
- `LocalEmbeddingFunction` with two tiers: tier 0 (bundled all-MiniLM-L6-v2,
  384d) and tier 1 (fastembed bge-base-en-v1.5, 768d via `pip install conexus[local]`).
- `NX_LOCAL_CHROMA_PATH` env var to override local ChromaDB storage path
  (default: `~/.local/share/nexus/chroma`).
- `nx doctor` shows local mode health checks: path, embedding model, collection
  count, disk usage. Cloud checks skipped in local mode.
- `[local]` optional dependency group: `pip install conexus[local]` for better
  embedding quality via fastembed.
- `sqlite3.OperationalError('database is locked')` added to retryable errors
  for PersistentClient concurrent write handling.
- Indexer pipeline local mode: `embed_fn` injection in `IndexContext`, local
  embedding in code/prose/PDF indexers.
- Search reranker skipped in local mode (no Voyage AI reranker available).
- `memory promote` uses `make_t3()` — works seamlessly in both local and cloud mode.

### Changed
- `T3Database.__init__` accepts `local_mode` and `local_path` parameters
  (first branch, before cloud probe).
- `make_t3()` returns local or cloud T3Database based on `is_local_mode()`.
- `store.py` `_t3()` skips cloud credential checks in local mode.
- MAX_QUERY_RESULTS clamping and CCE embedding paths gated on `_local_mode`.

### Docs
- `getting-started.md`: local-first zero-config section before cloud setup.
- `configuration.md`: local mode config reference (NX_LOCAL, NX_LOCAL_CHROMA_PATH).
- `storage-tiers.md`: local vs cloud T3 comparison table with tier details.
- `architecture.md`: updated T3 description for local/cloud backends.
- `README.md`: updated Quick Start and tier table for zero-config local mode.
- `CLAUDE.md`: updated T3 description and source layout for `local_ef.py`.

## [2.0.0] - 2026-03-14

### Breaking Changes
- **T3 storage consolidated from 4 databases to 1** (RDR-037) — `chroma_database`
  is now the actual database name, not a base prefix. All collection prefixes
  (`code__*`, `docs__*`, `rdr__*`, `knowledge__*`) coexist in a single ChromaDB
  Cloud database.
  - `nx config init` provisions 1 database instead of 4
  - `nx doctor` checks 1 database instead of 4
  - Old four-database layout is auto-detected on startup with migration guidance
  - Set `NX_MIGRATED=1` after migrating to skip the probe
  - **Migration is non-destructive** — old databases are never modified or deleted.
    They remain in your ChromaDB Cloud dashboard until you choose to remove them.
  - Migration steps:
    1. Export with the **pre-upgrade** version: `nx store export --all`
    2. Upgrade nexus
    3. Provision single DB: `nx config init` (creates `{chroma_database}`)
    4. Re-index repos: `nx index repo .`
    5. Import stored knowledge: `nx store import`
    6. Set flag: `export NX_MIGRATED=1` (or `nx config set migrated 1`)
    7. Verify: `nx doctor`
    8. Optional: delete the 4 old databases from the ChromaDB Cloud dashboard

### Changed
- `T3Database.__init__` uses probe-first single-client connection (was four-client loop)
- `_client_for()` is now a shim returning the single client (routing removed)
- `ensure_databases()` creates 1 database (was 4)
- `OldLayoutDetected` exception raised when old `{base}_code` database still exists

## [1.12.1] - 2026-03-14

### Docs
- **README intro paragraph** — rewritten for clarity: leads with what Nexus is,
  then what it provides, then the compounding value proposition.

## [1.12.0] - 2026-03-13

### Docs
- **README rewrite** — problem-first framing centered on knowledge management
  lifecycle rather than repository indexing. Three intro paragraphs: context loss
  problem, Nexus as solution with compounding knowledge, RDR as human-AI design
  system for team alignment.
- **Three tiers, one lifecycle** — storage tier section rewritten to explain why
  each tier exists (different lifetimes, different access patterns) and how agents
  use them cooperatively. T1 consistently framed as inter-agent coordination, not
  developer scratch pad.
- **Getting Started reorganized** — local-first flow: Install → T1/T2 (no keys) →
  Claude Code plugin → T3 semantic search. Readers get value before configuring
  cloud credentials.
- **RDR documentation overhaul** — Overview, Workflow, Nexus Integration, and
  Templates all edited for readability. Reduced density, removed duplication across
  documents, flattened deep heading nesting, removed excess section dividers.
- **docs/README restructured** — Core Concepts / RDR / Plugin / Reference grouping
  with improved descriptions.
- **Cross-document consistency** — T1 terminology, Getting Started descriptions,
  nav bar headers/footers, and link targets aligned across all docs.

## [1.11.1] - 2026-03-13

### Fixed
- **rdr-accept chain orchestration** — the planning chain (strategic-planner →
  plan-auditor → plan-enricher) broke after the planner completed because agents
  relied on impossible agent-to-agent relay (subagents cannot spawn subagents).
  The accept skill now explicitly orchestrates all three sequential dispatches.
- **Agent handoff model** — replaced "Successor Enforcement" sections across all
  15 agents with "Recommended Next Step" output blocks. Agents now output structured
  handoff recommendations; the caller (skill or main conversation) dispatches the
  next agent. Removes dead code that instructed agents to use tools they don't have.
- **Template variable mismatches** — `{rdr_file_path}` and `{path}` corrected to
  `{rdr_file}` in rdr-accept command and skill
- **Stale "spawn" imperatives** — architect-planner and developer agents updated
  from "spawn X" to output-oriented language matching the new handoff model
- **enrich-plan skill** added to using-nx-skills directory (was missing from
  skill registry table)
- **Flaky test on Python 3.13** — `test_entries_6_to_8_title_only` failed in CI
  because all entries shared the same second-level timestamp, making SQLite
  ordering non-deterministic. Test now asserts on snippet/title-only counts
  rather than specific entry names.

## [1.11.0] - 2026-03-12

### Added
- **Post-accept planning workflow** (RDR-036) — `/rdr-accept` now offers an optional
  planning handoff after acceptance: auto-detects multi-phase RDRs (2+ phases defaults
  yes), dispatches `strategic-planner → plan-auditor → plan-enricher` chain to create
  and enrich execution beads at accept time rather than close time
- **plan-enricher agent** (sonnet) — terminal node in planning chain; enriches beads
  with audit findings, execution context, file paths, and codebase alignment
- **`/enrich-plan` skill and command** — invoke plan-enricher standalone or as part of
  the RDR planning chain
- **Conditional successor routing in plan-auditor** — uses T1 `rdr-planning-context`
  tag with RDR ID correlation to route to plan-enricher only in RDR planning context

### Changed
- **`/rdr-close` bead decomposition replaced with advisory** — close no longer creates
  beads; displays a read-only bead status table (if beads exist from accept-time
  planning) and lets the human decide which to close
- **strategic-planner Phase 3** renamed from "Audit and Iteration" to "Audit Handoff";
  removed aspirational "iterate based on audit feedback" instruction
- Plugin now has 15 agents (was 14) and 28 skills (was 27)

## [1.10.3] - 2026-03-12

### Fixed
- **PyPI README links** — converted all relative markdown links to absolute GitHub URLs
  so documentation links work on the PyPI project page

### Docs
- Updated RDR section in README to reflect actual usage (35+ RDRs and counting) rather
  than hypothetical projections; added concrete cross-reference example (RDR-035/023)

## [1.10.2] - 2026-03-12

### Fixed
- **Remove `tools:` frontmatter from all 14 agents** (RDR-035) — Claude Code has a
  confirmed bug where explicit `tools:` declarations in plugin-defined agents filter
  out MCP tools, rendering the MCP server non-functional for subagents. Agents now
  inherit all tools from the parent session; the PermissionRequest hook remains as
  runtime enforcement.

### Docs
- Updated `nx/README.md` and `docs/contributing.md` to document the `tools:` bug
- Added supersession note to RDR-023, post-implementation note to RDR-034
- Created and closed RDR-035

## [1.10.1] - 2026-03-11

### Fixed
- Removed `SessionEnd` hook — Claude Code cancels hooks during process teardown,
  producing a spurious "Hook cancelled" error on every exit. The T1 server stops
  automatically when the process tree dies; the hook was effectively a no-op.

## [1.10.0] - 2026-03-11

### Added
- **MCP server for agent storage operations** (RDR-034) — FastMCP server (`nx-mcp`)
  exposing 8 structured tools for direct T1/T2/T3 access by agents without Bash
  dependency. Tools: `search`, `store_put`, `store_list`, `memory_put`, `memory_get`,
  `memory_search`, `scratch`, `scratch_manage`. Thread-safe lazy singletons with
  double-checked locking for T1/T3; per-call context managers for T2. Collection
  name cache with 60s TTL and short-circuit for fully-qualified corpus names.
  Entry point: `nx-mcp = "nexus.mcp_server:main"` in pyproject.toml.
- **Plugin migration to MCP tools** — all 14 agents, shared protocols, and 9 skills
  updated from CLI syntax (`nx scratch put ...`) to MCP tool syntax
  (`mcp__plugin_nx_nexus__scratch`). Human-facing docs (`docs/`) retain CLI syntax.

### Changed
- `id` parameter renamed to `entry_id` in `scratch()` and `scratch_manage()` MCP tools
  to avoid shadowing Python builtin.

### Docs
- Architecture diagram updated with dual Human→CLI / Agent→MCP access paths.
- Storage tiers doc notes two access paths (CLI for humans, MCP for agents).
- Plugin README expanded with full MCP Servers section and permission auto-approval.
- Contributing guide notes MCP tool requirements for agent authoring.

## [1.9.1] - 2026-03-10

### Docs
- **Documentation audit for 1.9.0 features** — all user-visible features now documented:
  - `architecture.md`: module map updated with decomposed indexer modules (`code_indexer.py`,
    `prose_indexer.py`, `index_context.py`, `indexer_utils.py`, `languages.py`) and `exporter.py`
  - `configuration.md`: new `[tuning]` section documenting all `TuningConfig` parameters
  - `storage-tiers.md`: T3 export/import section with usage examples and format description
  - `repo-indexing.md`: CODE extension count corrected (52), pipeline versioning section,
    minified code handling section
  - `README.md`: store command description updated
- **Release process hardened** — `docs/contributing.md` step 2 now requires a mandatory docs
  audit against `git log` before every release, with a checklist of docs to verify. Quick
  reference table expanded to list all docs that may need updates.

## [1.9.0] - 2026-03-10

### Added
- **Hybrid search score boosting** (RDR-026) — ripgrep exact-match results boost
  vector search scores by `EXACT_MATCH_BOOST=0.15`. Pre-reranker capture of
  `rg_file_paths` and `rg_matched_lines` metadata for downstream context windowing.
  Ripgrep-only results (files not in vector top-K) kept with `RG_FLOOR_SCORE * 0.8`
  penalty. Snapshot regression tests for search quality via syrupy.
- **Context line windowing** (RDR-027 Phase 1) — `-A`/`-B`/`-C` flags now center
  on matching lines within chunks (keyword match or rg_matched_lines) rather than
  always showing from chunk start. `-C N` changed from after-only alias to
  before+after (matching grep semantics). Bridge merging joins nearby matches
  separated by ≤2 lines.
- **Syntax highlighting** (RDR-027 Phase 2) — `--bat` flag pipes results through
  `bat` with per-file batching, merged line ranges, and graceful fallback. Skipped
  when `--no-color` or `NO_COLOR` is set.
- **Compact mode** (RDR-027 Phase 3) — `--compact` flag outputs one line per result
  in `path:line:text` format (grep-compatible).
- **Query-aware vimgrep** — `--vimgrep` now reports the best-matching line within
  the chunk when a query is provided, not always the first line.
- **Unified language registry** (RDR-028) — consolidated `LANGUAGE_REGISTRY` in
  `nexus.languages` maps 44 file extensions to 31 tree-sitter AST languages.
  Single source of truth replaces scattered `AST_EXTENSIONS`, `_COMMENT_CHARS`,
  and classifier extension sets. 8 new AST languages: Clojure, Dart, Elixir,
  Erlang, Haskell, Julia, OCaml, Perl.
- **Pipeline version stamping** (RDR-029) — `PIPELINE_VERSION` constant (currently 4)
  stored in collection metadata. `--force-stale` flag on `nx index repo` re-indexes
  only collections whose stamped version is outdated. `nx doctor` reports pipeline
  version status per collection.
- **Collection export/import** (RDR-031) — `nx store export` writes collections to
  portable `.nxexp` files (JSON header + gzip-compressed msgpack stream of records
  with embeddings). `nx store import` restores without re-embedding. Supports
  `--include`/`--exclude` glob filters, `--all` for bulk export, `--remap` for
  path substitution on import, and `--collection` for rename on import. Embedding
  model mismatch is rejected to prevent vector space corruption.
- **`nx store get`** — retrieve a T3 entry by its 16-char hex document ID, with
  optional `--json` output.
- **Minified code handling** — AST chunker detects minified files (avg line length
  > 500 chars) and falls back to byte-based splitting instead of producing
  single-chunk monsters.

### Changed
- **Indexer module decomposition** (RDR-032) — `indexer.py` split into focused
  modules: `code_indexer.py` (AST chunking + context extraction), `prose_indexer.py`
  (markdown indexing), `index_context.py` (IndexContext dataclass), `indexer_utils.py`
  (shared utilities). Backward-compatible re-exports from `nexus.indexer`.
- **TuningConfig externalized** (RDR-032) — `vector_weight`, `frecency_weight`,
  `file_size_threshold`, `ripgrep_timeout`, `pdf_chunk_chars`, and other knobs
  now read from `~/.config/nexus/config.yml` `[tuning]` section. Defaults derived
  from `TuningConfig()` dataclass to prevent drift.

### Fixed
- **Reliability hardening** (RDR-030) — silent error audit across 24 catch-and-pass
  blocks. All `except` blocks now log via structlog at appropriate levels. Log output
  directed to stderr; warnings suppressed in structured search output. T2 FTS5
  title field added to index for memory search.
- **Streaming export/import** — export writes page-by-page directly to gzip stream
  instead of accumulating all records in memory. Import flushes batches as records
  are unpacked from a single file handle (eliminates TOCTOU window). msgpack
  Unpacker limited to 10 MB buffer to prevent memory exhaustion on crafted input.
- **IndexContext.voyage_key** marked `repr=False` to prevent API key leakage in
  logs and tracebacks.
- **Empty remap prefix guard** — `nx store import --remap ":foo"` now raises
  `UsageError` instead of silently matching every path.
- **Code indexer double-encode fix** — content hashing uses `source_bytes` directly
  instead of re-encoding from the already-decoded string.

### Docs
- `cli-reference.md` updated with `nx store get`, `nx store export`, `nx store import`,
  and `--force-stale` flag documentation.

### Tests
- 2209 tests (up from ~2050 in 1.8.0). New coverage for `_extract_context` (5 AST
  scenarios), `index_code_file` happy path, `index_prose_file` non-markdown path,
  exporter edge cases (empty collection, corrupt msgpack, remap validation),
  and `TuningConfig` wiring.

## [1.8.0] - 2026-03-08

### Changed
- **Language-agnostic agents** (RDR-025) — renamed 3 Java-specific agents to
  language-agnostic names: `java-developer` → `developer`, `java-debugger` →
  `debugger`, `java-architect-planner` → `architect-planner`. Agents now read
  CLAUDE.md at runtime to detect language, build system, test command, and coding
  conventions. Slash commands renamed: `/java-implement` → `/implement`,
  `/java-debug` → `/debug`, `/java-architecture` → `/architecture`.
- **Plugin registry updated** — all pipelines, predecessor/successor chains,
  naming aliases, and model summary reflect new agent names.

### Added
- **CLAUDE.md preflight check** — `/nx-preflight` now includes a section 6 that
  validates CLAUDE.md has language, build system, and test command information.
  Missing sections show `[?]` warnings (not errors).

## [1.7.1] - 2026-03-07

### Added
- **Project-local `/release` skill** — enforces the full release checklist from
  `docs/contributing.md` as an actionable step-by-step workflow. Prevents skipping
  steps like `uv tool install --reinstall` or using `gh release create` instead
  of `git tag`.

## [1.7.0] - 2026-03-07

### Added
- **Agent tool permissions** (RDR-023) — all 14 nx agents now have explicit `tools`
  frontmatter following least-privilege assignments. Each agent declares only the
  tools it needs (Read/Grep/Glob, Bash, Write/Edit, WebSearch/WebFetch, Agent).
  Sequential thinking MCP tool added to all agents uniformly.
- **PermissionRequest hook expansion** (RDR-023) — auto-approve safe non-Bash tools
  (Read, Grep, Glob, Write, Edit, WebSearch, WebFetch, Agent, sequential thinking)
  so subagents are not silently denied. Bash allowlist expanded with `uv run pytest`,
  additional `bd` subcommands, and read-only `git branch`/`git tag` forms.
- **RDR process guardrails** (RDR-024) — soft-warning pre-checks at three workflow
  points to catch implementation attempts on ungated/unaccepted RDRs:
  brainstorming-gate skill (step 6), strategic-planner relay validation (step 6),
  and bead context hook (regex RDR-NNN detection).

### Fixed
- **git branch/tag hook patterns** — restricted to read-only forms only (`git branch -a`,
  `git tag -l`). Previously, bare `branch` and `tag` matched destructive operations
  like `git branch -D` and `git tag -d`.

## [1.6.1] - 2026-03-06

### Fixed
- **PermissionRequest hook** — auto-approve all `nx *` subcommands (previously only read-only
  subcommands were approved). `nx collection delete` is explicitly denied and requires
  user confirmation. New subcommands added in future releases are approved automatically.

## [1.6.0] - 2026-03-06

### Added
- **`nx memory delete`** (RDR-022) — delete T2 memory entries by `--project`/`--title`,
  `--id`, or `--project`/`--all`. Confirmation prompt shows `project/title` and content
  preview. `--yes` bypasses prompts. `--all` requires `--project` and is mutually
  exclusive with `--title` and `--id`.
- **`nx store delete`** (RDR-022) — delete T3 knowledge entries by exact 16-char `--id`
  or by `--title` (exact metadata match, paginated to handle multi-chunk documents).
  `--collection` is required. `--yes` bypasses the `--title` confirmation prompt.
- **`nx scratch delete`** (RDR-022) — delete a T1 scratch entry by ID prefix (as shown
  by `nx scratch list`). No confirmation prompt (T1 is ephemeral). Session ownership is
  verified before deleting — entries from other sessions cannot be removed.
- `T2Database.delete()` overloaded with `id: int | None` keyword argument, matching the
  `get()` API pattern.
- `T3Database.delete_by_id()`, `find_ids_by_title()` (paginated), `batch_delete()`.
- `T1Database.delete()` with two-step session-ownership check.

### Changed
- `nx store list` now shows the full 16-char document ID (previously truncated to 12),
  enabling copy-paste into `nx store delete --id`.

## [1.5.3] - 2026-03-05

### Docs
- Corrected release notes: 1.5.2 CHANGELOG now includes RDR-020 Voyage AI timeout
  entries that were missing from the initial squash merge.

## [1.5.2] - 2026-03-05

### Added
- **Voyage AI read timeout** (RDR-020) — all `voyageai.Client` construction sites now
  receive `timeout=120.0` (configurable via `voyageai.read_timeout_seconds` in config or
  `NX_VOYAGEAI_READ_TIMEOUT_SECONDS` env var) and `max_retries=3`. Prevents indefinite
  hangs on stalled Voyage AI API calls.
- **Voyage AI transient-error retry** — `_voyage_with_retry` wraps all six Voyage AI
  call sites (CCE embed, fallback embed, standard embed, code embed, rerank) with
  exponential backoff (1 → 2 → 4 s, capped at 10 s) retrying `APIConnectionError` and
  `TryAgain` up to 3 times. Errors handled by the built-in `max_retries` tenacity layer
  (Timeout, RateLimitError, ServiceUnavailableError) are kept disjoint.

### Refactor
- **`nexus.retry` leaf module** — moved `_chroma_with_retry`, `_is_retryable_chroma_error`,
  `_voyage_with_retry`, and `_is_retryable_voyage_error` from `db/t3.py` into a new
  `retry.py` with no `nexus.*` imports. Eliminates a local-import workaround in
  `scoring.py` that was required to avoid a circular-import test-isolation bug.

## [1.5.1] - 2026-03-04

### Fixed
- **ChromaDB transient error retry** — all ChromaDB Cloud network calls in `db/t3.py`,
  `indexer.py`, and `doc_indexer.py` are now wrapped with `_chroma_with_retry` (from
  `retry.py`): exponential
  backoff (2 → 4 → 8 → 16 → 30 s, capped) retrying up to 5 times on HTTP 429/502/503/504
  and transport-level errors (`ConnectError`, `ReadTimeout`). Non-retryable errors raise
  immediately. Fixes multi-thousand-file indexing runs aborted by a single transient 504.

### Docs
- **Transient Error Resilience section** added to `docs/repo-indexing.md` documenting
  retry behaviour and link to RDR-019.
- **Pre-push release checklist** added to `docs/contributing.md` to catch missing
  `uv.lock` commits before tagging.

### Tests
- Unit and integration tests for `_is_retryable_chroma_error` and `_chroma_with_retry`.
- `test_uv_lock_version_matches_pyproject` added to `TestMarketplaceVersion` — CI now
  enforces that `pyproject.toml`, `uv.lock`, and `marketplace.json` all carry the same
  version.

## [1.5.0] - 2026-03-04

### Added
- **Auto-provision T3 databases** — `nx config init` now creates the ChromaDB Cloud tenant
  and database automatically; `nx migrate` has been removed.

### Fixed
- `chroma_tenant` is now optional in credential validation.
- Resolve real tenant UUID before admin calls; use `get_database` for existence check.

### Docs
- White-glove UX polish: help text, wizard flow, troubleshooting, plugin agents/skills,
  and RDR documentation.
- RDR-001, 002, 017, 018 closed as implemented.

### Tests
- Coverage gaps from test-validator audit closed.

## [1.4.0] - 2026-03-03

### Added
- **File lock on `index_repository`** — per-repo `fcntl.flock` prevents concurrent
  indexing of the same repository. Supports `--on-locked skip` (return immediately,
  default) and `--on-locked wait` (block until lock released).
- **`nx hooks install / uninstall / status`** — installs `post-commit`, `post-merge`,
  and `post-rewrite` git hooks that automatically trigger `nx index repo` on each
  commit/merge. Hooks use a sentinel-bounded stanza so they compose safely with
  pre-existing hook scripts.
- **Hooks reminder in `nx index repo`** — on first successful index, if no hooks are
  installed the CLI prints a one-time suggestion to run `nx hooks install`.
- **`nx doctor` hooks check** — reports hook installation status and checks the index
  log for recent errors.

### Removed
- **`nx serve` / Flask / Waitress** — the polling server and all associated code
  (`server.py`, `server_main.py`, `polling.py`, `commands/serve.py`) have been
  deleted. Git hooks replace the auto-indexing use-case. Dependencies `flask>=3.0`
  and `waitress>=3.0` removed from `pyproject.toml`.

### Docs
- `cli-reference.md`: `nx serve` section replaced with `nx hooks` section.
- `repo-indexing.md`: HEAD polling explanation replaced with git hooks explanation.
- `architecture.md`: Server module row replaced with Hooks module row.
- `configuration.md`: `server.port` / `server.headPollInterval` rows removed.
- `contributing.md`: `nx hooks install` added to development setup steps.

## [1.3.0] - 2026-03-03

### Added
- **`--force` flag** on all four `nx index` subcommands (`repo`, `pdf`, `md`, `rdr`) —
  bypasses staleness check and re-chunks/re-embeds in-place. Mutually exclusive with
  `--frecency-only` (repo) and `--dry-run` (pdf).
- **`--monitor` flag** on all four `nx index` subcommands — prints per-file progress
  lines with file name, chunk count, and elapsed time. For `pdf` and `md`, prints
  page range, title, author, and section count after indexing.
- **Auto-enable monitor in non-TTY contexts** — per-file output is now emitted
  automatically when stdout is not a TTY (piped, backgrounded, CI), without needing
  `--monitor`. The flag remains available to force output in interactive sessions.
- **tqdm progress bar** on `repo` and `rdr` subcommands — shows a file-count bar in
  interactive TTY sessions; auto-suppressed when piped or backgrounded.
- **`on_start` / `on_file` progress callbacks** on the indexer layer — `index_repository`
  and `batch_index_markdowns` accept optional callbacks for real-time progress reporting.
- **`return_metadata`** parameter on `index_pdf` and `index_markdown` — returns a dict
  with chunk count, page range, title, author, and section count instead of a plain int.
- **Proactive 12 KB chunk byte cap** (`SAFE_CHUNK_BYTES = 12_288`) — single constant in
  `chroma_quotas.py` enforced across all three chunkers:
  - `chunker.py` escape hatch fixed: single oversized lines are now truncated at the
    UTF-8 boundary instead of emitted as-is.
  - `md_chunker.py` byte cap post-pass added after semantic/naive splitting.
  - `pdf_chunker.py` byte cap post-pass added after char splitting.
  - `t3.py _write_batch` last-resort drop-and-warn for any document exceeding
    `MAX_DOCUMENT_BYTES` (16 384) before upsert.

### Fixed
- **AST chunk line ranges** (RDR-016) — line numbers now derived from
  `node.start_char_idx` / `node.end_char_idx` instead of a hardcoded formula that
  produced systematically wrong ranges.
- **`_run_index` missing registry entry** — returns `{}` instead of raising when the
  path is not registered, preventing unhandled exceptions on first-run edge cases.

### Changed
- **Indexer helpers** return `int` chunk count instead of `bool` — callers get
  actionable count rather than a success/failure flag.

### Docs
- `cli-reference.md` updated with full `nx index` flag coverage: `--force`, `--monitor`
  (with auto-enable note), `--collection`, `--dry-run`, and `--frecency-only` mutual
  exclusion.

## [1.2.0] - 2026-03-03

### Added
- **`ContentClass.SKIP`** — fourth classification category silently ignores known-noise
  files (config, markup, shader, lock) instead of emitting them into `docs__` collections.
  18 extensions skipped: `.xml`, `.json`, `.yml`, `.yaml`, `.toml`, `.properties`,
  `.ini`, `.cfg`, `.conf`, `.gradle`, `.html`, `.htm`, `.css`, `.svg`, `.cmd`, `.bat`,
  `.ps1`, `.lock`.
- **Expanded code extensions** — 9 new extensions classified as CODE: `.proto`, `.cl`,
  `.comp`, `.frag`, `.vert`, `.metal`, `.glsl`, `.wgsl`, `.hlsl` (Protobuf and GPU
  shaders now indexed into `code__` with `voyage-code-3`).
- **Shebang detection** — extensionless files are classified as CODE when their first two
  bytes are `#!`, SKIP otherwise (catches `Makefile`, `LICENSE`, etc. correctly).
- **Context prefix injection (embed-only)** — each code chunk's embedding text is
  prefixed with `// File: X  Class: Y  Method: Z  Lines: N–M`. The raw chunk text is
  stored in ChromaDB unchanged; only the Voyage AI embedding call sees the prefix.
  Improves recall for algorithm-level queries in domain-specific codebases.
- **14-language class/method extraction** via tree-sitter `DEFINITION_TYPES` mapping
  (Python, Java, Go, TypeScript, Rust, C, C++, C#, Ruby, PHP, Swift, Kotlin, Scala).
  Used to populate the `class_name` and `method_name` fields in the context prefix.
- **AST language expansion** — `AST_EXTENSIONS` expanded from 16 to 28 mappings across
  19 parsers: Kotlin, Scala, Swift, PHP, Lua, Objective-C now receive AST-aware chunking.
- **`preserve_code_blocks`** — `SemanticMarkdownChunker` now defaults to
  `preserve_code_blocks=True`, preventing fenced code blocks from being split mid-content.
- **`_STRUCTURAL_TOKEN_TYPES` blocklist** — `paragraph_open`, `list_item_open`,
  `tr_open`, and similar structural markdown-it-py tokens are filtered so content
  appears exactly once per chunk (eliminates duplication from open/close token pairs).

### Changed
- **Chunk metadata** now includes `class_name`, `method_name`, and `embedding_model`
  fields on all code chunks.

### Removed
- **`--chunk-size` and `--no-chunk-warning`** flags removed from `nx index repo` —
  chunk size is not user-configurable; these flags were dead after the AST-first pipeline.

## [1.1.1] - 2026-03-02

### Fixed
- **`nx doctor` server check** — optional Nexus server now shows `✓` with status in
  detail string instead of `✗` with a Fix: hint, preventing false failures in
  preflight scripts that check exit code.

### Changed
- **Release process docs** — added explicit `uv sync` step and `uv.lock` to the
  `git add` list so lock file is never missed in a release commit.

### Docs
- RDR skill docs: `rdr-close` pre-check aligned with actual command behaviour
  (`"accepted"` not `"final"`); agent and skill counts corrected after PM removal.

## [1.1.0] - 2026-03-02

### Removed
- **`nx pm` command layer** — `nx pm new/status/close/list/archive/restore` commands
  removed. T2 memory (`nx memory`) serves this purpose directly with less overhead.
- **Mixedbread integration** — `--mxbai` search flag and `fetch_mxbai_results()` removed.
  Voyage AI via ChromaDB Cloud covers all semantic search needs.

### Added
- **`bd` and `uv` checks in `nx doctor`** — both reported as optional (informational only,
  no exit 1); `bd` includes install URL when absent.

### Fixed
- **`chroma` CLI no longer required on PATH** — `start_t1_server()` now locates the
  `chroma` entry-point relative to `sys.executable`, so it is always found when
  `conexus` is installed via `uv tool install` or `uv sync`. No separate install step.

## [1.0.0] - 2026-03-01

First stable release. Promoted from rc10 after live validation. No functional changes
from rc10 — this entry marks the API, CLI, and plugin contract as stable.

### Changed
- `Development Status` classifier promoted from `4 - Beta` to `5 - Production/Stable`.

## [1.0.0rc10] - 2026-03-01

### Changed
- Version bump to rc10 for release candidate validation prior to 1.0.0 final.
- Polish pass: CHANGELOG entries for rc7/rc8/rc9, hook script package name fix
  (conexus not nexus), skill count corrected to 28, serena-code-nav added to
  plugin README, free tier callout for ChromaDB and Voyage AI.

## [1.0.0rc9] - 2026-03-01

### Added
- **Storage tier awareness for agents**: SubagentStart hook injects live T1 scratch entries
  into every spawned agent's context — agents see what siblings and parent agents already
  discovered this session without duplicating work.
- **Storage Tier Protocol** in `using-nx-skills` SKILL.md: T3→T2→T1 read-widest-first
  table and T1→persist→knowledge-tidy write path, giving all agents a clear data discipline.

### Fixed
- **T2 FTS5 search crash on hyphenated queries**: `nx memory search "foo-bar"` raised
  `sqlite3.OperationalError: no such column: bar` — FTS5 was interpreting hyphens as column
  filter separators. Added `_sanitize_fts5()` helper that quotes special-character tokens
  before `MATCH`. Trailing `*` prefix wildcard preserved. Applies to `search()`,
  `search_glob()`, and `search_by_tag()`.

## [1.0.0rc8] - 2026-03-01

### Added
- **T1 ChromaDB HTTP server** (RDR-010): replaced `EphemeralClient` with a per-session
  `chroma run` subprocess. All agents spawned from the same Claude Code window share one
  T1 scratch namespace via PPID chain propagation — cross-process `nx scratch` reads and
  writes work correctly across separate shell invocations.
- **`serena-code-nav` skill**: navigate code by symbol — find definitions, all callers,
  type hierarchies, and safe renames without reading whole files.
- **`nx hook session-start` / `session-end`** (RDR-008): nx workflow integration hooks
  for session lifecycle management; T1 server is started on session-start and stopped on
  session-end.
- **`using-nx-skills` skill polish**: full 29-skill directory table with 5 categories,
  Announce step in process flow, 12 red flags (up from 7), `brainstorming-gate` replaces
  `verification-before-completion` in Skill Priority. Registry trigger conditions sharpened
  for knowledge-tidier, orchestrator, and substantive-critic. SessionStart hook matcher
  tightened to `startup|resume|clear|compact`.

### Removed
- **`--agentic` and `--answer` flags** removed from `nx search` (RDR-009): both modes
  required Anthropic API key and added latency for marginal benefit. Answer synthesis and
  agentic refinement are now agent responsibilities via the plugin skill suite.

### Fixed
- **T1 server startup**: removed `--log-level ERROR` from `chroma run` invocation — flag
  was dropped in chroma 1.x and silently caused every T1 start to exit code 2, falling
  back to isolated per-process EphemeralClient.
- **Session file keyed to grandparent PID**: `hooks.py` now calls `_ppid_of(os.getppid())`
  to reach the stable Claude Code PID rather than the transient shell subprocess that dies
  immediately after writing the session file.
- **T1 SESSIONS_DIR test isolation**: added `autouse` pytest fixture redirecting
  `SESSIONS_DIR` to `tmp_path`, preventing tests from discovering a live server's session
  records.

## [1.0.0rc7] - 2026-02-28

### Added
- **File-size scoring penalty for code search** (RDR-006): chunks from large files are
  down-ranked proportionally — `score *= min(1.0, 30 / chunk_count)`. Applied unconditionally
  to all `code__` results regardless of `--hybrid`. Files ≤ 30 chunks are unaffected.
- `nx search --max-file-chunks N`: pre-filters code results to files with at most N chunks
  via a ChromaDB `chunk_count $lte` where filter. Combines with `--where` using `$and`.
- **T2 multi-namespace prefix scan** (RDR-007): SubagentStart hook surfaces all
  `{repo}*` T2 namespaces (not just the bare project namespace) with a cap algorithm:
  5 entries with snippet + 3 title-only + remainder as count per namespace; 15-entry
  cross-namespace hard cap.
- `nx index repo --chunk-size N`: configurable lines-per-chunk for code files
  (default 150, minimum 1).
- `nx index repo --no-chunk-warning`: suppress the large-file pre-scan warning.
- **Large-file pre-scan warning**: detects code files exceeding 30× chunk size before
  indexing and suggests `--chunk-size 80`; adaptive recommendation when chunk size is
  already set.

## [1.0.0rc6] - 2026-02-28

### Fixed
- **CCE query model mismatch** (P0, affected rc1–rc5): `docs__`, `knowledge__`, and `rdr__`
  collections were indexed with `voyage-context-3` (CCE) but queried with `voyage-4`.
  These two models produce vectors in incompatible geometric spaces (cosine similarity ≈ 0.05
  — effectively random noise). All three collection types were returning semantically
  meaningless results since rc1. `code__` collections were unaffected.
  Fix: `corpus.py` returns `voyage-context-3` for CCE collections; `T3Database.search()`
  bypasses the ChromaDB `VoyageAIEmbeddingFunction` for CCE collections and calls
  `contextualized_embed([[query]], input_type="query")` directly. `T3Database.put()`
  likewise uses `contextualized_embed` with `input_type="document"` so single entries
  stored via `nx store put` land in the same CCE vector space as indexed chunks.
  **All CCE-indexed collections (`docs__*`, `knowledge__*`, `rdr__*`) must be re-indexed
  after upgrading from rc1–rc5.**

## [1.0.0rc5] - 2026-02-28

### Added
- **Four-store T3 architecture** (RDR-004): T3 now routes collections to four dedicated
  ChromaDB Cloud databases (`{base}_code`, `{base}_docs`, `{base}_rdr`, `{base}_knowledge`),
  one per content type. All routing is internal to `T3Database`; no CLI commands change.
- `nx migrate t3`: new command that copies collections from an old single-database T3 store
  to the new four-store layout. Idempotent; copies embeddings verbatim (no re-embedding).
- `nx doctor` now checks connectivity to all four T3 databases when credentials are present.

### Fixed
- Eliminated spurious per-corpus warning noise during `nx search`: warnings now fire once per unmatched corpus term across all collections, not once per internal resolver call

## [1.0.0rc4] - 2026-02-27

### Added
- `/rdr-accept` slash command with gate-result verification and T2 status synchronization
- `rdr-accept` skill: accepts gated RDRs, updates T2 and file frontmatter atomically (RDR-002)
- `-v`/`--verbose` flag for `nx` CLI: enables debug logging for network calls and index operations
- RDR indexing status shown in `nx index repo` output (count of RDRs indexed)
- MCP sequential-thinking server (`.mcp.json`): replaces `nx thought` for compaction-resilient reasoning chains

### Changed
- `nx thought` session isolation now uses Claude session ID instead of `getsid(0)` (RDR-002)
- SessionStart hook: T2 session records synchronized on startup (RDR-002)
- Sequential-thinking skill updated to use `mcp__sequential-thinking__sequentialthinking` instead of `nx thought add`

### Fixed
- `rdr-gate`: strip fenced code blocks before extracting section headings (false negatives on structured RDRs)
- `rdr-list` and all RDR commands: handle `RDR-NNN` naming convention; single-pass Python heredoc
- `hooks.json` format: wrap in `{hooks:{}}` with matcher/hooks nesting (plugin hook discovery was silently failing)
- Empty strings filtered from embedding batches before Voyage AI calls (prevented API errors on sparse content)
- Suppressed `llama_index` pydantic `validate_default` warning and `httpx`/`httpcore` wire-trace noise in `-v` mode
- structlog level in tests follows `pytest --log-level` (default: WARNING); debug logs surface on failure
- `rdr-accept` skill: description, relay template, PRODUCE section, and `nx scratch` reference now conform to plugin structure tests

## [1.0.0rc3] - 2026-02-26

## [1.0.0-rc2] - 2026-02-26

### Added
- Six RDR slash commands with live context injection (`/rdr-create`, `/rdr-list`, `/rdr-show`, `/rdr-research`, `/rdr-gate`, `/rdr-close`)
  - Each command pre-fetches project state (RDR dir, existing IDs, T2 metadata, active beads, git branch) before invoking the corresponding skill
  - Mirrors the context-injection pattern used by agent commands (`/review-code`, `/create-plan`, etc.)
- Plugin test suite: 752 unit + structural tests covering install simulation, `$CLAUDE_PLUGIN_ROOT` reference integrity, markdown link resolution, hook script presence, and marketplace version consistency
- E2E debug-load scenario (scenario 00): validates plugin load diagnostics, hook script execution, and component discovery via Claude `-p` mode without a live interactive session
- E2E test sandbox now includes locally-cached superpowers plugin alongside nx, enabling cross-plugin skill validation
- E2E isolation guard: verifies `nx@nexus-plugins` loads from dev repo, not the installed v1 cache

### Changed
- RDR skills now read the RDR directory from `.nexus.yml` `indexing.rdr_paths[0]` (default: `docs/rdr`) instead of hardcoding the path — consistent with the nx repo indexer config
- `registry.yaml` RDR skill entries updated with `command_file` references linking skills to their context-injecting command counterparts

### Fixed
- Marketplace version corrected from `1.0.0-rc1` to `1.0.0-rc2` (plugin structure test caught mismatch)
- E2E test harness: Python 3.14 incompatibility with chromadb/pydantic resolved by pinning install to Python 3.12

## [1.0.0-rc1] - 2026-02-25

### Added
- `nx thought` command group: session-scoped sequential thinking chains backed by T2 SQLite
  - `nx thought add CONTENT` — append thought, return full accumulated chain + MCP-equivalent metadata
  - `nx thought show` / `close` / `list` — chain lifecycle management
  - Chains scoped per session via `os.getsid(0)`, expire after 24 hours
  - Semantic equivalence with sequential-thinking MCP server: `thoughtHistoryLength`, `branches[]`, `nextThoughtNeeded`, `totalThoughts` auto-adjustment
  - Compaction-resilient: state stored externally in T2, not in Claude's context window
- `nx:sequential-thinking` skill: replaces external MCP dependency; uses `nx thought add` for compaction-resilient chains
- `/nx-preflight` slash command: checks all plugin dependencies (nx CLI, nx doctor, bd, superpowers) with PASS/FAIL per check
- Plugin prerequisites section in `nx/README.md` with dependency table and install commands
- Smart repository indexing: code routed to `code__` collections, prose to `docs__`, PDFs to `docs__`
- 12-language AST chunking via tree-sitter (Python, JS, TS, Java, Go, Rust, C, C++, Ruby, C#, Bash, TSX)
- Semantic markdown chunking via markdown-it-py with section-boundary awareness
- RDR (Research-Design-Review) document indexing into dedicated `rdr__` collections
- `nx index rdr` command for manual RDR indexing
- Frecency scoring: git commit history decay weighting for hybrid search ranking
- `--frecency-only` reindex flag: update scores without re-embedding
- Hybrid search: semantic + ripgrep keyword scoring with `--hybrid` flag
- Agentic search mode: multi-step Haiku query refinement with `--agentic` flag
- Answer synthesis mode: cited answers via Haiku with `--answer`/`-a` flag
- Reranking via Voyage AI `rerank-2.5` with automatic fallback
- Path-scoped search with `[path]` positional argument
- `--where` filter support for metadata queries
- `-A`/`-B`/`-C` context lines flags for `nx search`
- `--vimgrep` and `--files` output formats
- `nx pm` full lifecycle: init, status, resume, search, archive, restore
- `nx store list` subcommand
- `nx collection verify --deep` deep verification
- Background server HEAD polling for auto-reindex on commit
- Claude Code plugin (`nx/`): 15 agents, 26 skills, session hooks, slash commands
- RDR workflow skills: rdr-create, rdr-list, rdr-show, rdr-research, rdr-gate, rdr-close
- E2E test suite requiring no API keys (1258 tests)
- Integration test suite with real API keys (`-m integration`)

### Changed
- `sequential-thinking` skill now uses `nx thought add` as its tool-call mechanism (compaction-resilient by design)
- All agents previously using `mcp__sequential-thinking__sequentialthinking` updated to use `nx:sequential-thinking` skill
- All 11 agents with sequential-thinking now have domain-specific thought patterns, When to Use, and control reminders
- `nx doctor` improved: Python version check, inline credential fix hints, non-fatal server check
- CLI help text audited and aligned with `docs/cli-reference.md`; 15+ mismatches corrected
- Renamed `nx index code` → `nx index repo`
- Collection names use `__` separator (never `:`)
- Session ID scoped by `os.getsid(0)` (terminal group leader PID) for worktree isolation
- Stable collection names across git worktrees via `git rev-parse --git-common-dir`
- Embedding models: `voyage-code-3` for code indexing, `voyage-context-3` (CCE) for docs/knowledge, `voyage-4` for all queries
- T1 session architecture: shared EphemeralClient store + `getsid(0)` anchor
- Plugin discovery: `.claude-plugin/marketplace.json` at repo root (replaces `nx/.claude-plugin/plugin.json`)
- `nx pm` namespace collapsed; session hooks simplified
- Plugin slash commands: `/plan` → `/create-plan`, `/code-review` → `/review-code`

### Fixed
- CCE fallback metadata bug
- Search round-robin interleaving
- Collection name collision on overflow
- Registry resilience under concurrent access
- Credential TOCTOU race condition
- `nx serve stop` dead code removed
- Indexer ignorePatterns filtering
- Upsert idempotency in doc pipeline
- T1/T2 thread-safe reads

### Removed
- `nx install` / `nx uninstall` legacy commands
- `nx pm migrate` command
- Homebrew tap formula (superseded by `uv tool install`)
- `nx/.claude-plugin/` legacy plugin discovery directory

## [0.4.0] - 2026-02-24

### Added
- nx plugin v0.4.0: brainstorming-gate, verification-before-completion, receiving-code-review, using-nx-skills, dispatching-parallel-agents, writing-nx-skills skills
- Graphviz flowcharts in decision-heavy skills
- REQUIRED SUB-SKILL cross-reference markers
- Companion reference.md for nexus skill
- SessionStart hook for using-nx-skills injection
- PostToolUse hook with bd create matcher

### Changed
- All skill descriptions rewritten to CSO "Use when [condition]" pattern
- Relay templates deduplicated: hybrid cross-reference to RELAY_TEMPLATE.md
- Agent-delegating commands simplified with pre-filled relay parts
- Nexus skill split into quick-ref SKILL.md + detailed reference.md

### Fixed
- PostToolUse hook performance: now fires only on bd create, not every tool use
- Removed non-standard frontmatter fields from all skills

## [0.3.2] - 2026-02-22

### Added
- E2E tests for indexer pipeline and HEAD-polling logic

### Fixed
- `nx serve stop` dead code path

## [0.3.1] - 2026-02-22

### Added
- `nx store list` subcommand
- Integration test improvements: knowledge corpus scoping

### Changed
- README full readability pass: clearer setup path, optional vs required deps

## [0.3.0] - 2026-02-22

### Added
- Voyage AI CCE (`voyage-context-3`) for docs and knowledge collections at index time
- Ripgrep hybrid search: `rg` cache wired to `--hybrid` retrieval
- `--content` flag and `[path]` path-scoping for `nx search`
- `--where` metadata filter, `-A`/`-B`/`-C` context flags, `--reverse`, `-m` alias
- P0 regression test suite
- T3 factory extraction (`make_t3()`) with `_client`/`_ef_override` injection for tests
- `nx pm promote` and `NX_ANSWER` env override
- `nx collection verify --deep` and info enhancements
- Frecency-only reindex flag

### Changed
- Removed pdfplumber in favour of pymupdf4llm
- `search_engine.py` refactored into focused modules (`scoring.py`, `search_engine.py`, `answer.py`, `types.py`, `errors.py`)
- structlog migration

### Fixed
- 10 P0 bugs, 10 P1 bugs, 10 P2 bugs, 5 P3 observations
- CCE fallback metadata bug; `batch_size` dead parameter removed
- `serve` status/stop lifecycle, collection collision, registry resilience
- Credential TOCTOU, env override error handling
- T1 session architecture (getsid anchor, thread-safe reads)

## [0.2.0] - 2026-02-21

### Added
- `nx config` command with credential management and `config init` wizard
- Integration test suite (requires real API keys)
- E2E test suite (no API keys, 505 tests at release)
- T1 session architecture overhaul: shared EphemeralClient + getsid(0) anchor
- Scratch tier fix for CLI use outside Claude Code

### Changed
- Full README rewrite: installation, quickstart, command reference, architecture

### Fixed
- Scratch tier session isolation
- 5-stream global code review: 15 critical/significant fixes (mxbai chunk ID, security, resilience)

## [0.1.0] - 2026-02-21

### Added
- Project scaffold: `src/nexus/` package, `nx` CLI entry point via Click
- T1: `chromadb.EphemeralClient` + ONNX MiniLM, session-scoped scratch (`nx scratch`)
- T2: SQLite + FTS5 WAL, per-project persistent memory (`nx memory`)
- T3: `chromadb.CloudClient` + Voyage AI, permanent knowledge store (`nx store`, `nx search`)
- `nx index repo` (originally `nx index code`): git-aware code indexing with tree-sitter AST
- `nx serve`: Flask/Waitress background daemon with HEAD polling for auto-reindex
- `nx pm`: project management lifecycle (init, status, resume, search, archive, restore)
- `nx doctor`: prerequisite health check
- Claude Code plugin (`nx/`): initial agents, skills, hooks, registry
- Config system: 4-level precedence (defaults → global → per-repo → env vars)
- Hybrid search: semantic + ripgrep keyword scoring
- Answer synthesis: Haiku with cited `<cite i="N">` references
- Agentic search: multi-step Haiku query refinement
- Phase 1–8 implementations covering all CLI surface

[Unreleased]: https://github.com/Hellblazer/nexus/compare/v1.12.0...HEAD
[1.12.0]: https://github.com/Hellblazer/nexus/compare/v1.11.1...v1.12.0
[1.11.1]: https://github.com/Hellblazer/nexus/compare/v1.11.0...v1.11.1
[1.11.0]: https://github.com/Hellblazer/nexus/compare/v1.10.3...v1.11.0
[1.10.3]: https://github.com/Hellblazer/nexus/compare/v1.10.2...v1.10.3
[1.10.2]: https://github.com/Hellblazer/nexus/compare/v1.10.1...v1.10.2
[1.10.1]: https://github.com/Hellblazer/nexus/compare/v1.10.0...v1.10.1
[1.10.0]: https://github.com/Hellblazer/nexus/compare/v1.9.1...v1.10.0
[1.9.1]: https://github.com/Hellblazer/nexus/compare/v1.9.0...v1.9.1
[1.9.0]: https://github.com/Hellblazer/nexus/compare/v1.8.0...v1.9.0
[1.0.0]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc10...v1.0.0
[1.0.0rc10]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc9...v1.0.0rc10
[1.0.0rc10]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc9...v1.0.0rc10
[1.0.0rc9]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc8...v1.0.0rc9
[1.0.0rc8]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc7...v1.0.0rc8
[1.0.0rc7]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc6...v1.0.0rc7
[1.0.0rc6]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc5...v1.0.0rc6
[1.0.0rc5]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc4...v1.0.0rc5
[1.0.0rc4]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc3...v1.0.0rc4
[1.0.0rc3]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc2...v1.0.0rc3
[1.0.0rc2]: https://github.com/Hellblazer/nexus/compare/v1.0.0-rc1...v1.0.0rc2
[1.0.0-rc1]: https://github.com/Hellblazer/nexus/compare/v0.4.0...v1.0.0-rc1
[0.4.0]: https://github.com/Hellblazer/nexus/compare/v0.3.2...v0.4.0
[0.3.2]: https://github.com/Hellblazer/nexus/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/Hellblazer/nexus/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Hellblazer/nexus/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Hellblazer/nexus/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Hellblazer/nexus/releases/tag/v0.1.0
