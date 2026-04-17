---
title: "RDR-085: Glossary-Aware Topic Labeler — Project Vocabulary via `claude_dispatch`"
id: RDR-085
status: draft
type: Feature
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-16
related_issues: []
related: [RDR-070, RDR-080, RDR-081]
supersedes_part_of: [RDR-081]
---

# RDR-085: Glossary-Aware Topic Labeler

The topic labeler at `src/nexus/commands/taxonomy_cmd.py:859` shells out
to `claude -p --model haiku` with no project vocabulary context. The
2026-04-15 ART field report (§3.1/F2) documents the consequence: a
cluster containing the acronym `SSMF` (which in ART means
`SelfSimilarMaskingField`) gets auto-labeled **"Gated Multipole Single
Mode Fiber"** because Claude's training prior resolves `SSMF` to
optical fiber and the labeler has no evidence to override it.

This RDR fixes that class of silent mislabeling by injecting a per-
project glossary into the labeler's system prompt. It also migrates the
labeler off its bespoke subprocess shell-out onto the shipped
`claude_dispatch` substrate (from RDR-080) so the labeler picks up
structured-output schema enforcement, unified auth, and a single
dispatch-bug-fix surface.

**This RDR supersedes the labeler portion of RDR-081.** The original
RDR-081 draft bundled the labeler and the stale-reference validator and
was written against RDR-079's operator-pool architecture. RDR-079 was
abandoned; RDR-080 shipped `claude_dispatch` instead. The validator
portion of RDR-081 shipped standalone (closed 2026-04-16). This RDR
re-targets the labeler work at the actual shipped substrate.

## Problem Statement

### Enumerated gaps to close

#### Gap 1: Topic labels hallucinate without project vocabulary context

`_generate_labels_batch()` composes a labeling prompt from cluster terms
+ three sample document names and sends it to `claude -p`. Nothing in
the prompt tells the labeler what domain it's in. Claude fills the gap
with its training prior: `SSMF` resolves to "Single Mode Fiber" because
optical fiber is the dominant referent in the training data, and the
labeler has no evidence that this corpus uses `SSMF` for
`SelfSimilarMaskingField`. The bad label lands in the taxonomy, surfaces
in projection / hub views, and erodes user trust.

#### Gap 2: The labeler runs in a bespoke subprocess path, separate from every other LLM dispatch in Nexus

Every invocation is a cold `claude -p --model haiku` spawn (~13-27s
variable latency measured during RDR-081 spike). Output is free-form
text with `re.match` parsing — a single malformed response silently
drops labels for that batch. Meanwhile, `claude_dispatch` (shipped
RDR-080) provides: JSON-schema-enforced structured output, single
auth inheritance path, and a single dispatch-bug-fix surface (e.g. the
`structured_output` unwrap bug fixed in `f68b084` fixed 8 callers at
once). The labeler duplicates every concern at lower quality.

## Context

### Background

RDR-081 draft 2 (2026-04-15) proposed migrating the labeler onto
RDR-079's operator pool (`get_operator_pool("labeler", ...)` with warm
workers, `dispatch_with_rotation`, shared T1 session). RDR-079 was
abandoned; the pool infrastructure never shipped. RDR-080 shipped a
simpler substrate: `claude_dispatch(prompt, json_schema, timeout)` spawns
a single `claude -p --output-format json --json-schema …` subprocess
per call, unwraps `structured_output` from the claude wrapper, returns
the schema-conforming dict.

This RDR targets `claude_dispatch` directly. No new infrastructure.
The labeler becomes a caller of `claude_dispatch` with a label-specific
prompt and schema.

### Technical Environment

- Python 3.12+, Click CLI.
- Labeler call site: `src/nexus/commands/taxonomy_cmd.py:859`
  (`_generate_labels_batch`), batched. Called from `relabel_topics`
  (`:915`) and the post-discover auto-label banner at
  `src/nexus/commands/index.py:187`.
- Auth: `claude -p` auth inheritance already works — same path the
  existing labeler subprocess uses.
- Config hierarchy: `.nexus.yml` + `nexus.config`; `taxonomy` section
  already established by RDR-081.

## Research Findings

### RF-1 (from RDR-081) — Glossary A/B against Haiku 4.5

Verified during RDR-081 drafting (2026-04-15). Direct shell-level A/B
showed glossary injection **reliably eliminates literal hallucinations**
on CCE / chash / MMR — the acronym classes Nexus owns — and **reframes
but does not fully fix** adversarial cases where co-tokens are
themselves strongly training-prior-aligned (SSMF with optical co-tokens).

Quality floor: all Run B labels at or above Run A quality on every case.
No regression on non-ambiguous topics.

Result: Assumption 1 is **Verified with caveat**. Default is good
enough to ship. Adversarial cases (if they matter for a specific
project) will be handled by per-project model override — see
Follow-ups.

### RF-2 — `claude_dispatch` signature and usage

Source-verified against `src/nexus/operators/dispatch.py`:

```python
async def claude_dispatch(
    prompt: str,
    json_schema: dict[str, Any],
    timeout: float = 60.0,
) -> dict[str, Any]:
```

Returns the schema-conforming dict after unwrapping `structured_output`
from claude's wrapper. Already proven in 8 call sites (5 `operator_*`
tools + 3 stub-replacement tools). A label-specific prompt + schema
slots in cleanly.

### Critical Assumptions

- [x] Glossary injection improves label quality on non-adversarial
  domains — **Verified** (RF-1, carried from RDR-081).
- [x] `claude_dispatch` at ~10-30s per call is an acceptable latency
  for interactive `nx taxonomy label` runs. **Verified — 2026-04-16**:
  live quality run against `rdr__nexus-571b8edd` (6 topics, 1 batch)
  completed well under a minute on Haiku 4.5. The existing subprocess
  envelope (13-27s/batch, from RDR-081 spike) already spans the
  `claude_dispatch` envelope (~10-30s/batch per RDR-080 validation),
  so the substrate migration does not move the latency envelope in
  a measurable way at typical batch sizes. Formal 20-topic timing is
  deferred as a calibration follow-up if observed runs drift.
- [ ] Batched labeling remains the right granularity (one dispatch
  per batch of 20 topics rather than one per topic). **Verification
  plan**: a priori YES — the current code is already batched; the
  migration preserves that shape.

## Proposed Solution

### Approach

Three coordinated changes, all building on shipped infrastructure:

1. **Migrate `_generate_labels_batch` to `claude_dispatch`.** Replace
   the `subprocess.run(["claude", "-p", "--model", "haiku", ...])` call
   with `await claude_dispatch(prompt, _LABEL_SCHEMA, timeout=120.0)`.
   The schema enforces `{labels: [{label: str}]}` — one entry per topic,
   length-bounded. Deletes the subprocess code and the text-parsing
   `re.match` scaffolding.

2. **Glossary resolution from `.nexus.yml` or `docs/glossary.md`.**
   New module `src/nexus/glossary.py` with `load_glossary(project_root,
   collection=None) -> dict[str, str]` and `format_for_prompt(terms,
   max_tokens=500) -> str`. Priority: `.nexus.yml#taxonomy.glossary` →
   `docs/glossary.md` → empty (opt-in — no glossary configured means
   today's prompt with no preamble).

3. **Plumb glossary through to the prompt.** `_generate_labels_batch`
   accepts an optional `glossary_text: str = ""` parameter; when
   present, it's prepended to the numbered-topics prompt as
   `"Project vocabulary (use these expansions when an acronym matches):\n..."`.
   `relabel_topics` + the post-discover auto-label path resolve the
   glossary once per command and pass it to every batch.

### Technical Design

**New module** `src/nexus/glossary.py`:

```text
# Illustrative — verify at implementation time
def load_glossary(
    project_root: Path, collection: str | None = None
) -> dict[str, str]:
    """Priority: .nexus.yml#taxonomy.glossary → docs/glossary.md →
    <collection>.glossary.md → {}."""

def format_for_prompt(terms: dict[str, str], max_tokens: int = 500) -> str:
    """Return 'Project vocabulary:\n- TERM: expansion\n- ...'
    truncated at max_tokens."""
```

**Labeler rewrite** — as-built (`src/nexus/commands/taxonomy_cmd.py:859`).
The schema requires both `idx` (1-based topic number) and `label`; the
dispatcher populates slots by explicit `idx - 1` rather than positional
enumerate, so partial responses that skip a topic or return them out of
order still land in the correct slot with `None` in any gap.

```text
_LABEL_SCHEMA: dict = {
    "type": "object",
    "required": ["labels"],
    "properties": {
        "labels": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["idx", "label"],
                "properties": {
                    "idx":   {"type": "integer", "minimum": 1},
                    "label": {"type": "string", "minLength": 3, "maxLength": 60},
                },
            },
        },
    },
}


async def _generate_labels_batch(
    items: list[tuple[list[str], list[str]]],
    glossary_text: str = "",
) -> list[str | None]:
    if not items:
        return []

    lines = []
    for i, (terms, doc_ids) in enumerate(items, 1):
        doc_names = [d.split("/")[-1].split(":")[0][:25] for d in doc_ids[:3]]
        lines.append(
            f"{i}. terms=[{', '.join(terms[:5])}] docs=[{', '.join(doc_names)}]"
        )

    prompt_parts: list[str] = []
    if glossary_text:
        prompt_parts.append(glossary_text)
    prompt_parts.append(
        "You are a topic labeler. Label each numbered topic in 3-5 words.\n"
        'Return {"labels": [{"idx": <1-based>, "label": "..."}, ...]} — '
        "one entry per numbered topic, idx matches the number you were given.\n"
    )
    prompt_parts.append("\n".join(lines))
    prompt = "\n\n".join(prompt_parts)

    from nexus.operators.dispatch import claude_dispatch
    try:
        payload = await claude_dispatch(prompt, _LABEL_SCHEMA, timeout=120.0)
    except Exception:
        return [None] * len(items)

    results: list[str | None] = [None] * len(items)
    for entry in (payload.get("labels") if isinstance(payload, dict) else None) or []:
        idx = entry.get("idx")
        label = entry.get("label", "")
        if isinstance(idx, int) and isinstance(label, str):
            slot = idx - 1
            if 0 <= slot < len(items) and 3 <= len(label) <= 60:
                results[slot] = label.strip().strip('"').strip("'")
    return results
```

**Async/sync bridge:** `_generate_labels_batch` becomes `async def`.
Callers in `relabel_topics` (sync) wrap with `asyncio.run()` inside
ThreadPoolExecutor workers (safe — worker threads have no pre-existing
event loop).

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
|---|---|---|
| LLM dispatch | `src/nexus/operators/dispatch.py` `claude_dispatch` | **Reuse** — no new infra |
| Structured output | `claude_dispatch` unwraps `structured_output` already | **Reuse** |
| Glossary resolver | none | **New** — `src/nexus/glossary.py` |
| Config key | `.nexus.yml#taxonomy.*` already present (RDR-081) | **Extend** with `glossary` dict |
| Call sites | `taxonomy_cmd.py:859`, `index.py:187` | **Rewrite** — migrate both |

### Decision Rationale

- **`claude_dispatch` over pool** — the pool was abandoned; the dispatch
  path shipped. Reuse what exists; don't invent.
- **Glossary-in-prompt, not-in-schema** — the glossary is textual
  context, not structured output. Keeping it in the prompt means the
  schema stays identical across labeler and operator calls.
- **Opt-in** — no `.nexus.yml` glossary configured → today's behavior
  (minus subprocess-to-dispatch migration latency gain/loss). Zero
  risk of regressing users who haven't adopted.
- **Best-effort** — dispatch exceptions return `[None] * len(items)`;
  the taxonomy code already handles `None` labels as "use the c-TF-IDF
  fallback." No corrupt writes.

## Alternatives Considered

### Alternative 1: Keep the subprocess shell-out; add glossary only

**Description**: Minimal diff — add glossary config + prompt prepend,
leave subprocess.

**Pros**: Smaller surface.

**Cons**: Duplicates every `claude_dispatch` benefit (structured output
enforcement, unwrap-bug single-fix surface, unified auth). Text-parsing
`re.match` remains a silent failure mode. When the next
`structured_output`-class bug is fixed in `claude_dispatch`, the
labeler doesn't benefit.

**Reason for rejection**: The migration is small (≈80 lines of diff).
Cost of keeping a parallel path over time is much higher than the one-
shot migration.

### Alternative 2: Per-collection glossary (not per-project)

**Description**: Glossary lives in a collection-level sidecar
(`<collection>.glossary.md`) rather than a project-level config.

**Pros**: Different collections in the same repo can carry different
vocabularies.

**Cons**: Rare use case; adds config surface area; glossary-resolver
logic gets fallback-chain complexity for negligible ergonomic gain.

**Reason for rejection**: Load order supports it as an extension (collection
glossary falls back to project glossary), but primary surface is project-level.

### Briefly Rejected

- **Per-operator model override** (Haiku → Sonnet just for labeling) —
  deferred. If glossary + Haiku is insufficient on an adversarial case,
  raise it as a separate RDR rather than scope it here.
- **Auto-extracted glossary from co-tokens** — too speculative; let
  humans curate initially.

## Trade-offs

### Consequences

- Labeler joins the `claude_dispatch` family — unified bug-fix surface,
  schema-enforced output eliminates the silent text-parse failure mode.
- One new config key (`taxonomy.glossary`), additive.
- Existing callers keep their interface; no agent-facing behavior
  change except label quality improvement on ambiguous topics.

### Risks and Mitigations

- **Risk**: `claude_dispatch` latency per call is higher than the
  current subprocess (structured-output parsing + JSON-schema
  validation overhead).
  **Mitigation**: Measure during MVV; raise the observation if it
  matters. The labeler is already batched; per-dispatch overhead is
  amortized.
- **Risk**: Glossary format in prompt is brittle — model ignores or
  misinterprets.
  **Mitigation**: RF-1 measured this; the bullet-list format is the
  empirically-tested shape.
- **Risk**: Adversarial cases (SSMF-class) not fully fixed.
  **Mitigation**: Acknowledged in RF-1; follow-up RDR adds per-operator
  model override if real-world projects hit this.
- **Risk**: `asyncio.run()` inside ThreadPoolExecutor worker crashes.
  **Mitigation**: Standard Python asyncio contract — worker threads
  have no event loop; `asyncio.run()` is safe. Tested in integration.

### Failure Modes

- `claude_dispatch` exception → `[None] * len(items)`; taxonomy code
  uses c-TF-IDF fallback. No corrupt DB writes.
- Malformed schema response → schema enforcement rejects; caught as
  `claude_dispatch` exception; same fallback.
- Glossary load failure → log warning, pass empty string, labeler
  proceeds with today's behavior.

## Implementation Plan

### Prerequisites

- [x] RDR-080 shipped — `claude_dispatch` substrate.
- [x] RDR-081 shipped — `taxonomy.collection_prefixes` established the
  `taxonomy` config section shape.
- [ ] Critical Assumption 2 (latency envelope) — verified by MVV.

### Minimum Viable Validation

1. Register a 3-term glossary in `.nexus.yml#taxonomy.glossary` on a
   real Nexus repo.
2. Run `nx taxonomy relabel -c <collection>` on a collection that
   contains at least one topic cluster with a known-ambiguous
   acronym.
3. Verify: (a) the label contains the project-term expansion, not
   the training-prior default; (b) non-ambiguous topics unchanged or
   improved; (c) labeling succeeds for all topics (no silent
   schema-reject drops).

### Phase 1: Code Implementation

#### Step 1: Config + glossary resolver

- Add `taxonomy.glossary` (dict) to `.nexus.yml` schema.
- Implement `src/nexus/glossary.py`.
- Unit tests: fixture `.nexus.yml` + `docs/glossary.md`, verify
  priority + truncation.

#### Step 2: Labeler migration

- Rewrite `_generate_labels_batch` as `async def`, dispatching via
  `claude_dispatch` with `_LABEL_SCHEMA`.
- Update `relabel_topics` to wrap with `asyncio.run()` inside
  ThreadPoolExecutor workers.
- Update the post-discover auto-label banner path (`index.py:187`) to
  use the same pathway.
- Delete subprocess / regex-parse scaffolding.
- Unit tests: mock `claude_dispatch`, assert schema-conforming
  response produces labels; assert `len(labels) == len(items)`
  invariant; assert glossary appears in the prompt.

#### Step 3: Documentation + release notes

- Update `docs/cli-reference.md` — `nx taxonomy label` mentions
  glossary.
- Update `docs/configuration.md` — `taxonomy.glossary` key.
- Update `docs/taxonomy.md` — glossary section.
- Entry in `CHANGELOG.md`.

### Phase 2: Operational Activation

#### Activation Step 1: Dogfood on Nexus taxonomies

- Author a short Nexus glossary (CCE, T1/T2/T3, tumbler, chash, etc.);
  run `nx taxonomy relabel` against a small collection; confirm label
  improvements on at least one previously-ambiguous topic.

### New Dependencies

None. Reuses shipped `claude_dispatch`.

## Test Plan

- **Scenario**: Glossary-aware labeling on a known-ambiguous topic
  → label contains the project-term expansion.
- **Scenario**: No glossary configured → `claude_dispatch` called
  without glossary preamble; label quality at or above current.
- **Scenario**: `claude_dispatch` raises → all labels `None`; taxonomy
  uses c-TF-IDF fallback.
- **Scenario**: Schema-rejection (malformed response) → same as above.
- **Scenario**: `len(items) == len(results)` invariant holds for
  partial-batch responses (some topics missing labels).
- **Scenario**: Post-discover auto-label path also uses the new
  pathway (banner text updated).

## Validation

### Testing Strategy

1. **Unit** — glossary resolver priority + truncation; labeler
   dispatch shape; invariant tests.
2. **Integration** — mock `claude_dispatch` returning schema-conforming
   dict; end-to-end `relabel_topics` run.
3. **Manual smoke** — against a real taxonomy with glossary configured;
   verify label improvement on ambiguous topic.

### Performance Expectations

Per-batch labeler latency: RDR-080 `claude_dispatch` amortized envelope
(~10-30s per call; structured-output parsing overhead). Existing
subprocess envelope: 13-27s per batch. Expected neutral-to-slightly-
faster; measured during MVV.

## References

- `docs/field-reports/2026-04-15-architecture-as-code-from-art.md`
  §3.1/F2 — original SSMF failure case
- RDR-070 (taxonomy + HDBSCAN)
- RDR-080 (`claude_dispatch` substrate)
- RDR-081 (taxonomy config section; superseded-part)
- `nexus-axu` (closed) — prompt-cache persistence across rewound
  `claude -p --resume` sessions, measured 2026-04-15. Informed the
  decision to defer session-reuse optimization (see Follow-up).

## Follow-up (out of scope)

- **Per-operator model override** (Haiku → Sonnet for labeling when
  the glossary is known insufficient). Files as a separate RDR once
  a concrete project demonstrates the need.
- **Auto-extracted glossary** from co-token clustering. Speculative.
- **Cross-collection glossary merging** (when two collections share
  vocabulary). Easy extension of `load_glossary`.
- **Session-based prompt-cache reuse** across batches. Evaluated
  2026-04-16: the `nexus-axu` Phase A measurement (47–97k
  `cache_read` tokens per post-rewind turn on Haiku) confirms the
  mechanism works for single-caller batched workloads, but the
  current labeler envelope (13–27s per batch × 4 parallel workers
  on typical 50-topic runs → ~20-25s wall clock) is already
  comfortable. Adding per-worker session UUIDs with cumulative-
  context rotation is real complexity for pennies of savings on
  today's workload. Reopen if observed runs exceed a minute and
  the input-token fraction dominates.
