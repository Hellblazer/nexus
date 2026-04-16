---
title: "RDR-082: Doc-Build Token Resolution — `nx doc render` with Bead/RDR/Anchor Tokens"
id: RDR-082
status: draft
type: Feature
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-15
related_issues: []
related: [RDR-075, RDR-077, RDR-078, RDR-081]
---

# RDR-082: Doc-Build Token Resolution

Architecture and design docs routinely hard-code values that Nexus already owns authoritatively: bead statuses (`Phase 3 = COMPLETE`), RDR states (`RDR-072 ACCEPTED`), component wiring claims (`SovereignDialogPipeline WIRED`), and empirical cross-collection topic anchors. Every one requires manual update when the underlying truth moves. The ART field report (2026-04-15, §3.1/F3 and §3.2/F8) documents multiple drift cases — superseded `MaskingFieldCompetition`, RDR-061 status ambiguity, dead-code references to `SemanticResponseField` that was in fact wired. We have all the source data (bead DB, RDR frontmatter, T2 `topic_assignments`); we lack a renderer that expands authored tokens at build time. This RDR introduces `nx doc render` as the minimal CLI surface and defines a small, versioned token grammar (`{{bd:…}}`, `{{rdr:…}}`, `{{nx-anchor:…}}`) resolved from existing stores.

## Problem Statement

### Enumerated gaps to close

#### Gap 1: Bead and RDR status claims in prose go stale on every state transition

Every architecture doc that names a bead or RDR embeds a status snapshot. When the bead closes, the RDR accepts, or the work gets superseded, the prose lies until someone notices. The ART session found three such drifts in ten files during one audit; scaled across all downstream projects, this is the dominant doc-drift class. We already have the state machines (`bd` DB, RDR frontmatter + `{repo}_rdr` T2 project); the missing primitive is a token syntax in markdown that expands at render time.

#### Gap 2: Docs cannot surface their own empirical semantic shape

A reader of `docs__art-architecture` cannot tell from the prose that the collection's semantic center of mass is "Semantic Vector Topic Clustering" at 0.75 cosine — they would have to run projection and ICF by hand. RDR-077's projection-quality data makes this answerable in a single query, but there is no rendering convention that embeds the answer into a doc. Readers see only the *claimed* shape, never the *empirical* shape.

#### Gap 3: No machine-checkable contract for render correctness

Even if tokens were defined ad-hoc, a doc with an unknown or malformed token should fail loudly at render, not fall through to literal text in published output. A validator step (parse → resolve → error on unresolved) is part of the same primitive, not a separate surface.

## Context

### Background

RDR-081 shipped a scanner for collection-name drift in prose. This RDR is the natural next layer: instead of scanning hand-written references and flagging drift, we let authors express the reference as a token, and the renderer resolves it from authoritative state on every build. The two are complementary — `validate-refs` handles legacy prose; tokens handle new authoring. ART's disciplines in §6 of the field report (explicit §0 notation block, per-layer canonical files, hand-curated tumblers) are exactly the patterns that benefit: ceremonial boilerplate today, token-resolved at build-time after this RDR.

Bead state lives in `.beads/` (SQLite, `bd show <id>` JSON output). RDR state lives in `docs/rdr/<id>.md` frontmatter and is mirrored in T2 `{repo}_rdr` (memory_get by title). Projection-anchor data lives in T2 `topic_assignments` (enriched by RDR-077). All are queryable today; none are surfaced through a markdown-author-friendly API.

### Technical Environment

- Python 3.12+, Click CLI, `tomllib`, existing `structlog`.
- `bd` CLI + SQLite DB at `.beads/beads.db`.
- T2 facade `T2Database` with `MemoryStore` (RDR access) and `CatalogTaxonomy` (projection data).
- Markdown rendered as-is by common downstream consumers (GitHub, `mkdocs`, local editors). The renderer produces a `.rendered.md` sibling, not in-place.

## Research Findings

### Investigation

- Surveyed current ad-hoc drift patterns in `docs/rdr/*.md` and `docs/architecture*.md`: most drift clusters around three token types (bead status, RDR status, collection/topic anchors). Other long-tail patterns (chunk counts, call-graph fragments, file-path lists) are out of scope — they belong to the chunk-citation work (RDR-083) or symbol-graph work (F9).
- Verified `bd show <id> --json` exposes `status`, `title`, `assignee`, `closed_at`, `epic_id`, `progress` in stable fields.
- Verified RDR T2 records (RDR-081 Step 4 precedent in `nx/skills/rdr-create/SKILL.md`) contain `status`, `gated`, `closed`, `close_reason`, `epic_bead`.
- Verified projection anchor query is a bounded SQL: top-K topics by `SUM(similarity)` grouped by `topic_id` for a given `source_collection`.

### Key Discoveries

- **Verified** — Three token families cover the observed drift cases in the field report with no residual unhandled.
- **Documented** — `bd show --json` and `memory_get` both return structured records today; no new exporters needed.
- **Documented** — RDR-078 (plan-centric retrieval) introduces `follow_links` but is orthogonal to rendering; no coupling.
- **Assumed** — A two-phase renderer (parse → resolve → emit) is sufficient; nested tokens are not needed for the in-scope cases. **Verification**: sample-set pass through a prototype.

### Critical Assumptions

- [ ] The token grammar `{{NAMESPACE:KEY[.FIELD][|FILTER]}}` covers all three in-scope token families without special-casing — **Status**: Unverified — **Method**: Prototype + sample docs.
- [ ] Rendering can be fully synchronous (no web calls) on a typical 100-page doc in <5s — **Status**: Unverified — **Method**: Spike on `docs/rdr/` corpus.
- [ ] Downstream markdown consumers (GitHub, mkdocs) treat `{{…}}` as literal text when unrendered, so an un-rendered source doc does not silently look wrong — **Status**: Verified by inspection — **Method**: Docs Only.

## Proposed Solution

### Approach

A single new command `nx doc render <path>` that expands tokens from a small, versioned grammar. Three token families ship in v1; grammar is extensible so RDR-083 (chunk citations) can add `{{chash:…}}` without schema churn.

- `{{bd:<id>.<field>}}` — bead state: `.status`, `.title`, `.assignee`, `.epic.progress`.
- `{{rdr:<id>.<field>}}` — RDR state: `.status`, `.title`, `.gated`, `.closed`.
- `{{nx-anchor:<collection>[|top=N]}}` — top-N projected topics for the collection, rendered as a markdown list.

Author writes tokens inline; `nx doc render` emits a resolved `.rendered.md` sibling; `nx doc validate` (same engine, `--no-emit`) exits non-zero on any unresolved token. Default is fail-loud — an unknown bead ID or collection fails the render.

### Technical Design

**Token grammar** (v1):

```text
{{NAMESPACE:KEY[.FIELD][|FILTER=VALUE]*}}

namespaces: bd | rdr | nx-anchor
key:        bead id | rdr id | collection name
field:      dotted path into resolver output (optional; resolver supplies default)
filter:     resolver-specific directive, e.g., top=5
```

**Resolver protocol** (new, `src/nexus/doc/resolvers.py`):

```text
# Illustrative — verify interfaces during implementation
class Resolver(Protocol):
    namespace: ClassVar[str]
    def resolve(self, key: str, field: str | None, filters: dict[str, str]) -> str: ...
    # raises ResolutionError on unknown key / unsupported field
```

Three resolvers ship in v1, each small:

- `BeadResolver` shells `bd show <id> --json`, indexes fields.
- `RdrResolver` reads `docs/rdr/rdr-<id>-*.md` frontmatter (authoritative) with T2 `memory_get` fallback.
- `AnchorResolver` runs a bounded SQL over `topic_assignments` filtered by `source_collection`, ordered by `SUM(similarity) DESC`.

**Renderer** (new, `src/nexus/doc/render.py`):

```text
nx doc render <path>... [--out DIR] [--fail-on-unresolved] [--format md|html]
nx doc validate <path>...                # alias: render --no-emit --fail-on-unresolved
```

Pipeline: read markdown → regex-tokenize with a single pass (`\{\{([a-z-]+):([^}|]+)(?:\.[^}|]+)?(?:\|[^}]+)?\}\}`) → for each token, look up namespace→resolver, call `resolve()` → substitute. Unknown namespace / unresolved key → raise `ResolutionError` collected into a report.

**Output convention**: `<path>.rendered.md` by default; `--out DIR` writes to a mirror tree. The source `.md` is never modified.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Bead lookup | `bd` CLI subprocess | Reuse via `subprocess.run` — same pattern as labeler in RDR-081 |
| RDR frontmatter read | `src/nexus/commands/rdr.py` (if exists) or inline YAML parse | Implement inline with `tomllib`/`pyyaml` — trivial |
| Projection anchor SQL | `src/nexus/db/t2/catalog_taxonomy.py` | Extend with `top_topics_for_collection(collection, top_n)` method |
| Token parser | none — new | New module |
| CLI surface | `src/nexus/commands/doc.py` | New command group |

### Decision Rationale

- Pre-defined three-namespace grammar avoids bikeshedding and covers the observed drift cases with zero waste. Extension requires writing a new Resolver, not grammar changes.
- Renderer over sidecar output (`<path>.rendered.md`) preserves source-as-truth discipline — diffing, review, and version control all stay clean.
- Bead access via subprocess matches RDR-081's labeler pattern; no new `bd` client library dependency.
- AnchorResolver reuses RDR-077's `source_collection` column directly; zero new schema.

## Alternatives Considered

### Alternative 1: Runtime template engine (Jinja2 / Mustache)

**Description**: Use an off-the-shelf template engine; author writes `{{ bead('ART-xxx').status }}`.

**Pros**:
- Familiar syntax; batteries included.

**Cons**:
- Jinja macro expansion is Turing-complete; invites logic in docs.
- Binding resolvers to Jinja globals is more code than writing our own 30-line pass.
- We end up restricting the grammar anyway, losing the "familiar" benefit.

**Reason for rejection**: We want a small, auditable, non-executing grammar. Restricted syntax is a feature.

### Alternative 2: In-place rewrite (no sidecar)

**Description**: Render modifies the source markdown directly.

**Pros**:
- No two-file drift; readers on GitHub see rendered output.

**Cons**:
- Source is no longer authoritative; edits-to-rendered vs. edits-to-source become ambiguous.
- Every render is a git diff.

**Reason for rejection**: Loses the property that source files are canonical.

### Briefly Rejected

- **MCP tool instead of CLI**: rendering is a build step, not a chat action. CLI is the right surface.
- **Pre-commit hook as v1**: hooks are an adoption pattern, not the primitive. Ship the primitive; hooks are a downstream choice.

## Trade-offs

### Consequences

- Authors get a small vocabulary to learn (three token forms). Unused by projects that don't opt in.
- `.rendered.md` files proliferate if committed; gitignore template recommendation ships with the RDR.
- Subprocess shell-out to `bd` per token is fine at author-time; batched lookup is a later optimization if needed.

### Risks and Mitigations

- **Risk**: Resolver latency (N tokens × bead subprocess) feels slow on large docs.
  **Mitigation**: Per-process cache keyed on `(namespace, key)` — resolve each unique reference once per render.
- **Risk**: Authors commit `.rendered.md` into VCS and then diverge from source.
  **Mitigation**: Ship a `.gitignore` snippet (`*.rendered.md`) documented in `docs/cli-reference.md`; renderer does not create `.gitignore`.
- **Risk**: Resolver output contains markdown-unsafe characters (pipe in a title breaks tables).
  **Mitigation**: Resolvers emit pre-escaped text by default; filter syntax allows opt-out (`|raw=true`) for authors who know what they're doing.

### Failure Modes

- Unknown bead ID: `ResolutionError`; render fails non-zero unless `--allow-unresolved` (not recommended; default fail-loud).
- Stale bead DB (session hasn't pulled): resolver returns whatever `bd show` returns; the `bd prime` convention governs freshness.
- Malformed token (`{{bd:ART-xxx|}}`): parser rejects at parse time with line/column location.

## Implementation Plan

### Prerequisites

- [ ] RDR-077 `source_collection` column populated (already accepted; backfill in progress) — for AnchorResolver.
- [ ] RDR-081 merged — not a hard dep, but parallel work should land first to avoid merge conflicts in `src/nexus/doc/`.

### Minimum Viable Validation

`nx doc render docs/rdr/rdr-078-unified-context-graph-and-retrieval.md` on a copy that has been edited to include one token of each family (`{{bd:nexus-xxx.status}}`, `{{rdr:072.status}}`, `{{nx-anchor:docs__nexus-rdrs|top=5}}`) produces a `.rendered.md` with all three resolved correctly. Must be executed in scope; not deferred.

### Phase 1: Code Implementation

#### Step 1: Token parser + resolver protocol

- Implement `src/nexus/doc/tokens.py` with `parse_tokens(text) -> list[Token]`.
- Define `Resolver` protocol in `src/nexus/doc/resolvers.py`.
- Unit tests: grammar coverage, malformed rejection.

#### Step 2: Three v1 resolvers

- `BeadResolver` (subprocess `bd show --json`; cache per render).
- `RdrResolver` (frontmatter read; cache per render).
- `AnchorResolver` (SQL on `topic_assignments`; new `CatalogTaxonomy.top_topics_for_collection()`).
- Unit tests per resolver: mock bead/fs/db.

#### Step 3: Render engine + CLI command

- Implement `src/nexus/doc/render.py` with `render_file(path, resolvers, opts)`.
- Implement `src/nexus/commands/doc.py` with `render` and `validate` subcommands; register `doc` group in `cli.py`.
- Unit tests: golden-file tests on small fixtures.

#### Step 4: Documentation + release notes

- `docs/cli-reference.md` — `nx doc render` and `nx doc validate` reference.
- `docs/contributing.md` — authoring guide section on tokens + `.gitignore` snippet.
- `CHANGELOG.md` entry.

### Phase 2: Operational Activation

#### Activation Step 1: Dogfood on nexus docs

- Migrate ~3 high-drift docs in `docs/rdr/README.md` + `docs/architecture.md` to use token references; establish the rendering pattern.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Rendered sidecar | `ls *.rendered.md` | diff against source | `rm` | Re-render | Source is backup |
| Resolver cache | In-process only | N/A | N/A | N/A | N/A |
| Projection anchors | T2 `topic_assignments` | `nx taxonomy project` | via T2 delete | Re-run projection | Via T2 backup |

### New Dependencies

None.

## Test Plan

- **Scenario**: Render doc with 3 bead tokens, 2 RDR tokens, 1 anchor token — **Verify**: all 6 resolved, `.rendered.md` byte-exact against golden.
- **Scenario**: Unknown bead ID — **Verify**: `nx doc render` exits non-zero; reports `file:line:col`.
- **Scenario**: Malformed token `{{bd:|}}` — **Verify**: parser rejects with clear error.
- **Scenario**: Collection with <5 projected topics, `top=5` requested — **Verify**: resolver returns what exists (3/4/5), does not pad.
- **Scenario**: `nx doc validate` on clean doc — **Verify**: exit 0, no emit.
- **Scenario**: `nx doc validate` on doc with one stale RDR ID — **Verify**: exit non-zero, no emit, error lists offending token.
- **Scenario**: Author updates source; `nx doc render` produces updated sidecar — **Verify**: diff of sidecar matches source change semantically.

## Validation

### Testing Strategy

1. **Unit** — parser (positive + negative grammar tests), each resolver (mocked backends), renderer composition.
2. **Integration** — real bead DB + real RDR files on current repo; render a sample doc and assert expected substitution.
3. **Regression** — render-then-validate round-trip on every doc in `docs/rdr/` that contains no tokens; must be byte-equal.

### Performance Expectations

Measured on `docs/rdr/` corpus (~80 files, assume mean 10 tokens per token-using doc). Target: <5s per doc, dominated by `bd show` subprocess overhead; optimization via batch `bd show` is a follow-up if real numbers miss.

## Finalization Gate

### Contradiction Check

To be filled at gate.

### Assumption Verification

To be filled at gate.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `bd show <id> --json` | beads CLI | Source Search |
| T2 `memory_get` | `nexus.db.t2` | Source Search |
| YAML frontmatter parse | `pyyaml` (already a dep) | Source Search |

### Scope Verification

To be filled at gate.

### Cross-Cutting Concerns

- **Versioning**: Token grammar is versioned in renderer header; unknown version fails with a migration pointer. Resolvers are additive.
- **Build tool compatibility**: CLI only; no mkdocs/mkdocs-material plugin in scope (natural follow-up).
- **Licensing**: N/A
- **Deployment model**: N/A (CLI)
- **IDE compatibility**: N/A — tokens are plain text; editors see them as literal.
- **Incremental adoption**: Fully opt-in. A doc without tokens renders to an identical sidecar (or can skip rendering entirely).
- **Secret/credential lifecycle**: N/A
- **Memory management**: Per-render resolver cache; bounded by document size.

### Proportionality

One CLI command, three resolvers, one small grammar. Resist adding token families speculatively; let RDR-083 add `{{chash:…}}` on its own timeline.

## References

- `docs/field-reports/2026-04-15-architecture-as-code-from-art.md` §3.1/F3, §3.2/F8
- RDR-075 (cross-collection projection)
- RDR-077 (projection quality — `source_collection`)
- RDR-081 (stale-reference validator — complementary: legacy prose vs. new tokens)
- `bd` beads CLI — `bd show --json` output contract

## Revision History

- 2026-04-15 — Draft authored from ART field report findings F3, F8.
