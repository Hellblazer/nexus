# Plan Authoring Guide

A *plan* is a YAML/JSON template describing a reusable retrieval or
analysis recipe — what to retrieve, in which order, scoped how,
parameterised by which caller-supplied bindings. RDR-078 introduced
the plan-centric retrieval contract; this guide is the canonical
reference for anyone authoring a new plan template (by hand, via
`verb:plan-author`, or via a downstream tool).

Companion references:

- **RDR-078** — the design decision. Read the Vocabulary + Phase 1-4
  sections for the why.
- `docs/catalog-link-types.md` — the seven link-type values
  (`implements`, `cites`, `supersedes`, …) that `traverse` walks.
- `docs/catalog-purposes.md` — named aliases that map to link-type
  lists; use `purpose:` in plans instead of bare `link_types:` when a
  named alias exists.

## Vocabulary

Three strings, three jobs — do not confuse them:

| Term | Stored? | Role |
|------|---------|------|
| `description` | Yes (on the plan) | Embedded for `plan_match` cosine retrieval. Prose that answers "when should an authoring agent reach for this plan?" Authoring quality directly affects match confidence. |
| `intent` | No (caller-side, per-call) | The caller's phrasing of what they're trying to do right now. Passed to `plan_match` at call time; never persists on the plan. |
| `name` | Yes (optional) | Human disambiguator between otherwise-identical dimension sets (usually `default`). Has no effect on identity — `canonical_dimensions_json(dimensions)` is the dedup key. |

## Template schema

Full YAML shape — see `src/nexus/plans/schema.py::validate_plan_template`
for the authoritative validator:

```yaml
# REQUIRED
description: >-
  Prose describing when an agent should pick this plan. This is what
  `plan_match` ranks on — write crisp, discriminative copy. Sloppy
  descriptions fail silently (low match confidence), not loudly.
dimensions:                       # identity — the pinned set
  verb: <registered-verb>         # required
  scope: <registered-scope>       # required
  strategy: default | security | performance | ...   # optional, defaults to "default"
  object: ...                     # optional (e.g. change-set, rdr, module)
  domain: ...                     # optional (e.g. security, ml-systems)
  # any registered dimension from nx/plans/dimensions.yml

# OPTIONAL
name: <human-disambiguator>       # distinguishes otherwise-identical dimension sets
parent:                           # currying lineage; `default_bindings` inherit
  verb: ...
  scope: ...
  strategy: default
default_bindings:                 # fills $vars when the caller omits them
  depth: 2
  limit: 10
required_bindings: [intent, concept]    # `plan_run` aborts if absent
optional_bindings: [subtree]            # silent pass-through if absent
tags: comma,separated,free-form

# REQUIRED
plan_json:
  steps:
    - tool: search | query | traverse | extract | summarize | rank | compare | generate
      args: {<kwargs>}            # may contain $var and $stepN.<field> refs
      scope:                      # optional — Phase 2 domain routing
        taxonomy_domain: prose | code
        topic: <label or $var>
      # traverse-only (mutually exclusive — SC-16):
      link_types: [...]           # explicit
      # OR
      purpose: <registered-name>
```

### What makes a good description

`plan_match` is a cosine / FTS5 retrieval against the synthesised
`match_text` payload, whose prefix is the description text. Rule of
thumb: write what would come out of the mouth of an agent that
should invoke this plan.

**Bad:** `"research plan"` — no discriminative signal.
**Good:** `"Walk from an RDR to its implementing modules and summarise design intent with citations."`

The description should name the verb, the objects, and the outcome.
Two plans whose descriptions differ only by paraphrase will compete
for matches; the better-worded one wins.

RDR-092 Phase 1 + Phase 3 appended a dimensional suffix to the
embedded payload. The description still does the heavy lifting; the
matcher now also sees `<verb> <name> scope <scope>` so queries that
know the identity (e.g. `research find-by-author`) hit even when
their phrasing does not overlap the description. Authoring guidance
is unchanged: write a crisp description; the suffix is automatic.
See [Plan-Centric Retrieval § match_text synthesis](plan-centric-retrieval.md#match_text-synthesis)
for the full shape.

### Dimension conventions

Every plan pins at minimum `verb` and `scope`. The canonical axes:

| Axis | Values | When to pin |
|------|--------|-------------|
| `verb` | `research`, `review`, `analyze`, `debug`, `document`, `plan-author`, `plan-promote`, `plan-inspect` | Always. The action the plan performs. |
| `scope` | `personal`, `rdr-<slug>`, `project`, `repo`, `global` | Always. Where the plan is published. |
| `strategy` | `default`, `security`, `performance`, `compliance`, … | When a verb has multiple valid strategies; defaults to `default`. |
| `object` | `change-set`, `rdr`, `module`, `test-suite`, … | When the verb specifically targets a known artefact type. |
| `domain` | `security`, `compliance`, `ml-systems`, `frontend` | When the strategy variant keys off a subject-matter qualifier. |

Canonical ordering is enforced internally — `canonical_dimensions_json`
sorts keys + lowercases values so `{verb: "r", scope: "g"}` and
`{scope: "g", verb: "r"}` produce byte-identical identities.
Authoring order doesn't matter; the loader handles it.

### `link_types` vs `purpose` (SC-16)

A `traverse` step must pick one of the two — never both:

- `link_types: [implements, implements-heuristic]` — explicit literal
  list. Freezes the set; any new link type introduced in the catalog
  after the plan was authored will **not** be walked.
- `purpose: find-implementations` — named alias resolved at run time
  via `nx/plans/purposes.yml`. Forward-compatible: if a new link type
  (`semantic-implements`, …) ships under the purpose, older plans pick
  it up automatically.

Prefer `purpose:` for generic traversals. Reserve explicit
`link_types:` for audit-style plans that need to freeze the walked
set for reproducibility.

Specifying both raises a schema validator error (SC-16) and the
`traverse` MCP tool returns a structured `{"error": ...}` payload
rather than running a half-resolved walk.

### Dedup identity (SC-18)

A plan's identity is `canonical_dimensions_json(dimensions)`. Two
plans with byte-identical identities are a conflict — the
`PlanTemplateLoader` refuses the second with both source labels in
the error. If you really want two plans with the same dimensions,
differentiate them on `scope` (e.g. one at `scope:global`, one at
`scope:project`), or use the `strategy` axis.

## Phase 2 scope field (routing)

A step's `scope` narrows the retrieval without changing the tool:

```yaml
steps:
  - tool: search
    args: {query: "$intent"}
    scope:
      taxonomy_domain: prose       # code | prose
      topic: "$concept"
```

- `taxonomy_domain: prose` injects `args["corpus"] =
  "knowledge,docs,rdr,paper"`. `code` injects `"code"`. Caller-pinned
  `corpus` / `collection` arg wins (the runner's SC-10 guard
  separately enforces consistency between declared domain and the
  pinned collection's embedding model).
- `topic: <literal or $var>` forwards as `args["topic"]`. Runtime
  `$concept` substitution from bindings works exactly the same as in
  `args`.
- `traverse` is exempt — it operates on tumblers, not embeddings, so
  no corpus injection is applied even when `scope.taxonomy_domain`
  is declared.

Cross-embedding boundary: a step declaring `taxonomy_domain: code`
that somehow dispatches to a `docs__*` collection raises
`PlanRunEmbeddingDomainError` (SC-10). Never compute cosine across
`voyage-code-3` and `voyage-context-3` — they're incompatible
embedding spaces.

## `traverse` operator

Signature:

```yaml
- tool: traverse
  args:
    seeds: [<tumbler>] | $stepN.tumblers
    depth: 1..3                    # capped at Catalog._MAX_GRAPH_DEPTH (3) per SC-4
    direction: out | in | both     # default: both
    link_types: [...]              # OR purpose — exactly one
    purpose: <registered-name>
```

Output contract (every retrieval step emits this shape):

```python
{
    "tumblers": [str, ...],        # reachable nodes' tumbler strings
    "ids":      [str, ...],        # chunk IDs when tracking lands
    "collections": [str, ...],     # physical_collection union
}
```

`$stepN.tumblers`, `$stepN.ids`, `$stepN.collections` resolve from
this shape in downstream steps. The common pattern:

```yaml
- tool: search             # find design docs
  args: {query: "$concept"}
  scope: {taxonomy_domain: prose}
- tool: traverse           # walk to implementing code
  args:
    seeds: $step1.tumblers
    purpose: find-implementations
    depth: 2
- tool: search             # scope next retrieval to just those collections
  args:
    query: "$concept usage"
    subtree: $step2.collections
  scope: {taxonomy_domain: code}
```

### `_MAX_GRAPH_NODES = 500`

`Catalog.graph_many` caps the merged frontier across all
(seed, link_type) fan-out calls at 500 nodes. When the cap is hit:

- A structured warning `graph_many_node_limit` logs, naming the seed
  count and link-type list.
- Partial results are returned — `tumblers` / `collections` reflect
  whatever the BFS surfaced before the cap.
- The plan run continues with the partial result. Downstream steps
  that key off `$stepN.tumblers` see fewer seeds than they might
  have.

If your plan regularly hits this cap, split into multiple plans with
narrower seed sets or a lower depth.

## Lifecycle tiers

Every scope above `personal` is a git-tracked YAML path:

| Scope | Location | Promotion |
|-------|----------|-----------|
| `personal` | T2 only (local plans DB) | `plan_save` at runtime |
| `rdr-<slug>` | `docs/rdr/<slug>/plans.yml` (peer to the RDR) | `nx catalog setup` when RDR is `status: accepted` |
| `project` | `.nexus/plans/*.yml` or `.nexus/plans.yml` | `nx catalog setup` |
| `repo` | umbrella `.nexus/plans/_repo.yml` | `nx catalog setup` (umbrella detection) |
| `global` | `nx/plans/builtin/*.yml` (nx plugin) | plugin release |

A plan authored at one scope can be *promoted* — copied to a higher
scope with a new dimension map. Promotion flow (tool surface in
RDR-079) is out of scope here; forward-reference only.

**Lifecycle ops deferred:** `nx plan promote` CLI, `nx plan audit`
CLI, and RDR-close hooks for plan seeding / archival all ship in
**RDR-079** (not RDR-078). The `verb:plan-promote, strategy:propose`
meta-seed (Phase 4d) is a primitive form of the audit CLI usable
today via `plan_run`.

## Bindings

Three sources of values, merged at `plan_run` time as
`{**match.default_bindings, **caller.bindings}` (caller wins on
conflict):

1. `default_bindings` on the plan — good for "usually depth 2"
   boilerplate.
2. Caller bindings passed to `plan_run(match, bindings=...)` — the
   per-invocation customisation.
3. `required_bindings` must all be present after merging or
   `plan_run` raises `PlanRunBindingError(missing=[...])`.
4. `optional_bindings` are just documentation; unresolved `$var`
   references fall through unchanged rather than raising.

Naming conventions: snake_case, descriptive. `$intent`, `$concept`,
`$changed_paths` are all from existing scenario templates — reuse
these names when the plan does the same thing they do.

## `$var` and `$stepN.<field>` resolution

Substitution fires **only** on values that are exactly a single
reference token — no inline interpolation:

- `"query": "$intent"` → substituted from bindings
- `"seeds": "$step1.tumblers"` → substituted from prior step's output
- `"message": "hello $intent"` → **not substituted**; literal string

If you need string interpolation, do it in an `extract` step — plan
authoring discipline is "one reference per arg value".

Unknown `$stepN.<field>` references raise `PlanRunStepRefError` with
the list of fields actually present in the prior step's output.
Unknown `$var` names fall through as literal strings (since
required-binding validation runs upfront, any unmatched `$var` past
that point is deliberate).

## Pointer: `verb:plan-author`

The `verb:plan-author, scope:global, strategy:default` meta-seed
(Phase 4d) is a self-referential plan that authors new plan
templates. It walks this guide, the dimension registry, and the
schema, then prompts you for the new plan's dimensions / description
/ bindings, drafts a candidate `plan_json`, and saves via
`plan_save`. Invoke it via:

```
plan_match("author a new plan template", dimensions={verb: "plan-author"}, n=1)
→ plan_run(match, bindings={concept: "what the new plan does"})
```

## `scope_tags` (matcher routing)

`scope_tags` is a comma-separated string on the `plans` row that names
the corpora or collections a plan actually touches. It is separate from
the `scope` dimension, which names the plan's publication tier
(`global`, `project`, `rdr-<slug>`, `personal`). The matcher uses
`scope_tags` at match time to filter out plans whose retrieval space
conflicts with the caller's `scope_preference`, and to boost plans whose
space matches.

### Inference vs explicit

`plan_save` infers `scope_tags` from the plan JSON by walking retrieval
steps and unioning the literal string values of each step's `corpus` and
`collection` args. `$var` placeholders (e.g. `"corpus": "$corpus"`) are
skipped, since the real routing is decided by the caller's bindings.
`traverse`-only plans contribute nothing, so their `scope_tags` is `""`
(agnostic).

Pass an explicit `scope_tags="rdr__arcaneum,code__nexus"` kwarg to
`plan_save` when inference would undershoot, the common case being a
plan whose retrieval steps use `$var` placeholders but which you
nonetheless know targets a specific corpus family. Explicit values
override inference; they are normalized on the way in.

### Normalization contract

Both save-time and match-time normalization applies the same rules:

- Trailing 8-char lowercase hex suffixes are stripped: `rdr__arcaneum-2ad2825c` → `rdr__arcaneum`. Shorter, longer, or non-hex tails are preserved verbatim (the collection hash convention is strict).
- Trailing `*` or `-*` globs are stripped at match time only: caller scope `rdr__arcaneum-*` normalizes to `rdr__arcaneum`.
- Bare family prefixes (`rdr__`) and tumbler addresses (`1.16`) pass through unchanged.

### Matching semantics

The matcher compares a plan's `scope_tags` to the caller's normalized
`scope_preference` using `startswith` in either direction: tag
`rdr__arcaneum` matches caller scope `rdr__` (bare family), and caller
scope `rdr__arcaneum` matches tag `rdr__` (broader plan serving narrower
query).

- **Agnostic plan** (`scope_tags=""`) stays in the pool and competes on base cosine alone.
- **Matching plan** gets a multiplicative boost `final_score = base_confidence * (1 + scope_fit_weight * 1.0)` with `scope_fit_weight = 0.15`.
- **Conflicting plan** (non-empty `scope_tags`, no tag prefix-matches caller scope) is dropped from the candidate pool before scoring.

### Multi-corpus bridging plans

A plan tagged `scope_tags="knowledge__delos,knowledge__arcaneum"` passes
when the caller scope prefix-matches *either* tag. Intersect semantics
are the rule, so bridging plans that span a few corpora are not
accidentally filtered out the moment the caller picks one of them.

### Interaction with grown plans

The `nx_answer` ad-hoc save path (RDR-084) passes the caller's `scope`
argument through as the grown plan's `scope_tags`, so grown plans are
anchored to the retrieval space that produced them. Running the same
question against `knowledge__delos` and then against `knowledge__arcaneum`
saves two grown plans with different `scope_tags`, each ready to serve
future questions in the same scope.

When `nx_answer` is called without a `scope` argument, the grown plan's
`scope_tags` is inferred from literal corpus/collection args in the
executed plan_json; plans whose retrieval steps use `$var` bindings or
the `"all"` wildcard end up agnostic (`scope_tags=""`) in that case.

### Authoring guidance

- Let inference do the work when your retrieval steps use literal corpus/collection names.
- Pass explicit `scope_tags` when retrieval steps use `$var` placeholders but the plan targets a known corpus family.
- For cross-corpus bridging plans, list every corpus the plan legitimately touches; inference already does this for you when the args are literals.

## Common mistakes

- **Forgetting `scope`.** Every plan needs one; the validator
  rejects templates missing it.
- **Thin descriptions.** "Research plan" matches everything and
  nothing. Name the verb, the object, the outcome.
- **Explicit `link_types` when `purpose` suffices.** Freezes the
  walked set; use `purpose:` for generic traversals so forward-compat
  link types are picked up automatically.
- **Inline `$var` interpolation.** `"hello $x"` stays literal. Use
  an `extract` or `generate` step if you need string assembly.
- **Cross-embedding cosine.** Never dispatch a `search` step
  declaring `taxonomy_domain: code` to a `docs__*` collection.
  Runtime raise; schema validator doesn't catch this ahead of time.
- **Same dimensions at the same scope.** `canonical_dimensions_json`
  dedup will reject the second. Differentiate on `strategy`, or
  publish at a different scope.

## Day-2 operations

- `nx doctor --check-plan-library` (RDR-092 Phase 0c) reports plan
  rows by bucket (authored vs backfilled vs non-dimensional) and
  flags a stale global tier. Run after any plugin install to
  confirm `nx catalog setup` seeded the 12 builtins.
- `nx plan repair` (RDR-092 Phase 0d) backfills `verb` / `name` /
  `dimensions` on legacy NULL-dimension rows using a 20-rule stem
  dictionary + wh-fallback, and lists `backfill-low-conf` rows for
  manual review. Idempotent.

## See also

- `nx/plans/dimensions.yml` — registered dimension keys.
- `nx/plans/purposes.yml` — registered purpose names and link-type mappings.
- `src/nexus/plans/schema.py` — the validator enforcing this schema.
- `src/nexus/plans/runner.py` — `plan_run` implementation.
- `src/nexus/plans/matcher.py` — `plan_match` (T1 cosine + FTS5 fallback).
- `src/nexus/db/t2/plan_library.py::_synthesize_match_text`, the
  hybrid match-text synthesiser (RDR-092 Phase 1 + Phase 3).
