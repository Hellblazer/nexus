# Field Report: Architecture-as-Code — Lessons from the ART Collection Rebuild

**Date:** 2026-04-15
**Source project:** ART (neural architecture for grounded dialog)
**Nexus version observed:** 4.4.0
**Scope:** Actionable tooling gaps surfaced while authoring, auditing, and
rebuilding a ~100-chunk project-internal architecture documentation
collection against Nexus T3 + taxonomy + catalog infrastructure.

This is a forward-looking design input for the Nexus roadmap, not a bug
report. Specific bugs encountered during the session are filed separately
as `nexus-40t`, `nexus-lub`, `nexus-8l6` and referenced below.

---

## 1. Context — what the downstream project attempted

ART maintains an NLP-chat architecture documentation collection
(`docs__art-architecture`, 10 files, now 101 chunks) that explains how the
ART codebase realizes Stephen Grossberg's neural architecture for grounded
dialog. The collection cites 80 Grossberg papers, references ~40 closed
RDRs, and describes a 9-layer system architecture built atop Grossberg's
6-layer cortical laminar circuit.

The collection was originally authored (March–April 2026) when Nexus T3 was
functional but largely unaudited. During a 2026-04-15 rebuild session, the
project:

1. Separated 19,417 paper chunks into `docs__art-grossberg-papers` from a
   mixed `docs__ART-8c2e74c0` collection.
2. Ran full cross-collection projection backfill (267K assignments across
   95 collections).
3. Ran `nx taxonomy audit` on every ART-relevant collection.
4. Used the substantive-critic agent with projection evidence to critique
   the architecture collection.
5. Applied 6 prioritized edits in place + added one missing sibling file
   (`layer8-dialog-management.md`).
6. Discovered and fixed a systemic notation collision (5 coexisting
   numbering schemes: cortical `L1-L6`, system `Layer 1-9`, boundary
   `B1-B7`, feedback `F1-F7`, Gated-Multipole `GM1-GM4`).

This exercise generated substantive feedback about how Nexus could better
support *architecture-as-code* workflows for downstream projects.

---

## 2. What worked well (don't break these)

Before the critique, worth explicitly calling out capabilities that proved
their value:

- **Cross-collection projection with persisted assignments.** Made the
  empirical "what is this collection *actually* about?" question answerable.
  Surfaced the finding that `docs__art-architecture` projects more strongly
  into `rdr__ART-8c2e74c0` than into the paper collection — revealing the
  docs describe *implementation* more than *theory*.
- **`nx taxonomy audit` threshold diagnostics.** p10/p50/p90 cosine
  quantiles and below-threshold counts per collection are exactly the
  right granularity for validating a documentation collection.
- **`nx taxonomy hubs` with ICF weighting.** Spotted cross-collection
  concept formation cleanly once projection was persisted.
- **Content-addressed ChromaDB operations.** Direct ChromaDB API usage was
  necessary for the collection-split + rebalance work. The fact that
  embeddings, metadata, and documents can be copied between collections
  without re-ingestion is a lifeline — this kept a
  20-minute-otherwise-hours-long operation tractable.
- **Substantive-critic with projection evidence injected into its prompt.**
  The agent produced 6 prioritized findings, 2 critical + 4 significant,
  each with a specific file + section + recommended edit. No generic
  "consider adding tests" noise.
- **Catalog tumbler system.** `1.653.XX` identifiers appeared in the ART
  Tier 3 primary-sources table and proved their value as stable
  citations — content-addressed, paper-rename-proof, directly usable in
  prose.

## 3. Findings — tooling gaps surfaced by actual use

Each finding below has: *what failed*, *what the user had to do as a
workaround*, and *what capability would have prevented it*. Ranked by
effort-to-impact ratio.

### 3.1 Quick wins (low effort, high impact)

#### F1 — Stale-reference detection across collection moves

**What failed.** `docs/architecture/primary-sources.md` line 3 asserted
"12,900 chunks from Grossberg papers indexed in `knowledge__art`." This
was correct when written. A subsequent collection-level move (papers
relocated to `docs__art-grossberg-papers`) silently broke the pointer. A
reader or agent following the instruction would query the wrong collection
and get incomplete results. Nothing in Nexus flagged this drift.

**Workaround.** Manual grep + human review during a full-collection audit.

**Proposed capability.** `nx taxonomy validate-refs <doc-path>` — scans a
markdown file for collection-name mentions and chunk-count claims, compares
against current T3 state, reports drift. Could run as a pre-commit hook
when `docs/` files change, or as a CI gate. Related to RDR-077 (projection
quality) — same ICF signal applies: if a topic's highest ICF has shifted
to a different collection, any doc citing the old collection is stale.

#### F2 — Domain glossary feeding taxonomy labeler (blocks hallucinated labels)

**What failed.** A topic in `rdr__ART-8c2e74c0` covering 43 chunks of
RDR-068 gated-multipole plan-drive competition was auto-labeled **"Gated
Multipole Single Mode Fiber"**. The term `SSMF` in the cluster content
means `SelfSimilarMaskingField` (an ART-internal class name); Claude's
labeler hallucinated the optical-fiber expansion because it had no domain
glossary to anchor abbreviations.

**Workaround.** Manual detection (by reading the terms field) + `nx
taxonomy rename`.

**Proposed capability.** Per-project glossary registration. A project
declares abbreviations in `.nexus.yml` or `docs/glossary.md`, and the
taxonomy labeler injects these into Claude's system prompt as
disambiguation context. Scope: small — the labeler prompt is already
LLM-composed; adding a glossary paragraph is a 5-line change. Impact: high
— prevents a class of silent mislabeling that erodes taxonomy trust.

#### F3 — RDR / bead status as doc-build tokens

**What failed.** Architecture docs hard-code claims like "Phase 3 =
COMPLETE", "RDR-072 ACCEPTED", "SovereignDialogPipeline WIRED". Each
requires manual update when the underlying bead closes or reopens. We
discovered multiple drift cases this session (superseded
`MaskingFieldCompetition`, RDR-061 "closed/specified" ambiguity, dead-code
references to `SemanticResponseField` that was later wired).

**Workaround.** Substantive-critic audit caught the drift; human edits
applied in place.

**Proposed capability.** Doc-build-time token resolution:
`{{bd:ART-xxx.status}}`, `{{rdr:072.status}}`, `{{bd:ART-9nfy.epic.progress}}`.
Implementation: `nx doc render <path>` that expands tokens by querying the
bead DB / RDR index. Optional: `nx doc validate <path>` that flags tokens
referencing unknown beads. Leverages existing bead/RDR state infrastructure.

#### F4 — Notation-scheme linter (project-scoped style rules)

**What failed.** The ART architecture collection accumulated FIVE
coexisting numbering schemes with overlapping alphabets (cortical
`L1-L6`, system `Layer 1-9`, forward boundaries `B1-B7`, feedback loops
`F1-F7`, Gated-Multipole `GM1-GM4`). Nothing prevented authors from writing
`L8` when they meant system Layer 8 — a silent collision because `L`
conventionally means cortical laminae (1-6 range).

**Workaround.** Post-hoc grep audit + human rule formulation +
hand-applied disambiguations.

**Proposed capability.** Per-project notation rules declared once
(e.g., `docs/.nxlint.yml`), checked on doc commit:
```yaml
notation:
  cortical-laminae: {prefix: L, range: [1, 6]}
  system-layers: {format: "Layer {n}", range: [1, 9]}
  boundaries: {prefix: B, range: [1, 7]}
```
CI fails the doc if `L7` or `L8` appears in prose. Related to existing
`query-sanitizer` work (RDR-071) — same class of guard, applied
upstream to authoring.

### 3.2 Medium effort (high leverage once built)

#### F5 — Per-claim grounding validator

**What failed.** Every architectural claim in the ART collection is
implicitly grounded in a paper or RDR, but grounding is expressed as
prose ("per Grossberg 2013") rather than machine-checkable references. A
reader cannot tell whether a claim's citation actually exists in the
paper corpus without manual search.

**Workaround.** Substantive-critic read the 10 files directly and
cross-checked against T3. Doesn't scale.

**Proposed capability.** Treat catalog tumblers / `chash:` spans as
first-class citations in markdown. A validator mode:
```bash
nx doc check-grounding docs/architecture/primary-sources.md
# scans for citation patterns (e.g., [grounded-by: 1.653.XX])
# for each, verifies the tumbler resolves to indexed content
# flags prose-only citations ("Grossberg 2013") as ungrounded
```
Bootstraps incrementally: the tool doesn't require every claim to have a
tumbler, but it reports coverage. Over time coverage ratchets up.

#### F6 — Author-extension auto-flag via projection threshold

**What failed.** The ART collection has hand-flagged `> [Author extension]`
disclaimers for Maturana-Varela / Prigogine / Ashby framings. Each flag
was added by a human noticing "this isn't Grossberg's terminology." Missed
ones would slip through; redundant ones accumulate.

**Workaround.** Hand-review of every claim, inline disclaimer
accumulation.

**Proposed capability.** Taxonomy-driven auto-flag. Compute: does this
chunk project at threshold into `docs__art-grossberg-papers`? If no, it's
a candidate for `[Author extension]` tagging. `nx taxonomy check-doc
--primary-source docs__art-grossberg-papers` reports chunks that fail
grounding and suggests flagging. Uses RDR-077 projection quality
infrastructure directly.

#### F7 — Pre-commit substantive-critic gate

**What failed.** The substantive-critic agent was invoked post-hoc and
found 6 issues that had been merged weeks earlier. Earlier invocation
would have caught each at authoring time.

**Workaround.** Post-hoc critique session.

**Proposed capability.** `nx doc review <paths>` command that dispatches
the substantive-critic with relevant projection evidence pre-injected.
Git pre-commit hook invokes it on `docs/**/*.md` changes. Blocks the
commit on Critical findings; warns on Significant. Runs against the
docs-to-be-committed, not HEAD — so it catches issues before they land.

#### F8 — "Empirical anchors" rendered section per doc

**What failed.** A reader of `docs__art-architecture` has no direct way
to know that the collection's semantic center of mass is "Semantic Vector
Topic Clustering" at 0.75 cosine. They have to run projection + audit
manually. The substantive-critic surfaced this — but only because it was
given the projection data by the operator.

**Workaround.** Agent-mediated discovery. One-off insight.

**Proposed capability.** Every doc can embed a rendered `<!-- nx-anchor -->`
block that `nx doc render` expands at build time to show the top-5
cross-collection topic anchors for the doc. Readers see the empirical
shape of the doc they're reading, not just the claimed shape. Live data,
refreshed on each render.

### 3.3 Long-term investments

#### F9 — Symbol-analysis feeding "Implemented but Not Wired" queries

**What failed.** The `component-inventory.md` section "Implemented but
Not Wired" is hand-maintained. During this session we confirmed
`SemanticResponseField` was wired (earlier MEMORY.md said it was dead
code), meaning the manual table had drifted. A call-graph analyzer would
answer "zero production callers" deterministically.

**Proposed capability.** Integrate repo indexing with JVM (or equivalent)
symbol analysis. Expose call-graph queries via `nx code call-graph <class>`.
Allows documentation to embed queries rather than hand-maintained tables.
Related to the Serena JetBrains backend work — this may already be
partially buildable.

#### F10 — Graph-first authoring with markdown-as-view

**What failed.** Current architecture docs are 10 flat markdown files
with duplicated cross-references, drift-prone `see X.md` prose, and
repeated author-extension disclaimers. Each piece of content exists in
exactly one place; moving it breaks references.

**Proposed capability.** Catalog-graph as primary representation. Every
claim is a node, every citation a typed edge, every doc a named view. `nx
doc render view:architecture-master` produces the master markdown from
the graph. Refactoring is a graph rewrite. Related: RDR-078 (unified
context graph) — this is an authoring use case for the same substrate.

#### F11 — Chunk-granular citation viewers

**What failed.** The catalog tumbler system works for paper-level
citations but not chunk-level. When a claim is grounded in a specific
paragraph of a paper, there's no ergonomic way to cite that paragraph.

**Proposed capability.** `chash:` span syntax in prose: `[as shown
in](chash:abc123...)`. On hover/click, viewer displays the chunk content.
Lets claims cite at the resolution they actually depend on.

#### F12 — ICF-based stale-reference auto-detection at query time

**What failed.** F1 above (manual drift detection) captures the pattern
for a specific doc-time check. The broader pattern: any time a collection
receives chunks that match a topic better than its current home, the ICF
distribution shifts. Existing docs that cite the old home become stale.

**Proposed capability.** Periodic `nx taxonomy detect-drift` that
identifies topics whose ICF-weighted best home has changed since the last
audit. Reports the drift and flags any indexed documents that cite the
stale home. Works with existing RDR-077 infrastructure.

---

## 4. Connection to existing Nexus issues

| Finding | Related bead | Notes |
|---------|------|-------|
| F1, F12 | RDR-077 (projection quality) | Same ICF infrastructure |
| F2 | nexus-40t (metadata hygiene) | Better metadata → better labeling context |
| F3, F8 | new capabilities | |
| F4 | RDR-071 (query sanitizer) | Same class of input guard |
| F5, F10, F11 | RDR-078 (unified context graph) | Direct authoring use case |
| F6 | RDR-077 + nexus-40t | Projection + clean metadata |
| F7 | new — requires agent-in-loop infra | |
| F9 | Serena integration | |
| — | nexus-lub (orphan taxonomy cleanup) | Prerequisite hygiene |
| — | nexus-8l6 (empty source_title) | Prerequisite hygiene |

---

## 5. Priority ranking for the Nexus roadmap

If a Nexus roadmap window opens, the recommended order:

1. **F2 (glossary labeler context)** and **F4 (notation linter)** — both
   are small changes with immediate authoring-trust wins. F2 alone
   prevents an entire class of taxonomy mislabeling.
2. **F1 (stale-reference validator)** — high-leverage, requires only
   existing projection data. Could ship as a single CLI subcommand.
3. **F3 (doc-build token resolution)** — moderate effort, high ROI for
   any project with many architecture docs. Leverages beads-as-data.
4. **F7 (pre-commit substantive-critic gate)** — needs agent-in-loop
   plumbing; unlocks the whole quality regime.
5. **F5 + F6 (grounding validator + author-extension flag)** —
   co-designed; both depend on clean tumbler infrastructure.
6. **F8 (empirical anchors)** — rendering convention once tokens exist.
7. **F9, F10, F11 (symbol analysis, graph-first authoring,
   chunk-granular citations)** — larger investments, probably their own
   RDRs.

---

## 6. What the downstream project is doing in the meantime

ART has adopted interim disciplines at the markdown level:

- Explicit §0 Notation block in the master doc declaring all five
  numbering schemes (cortical, system, boundary, feedback, Gated Multipole)
  with usage rules.
- Substantive-critic runs post-edit until pre-commit integration exists.
- Manual `nx taxonomy audit` after significant collection changes.
- Hand-curated catalog tumblers for Tier 3 primary sources.
- Per-layer canonical files (`layer8-dialog-management.md` added this
  session) to constrain scope drift.

None of these are durable — they depend on author discipline. A Nexus
instance that directly supports F1–F8 would eliminate most of this
overhead.

---

## 7. Reproducibility

All T3 operations from this session are captured in the ART repo session
memory under `/Users/hal.hildebrand/.claude/projects/-Users-hal-hildebrand-git-ART/memory/`.
Specifically:

- `feedback_t3_sanitize_obsolete.md` — metadata whitelist workaround
  (nexus-40t origin)
- `feedback_git_meta_dual_read.md` — git_meta consolidation (verified 2026-04-15)

The three existing bugs (`nexus-40t`, `nexus-lub`, `nexus-8l6`) capture
the infrastructure issues exposed during the session. This field report
complements them with forward-looking capability proposals.
