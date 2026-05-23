---
title: "Semantic Tuple Space: Unified Coordination Primitive over ChromaDB + SQLite"
id: RDR-110
type: Architecture
status: scrapped
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-09
accepted_date: 2026-05-09
scrapped_date: 2026-05-19
scrap_reason: "Part of the RDR-110/111/112/113/118/119 arc that bundled the storage-substrate split with new abstractions (tuplespace, ORB cockpit, host-trust, surfaces-as-tuples, UI fabric). Scope discipline failed across nine RDRs and 67 stranded beads. Substrate work continues under RDR-120 with an explicit moratorium on co-shipped consumers. Postmortem: docs/postmortem/2026-05-16-rdr110-113-remediation-chain.md."
related_issues: []
related_rdrs: [RDR-004, RDR-041, RDR-077, RDR-078, RDR-079, RDR-087, RDR-092, RDR-105, RDR-106, RDR-107, RDR-108, RDR-112, RDR-113, RDR-120]
related_tests: []
implementation_notes: ""
---

> **TOMBSTONE 2026-05-19.** This RDR is preserved as historical reference. The arc was scrapped due to scope entanglement; see frontmatter `scrap_reason` and the [postmortem](../postmortem/2026-05-16-rdr110-113-remediation-chain.md). Active substrate work: [RDR-120](rdr-120-storage-substrate-split.md). Do not implement against this design.

---


# RDR-110: Semantic Tuple Space: Unified Coordination Primitive over ChromaDB + SQLite

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Nexus has accreted three retrieval surfaces that are structurally the
same abstraction with different names: `plan_match` over the plan
library, T1 scratch over per-session ChromaDB, and T2 memory over
SQLite + FTS. Each has its own dimensions/tags vocabulary, its own
threshold-tuning story, its own match semantics, and its own mental
model. The shared shape is *"post a typed payload that gets indexed
for semantic retrieval; later, query by associative match with
optional structural filters."* That shape has a name in distributed
computing — Linda's tuple space — but nexus has neither the name nor
the unified primitive.

The cost is twofold. First, three parallel implementations means
schema drift, three sets of threshold-tuning bugs (RDR-077, RDR-087,
RDR-092 each addressed the same calibration problem in different
surfaces), and N+1 cognitive load when a developer wants to add a
fourth surface. Second, and more material, **none of the existing
surfaces support atomic destructive read**. Plans are read-only.
Scratch is read-multiple. Memory is read-multiple. The Linda
operation `in` — atomic take — is missing. Without it, agentic
patterns that need coordination (work-stealing pools, inter-agent
mailboxes, mutexes/leases, barriers, request-reply) cannot be
expressed without ad-hoc SQL or external infrastructure.

The fix is structural: name the abstraction, ship it as a primitive,
and treat the existing surfaces as instances. A semantic tuple space
generalises Linda's typed-template matching to typed-structural-filter
plus associative-similarity match, runs over nexus's existing
ChromaDB + SQLite + tier model, and adds atomic destructive read via
a SQLite claim ledger. The three existing surfaces become subspaces
in the same registry. Net result: one primitive, one mental model,
one calibration story, and a coordination layer that didn't previously
exist.

### Enumerated gaps to close

#### Gap 1: No atomic destructive read primitive

Plans, scratch, and memory all expose `query`/`search` operations
that return matches without consuming them. Two consumers querying
the same scratch entry both see it. There is no operation analogous
to Linda's `in` — match, claim atomically, return to exactly one
caller. Agentic patterns that depend on this (work-stealing,
mailboxes, mutexes, barriers) are unbuildable on the current
surfaces without external coordination.

#### Gap 2: Three parallel implementations of the same abstraction

`plan_match` (`src/nexus/plans/match.py`), T1 scratch
(`src/nexus/db/t1.py` + `src/nexus/commands/scratch.py`), and T2
memory (`src/nexus/db/t2/memory_store.py`) each implement: post a
typed payload → embed → store with metadata → query semantically
with metadata filter. Three parallel code paths, three sets of
threshold-tuning bugs, three vocabulary registries (plan dimensions
in YAML; scratch tags as informal RDR-041 vocabulary; memory
collections as freeform strings).

#### Gap 3: Threshold calibration is per-surface, ad-hoc, and inconsistent

RDR-079 P5 set plan-match `min_confidence` floor at 0.40. RDR-077
calibrated topic-projection thresholds against per-collection
distance distributions. RDR-087 surfaced that default `docs`
threshold (0.65) filtered all candidates from PDF collections with
~0.81 distance floors. Each surface re-discovered the same calibration
problem. There is no shared mechanism for declaring "the right floor
for this kind of payload" once and reusing it.

#### Gap 4: Tag/dimension vocabularies drift without a registry

RDR-041 defined a standard scratch tag vocabulary
(`impl`, `checkpoint`, `failed-approach`, `hypothesis`, `discovery`,
`decision`) as SHOULD-not-MUST guidance. In practice, agents use
ad-hoc tags. Plans avoid this because plan dimensions ARE registered
(verb / scope / strategy / object / domain) and validated at seed
time — a violation raises. Scratch and memory have no equivalent
enforcement; the registry concept exists for one surface and not the
others.

#### Gap 5: No coordination layer for cross-process agentic patterns

Work-stealing across N parallel agents, request-reply between agents,
mutex on a shared resource, N-way barriers — none have a primitive in
nexus today. Beads provides a project-scoped task queue but with very
different semantics (manual claim, no semantic match, dependency
graph). Beads is the right answer for human-curated multi-session
work; it is the wrong answer for ephemeral intra-session coordination
between agents that need to discover each other by intent rather than
by ID.

## Context

### Background

Three closely related decisions over the last two months frame this
RDR:

- **RDR-105 (closed 2026-05-08)** restructured T1 around hybrid
  discovery (env-var passdown for MCP-dispatched subprocesses,
  single-writer address file for Claude-Code-dispatched siblings).
  Critically, RDR-105 designated **T2 as "the shared bus across all
  processes" tier** — the multi-process-safe SQLite + WAL backbone.
  Any coordination primitive that needs to serialise writes across
  processes belongs in T2.
- **RDR-108 (accepted 2026-05-08)** ruled that natural IDs must be
  content-derived, not position-derived, and that catalog holds
  normalised identity with foreign-key references rather than
  denormalised name copies. Tuple IDs in this RDR follow that rule:
  `sha256(canonical(subspace + content + dimensions))`. `out`
  becomes naturally idempotent — the same content+dimensions produces
  the same ID, and `INSERT … ON CONFLICT DO NOTHING` collapses
  duplicates.
- **RDR-078 / RDR-079 / RDR-092** built the plan-centric retrieval
  trunk: `match_text` payloads, dimensional plan identity, T1 cosine
  + T2 FTS5 cascade, calibrated `min_confidence` per verb. The plan
  library is already a tuple space in disguise; this RDR names it.

The user-facing observation that triggered this RDR: a design
conversation about persistent addressable surfaces in agentic UIs
(MCP Apps, MCP-UI, OpenAI Apps SDK, A2UI) noted that none of them
have a story for long-lived multi-writer surfaces that survive
agentic boundary crossings. The architectural sketch that fell out
— surfaces as CRDTs / event logs with associative retrieval — is
strictly a special case of a tuple space. The tuple-space primitive
is the natural foundation; surfaces are a Phase 4 application of it.

### Technical Environment

- **T1**: Per-session ChromaDB HTTP server, post-RDR-105 hybrid
  discovery, owned by `nx-mcp` lifespan. Multi-process via HttpClient.
- **T2**: SQLite + WAL at `~/.config/nexus/memory.db` (and adjacent
  DBs). Multi-process safe by storage design. Hosts plans, memory,
  catalog, claim ledger.
- **T3**: ChromaDB Cloud, four-database split per RDR-004
  (`{base}_code`, `{base}_docs`, `{base}_rdr`, `{base}_knowledge`).
  No per-row CAS — `take` is structurally disabled at this tier.
- **Existing dimension registry**: `nx/plans/builtin/*.yml` declares
  plan templates with `(verb, scope, strategy, object, domain)`
  schemas. Validated at seed time. Pattern this RDR generalises.
- **Existing claim-table prior art**: beads' issue claim flow
  (SQLite `claimed_at` + `assignee` columns) is the closest existing
  pattern. Different semantics (manual claim, no lease, no semantic
  match) but the SQLite-as-CAS shape is proven.

## Research Findings

### Investigation

This RDR is the product of a multi-turn design dialogue. The
investigation was structural rather than empirical: enumerate
agentic patterns that the abstraction must serve, validate them
against existing nexus surfaces, identify load-bearing decisions,
push hard on each. Twelve sequential-thinking iterations resolved
the design surface (claim `take` semantics, confidence floors,
schema registry, tier routing).

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| ChromaDB local persistent collection | Yes — `src/nexus/db/t2/*` already uses this pattern | Concurrent reads + serialised writes within a single MCP process; metadata `where` filters are O(log n) on indexed fields; no multi-collection transactions. |
| SQLite WAL CAS | Yes — beads + RDR-105 patterns | `INSERT … ON CONFLICT DO NOTHING RETURNING …` gives single-statement CAS; UNIQUE constraint serialises concurrent claimants correctly. |
| chash-derived natural IDs | Yes — RDR-108 | `sha256(canonical(...))` is the house standard. Stable across re-embedding and rechunking. |
| Tier-cascade read | Yes — `plan_match` already implements T1-cosine → T2-FTS5 fallback (`src/nexus/plans/match.py`) | Pattern is proven; this RDR generalises it across N tiers per subspace. |

### Key Discoveries

- **Verified** — Plans, scratch, and memory share the same
  abstraction with different vocabulary. Refactoring them as
  subspaces is structural renaming, not new logic. (Source:
  `src/nexus/plans/match.py`, `src/nexus/db/t1.py`,
  `src/nexus/db/t2/memory_store.py`.)
- **Verified** — Linda's `in` superpower is **atomicity**, not
  destructive read. Six concrete agentic patterns (work-stealing,
  mailboxes, mutexes, barriers, audit-by-consumption, backpressure)
  collapse without atomic claim. Replacing destructive read with
  read-and-mark-handled preserves the audit trail and matches
  RDR-106 / RDR-107's tombstone house style.
- **Verified** — Confidence floors for destructive reads are a
  *safety* concern, not a quality one. False-positive `take` leaks
  intended-for-someone-else tuples; false-negative `take` returns
  None and the caller polls. Bias must be toward false-negatives.
  Floor + margin (top-1 vs top-2 gap) is the right shape; calibrated
  per subspace, not globally.
- **Verified** — RDR-105 designated T2 as the shared-bus tier. The
  claim ledger therefore lives in T2 (SQLite + WAL), not in T1
  (per-session) or T3 (no CAS). This is non-negotiable.
- **Verified** — RDR-108 mandates content-derived natural IDs as
  a principle. RDR-110 follows the principle but uses its own
  formula: `sha256(canonical(subspace + content + dimensions_json
  + match_text_or_empty))[:32]`. Routing metadata and the
  embedding-text override participate in the hash because they
  distinguish logically distinct tuples. RDR-108 chunk IDs and
  RDR-110 tuple IDs are separate namespaces stored in separate
  chroma collections. `out` is idempotent for the same
  `(subspace, content, dimensions, match_text)` quadruple;
  callers wanting bit-identical duplicates add a nonce dimension.
- **Documented** — JavaSpaces' lease-and-renewal pattern handles
  crash recovery cleanly. Lease + idempotent retry on
  `(tuple_id, claimant)` is the right at-least-once delivery model
  for agents that may crash and replay.
- **Assumed** — Per-template Chroma collection layout (one collection
  per registered subspace template, with concrete subspace string
  in metadata) scales to ~50 templates without operational pain.
  *Status*: Unverified — *Method*: Source Search (existing four-
  database split per RDR-004 already runs ~150 collections in T3
  without incident; this is a smaller fan-out at T2.)
- **Assumed** — Synchronous (non-blocking) `take` is sufficient for
  v1; polling is acceptable for the agentic workloads enumerated.
  *Status*: Unverified — *Method*: Spike (Phase 1 work-stealing
  smoke test will surface whether polling latency is intolerable.)

### Critical Assumptions

- [x] **CA #1: SQLite single-writer lock guarantees `UPDATE …
      RETURNING` atomicity for concurrent claimants on the same
      tuple.** — *Status*: Verified — *Method*: Source Search +
      honker production reference. The original two-statement
      design (`INSERT into tuple_claims` + re-check) had a
      documented race under WAL DEFERRED isolation per Layer 3
      critique. The revised single-statement design (`UPDATE
      tuples SET claim_state='claimed' WHERE … RETURNING` per
      RF-9) is atomic under SQLite's single-writer lock by
      construction: writes serialise globally, and `RETURNING`
      reflects the row state observed by the writer. Honker's
      queue implementation runs this exact pattern in
      production. The Phase 1 stress test below confirms
      behaviour empirically against the deployment harness.
- [ ] **CA #2: The Phase 1 stress test exercises the actual race
      window, not just the happy path.** — *Status*: Unverified —
      *Method*: Spike. Per Layer 3 critique observation O2: a
      naive 10-parallel-worker test will frequently pass even
      with a broken algorithm because WAL serialises commits and
      most real interleavings work. The stress harness must
      inject a controlled sleep between candidate-selection and
      UPDATE to force the race window to manifest, then assert
      exactly-one-winner under that interleaving.
- [x] **CA #3: chash-derived tuple IDs do not collide for distinct
      `(subspace, content, dimensions, match_text)` quadruples
      in practice.** — *Status*: Verified — *Method*: Source
      Search. Same primitive (sha256 truncated to 32 hex chars)
      that RDR-108 ships for chunks with 99.02% coverage and
      zero observed collisions across 290k chunks. The 32-hex
      truncation gives 128 bits of address space; collision
      probability under birthday-paradox math is negligible at
      any realistic tuple population.
- [ ] **CA #4: Per-subspace `take.floor` + `take.margin`
      calibration prevents false-positive destructive reads
      under realistic query paraphrase distributions.** —
      *Status*: Unverified — *Method*: Spike. Phase 1 includes a
      paraphrase-fuzz test against the `mailbox/<agent>` and
      `tasks/<project>` subspaces asserting no false-positive
      takes across 100 paraphrased queries per intended target.
- [ ] **CA #5: Polling-based `take` (default `block=False`) is
      workable for v1 agentic patterns; blocking `take`
      (`block=True`) on the data_version wake mechanism
      delivers ~1-2ms median cross-process wake latency.** —
      *Status*: Unverified — *Method*: Spike. Phase 1
      work-stealing smoke test reports both polling and blocking
      median + tail latency. Honker's published benchmarks claim
      "1-2 ms median on M-series"; spike validates this on the
      deployment hardware.
- [x] **CA #6: `PRAGMA data_version` polling at 1ms cadence has
      negligible CPU cost on the deployment hardware.** —
      *Status*: Verified — *Method*: Source Search. SQLite's
      `PRAGMA data_version` is a single integer read from
      database header memory; cost is bounded by syscall
      overhead. Honker's production deployment confirms
      negligible cost. One thread per `tuples.db` connection;
      worst case ~1000 reads/sec/database with no I/O.

**Renumbering note (post-re-gate)**: prior versions referred to
the same assumptions as CA #1, #1.5, #2, #3, #4, #5. Under the
final numbering, prior CA #1.5 becomes CA #2, prior CA #2-#5
shift to CA #3-#6. Cross-references throughout the document
(Prerequisites, Step 7 spike list, Revision History) use the
current numbering.

### RF-1: Four distinct consumers each need a different "right time, right place"

The user-facing leverage of this RDR depends on the abstraction
being slicker to use than the three parallel surfaces it replaces.
"Slicker" is not one thing — it is four things, one per consumer
class. Each has a different discovery channel and a different
friction profile.

| Consumer | Discovery surface today | Friction shape |
|---|---|---|
| **Agent** (Claude Code session running tools) | MCP tool list + skill registry + `nx/agents/_shared/CONTEXT_PROTOCOL.md` + per-agent prompt + `CLAUDE.md` | Three parallel surfaces (`scratch` / `memory_put` / `store_put`) with overlapping intent — agent must guess which tier suits the finding's lifetime. RDR-041 documented this; tag-vocabulary drift confirms it persists. |
| **Session** (the coordination context itself) | Session-start hook output + agent-loaded T1/T2/T3 banner | Lifetime semantics are implicit. T1 dies on session-end; agent has no warning before it does. T2 survives but agent doesn't know which T2 entries are session-scoped vs project-scoped vs permanent. |
| **User** (human at terminal running `nx`) | `nx --help`, `docs/`, shell completion | Six different subcommands for tuple-shaped operations (`nx scratch put/search/list`, `nx memory put/search/list`, `nx store put/get/list`, `nx plan ...`). User must internalise three taxonomies. |
| **Script** (hooks, CI, automation) | Env vars (`NX_T1_HOST`, `NX_SESSION_ID`, `NEXUS_SKIP_T1`) + CLI invocation | Discovery is fragile — RDR-105's six-bug class came from script-vs-hook coordination on T1 specifically. Scripts have no introspection: a script can't ask "what subspaces exist and what do they accept?" |

**Implication for the design**: each consumer's discovery channel
must explicitly route to the new primitive. Not enough to ship the
API; the discovery surfaces have to recommend it at the right
moment. Skill rewrites, prompt updates, CLI subsumption, and
script-introspection tools are not optional polish — they are the
load-bearing UX of the migration.

**Persisted**: this RF lives in the RDR file; T2 entry deferred
until research synthesis stabilises.

### RF-2: Existing surfaces overlap by intent, not by tier

The naive view is that `scratch` / `memory` / `store` differ by
storage tier (T1 / T2 / T3) and that's the choice point. The
empirical view is that **agents pick by intent** ("is this a
working hypothesis or a permanent finding?") and the tier is
downstream of intent. The current architecture forces agents to
make the tier choice explicit, which is the wrong axis to
foreground.

Examples of the friction:

- An agent forms a hypothesis. Should it go to `scratch` (session
  ephemeral) or `memory_put` (project persistent)? The honest
  answer is "scratch first, promote to memory if it pans out" —
  but no operation expresses that lifecycle. The agent picks one
  and moves on; promotion is a manual `scratch_manage flag` plus
  a separate `memory_put`.
- An agent finds a cross-project pattern. Should it go to `memory`
  (`project=…`) or `store_put` (knowledge T3)? Both work. The
  guidance is split across CONTEXT_PROTOCOL and agent files;
  agents pick by feel.
- An agent wants to coordinate with a sibling agent. **No
  surface answers this.** Scratch is multi-reader but not
  destructive; memory is project-scoped but persists past the
  coordination need; store is permanent. Agents end up using
  scratch with conventional tags (RDR-041's pattern) and reading
  via semantic search — which is exactly an unnamed `read`
  operation against an unnamed subspace, just without the schema
  discipline.

**Implication for the design**: the tuple-space API foregrounds
*intent* (what subspace) over *tier* (where it lives). Tier becomes
a property of the subspace, not a per-call decision. Agents pick
"this is a `scratch/<session>` finding" or "this is a
`memory/<project>` decision"; the tier follows. This is a different
mental model and the migration must explicitly teach it.

### RF-3: CONTEXT_PROTOCOL is the single highest-leverage prompt surface

`nx/agents/_shared/CONTEXT_PROTOCOL.md` is loaded by every
proactive-search and relay-reliant agent and dictates the search
order across T1/T2/T3 (RDR-041's surface). It is the canonical
guidance for "when to use which tier" today. If RDR-110's API
ships and this file isn't rewritten, agents will continue using
the parallel surfaces because the prompt tells them to.

The rewrite is structurally simple: replace the per-tier guidance
with per-subspace guidance, plus a fallthrough rule. Pseudo-form:

```
For working hypotheses, failed approaches, in-flight findings:
  → out(subspace="scratch/<session>", ...)
For project decisions, cross-session context:
  → out(subspace="memory/<project>", ...)
For coordination with sibling agents:
  → out(subspace="mailbox/<agent>", ...)  or
    out(subspace="tasks/<project>", ...)
For permanent knowledge across all projects:
  → defer to /conexus:knowledge-tidy (T3 promotion)
```

The proactive-search-agents-vs-relay-reliant-agents split survives
intact — the search order changes from "T1 first, T2 fallback,
T3 deepest" to "scratch subspace, then memory subspace, then
permanent subspaces" — same shape, named differently, with the
parallel-implementation seam removed.

**Implication for cutover**: CONTEXT_PROTOCOL is rewritten in
Phase 3 (when the wrappers exist), not Phase 1 (when only the new
core subspaces exist). Premature rewrite confuses agents because
the recommended subspaces wouldn't yet have wrappers; Phase 1
agents still use scratch/memory directly.

### RF-4: Skill rewrites — the discovery layer for agent-side intent

Skills are the user-invokable middle layer between MCP tools and
agent prompts. The current shape (per the system reminder
inventory at session start) includes ~80 skills, of which ~15 are
directly tuple-space-shaped:

- Storage: `nx:nexus`, `beads:beads` (catalog of skills exposing
  storage)
- Knowledge: `nx:knowledge-tidy`, `nx:knowledge-tidying`
- Plans: `nx:plan-author`, `nx:plan-validation`, `nx:plan-promote`,
  `nx:plan-inspect`, `nx:plan-first`
- Search/answer: `nx:query`, `nx:research`, `nx:analyze`,
  `nx:review`, `nx:document`, `nx:debug`
- Coordination (mostly absent today — this is the gap): nothing
  for work-stealing, mailboxes, locks, barriers, events.

The rewrite proposal:

1. **Add coordination skills** (Phase 1, alongside the API):
   `nx:tuplespace-tasks`, `nx:tuplespace-mailbox`,
   `nx:tuplespace-lock`, `nx:tuplespace-events`,
   `nx:tuplespace-barriers`. Each is a thin shell pointing the
   agent at the right subspace + canonical patterns. These are
   *new* skills filling a gap, not replacements.
2. **Update existing skills to mention the unified API** (Phase 3):
   `nx:nexus` describes T1/T2/T3 today; rewrite to describe
   subspace-scoped tuple operations with tier as a property.
   `nx:plan-first` and `nx:query` mention `plan_match`; rewrite
   to mention `read(subspace="plans/<verb>")` (behavior unchanged
   under the wrapper, language updated).
3. **Add introspection skills** (Phase 1):
   `nx:tuplespace-list` and `nx:tuplespace-stats` are the
   discovery primitives. Used by both agents (in CONTEXT_PROTOCOL)
   and humans (via `nx tuplespace list`).
4. **Deprecate parallel-vocabulary skills** (Phase 4):
   `nx:knowledge-tidy` becomes a thin wrapper over a `take`
   from `scratch/<session>` followed by `out` to permanent-tier
   subspaces. The skill stays (the *workflow* is real); the
   internals change.

The skill registry is the right rewrite target because skills are
the lowest-friction recommendation surface — an agent invoking
`/conexus:tuplespace-tasks` gets the work-stealing pattern in its
context window with one tool call. No CONTEXT_PROTOCOL paragraph
to read, no MCP tool list to scan.

### RF-5: Cutover precedent from RDR-105 / RDR-094 / RDR-062

Three recent migrations bound the cutover-pattern design space.
This RDR should follow the RDR-105 pattern (it is the most
recent, most relevant — both are agent-facing infrastructure
rewrites under the MCP-server lifecycle), with one structural
adaptation.

**RDR-105 pattern** (T1 hybrid discovery, shipped 4.27.0):
- Five phases over ~1 week elapsed time.
- Single feature flag (`NX_T1_NEW_DISCOVERY=1`) gating the entire
  new path.
- Mutual-exclusion contract: any one MCP server runs entirely
  on flag-on or flag-off; no mixed paths within a process.
- Phase 3 default flip after Phase 2 spike clears.
- Phase 4 deletes the legacy code paths once flag stays default-on.
- Sandbox shakedown: real Claude Code session with multiple
  Agent-tool sub-agents + `claude -p` dispatch + 10-parallel
  stress test before flag removal.
- Net: ~5000 LOC deleted in Phase 4.

**RDR-094 pattern** (MCP-owned T1 lifecycle): four phases, three
spikes (Spike A lifecycle 40-run probe, Spike B subagent race,
Spike C mid-session SIGTERM observation). Critical Assumptions
were spike-gated, not just docs-only.

**RDR-062 pattern** (`nx-mcp` + `nx-mcp-catalog` split): single-
shot tooling refactor; no feature flag because the change was
client-side discovery only. Not the right precedent for RDR-110.

**RDR-105's adaptation for RDR-110**: the feature flag
(`NX_TUPLESPACE_V1=1`?) gates whether the *new core API* is
available, NOT whether the *existing surfaces* use it. The new
API is purely additive in Phase 1 — agents that don't know about
it cannot break. The wrapper migration in Phase 3 needs its own
flag (`NX_TUPLESPACE_WRAPPED_PLANS=1`,
`NX_TUPLESPACE_WRAPPED_SCRATCH=1`,
`NX_TUPLESPACE_WRAPPED_MEMORY=1`) so each surface can be flipped
independently and rolled back independently. RDR-105's "single
flag" model would couple three independent migrations.

**Implication for the implementation plan**: Phase 1 ships
unflagged (additive, no risk to existing callers). Phase 3
introduces three independent flags, one per wrapper. Phase 4
removes flags after all three default-flip cycles complete.

### RF-6: The four-consumer matrix maps to four concrete deliverables

Putting RF-1 / RF-2 / RF-3 / RF-4 together: each consumer class
gets one explicit deliverable that lands the new primitive at its
discovery surface.

| Consumer | Deliverable | Phase | What it lands |
|---|---|---|---|
| **Agent** | CONTEXT_PROTOCOL rewrite + 5 new coordination skills + tuplespace introspection skills | 1 (skills) + 3 (CONTEXT_PROTOCOL) | Subspace-by-intent vocabulary; coordination patterns previously absent. |
| **Session** | Lifetime documentation in MCP tool docstrings + a session-start banner pointing at `nx tuplespace list --tier=ephemeral` | 1 | Explicit "what dies when" semantics surfaced at session start. |
| **User** | `nx tuplespace` CLI subcommand subsuming `out` / `read` / `take` / `list` / `stats` / `schema` | 1 | One verb (`nx tuplespace`) replaces three (`scratch` / `memory` / `store`) for the unified ops. Existing CLIs deprecate in Phase 4. |
| **Script** | Env-var contract (`NX_TUPLESPACE_SUBSPACE`, `NX_TUPLESPACE_CLAIMANT`, etc.) + `nx tuplespace --schema-json` for introspection | 1 | Scripts can introspect available subspaces and post programmatically. |

The session-start banner is the smallest of these but the
highest-leverage for agent UX: every Claude Code session begins
with a banner telling the agent which subspaces exist in this
project and which are ephemeral-vs-persistent. RDR-041's pain
point ("agents don't know what's in scratch from prior sessions")
is exactly the discovery gap this fixes — but generalised to all
subspaces, not just scratch.

### RF-7: Compatibility contract for the v1 → v3 transition

The migration must not break:

- **Existing skill invocations**: agents calling `/conexus:knowledge-tidy`
  or `/conexus:plan-first` continue to work unchanged across all phases.
  Skills are the public contract; their internals can be rewritten
  freely.
- **Existing MCP tool calls**: `scratch`, `memory_put`,
  `memory_search`, `store_put`, `store_get`, `store_list`,
  `plan_save`, `plan_search` keep their signatures and behavior
  through Phase 3. Phase 4 marks them deprecated (warning); Phase
  5 (separate RDR) removes them.
- **CLI subcommands**: `nx scratch put/search/list`,
  `nx memory put/search/list`, `nx store put/get/list` keep
  working through Phase 3. Phase 4 prints deprecation banners.
- **Hook contracts**: `SessionStart` and `SessionEnd` hooks that
  call `scratch` continue to work — the wrapper is at the MCP-tool
  layer, not the CLI layer they use.
- **External-script contracts**: `NX_T1_HOST` / `NX_T1_PORT` /
  `NX_T1_ISOLATED` (RDR-105) keep their semantics; tuple-space
  operations route through the existing T1 server when
  `tier="ephemeral"`, so script-level T1 access stays compatible.

What CAN break, with explicit deprecation:

- **Scratch tag vocabulary** (RDR-041): the `failed-approach` /
  `checkpoint` / `hypothesis` / `discovery` / `decision` informal
  tags become the registered enum on `scratch/<session>`. Agents
  using ad-hoc tags get a `SubspaceSchemaError` warning at Phase 3
  cutover; Phase 4 promotes warning to error. Migration path:
  agents update their prompts to use registered tags or set
  `tags=` to one of the new values.

- **Scratch / memory entry-ID format** (S2 fix). Today's
  `scratch put` returns a chroma-assigned UUID; today's
  `memory_put` returns a `(project, title)` natural key. After
  the wrapper flips (`NX_TUPLESPACE_WRAPPED_SCRATCH=1`,
  `NX_TUPLESPACE_WRAPPED_MEMORY=1`), both return chash-derived
  tuple IDs (`sha256(canonical(...))[:32]` hex strings). Agents
  or scripts that cache returned entry IDs across the flip will
  hold stale-format IDs. **Resolution**: explicit carve-out, not
  a forwarding table. The compatibility contract for these two
  surfaces is "signatures and semantics hold, ID format does not."
  Documented in CHANGELOG; `nx doctor --check-tuplespace-migration`
  (Phase 4 Step 3) flags caller code that pattern-matches against
  the legacy ID format. Agents that don't cache IDs across
  invocations are unaffected. The forwarding-table alternative
  was rejected because (a) it requires persisting a UUID→chash
  mapping that itself violates the compatibility contract on
  external storage shape, and (b) the agentic workloads of
  interest don't cache scratch/memory IDs across sessions in
  practice.
- **Plan-match `min_confidence` floor**: today defaults to 0.40
  (RDR-079 P5). Tuple-space `read.default_floor` for
  `plans/<verb>` matches 0.40 to preserve behavior. No drift.
- **Memory `permanence` field**: today three values
  (permanent / project / session). Becomes a registered enum
  dimension on `memory/<project>`. Same values, validated.

The transition is cooperative: each surface migrates on its own
flag, each can roll back independently, and the public surfaces
(skills, CLIs, tools) hold their contracts through Phase 3.

### RF-9: Honker's `PRAGMA data_version` polling enables blocking `take` without long-poll infrastructure; `UPDATE … RETURNING` resolves the v1-draft CAS race

Honker (https://github.com/russellromney/honker) is a SQLite-only
queue/pub-sub library that solves the same coordination problem
this RDR addresses for queues, with two patterns nexus should
adopt directly:

**Pattern 1 — `PRAGMA data_version` as wake source.** Honker runs
one thread per `Database` polling `PRAGMA data_version` every
~1ms. The counter "increments on every commit in every journal
mode and is visible across processes" — single integer read,
1-2ms median wake latency, no kernel events / file watches /
sockets / signal handlers. CPU cost is one integer read per
millisecond per database.

This eliminates the v1-draft argument for removing `block` from
the `take` API. The original objection ("MCP transport can't
honor long-blocking calls without server-side wait queues plus
cancellation on client disconnect") assumed a per-subspace wait
queue. Honker shows the wait queue is a single shared condition
variable per database that the polling thread fires on commit.
Operationally trivial; bounded timeout (≤30s, well within MCP
request budgets) keeps it within the transport contract.

**Pattern 2 — single-statement claim via `UPDATE … RETURNING`.**
Honker's claim is "one `UPDATE … RETURNING` via a partial
index" gated by `WHERE state='pending'`. Atomic under SQLite's
single-writer lock by construction — no two-step "insert claim
row, then re-check for another claimant" pattern. This is
precisely the C1 fix the gate critic identified. The v1 draft's
"INSERT into `tuple_claims` ON CONFLICT DO NOTHING, then re-check
who's the open claimer" is genuinely broken under WAL DEFERRED
isolation; honker's pattern is genuinely correct and faster
(no join required).

**Pattern 3 — partial index on working set.** Honker's index is
`(queue, priority DESC, run_at, id) WHERE state IN ('pending',
'processing')`. Tombstones don't slow the claim path because
they're excluded from the index. Direct port: our claim index
becomes `(subspace, …) WHERE consumed_at IS NULL AND
(claim_state IS NULL OR claim_expires_at < now)`.

**What this changes in RDR-110's design:**

- `take` re-acquires `block: bool = False, timeout_seconds:
  float | None = None` parameters, with `timeout_seconds`
  capped at 30s (MCP request-budget guard). The implementation
  uses a single `data_version` polling thread per database +
  per-blocking-call `threading.Event` woken by the poller.
- The claim algorithm becomes a single statement of the form
  `UPDATE tuples SET claim_state='claimed', claimant=?,
  claim_id=?, claim_expires_at=? WHERE id = (SELECT id FROM
  tuples WHERE id IN (chroma top-K) AND consumed_at IS NULL
  AND (claim_state IS NULL OR claim_expires_at < ?) ORDER BY
  created_at LIMIT 1) RETURNING id`. The `LIMIT 1` lives inside
  the SELECT (universally supported); the outer UPDATE
  atomically claims that one row. Single SQL statement, no
  INSERT-into-separate-table, no re-check. Atomic by SQLite's
  single-writer lock. (`LIMIT` directly on UPDATE requires
  `SQLITE_ENABLE_UPDATE_DELETE_LIMIT` which is not set in
  CPython's stdlib `sqlite3`; the subquery form is portable.)
- The `tuple_claims` table merges into the `tuples` table as
  state columns (`claim_state` / `claimant` / `claim_id` /
  `claim_expires_at`) plus tombstone columns (`consumed_at` /
  `consumed_by`). Schema simpler; one fewer index.
- **Ack/nack are captured as `tuple_claim_log` transitions, not
  as columns on `tuples`** (N-S2 fix). `ack(claim_id)` sets
  `consumed_at` + `consumed_by` on the tuple AND inserts a
  `transition='ack'` row in `tuple_claim_log`. `nack(claim_id)`
  clears the claim state on the tuple AND inserts a
  `transition='nack'` row. The append-only `tuple_claim_log`
  table preserves claim history for audit (every state
  transition: claim, ack, nack, expire). This is what the
  original `tuple_claims` table was implicitly doing for audit;
  making it append-only-log instead of CAS-target removes the
  race surface from the audit data.

**What does NOT change**: subspace registry, schema validation,
chash IDs, tier model, the eight-function API surface (modulo
`block`/`timeout_seconds` re-introduced on `take`), the four-
consumer landing surface in Phase 1.

**Persisted prior art**: honker's nine-language binding model
(Python, Node, Ruby, Go, Elixir, etc. wrapping a Rust core) is
not relevant for nexus's Python-only context. The interesting
inheritance is the SQLite-internal wake mechanism, not the
binding strategy.

**Confidence**: High on patterns 1 + 2 + 3. The `data_version`
mechanism is a documented SQLite feature with well-understood
cross-process semantics; the `UPDATE … RETURNING` pattern is the
canonical SQLite work-stealing primitive. Both are
production-proven by honker.

### RF-8: One open question — should `take` ship in the v2 wrappers?

The v1 wrappers in Phase 3 are behavior-preserving: scratch's
`promote` flow becomes `take` from `scratch/<session>` followed
by `out` to `memory/<project>`, but only because the underlying
semantics already exist. For plans (read-only) and memory
(no destructive read in current API), `take` is *not* available
through the wrappers in v2 — even though the underlying tuple
space supports it.

This is conservative but possibly leaves leverage on the floor.
Two options:

**A. Conservative (recommended for v2)**: wrappers expose only
the existing surface area. New consumers wanting `take` against
plans or memory must use the raw tuple-space API
(`take(subspace="memory/<project>", ...)`). Net: zero risk to
existing callers.

**B. Aggressive**: extend memory's surface to include `take`
(e.g. as a new `memory_take(...)` tool). Lets existing memory
consumers adopt coordination patterns without learning the
tuple-space vocabulary. Net: small surface-area growth,
small adoption-friction reduction.

Recommend (A) for v2 → v3. (B) is a follow-up RDR if a real
consumer asks for `memory_take`.

## Proposed Solution

### Approach

Ship a semantic tuple space as a v1 primitive with three core
operations (`out` / `read` / `take`), two claim-lifecycle operations
(`ack` / `nack`), and three discovery operations (`list_subspaces` /
`subspace_schema` / `subspace_stats`). Total surface: 8 functions.
Tuples live in tier-appropriate backends (T1 / T2 / T3); the claim
ledger lives in T2 SQLite. Subspaces are registered via static YAML
in `nx/tuplespace/builtin/*.yml`, validated at MCP startup, and
discovered at runtime via `list_subspaces`. Existing surfaces (plans,
scratch, memory) are NOT migrated in v1; v2 wraps them as subspaces
behavior-preservingly.

### Technical Design

**Tuple shape:**

```text
Tuple(
    id: str                        # sha256(canonical(subspace + content + dimensions_json + match_text_or_empty))[:32]
    subspace: str                  # concrete, e.g. "tasks/nexus"
    template_name: str             # registered, e.g. "tasks/<project>"
    content: str                   # body
    dimensions: dict[str, str]     # validated against subspace schema
    embed_text: str                # what was embedded; resolved per schema.embed_from
    match_text: str | None         # optional override for embedding source; participates in id
    tier: Literal["ephemeral", "project", "permanent"]
    created_at: float
    expires_at: float | None
    consumed_at: float | None      # tombstone; NULL = available
    consumed_by: str | None        # claimant who acked
)
```

**Tuple ID formula (C3 fix):** the canonical-bytes input to
`sha256` includes `match_text` when non-None. Two `out` calls
with identical `(subspace, content, dimensions)` but different
`match_text` produce different IDs and are stored as distinct
tuples — this is correct because their retrieval embeddings
differ. Empty/None `match_text` is normalised to the empty
string before hashing so callers can omit the parameter without
ID drift. Callers wanting bit-identical idempotency must
ensure `match_text` is stable across re-submissions.

**Core API (illustrative — verify signatures during implementation):**

```text
out(*, subspace, content, dimensions=None, match_text=None,
    tier=None, ttl_seconds=None) -> tuple_id
read(*, subspace, query, where=None, n=1, min_confidence=None,
     tier=None) -> list[Tuple]
take(*, subspace, query, claimant, where=None, min_confidence=None,
     lease_seconds=None,
     block=False, timeout_seconds=None) -> (Tuple, claim_id) | None
ack(claim_id) -> None              # commits the take
nack(claim_id) -> None             # releases the claim
list_subspaces() -> list[SubspaceSchema]
subspace_schema(name) -> SubspaceSchema
subspace_stats(name) -> SubspaceStats
```

`block=True` causes `take` to wait on a `PRAGMA data_version`
counter (per RF-9) until either a candidate appears or
`timeout_seconds` elapses. `timeout_seconds` is capped at 30s by
the MCP transport budget; values above the cap raise
`InvalidTimeoutError`. The default `block=False` returns
immediately with `None` if no candidate clears floor + margin.

**Subspace schema (YAML, registered statically):**

Each `nx/tuplespace/builtin/*.yml` declares a template:

```yaml
name: tasks/<project>              # single-segment param
tier: project                      # default; out can override
content_type: text                 # text | json
embed_from: content                # content | match_text | dimensions:<key>
dimensions:
  status:    { type: enum, values: [open, in_progress, done, cancelled], required: true }
  priority:  { type: enum, values: [P0, P1, P2, P3, P4], required: true }
  assignee:  { type: string, required: false }
  created_by:{ type: string, required: true }
take:
  enabled: true
  mode: semantic                   # semantic (default) | exact
  floor: 0.55                      # absolute cosine floor (1 - distance) — semantic mode only
  margin: 0.08                     # required gap between top-1 and top-2 — semantic mode only
  match_keys: [...]                # required for exact mode — see locks subspace below
  default_lease_seconds: 600
read:
  default_floor: 0.40
  default_n: 5
tiers: [project]                   # cascade order; each subspace declares
retention_seconds: 7776000         # 90 days; tombstone GC after this
```

**`take.mode` (C2 fix):** declares whether the candidate-selection
step uses semantic similarity (`semantic`, default) or exact
match on a declared dimension key set (`exact`). Mode `exact`
bypasses the chroma query entirely and selects candidates via
SQL `WHERE subspace = ? AND <dimension equality conditions>`.
Floor and margin do not apply under `exact`; the take is
deterministic on the dimension keys. The schema's `match_keys`
array names which dimensions form the exact-match key (typically
a single key for lock-style subspaces).

Lock subspaces (mutex / lease patterns) use `mode: exact`
because semantic match cannot guarantee exclusion — a query
semantically distant from the lock's content could falsely
return `None` ("lock held"), or a query semantically close to
two distinct lock resources could match the wrong one. Linda's
`in` over a typed-template-with-exact-match is the correct
primitive here, not Linda's-with-similarity.

Validation rules:
- At `out`: dimension keys must be in the registered set; required
  dimensions must be present; enum values validated; type-conformance
  enforced. Breach raises `SubspaceSchemaError` before any write.
- At `read`/`take`: `where` keys must be registered dimensions;
  enum-typed where-values must be registered. Unknown subspace raises
  `UnknownSubspaceError`.
- Schema evolution: additive only in v1. Adding dimensions or enum
  values is forward-compatible. Removing or renaming requires a
  migration RDR.

**Five canonical v1 subspaces** (`tasks`, `mailbox`, `locks`,
`events`, `barriers`) ship with the registry. Three v2 wrappers
(`plans`, `scratch`, `memory`) are sketched in the registry but not
wired until Phase 3.

**SQL schema (T2; new database `~/.config/nexus/tuples.db` to keep
operational separation):**

The schema was revised post-Layer-3 critique to absorb honker's
single-table-with-state-column pattern (RF-9). The original
two-table design (`tuples` + `tuple_claims`) had a CAS race
under WAL DEFERRED isolation between two distinct claimants.
The single-table pattern with `UPDATE … RETURNING` is atomic
under SQLite's single-writer lock by construction. Claim
history moves to an append-only `tuple_claim_log` for audit.

```text
-- Body store + claim state; the source of truth.
-- Chroma is a derived index over (id, embed_text, dimensions_json) per RDR-108.
CREATE TABLE tuples (
    id              TEXT PRIMARY KEY,
    subspace        TEXT NOT NULL,
    template_name   TEXT NOT NULL,
    content         TEXT NOT NULL,
    dimensions_json TEXT NOT NULL,
    embed_text      TEXT NOT NULL,
    created_at      REAL NOT NULL,
    expires_at      REAL,                           -- TTL; NULL = no expiry
    -- Claim state (formerly tuple_claims). Atomic via UPDATE … RETURNING.
    claim_state     TEXT,                           -- NULL = available; 'claimed' = in-flight
    claimant        TEXT,                           -- set with claim_state
    claim_id        TEXT,                           -- the value returned by take()
    claim_expires_at REAL,                          -- lease expiry; NULL when claim_state IS NULL
    -- Tombstone state (RDR-106 / RDR-107 style).
    consumed_at     REAL,                           -- NULL = available
    consumed_by     TEXT
);
-- Working-set partial index per honker's pattern (RF-9).
-- Tombstones don't slow the claim path because they're excluded.
CREATE INDEX idx_tuples_avail
    ON tuples (subspace, expires_at) 
    WHERE consumed_at IS NULL 
      AND (claim_state IS NULL OR claim_expires_at < unixepoch());
CREATE INDEX idx_tuples_claimed
    ON tuples (claim_id) WHERE claim_state = 'claimed';
CREATE INDEX idx_tuples_expires
    ON tuples (expires_at)  WHERE expires_at IS NOT NULL AND consumed_at IS NULL;

-- Append-only claim history for audit. Insert-only; never updated.
-- Records every state transition (claim, ack, nack, expiry-release).
CREATE TABLE tuple_claim_log (
    log_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tuple_id        TEXT NOT NULL,
    claim_id        TEXT NOT NULL,
    claimant        TEXT NOT NULL,
    transition      TEXT NOT NULL,                  -- 'claim' | 'ack' | 'nack' | 'expire'
    at              REAL NOT NULL
);
CREATE INDEX idx_claim_log_tuple ON tuple_claim_log (tuple_id, at);
CREATE INDEX idx_claim_log_claimant ON tuple_claim_log (claimant, at);
```

The `tuple_claim_log` table is purely informational — sweeps and
queries do not consult it. Audit consumers (`subspace_stats`,
`nx tuplespace stats`, post-mortem queries) read from it but
never mutate it. Its growth is bounded by retention policy
(separate from tuple retention; default 30 days).

**Chroma collection layout (project tier):**

One persistent local collection per template, named
`tuples__<template_slug>` where the slug replaces `/` with `_` and
strips `<>` from parameterised segments. Concrete subspace strings
live in chroma metadata, alongside the validated dimensions. A
`read(subspace="tasks/nexus", where={status:"open"})` becomes a
chroma `query` against `tuples__tasks` with metadata filter
`{$and: [{subspace: "tasks/nexus"}, {status: "open"}]}` followed by
a SQL post-filter that drops tuples with `consumed_at IS NOT NULL`
or with an open claim row.

**`take` algorithm (single-statement CAS via `UPDATE … RETURNING`):**

The algorithm uses honker's single-statement claim pattern (RF-9).
Atomicity is structural: `UPDATE … WHERE state-is-available …
RETURNING` runs under SQLite's single-writer lock, so two
concurrent claimants cannot both succeed on the same row. No
re-check is needed.

```text
# Illustrative — verify against implementation.
def take(*, subspace, query, claimant, where=None,
         min_confidence=None, lease_seconds=None,
         block=False, timeout_seconds=None):
    schema = registry.get_schema_for(subspace)         # raises UnknownSubspaceError
    if not schema.take.enabled: raise TakeDisabledError(subspace)
    schema.validate_where(where or {})
    if timeout_seconds and timeout_seconds > 30:
        raise InvalidTimeoutError("timeout_seconds capped at 30 by MCP transport budget")

    floor   = min_confidence or schema.take.floor
    margin  = schema.take.margin
    lease_s = lease_seconds  or schema.take.default_lease_seconds

    deadline = time.time() + (timeout_seconds or 0) if block else None
    mode = schema.take.mode  # 'semantic' (default) or 'exact' (C2 fix)

    while True:
        if mode == 'exact':
            # (C2 fix) Bypass chroma. Candidate selection is pure SQL on
            # match_keys. Floor and margin do not apply.
            #
            # Column names (`schema.match_keys`) are registered at
            # schema-load time, never caller-supplied, so interpolating
            # them into the SQL fragment is safe. Values are bound via
            # placeholders. No `exact_match_sql` helper — the pattern is
            # explicit here so the parameterisation is auditable in one
            # place. Validation: every match_key must be present and
            # non-empty in `where`; missing keys raise SubspaceSchemaError.
            for k in schema.match_keys:
                if k not in (where or {}):
                    raise SubspaceSchemaError(f"missing required match_key: {k}")
            match_clause = " AND ".join(f"{k} = ?" for k in schema.match_keys)
            match_values = tuple(where[k] for k in schema.match_keys)
            top_ids = [r["id"] for r in db.execute(f"""
                SELECT id FROM tuples
                WHERE subspace = ?
                  AND consumed_at IS NULL
                  AND (claim_state IS NULL OR claim_expires_at < ?)
                  AND {match_clause}
                ORDER BY created_at
                LIMIT 1
            """, (subspace, time.time(), *match_values)).fetchall()]
        else:
            # Semantic mode (default). chroma → floor → margin → top-K IDs.
            candidates = chroma_query(schema.collection, subspace, query,
                                      where, n=max(5, schema.read.default_n))
            top_ids = []
            if candidates and (1 - candidates[0].distance) >= floor:
                margin_ok = (
                    len(candidates) == 1
                    or (candidates[1].distance - candidates[0].distance) >= margin
                )
                if margin_ok:
                    top_ids = [c.tuple_id for c in candidates
                               if (1 - c.distance) >= floor]

        if top_ids:
            # Single-statement CAS over candidate IDs.
            # Atomic by SQLite's single-writer lock; no re-check needed.
            # Portable form: `LIMIT 1` lives in the inner SELECT (universally
            # supported), the outer UPDATE atomically claims that one row.
            # `LIMIT 1` directly in UPDATE requires SQLITE_ENABLE_UPDATE_DELETE_LIMIT
            # which is not set in CPython's stdlib sqlite3 build.
            now = time.time(); expires = now + lease_s
            claim_id = chash_id(claimant, top_ids[0], now)
            placeholders = ",".join("?" * len(top_ids))
            row = db.execute(f"""
                UPDATE tuples
                SET claim_state='claimed',
                    claimant=?, claim_id=?, claim_expires_at=?
                WHERE id = (
                    SELECT id FROM tuples
                    WHERE id IN ({placeholders})
                      AND consumed_at IS NULL
                      AND (claim_state IS NULL OR claim_expires_at < ?)
                    ORDER BY created_at
                    LIMIT 1
                )
                RETURNING id
            """, (claimant, claim_id, expires, *top_ids, now)).fetchone()
            if row:
                db.execute(
                    "INSERT INTO tuple_claim_log "
                    "(tuple_id, claim_id, claimant, transition, at) "
                    "VALUES (?, ?, ?, 'claim', ?)",
                    (row["id"], claim_id, claimant, now))
                return load_tuple(row["id"]), claim_id
            # No row matched — candidates raced. Loop or return.

        # No candidate cleared the gates. Block or return.
        if not block: return None
        remaining = deadline - time.time()
        if remaining <= 0: return None
        # Wait on data_version counter (per RF-9). Wakes on any commit
        # to tuples.db; we re-attempt the candidate scan on each wake.
        wake_event.wait(timeout=min(remaining, 1.0))
```

**Idempotent retake by same claimant**: handled by the
`(claim_state IS NULL OR claim_expires_at < ?)` guard in the
UPDATE. If the same claimant retakes within their lease, the
guard fails (claim_state='claimed' AND claim_expires_at > now)
and the UPDATE returns no row. Caller treats this as "I still
hold the claim from before"; lookup via `claim_id` recovers the
existing claim. Implementation detail: same-claimant retake is a
two-statement read-then-update because the simpler pattern
above conflates same-claimant retake with foreign-claimant
contention. Documented; not in the pseudocode for clarity.

**`data_version` polling thread** (`NX_STORAGE_MODE=direct` only):
a single `_TupleSpaceWatcher` thread per `tuples.db` connection
runs `PRAGMA data_version` every 1ms, fires `wake_event` on
increment. Started at MCP lifespan start (per RDR-094), stopped
at lifespan finally. CPU cost: one integer read per millisecond
per database. All blocking `take` calls share the same wake_event;
spurious wakes (commits not relevant to this caller's subspace)
cost one extra UPDATE attempt that returns no row.

**Under `NX_STORAGE_MODE=daemon` (RDR-112)**, the watcher is
*daemon-internal* — the daemon owns the single `tuples.db`
connection (one process, one lock, one `data_version`) and exposes
client-side waiting via the **blocking-take RPC** and the
**`EventStream(subspace_prefix, since_cursor) → Stream<Event>`
RPC** (RDR-112 Approach §7). Clients never hold a `tuples.db`
connection in daemon mode; doing so would fork the WAL (RDR-112
§A2) and is forbidden by the `nx doctor --check-storage-boundary`
lint (RDR-112 §5). The in-process watcher described above is the
direct-mode path only.

**Tier routing:**

| Operation | ephemeral (T1) | project (T2) | permanent (T3) |
|---|---|---|---|
| Body store | T1 chroma collection metadata | `tuples` SQL table | T3 collection metadata |
| Vector index | T1 chroma per-subspace collection | T2 chroma persistent local | T3 cloud collection |
| Claim ledger | In-memory dict per MCP process | Claim state on `tuples` row + append-only `tuple_claim_log` | N/A — `take` raises |
| `take` enabled | yes | yes | no (registry-load-time check) |

Cross-tier `read` cascades in registry-declared order, dedups by
chash ID, applies the highest tier's floor across results.

**Compaction sweeps** (piggyback on existing periodic GC in
`mcp/core.py`). Schema revised post-RF-9 to single-table state.

```text
sweep_expired_claims():   # release stale claims (lease expired without ack/nack)
    db.execute("""
        UPDATE tuples
        SET claim_state = NULL, claimant = NULL,
            claim_id = NULL, claim_expires_at = NULL
        WHERE claim_state = 'claimed' AND claim_expires_at < unixepoch()
        RETURNING id, claim_id, claimant
    """)
    # log each transition to tuple_claim_log with transition='expire'

sweep_tombstones():       # hard-delete consumed tuples past retention
    for each schema with retention_seconds:
        ids = SELECT id FROM tuples
              WHERE template_name = schema.name
                AND consumed_at IS NOT NULL
                AND consumed_at < unixepoch() - schema.retention_seconds
        chroma.delete(ids); DELETE FROM tuples WHERE id IN ids

sweep_claim_log():        # bound the audit log size
    DELETE FROM tuple_claim_log WHERE at < unixepoch() - 30*24*3600
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Subspace registry (YAML loader, schema validation) | `nx/plans/builtin/*.yml` + plan dimension registry | **Reuse pattern, new registry module.** Plans' registry is plan-specific; tuple-space registry generalises it but does not subsume it in v1. |
| Body store (T2 SQLite tables) | `~/.config/nexus/memory.db` | **New database `tuples.db`.** Operational separation per RDR-004 logic; tuples are a different access pattern from memory. |
| Vector index (per-template chroma collection) | RDR-004 four-database split for T3 | **Reuse pattern.** Per-template chroma collection at T2 (local persistent) and T3 (cloud, `take`-disabled). |
| Claim ledger CAS | beads SQLite claim flow + RDR-105's claims approach + honker `UPDATE … RETURNING` pattern | **Claim state columns on `tuples` + append-only `tuple_claim_log` for audit.** Beads' claim flow is human-curated, no lease, no semantic match — wrong fit. SQLite WAL + honker's single-statement claim is the load-bearing pattern; see RF-9. |
| Tier cascade for `read` | `src/nexus/plans/match.py` (T1 cosine → T2 FTS5) | **Generalise.** Pattern proven; lift to a tier-cascade helper used by `read` for any subspace declaring `tiers: [...]`. |
| chash natural IDs | RDR-108 chunk natural-ID scheme | **Follows the principle, not the formula** (S3 fix). RDR-108 hashes raw content bytes only (`sha256(chunk_text_bytes)`) for chunks. RDR-110 hashes content + routing metadata + match-text override (`sha256(canonical(subspace + content + dimensions_json + match_text_or_empty))[:32]`) because tuple identity must include subspace and dimensions to distinguish logically distinct tuples. The two are distinct ID namespaces — RDR-108 chunk IDs and RDR-110 tuple IDs do not collide because they live in different chroma collections (`code__*` / `docs__*` / `rdr__*` / `knowledge__*` for chunks; `tuples__*` for tuples). Cross-tier `read` dedup operates within the RDR-110 tuple-ID namespace only; chunk and tuple namespaces never merge. |
| Tombstone soft-delete | RDR-106 + RDR-107 | **Reuse pattern.** `consumed_at` + `consumed_by` columns; periodic compaction sweep. |
| Periodic GC sweeps | `src/nexus/mcp/core.py` orphan-reaper + tmpdir sweep | **Extend.** Add `sweep_expired_claims` and `sweep_tombstones` to the same loop. |
| MCP tool surface | `src/nexus/mcp/core.py` FastMCP tool registry | **Extend.** Eight new tools for the v1 surface; future migration of plans/scratch/memory subsumes their existing tools. |

### Decision Rationale

The shape of the abstraction is structural — three existing surfaces
already implement it informally. Naming it explicitly and shipping a
single primitive does three things at once:

1. **Eliminates parallel-implementation drift.** v2 wraps plans /
   scratch / memory as subspaces; v3 deletes the parallel APIs. Same
   pattern RDR-105 used to consolidate T1's discovery layer.
2. **Adds a coordination primitive (`take`) that didn't exist.** Six
   agentic patterns become buildable on the existing tier model with
   no new infrastructure beyond a SQLite claim table.
3. **Provides a foundation for persistent addressable surfaces.**
   The original design conversation (multi-writer agentic UI surfaces
   surviving boundary crossings) reduces to a Phase 4 application of
   this primitive: a surface is a tuple subspace; the renderer is a
   `read`-based projection over the tuple stream. No new mechanism
   needed beyond what this RDR ships.

The decision to ship `take` in v1 (rather than `out`/`read` only) is
deliberate: without atomic destructive read, the abstraction degrades
to "another `query` API" with no leverage over what already exists.
With `take`, the leverage is the six concrete patterns enumerated
above, each unblockable today.

The decision to register subspaces statically (YAML in code, not
runtime) follows from RDR-041's lesson: SHOULD-not-MUST vocabularies
drift. Static registration catches schema breach at MCP startup
rather than at first-write, which keeps "what does this subspace
mean" answerable from `git log` alone.

## Alternatives Considered

### Alternative 1: Status quo — keep plans, scratch, memory as parallel implementations

**Description**: Continue maintaining three independent retrieval
surfaces. Add a fourth surface (e.g. inter-agent mailboxes) as
another parallel implementation when a real consumer asks.

**Pros**:
- Zero migration cost, zero risk to existing surfaces.
- Each surface stays optimised for its specific use case.

**Cons**:
- Three threshold-tuning stories continue diverging (RDR-077 / RDR-087
  / RDR-092 keep re-discovering the same calibration problem).
- Agentic coordination patterns (work-stealing, mailboxes, mutexes)
  remain unbuildable without external infrastructure.
- The persistent-addressable-surface design problem stays unsolved
  because there's no underlying primitive.

**Reason for rejection**: The cost of NOT having a unified primitive
compounds. Each new surface re-pays the threshold-tuning tax and adds
a fourth vocabulary to keep coherent. The coordination gap is real
and growing as agentic patterns mature.

### Alternative 2: Build coordination as a separate primitive (job queue / pub-sub)

**Description**: Ship a Linda-free job queue (or Redis-style pub-sub
layer) for coordination, leave plans/scratch/memory untouched.

**Pros**:
- Clean separation of concerns: retrieval is one thing, coordination
  is another.
- Can use specialised infrastructure (Redis, RabbitMQ) tuned for
  queue semantics.

**Cons**:
- Doesn't reuse semantic match — work-stealing on natural-language
  task descriptions wants the same vector index plans/scratch already
  have.
- Adds an external dependency (Redis or equivalent) for what should
  be a SQLite + chroma feature.
- Mailboxes / request-reply benefit from semantic match too: "give
  me the response to my last auth-related question" is a vector
  query, not a queue pop.

**Reason for rejection**: The whole leverage of the proposal is that
semantic match composes with coordination. Splitting them duplicates
infrastructure and loses the composition.

### Alternative 3: Ship `out`/`read` only in v1, defer `take`

**Description**: v1 is append-only with semantic retrieval. `take`
ships in v2 once a real consumer needs it.

**Pros**:
- Smaller v1 surface, less to verify.
- No claim-table design needed yet.

**Cons**:
- The leverage from "leveraging the heck out of this" disappears.
  v1 becomes "another `query` API" with no agentic-coordination
  payoff.
- `take`'s atomicity story is the design crux; deferring it means
  v2 has to re-design half the schema (claim table, idempotent
  retake, lease semantics, confidence floors as safety knobs vs
  quality knobs).

**Reason for rejection**: The user explicitly opted in to `take` in
v1 because the coordination patterns are the point. Shipping without
it produces a surface that solves Gap 2 but not Gap 1 or Gap 5.

### Briefly Rejected

- **Runtime subspace registration via a `subspaces` meta-subspace**:
  introduces chicken-and-egg (validating the `subspaces` registration
  needs the `subspaces` schema). Defer to v2 once a real use case
  surfaces. Static YAML is what plans already do and works.
- **Multi-valued dimensions in v1**: chroma metadata is single-valued.
  Multi-value would need a normalised side-table or JSON-array
  convention. The v2 scratch wrapper needs it; v1 core subspaces
  don't. Defer.
- **`block=True` synchronous waiting on `take`** *(initially
  rejected; superseded by RF-9)*: the v1-draft rejected `block`
  on the basis that "MCP transport can't honour long-blocking
  calls without server-side wait queues plus cancellation on
  client disconnect." RF-9's honker absorption reversed this:
  `PRAGMA data_version` polling provides cross-process wake at
  1-2ms median latency via a single shared condition variable
  per database (one watcher thread per `tuples.db`). Bounded
  timeout (≤30s) keeps the call within MCP request budgets.
  `block=True` ships in v1; the rejection rationale is preserved
  here as the historical alternative considered.
- **Hard-delete consumed tuples instead of tombstoning**: tombstone
  matches RDR-106 / RDR-107 house style; the audit trail (who
  consumed what, when) comes free.

## Trade-offs

### Consequences

- **Positive**: One mental model for plans/scratch/memory plus the
  five new coordination subspaces. Eight tools instead of three
  per-surface tool sets in v2/v3.
- **Positive**: `take` unblocks six concrete agentic patterns
  (work-stealing, mailboxes, mutexes, barriers, audit-by-consumption,
  backpressure) with no new infrastructure.
- **Positive**: Persistent addressable surfaces become a Phase 4
  application of this primitive rather than a separate subsystem.
- **Positive**: chash-derived IDs make `out` idempotent for free
  (same content+dimensions = same ID).
- **Negative**: New SQLite database + N new chroma collections (one
  per registered template) at MCP startup. Operational footprint
  grows.
- **Negative**: Per-subspace threshold calibration is a real cost.
  Each new subspace requires picking floor + margin against a
  representative paraphrase distribution.
- **Negative**: v2 migration of plans/scratch/memory is mechanical
  but touches multiple surfaces; behavior-preservation requires care.
- **Negative**: Agents must learn a new surface alongside the
  existing ones during the v1 → v3 transition.

### Risks and Mitigations

- **Risk**: Confidence-floor calibration is wrong for some subspace,
  causing systematic false-positive takes that leak intended-for-X
  tuples to Y.
  **Mitigation**: Critical Assumption #3 spike with paraphrase fuzz.
  Default schema floor + margin are conservative (0.55 / 0.08) for
  generic subspaces; lock-style subspaces use ~0.95 / 0.10 for
  near-exact match. `nx doctor --check-tuplespace` proposed (Phase 1)
  that scores per-subspace claim distributions and flags drift.
- **Risk**: Claim-table contention under heavy concurrent take load
  produces SQLite lock errors.
  **Mitigation**: SQLite WAL handles writer serialisation correctly
  and the lock window is microseconds (one CAS insert + one read).
  Phase 1 stress test (10-parallel-worker race) verifies. If
  contention surfaces, batched-claim or per-subspace claim-DB
  partitioning are escape hatches.
- **Risk**: Tombstone GC sweeps run too rarely → table bloat; or too
  often → wasted churn.
  **Mitigation**: Tombstone count threshold (e.g. >10k consumed-but-
  unswept) plus age threshold (e.g. age > min(retention_seconds /
  10, 1 hour)) trigger sweep. Reuse `mcp/core.py` periodic-GC pattern
  from RDR-105.
- **Risk**: Per-template chroma collection fan-out grows unbounded
  as new subspaces ship.
  **Mitigation**: Subspaces are statically registered; growth is
  proportional to RDR cadence. RDR-004 already runs ~150 collections
  in T3 without operational pain. Re-evaluate at >50 templates.
- **Risk**: Subspace schema breaches at `out` time block legitimate
  work because the schema is too strict.
  **Mitigation**: Additive-only evolution rule means schemas can
  always be widened without a migration. `nx doctor` reports recent
  `SubspaceSchemaError`s so over-strict schemas surface fast.

### Failure Modes

- **Visible**: `out` rejects a payload with `SubspaceSchemaError`
  before any write. Caller sees the field/reason; no tuple is
  created. Standard.
- **Visible**: `take` returns `None` when no candidate clears
  floor+margin. Caller polls or surfaces "no work available."
- **Visible**: Periodic sweep logs `claims_expired` /
  `tombstones_swept` counters via structlog.
- **Silent (resolved in design)**: Crash-mid-take leaves a claim row
  with no ack/nack. Lease expiry releases it; idempotent retake by
  same claimant returns the same tuple on replay. At-least-once
  delivery; explicit in the `take` contract.
- **Silent (resolved in design)**: Two claimants race for the same
  tuple. UNIQUE constraint on `(tuple_id, claimant)` plus the
  open-claim re-check inside the SQL transaction ensures exactly
  one wins; the loser falls through to the next candidate.
- **Silent (open until spike)**: False-positive take on an ambiguous
  match. Margin gate guards against this but the calibration of
  floor + margin is per-subspace and depends on the paraphrase
  distribution. Mitigation: spike (CA #4); per-subspace tuning at
  Phase 1.

## Implementation Plan

### Prerequisites

- [ ] All Critical Assumptions verified. CA #1, #3, #6 are
      Verified (source-search). CA #2, #4, #5 are spike-gated
      and bundled into Phase 1 Step 7 — Steps 8-12 of Phase 1
      cannot ship before these clear.
- [ ] RDR-108 Phase 2 (chunk natural-ID migration) does not need to
      complete before this RDR ships; the `chash_id` primitive is
      already available.
- [ ] No outstanding RDR-105 fixes in flight that would conflict
      with new T2 SQL schema additions.

### Minimum Viable Validation

A 10-parallel-worker work-stealing harness pulls tasks from a
populated `tasks/<project>` subspace via `take`. After completion:

- Every task is `consumed_by` exactly one worker (no double-claim).
- No task remains available unless all workers have failed it (no
  starvation).
- Tombstone count after sweep matches consumed task count.
- Median `take` latency under 100ms; tail p99 under 500ms.

### Phase 1: Core API, project tier, and four-consumer landing surface

Phase 1 is intentionally large. Per RF-1 / RF-6 the four consumer
classes (agent / session / user / script) each need an explicit
deliverable that lands the new primitive at its discovery surface;
shipping the API without the discovery surfaces would leave the
slickness gap unfilled. Phase 1 is additive only — no existing
caller can break because no existing surface changes.

#### Step 1: Schema registry module

Create `src/nexus/tuplespace/registry.py` that loads
`nx/tuplespace/builtin/*.yml` at MCP startup, validates each schema
against a JSON Schema for the registry format itself, exposes
`get_schema_for(subspace)` with parameterised-name matching.

#### Step 2: Body store + claim ledger

Create `src/nexus/tuplespace/store.py` with the `tuples` and
`tuple_claim_log` tables in a new `~/.config/nexus/tuples.db`. Add
SQLite migration to create on first MCP startup post-upgrade.

#### Step 3: Chroma collection layout (project tier)

One persistent local chroma collection per registered template. Add
`src/nexus/tuplespace/index.py` with thin wrappers for
`out`/`read` against (collection, metadata) — caller doesn't see
chroma directly.

#### Step 4: Core API + `data_version` watcher

Implement `out`/`read`/`take`/`ack`/`nack`/`list_subspaces`/
`subspace_schema`/`subspace_stats` in `src/nexus/tuplespace/api.py`.
Wire into `nx-mcp` as eight new MCP tools. `take` accepts
`block`/`timeout_seconds` per RF-9.

In the same step, add `src/nexus/tuplespace/watcher.py` —
a `_TupleSpaceWatcher` thread per `tuples.db` connection that
polls `PRAGMA data_version` every 1ms and fires a shared
`threading.Event` (the `wake_event` referenced in the `take`
algorithm) on any increment. Started at MCP lifespan start
(per RDR-094 / RDR-105 lifecycle pattern), stopped at lifespan
finally. Honker's production cadence is the reference (1-2ms
median wake latency).

**Mode split — daemon vs direct (RDR-112)**: the watcher described
above is the **`NX_STORAGE_MODE=direct`** path only (same-host,
single-filesystem deployments). Under
**`NX_STORAGE_MODE=daemon`**, the MCP server is a client of the T2
daemon (RDR-112 D1) and does **not** hold a `tuples.db`
connection — the watcher runs daemon-side, owned by the single
process that owns the SQLite file. Client-side `block=True`
calls into `take` route through the daemon's **blocking-take
RPC**; client-side observers (RDR-111's `_BindingWatcher`,
cockpit panels) subscribe via the daemon's
**`EventStream(subspace_prefix, since_cursor)`** RPC (RDR-112
Approach §7). Holding a client-side `tuples.db` connection
under daemon mode is forbidden by the
`nx doctor --check-storage-boundary` lint (RDR-112 §5) and would
fork the WAL across the overlayfs boundary (RDR-112 §A2).
`block=True` ships feature-flagged in Step 4 and is enabled when
daemon mode is the default, per the sequencing constraint in
the Revision-History "Further-revised scope (2026-05-13)" block.

#### Step 5: Periodic sweeps

Add `sweep_expired_claims`, `sweep_tombstones`, and
`sweep_claim_log` to the existing `mcp/core.py` periodic-GC loop.
Trigger thresholds: claim sweep on 30s cadence; tombstone sweep
on count > 10k or age > min(retention/10, 1h); claim-log sweep
nightly. The expired-claims sweep is the recovery path for
crashed claimants (lease lapses; sweep clears `claim_state`,
appends `transition='expire'` to `tuple_claim_log`).

#### Step 6: Five canonical v1 subspaces

Ship `tasks/<project>`, `mailbox/<agent>`, `locks/<resource>`,
`events/<topic>`, `barriers/<barrier_id>` YAML files in
`nx/tuplespace/builtin/`.

#### Step 7: Critical Assumption spikes (gates Steps 8-12)

Per Layer 3 critique S1, the four-consumer landing surface
(Steps 8-12 below) cannot ship before CA #2, #4, and #5 clear
(the three spike-gated assumptions). Critical assumptions about
`take` atomicity and wake-mechanism behaviour must validate
against the deployment harness before agent-facing surfaces
document the API contract.

- 10-parallel-worker take-on-same-tuple stress test (CA #1 + #2).
  Per the prior re-gate observation: harness must inject a
  controlled sleep between candidate-selection and UPDATE to
  force the race window; assert exactly-one-winner under that
  interleaving.
- 100-paraphrase fuzz test against `mailbox` and `tasks`
  subspaces (CA #4).
- Polling-latency observation harness on a 10-worker work
  queue, both `block=False` and `block=True` paths (CA #5).
- `data_version` cost measurement on the deployment hardware
  (CA #6; honker reports 1-2ms median; verify on M-series and
  Linux x86_64).

If any spike fails, design returns to revision (e.g. UPDATE-
WHERE-NOT-EXISTS variant of the claim pattern, or slower
`data_version` cadence). Steps 8-12 are blocked until spikes
clear.

#### Step 8: Agent-consumer landing — five new coordination skills

Per RF-4, ship five new skill files in `nx/skills/` (or wherever the
plugin's skill registry lives):

- `nx:tuplespace-tasks` — work-stealing pattern; points at
  `tasks/<project>` with `out` / `take` / `ack` examples.
- `nx:tuplespace-mailbox` — request-reply pattern; points at
  `mailbox/<agent>` with correlation_id usage.
- `nx:tuplespace-lock` — mutex / lease pattern.
- `nx:tuplespace-events` — append-only telemetry pattern;
  read-only consumers.
- `nx:tuplespace-barriers` — N-way coordination.

Each skill is a thin wrapper that brings the canonical pattern into
the agent's context window in one tool call. Skills are *additive*
in Phase 1 — they recommend the new primitive but do not replace
or deprecate existing skills (`nx:nexus`, `nx:knowledge-tidy`, etc.).
The deprecation/rewrite of existing skills is Phase 3.

**O1 mitigation — CONTEXT_PROTOCOL forward reference**: alongside
the new skills, prepend a one-paragraph note to
`nx/agents/_shared/CONTEXT_PROTOCOL.md` naming the five
coordination skills and stating: *"Use these for cross-agent
coordination patterns (work-stealing, mailboxes, mutexes,
barriers, events). The canonical tier-vs-subspace guidance below
is unchanged for now and will be rewritten in Phase 3 once the
plans/scratch/memory wrappers ship."* This closes the
confused-agent window between new skills landing in Phase 1 and
the full CONTEXT_PROTOCOL rewrite in Phase 3 Step 8.

#### Step 9: Agent-consumer landing — introspection skills

Two skills that double as MCP tools for agent-side discovery:

- `nx:tuplespace-list` — wraps `list_subspaces`; returns the registry
  formatted for agent context.
- `nx:tuplespace-stats` — wraps `subspace_stats`; per-subspace counts
  and dimension distributions.

These are the "I don't know what's available" answer for agents that
join a session mid-flight or after compaction.

#### Step 10: Session-consumer landing — session-start banner

Per RF-1 / RF-6, the session-start hook (post-RDR-105) emits a
banner. Extend it with a one-line tuplespace summary:

```
T1 ephemeral subspaces: scratch, mailbox, locks, barriers (3 active, 12 entries)
Project subspaces:      tasks, events, memory (47 open tuples)
```

Cost: one extra `subspace_stats` call at session start. Surfaces
the "what's available right now" question agents currently can't
answer.

#### Step 11: User-consumer landing — `nx tuplespace` CLI

Per RF-6, ship a unified CLI subcommand that subsumes the
operations across surfaces:

- `nx tuplespace list [--tier=...]`
- `nx tuplespace show <subspace>` (schema, current stats)
- `nx tuplespace put <subspace> [-d key=value]... <content>`
- `nx tuplespace get <subspace> --query=... [-d key=value]...`
- `nx tuplespace take <subspace> --claimant=... --query=...`
- `nx tuplespace ack <claim_id>` / `nack <claim_id>`
- `nx tuplespace stats <subspace>`

Existing CLIs (`nx scratch`, `nx memory`, `nx store`) are NOT
modified in Phase 1 — they keep working unchanged. Phase 4 adds
deprecation banners.

#### Step 12: Script-consumer landing — env-var contract + JSON schema

Per RF-1, scripts need introspection. Provide:

- `nx tuplespace list --json` — machine-parseable subspace list.
- `nx tuplespace show <subspace> --schema-json` — JSON schema for
  the subspace's dimensions, suitable for client-side validation.
- Env-var contract: `NX_TUPLESPACE_SUBSPACE` + `NX_TUPLESPACE_CLAIMANT`
  set automatically when scripts are dispatched from the MCP layer
  (analogous to how RDR-105's dispatcher sets `NX_T1_HOST/PORT`).

### Phase 2: Ephemeral and permanent tiers

#### Step 1: Ephemeral tier (T1)

T1 chroma per-subspace collection on the per-session HTTP server.
Claim ledger as in-memory dict keyed by `(tuple_id, claimant)` per
MCP process. Lifecycle bound to MCP lifespan per RDR-105.

#### Step 2: Permanent tier (T3)

T3 chroma collection per subspace under a new `{base}_tuples`
database (extends RDR-004's split). Registry validation rejects
`take.enabled: true` for any subspace with permanent in `tiers:`.

#### Step 3: Tier cascade for `read`

Generalise `plan_match`'s T1-cosine → T2-FTS5 fallback to
N-tier cascade. Per-subspace `tiers: [a, b, c]` declares order.
Dedup by chash ID; floor evaluated per tier.

### Phase 3: v2 wrappers (behavior-preserving migration with independent flags)

Per RF-5, three independent feature flags gate the three wrapper
migrations. Each flag flips on its own schedule, each rolls back
independently, none couples to the others.

| Flag | Wraps | Default off through | Default on starting |
|---|---|---|---|
| `NX_TUPLESPACE_WRAPPED_PLANS=1` | `plan_match`, `plan_save`, `plan_search` | Phase 3 Step 2 land | Phase 3 Step 5 default-flip |
| `NX_TUPLESPACE_WRAPPED_SCRATCH=1` | `scratch` MCP tool, `nx scratch` CLI | Phase 3 Step 3 land | Phase 3 Step 6 default-flip |
| `NX_TUPLESPACE_WRAPPED_MEMORY=1` | `memory_put`, `memory_search`, `memory_get`, `memory_consolidate`, `memory_delete` | Phase 3 Step 4 land | Phase 3 Step 7 default-flip |

The flag-isolation contract from RDR-105 applies: a given MCP
server runs each surface entirely on flag-on or flag-off; tests
target one path per process per surface.

#### Step 1: Schema rollout for wrapped subspaces

Ship `nx/tuplespace/builtin/plans.yml`, `scratch.yml`, `memory.yml`
schemas in the registry. These are inert until their respective
flags flip — registered but no callers yet.

#### Step 2: Wrap `plan_match` as `read(subspace="plans/<verb>")`

Refactor `src/nexus/plans/match.py` internals to call the
tuple-space API behind `NX_TUPLESPACE_WRAPPED_PLANS`. Keep public
function signatures unchanged. Both flag paths exercised in CI.

#### Step 3: Wrap T1 scratch behind `NX_TUPLESPACE_WRAPPED_SCRATCH`

Refactor `src/nexus/db/t1.py` and `src/nexus/commands/scratch.py`.
The promote-to-T2 flow (`scratch_manage flag` + flush) becomes
`take` from `scratch/<session>` followed by `out` to
`memory/<project>`. RDR-041's tag vocabulary lands as the
registered enum on `scratch/<session>` — agents using ad-hoc tags
get a structured warning when the flag flips on (Phase 3 Step 6).

#### Step 4: Wrap T2 memory behind `NX_TUPLESPACE_WRAPPED_MEMORY`

`memory_put`/`memory_search`/`memory_get`/`memory_consolidate`/
`memory_delete` become thin wrappers over `out`/`read`/`take`
(disabled)/etc. Memory's `permanence` field becomes a registered
enum dimension.

#### Step 5: Default-flip wrapped plans

Sandbox shakedown (real Claude Code session running plan-matched
queries via `nx_answer`); zero behavior delta vs flag-off baseline.
Flip default. Hold for one release cycle before Step 6.

#### Step 6: Default-flip wrapped scratch

Same shakedown shape. Phase 3 introduces the tag-vocabulary
warning at flip time (RDR-041's enum becomes enforced). Hold one
release cycle before Step 7.

#### Step 7: Default-flip wrapped memory

Final wrapper flip. After this, all three legacy surfaces route
through the tuple-space primitive internally.

#### Step 8: CONTEXT_PROTOCOL rewrite

Per RF-3, after all three wrappers default-on, rewrite
`nx/agents/_shared/CONTEXT_PROTOCOL.md` from per-tier guidance to
per-subspace guidance. Update agent files in `nx/agents/*.md` that
quote tier-specific guidance. The proactive-search-vs-relay-reliant
agent split survives unchanged; only the tier-vocabulary changes.

#### Step 9: Existing skill rewrites

Update `nx:nexus`, `nx:knowledge-tidy`, `nx:plan-first`,
`nx:query`, `nx:research`, `nx:analyze`, `nx:review`, `nx:document`,
`nx:debug` to describe the new subspace-by-intent vocabulary.
Skills' public contracts (skill names + invocation shapes) hold;
internals update to recommend `out(subspace=…)` patterns.

### Phase 4: Deprecation cycle

After Phase 3 default-flips have stabilised across one release cycle:

#### Step 1: Deprecation warnings on legacy MCP tools

Add `DeprecationWarning` to `scratch`, `memory_put`, `memory_search`,
`memory_get`, `store_put`, `store_get`, `store_list`, `plan_save`,
`plan_search`. Each warning points at the equivalent
`nx tuplespace` operation.

#### Step 2: Deprecation banners on legacy CLIs

`nx scratch`, `nx memory`, `nx store` print one-line deprecation
banners pointing at `nx tuplespace`. Existing scripts continue to
work — banner is informational.

#### Step 3: `nx doctor --check-tuplespace-migration`

Audit hook that flags codebase usage of legacy tools/CLIs and
suggests the equivalent tuple-space invocation. Helps users find
migration work in their own scripts.

#### Step 4: Documentation cutover

`docs/architecture.md` rewritten to describe tuple-space as the
primitive, with plans/scratch/memory presented as canonical
subspace examples. `docs/cli-reference.md` foregrounds
`nx tuplespace`. `docs/memory-and-tasks.md` retitled or replaced.

### Phase 5: Persistent addressable surfaces (separate RDR)

Out of scope for this RDR. The surfaces application is a separate
design that builds on this primitive: a surface is a tuple subspace;
the renderer is a `read`-based projection over the tuple stream;
multi-writer support comes from append-only `out` + projection-based
read. RDR-1NN to be authored once Phase 4 stabilises.

### Phase 6: Removal (separate RDR)

Removal of legacy MCP tools, CLI subcommands, and Python public
APIs is gated on a separate RDR after Phase 4 stabilises across
multiple release cycles. Following RDR-105's ~one-month observation
window between deprecation and removal as precedent. Out of scope
for this RDR.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Subspace registry | `nx tuplespace list` | `nx tuplespace show <name>` | N/A (delete via PR) | `nx doctor --check-tuplespace` | git (YAML in repo) |
| `tuples.db` SQLite database | `ls ~/.config/nexus/tuples.db` | `sqlite3 ... .schema` | manual (rebuild from chroma) | `nx doctor` | sqlite3 backup |
| Per-template chroma collections | `nx tuplespace list` | `nx tuplespace stats <name>` | sweep on subspace removal | `nx doctor` | rebuilt from `tuples.db` |
| Claim ledger | `nx tuplespace stats <name>` shows open/expired/acked counts | per-claim via `tuple_claim_log` SQL (transition history) | N/A (sweep-managed) | `nx doctor` | covered by `tuples.db` backup |
| Tombstones | `nx tuplespace stats <name>` | counts by age | sweep | `nx doctor --check-tuplespace` | covered |

`nx tuplespace` is a new CLI subcommand bundled with Phase 1.

### New Dependencies

None. ChromaDB and SQLite are existing deps. PyYAML is already used
for plan registry. ULID is dropped in favour of chash IDs (RDR-108
compliance).

## Test Plan

- **Scenario**: `out` with valid dimensions → tuple stored, ID
  returned. — **Verify**: `tuples` row exists; chroma collection
  has one document; ID matches `sha256(canonical(...))`.
- **Scenario**: `out` with same content+dimensions twice → second
  call is idempotent. — **Verify**: Same ID returned both times;
  `tuples` table has exactly one row.
- **Scenario**: `out` with invalid enum value → raises
  `SubspaceSchemaError`. — **Verify**: No row written; no chroma
  document added; structured error mentions field + reason.
- **Scenario**: `read` returns top-k matches above floor with
  metadata filter. — **Verify**: Distance ranked ascending; all
  returned items satisfy `where`; floor enforced.
- **Scenario**: `read` filters out consumed tuples. — **Verify**:
  Tuples with `consumed_at IS NOT NULL` not returned even if their
  embedding matches.
- **Scenario**: `take` succeeds → returns `(Tuple, claim_id)`,
  claim row inserted with `expires_at` correctly set. — **Verify**:
  Claim row exists; tuple body reachable; subsequent `read` filters
  it out.
- **Scenario**: `take` ambiguous (top-1 vs top-2 gap < margin) →
  returns `None`. — **Verify**: No claim row inserted; logged at
  DEBUG.
- **Scenario**: 10 parallel `take` calls on a single tuple →
  exactly one wins. — **Verify**: One claim row inserted; nine
  losers fall through to next candidate or return `None`. CA #1 + #2.
- **Scenario**: `take` then crash before `ack` → lease expires →
  next `take` succeeds. — **Verify**: Tuple's `claim_expires_at <
  now` until sweep runs; `sweep_expired_claims` clears the tuple's
  claim state and appends `transition='expire'` to
  `tuple_claim_log`; subsequent `take` returns the tuple to a new
  claimant.
- **Scenario**: `take` by same claimant twice within lease →
  returns the same `(Tuple, claim_id)` pair (idempotent). —
  **Verify**: Tuple's claim_state remains `'claimed'` with
  matching `claimant`; UPDATE finds no eligible row (guard fails);
  caller's lookup-by-claim_id recovers the existing claim.
- **Scenario**: `ack(claim_id)` → sets `consumed_at` +
  `consumed_by` on the tuple and inserts `transition='ack'` row
  into `tuple_claim_log`. — **Verify**: `SELECT consumed_at FROM
  tuples WHERE claim_id=?` is non-NULL; `SELECT 1 FROM
  tuple_claim_log WHERE claim_id=? AND transition='ack'` returns
  one row; subsequent `read` excludes the tuple; tombstone count
  increments.
- **Scenario**: `nack(claim_id)` → tuple becomes available again. —
  **Verify**: Subsequent `take` (different claimant) succeeds.
- **Scenario**: Permanent-tier subspace with `take.enabled: true`
  in YAML → registry load fails. — **Verify**: MCP startup raises
  with clear error.
- **Scenario**: Tier cascade `read` against `tiers: [ephemeral,
  project]` → ephemeral matches returned first; falls through to
  project if below floor. — **Verify**: Result ordering matches
  cascade declaration.
- **Scenario**: Tombstone sweep removes consumed tuples past
  `retention_seconds`. — **Verify**: SQL row gone; chroma document
  gone.
- **Scenario**: 100-paraphrase fuzz → no false-positive take across
  intended-target / paraphrased-but-different-subject pairs. CA #4.
- **Scenario**: Subspace registration with `<param>` → concrete
  subspaces match the template. — **Verify**: `tasks/nexus` and
  `tasks/conexus` resolve to the same template, distinct concrete
  metadata.

## Validation

### Testing Strategy

- **Unit tests**: schema validation, chash computation, take CAS
  algorithm, tier routing logic. Target: 90%+ coverage on
  `src/nexus/tuplespace/`.
- **Integration tests**: end-to-end `out`/`read`/`take`/`ack`/`nack`
  against real SQLite + local chroma. Each canonical subspace has
  at least one happy-path and one failure-path test.
- **Concurrency tests**: 10-parallel-worker stress on `take` with
  injected sleep (CA #1 + #2); paraphrase fuzz on take-floor
  (CA #4).
- **Migration tests** (Phase 3): all existing plans / scratch /
  memory tests pass without modification after the wrapper refactor.
- **Smoke tests**: a representative agentic pattern (work-stealing
  pool with 5 workers, 50 tasks) executes end-to-end and the
  expected throughput / completion semantics hold.

### Performance Expectations

Empirical baseline only. The architecture reuses existing chroma +
SQLite primitives; no novel performance claims. Phase 1 spike
records median + tail latency for `out`/`read`/`take` against the
project tier and informs whether `block` becomes necessary in v2.

## Finalization Gate

> Complete each item with a written response before marking this
> RDR as **Accepted**.

### Contradiction Check

_To be completed during /conexus:rdr-gate._

### Assumption Verification

_To be completed during /conexus:rdr-gate. CA #2, #4, #5 are
spike-gated; CA #1, #3, #6 are Verified via source search +
honker production reference._

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `chromadb.PersistentClient.get_or_create_collection` | chromadb | Source Search (existing T2 catalog usage) |
| `chromadb.Collection.query(query_texts=, where=, n_results=)` | chromadb | Source Search (existing search engine usage) |
| `sqlite3.execute("INSERT … ON CONFLICT (…) DO NOTHING")` | sqlite3 | Source Search (existing beads + RDR-105 usage) |
| `hashlib.sha256(canonical_bytes).hexdigest()[:32]` | stdlib | Source Search (RDR-108 chunk natural ID) |
| FastMCP tool registration | mcp.server.fastmcp.FastMCP | Source Search (existing nx-mcp tool registry) |

### Scope Verification

_To be completed during /conexus:rdr-gate. The MVV (10-parallel-worker
work-stealing harness) is in scope and bundled into Phase 1 Step 7._

### Cross-Cutting Concerns

- **Versioning**: Schemas evolve additively in v1 (adding dimensions
  / enum values is forward-compatible). Removing or renaming
  requires a migration RDR.
- **Build tool compatibility**: N/A.
- **Licensing**: No new dependencies.
- **Deployment model**: Schema YAML ships in the conexus package
  alongside plan templates; loaded at MCP startup.
- **IDE compatibility**: N/A.
- **Incremental adoption**: v1 ships alongside existing surfaces;
  no callers forced to migrate. v2 wraps existing surfaces
  behavior-preservingly. v3 deprecates parallel APIs.
- **Secret/credential lifecycle**: N/A in the tuple-space layer
  itself. The trust boundary for agent-private subspaces (notably
  `mailbox/<agent>`) is inherited from the storage substrate: under
  `NX_STORAGE_MODE=daemon`, **RDR-113** (host-trust model — UDS
  `chmod 0600` + peer-credential check, single-user v1) ensures
  only the daemon-owner UID can read/write any subspace through the
  daemon. Under `direct` mode, host filesystem permissions on
  `tuples.db` are the gate (same-host single-user assumption).
- **Memory management**: Per-template chroma collections at T2 are
  bounded by `retention_seconds`. Tombstone sweep enforces ceiling.
  Claim ledger is bounded by lease expiry + sweep cadence.

### Proportionality

_To be completed during /conexus:rdr-gate. The document is scoped at the
"new architectural primitive with phased migration" level; sections
match RDRs 094 / 105 / 108._

## References

- RDR-004 — Four-Store T3 Architecture (operational separation
  rationale).
- RDR-041 — T1 Scratch Inter-Agent Context Sharing (tag vocabulary
  drift lesson).
- RDR-077 — Projection Quality / Threshold Tuning (per-collection
  calibration prior art).
- RDR-078 — Unified Context Graph and Retrieval (plan-centric
  retrieval trunk).
- RDR-079 — Operator Dispatch + Calibration (`min_confidence` floor
  precedent).
- RDR-087 — Collection Observability and Curation Surfaces
  (threshold-mismatch failure shape).
- RDR-092 — Plan-Match Text from Dimensional Identity (`match_text`
  payload shape).
- RDR-105 — T1 Chroma Architecture Env-Passdown (T2-as-shared-bus
  designation; hybrid discovery pattern).
- RDR-106 — Soft-Delete Tombstone Column (tombstone house style).
- RDR-107 — T3 Chunk Soft-Delete (superseded by RDR-108 but
  contributes the soft-delete pattern).
- RDR-108 — Graph Identity Normalization (content-derived natural
  IDs; FK-not-string-copy normalisation rule).
- `src/nexus/plans/match.py` — existing plan-match implementation
  (refactor target in Phase 3).
- `src/nexus/db/t1.py` — existing T1 scratch implementation
  (refactor target in Phase 3).
- `src/nexus/db/t2/memory_store.py` — existing T2 memory
  implementation (refactor target in Phase 3).
- Linda / tuple-space prior art:
  https://en.wikipedia.org/wiki/Tuple_space ;
  Gelernter & Carriero, "Generative Communication in Linda", 1985.
- JavaSpaces specification — lease + atomic-take + leased-write
  semantics that inform the at-least-once delivery model.
- Klaim — multiple named tuple spaces with locality, the
  inspiration for subspaces.
- Honker (https://github.com/russellromney/honker) — SQLite-only
  durable queue + pub/sub + stream library. Source for RF-9's
  three load-bearing patterns: `PRAGMA data_version` polling
  for cross-process wake (1-2ms median latency), single-statement
  `UPDATE … RETURNING` claim under SQLite's single-writer lock,
  and partial-index-on-working-set for tombstone-isolated claim
  performance. The wake mechanism specifically resolves the v1-
  draft objection to a `block` parameter on `take`. Honker's
  multi-language binding strategy is not relevant to nexus's
  Python-only runtime.

## Revision History

### Layer 3 critique 2026-05-09 — BLOCKED (3 critical, 3 significant, 3 observations)

Substantive-critic dispatch surfaced three critical defects in
the v1-draft design plus three significant phasing/compatibility
issues. Critical findings summarised below; significant + observations
unchanged from gate report.

- **C1 — `take` CAS two-winner race under concurrent distinct
  claimants.** Two-statement INSERT-then-recheck pattern broke
  under WAL DEFERRED isolation. **Resolved by RF-9 absorption
  (this revision):** schema collapses `tuples` + `tuple_claims`
  into a single table with state columns; claim becomes
  single-statement `UPDATE … RETURNING` atomic under SQLite's
  single-writer lock. Append-only `tuple_claim_log` preserves
  audit history. Honker's production reference confirms the
  pattern.
- **C2 — `locks/<resource>` mutual exclusion broken under
  semantic-match.** Margin gate skipped for single-candidate
  case; floor 0.95 over name-derived embeddings doesn't deliver
  exact-match. **Pending fix in next revision.**
- **C3 — `match_text` excluded from chash formula collapses
  logically distinct tuples.** Two `out` calls with identical
  content+dimensions but different `match_text` collide on tuple
  ID; second's embedding silently discarded. **Pending fix in
  next revision** (include `match_text` in canonical-bytes input
  to `sha256`).

Significant findings (S1: Phase 1 sequencing, S2: scratch wrapper
ID format change, S3: "mirrors RDR-108" overstatement) and three
observations remain pending.

### Honker absorption 2026-05-09 — RF-9 + design revisions

External prior art review (https://github.com/russellromney/honker)
surfaced three patterns that materially improve v1:

1. `PRAGMA data_version` polling enables blocking `take` without
   long-poll infrastructure. `block`/`timeout_seconds` parameters
   re-introduced on `take`; one watcher thread per `tuples.db`
   connection drives a shared wake event. CPU cost: one integer
   read per millisecond.
2. Single-statement `UPDATE … RETURNING` claim resolves C1.
   Schema collapses to one table with state columns plus an
   append-only audit log. Working-set partial index keeps claim
   performance bounded by active tuples, not history.
3. Visibility timeout = lease (terminology equivalence noted).

Implementation plan Step 4 expanded to include the
`_TupleSpaceWatcher`. Step 5 sweep set extended with
`sweep_claim_log`. References extended with honker reference.
CA #1 status upgraded to Verified (source-search + production
reference); new CA #6 added for `data_version` cost.

### Gate-finding closeout 2026-05-09 (post-honker absorption)

All Critical and Significant findings from the Layer 3 critique
are now resolved in-document:

- **C1 — `take` CAS race**: resolved by RF-9 honker absorption
  (single-statement `UPDATE … RETURNING`; collapsed schema).
- **C2 — locks mutex**: resolved. `take.mode: enum (semantic |
  exact)` added to subspace schema. Locks subspace declares
  `mode: exact` and bypasses chroma similarity entirely; SQL-only
  candidate selection on `match_keys`. Take algorithm pseudocode
  updated to branch on mode.
- **C3 — match_text in chash**: resolved. Tuple ID formula
  updated to `sha256(canonical(subspace + content + dimensions_json
  + match_text_or_empty))[:32]`. Tuple-shape definition, Key
  Discoveries entry, and §"Tuple ID formula" callout updated.
- **S1 — Phase 1 sequencing**: resolved. Spikes lifted from
  Step 12 to Step 7; agent-facing surfaces (Steps 8-12)
  explicitly gated on spike clearance. Steps 8-12 cannot ship if
  CA #2, #4, or #5 fail.
- **S2 — scratch wrapper ID format change**: resolved. RF-7
  amended with explicit carve-out: signatures and semantics hold,
  ID format does not. CHANGELOG documents; `nx doctor
  --check-tuplespace-migration` flags caller code that
  pattern-matches against legacy ID format. Forwarding-table
  alternative rejected with documented rationale.
- **S3 — "mirrors RDR-108" overstatement + cross-tier dedup
  semantics**: resolved. Existing Infrastructure Audit row
  rewritten to "Follows the principle, not the formula" with
  explicit namespace separation note. Key Discoveries entry on
  RDR-108 also clarified. Cross-tier dedup explicitly scoped to
  RDR-110 tuple-ID namespace only; chunk and tuple namespaces
  never merge.
- **O1 — confused-agent window**: resolved. Phase 1 Step 8
  (coordination skills) now ships a forward-reference paragraph
  in CONTEXT_PROTOCOL alongside the new skills, naming them and
  pointing forward to the Phase 3 rewrite.
- **O2 — spike test depth**: resolved. CA #2 (was CA #1.5 in
  the prior numbering) added explicitly
  for race-window-injection harness; Step 7 spike list updated
  to require sleep injection between candidate-selection and
  UPDATE.
- **O3 — cross-tier dedup semantics**: resolved as part of S3
  (namespace separation explicit; cross-tier dedup scoped to
  RDR-110 namespace).

### Re-gate Layer 3 critique 2026-05-09 — partial (1 critical, 3 significant, 2 observations)

Re-gate verified all nine prior findings genuinely resolved;
identified four new issues introduced by the fixes themselves.
All four resolved in a follow-up pass:

- **N-C1 — `LIMIT 1` in `UPDATE … RETURNING` requires non-default
  SQLite compile flag** (`SQLITE_ENABLE_UPDATE_DELETE_LIMIT`,
  not enabled in CPython stdlib `sqlite3`). **Resolved** by
  rewriting the take's CAS UPDATE to use a portable subquery
  form: `WHERE id = (SELECT id FROM tuples WHERE … ORDER BY …
  LIMIT 1)`. `LIMIT` lives inside the SELECT (universally
  supported); the outer UPDATE atomically claims that one row.
- **N-S1 — `exact_match_sql`/`exact_match_args` unspecified;
  potential SQL injection surface.** **Resolved** by replacing
  the opaque method calls in the pseudocode with the explicit
  pattern: `match_clause = " AND ".join(f"{k} = ?" for k in
  schema.match_keys)`, `match_values = tuple(where[k] for k in
  schema.match_keys)`. Column names come from the registered
  `match_keys` (controlled at schema-load), not caller input.
  Validation that all match_keys are present is explicit. No
  injection surface; no undocumented helper methods.
- **N-S2 — `acked_at`/`nacked_at` referenced inconsistently
  with the new schema.** **Resolved** by clarifying that ack/nack
  are captured as `tuple_claim_log` transitions
  (`transition='ack'` / `transition='nack'`), not as columns on
  `tuples`. Tuples table tombstones via `consumed_at` +
  `consumed_by` only. Test plan updated to read from
  `tuple_claim_log` for ack/nack verification.
- **N-S3 — stale "Briefly Rejected" entry for `block=True`
  contradicts shipped design.** **Resolved** by rewriting the
  bullet to record both the original rejection reasoning and the
  RF-9 reversal, with a forward reference to the honker absorption.

**Re-gate observations (also resolved):**

- **N-O1 — CA #1.5 not in numbered Critical Assumptions
  section.** Resolved by renumbering: prior CA #1.5 → CA #2;
  prior CA #2/#3/#4/#5 → CA #3/#4/#5/#6. All cross-references
  throughout the document (Step 7 spike list, Test Plan,
  Concurrency tests, Validation, Revision History) updated.
- **N-O2 — Two stale `tuple_claims` references survived the C1
  schema collapse** (tier-routing table + Existing
  Infrastructure Audit row). Both updated to reflect the
  collapsed schema.

### Third Layer 3 critique 2026-05-09 — partial (0 critical, 1 significant, 0 observations)

Single Significant: RF-9 prose at lines 619-647 still showed the
non-portable `RETURNING id LIMIT 1` directly inside the UPDATE
clause, contradicting the corrected subquery pseudocode at
lines 970-990. **Resolved** by rewriting the RF-9 narrative
bullet to use the portable subquery form
(`WHERE id = (SELECT id … LIMIT 1) RETURNING id`) with explicit
portability rationale referencing CPython's stdlib `sqlite3`
build configuration.

### Fourth Layer 3 critique 2026-05-09 — PASSED (0/0/0)

Verdict: justified. Zero critical, zero significant, zero
observations. RF-9 prose verified to use the portable subquery
form with explicit portability rationale inline. Pseudocode
consistent. No stale outer-UPDATE `LIMIT 1` form survives
anywhere in the document. End-to-end coherence across all four
revision rounds verified. Total findings closed across four
passes: 16 (3 critical → 0; 7 significant → 0; 6 observations →
0). RDR-110 ready for `/conexus:rdr-accept`.

### Post-acceptance cross-reference 2026-05-12 — RDR-112 (Storage-as-Service)

RDR-110's atomic-take primitive is built on SQLite WAL multi-process
safety (Critical Assumptions #1 and #2, both Verified for the
same-host POSIX-filesystem case). RDR-112 (draft 2026-05-12) found
this guarantee **does not extend across container overlayfs bind
mounts** — WAL requires `mmap` semantics that overlayfs blocks, so
a SQLite handle opened from inside a container against a host-mounted
path forks into a per-container WAL. The Linda `in` primitive
would silently break (or worse, silently double-claim) in that
configuration.

**This does not invalidate RDR-110.** The atomic-take CAS primitive
itself is correct; only its *call site* shifts. Under RDR-112, T2 is
owned by `nx daemon t2` (single process, single SQLite handle,
single WAL — exactly the configuration RDR-110's CAs assume).
Containerised clients call atomic-take via UDS/TCP RPC to the
daemon; the daemon executes the same
`UPDATE … RETURNING … WHERE id = (SELECT id … LIMIT 1)`
pattern against its local handle. The CAS is unchanged.

**Sequencing implication for planning**: RDR-110's planning chain
has two ordering options.

1. **Ship 110 first against direct-file T2** (current `T2Database`
   facade). Same-host atomic-take works immediately. RDR-112's
   daemon later wraps the existing CAS as an RPC; call sites
   move from `T2Database.take(...)` to `T2Client.take(...)` with
   no semantic change.
2. **Sequence 112's daemon before 110's atomic-take call sites**
   so the call sites are written once.

**Original preference (2026-05-12, since revised)**: Option 1. The
CAS primitive is independent of where it runs; the daemon is a
transport wrapper, not a redesign.

**Revised preference (2026-05-13, RDR-112 gate round 2)**: a
hybrid is required for the `block=True` path. RDR-112 gate
round 1 surfaced that the `data_version`-polling mechanism in
§RF-9 / §CA #6 — which underwrites `block=True` cross-process
wake — depends on the same `mmap` semantics that RDR-112 §A2
verified are broken across container overlayfs bind mounts.

**Further-revised scope (2026-05-13, RDR-110/111/112 triad
analysis)**: the round-2 carve-out was too narrow. The overlayfs
`mmap` failure is a **class** of bug, not specifically about
`data_version`. SQLite's single-writer lock — the foundation of
*every* RDR-110 operation, including the `block=False` CAS at
§RF-9 — relies on the same `mmap` semantics. Across overlayfs
bind mounts, two containers each open their own SQLite handle
that resolves to a forked WAL with its own single-writer lock.
**Two `block=False` claimants in different containers can both
win the CAS.** Sweeps (`sweep_expired_claims`,
`sweep_tombstones`) run against each container's forked WAL and
produce inconsistent tombstones. The `tuple_claim_log` audit
captures only its container's view.

**Definitive ordering (D3 from the triad rework)**:

- **Single-host, single-filesystem** (no container boundaries):
  direct-file T2 access is correct for both `block=False` and
  `block=True`. CA #1, CA #2, CA #5, CA #6 hold as Verified for
  this scope only.
- **Multi-container or any deployment that crosses an overlayfs
  bind mount**: all RDR-110 operations require
  `NX_STORAGE_MODE=daemon`. The daemon owns the single SQLite
  handle (single lock, single WAL, single `data_version`), and
  clients call atomic-take / sweep / audit via RPC. `block=True`
  is implemented daemon-side as a blocking-take RPC.
- **Phase 1 Step 4** ships the CAS implementation and the
  same-host watcher as designed. The `block=True` call sites
  land feature-flagged off until `NX_STORAGE_MODE=daemon` is
  available; `block=False` ships unconditionally and is correct
  same-host. **Multi-container deployments must run in daemon
  mode regardless of `block` value.**

The CAS primitive itself is unchanged in any path; only the
container-vs-daemon boundary determines correctness.

**No changes to RDR-110's design surface.** `related_rdrs`
frontmatter includes RDR-112; this revision-history note
records the cross-reference and the full sequencing constraint.

### 5x5 alignment pass 2026-05-13

After RDR-111 (PASSED R9), RDR-112 (PASSED light re-gate), and
RDR-113 (PASSED R1) all completed their gates, a focused
critic alignment check (`a25b97d1aac1d8523`) flagged one
significant + three observations against RDR-110. Closeouts in
this revision (no design-surface change; record-keeping only):

- **S** (significant): Technical Design `data_version` polling
  thread description (lines ~1025) and Implementation Plan
  Step 4 watcher description (lines ~1368) said the
  `_TupleSpaceWatcher` runs in-process against `tuples.db` —
  correct in `NX_STORAGE_MODE=direct`, but under
  `NX_STORAGE_MODE=daemon` (RDR-112) the watcher is
  daemon-internal and clients subscribe via the daemon's
  `EventStream` / blocking-take RPCs. Holding a client-side
  `tuples.db` connection in daemon mode is forbidden by
  `nx doctor --check-storage-boundary` (RDR-112 §5 lint).
  Both sites now carry a "Mode split — daemon vs direct"
  note pointing at RDR-112 Approach §7 and §A2. The
  in-process watcher path remains correct as the direct-mode
  implementation; daemon-mode adds the RPC indirection.
- **O1**: Frontmatter `related_rdrs` did not include RDR-113.
  Added.
- **O2**: Body prose had no cite for RDR-113's host-trust
  model on `mailbox/<agent>` and other agent-private
  subspaces. Added a paragraph in §Cross-Cutting Concerns
  §Secret/credential lifecycle naming RDR-113 as the trust
  boundary under daemon mode.
- **O3**: (RDR-111 Proposed Solution panel descriptions had
  stale "same process, direct SQL appropriate" justification
  — fixed in RDR-111 alongside this RDR's edits, not in
  RDR-110.)

No re-gate required — these are documentation-tracking
corrections, not design changes.
