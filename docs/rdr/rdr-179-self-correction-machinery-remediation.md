---
title: "Self-Correction Machinery Remediation: Relight Plan-Reuse, Retrieval Benchmarking, Plan-Library Hygiene, and Taxonomy-Aware Recall"
id: RDR-179
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-07-04
related_issues: [nexus-o02xe, nexus-9kq3h, nexus-vtp8h, nexus-r300v, nexus-83d6s]
related: [RDR-080, RDR-084, RDR-088, RDR-090, RDR-091, RDR-092, RDR-093, RDR-098, RDR-100, RDR-134, RDR-135, RDR-147, RDR-152, RDR-153]
---

# RDR-179: Self-Correction Machinery Remediation

## Problem Statement

The 2026-07-04 feature-drift/cohesion audit (T3
`analysis-nexus-feature-drift-cohesion-2026-07-04`, T2 same title) answered
"have we drifted into a shambles?" with a precise **no, but**: the
AgenticScholar core pipeline — ingest → catalog → aspects → operators →
`nx_answer` — is working, populated, and cohesive (18k catalog docs, 141k
chunks, 802 aspects, all ten §D operators wired, 447 topics), and RDR
governance is disciplined (141/173 closed; wrong directions scrapped clean).
What has failed is the **self-correction machinery around it** — the loops
that let the system feel its own drift:

1. **Reuse loop split-brained and polluted.** (CORRECTED by research
   179-R25/R26 — the audit's original "search is dark" framing was
   inverted.) The MCP path (`plan_search`/`plan_match`/`nx_answer`) is LIVE
   and correct against the service; the engine FTS is healthy (179-R27).
   What is dark is the CLI: all 12 `nx plan` subcommand sites hardcode the
   frozen pre-migration local SQLite (`src/nexus/commands/plan.py` —
   list/show/delete/disable/enable/set-scope/reseed + the 6 repair-*
   verbs), so operators inspect a dead snapshot and `reseed`/`repair-*`
   WRITE into it (179-R28). Meanwhile the live library the match gate
   actually consults is 79% pollution (55/71 rows are non-executable
   phase/bead dumps, 179-R29), degrading match quality. Same
   system-of-record-ambiguity failure shape as the 2026-06/07 postmortem —
   a split-brain between the surface operators trust and the store the
   system uses.
2. **Measurement loop never built.** RDR-090 (Realistic AgenticScholar
   Benchmark) was accepted and delivered as a 5-query spike, then never
   operationalized: no CI job, no NDCG guard, no plan-first-vs-naive A/B.
   The mechanism designed to catch retrieval drift is itself dormant — this
   audit existed only because a human asked.
3. **Curation loop absent.** `plan_save` accepts non-executable JSON
   (pre-migration: 116 plans, 77 null-verb, one bead-dump that MATCHED at
   0.66–0.70 then crashed the runner with `unknown tool ''`); the matcher
   never decays always-failing plans; `use_count` telemetry disagrees with
   run records. Live listing confirms the pollution survived migration:
   top plans are null-verb, zero-use, saved implementation plans.
4. **Recall investment stranded.** RDR-134 (taxonomy-aware recall in
   `nx_answer`) sits in draft while 447 topics / 190k assignments / 17.8k
   topic links go unread by the composed-retrieval path they were built for.

## Context

- Everything here is remediation of EXISTING accepted designs, not new
  architecture: RDR-080 defined plan-match-first; RDR-084 defined auto-save
  growth; RDR-090 defined the benchmark; RDR-134 defined taxonomy recall.
  This RDR sequences their repair/completion and adds the missing guard
  rails so the same silent decay cannot recur.
- Constraints inherited from the current program: RDR-147 stays
  world-blocked (post-cutover); RDR-155 P4b stays DO-NOT-START; cutover is
  freeze-gated conexus-side. Nothing in this RDR touches those.
- The audit's secondary findings ride along as P2s rather than separate
  RDRs: `use_count` telemetry drift, the `knowledge__agentic-scholar`
  provenance corpus missing from the live collection list, and the stalled
  post-cockpit UI drafts (RDR-122/127/136/150) needing explicit
  close-or-claim disposition.

## Proposed Approach (priority-ordered phases)

### Phase 1 — P0: Heal the plan-surface split-brain (bead nexus-o02xe)

Root cause is PINNED (179-R25..R28): client-side only, no engine cut, no
backfill. The engine FTS (`fts_vector GENERATED ALWAYS ... STORED`) and
`PlanRepository.searchPlans` are correct; `T2Database` routes to
`HttpPlanLibrary` correctly in service mode; the CLI never does.

1.1 Convert the 12 hardcoded `PlanLibrary(path=default_db_path())` sites in
    `src/nexus/commands/plan.py` (incl. `_open_plans_db()` for the repair-*
    family) to the backend-routed facade `T2Database` already uses
    (`storage_backend_for("plans")`).
1.2 The write-shaped verbs (`reseed`, `repair-*`, `delete`, `disable`,
    `enable`, `set-scope`) get explicit service-mode semantics — they were
    silently mutating the dead snapshot (179-R28); decide per-verb whether
    each routes to the service or errors with guidance.
1.3 Recurrence guard: real-client-over-faked-transport save→list→show→
    search round-trip through `HttpPlanLibrary`; rehearsal shakeout Phase B
    gains a `plan save → plan match` round-trip so a future cutover cannot
    split-brain this surface silently.
1.4 Verify end-to-end: `nx plan list` and MCP `plan_search` agree on the
    same store; `nx_answer` logs a plan-match HIT on a builtin template
    query (ids 3-17 exist live, 179-R29).

### Phase 2 — P0: Operationalize the RDR-090 benchmark (bead nexus-9kq3h)

(Research 179-R20/R23: the harness is 6/11 BUILT — scaffold in-tree at
`scripts/bench/`, NDCG@3 + multi-hop precision implemented, three A/B paths
wired, `force_dynamic` live. The remaining work is exactly the RDR-090
epic's five open beads; do not re-derive the design.)

2.1 Close nexus-lnlm/ufvu/o9fd: author the 10/10/10 factual/comparative/
    compositional query sets against the pinned `rdr__nexus-571b8edd`
    corpus (continuity with the spike's recorded baselines: A=0.555,
    B/C=0.277 NDCG@3); the 5 spike queries fold into their buckets.
2.2 Close nexus-inp8 + nexus-lky6: weekly-cron + retrieval-path-filtered
    GH workflow; threshold policy AS ALREADY DECIDED at RDR-090's gate
    (relative >=10% drop AND absolute floor — `check_regression.py` +
    `floors.yaml`). Budget reality (179-R23): ~45 min and ~$6-30 per full
    run, ~$25-120/month at weekly cadence.
2.3 NEW scope this RDR adds (the one genuine gap): an answer-groundedness
    metric (per-claim citation check against retrieved chunks) — 2.1's
    NDCG-only scoring cannot see a fluent-but-ungrounded answer.
2.4 First run baselines, second gates; consumes Phase 1's healed surface
    so the plan-first leg is honest. Do NOT fold `bench/queries/
    abstract-themes.yml` in — separate informal artifact (179-R21).

### Phase 3 — P1: Plan-library curation loop (bead nexus-vtp8h)

3.1 Save-time validation: `plan_save` rejects payloads that do not parse
    into an executable DAG (steps reference known tools; verb present or
    derivable) — the bead-dump class becomes a 4xx, not a library entry.
3.2 Match-time decay: plans with `success_count == 0 && failure_count >= N`
    are skipped by the matcher and flagged for review; repeated post-match
    execution failure decrements eligibility (CacheRAG floor from RDR-100
    finally enforced on the failure side).
3.3 One-time prune per the 179-R29/R31 census manifest: KEEP ids 3-17 + 20
    (16 genuine templates: RDR-078/092/097/098 builtins + one micro-plan);
    RETAG the 55 pollution rows `verb=implement` (excluded from the match
    gate — resolves OQ-3); DELETE outright the CLI-command-as-tool
    sub-class (the runner-crash mode — retag alone leaves a live grenade).
3.3b Close the admission seam at its SOURCE (179-R30): the verbatim
    plan_save boilerplate in 11 agent/skill instruction files tells every
    agent to save implementation output into the QUERY-plan library —
    rewrite the boilerplate to tag `verb=implement` (or a separate save
    surface) in the same pass as 3.1's validation, or 3.1 alone just makes
    those saves fail loudly instead of polluting.
3.4 Reconcile `use_count` with `nx_answer_runs` and fix the increment seam
    (concrete drift: id 13 match_count=362/use_count=0, id 12 match=46/
    use=0 — 179-R31).

### Phase 4 — P1: Taxonomy-aware recall (RDR-134 — gate, accept, implement)

4.1 This RDR does not duplicate RDR-134's design; it sequences it. Per
    179-R24 the substrate made 134 SMALLER than drafted: both primitives
    already exist server-side (`HttpCentroidStore.ann_query`/`nearest` for
    topic ranking, `search_topic_scoped` for scoped retrieval — RDR-156
    deliverables) and the named prerequisite nexus-n1908 is closed; the
    unbuilt piece is the query-time glue in nx_answer. Sequence: a pre-gate
    refresh pass on 134's Approach/infrastructure-audit table (name the
    existing primitives; resolve the granularity fork to per-topic), then
    `/conexus:rdr-gate 134` + accept + implement. Also fix in passing: the
    `search` tool's `cluster_by` docstring claims a "semantic" default the
    code does not implement (core.py:944 vs :957 — 179-R24 item 5).
4.2 Phase 2's benchmark gains a taxonomy-on/off A/B leg the moment 134
    lands, closing the loop between the investment and its measurement.

### Phase 5 — P2 sweep (audit residuals)

5.1 Provenance corpus: locate/migrate/re-index the original
    `knowledge__agentic-scholar` 172-chunk corpus (or document its
    intentional retirement).
5.2 Stalled-draft disposition: RDR-122, 127, 136, 150 each get an explicit
    close-or-claim decision recorded in frontmatter (the audit found the
    cockpit-adjacent drafts parked without a verdict).
5.3 Fold in the standing measurement residual nexus-r300v (cfc72 1-vs-2
    warm-cloud throughput) — it is the same "we built it, we never measured
    it" class and Phase 2's benchmark harness is a natural host.

## Alternatives Considered

- **Fix beads ad-hoc without an RDR.** Rejected: the audit's core lesson is
  that these loops decayed precisely because no single artifact owned them;
  the individual features all have closed RDRs that say "done." A
  remediation RDR is the artifact whose §Approach a phase-review-gate can
  cross-walk.
- **Rebuild the plan library from scratch (drop + regrow via RDR-084
  auto-save).** Rejected as primary: the migrated store contains genuinely
  useful templates (RDR-097/098 hybrids) worth pruning rather than
  discarding; regrow-from-zero also loses the A/B baseline. Prune-in-place
  (3.3) with the option to fall back if classification proves junk-dominated.
- **Fold RDR-134 implementation into this RDR.** Rejected: 134 has its own
  design surface (recall fusion weights, topic-boost shape) that deserves
  its own gate; this RDR only sequences and motivates it.

## Consequences

- Positive: the moat (plan-reuse) operates again and is smoke-gated per
  release; retrieval quality becomes a measured, CI-gated property instead
  of a vibe; the library becomes self-cleaning; the taxonomy investment
  reaches recall; the next silent-decay gets caught by machinery, not by a
  human asking "have we drifted?"
- Costs: a weekly benchmark job burns Voyage/API budget (bounded, budgeted
  in 2.1); save-time validation may reject payloads existing callers send
  (the implementation-plan save path needs a sanctioned home, 3.3);
  engine-side plan-search fixes ride an engine cut.
- Risks: Phase 1 root-cause may reveal deeper FTS-parity gaps (RDR-152
  FTS contract) — if so, scope stays confined to the plans store here and
  a finding is filed for the general contract.

## Open Questions

- OQ-1: ANSWERED (179-R25..R27): client-side CLI hardcoding — 12 sites in
  commands/plan.py; engine FTS healthy; no backfill; the MCP path was
  never broken.
- OQ-2: ANSWERED (179-R23): keep the pinned `rdr__nexus-571b8edd` corpus +
  the RDR-090 epic's pre-decomposed 10/10/10 query beads; do not
  re-derive. The genuinely open design item moved to Phase 2.3
  (groundedness metric).
- OQ-3: ANSWERED (179-R29/R31): `verb=implement` retag, except the
  CLI-command-as-tool sub-class which is deleted (crash risk).
- OQ-4: Does RDR-135 (windowed aspect extraction, draft) join Phase 5's
  disposition sweep or stay independently scheduled?

## Research Findings

### 179-R20: RDR-090 benchmark harness is far more built than the Problem
Statement implies — Phase 1 infra is CLOSED, only query-authoring (P2) and
CI (P3) remain

**Evidence**: `bd show nexus-ic83` (the RDR-090 epic) shows 6/11 children
CLOSED: `nexus-dslg` (force_dynamic flag — P1.1), `nexus-smhc` (plan_match
scope-aware corpus filter — P1.2), `nexus-zgko` (plan-grow match-text
hygiene — P1.3), `nexus-u7r0` (T3 staleness sweep — P1.4), `nexus-zg4c`
(catalog staleness remediation — P1.5), `nexus-q5yt` (bench harness
scaffold — P1.6). The scaffold is real and in-tree: `scripts/bench/{runner,
metrics,schema,paths}.py`, `bench/queries/spike_5q.yaml` (the 5 gate-decision
queries verbatim), `tests/benchmarks/test_retrieval_ndcg.py` (NDCG math
unit tests + a synthetic-corpus smoke test, explicitly NOT a production
quality proxy). `force_dynamic` is live in
`src/nexus/mcp/core.py:4636-4752` and `scripts/bench/paths.py`'s `run_path_c`
calls it directly (with a documented fallback to pre-#346 `scope=corpus`
semantics that is now dead code since dslg shipped).

**What's still open** (matches epic's remaining P4 children, all OPEN):
`nexus-lnlm`/`nexus-ufvu`/`nexus-o9fd` (author 10 factual / 10 comparative /
10 compositional queries — only the original 5-query spike set exists, no
30-query set), `nexus-inp8` (GH Actions weekly workflow — absent; confirmed
`ls .github/workflows/` has no bench-named workflow), `nexus-lky6`
(regression-alert thresholding — absent, no `check_regression.py` or
`floors.yaml` anywhere in tree). Epic notes (2026-05-17 audit) explicitly
record the bench infra files (factual/comparative/compositional YAMLs,
`check_regression.py`, `bench.yml`, `floors.yaml`) as absent and downgrade
the epic to P4 as a "speculative consumer; no current contract gating
release" — i.e. Phase 2/3 were deliberately deprioritized, not forgotten.

**Confidence**: High (direct bead + file-tree verification).

### 179-R21: A second, unrelated "bench" query set exists
(`bench/queries/abstract-themes.yml`) — do not conflate it with the RDR-090
harness when scoping Phase 2

**Evidence**: `bench/queries/abstract-themes.yml` (10 queries across two
corpora: `docs__art-grossberg-papers`, `knowledge__delos`) uses a different,
informal schema (`dominant_themes_2026_05_03` substring lists,
"informational; not a pass/fail gate") and is explicitly NOT NDCG-scored —
its docstring says the behavioral contract lives in
`tests/test_abstract_themes_plan_integration.py` and this file is for
"delta inspection," superseding a prior hardcoded-substring test
(commit `7bc5a940`). It does not extend or count toward RDR-090's 30-query
factual/comparative/compositional design; RDR-179 Phase 2.1 ("promote the
5-query spike... to a maintained benchmark module") should treat it as a
separate, pre-existing artifact, not a starting point to fold in.

**Confidence**: High (file read + commit message).

### 179-R22: The AgenticScholar paper's source corpus
(`knowledge__agentic-scholar`) is confirmed ABSENT from the live T3
collection list — corroborates RDR-179's Phase 5.1 residual, but also means
the paper's own §6.1-6.2 methodology text is not independently re-verifiable
via T3 search right now

**Evidence**: `mcp__plugin_conexus_nexus__collection_list()` full output
(2026-07-04) enumerates 44 live collections (`code__*`, `docs__*`,
`knowledge__*`, `rdr__*`) — no `knowledge__agentic-scholar` entry, and
`search(query="AgenticScholar deep research...", corpus="all")` /
`catalog_search(query="agentic scholar")` both return no hits on the paper
itself (only nexus's own internal aspect-extractor scholarly-paper prompt
code, and unrelated benchmark-comparison notes like `herb-benchmark-nexus-
integration`). RDR-090's §Context already captured the paper's key numbers
from when the corpus was presumably still indexed (NDCG@3=0.606 vs naive
0.411 on a 25-doc synthetic corpus, template-constrained LLM query
generation, §6.2 acknowledged circularity) — that record is the only
surviving citation-level detail; a fresh read of the paper's actual
methodology section is not possible without re-indexing.

**Confidence**: High (live collection_list is a direct enumeration; the
paper's absence is not a search-threshold artifact — catalog_search found
literally zero entries for "agentic scholar").

### 179-R23: Benchmark harness proposal for RDR-179 Phase 2 (OQ-2 answer)

**(a) What exists vs what RDR-090 promised** — see 179-R20/R21 for the
itemized gap; summary: infra scaffold + 5/30 queries + 0/2 CI pieces.

**(b) Concrete harness proposal**:

- **Corpus slice**: keep `rdr__nexus-571b8edd` (the spike's pinned corpus,
  `--corpus` default in `runner.py`) for continuity — same rationale RDR-090
  itself gives (versioned snapshots so bench re-validates applicability).
  Do NOT re-derive against the full 18k-doc catalog: the spike already
  found 7.8% source-path staleness at N=5 scale before remediation (u7r0
  fixed this class), and a fresh slice loses comparability with the
  existing 5-query baseline numbers recorded in the gate decision
  (Path A=0.555, B=0.277, C=0.277 NDCG@3).
- **Query set**: OQ-2's own leaning ("keep the 5 for continuity, add
  15-20 derived from live plan-library verbs") is directionally right but
  should be tightened to RDR-090's original 10/10/10 split rather than an
  ad hoc 15-20, since `schema.py`'s `VALID_CATEGORIES` already hardcodes
  `{factual, comparative, compositional}` and the 3 open beads
  (`lnlm`/`ufvu`/`o9fd`) are pre-decomposed at 10 each — reuse them
  verbatim rather than re-deriving a new split. The 5 spike queries fold
  into the 10 factual+comparative+compositional buckets they already
  belong to (Q1/Q2/Q3 factual, Q4 comparative, Q5 compositional) rather
  than being additive.
- **Metrics**: NDCG@3 (already implemented, `bench/metrics.py:ndcg_at_k`,
  hand-verified against known values in `test_retrieval_ndcg.py`) +
  multi-hop precision for compositional queries (implemented,
  `multi_hop_precision`, fraction of grade>=2 GT keys hit). No answer-
  groundedness scorer exists yet — RDR-179's Phase 2.1 text says "NDCG@k +
  answer-groundedness scoring" but the harness only has NDCG@3 today;
  groundedness would need a new metric (e.g. `operator_verify` per-claim
  citation check against retrieved chunks) — this is new work, not a gap
  in what's already built. The AgenticScholar paper's own metric is also
  NDCG@3 (179-R22), so no cross-paper metric alignment issue.
- **A/B legs**: path A (naive `nx search`) / B (`nx_answer` plan-routed) /
  C (`nx_answer(force_dynamic=True)`) already exist and are wired
  end-to-end (179-R20). A taxonomy on/off leg (RDR-179 §4.2) cannot be
  added until RDR-134 lands (see 179-R24) — Phase 2's harness should add
  the leg as a stub/placeholder path (`D: nx_answer(taxonomy_prefilter=
  True)`) once 134 ships, not now.
- **Runtime budget + cost**: RDR-090's own estimate (30 queries × 3 paths ×
  ~2s/query naive ≈ 3 min) undercounts — the spike's actual path-B/C
  numbers were 45.7s/query and 35.4s/query respectively (both go through
  `claude -p` operator dispatch), so 30×2 paths(B,C)×~40s ≈ 40 minutes,
  plus path A (30×~9.4s ≈ 5 min) ≈ **45 min per full run**, not 3.
  API cost: each B/C query is a `claude -p` operator subprocess (multi-step
  plan_run), so budget per query is closer to the `operator_*` tools'
  ~$0.05-0.25/call (per `nx_answer`'s own `budget_usd` default of 0.25) ×
  ~2-4 operator steps ≈ **$0.10-0.50/query** × 30 queries × 2 paths (B,C)
  ≈ **$6-30/run**. Weekly cadence ≈ $25-120/month — bounded but non-trivial;
  RDR-179's §Consequences flags this ("bounded, budgeted in 2.1") without
  a number, this estimate should replace the placeholder.
- **CI trigger shape**: weekly cron (per RDR-090 §Technical Design) PLUS
  path-filtered triggers on retrieval-surface changes. Enumerated paths
  (verified against current tree): `src/nexus/search_engine.py`,
  `src/nexus/plans/runner.py`, `src/nexus/plans/**`, `src/nexus/mcp/core.py`
  (specifically the `nx_answer`/`search`/`search_topic_scoped`/
  `search_metadata_scoped`/`search_graph_hop` tool functions — core.py is
  4700+ lines and mixes unrelated tools, so a path-level trigger on the
  whole file will over-fire; a follow-up could scope via a CODEOWNERS-
  style include-list of function names, but git-diff path filters can't do
  that — accept whole-file granularity as the pragmatic v1), `src/nexus/
  db/http_vector_client.py` (the service-mode search functions),
  `src/nexus/db/t2/catalog_taxonomy.py` + `http_taxonomy_store.py` +
  `http_centroid_store.py` (once RDR-134 lands, these become retrieval-
  surface too).
- **Threshold policy**: RDR-090 §Technical Design already specifies
  BOTH an absolute floor per category and a relative regression alert
  (bead `nexus-lky6`: "relative >=10% drop AND absolute floor"), which
  matches RDR-179's own framing of the OQ as if it were still open — it
  isn't; the design decision was already made at RDR-090 gate time. The
  only remaining work is implementing `nexus-lky6` as code
  (`check_regression.py` + `floors.yaml`, both currently absent).

**(c) OQ-2 recommendation**: Do not re-litigate corpus/query-set choice —
RDR-090's gate-decision-time answer (pin `rdr__nexus-571b8edd`, grow to the
pre-decomposed 10/10/10 via the three open beads) is already the right
answer and matches the epic's existing task breakdown; RDR-179 Phase 2
should point directly at closing `nexus-lnlm`/`nexus-ufvu`/`nexus-o9fd`/
`nexus-inp8`/`nexus-lky6` rather than re-deriving a corpus/query-set design.
The one genuinely open design gap is the missing groundedness metric,
which RDR-179's Phase 2.1 text presumes exists and doesn't.

**Confidence**: High for (a)/(b) infra facts (direct file/bead
verification); Medium for the cost estimate (extrapolated from N=5 spike
timings, not measured at N=30 scale); Medium for the CI path-filter
granularity call (a judgment call, not a verified constraint).

### 179-R24: RDR-134 readiness — the pgvector/service substrate makes the
design EASIER than drafted, and one blocking prerequisite (nexus-n1908) is
already closed

**Evidence — substrate is more capable than RDR-134 assumed**:

1. RDR-134's Approach text says the recall prefilter should "rank candidate
   topics (or collections) via the existing centroid index /
   `project_against`" — assuming `CatalogTaxonomy`'s local ChromaDB
   `taxonomy__centroids` collection (RDR-070, local-mode design) is the
   only ranking mechanism. Current reality: `src/nexus/db/t2/
   http_centroid_store.py`'s `HttpCentroidStore` class exposes `ann_query`
   and `nearest` methods against a **service-backed pgvector centroid
   store** (`/v1/taxonomy/centroids` routes, RDR-156 `nexus-t1hnc`) — i.e.
   the exact "rank topics by query-to-centroid similarity" primitive
   RDR-134 needs already exists server-side, is NOT a local-only ChromaDB
   artifact, and needs no separate migration concern (`taxonomy_etl.py`'s
   docstring flagging centroids as "NOT migrated" describes the ETL's own
   scope, not present-day availability — nexus-t1hnc superseded that gap
   with a first-class centroid port).
2. `search_topic_scoped` (`src/nexus/mcp/core.py:1293-1356`, RDR-156 P4)
   is a live, wired, service-mode-only MCP tool: it joins the chunk table
   to `topic_assignments` server-side and vector-ranks in one SQL
   statement — this is the "run the search step scoped to that topic
   selection" half of RDR-134's Approach, already built, NOT drafted-only.
   It requires an explicit `topic` label as input (it does not itself do
   the query-to-topic ranking — that's `HttpCentroidStore.ann_query`'s
   job); the two pieces exist independently but are not yet composed.
3. The plan runner (`src/nexus/plans/runner.py`) already has scope-
   forwarding plumbing: `scope.taxonomy_domain` → `corpus` and
   `scope.topic` → `topic=` kwarg forwarding into search-step args
   (`_apply_scope_to_args`, lines ~588-646, ~1279-1291) — but this only
   fires when a SAVED PLAN declares `scope.topic` explicitly. There is
   still no automatic, query-time "embed the question, rank topics,
   inject the winning topic into the search step" glue — that remains
   the one genuinely unbuilt piece, and it is smaller in scope than
   RDR-134 drafted (no new centroid infrastructure needed, no migration
   spike needed — just wiring `nx_answer`'s composed-retrieval assembly
   to call `HttpCentroidStore.ann_query`/`nearest` and populate
   `scope.topic` before `plan_run` dispatches).
4. Gap 2 (topic vs collection granularity fork) is substantially
   DE-RISKED, not resolved: since the centroid ANN query and the scoped
   search are both per-topic (not per-collection) server-side primitives,
   and both are already proven at RDR-156's gate, there is no live
   per-collection-summary alternative to weigh — per-topic was always
   going to win once the server-side primitives were built at topic
   granularity. The "spike" RDR-134's Critical Assumptions call for
   (per-topic centroids sufficient vs need per-collection summaries) can
   be downgraded from "needs a spike" to "confirm empirically once wired,"
   since the infrastructure choice is already made upstream by RDR-156,
   not still open.
5. Gap 3 (MCP `cluster_by` default) is UNCHANGED / still valid: verified
   `src/nexus/mcp/core.py:944` — the `search` MCP tool's `cluster_by`
   parameter still defaults to `""` (off) despite a docstring on line 957
   claiming `"semantic"` is the default ("`cluster_by: "semantic" for
   topic/Ward clustering (default), empty to disable`" — the docstring is
   stale/wrong; actual code says `cluster_by: str = ""`). This gap is real
   and RDR-134's §Approach language for it stands unchanged.
6. The prerequisite RDR-134's own Context section names —
   `nexus-n1908` ("nx_answer degrades to summarizing SessionStart hook
   context on empty/weak retrieval") — is CLOSED (2026-05-28): scope
   normalization (comma-list scope -> unscoped + warning) and an explicit
   empty-retrieval guard shipped as two pure-function fixes with 11 unit
   tests. RDR-134's Critical Assumption "[ ] An empty/low-confidence
   prefilter fails honestly rather than degrading (depends on
   nexus-n1908)" can move from Unverified to substantially de-risked —
   the underlying honesty mechanism it depends on is shipped; what remains
   is confirming the new taxonomy-prefilter code path routes through the
   same guard rather than re-introducing the ambient-context-synthesis
   failure mode via a new code path.

**What would change in RDR-134's §Approach**: the Approach paragraph
should be rewritten to name the two already-built primitives explicitly
(`HttpCentroidStore.ann_query`/`nearest` for ranking, `search_topic_scoped`
for scoped retrieval) instead of describing them as things to build from
`CatalogTaxonomy.project_against`/local MiniLM. The "spike" language in
Critical Assumptions 1-2 (measure recall/latency, decide granularity)
should be narrowed to "confirm the already-built primitives compose
correctly and measure the composed path's recall/latency delta vs blind
fan-out" — a smaller, more scoped spike than originally framed. Gap 2's
"must pick one (or define when each applies)" framing can be resolved in
the RDR text now (topic granularity, per findings above) rather than left
open pending a spike.

**Is anything blocking gate/accept beyond someone deciding?** No hard
blocker found. `nexus-n1908` (the one named prerequisite) is closed. The
service-mode-only constraint on `search_topic_scoped` / centroid ANN is
not a blocker since RDR-179's own context confirms the program is past the
RDR-152/155/156 cutover on develop (service substrate is the live target,
not a future one). No world-block or freeze applies to RDR-134 the way it
does to RDR-147/155-P4b.

**Readiness verdict**: **Gate-ready, with a required redraft pass before
gating** — not "needs redraft sections X, Y" as a blocking condition, but
as the recommended pre-gate edit: update §Problem Statement Gap 2's framing
and §Proposed Solution's Approach/Existing-Infrastructure-Audit table to
name `HttpCentroidStore` and `search_topic_scoped` as existing (Reuse) components
rather than describing 2026-05-27-era local-ChromaDB mechanics that no
longer match the codebase RDR-134 would be implemented against. The
substance of the design (rank topics, scope search, `all` escape hatch,
depends on n1908) is unchanged and sound; only the "what already exists"
column needs a refresh so `/conexus:rdr-gate 134` doesn't flag stale
Technical Environment references. This matches RDR-179 Phase 4.1's own
framing ("run rdr-gate 134 + accept, then implement") — the redraft is a
same-session pre-gate step, not a separate blocking phase.

**Confidence**: High for the primitive-existence claims (direct source
reads: `http_centroid_store.py`, `core.py:1293-1356`, `runner.py` scope-
forwarding). Medium for the "no hard blocker" conclusion (based on bead/
RDR cross-reference, not an exhaustive freeze-gate audit — RDR-179's own
Context section says RDR-147 and RDR-155-P4b are the only named
world-blocks, and RDR-134 is not among them).

### 179-R25: OQ-1 answer — the dark seam is `nx plan {list,show,delete,
disable,enable,set-scope,reseed,repair-*}` bypassing service mode entirely,
not a broken engine route or missing tsvector backfill

**Evidence**: `src/nexus/commands/plan.py` hardcodes
`from nexus.db.t2.plan_library import PlanLibrary` (the raw-SQLite class)
and constructs it directly against `default_db_path()` in **every**
subcommand — `lib = PlanLibrary(path=db_path)` at lines 292 (`list`), 386
(`show`), 455 (`delete`), 504 (`disable`), 539 (`enable`), 609
(`set-scope`), 683 (`reseed`) — plus `_open_plans_db()` at line 71, which
opens a raw `sqlite3.connect(str(db_path))` for the five `repair-*`
subcommands (`repair_scope_tags_cmd`, `repair_dimensions_cmd`,
`repair_match_text_cmd`, `repair_retire_legacy_cmd`,
`repair_builtin_bindings_cmd`, `repair_all_cmd`, lines 93-220). None of
these 12 call sites checks `storage_backend_for("plans")` — contrast with
`src/nexus/db/t2/__init__.py:435-441`, where `T2Database.__init__`
correctly branches: `if storage_backend_for("plans") ==
StorageBackend.SERVICE: self.plans = HttpPlanLibrary()` else raw
`PlanLibrary(path)`. Every MCP tool (`plan_search`, `plan_save`, and
`nx_answer`'s plan-match gate at `mcp/core.py:4757-4769`, which does
`with _t2_ctx() as db: ... library=db.plans`) goes through this correctly-
branching facade. The CLI does not: it never calls `_t2_ctx()` or
`T2Database` at all for the plans commands.

**Confidence**: High (direct source read of both the buggy call sites and
the correct facade).

### 179-R26: Live reproduction — the seam runs the OPPOSITE direction from
the RDR's Problem Statement framing: `nx plan list` is the STALE/dark side,
`plan_search`/`plan_match`/`nx_answer` are correctly live

**Evidence**: Local `~/.config/nexus/memory.db` plans table:
`SELECT count(*), max(id), max(created_at) FROM plans` → `(116, 179,
'2026-06-30T21:56:01Z')` — exactly the "pre-migration: 116 plans" figure
RDR-179's own Problem Statement §3 cites, frozen at the moment of cutover
and never written to since (matches `nx plan list`'s max id of 179 seen in
this session). Direct live query against the Postgres service via
`HttpPlanLibrary` (`uv run python -c "... HttpPlanLibrary().list_plans(
limit=300, include_disabled=True)"`) returns 71 live rows with ids ranging
up to 350 — i.e. the service has kept growing (116 imported rows -> 71
surviving/deduped + new saves up to id 350) while the local SQLite file
sat frozen. Calling the MCP `plan_search` tool (which routes through
`_t2_ctx()` → `HttpPlanLibrary` → `POST /v1/plans/search`) with the query
`"jOOQ generated record reflection"` returned `[233] implement engine
native-image GraalVM Feature to fix jOOQ ge...` — a real, current match.
But `nx plan show 233` (CLI, raw SQLite) replied `No plan matches '233'`,
and that id never appears in `nx plan list`'s output (capped at 179). So
in this live environment, `plan_search` works correctly and finds fresh
content the CLI cannot see — the inverse of "list works, search is dark."
The RDR's stated symptom likely reflects an observation made via `nx plan
list` (the CLI, silently reading the frozen pre-migration snapshot) being
mistaken for "the live plan store," with a separate/contemporaneous
`plan_search` query that happened to return no hits for an unrelated
reason (FTS is a real no-match for novel text, not a broken index — see
179-R27).

**Confidence**: High (direct live reproduction against both stores in this
session).

### 179-R27: Engine-side FTS is healthy; no tsvector data backfill is
needed — the fix is entirely client-side

**Evidence**: `service/src/main/resources/db/changelog/plans-001-baseline.xml`
changeset `plans-001-3` defines `fts_vector` as a `GENERATED ALWAYS AS (...)
STORED` tsvector column computed from `match_text`/`tags`/`project` on
every INSERT/UPDATE — it is recomputed automatically, not backfilled
separately, so any row written through either `savePlan` (`doSave`,
`PlanRepository.java:550-615`) or the fidelity-preserving import path
(`doImport`/`importBatch`, lines 617-546) already carries a correct
tsvector, provided `match_text` was populated at write time (it always is:
`HttpPlanLibrary.save_plan` synthesizes `match_text` client-side via
`_synthesize_match_text` before every `/v1/plans/save` POST, http_plan_
library.py:136-146, and the import payload carries `match_text` verbatim
from the SQLite source row). `PlanHandler.java` routes `POST /v1/plans/
search` to `repo.searchPlans` (line ~256), and `PlanRepository.searchPlans`
(lines 265-287) builds the RDR-152 FTS-parity-contract-specified OR'd
`plainto_tsquery('english', ?) || plainto_tsquery('simple', ?)` condition
against `fts_vector`, ranks by `ts_rank`, and correctly excludes expired/
disabled rows. This is the same route `HttpPlanLibrary.search_plans` posts
to (http_plan_library.py:394-407). Nothing here is broken. **Fix location:
client-side only**, confined to `src/nexus/commands/plan.py`'s 12 raw-
SQLite call sites (179-R25) — route them through
`storage_backend_for("plans")` / `T2Database` (or equivalently construct
`HttpPlanLibrary()` directly when service mode is active) instead of
hardcoding `PlanLibrary(path=default_db_path())`. No engine change, no
data migration/backfill needed.

**Confidence**: High (direct source read of the changelog, both Java
repository methods, and the Python client's match_text synthesis).

### 179-R28: Fix-scope risk — `nx plan reseed`/`repair-*` are ALSO
silently local-only, so builtin-template reseeding and legacy-plan repair
via the CLI never reach the live Postgres store either

**Evidence**: Same 12 call sites as 179-R25. `nx plan reseed` (re-runs the
four-tier builtin-template seed loader) and all six `nx plan repair *`
subcommands operate exclusively against `default_db_path()`'s SQLite file
in service mode — meaning an operator who runs `nx plan reseed` today,
believing they are refreshing the live plan library, is actually writing
into the frozen local snapshot that nothing else reads. This should be
in-scope for RDR-179 Phase 1.2's fix, not just `list`/`search`-adjacent
commands, since the same silent-drift failure mode (system-of-record
ambiguity, cf. `project_system_of_record_ambiguity_postmortem`) applies to
every one of the 12 sites uniformly.

**Confidence**: High (same source evidence as 179-R25; scope inference is
direct, not speculative).

### 179-R29: Target 3 — live plan-store census (71 rows, via direct
`HttpPlanLibrary.list_plans` against the service, NOT the stale `nx plan
list`): ~21% genuinely valuable, ~79% pollution

**Evidence**: `HttpPlanLibrary(...).list_plans(limit=300,
include_disabled=True)` against `https://api.conexus-nexus.com` (this
session) returns 71 total rows. Classification:

- **Executable query templates (15 rows, ids 3-17)** — all tagged
  `builtin-template`, all with a non-null `verb`: RDR-078 verb-scoped
  dispatch templates (`review` id 13, `research` id 12, `plan-promote` id
  11, `plan-inspect` ids 9-10, `plan-author` id 8, `document` id 6, `debug`
  id 5, `analyze` id 3); RDR-092 catalog-aware templates (`research`/
  author-lookup id 7, `research`/type-scoped id 14, `research`/citation-
  chain id 4); RDR-097 hybrid-rag templates (`lookup` id 16, `lookup`/
  graph-traversal id 17); RDR-098 abstract-qa template (`query` id 15).
  Real usage signal exists on a handful: id 15 `match_count=33`; id 14
  `use_count=1, match_count=136, success_count=1`; id 7 `use_count=1,
  match_count=8, failure_count=1`; id 3 `use_count=1, match_count=1,
  success_count=1`. Everything else in this set of 15 has `use_count=0`.
- **One borderline legitimate auto-grown micro-plan**: id 20,
  `plan_json={"steps":[{"tool":"search","args":{"query":"$intent"}}]}`,
  tag `search`, verb null — a genuine (if minimal) single-tool executable
  DAG, RDR-084 grow-path shaped.
- **55 rows of non-executable saved-implementation-plan pollution** — null
  `verb`, `use_count=0` for all but a few, dominated by the tag
  `strategic-planner` (also seen: agent/skill-specific tags naming other
  repos' agents). `plan_json` shapes fall into three sub-classes, all
  unusable by `plan_runner`: (a) phase/bead/epic hierarchy dicts (e.g. id
  232 `{"rdr":"RDR-176","epic":"nexus-t9rmg","phases":5,...}`, id 116, 105,
  108, 106, 104, 102, 97 and more — "rdr_phase_summary" shape); (b)
  narrative string-array steps (e.g. id 233 `{"steps":["commit native-
  regression harness diagnostics...", "TDD guard test...", ...]}` — the
  exact plan shown via `nx plan show 179`/`plan_search` earlier, ids 233,
  107, 103, 101, 100, 98, 75, 66); (c) step dicts that carry a `tool` key
  whose value is a CLI-command-as-string, not a `plan_runner`-registered
  tool name — e.g. id 113 step `{"id":"epic","tool":"bd create -t
  epic",...}`, id 111 step `{"action":"write_failing_tests",...}`, id 59
  step `{"tool":"mcp__plugin_nx_nexus__scratch","args":{"action":"get",
  ...}}` (a real MCP tool name but used as a planning-context lookup, not
  a retrieval step), id 55 `{"tool":"Read+scratch.get"}` (not a real tool
  at all). This sub-class is the one that would reproduce the RDR's cited
  crash mode (`unknown tool ''`) if matched and executed by `plan_runner`.
  Sample ids inspected across all three sub-classes: 350, 116, 115, 113,
  112, 111, 110, 108, 106, 105, 104, 102, 99, 97, 93, 88, 87, 69, 60, 59,
  55, 46, 42, 41, 20 — spanning projects `nexus`, `conexus`, `conductus`,
  `qwen-coprocessor-stack`, `Luciferase` (confirming this is a cross-
  project pollution pattern, not nexus-specific).

**Confidence**: High for the row counts and shape classification (direct
live enumeration + `plan_json` inspection); the exact sub-class boundaries
are a judgment call on ambiguous middle cases (a handful of the 33 "other"-
bucket rows were not individually inspected).

### 179-R30: Target 3 — the admission-path seam is the "Post-flight" /
"Recommended Next Step" boilerplate baked into 11 agent/skill files;
`plan_save` itself performs zero shape validation

**Evidence**: `mcp/core.py`'s `plan_save` tool (~lines 3004-3028) only
checks `if not query or not plan_json: return "Error: ..."` — no check
that `plan_json` parses into a step-list, that steps reference real tools,
or that a `verb` is present/derivable. The exact call site instructing
every agent to populate the library with implementation output is a
verbatim (or near-verbatim) boilerplate block present in **11 files**:
`conexus/agents/{debugger,deep-analyst,codebase-deep-analyzer,architect-
planner,developer,test-validator,strategic-planner,code-review-expert,
deep-research-synthesizer,substantive-critic}.md` (10 agent definitions)
plus `conexus/skills/using-nx-skills/SKILL.md`, each containing: `**Multi-
step pipeline outcome** (caller orchestrating you alongside other agents)
→ plan_save(query="<task>", plan_json={"steps":[...],"tools_used":[...],
"outcome_notes":"..."}, tags="<agents>") so future runs of similar tasks
get a plan-match hit.` This is a general "any multi-step work" trigger,
not scoped to genuinely retrievable query DAGs — and the example shape it
models (`{"steps":[...], "tools_used":[...], "outcome_notes":"..."}`) is
exactly the narrative/phase shape found polluting the store in 179-R29.
`conexus/skills/plan-first/SKILL.md` (lines 33, 66) has a narrower,
legitimate version of this instruction scoped to the actual query-plan-
grow path (RDR-084) — that one is not part of the pollution problem.
`strategic-planner.md` and `architect-planner.md` both carry the
boilerplate at line 76 verbatim.

**Confidence**: High (direct grep + file read across all 11 files;
`plan_save`'s validation-free implementation directly confirmed).

### 179-R31: Target 3 — keep/retag/delete manifest shape for the Phase 3.3
one-time prune

**Recommendation** (informs Phase 3.3, does not implement it):

- **KEEP** (match-gate-eligible, no change): ids 3-17 (the 15
  `builtin-template` rows) + id 20 (the minimal grown search plan) — 16
  rows total.
- **RETAG** (OQ-3's leaning confirmed correct — tag, not a separate
  table): all 55 non-executable rows get a `verb=implement` (or an
  equivalent `kind=implementation` marker) stamp. Matcher-side, this is
  cheap to enforce: `_superset()` in `src/nexus/plans/matcher.py` already
  does dimension-based inclusion/exclusion (line 231-246) — a filter step
  excluding `verb=implement` rows from `search_plans`/`list_active_plans`
  candidate pools is a small, additive change, not a new mechanism. This
  preserves history (the phased-plan records stay queryable via `nx plan
  show`/direct SQL for provenance/audit) without them competing in the
  match gate.
- **DELETE** (junk/crash-risk, prioritize for outright removal over
  retag): the sub-class of rows whose `steps[].tool` value is not a real
  `plan_runner`-registered tool (179-R29 sub-class (c) — sample ids 113,
  111, 59, 55 and likely others in the uninspected "other" bucket) — these
  are the ones that reproduce the cited `unknown tool ''` crash if ever
  matched despite a `verb=implement` retag (a retag prevents the *gate*
  from selecting them, but doesn't prevent a manual `nx plan show <id>` +
  copy-paste re-admission, or a future matcher bug from surfacing them
  again). Phase 3.1's save-time DAG-validator is the durable fix that
  prevents recurrence; the one-time 3.3 prune should not rely on retag
  alone for this specific sub-class.
- Reconciliation note for 3.4: several rows already show `match_count` >>
  `use_count` (id 13 review template: `match_count=362, use_count=0`; id
  12 research template: `match_count=46, use_count=0`) — i.e. the matcher
  is selecting these templates but callers are not running them (or the
  `increment_run_started` seam is not firing on the taken path). This is
  concrete evidence for RDR-179 3.4's "`use_count` telemetry disagrees
  with run records" finding, not merely inferred from the audit.

**Confidence**: Medium — the keep/retag/delete boundary for the 33
"other"-bucket rows not individually inspected is a reasonable
extrapolation from the inspected samples, not row-by-row verified.
