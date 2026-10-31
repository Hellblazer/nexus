---
title: "The Tuple-Space Dictionary: Every Template Publishes What It Means, Who Writes It, Who Reads It, and a Post That Validates"
id: RDR-239
type: Feature
status: draft
priority: medium
author: Sam
reviewed-by: self
created: 2026-10-31
accepted_date:
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
retention and limits. The eight shipped templates (`aspects`,
`board/<topic>`, `board/ci/<topic>`, `directory/<name>`,
`ledger/<session_id>`, `lock/<resource>`, `mailbox/<address>`,
`queue/<name>`) carry their meaning in YAML comment headers: what the
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
subspace: the directory, the ledger, the aspect queue. A trigger in stage 2
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
| RDR-205 Linda tuple space | Substrate | The bus. Unchanged here. |
| RDR-211 Board, queue and lock templates | Extended | Owns the template schema and the registry route this RDR adds fields to. |
| RDR-235 Aspect queue onto the tuple queue | Example user | Its template's comments are one of the eight this RDR turns into fields. |

## Context

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

### Key Discoveries

- **✅ Verified** (source search): the parser rejects unknown top-level
  template fields (`TemplateSchemaParser.java:25,37`), and the engine's YAML
  reader drops comments, so template comments are invisible at runtime.
- **✅ Verified** (live): eight templates are registered
  (`nx tuple templates`, 2026-10-31).
- **⚠️ Documented** (linda, 2026-10-31): tuples suit status and index
  entries, not records found by content; template changes ride an engine
  cut; `from` is recorded, not checked.

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
the registry route. Fill it for the eight shipped templates. Render a
dictionary view that joins it with the live census. A lint requires the
block on every template in the repository.

### Technical Design

1. **Schema.** A new top-level field `doc`, a mapping with a closed set of
   fields:
   - `summary`: one sentence, what the subspace is for (required inside `doc`).
   - `writers`: list of who posts (a workflow, a hook, a verb, a role).
   - `readers`: list of who reads, and how (`rd` page, `in` claim, wake).
   - `dimension_values`: for a free-text dimension such as `kind`, the
     values in use and what each means. Documentation only; it does not
     restrict `out()`, since RDR-211 chose free text on purpose.
   - `example`: one tuple literal (`keys`, `dims`, `body`). At boot the
     engine validates it with the same checks `out()` applies; a template
     whose example fails does not load, and the error names the field.
   - `see_also`: RDR and doc references.
   Unknown fields inside `doc` are rejected, as at the top level.
2. **Route.** `renderTemplate` includes `doc` when present. Additive on the
   wire, with a wire-contract ledger entry.
3. **Clients.** `nx tuple dictionary [NAME] [--json]` renders the doc
   joined with `nx tuple list`'s live census for that template's concrete
   subspaces (live rows, last post time). `tuple_registry` returns `doc`
   unchanged.
4. **Content.** The eight templates' comment headers become `doc` blocks.
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
  closed and documentary; `dimension_values` restricts nothing.
- **Scope creep into stage 2.** Mitigation: the constraint at the top; a
  trigger or action field proposed during review goes to the stage-2 RDR.

## Implementation Plan

### Phase 1: Content first

Write the `doc` blocks for all eight templates as a review artifact, before
any parser change (A1).

### Phase 2: Engine

Parser support for `doc`, boot validation of `example`, the route field,
tests, the wire-ledger entry; ships in the next engine-service release.

### Phase 3: Clients and lint

`nx tuple dictionary`, `tuple_registry` passing `doc` through, and the
repository lint.

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

### Assumption Verification

### Scope Verification

### Cross-Cutting Concerns

### Proportionality

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
