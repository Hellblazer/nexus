---
title: "The Tuple-Space Dictionary: Every Template Publishes What It Means, Who Writes It, Who Reads It, and a Post That Validates"
id: RDR-239
type: Feature
status: accepted
priority: medium
author: Sam
reviewed-by: self
created: 2026-10-31
accepted_date: 2026-11-01
related_issues: [nexus-mdbds]
related_rdrs: [RDR-111, RDR-118, RDR-205, RDR-211, RDR-235, RDR-110]
---

# RDR-239: The Tuple-Space Dictionary: Every Template Publishes What It Means, Who Writes It, Who Reads It, and a Post That Validates

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

**Constraint, from the RDR-110 arc's scrap reason (2026-05-19).** That arc
"bundled the storage-substrate split with new abstractions (tuplespace, ORB
cockpit, host-trust, surfaces-as-tuples, UI fabric). Scope discipline failed
across nine RDRs and 67 stranded beads." This RDR is stage 1 of three
(epic nexus-mdbds) and ships alone: it adds documentation fields to the
template schema and the routes that serve them. It adds no trigger, no
action, no display, no scripting language and no new subspace. Stages 2
(declarative auras) and 3 (sandboxed Lua) are separate RDRs, each gated on
the one before it shipping and being used.

**Provenance.** Sam, 2026-10-31: emulate WeakAuras, and script the tuple
space the way AppleScript scripts applications, now that the tuple space
works. AppleScript's enabling piece was not its language but the
dictionary: each application publishes its nouns and verbs, and any script
can drive any application over one message bus. Nexus has the bus (the
RDR-205 tuple space) and half a dictionary (the RDR-211 template registry).
This RDR completes the dictionary.

## Problem Statement

A script, an agent or a person who wants to use a subspace today has to
reverse-engineer it from source. The registry tells them its shape. It does
not tell them what it is for.

### Enumerated gaps to close

#### Gap 1: The registry publishes shape, not meaning

`GET /v1/tuples/registry` (and `nx tuple templates`, and the MCP tool
`tuple_registry`) returns each template's keys, dimensions, id rule,
retention and limits. The seven shipped templates (`aspects`,
`board/<topic>`, `board/ci/<topic>`, `directory/<name>`,
`lock/<resource>`, `mailbox/<address>`, `queue/<name>`) carry their meaning in YAML comment headers: what the
subspace is for, why take is disabled, what the retention bounds, what
`kind` values mean. `board.yaml` has 37 lines of such comment above 25 lines
of schema. The engine reads these files with its own reader
(`service/src/main/java/dev/nexus/service/tuples/MiniYaml.java`), which
drops comments. So the meaning never leaves the repository. The fix
delivers the meaning as schema fields that the registry serves.

#### Gap 2: Nobody can find out who writes and who reads a subspace

`board/ci/nexus-develop` holds 1,330 tuples (2026-10-31). Which workflow
posts them, which sessions read them, and what `kind` values they carry can
be learned only by grepping workflows and Python. The same holds for every
subspace: the directory, the mailboxes, the aspect queue. A trigger in stage 2
cannot be written against a subspace whose writers and post shapes are
unknown. The fix delivers writers, readers and the documented values of
free-text dimensions per template.

#### Gap 3: There is no example post, so there is nothing to copy

AppleScript dictionaries and WeakAuras both spread by example. A template
today has no example tuple, so the first post anyone writes is a guess
against the validation errors. The fix delivers one example tuple per
template that the engine validates at boot, so an example can never drift
from the shape it illustrates.

#### Gap 4: Shape and live use are in two places

`nx tuple list` reports concrete subspaces and their row counts;
`nx tuple templates` reports templates. A reader wanting "what subspaces
exist, what each means, and how busy it is" joins the two by hand. The fix
delivers one dictionary view that joins a template's documentation with its
live census.

## Relationship to Prior RDRs

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-110 Semantic Tuple Space | Abandoned arc origin | Its scrap reason is this RDR's constraint (top). The substrate it lacked now exists (RDR-205). |
| RDR-111 The ORB | Abandoned; ambition inherited | Its Gap 2 ("no user-authored composition layer") and its bindings primitive are stage 2's subject, not this RDR's. Its principle "the composition is in data, not in code" is kept for stage 2, with stage 3's Lua as the escape hatch the ORB lacked. |
| RDR-118 Surfaces as Tuples | Abandoned | Said surfaces-as-tuples was "structurally not eligible until substrate work ships and the moratorium lifts". Whether the moratorium has lifted is Sam's decision; this RDR is a documentation change to shipped substrate and adds no consumer. |
| RDR-205 Linda tuple space | Substrate | The bus. Unchanged here, except that its v1 session ledger template is retired separately (nexus-onjvw), which Phase 1 found had no writer. |
| RDR-211 Board, queue and lock templates | Extended | Owns the template schema and the registry route this RDR adds fields to. |
| RDR-235 Aspect queue onto the tuple queue | Example user | Its template's comments are one of the seven this RDR turns into fields. |

## Context

### Background

The tuple space (RDR-205) is the engine's shared coordination store, and the
RDR-211 registry made its templates discoverable: a client can ask the
engine which subspaces exist and what shape a post must have. Sessions,
agents, hooks and CI workflows already use it daily for mailboxes, the
session directory, the CI board, locks and the aspect queue. Sam wants the
tuple space to become scriptable in three stages (epic nexus-mdbds): this
dictionary, then declarative triggers in the style of WeakAuras, then
sandboxed Lua scripts. Both later stages need a reader, human or program,
to learn from the engine alone what each subspace means, who writes it and
what a valid post looks like. Today that knowledge lives in YAML comments
the engine drops, in workflow files and in Python call sites. This RDR
moves it into the template, where the engine serves and validates it.

### Technical Environment

- Templates: `service/src/main/resources/tuples/templates/*.yaml`, loaded at
  boot by `TemplateRegistry`, parsed by `TemplateSchemaParser`, which
  rejects any top-level field outside a closed set (`rejectUnknownKeys`,
  `TOP_LEVEL_FIELDS`). A new field is therefore a parser change and rides an
  engine-service release. linda (2026-10-31): templates are "cheap to read,
  costly to change, so a template should be designed once for a stable
  shape". The documentation block is designed once for that reason.
- Registry route: `GET /v1/tuples/registry` in
  `service/src/main/java/dev/nexus/service/http/TupleHandler.java`
  (`handleRegistry`, `renderTemplate`); clients `nx tuple templates` and the
  `tuple_registry` MCP tool.

## Research Findings

### Investigation

Three questions were answered before the design was fixed. Does anything
carry template meaning at runtime today? A source search of the parser, the
YAML reader and the registry route says no: the reader drops comments and
the parser refuses unknown fields. Does one documentation block fit every
shipped template? A content-first spike wrote the block for every template
and replayed each example through the engine's own `out()` checks before
any parser change (Phase 1, A1). Do the comments that exist today tell the
truth about writers and readers? A live census and a source search of every
writer and reader per subspace say they do not (the last finding below).
Research records: T2 `nexus_rdr/239-research-1`, `239-research-2`, and
`nexus/rdr-239-phase1-doc-blocks-a1-verdict`.

### Key Discoveries

- **✅ Verified** (source search): the parser rejects unknown top-level
  template fields (`TemplateSchemaParser.java:25,37`), and the engine's YAML
  reader drops comments, so template comments are invisible at runtime.
- **✅ Verified** (live): eight templates were registered
  (`nx tuple templates`, 2026-10-31). After the ledger template's
  retirement (nexus-onjvw, engine-service-v0.1.174) seven remain, and this
  RDR documents those seven.
- **⚠️ Documented** (linda, 2026-10-31): tuples suit status and index
  entries, not records found by content; template changes ride an engine
  cut; `from` is recorded, not checked.

- **❌ A1 refuted, then resolved** (spike, Phase 1, 2026-10-31): the
  drafted field set did not fit all eight templates; four changes (above,
  Technical Design item 1) make it fit. The eight blocks are in
  `docs/rdr/rdr-239-doc-blocks.md`, and a scratch driver compiled against
  the real `MiniYaml`, `TemplateSchemaParser`, `TemplateSchema` and
  `TupleLimits` replayed `out()`'s shape, nonce and size checks on each
  example: 8 of 8 templates, 350 checks, 0 failures, 0 skipped; a planted
  dropped nonce, an out-of-enum key and an undeclared `fields` name were all
  caught. *Source: T2 `nexus/rdr-239-phase1-doc-blocks-a1-verdict`.*
- **✅ Verified** (source and live): template comments go stale, which is
  Gap 2 measured. `ledger.yaml`'s header names a writer that
  `CHANGELOG.md:957` records as deleted; the newest of 40,785 rows across
  446 ledger subspaces is dated 2026-10-04. That finding led to the
  template's retirement (nexus-onjvw). `board/<topic>` and
  `queue/<name>` have no writer in this repository and no live subspace.
  The `board/ci` writer is outside the repository (the conexus webhook
  adapter, RDR-220). A census join reads zero for every mailbox, because
  mailboxes hold only consumed rows, which `rd` does not return.

### Critical Assumptions

- **A1:** One small documentation block covers every template without
  per-template special cases. Verification: write the block for all eight
  templates before the parser change, and adjust the block until all eight
  fit.
- **A2:** A boot-validated example is cheap: one validation per template at
  boot, using the same path `out()` validates with. Verification: the
  registry boot time before and after.

## Proposed Solution

### Approach

Add one optional, closed `doc` block to the template schema. Serve it from
the registry route. Fill it for the seven shipped templates. Render a
dictionary view that joins it with the live census. A lint requires the
block on every template in the repository.

### Technical Design

1. **Schema.** A new top-level field `doc`, a mapping with a closed set of
   fields:
   - `summary`: one sentence, what the subspace is for (required inside `doc`).
   - `writers`: list of who posts (a workflow, a hook, a verb, a role).
   - `readers`: list of who reads, and how (`rd` page, `in` claim, wake).
   - `fields`: for any declared key or dimension, either a one-line
     description or a `{value: meaning}` map of the values in use. A name
     that is not a declared key or dimension fails at boot, so a typo
     cannot pass. Documentation only; it does not restrict `out()`, since
     RDR-211 chose free text on purpose. (Phase 1 replaced the drafted
     `dimension_values`: a meaningful name can be a key, and `from` and
     `topic` carry meaning with no enumerable values.)
   - `body`: a one-line description, or a `{json field: meaning}` map when
     the body is structured (the `board/ci` body carries `state`,
     `conclusion`, `workflow`, `job`, `sha`, `run`, `attempt`, `url`).
     Omitted where `max_body_bytes` is 0.
   - `example`: one tuple literal with `subspace`, `keys`, `dims`, `body`
     and, when `id_from` is `keys+nonce`, `nonce`. At boot the engine
     validates it with the same checks `out()` applies (`validateOutShape`,
     `validateNonce`, the size limits); a template whose example fails does
     not load, and the error names the field. `subspace` is required. Phase 1
     added it for the ledger, whose subspace shared nothing with its keys.
     With the ledger retired, each of the seven remaining examples could
     derive it, and it stays explicit because one field is simpler than a
     derivation rule that a future template may break.
   - `see_also`: RDR and doc references.
   `writers: []` is valid and means "none today". Unknown fields inside `doc`
   are rejected, as at the top level. `MiniYaml` has no list of mappings and
   no multi-line scalars, so `writers` and `readers` are lists of one-line
   strings, and the `doc` reader turns example keys and dims that arrive as
   `Long` or `Boolean` back into strings.
2. **Route.** `renderTemplate` includes `doc` when present. Additive on the
   wire, with a wire-contract ledger entry.
3. **Clients.** `nx tuple dictionary [NAME] [--json]` renders the doc
   joined with `nx tuple list`'s live census for that template's concrete
   subspaces (live rows, last post time). `tuple_registry` returns `doc`
   unchanged.
4. **Content.** The seven templates' comment headers become `doc` blocks.
   The comments that explain a schema decision (why take is disabled, why
   retention is a ceiling) stay as comments: they are for the people who
   change the template, while `doc` is for the people who use it.
5. **Lint.** A repository lint: every template file has `doc.summary` and
   `doc.example`. A new template without them fails the lint, so the
   dictionary cannot fall behind the registry.

### Decision Rationale

The documentation sits in the template file, next to the shape it
describes, and the engine serves both, so they change together in one
review. The example is validated at boot because a dictionary that can lie
is worse than none; this is the same reason the memory-curation draft
(nexus-qo7ie) filters superseded memory at the read.

## Alternatives Considered

### Alternative 1: A client-side dictionary file

Keep a Python or markdown dictionary in the client, with no engine change.

**Pros:** no engine cut; fast to write.

**Cons:** two files describe one template, one in `service/` and one in
`src/`, and nothing ties them; the first template change makes the
dictionary wrong.

**Reason for rejection:** drift is the failure this RDR exists to prevent.

### Alternative 2: Serve the YAML comments

Teach the reader to keep comments and serve them as text.

**Pros:** no new schema.

**Cons:** comments are unstructured, mix maintainer reasoning with user
documentation, and cannot carry a validated example.

**Reason for rejection:** a script cannot read prose.

## Trade-offs

### Consequences

- One engine-service release; the change is additive.
- Every template gains a `doc` block to maintain, and the lint keeps it
  present.

### Risks and Mitigations

- **The block grows into a second schema.** Mitigation: its field set is
  closed and documentary; `fields` restricts nothing.
- **Scope creep into stage 2.** Mitigation: the constraint at the top; a
  trigger or action field proposed during review goes to the stage-2 RDR.

## Implementation Plan

### Phase 1: Content first

#### Step 1: Write the blocks

Write the `doc` blocks for every shipped template as a review artifact,
before any parser change (A1). Done 2026-10-31:
`docs/rdr/rdr-239-doc-blocks.md`. Its ledger block is not carried forward,
since the template is retired.

### Phase 2: Engine

#### Step 1: Parser and boot validation

Parser support for `doc` and boot validation of `example`, with the parser
tests in the Test Plan. Measure registry boot time before and after (A2).

#### Step 2: Route and wire entry

`renderTemplate` emits `doc`, with a wire-contract ledger entry; ships in
the next engine-service release.

### Phase 3: Clients and lint

#### Step 1: Clients

`nx tuple dictionary` and `tuple_registry` passing `doc` through.

#### Step 2: Lint

The repository lint requiring `doc.summary` and `doc.example`.

## Test Plan

- Parser: a `doc` block with each field parses; an unknown `doc` field is
  refused naming it; an example that fails validation stops the template
  from loading and names the failing field; a template without `doc` still
  loads (the lint, not the parser, requires it).
- Route: `doc` round-trips through `GET /v1/tuples/registry`.
- Client: `nx tuple dictionary board/<topic>` shows the summary, writers,
  readers, example and the live census for `board/ci/nexus-develop`.
- Lint: removing `doc.example` from one template fails it.

## Finalization Gate

### Contradiction Check

The gaps, the design and the plan agree on one closed `doc` block, served by
the existing route and validated at boot. This revision resolves two
earlier contradictions. Risks named `dimension_values` after Phase 1 had
replaced it with `fields`, and the template count said eight while the
ledger's retirement (nexus-onjvw) leaves seven. No design item contradicts
the constraint at the top (no trigger, action, display, script or new
subspace): every item documents or validates shipped substrate.

### Assumption Verification

A1 (one block fits every template) was tested by the Phase 1 spike. As
drafted it was refuted, and the four field changes in Technical Design
item 1 make it hold: 8 of 8 examples passed the engine's own `out()` checks,
350 checks, and planted faults were caught. A2 (boot validation is cheap) is
not yet measured. It is an engineering assumption, not a design one: one
validation per template at boot uses the path `out()` already runs on
every post, and Phase 2 Step 1 measures it before the change ships.

### Scope Verification

The four gaps map to four deliverables: Gap 1 to the schema field and the
route, Gap 2 to `writers`, `readers` and `fields`, Gap 3 to the validated
`example`, and Gap 4 to `nx tuple dictionary`. Nothing in scope serves the
later stages beyond documenting what exists. Stages 2 and 3 get their own
RDRs (nexus-mdbds.1 first). The ledger's retirement shipped as its own
engine change and is not part of this RDR.

### Cross-Cutting Concerns

- **Wire contract.** The route change is additive, and old clients ignore
  `doc`. It needs a wire-contract ledger entry and rides an engine-service
  release.
- **Tenancy.** Templates are engine-wide resources, not tenant rows, so
  `doc` carries no tenant data and needs no RLS change.
- **Boot safety.** A bad example stops its template from loading, so a
  content error shows in the engine's boot test before release, not at a
  user's first post.
- **Drift.** The lint keeps every template documented, and the boot check
  keeps every example true.

### Proportionality

One optional schema field, one route field, one CLI verb, one lint, and
content for seven templates. That is the smallest change that puts meaning
where the engine can serve it. The cheaper alternatives (a client file,
served comments) fail the drift test this RDR exists for.

## References

- Epic nexus-mdbds (stages 1 to 3).
- RDR-110 scrap reason; `docs/postmortem/2026-05-16-rdr110-113-remediation-chain.md`.
- RDR-111 §Gap 2 and §The bindings primitive.
- linda on tuples as memory, 2026-10-31 (T2 `nexus_rdr/209-research-3`,
  `nexus_rdr/238-research-7`).

## Revision History

| Date | Change |
| --- | --- |
| 2026-10-31 | Filed as draft at Sam's direction: stage 1 of the scriptable tuple space (nexus-mdbds), the ORB's successor, constrained by the RDR-110 arc's scrap reason. |
| 2026-10-31 | Phase 1 content written (`docs/rdr/rdr-239-doc-blocks.md`); A1 refuted and resolved: `fields` replaces `dimension_values`, `body` added, `example` gains `subspace` and `nonce`; stale-comment and writerless-template findings recorded. |
| 2026-10-31 | Pre-gate revision: Background and Investigation added; Finalization Gate written; seven templates after the ledger retirement (nexus-onjvw); `dimension_values` in Risks corrected to `fields`; plan uses Phase and Step headings. |
- 2026-11-01: Gate round 1 — PASSED (0 Critical, 9 Significant, 0 ship-blocker(s)); commit `2ae483be1` (content-identical to the pre-rebase commit the gate read); critique `nexus_rdr/239-gate-critique-2026-11-01-r1`.
