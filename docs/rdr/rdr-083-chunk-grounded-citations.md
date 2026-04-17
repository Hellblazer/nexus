---
title: "RDR-083: Corpus-Evidence Tokens — `chash:` Spans, Grounding Validator, Author-Extension Auto-Flag, `nx-anchor` Rendering"
id: RDR-083
status: draft
type: Feature
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-15
related_issues: []
related: [RDR-053, RDR-075, RDR-077, RDR-078, RDR-082]
---

# RDR-083: Corpus-Evidence Tokens

Architecture docs cite papers and RDRs in prose ("per Grossberg 2013", "see RDR-068") without machine-checkable references, and they narrate their own structural shape ("this collection is about attentional resonance") without ever surfacing the *empirical* shape that projection data already computed. The ART field report (2026-04-15, §3.1/F3, §3.2/F5, F6, F8, F11) surfaced four coupled failures: a reader cannot verify that any given claim's citation actually exists in the indexed corpus; `[Author extension]` disclaimers are added by hand when a claim isn't grounded in the primary-source collection, with obvious false-negative and redundancy modes; when a claim depends on a specific paragraph of a paper, there's no ergonomic way to cite below paper granularity; and a reader cannot see the collection's projected topic shape without running projection and ICF by hand. Nexus already has content-addressed chunks (catalog tumblers + chunk hashes) and cross-collection projection assignments (RDR-075/077). This RDR introduces **corpus-evidence tokens** — a `chash:` markdown-link primitive plus an `{{nx-anchor:…}}` token, all resolved through RDR-082's renderer — and two validator passes (grounding, author-extension) that together close the four failures.

This RDR is scoped downstream of RDR-078 (plan-centric retrieval, accepted 2026-04-14) and RDR-082 (doc render / token resolution). Both are prerequisites: RDR-078 establishes the chunk-level addressing used here; RDR-082 provides the `nx doc` CLI surface, the token grammar, and the Resolver registry that 083 extends with corpus-evidence resolvers.

## Problem Statement

### Enumerated gaps to close

#### Gap 1: Claims cite papers, not paragraphs

Catalog tumblers (`1.653.XX`) work for paper-level citations; they resolve to a document, not a claim-supporting passage. A claim like "boundary feedback is gated by attentional resonance" depends on a specific paragraph, but the citation machinery operates at paper granularity. Readers cannot verify the claim without manual paper-reading; agents cannot retrieve the grounding chunk without re-searching. A `chash:` span syntax — content-addressed to the chunk, not the paper — lets prose cite at the resolution it actually depends on.

#### Gap 2: Grounding is prose-only and not machine-checkable

Nexus has every indexed chunk as a hashable artifact; it has no validator that says "this claim in the doc references a chunk that exists in the corpus." The field report shows the symptom: every architectural claim is implicitly grounded, but no tool can ratchet a project from 0% to higher machine-verifiable coverage over time. Without a grounding validator, citation quality cannot be measured, can only assert itself.

#### Gap 3: `[Author extension]` flagging is entirely manual

When a claim uses framing not present in the primary-source collection (e.g., ART's Maturana-Varela / Prigogine / Ashby framings alongside Grossberg's vocabulary), the author is expected to hand-flag it with `> [Author extension]`. This catches some cases, misses others, and accumulates redundant flags. RDR-075's cross-collection projection directly answers the underlying question: "does this chunk project at threshold into the designated primary-source collection?" A tool that computes this per claim and suggests author-extension flagging replaces the manual discipline with a data-grounded one.

#### Gap 4: Docs cannot surface their own empirical semantic shape

A reader of `docs__art-architecture` cannot tell from the prose that the collection's semantic center of mass is "Semantic Vector Topic Clustering" at 0.75 cosine — they would have to run projection and ICF by hand. RDR-077's projection-quality data makes this answerable in a single query, but there is no rendering convention that embeds the answer into a doc. Readers see only the *claimed* shape, never the *empirical* shape. (Originally scoped to RDR-082; moved here 2026-04-16 because the data dependency and validator story sit entirely in 083's surface.)

## Context

### Background

The ART collection's primary-sources.md manually curates 80 Grossberg papers as Tier 3 citations using `1.653.XX` tumblers. This proved the catalog tumbler system works for paper-level discipline but surfaced two limits: sub-paper granularity is absent, and no tool validates the citations at authoring time. The field report rates F5 (grounding validator) and F6 (auto-flag) as medium-effort high-leverage; F11 (chunk spans) as a long-term investment. This RDR bundles them because they share infrastructure — once chunk hashes are prose-citable, grounding validation and auto-flag drop out as validators over the same span set.

`chash` is already used internally in Nexus as the chunk hash field in `catalog_link` MCP tool and in RDR-078's chunk-level link targeting. Extending it to a markdown citation primitive is a surface addition, not a new model.

### Technical Environment

- Python 3.12+, Click CLI, markdown.
- Catalog schema (RDR-053 Xanadu fidelity + RDR-060 catalog-path rationalization) — chunks addressable by `chash:<hex>` with registered documents and links.
- Projection table `topic_assignments` with `source_collection` + `similarity` (RDR-077).
- `nx doc render` command group introduced in RDR-082.

## Research Findings

### Investigation

- Confirmed `chash:` usage in `src/nexus/catalog/catalog.py:resolve_chunk()` for chunk-level link targets. Extending to a markdown span is mechanical: `[text](chash:abcd1234…)` is valid markdown today (browsers ignore unknown schemes; renderers preserve them).
- Confirmed RDR-078 defines chunk-level `follow_links` traversal; the prose citation is the reciprocal surface (author-originated links rather than retrieval-time expansion).
- Verified `topic_assignments` with `source_collection` populated allows a direct threshold query: `SELECT COUNT(*) FROM topic_assignments WHERE doc_id = ? AND source_collection = 'docs__art-grossberg-papers' AND similarity >= 0.70`. Zero rows → not grounded → author-extension candidate.
- Surveyed markdown renderers: GitHub silently preserves `chash:` links (no navigation); pandoc preserves; `nx doc render` will expand them into footnote-style anchors or preview popovers (rendering design is Phase 2 polish).

### Key Discoveries

- **Verified** — `chash:` as a markdown URL scheme is rendering-neutral today; no ecosystem breakage.
- **Verified** — The same `topic_assignments` query answers both "is this chunk grounded?" and "is this claim an author extension?" — one data path, two reports.
- **Documented** — RDR-078's chunk-level link primitives align with this RDR's direction (reciprocal surfaces).
- **Assumed** — A per-claim `chash:` citation is ergonomic enough that authors will adopt it incrementally. **Verification**: dogfood on one Nexus RDR; measure author-visible friction.
- **Assumed** — Grounding coverage ratchets upward when reported as a percentage; projects set their own targets. **Verification**: land the metric first, observe behavior across two projects before setting thresholds.

### Critical Assumptions

- [x] `chash:` prefix can be reliably extracted from `[text](chash:…)` without regex pathologies — **Status**: Verified — `_CHASH_LINK_RE` in `src/nexus/doc/citations.py:55` enforces exactly 64 hex chars; positive + invalid-length + fenced-skip cases covered by parametrized tests in `tests/test_rdr_083_corpus_evidence.py`.
- [ ] The projection similarity threshold used by the auto-flag (default 0.70) is stable enough to be useful across domains — **Status**: Unverified — **Method**: Spike on ART and Nexus corpora; tune per-project if needed.
- [ ] Chunk hashes stay stable across reindexing (no salt changes, no chunking-strategy churn) — **Status**: Verified — **Method**: Source Search — RDR-053 fixed chunk boundaries and hash inputs.

## Proposed Solution

### Approach

Four cooperating surfaces, all under the `nx doc` command group introduced by RDR-082:

1. **Prose syntax** (`chash:` spans) — `[display text](chash:<chunk-hash>)`. Standard markdown link with a deliberate URL scheme; `chash:` is reserved in the catalog (`src/nexus/catalog/catalog.py:resolve_span`) for content-addressed chunk references. **v1 ships the grammar and the scanner; it does not resolve the hash to a chunk. A resolver (`resolve_chash`) is deferred — see §v1 Scope Reduction.**
2. **Grounding validator** — `nx doc check-grounding <path>`. Scans prose for citation-shaped patterns (`chash:` spans, `[<Author> <year>]` patterns, bracketed-number references) and reports per-doc counts + coverage ratio (chash / total). **v1 counts chash-shaped citations as grounded-shape; it does not verify that each hash exists in the corpus. `--fail-under <ratio>` exits non-zero when the ratio falls under a floor — this works against the shape-only counts.**
3. **Author-extension auto-flag** — `nx doc check-extensions <path> --primary-source <collection>`. Conceptually: for each chunk in the doc with a registered catalog entry, query `topic_assignments` filtered to `--primary-source` and flag chunks below threshold. **v1 limitation: the command passes `chash:` hex strings as `doc_ids`, but `topic_assignments.doc_id` stores ChromaDB collection-scoped IDs — the namespaces never intersect, so every input lands in `no_data`. The command emits a loud WARNING in that case and the docstring marks it `[experimental]`.**
4. **Corpus-shape anchor** (`{{nx-anchor:…}}` token) — `{{nx-anchor:<collection>[|top=N]}}` renders as a markdown list of the top-N projected topics for the collection, pulled from `topic_assignments` filtered by `source_collection`. Registers as an additional Resolver on RDR-082's Resolver registry; no grammar churn. **Fully functional in v1.**

All four land behind the `nx doc` group so the learning surface is coherent.

### v1 Scope Reduction

All four deferrals below are owned by **RDR-086: Chash Span
Resolution** (draft, 2026-04-16).  RDR-086's single primitive —
`catalog.resolve_chash(chash)` backed by a T2 `chash_index` table
— unblocks all four consumers here.

- `src/nexus/catalog/catalog.py` `resolve_chash(chash) -> ChunkRef | None` — lookup a chunk by its content hash across indexed collections. Without this, `check-grounding` cannot tell "valid hash in corpus" from "valid-looking hash of unindexed chunk".
- chash → ChromaDB-doc-id resolution — required for `check-extensions` to produce meaningful candidates rather than the current always-`no_data` behaviour.
- `--fail-ungrounded` flag on `check-grounding` — currently absent; would fail the build when any chash span fails to resolve. Depends on `resolve_chash`.
- `--expand-citations` flag on `nx doc render` — renderer-polish Phase 2, preserves `chash:` links in v1 output without expansion.

### Technical Design

**Span resolution** — *deferred from v1, see §v1 Scope Reduction.* The illustrative `resolve_chash()` method below is the design target for a follow-up bead; it is NOT part of the v1 ship.

```text
# Future — deferred to a follow-up bead
def resolve_chash(self, chash: str) -> ChunkRef | None:
    """Look up chunk by content hash; return (doc_id, chunk_index, text) or None."""
```

**Citation scanner** (new, `src/nexus/doc/citations.py`):

```text
# Illustrative — one pass, all three citation shapes
def scan_citations(md_text: str) -> list[Citation]:
    # yields Citation(kind=chash|prose|bracket, text, span, metadata)

class Citation:
    kind: Literal["chash", "prose", "bracket"]
    span: tuple[int, int]            # byte offsets for error reporting
    chash: str | None                 # set when kind == chash
    display: str                       # "as shown by Grossberg 2013" etc.
```

**Grounding validator** — shipped as `nx doc check-grounding` (separate subcommand in `src/nexus/commands/doc.py`, not bolted on to `render`):

```text
nx doc check-grounding <path>...
  --fail-under <ratio>              # e.g., 0.5 → fail if <50% of citations are chash-shaped
  --format table|json

  # --fail-ungrounded DEFERRED — depends on resolve_chash, see §v1 Scope Reduction
```

Reports per doc: total citations, chash-shaped, prose, bracketed. Coverage ratio = chash / total — a shape-only signal until `resolve_chash` ships.

**Author-extension validator** (new):

```text
nx doc check-extensions <path> --primary-source <collection>
  --threshold 0.70                  # projection-similarity cutoff
  --suggest                         # emit inline-edit suggestions
  --format table|json
```

**v1 limitation**: the command collects the unique `chash:` hex values from prose and passes them to `CatalogTaxonomy.chunk_grounded_in(doc_id, source_collection, threshold=...)`. That method queries `topic_assignments.doc_id`, which is populated with ChromaDB collection-scoped IDs (`knowledge__art:doc:chunk:0` shape) — a different namespace from content hashes. Every input therefore returns `None` (no data). The command emits a WARNING to stderr in that 100%-no-data case and the docstring marks it `[experimental]`. Meaningful candidate flagging is unblocked once `resolve_chash` ships — see §v1 Scope Reduction.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Chunk resolution | `src/nexus/catalog/catalog.py:resolve_chunk` | Extend with `resolve_chash` (RDR-078 already introduces chunk-level primitives) |
| Projection query | `src/nexus/db/t2/catalog_taxonomy.py` | Extend with `chunk_grounded_in(chunk_id, source_collection, threshold)` + `top_topics_for_collection(collection, top_n)` (the latter serves `{{nx-anchor:…}}`) |
| Markdown parse | RDR-082 parser in `src/nexus/doc/tokens.py` | Reuse for `chash:` span extraction (citations are markdown links, not tokens — different regex, same pass pattern) |
| Resolver registry | `src/nexus/doc/resolvers.py` (RDR-082) | Register `AnchorResolver` (namespace `nx-anchor`) at module import |
| `nx doc` command group | `src/nexus/commands/doc.py` (RDR-082) | Extend with three subcommands (`check-grounding`, `check-extensions`, plus `--expand-citations` on `render`) |
| Report formatting | `src/nexus/formatters.py` | Reuse |

### Decision Rationale

- Markdown-link-as-citation (`[text](chash:…)`) reuses existing syntax rather than inventing a new one; composes with every renderer.
- Splitting into three validators (`render`, `check-grounding`, `check-extensions`) preserves single-responsibility and lets projects opt in per-concern.
- Doing author-extension detection from `topic_assignments` exactly mirrors how a human auditor does it (the field report used projection evidence to critique ART); automating the same signal is the right leverage.
- Not adding a new rendered preview format in v1 — a standard markdown link with `chash:` scheme is enough to ship the grammar; downstream renderers can layer richer preview.

## Alternatives Considered

### Alternative 1: Custom citation grammar (non-markdown)

**Description**: Invent `@grounded(chash:…)` or similar non-markdown syntax.

**Pros**:
- Unambiguous; never confused with a real hyperlink.

**Cons**:
- Breaks every non-Nexus renderer; requires mandatory `nx doc render` to produce human-readable prose.
- No reason to fight markdown when it already supports custom schemes.

**Reason for rejection**: Adds cost with no compensating benefit. Markdown-link form composes cleanly.

### Alternative 2: Grounding as a T2 side-table instead of prose spans

**Description**: Maintain a separate "claim → chunk" mapping in T2; derive grounding from it.

**Pros**:
- Prose stays clean; mapping is structured.

**Cons**:
- Authoring ergonomics collapse — authors must edit a side file in lockstep with prose.
- Drift vector re-emerges (now prose vs. mapping).

**Reason for rejection**: The point is to ground *claims in prose*; the ergonomic home is next to the claim.

### Briefly Rejected

- **Hard-enforce 100% grounding**: unrealistic and wrong-tool-for-job; coverage is a ratcheting metric, not a gate.
- **LLM-based grounding inference** (have an agent decide "this sentence is grounded by that chunk"): useful as a suggestion tool later; v1 stays deterministic.
- **Require `chash:` on every claim**: too invasive; prose citations remain valid, just unvalidated until upgraded.

## Trade-offs

### Consequences

- Authors gain a verifiable citation form; projects that value rigor can track coverage.
- Renderer gains a chunk-lookup dependency; already a light DB call.
- Author-extension auto-flag changes the disclaimer rhythm — fewer manual flags, more tool-driven signal.
- Adoption is per-project and incremental; no forced migration of existing prose citations.

### Risks and Mitigations

- **Risk**: `chash:` references become stale if a chunking change invalidates hashes.
  **Mitigation**: RDR-053 fixes chunk boundaries and hash inputs. A future RDR must explicitly address hash-breaking changes; in the interim, `check-grounding` surfaces broken hashes as drift.
- **Risk**: Grounding coverage metric becomes vanity — projects optimize to the metric rather than to actual quality.
  **Mitigation**: Metric is advisory by default; fail-on-under is a CI opt-in. Document the intent (ratchet, not floor).
- **Risk**: Author-extension false positives (claim is grounded but projection threshold rejects).
  **Mitigation**: Threshold is configurable per project; validator is advisory (`--suggest`), not enforcing.
- **Risk**: Auto-flag suggestion text is wrong (inline edit that doesn't fit the prose).
  **Mitigation**: `--suggest` emits the flag-to-add and the line context; no automatic rewriting.

### Failure Modes

- Unknown `chash:` hash: `check-grounding` reports as broken; render preserves the markdown link (reader still sees display text).
- Projection data missing (project hasn't run `nx taxonomy project`): `check-extensions` reports "insufficient data" and exits cleanly; does not false-accuse.
- Citation in a code block: scanner respects fenced code; in-code `chash:` strings are not treated as citations.

## Implementation Plan

### Prerequisites

- [x] RDR-078 accepted + chunk-level primitives landed — status accepted 2026-04-14.
- [ ] RDR-082 merged — `nx doc` command group + Resolver registry exist.
- [ ] RDR-077 `source_collection` column populated (already accepted; backfill in progress) — required by `AnchorResolver` and `chunk_grounded_in`.
- [ ] All Critical Assumptions verified.

### Minimum Viable Validation

Add three `chash:` citations to a single Nexus RDR (say, this one), run `nx doc check-grounding`, and confirm: (a) resolved hashes report as grounded, (b) an intentionally-broken hash reports as broken, (c) coverage ratio prints correctly. `nx doc check-extensions` on an ART doc (if ART corpus reachable) reports at least one plausible author-extension candidate. Both must be executed in scope; not deferred.

### Phase 1: Code Implementation

#### Step 1: `chash:` span extension

- Add `resolve_chash(chash)` to catalog module.
- Implement `src/nexus/doc/citations.py` scanner.
- Unit tests: fixture markdown with chash/prose/bracket citation shapes.

#### Step 1b: `AnchorResolver` (nx-anchor token)

- Add `CatalogTaxonomy.top_topics_for_collection(collection, top_n)` SQL method.
- Implement `src/nexus/doc/resolvers_corpus.py` with `AnchorResolver` registering on RDR-082's Resolver registry at module import (namespace `nx-anchor`).
- Unit tests: fixture taxonomy with known projection rows; verify top-N selection and stable ordering.
- Verifies RDR-082's Resolver-registry extension point works as designed.

#### Step 2: `check-grounding` subcommand

- Wire into `src/nexus/commands/doc.py`.
- Emit per-doc report + coverage ratio.
- Unit + integration tests (real catalog fixture).

#### Step 3: `check-extensions` subcommand

- Add `CatalogTaxonomy.chunk_grounded_in()` SQL method.
- Wire into `src/nexus/commands/doc.py`.
- Unit + integration tests (fixture taxonomy with known projection rows).

#### Step 4: Renderer awareness (polish)

- `nx doc render` preserves `chash:` links; emits optional footnote block with resolved chunk excerpts when `--expand-citations` is passed.
- Does not block v1 — fallback is plain markdown rendering.

#### Step 5: Documentation + release notes

- Authoring guide: `docs/authoring-citations.md` (new).
- `docs/cli-reference.md`: `nx doc check-grounding`, `nx doc check-extensions`.
- `CHANGELOG.md` entry.

### Phase 2: Operational Activation

#### Activation Step 1: Dogfood citations in Nexus RDRs

- Pick 3 high-value RDRs; convert prose citations to `chash:` spans; run `check-grounding` and note coverage.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `chash:` references in prose | grep | `nx doc check-grounding` | Edit source | Re-run validator | Git |
| Grounding report | stdout / JSON | `--format json` | N/A (advisory) | Re-run | N/A |
| Projection data | T2 `topic_assignments` | `nx taxonomy project` | via T2 delete | Re-project | T2 backup |

### New Dependencies

None.

## Test Plan

- **Scenario**: Prose with 5 `chash:` citations, all resolvable — **Verify**: `check-grounding` coverage = 5/5, exit 0.
- **Scenario**: One broken `chash:` — **Verify**: reported as broken with file:line:col, exit non-zero under `--fail-ungrounded`.
- **Scenario**: Mixed citations (3 `chash:`, 4 prose) — **Verify**: coverage = 3/7 ≈ 0.43; `--fail-under 0.5` exits non-zero.
- **Scenario**: `check-extensions` against a doc whose chunks all project into `--primary-source` above threshold — **Verify**: zero author-extension candidates.
- **Scenario**: `check-extensions` against a chunk that does NOT project — **Verify**: candidate reported with suggested inline flag.
- **Scenario**: `chash:` inside fenced code block — **Verify**: scanner ignores; not counted as citation.
- **Scenario**: Renderer with `--expand-citations` on `chash:` spans — **Verify**: footnote block contains the cited chunk excerpt.
- **Scenario**: `{{nx-anchor:docs__nexus-rdrs|top=5}}` in a doc — **Verify**: renders as a markdown list of the top 5 projected topics ordered by `SUM(similarity) DESC`.
- **Scenario**: `{{nx-anchor:<collection>|top=5}}` where the collection has <5 projected topics — **Verify**: resolver returns what exists (3/4/5), does not pad.
- **Scenario**: `{{nx-anchor:<collection>}}` on a collection with no projection data — **Verify**: resolver raises `ResolutionError`; render fails with clear pointer to `nx taxonomy project`.

## Validation

### Testing Strategy

1. **Unit** — scanner grammar, resolver fallback, grounding arithmetic, extension-threshold logic.
2. **Integration** — real catalog + real T2 fixture; both validators run end-to-end against a sample doc.
3. **Regression** — render of a doc with no citations byte-equal pre/post patch (scanner doesn't mangle).
4. **Adoption smoke** — manually author 5 `chash:` citations in one Nexus RDR; validate; commit.

### Performance Expectations

`check-grounding`: dominated by `resolve_chash` lookups (one per span). Target <2s per doc. `check-extensions`: dominated by one SQL per doc-level chunk. Target <5s per doc.

## Finalization Gate

### Contradiction Check

To be filled at gate.

### Assumption Verification

To be filled at gate.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `catalog.resolve_chash` | `nexus.catalog.catalog` | Source Search (extending existing `resolve_chunk`) |
| `topic_assignments` SELECT | SQLite | Source Search |
| `Resolver` registry hook | `nexus.doc.resolvers` (RDR-082) | Source Search after 082 ships; `AnchorResolver` must register without modifying parser/engine/CLI |

### Scope Verification

To be filled at gate.

### Cross-Cutting Concerns

- **Versioning**: Citation scanner has a grammar version; broken-hash failures include the grammar version in the error report.
- **Build tool compatibility**: N/A
- **Licensing**: N/A
- **Deployment model**: CLI only
- **IDE compatibility**: N/A — `chash:` URLs are inert in standard editors.
- **Incremental adoption**: Fully opt-in; prose citations remain valid, just unvalidated.
- **Secret/credential lifecycle**: N/A
- **Memory management**: Per-render cache bounded by document chunk count.

### Proportionality

Three cooperating validators atop one new prose primitive. Resist LLM-based grounding inference in v1; deterministic signal first.

## References

- `docs/field-reports/2026-04-15-architecture-as-code-from-art.md` §3.1 (F3 anchor), §3.2 (F5, F6, F8), §3.3 (F11)
- RDR-053 (Xanadu fidelity — chunk hash stability)
- RDR-075 (cross-collection projection)
- RDR-077 (projection quality — `source_collection`, similarity)
- RDR-078 (plan-centric retrieval — chunk-level primitives)
- RDR-082 (doc render / `nx doc` command group — Resolver registry extended here)

## Revision History

- 2026-04-15 — Draft authored from ART field report findings F5, F6, F11.
- 2026-04-16 — Scope expansion: absorbed `{{nx-anchor:…}}` corpus-shape token (originally scoped to RDR-082) and Gap 4 (empirical collection shape). 083 now owns every rendering surface that depends on projection data; 082 stays on system-of-record tokens only. Title retyped "Chunk-Grounded Citations" → "Corpus-Evidence Tokens" to reflect the broader scope.
