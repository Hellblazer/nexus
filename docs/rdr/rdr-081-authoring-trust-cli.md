---
title: "RDR-081: Stale-Reference Validator (nx taxonomy validate-refs)"
id: RDR-081
status: closed
type: Feature
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-15
revised: 2026-04-16
accepted_date: 2026-04-16
closed_date: 2026-04-16
close_reason: implemented
related_issues: []
related: [RDR-075, RDR-077, RDR-080, RDR-085]
supersedes_drafts: [rdr-081-2026-04-15-labeler-pool]
---

# RDR-081: Stale-Reference Validator

Prose documentation under `docs/` routinely asserts things like
`"12,900 chunks from Grossberg papers indexed in knowledge__art"`. These
claims are accurate when written. A later collection rename/split/merge
silently invalidates every such pointer. A reader or agent following the
instruction queries the wrong collection and gets incomplete results.
Nothing in Nexus flags the drift, even though the data needed to detect
it (`collection_list`, `count()`) is readily available.

This RDR adds a deterministic, LLM-free CLI — `nx taxonomy validate-refs
<path>...` — that scans markdown for collection references and proximate
chunk-count claims, compares against current T3 state, and reports drift.

## Scope note (2026-04-16 revision)

An earlier draft bundled two authoring-trust gaps into one RDR:

1. **Stale-reference validator** (this RDR — shipped).
2. **Glossary-aware topic labeler** (deferred).

The labeler revision was written against RDR-079's operator-pool
infrastructure (warm workers, `get_operator_pool`, `dispatch_with_rotation`).
RDR-079 was abandoned; RDR-080 shipped the simpler `claude_dispatch` one-shot
subprocess path instead. The glossary-aware labeler work still has merit
but needs redesign against the current substrate. That work is filed as a
follow-up — not blocking this RDR.

Scope reduction rationale: the validator is **fully deterministic** (pure
regex + SQL/ChromaDB count calls; no LLM), **independent** of any planned
LLM infrastructure changes, and closes a concrete documented failure
(field-report §3.1/F1). Shipping it standalone respects the "one branch,
one arc" convention without waiting for the labeler substrate decision.

## Problem Statement

### Enumerated gaps to close

#### Gap 1: Prose references to collections and chunk counts drift silently

Markdown docs under `docs/` routinely assert things like `"12,900 chunks
from Grossberg papers indexed in knowledge__art"`. These claims were
accurate when written. A later collection rename/split/merge (ART
split papers out of `docs__ART-8c2e74c0` into `docs__art-grossberg-papers`)
silently invalidates every such pointer. A reader or agent following the
instruction queries the wrong collection and gets incomplete results.
Nothing in Nexus flags the drift, and the `collection_list` / projection
data needed to detect it is already persisted in T2/T3.

#### Gap 2: No CI-friendly advisory for doc-to-infrastructure drift

Drift detection today requires manual grep + human review during a
full-collection audit. There is no zero-cost advisory a CI pipeline or
pre-commit hook can run to surface the problem at the moment docs change.

## Context

### Background

The 2026-04-15 ART field report (§3.1/F1) documented a rebuild where
`docs/architecture/primary-sources.md` line 3 asserted a collection-
chunk-count pair that had been invalidated by a subsequent split. Nexus
had all the data needed to flag it — but no surface that put the data
to work at authoring time. The validator is that surface.

### Technical Environment

- Python 3.12+, Click CLI.
- Projection assignments persisted in T2 `topic_assignments` (RDR-075)
  with similarity + `source_collection` (RDR-077) — available for future
  validator extensions (drift via projection signal, F12 in the field
  report).
- Collection listing via `registry.list_sibling_collections()`,
  `collection_list` MCP tool, and the T3 `chromadb.Collection.count()`.

## Research Findings

### RF-1 — Prefix-set coverage for collection references

Direct source search across `docs/*.md` confirms the prefix set
`docs__|code__|knowledge__|rdr__` captures all user-facing collection
references. Spot evidence: `docs__art-architecture`, `code__myrepo`,
`knowledge__delos`, `rdr__ART-8c2e74c0`, `docs__default`, `docs__nexus`.
38 occurrences of internal-prefix names (`taxonomy__centroids`,
`plans__session`) across 8 files — these are implementation-fixed
collection names that do not rename and are correctly excluded.

**Refinement**: make the prefix set config-driven (`taxonomy.collection_prefixes`,
default `[docs, code, knowledge, rdr]`) so a future new user-facing
prefix does not silently bypass the scanner.

### Critical Assumptions

- [x] Prefix whitelist is the correct scanner contract — **Verified** (RF-1).
- [x] Chunk-count extraction with ±10% tolerance is sufficiently precise
  to surface real drift without high false-positive rate — **Verified
  by MVV 2026-04-16**: dogfood on `docs/rdr/README.md` +
  `docs/architecture.md` on clean main reported "No references found"
  (zero false positives); fixture file claiming 50 chunks against a
  collection with actual count 134 correctly reported `Drift` with
  exit code 1; fixture claims "134 chunks" (exact) and "140 chunks"
  (within ±10%) both reported `OK`. Evidence: commit `2ab7845`, live
  sandbox runs.

## Proposed Solution

### Approach

One deterministic command, no new LLM infrastructure:

```
nx taxonomy validate-refs <path>...
  --tolerance 0.10          # chunk-count match window (default 10%)
  --strict                  # also flag unresolved refs (not just drifted)
  --prefixes docs,code,knowledge,rdr   # whitelist override (default from config)
  --format table|json       # report shape
```

### Algorithm (per file)

1. Parse markdown, **respecting fenced code blocks** (do not scan inside ``` … ``` or ~~~~).
2. Regex-scan for collection names matching `(<prefix>)__<name>` using the
   configured prefix whitelist.
3. Within the same paragraph as each reference, scan for integer
   chunk-count claims (patterns like `"12,900 chunks"`, `"~13k chunks"`,
   `"about 13000 chunks"`).
4. For each reference, query current T3 state:
   - `collection_list()` → does the collection exist?
   - `collection.count()` → does the cited count match (±tolerance)?
5. Emit one row per reference with verdict: `OK` | `Drift` | `Missing`,
   carrying file:line, claimed-count, actual-count, delta.

### Exit codes

- `0` — every scanned reference is `OK` (or `Missing` without `--strict`).
- `1` — at least one `Drift` found, or `Missing` with `--strict`.
- `2` — scanner failure (I/O, regex compilation, T3 unavailable).

### Technical Design

**New module** `src/nexus/doc/ref_scanner.py`:

```
# Illustrative — verify interfaces during implementation
def scan_markdown(path: Path, prefixes: list[str]) -> list[Reference]:
    """Parse markdown respecting fenced code blocks; return every
    collection reference found with its line number and the claimed
    chunk-count if one appears in the same paragraph."""

def validate(refs: list[Reference], t3_db) -> list[Drift]:
    """Look up current state for each ref; return verdicts."""
```

**New CLI subcommand** under `nx taxonomy`:

```python
@taxonomy.command("validate-refs")
@click.argument("paths", nargs=-1, type=click.Path(exists=True, ...))
@click.option("--tolerance", default=0.10, type=float)
@click.option("--strict", is_flag=True, default=False)
@click.option("--prefixes", default="", help="Comma-separated override")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
def validate_refs(paths, tolerance, strict, prefixes, fmt):
    ...
```

**Config key** (additive to existing `.nexus.yml` schema):

```yaml
taxonomy:
  collection_prefixes: [docs, code, knowledge, rdr]
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
|---|---|---|
| Reference scanner | none | **New** — `src/nexus/doc/ref_scanner.py` |
| Collection-existence check | `src/nexus/db/t3.py` `T3Database.list_collections()` | **Reuse** |
| Chunk-count query | `chromadb.Collection.count()` | **Reuse** |
| CLI surface | `src/nexus/commands/taxonomy_cmd.py` | **Extend** with `validate-refs` subcommand |
| Config loader | `src/nexus/config.py` | **Extend** with `taxonomy.collection_prefixes` |

### Decision Rationale

- **Deterministic** — drift detection needs no language model. `is this
  collection name in current T3?` is a lookup. Keeping it deterministic
  makes it safe to run on every commit / CI run with zero cost or auth
  requirement.
- **Config-driven prefix set** — avoids a future migration when a new
  user-facing prefix is added. Default covers today's four.
- **Fenced-code-block respect** — scanner must ignore `docs__foo` inside
  ``` … ``` or it false-positives on every tutorial / how-to document.
- **Narrow advisory scope** — the command reports drift; it does **not**
  auto-rewrite references. Auto-rewrite is too destructive without human
  review. Blocking commits on drift is deferred to a future RDR-083.

## Alternatives Considered

### Alternative 1: Bundle with glossary-aware labeler (original RDR-081 draft)

**Description**: Ship labeler pool migration + glossary injection + validator
together.

**Pros**: Two authoring-trust surfaces arrive together.

**Cons**: Labeler revision depended on RDR-079 operator pool which was
abandoned. Redesigning the labeler against `claude_dispatch` (the shipped
RDR-080 path) is additional scope without clear cost envelope. Blocking
the validator on that decision would indefinitely delay a concrete
self-contained fix.

**Reason for rejection**: Ship the deterministic, independent piece now;
file the labeler work as a follow-up.

### Alternative 2: Embedding-based drift detection (F12 in field report)

**Description**: Use projection ICF to detect "topic has shifted to a
different collection"; alert at query time rather than authoring time.

**Pros**: Uniform signal across all authoring-trust domains.

**Cons**: Requires re-running projection jobs; query-time overhead;
bigger surface; later-stage detection (drift already in docs when query
fires). The F1 workflow is simpler — the facts are literally in the
text, the lookup is `count() == claim?`.

**Reason for rejection**: Disproportionate to the concrete F1 failure.
Validator addresses F1 directly; F12 can layer on later.

### Briefly Rejected

- **Auto-rewrite drifted references**: too destructive without human
  review; validator reports drift and author fixes in their normal
  editor.
- **Block commits on drift**: deferred to RDR-083 — validator lands
  first as advisory, adoption is opt-in, CI integration comes after the
  baseline FP rate is measured on real documentation.

## Trade-offs

### Consequences

- New deterministic authoring tool; zero runtime cost except during
  validation runs; no auth or credentials required beyond reading the
  local T3 collection metadata.
- Reference validator becomes the canonical way to check doc-to-infrastructure
  alignment. Future RDRs layering CI integration (RDR-083) or
  auto-rewrite can compose on top.

### Risks and Mitigations

- **Risk**: Reference scanner false positives on code-block content
  (`docs__foo` inside a fenced block).
  **Mitigation**: Scanner respects fenced code blocks; in-code matches
  are not counted. Test case pins this behavior.
- **Risk**: Prefix set expands (new user-facing collection prefix) and
  scanner silently misses.
  **Mitigation**: RF-1 refinement — prefix set is config-driven.
- **Risk**: Chunk-count tolerance too tight/loose.
  **Mitigation**: `--tolerance` is a CLI flag; default 10%; operators
  can tune per-project.
- **Risk**: Scanner runs against a T3 that isn't provisioned (sandbox /
  fresh install) and every ref looks `Missing`.
  **Mitigation**: `--strict` is opt-in; default treats `Missing` as
  informational (not an exit-1 fail) so running the tool in a fresh
  sandbox doesn't break CI.

### Failure Modes

- T3 unavailable / `collection_list()` fails → exit 2 with clear error
  message (not confused with exit 1 for drift found).
- Invalid markdown (unparseable front-matter, malformed code fences) →
  scanner emits a warning for the file and continues; remaining refs
  in valid files still reported.
- Regex catastrophically matches on a giant single-line doc → scanner
  uses linear regex only; no backtracking hazards.

## Implementation Plan

### Prerequisites

- [x] RDR-075 + RDR-077 shipped (not strictly required for Gap 1 but
  used by the future F12 extension).
- [x] `T3Database.list_collections()` + `Collection.count()` stable APIs.

### Minimum Viable Validation

On a clean checkout: register prefix whitelist in `.nexus.yml` (default
values are fine); run `nx taxonomy validate-refs docs/rdr/README.md
docs/architecture.md` and verify **zero drifts reported**. Then run
against a fixture doc referencing a non-existent collection, verify
**drift reported with file:line, claimed, actual**.

### Phase 1: Code Implementation

#### Step 1: Config + scanner module

- Add `taxonomy.collection_prefixes` (list, default `[docs, code,
  knowledge, rdr]`) to `.nexus.yml` schema.
- Implement `src/nexus/doc/ref_scanner.py`:
  - `scan_markdown(path, prefixes) -> list[Reference]`
  - `validate(refs, t3_db, tolerance) -> list[Drift]`
  - Fenced-code-block aware tokenizer (trivial state machine:
    toggle-on-``` or `~~~~`).
- Unit tests: fixtures with OK / drifted / missing / in-code /
  ambiguous refs; prefix override; tolerance boundary.

#### Step 2: CLI subcommand

- Register `validate-refs` under the existing `nx taxonomy` click group.
- Table formatter (default) + JSON formatter (`--format json` for CI).
- Exit-code contract documented in `--help`.

#### Step 3: Documentation + release notes

- `docs/cli-reference.md` — new subcommand section.
- `docs/configuration.md` — new `taxonomy.collection_prefixes` key.
- `docs/taxonomy.md` — reference-validation section.
- Entry in `CHANGELOG.md`.

### Phase 2: Operational Activation

#### Activation Step 1: Dogfood on Nexus docs

Run `nx taxonomy validate-refs docs/**/*.md` on the current repo. Expect
zero drifts on clean main (any failure reveals either a scanner bug or
a real drift — both worth fixing).

### Day 2 Operations

| Resource | List | Info | Modify | Verify |
|---|---|---|---|---|
| Prefix whitelist | `.nexus.yml` read | `nx config list` | Edit `.nexus.yml` | `validate-refs` on known-good doc |
| Reference-scan results | `validate-refs` output | `--format json` | N/A (advisory) | Re-run |

### New Dependencies

None. Reuses existing SQLite + ChromaDB client + stdlib `re`.

## Test Plan

- **Scenario**: `validate-refs` against a doc with known-good references
  → zero drifts, exit 0.
- **Scenario**: `validate-refs` against a doc with a renamed collection
  → drift reported with file:line, claimed, actual.
- **Scenario**: `validate-refs` with `--strict` on doc with unresolvable
  reference → reports `Missing`, exits non-zero.
- **Scenario**: Chunk-count within tolerance → `OK`. Outside tolerance
  → `Drift` with delta.
- **Scenario**: `docs__foo` inside a fenced code block → scanner ignores.
- **Scenario**: `docs__foo` inside a `~~~` fence → scanner ignores.
- **Scenario**: Custom prefix added to `taxonomy.collection_prefixes`
  → scanner matches it.
- **Scenario**: Multiple references in the same paragraph, only one has
  a chunk-count claim → claim is associated with the closest matching
  reference (same paragraph, nearest preceding/following ref).
- **Scenario**: T3 unavailable → exit 2 with clear error, not exit 1.
- **Scenario**: `--format json` → machine-parseable output with stable
  field names (`path`, `line`, `collection`, `claimed_count`,
  `actual_count`, `verdict`, `delta`).

## Validation

### Testing Strategy

1. **Unit** — glossary-free scanner regex coverage, fenced-code-block
  respect, chunk-count arithmetic, tolerance boundary cases.
2. **Integration** — against a real `T3Database` (local mode is enough),
  fixture collection created with `N` chunks, claim within and outside
  tolerance, verify reported drift.
3. **Regression** — existing `nx taxonomy` subcommands unaffected.
4. **Manual smoke** — `validate-refs docs/rdr/README.md docs/architecture.md`
  on clean checkout → zero drifts.

### Performance Expectations

Sub-second per document. Linear in file size (regex scan) + constant
per distinct collection reference (one `list_collections()` call cached,
plus one `count()` per referenced collection).

## Finalization Gate

### Contradiction Check

No contradictions after the scope reduction: the RDR now describes a
single deterministic CLI with no LLM dependency. The RDR-079/pool
material from the earlier draft was the only source of contradictions
(stale after RDR-079 abandonment) and has been removed.

### Assumption Verification

- A1 (prefix whitelist) — RF-1, verified via source search.
- A2 (tolerance envelope) — **Verified** by MVV 2026-04-16 (see
  Critical Assumptions § A2 above).

#### API Verification

| API Call | Library | Verification |
|---|---|---|
| `T3Database.list_collections()` | `chromadb` | Source Search |
| `Collection.count()` | `chromadb` | Source Search |
| Fenced-code-block regex | stdlib `re` | Source Search — stdlib behaviour |

### Scope Verification

MVV is in scope: validate-refs reports drift when given a test file
referencing a non-existent collection and zero drifts on clean main.
Both executed during Step 2.

### Cross-Cutting Concerns

- **Versioning**: No T2/T3 schema change. Config key additive.
- **Build tool compatibility**: N/A
- **Licensing**: N/A (no new deps)
- **Deployment model**: Part of `nx` CLI; no new process.
- **Incremental adoption**: Fully opt-in — no new keys required in
  `.nexus.yml`; prefix default is hardcoded.
- **Memory management**: Single-file pipeline; bounded memory.

### Proportionality

One subcommand, one scanner module, one config key. Resist adding a
`nx doc` command group or per-operator model overrides — scoped out
to future RDRs.

## References

- `docs/field-reports/2026-04-15-architecture-as-code-from-art.md`
  §3.1/F1 — original failure report
- RDR-075 (cross-collection projection)
- RDR-077 (`source_collection` column)
- RDR-080 (retrieval-layer consolidation — `claude_dispatch` substrate
  the deferred labeler work will target)

## Follow-up (out of scope for this RDR)

- **Glossary-aware topic labeler.** Original RDR-081 Gap 1. Filed as
  **RDR-085** (glossary-aware topic labeler, re-targeted at the
  shipped `claude_dispatch` substrate per RDR-080 rather than the
  abandoned RDR-079 pool). Concrete failure case documented in field
  report §3.1/F2 (SSMF → "Single Mode Fiber").
- **RDR/bead status as doc-build tokens.** Field report §3.1/F3 — separate
  concern, separate RDR.
- **Notation linter.** Field report §3.1/F4 — separate concern.
- **CI integration + auto-rewrite.** Layered on top of this validator;
  deferred to RDR-083.
- **ICF-based stale-reference auto-detection at query time.** Field
  report §3.2/F12 — uses the projection infrastructure; later-stage
  detection; orthogonal to this authoring-time check.

## Revision History

- 2026-04-15 — Draft authored from ART field report findings F1, F2 +
  labeler model upgrade.
- 2026-04-15 — Revised to leverage RDR-079 operator pool: labeler migrates
  to `operator_extract` with `--json-schema` enforcement; RF-1/RF-2/RF-3
  recorded.
- 2026-04-15 — Gate round 1: BLOCKED on stale `params` reference,
  glossary off-by-one, async/sync bridge. Fix revision committed to
  dedicated `"labeler"` pool strategy.
- **2026-04-16 — Scope reduced.** RDR-079 was abandoned; RDR-080 shipped
  `claude_dispatch` instead of the operator pool. The labeler-migration
  portion of this RDR (Gaps 1 + 3 of the original draft) assumed
  infrastructure that never shipped. Scope narrowed to the deterministic
  stale-reference validator (original Gap 2). The labeler work is filed
  as a follow-up. RF-2 and RF-3 were scoped to the deferred labeler work
  and are omitted from this revision; RF-1 (prefix whitelist) remains
  load-bearing.
