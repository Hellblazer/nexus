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
template that the engine validates when it loads its templates, with the
same checks a real post meets, so an example that no longer fits its
template fails the engine's boot test before release. The examples use
reserved placeholder names (a subspace segment that starts with `example`,
identifiers that are zeros except their last digit), so posting one as
written reaches no real session and no named subspace. The one exception is
`aspects`, whose subspace is the literal `aspects`, the live aspect queue.

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
| RDR-205 Linda tuple space | Substrate | The bus. Unchanged here, except that its v1 session ledger template is retired separately (nexus-onjvw, shipped in engine-service-v0.1.174), which Phase 1 found had no writer. |
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
  RDR documents those seven. The retirement commit is on `develop` and
  inside that engine tag on `origin` (verified 2026-11-01; *Source: T2
  `nexus_rdr/239-research-6`*).
- **⚠️ Documented** (linda, 2026-10-31): tuples suit status and index
  entries, not records found by content; template changes ride an engine
  cut; `from` is recorded, not checked.

- **❌ A1 refuted, then resolved** (spike, Phase 1, 2026-10-31): the
  drafted field set did not fit all eight templates (seven after
  nexus-onjvw); four changes (above,
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
  mailboxes hold only consumed rows, which `rd` does not return. The
  dictionary view therefore shows the consumed and claimed counts beside
  the available ones (Technical Design item 3).

### Critical Assumptions

- **A1:** One small documentation block covers every template without
  per-template special cases. Verification: write the block for all eight
  templates (seven after nexus-onjvw) before the parser change, and adjust
  the block until all of them fit.
- **A2:** Serving and validating the dictionary is cheap in the cost that
  matters. That cost is the size of the registry response on every call
  that fetches it (the Python client calls the registry from seven places,
  and the `tuple_registry` tool returns the whole response to the model; T2
  `nexus_rdr/239-research-4`), plus a small amount of boot work: one
  validation per template, run by the same shared validator that `out()`
  uses. The boot check and `out()` call one validator, extracted in Phase 2
  Step 1; today the shape, nonce and size checks are private methods of
  `TupleRepository` (`validateOutShape`, `validateNonce`) that the template
  loader cannot call, and a second copy would drift. The example's
  `subspace` must resolve, by the registry's own matching, to the template
  that holds the example. Verification: the registry response in bytes
  before and after, for the default response and for `?doc=1`, and the
  registry boot time before and after. Baseline: the gate critic read 3,695
  bytes for the eight-template response on 2026-10-31 (T2
  `nexus_rdr/239-research-3`); Phase 2 takes its own before figure on the
  unchanged engine.

## Proposed Solution

### Approach

Add one optional, closed `doc` block to the template schema. Serve it from
the registry route on request (`?doc=1`). Fill it for the seven shipped
templates. Render a dictionary view that joins it with the live census. A
lint requires the block on every template the engine loads.

### Technical Design

1. **Schema.** A new top-level field `doc`, a mapping with a closed set of
   fields:
   - `summary`: one sentence, what the subspace is for (required inside
     `doc`).
   - `writers`: list of who posts (a workflow, a hook, a verb, a role).
   - `readers`: list of who reads, and how (`rd` page, `in` claim, wake).
   - `fields`: for any declared key or dimension, either a one-line
     description or a `{value: meaning}` map of the values in use. A name
     that is not a declared key or dimension is a doc error (see Failure
     below), so a typo cannot pass. When a dimension declares allowed values
     (the mailbox `address_kind` allows `agent` and `session`), a value map
     for it must have exactly those values as its keys, so the map cannot
     disagree with the enum. Documentation only; it does not restrict
     `out()`, since RDR-211 chose free text on purpose. (Phase 1 replaced the
     drafted `dimension_values`: a meaningful name can be a key, and `from`
     and `topic` carry meaning with no enumerable values.)
   - `body`: a one-line description, or a `{json field: meaning}` map when
     the body is structured (the `board/ci` body carries `state`,
     `conclusion`, `workflow`, `job`, `sha`, `run`, `attempt`, `url`).
     Omitted where `max_body_bytes` is 0.
   - `example`: one tuple literal with `subspace`, `keys`, `dims`, `body`
     and, when `id_from` is `keys+nonce`, `nonce`. When the templates load,
     the engine validates it with the checks `out()` applies to a real post:
     shape, nonce and size limits. Those checks are one shared validator
     that `out()` and the boot check both call (Phase 2 Step 1 extracts it).
     The example's `subspace` must resolve, by the registry's own matching,
     to the template that holds the example, so an example cannot pass
     against some other template. The convention that `keys.topic` equals
     the last segment of the subspace is not checked, because `out()` does
     not check it. `subspace` is required. Phase 1 added it for the ledger,
     whose subspace shared nothing with its keys. With the ledger retired,
     each of the seven remaining examples could derive it, and it stays
     explicit because one field is simpler than a derivation rule that a
     future template may break.
   - `see_also`: RDR and doc references.
   `writers: []` is valid and means "none today". Unknown fields inside `doc`
   are rejected, as at the top level. `MiniYaml` has no list of mappings and
   no multi-line scalars, so `writers` and `readers` are lists of one-line
   strings, and the `doc` reader turns example keys and dims that arrive as
   `Long` or `Boolean` back into strings.

   **Byte caps.** A `summary`, a `writers`, `readers` or `see_also` string,
   and a description in `fields` or `body` (each value's meaning in a map
   counts as a description) is at most 300 bytes. A whole `doc` block is at
   most 4,096 bytes as the engine's reader sees it. The caps keep the
   registry small and turn "one sentence" into a rule. The seven drafted
   blocks fit with room: the longest string is 197 bytes, and the largest
   block is 1,855 bytes as YAML text (T2 `nexus_rdr/239-research-5`).

   **Failure.** A `doc` block that breaks any rule above (an unknown field,
   an undeclared name, a value map that disagrees with its declared values,
   a failing example, a string over its cap) is handled by where the
   template came from. A classpath template, one shipped inside the engine,
   stops the engine booting, with an error that names the template and the
   field; the engine's own boot test therefore catches a content error
   before release. A template read from `NX_TUPLE_TEMPLATE_DIR` (an
   optional second source that `TemplateRegistry` describes as test-only)
   does not take the engine down: the engine logs a structured warning that
   names the template and the field, drops that template's `doc`, and loads
   the template without it.
2. **Route.** `renderTemplate` includes `doc` only when the request is
   `GET /v1/tuples/registry?doc=1`. Without the parameter the response is
   what it is today, so the client's seven call sites that fetch the
   registry (T2 `nexus_rdr/239-research-4`) pay nothing for it. `doc` is
   part of the registry digest (the hash of the loaded templates that a
   client compares to see whether the registry changed): `doc` joins the
   hand-listed canonical form that `computeDigest` builds, so a doc-only
   edit changes the digest, which is what the `tuple_registry` description
   already promises ("changes whenever a template file changes"). A test
   pins this, because `lock` was once left out of that list. Additive on the
   wire, with a wire-contract ledger entry.
3. **Clients.** `nx tuple dictionary [NAME] [--json]` asks the registry with
   `?doc=1` and renders the doc joined with the live census, which has one
   row per concrete subspace with its counts, for the template's subspaces.
   The columns shown are `available`, `claimed`, `consumed`, `dead` and
   `newest_created_at`, not "live rows": a take-enabled template such as a
   mailbox or the aspect queue holds consumed rows, so a live-rows count
   reads zero for a template that is busy. The engine adds a `template`
   field, the owning template's name, to each census row, so the client
   carries no third copy of the subspace-to-template matching beside
   `TemplateRegistry.resolve` and the subscription code. `NAME` is a
   template name as `nx tuple templates` lists it (`board/<topic>`, which
   needs shell quoting) or a concrete subspace that resolves to its
   template (`board/ci/example-branch` shows `board/ci/<topic>`); with no
   `NAME` the verb covers every template. Against an engine that predates
   the dictionary the registry has no `doc`: the verb prints "engine
   predates the dictionary" and still shows the census. The
   `tuple_registry` MCP tool takes a `doc` option, off by default, and
   passes `?doc=1` through; its description of `digest` is updated with it.
4. **Content.** The seven templates' comment headers become `doc` blocks.
   The comments that explain a schema decision (why take is disabled, why
   retention is a ceiling) stay as comments: they are for the people who
   change the template, while `doc` is for the people who use it. Two
   templates, `board/<topic>` and `queue/<name>`, have no writer in this
   repository and no live subspace, so what their `writers` and `readers`
   say is intent, not evidence: every `writers` and `readers` string of
   those two starts with `Unverified: `. The other five name code and
   workflows that existed when the block was written, which nothing keeps
   true (see Drift).
5. **Lint.** A repository lint, written as a Java test over the templates
   that `TemplateRegistry.loadAtBoot` loads, so it reads them with the
   engine's own parser and not a second YAML reader (a full YAML library
   reads the `board/ci` example body as a mapping where `MiniYaml` keeps a
   string). It requires `doc.summary` and `doc.example` on every loaded
   template, so a new template without them fails the lint. It checks that
   the templates directory and the hand-kept `RESOURCE_TEMPLATE_PATHS` list
   name the same files, so a template file left off the list, and so never
   loaded, fails the lint instead of going unchecked. It checks that every
   file path and RDR id cited in `writers`, `readers` and `see_also` exists
   in the repository (paths and RDR ids only; module and symbol names are
   not checked). And it checks the placeholder rule for examples: an
   address segment starts with `example`, a UUID-shaped value is all zeros
   except its last digit, and a 40-hex value is all zeros.

### Decision Rationale

The documentation sits in the template file, next to the shape it
describes, and the engine serves both, so they change together in one
review. What can be checked is checked: the example and the names in
`fields` are validated when the templates load, because an example that
lies about the shape is worse than none; this is the same reason the
memory-curation draft (nexus-qo7ie) filters superseded memory at the read.
What cannot be checked, the prose of `summary`, `writers`, `readers` and
the descriptions, sits in the same file so that it is reviewed with the
shape, and the two templates with no known user say so (`Unverified`).

## Alternatives Considered

### Alternative 1: A client-side dictionary file

Keep a Python or markdown dictionary in the client, with no engine change.

**Pros:** no engine cut; fast to write.

**Cons:** two files describe one template, one in `service/` and one in
`src/`, and nothing ties them; the first template change makes the
dictionary wrong.

**Reason for rejection:** the example and the field names would not be
checked against the template, and the two files would change in separate
reviews. The in-template block is reviewed with the shape, and its
checkable parts are enforced (see Drift).

### Alternative 2: Serve the YAML comments

Teach the reader to keep comments and serve them as text.

**Pros:** no new schema.

**Cons:** comments are unstructured, mix maintainer reasoning with user
documentation, and cannot carry a validated example.

**Reason for rejection:** comments mix maintainer reasoning with user
documentation, and cannot carry an example that the engine validates.

## Trade-offs

### Consequences

- One engine-service release; the change is additive.
- Every template gains a `doc` block to maintain, and the lint keeps it
  present. The prose parts are maintained by hand, and nothing checks that
  they are true.

### Risks and Mitigations

- **The block grows into a second schema.** Mitigation: its field set is
  closed and documentary; `fields` restricts nothing.
- **The prose goes stale.** `writers`, `readers`, `summary` and the
  descriptions name client code, which changes on the client's release
  cadence, while the doc changes only with an engine cut. Mitigation: the
  path check catches a cited file that is gone, not a changed behaviour;
  board and queue are marked `Unverified`; Drift says what is enforced.
- **The registry response grows.** Mitigation: `doc` rides only
  `?doc=1`, caps bound each string and each block, and A2 measures the
  bytes before and after.
- **Scope creep into stage 2.** Mitigation: the constraint at the top; a
  trigger or action field proposed during review goes to the stage-2 RDR.

## Implementation Plan

### Phase 1: Content first

#### Step 1: Write the blocks

Write the `doc` blocks for every shipped template as a review artifact,
before any parser change (A1). Done 2026-10-31:
`docs/rdr/rdr-239-doc-blocks.md`, revised 2026-11-01 for the planning
decisions (reserved example names, `Unverified` on board and queue, value
maps checked against declared values, byte caps). Its ledger block is not
carried forward, since the template is retired.

### Phase 2: Engine

Precondition: nexus-onjvw is on `develop` and inside engine-service-v0.1.174
(verified 2026-11-01, T2 `nexus_rdr/239-research-6`); Step 1 checks it
again. The engine ships first, and the client tolerates a missing `doc`, so
Phase 3 does not wait for the deployed engine except for its own
end-to-end check.

#### Step 1: Shared validator

Extract the shape, nonce and size checks that `out()` applies into one
validator that `out()` and the boot check both call. Run the existing
`TupleRepositoryTest` and `TupleSizeLimitsWireTest` unchanged against it.

#### Step 2: Parser and boot validation

Parser support for `doc` and boot validation of `example` (owner
resolution, byte caps, value maps, and the failure rule in Technical Design
item 1), with the parser tests in the Test Plan. Measure registry boot time
before and after (A2).

#### Step 3: Content into the templates

Copy the seven blocks of `docs/rdr/rdr-239-doc-blocks.md` into the template
files. That file stays as the Phase 1 record; once the templates carry the
blocks, a header line in it points at the templates, and it is not kept in
step after that.

#### Step 4: Route, census field and wire entry

`renderTemplate` emits `doc` only with `?doc=1`, and `doc` joins the
digest; census rows gain the `template` field; a wire-contract ledger entry
covers both. Measure registry response bytes before and after, default and
`?doc=1` (A2). Ships in the next engine-service release.

### Phase 3: Clients and lint

#### Step 1: Clients

`nx tuple dictionary` (census columns, `NAME`, and the absent-`doc` message
from Technical Design item 3) and `tuple_registry` with its `doc` option.

#### Step 2: Lint

The Java lint over `loadAtBoot` described in Technical Design item 5:
`doc.summary` and `doc.example` on every loaded template, the directory
against `RESOURCE_TEMPLATE_PATHS`, cited paths and RDR ids, and the
placeholder rule for examples.

#### Step 3: Repoint duplicated meanings

Two skills use the mailbox `kind` values inline in their call procedures:
the cross-instance request and ack line of `conexus/skills/mailbox/SKILL.md`
and the send example and mailbox request line of `conexus/skills/peer-messaging/SKILL.md`.
Those procedures keep the values they pass; each gains a pointer to the
dictionary for what the values mean, so the meaning lives in one place.
`docs/operations/tuple-space.md` has no `kind` text today and gets at most
one pointer sentence.

## Test Plan

- Parser (Java, the real parser and registry): a `doc` block with each
  field parses; an unknown `doc` field is refused naming it; an undeclared
  name in `fields` is refused naming it; a value map whose keys differ from
  a dimension's declared values is refused (one missing key, one extra key);
  `nonce` is required exactly when `id_from` is `keys+nonce` and refused
  otherwise; an example whose subspace resolves to a different template is
  refused; `writers: []` is accepted; `Long` and `Boolean` example keys and
  dims come back as strings and validate; a string over 300 bytes and a
  block over 4,096 bytes are refused; a template without `doc` still loads
  (the lint, not the parser, requires it).
- Failure rule: the same defective `doc` stops `loadAtBoot` for a classpath
  template and names the template and the field; from `NX_TUPLE_TEMPLATE_DIR`
  the template loads without `doc` and a structured warning names the
  template and the field. The shipped set loads through `loadAtBoot`.
- Shared validator: a change to the validator makes both an `out()` call and
  the boot check of an example fail (a positive control, so the two cannot
  have drifted apart).
- Digest: a doc-only edit changes the registry digest.
- Route: `doc` round-trips through `GET /v1/tuples/registry?doc=1`; the
  default response carries no `doc`; census rows carry `template`.
- Client: `nx tuple dictionary 'board/ci/<topic>'` shows the summary,
  writers, readers, example and the census (`available`, `claimed`,
  `consumed`, `dead`, `newest_created_at`) for `board/ci/nexus-develop`;
  against a registry with no `doc` it prints "engine predates the
  dictionary" and still shows the census.
- Lint: removing `doc.example` from one template fails it; a template file
  in the templates directory and not in `RESOURCE_TEMPLATE_PATHS` fails it;
  a cited path that does not exist fails it; an example that breaks the
  placeholder rule fails it.

## Finalization Gate

### Contradiction Check

The gaps, the design and the plan agree on one closed `doc` block, served by
the existing route on request and validated when the templates load.
This revision resolves two
earlier contradictions. Risks named `dimension_values` after Phase 1 had
replaced it with `fields`, and the template count said eight while the
ledger's retirement (nexus-onjvw) leaves seven. No design item contradicts
the constraint at the top (no trigger, action, display, script or new
subspace): every item documents or validates shipped substrate. Gate round
1 found the Test Plan naming the wrong template and a leftover "eight"; the
2026-11-01 amendment fixes both.

### Assumption Verification

A1 (one block fits every template) was tested by the Phase 1 spike. As
drafted it was refuted, and the four field changes in Technical Design
item 1 make it hold: 8 of 8 examples passed the engine's own `out()` checks,
350 checks, and planted faults were caught. The spike used a copy of those
checks, so "the same checks as `out()`" holds for the shipped code only once
Phase 2 Step 1 makes them one shared validator, and the Test Plan's
validator case checks that both paths fail together. A2 (serving and
validating the dictionary is cheap) is not yet measured. It is an
engineering assumption, not a design one. The cost that matters is the
registry response size, so `doc` rides only `?doc=1`; Phase 2 Step 2
measures boot time, and Phase 2 Step 4 measures response bytes for the
default response and for `?doc=1`, before the change ships.

### Scope Verification

The four gaps map to four deliverables: Gap 1 to the schema field and the
route, Gap 2 to `writers`, `readers` and `fields`, Gap 3 to the validated
`example`, and Gap 4 to `nx tuple dictionary` and the census `template`
field. Nothing in scope serves the
later stages beyond documenting what exists. Stages 2 and 3 get their own
RDRs (nexus-mdbds.1 first). The ledger's retirement shipped as its own
engine change (engine-service-v0.1.174) and is not part of this RDR.

### Cross-Cutting Concerns

- **Wire contract.** The route change is additive: `doc` appears only with
  `?doc=1`, so old clients and the client's existing call sites see the
  response they see today. The digest value changes with the first engine
  that carries `doc`, because `doc` is part of it. The census `template`
  field is additive too. The engine ships first: a client that is newer than
  its engine gets no `doc`, so `nx tuple dictionary` says "engine predates
  the dictionary" and still shows the census. It needs a wire-contract
  ledger entry and rides an engine-service release.
- **Tenancy.** Templates are engine-wide resources, not tenant rows, so
  `doc` carries no tenant data and needs no RLS change.
- **Boot safety.** A defective `doc` in a classpath template stops the
  engine booting, so a content error shows in the engine's boot test before
  release, not at a user's first post; this also means a documentation
  error can refuse the whole engine's boot, which is accepted because the
  shipped set is tested. In a template from `NX_TUPLE_TEMPLATE_DIR` it only
  costs that template its `doc`, with a logged warning.
- **Drift.** Enforced: the example, which the engine validates with the
  checks `out()` applies; the names in `fields`; value maps against declared
  values; the file paths and RDR ids cited in `writers`, `readers` and
  `see_also`; and the presence of `doc.summary` and `doc.example` on every
  loaded template. Not enforced: what `summary`, `writers`, `readers` and
  the descriptions say. That is maintained prose, and it can go stale the
  way the comment in `ledger.yaml` did. The mitigations are that it sits in
  the file whose shape it describes, and that board and queue say
  `Unverified`.

### Proportionality

One optional schema field, one route parameter and field, one census field,
one shared validator extracted from `out()`, one CLI verb, one lint, and
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
| 2026-11-01 | Amended after gate round 1 for the planning decisions of nexus-mdbds.2 (decision beads nexus-mdbds.2.1 to 2.5): `doc` rides only `?doc=1` and enters the digest, with byte caps and a response-size measure for A2; a failing `doc` is fatal for classpath templates and dropped with a warning for `NX_TUPLE_TEMPLATE_DIR` templates; the drift claim is restated and board and queue are marked `Unverified`; census columns, the `template` field and `NAME` are defined; the lint is a Java test over `loadAtBoot`; the boot check and `out()` share one validator; the ledger retirement is verified shipped; the Test Plan gaps are filled. Research: `nexus_rdr/239-research-3` to `-7`. |
- 2026-11-01: Gate round 1 — PASSED (0 Critical, 9 Significant, 0 ship-blocker(s)); commit `2ae483be1` (content-identical to the pre-rebase commit the gate read); critique `nexus_rdr/239-gate-critique-2026-11-01-r1`.
