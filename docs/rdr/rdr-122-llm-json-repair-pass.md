---
title: "LLM-JSON Repair Pass: Port a2ui PayloadFixer Pattern to nx Structured-Output Parsers"
id: RDR-122
type: Technical
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-19
accepted_date:
related_issues: []
related_rdrs: [RDR-119]
related_tests: []
implementation_notes: ""
---

# RDR-122: LLM-JSON Repair Pass — Port a2ui PayloadFixer Pattern to nx Structured-Output Parsers

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Nexus parses LLM-generated JSON in several hot paths: `plan_json` (plan library save/match), RDR frontmatter assembly, catalog payload generation, structured operator outputs (`extract`, `summarize`, `generate`). Each of these uses a fail-then-retry loop: schema validation rejects malformed output, the caller re-prompts the LLM with the error, the LLM regenerates. Retry cost is non-trivial — a regeneration of a 4k-token plan after a single trailing-comma is 4k tokens of waste plus a round trip.

a2ui (Google's Agent-to-User Interface project) hit the same problem first and solved it with a `PayloadFixer` repair pass interposed between LLM output and schema validation. The fixer applies a fixed set of mechanical repairs (trailing commas, unbalanced braces, smart quotes, JSON-in-markdown extraction, common key misspellings registered in the catalog) before validation runs. Their conformance suite shows repair cuts retry rate substantially on long generations and is **strictly additive** — validation still runs after, so the contract is preserved.

Nexus's parsers have no equivalent layer. Every malformed token is a full retry.

## Context

### Background

The a2ui repository (`/Users/hal.hildebrand/git/a2ui`) ships `PayloadFixer` in both Python (`agent_sdks/python/src/a2ui/parser/payload_fixer.py`) and Kotlin SDKs. It runs after `StreamingParser` and before `A2uiValidator` (architecture analysis in T3 `architecture-a2ui-overview`). The fixer is intentionally narrow: it only repairs **mechanical** errors that the LLM produced under tokenization pressure, never **semantic** errors that would require model reasoning to fix. Semantic errors still bounce to retry.

The relevant precedent in nexus is RDR-119 (cockpit UI fabric) which already adopts a2ui as the wire-level surface descriptor. Borrowing a2ui's parser-side hardening is a coherent extension — same protocol, same pre-validation layer.

### Technical Environment

Existing nexus parsers and their malformed-output behavior:

- **`plan_save` / `plan_match`** — JSON schema validation in `nexus/db/plan_library.py`. Validation failures bounce to caller with no repair attempt.
- **RDR frontmatter generators** — `/conexus:rdr-create` skill builds YAML+JSON; the writing scripts in `conexus/hooks/scripts/` are tolerant but produce silent malformed entries when LLM output is close-but-wrong.
- **Operators `extract` / `summarize` / `generate`** — return JSON; downstream callers `json.loads` and propagate parse errors.
- **Catalog payload assembly** — `tools/build_catalog` paths.

### Constraints

- **Strict additivity.** The repair pass must never change valid output. Round-trip on already-valid JSON must be a no-op.
- **No semantic repair.** If repair requires understanding what the LLM *meant*, that's retry territory, not fixer territory.
- **Observable.** Every repair must emit a structured event (kind, before-byte, after-byte) so we can measure repair rate per parser and tune the fixer.
- **Cross-language parity not required.** Python-only initially; Kotlin SDK already has its own fixer if/when a Kotlin-side nexus parser appears.

## Decision

Introduce a `nexus.parsers.repair` module with a single entry point `repair_json(text: str, schema_id: str | None = None) -> tuple[str, list[RepairEvent]]`. Wire it into the four parsers above as a pre-validation pass. Default-on; togglable per call site via `repair=False` for parsers that need raw LLM output (eval harness, for instance).

### Repair classes (v1)

1. **Markdown-fence stripping** — extract JSON from triple-backtick blocks if the entire response is wrapped.
2. **Trailing comma removal** — in objects and arrays.
3. **Unbalanced delimiter close** — append missing `}`/`]` if the AST is otherwise parseable and the imbalance is at end-of-string.
4. **Smart-quote → ASCII-quote** normalization (`"` `"` `'` `'` → `"`/`'`).
5. **JS-style comment stripping** (`//` line, `/* */` block) — LLMs sometimes annotate.
6. **Common key misspellings** when a schema_id is provided: leventshein-1 against the schema's known property names. Only triggers if the property is not a valid name in the schema.
7. **Bare-key quoting** — JS-style object literals `{key: "value"}` → `{"key": "value"}`.

Repairs are applied in fixed order, each idempotent. After all repairs run, the result is fed to `json.loads`; if that fails, return the original (not the partial repair) and emit a `repair_unrecoverable` event so the caller's retry path engages.

### Wiring

| Call site | Default | Toggle |
|---|---|---|
| `plan_save` / `plan_match` | repair on | `repair=False` env-flag for benchmark runs |
| RDR frontmatter writers | repair on | n/a (these never want raw) |
| Operators | repair on | per-call kwarg for eval framework |
| Catalog payload | repair on | n/a |

## Alternatives Considered

### Alt 1: Tolerant JSON parser (json5, demjson3, dirty-json)

A library that accepts a permissive JSON dialect. Rejected because:
- We lose the validation gate. A tolerant parser will happily parse `{trailing: "comma",}` but downstream code expects strict JSON semantics.
- No per-repair telemetry — we can't measure which classes of error are worth modeling differently in prompts.
- Couples nexus to a third-party dialect rather than to a narrow internal contract.

### Alt 2: Prompt-time only (better few-shot, stricter system prompts)

Improving prompts so LLMs produce valid JSON more often. Rejected as **complementary, not alternative**: prompts already include schema and examples, and the residual failure rate is what motivates this RDR. Repair operates on what prompts couldn't prevent.

### Alt 3: Constrained decoding (grammar-constrained sampling)

Force the model to emit only schema-valid tokens. Rejected because:
- Not available for the Claude API surface nexus uses.
- Constrained decoding interacts badly with streaming.
- Locks us to a specific inference backend.

### Alt 4: Status quo (fail-then-retry)

Current behavior. Quantified retry cost is the rejection reason — see Success Criteria for the metric we'll use to validate the change.

## Consequences

### Positive

- Lower retry rate on `plan_save` and operator paths — direct cost reduction.
- Structured telemetry on LLM-output failure modes feeds back into prompt iteration.
- Pattern available for future structured-output parsers (RDR-N for tool-call argument repair, e.g.).

### Negative

- A new layer to maintain. Each repair class is a small temptation to add "just one more" — must keep the bar at *mechanical, fixed-set*.
- Risk of masking a prompt regression: if a prompt change makes the LLM emit malformed output 10× more, repair hides the symptom. Mitigate via per-parser repair-rate dashboards (existing T3 telemetry channel).

### Neutral

- Eval framework must opt out (`repair=False`) when measuring raw LLM JSON quality.

## Success Criteria

- [ ] `repair_json` shipped with v1 repair classes and unit tests covering each class as both repair-applies and idempotent-on-valid.
- [ ] Wired into `plan_save`, `plan_match`, RDR frontmatter writers, and the three operators listed above.
- [ ] Repair telemetry emitted to T2 per-parser counters.
- [ ] Measurable retry-rate reduction on `plan_save` in a 1-week observation window after rollout (target: ≥30% reduction in `plan_save_retry` events). If not met, RDR is wrong — iterate.

## Open Questions

1. **Streaming repair** — a2ui's StreamingParser handles incremental output; nexus's operators mostly return complete payloads. Worth a streaming repair API for future operators that stream? Defer to follow-up RDR.
2. **Catalog of known schemas** — schema_id-driven repairs (class 6) need a schema registry. Use existing `nexus.schemas` or new? Defer to implementation.
3. **Should retry path consume repair events?** — if repair *almost* worked (got to one event short of success), the retry prompt could include the partial repair as a hint. Out of v1 scope but interesting.

## References

- a2ui architecture analysis — T3 doc `architecture-a2ui-overview` (2026-05-19)
- a2ui `PayloadFixer` source — `agent_sdks/python/src/a2ui/parser/payload_fixer.py` in `/Users/hal.hildebrand/git/a2ui`
- RDR-119 — adopts a2ui as cockpit wire-level descriptor (precedent for borrowing from a2ui)
