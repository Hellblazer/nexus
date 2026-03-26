---
title: "T1 Scratch Inter-Agent Context Sharing"
id: RDR-041
type: Architecture
status: accepted
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-26
accepted_date: 2026-03-26
related_issues:
  - "RDR-040 - Developer Agent Circuit Breaker (closed)"
---

# RDR-041: T1 Scratch Inter-Agent Context Sharing

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Relay-reliant agents (developer, code-review-expert, debugger, test-validator, plan-auditor) never check T1 scratch before starting work. The CONTEXT_PROTOCOL explicitly tells them to "rely on relays" and not proactively search. This means they are blind to findings their predecessors left in scratch during the same session.

When a developer struggles with an edge case for 5 minutes, eventually solves it, and hands off to the code reviewer — the reviewer has no idea that section is fragile. When a debugger is invoked ad-hoc (not via circuit breaker), it doesn't know what the developer already tried. The relay carries the structured handoff, but unstructured session context — failed approaches, surprising discoveries, intermediate hypotheses — is lost.

## Context

### Background

RDR-040 introduced the circuit breaker, which captures the developer's last 2 failed attempts in a structured ESCALATION report. This works for the escalation path but doesn't address:

1. Failed approaches that don't trigger escalation (first failure, then success on second attempt)
2. Developer discoveries that inform code review focus areas
3. Cross-agent context when agents are invoked ad-hoc (not via pipeline relay)
4. Planner/auditor findings that should inform the developer's approach

### Technical Environment

- CONTEXT_PROTOCOL: `nx/agents/_shared/CONTEXT_PROTOCOL.md`
- T1 scratch: session-scoped ChromaDB via MCP tools (`scratch`, `scratch_manage`)
- Shared across all agents in a session via HTTP server (PPID chain)
- Tags supported but no standardized vocabulary

## Research Findings

### Investigation

Reviewed CONTEXT_PROTOCOL (lines 9-35):

- **Proactive Search Agents** (planner, architect, researcher, analyzer): search T1/T2/T3 before starting. T1 is step 5 in their search order.
- **Relay-Reliant Agents** (developer, reviewer, debugger, test-validator, plan-auditor): told to "rely on relays" and NOT proactively search. They only hit RECOVER (which includes T1 search) when the relay is incomplete.

Reviewed developer agent (`developer.md`):
- Writes checkpoints to scratch (line 126-130): `scratch tool: action="put", content="Checkpoint: {step} complete"` with tag `impl,checkpoint`
- Does NOT write failed approaches — only successful checkpoints
- Circuit breaker captures last 2 failures but only in the escalation report, not in scratch

Reviewed code-review-expert agent:
- No scratch reads at any point
- Receives relay with file list, reviews blind to developer's session experience

Reviewed debugger agent:
- Reads relay context (failure description)
- No scratch search for prior attempts (unless relay is incomplete → RECOVER)

### Key Discoveries

- **Verified** — Relay-reliant agents skip T1 entirely in the happy path. Only RECOVER (incomplete relay) triggers a scratch search.
- **Verified** — Developer writes checkpoints but not failed approaches. The gap between "what I tried" and "what worked" is invisible to successors.
- **Verified** — Scratch tags exist but have no standardized vocabulary. Agents use ad-hoc tags (`impl`, `checkpoint`, `hypothesis`) with no cross-agent convention.
- **Documented** — T1 scratch search is a single MCP call (~100ms). Adding it to relay-reliant agents has negligible cost.

### Critical Assumptions

- [x] Scratch search returns useful results when predecessors have written to it — **Status**: Verified — **Method**: Source Search (scratch uses ChromaDB DefaultEmbeddingFunction, semantic matching works for natural-language findings)
- [x] Adding a SHOULD (not MUST) scratch check won't add meaningful latency — **Status**: Verified — **Method**: Documented (single MCP call, returns empty if no entries)

## Proposed Solution

### Approach

Four targeted changes to agent prompts and the CONTEXT_PROTOCOL. All are prompt/doc changes — no Python code. The pattern follows RDR-040: small, specific, measurable.

### Technical Design

#### 1. CONTEXT_PROTOCOL — Standardized scratch tag vocabulary

Add to the `## T1 — Session Scratch` section, after the "When to use T1" list:

```markdown
### Standard Scratch Tags

All agents SHOULD use these tags when writing to scratch:

| Tag | Meaning | Written by | Useful for |
|-----|---------|-----------|------------|
| `impl` | General implementation work (combine with others) | developer | any successor |
| `checkpoint` | Implementation step completed | developer | reviewer, test-validator |
| `failed-approach` | Attempted fix/approach that didn't work | developer, debugger | reviewer, debugger |
| `hypothesis` | Working theory about a problem | debugger, analyst | developer |
| `discovery` | Unexpected finding during work | any agent | any successor |
| `decision` | Design/approach choice made during work | planner, architect | developer |

Note: `impl` is already in production use (developer.md writes
`tags="impl,checkpoint"`). It is a combination tag, not standalone.

Tags are comma-separated. Combine with domain tags: `failed-approach,auth,retry`.
```

#### 2. CONTEXT_PROTOCOL — Sibling context for relay-reliant agents

Add to the `### Relay-Reliant Agents` section, after the agent list:

```markdown
**Sibling context (SHOULD, not MUST):** Before starting work, relay-reliant
agents SHOULD search scratch for predecessor findings:

Use scratch tool: action="search", query="[task topic]", n=5

If results exist, incorporate them as supplementary context. If scratch is
empty, proceed normally. This adds one MCP call (~100ms) and provides
context that relays may omit.

**Precedence rule:** Relay context takes precedence over scratch context.
Scratch entries are hints, not authoritative. If a scratch `decision` entry
conflicts with the relay, proceed per the relay and note the discrepancy.
```

#### 3. Developer agent — Write failed approaches to scratch

Add to the developer agent's `## Problem-Solving Approach` section:

```markdown
**Record failed approaches (SHOULD).** When you try a fix and it doesn't
work (but you haven't hit the circuit breaker yet), write a brief scratch
entry:

Use scratch tool: action="put", content="Failed approach: [what you tried]
→ [why it didn't work]", tags="failed-approach,[domain]"

This gives the code reviewer and any future debugger context about what
was already ruled out.
```

Additionally, add a MUST write to the Circuit Breaker section (from RDR-040).
Before outputting the ESCALATION block, the developer MUST write each failed
attempt to scratch:

```markdown
**Before outputting the ESCALATION block**, write your failed attempts to
scratch (MANDATORY — this is the highest-value moment for successor context):

Use scratch tool: action="put", content="Failed approach 1: [what you tried]
→ [result]", tags="failed-approach,[domain]"
Use scratch tool: action="put", content="Failed approach 2: [what you tried]
→ [result]", tags="failed-approach,[domain]"
```

This creates a two-tier write strategy:
- **SHOULD** write after each non-escalation failure (best effort)
- **MUST** write at circuit breaker trigger (guaranteed — agent is stopped)

The MUST write ensures scratch always has content when the circuit breaker
fires, even if the SHOULD writes were skipped. The escalation report and
scratch entries carry redundant information in the escalation path — this
is intentional (scratch serves successors; the escalation report serves
the debugger relay).

#### 4. Code-review-expert — Check scratch for developer struggles

Add to the code-review-expert agent's review process, before starting the review:

```markdown
**Check for developer context.** Before reviewing, search scratch for the
developer's session experience. Run two searches:

Use scratch tool: action="search", query="failed approach what was tried didn't work", n=5
Use scratch tool: action="search", query="implementation checkpoint step completed", n=5

If the developer struggled with specific areas (tagged `failed-approach`),
focus extra review attention there — code that was hard to get right is
more likely to have subtle issues.
```

Note: ChromaDB semantic search does not support boolean OR. Use
natural-language queries that semantically match the target content.
Two focused queries are more precise than one broad query.

### Existing Infrastructure Audit

| Proposed Change | Existing Module | Decision |
|---|---|---|
| Tag vocabulary | CONTEXT_PROTOCOL T1 section | Extend — add standard tags table |
| Sibling context | CONTEXT_PROTOCOL Relay-Reliant section | Extend — add SHOULD search |
| Failed approaches | developer.md Problem-Solving | Extend — add scratch write |
| Review focus | code-review-expert.md | Extend — add scratch read |

### Decision Rationale

These changes use existing T1 infrastructure — no new tools, no new MCP calls, no Python changes. The SHOULD (not MUST) approach for scratch reads avoids adding mandatory overhead. The tag vocabulary creates a shared language without enforcing structure. Failed approach recording fills the gap between the circuit breaker (which captures the last 2 failures at escalation time) and normal development flow (where struggles are invisible).

## Alternatives Considered

### Alternative 1: Mandatory T1/T2/T3 scan for all agents

**Description**: Make all agents proactively search all tiers before starting.

**Pros**: Comprehensive context.

**Cons**: 3+ tool calls per agent, most return nothing, adds latency to every invocation.

**Reason for rejection**: The proactive search agents already do this where it matters. Relay-reliant agents are invoked frequently and should stay fast.

### Alternative 2: PostToolUse hook to auto-capture failed test output

**Description**: Hook watches Bash output for test failures, auto-writes to scratch.

**Pros**: No agent prompt changes needed.

**Cons**: Fragile regex across languages, captures test output but not the developer's interpretation of WHY it failed.

**Reason for rejection**: The developer's "what I tried and why it didn't work" is more valuable than raw test output. Agent-written scratch entries carry intent.

### Briefly Rejected

- **Structured scratch schema**: Over-engineering. Free-text with tags is sufficient for inter-agent hints.
- **Relay enrichment middleware**: A hook that auto-appends scratch context to relays. Complex, fragile, and the agent can do its own search.

## Trade-offs

### Consequences

- Positive: Code reviewer knows where the developer struggled — focused review
- Positive: Ad-hoc debugger invocations get predecessor context from scratch
- Positive: Standardized tags make scratch searchable by intent across agents
- Positive: Zero cost when scratch is empty (single call returns nothing)
- Negative: Developer writes more to scratch — slight overhead per failed approach
- Negative: SHOULD semantics mean agents may skip the scratch check

### Risks and Mitigations

- **Risk**: Agents ignore the SHOULD scratch check
  **Mitigation**: Monitor in practice. If consistently skipped, promote to MUST for specific agents (code-review-expert being the highest-value target).
- **Risk**: Scratch fills with noise from too many `failed-approach` entries
  **Mitigation**: Tags enable filtering. Scratch is session-scoped — wiped at end. Noise is bounded.

### Failure Modes

- **Scratch server not running**: Graceful degradation — MCP call returns error, agent proceeds without context. Same as current behavior.
- **No predecessor entries**: Search returns empty. Agent proceeds normally. Zero regression.
- **Tags used inconsistently**: Search still works via semantic matching on content. Tags improve precision but aren't required for recall.

## Implementation Plan

### Prerequisites

- [ ] All Critical Assumptions verified (done — both verified)

### Phase 1: CONTEXT_PROTOCOL changes

#### Step 1: Add tag vocabulary table

Add standard scratch tags table to T1 section of CONTEXT_PROTOCOL.

#### Step 2: Add sibling context suggestion

Add SHOULD scratch search to relay-reliant agents section.

### Phase 2: Agent prompt changes

#### Step 3: Developer — write failed approaches

Add failed approach scratch writing to Problem-Solving section.

#### Step 4: Code-review-expert — check scratch

Add scratch search before review start.

### Phase 3: Validation

#### Step 1: Pipeline test

Run a developer → code-review-expert pipeline. Verify:
- Developer writes `failed-approach` entries when attempts fail
- Code reviewer finds and references those entries in its review

## Test Plan

- **Scenario**: Developer hits 1 failure then succeeds — **Verify**: `failed-approach` entry written to scratch with meaningful content
- **Scenario**: Code reviewer starts after developer — **Verify**: Reviewer searches scratch, finds `failed-approach` entries, focuses review on those areas
- **Scenario**: Scratch is empty (no predecessor ran) — **Verify**: Reviewer proceeds normally, no error, no delay
- **Scenario**: Debugger invoked ad-hoc with a complete relay — **Verify**: Debugger searches scratch per sibling context SHOULD (Change 2), finds developer's failed approaches if present (does NOT rely on RECOVER)
- **Scenario**: Tags used correctly — **Verify**: `failed-approach`, `checkpoint`, `discovery` tags present on relevant entries

## References

- CONTEXT_PROTOCOL: `nx/agents/_shared/CONTEXT_PROTOCOL.md`
- Developer agent: `nx/agents/developer.md`
- Code-review-expert agent: `nx/agents/code-review-expert.md`
- RDR-040: Developer Agent Circuit Breaker (predecessor — structured escalation handoff)
- T1 scratch implementation: `src/nexus/db/t1.py`, `src/nexus/session.py`

## Revision History

### Gate Review (2026-03-26)

### Critical — Resolved

**C1. Scratch search uses fake boolean OR — RESOLVED.** `"failed-approach OR
checkpoint"` is not boolean OR in ChromaDB semantic search. Fixed: replaced
with two separate natural-language queries that semantically match target
content. Added note explaining ChromaDB does not support boolean OR.

**C2. SHOULD write semantics too weak at failure moment — RESOLVED.** Added
two-tier write strategy: SHOULD after each non-escalation failure (best
effort), MUST at circuit breaker trigger (guaranteed — agent is already
stopped). The MUST write is added to the Circuit Breaker section from
RDR-040. Scratch entries and escalation report carry intentionally redundant
information in the escalation path.

### Significant — Resolved

**S1. Missing `impl` tag from vocabulary — RESOLVED.** Added `impl` to the
standard tags table with note that it's already in production use as a
combination tag in developer.md.

**S2. No relay-over-scratch precedence rule — RESOLVED.** Added explicit
precedence rule to Change 2: "Relay context takes precedence over scratch
context. Scratch entries are hints, not authoritative."

**S3. Ad-hoc debugger test scenario incorrect — RESOLVED.** Fixed: scenario
now tests sibling context SHOULD search (Change 2), not RECOVER. Debugger
searches scratch directly, not via incomplete-relay fallback.

### Observations — Applied

- O1: Acknowledged intentional redundancy between scratch writes and escalation report
- O2: Validation is non-deterministic (LLM behavior); plan for manual spot-checks
- O3: `discovery` tag has no write trigger — accepted as aspirational; agents may use it organically
- O4: Replaced fragile line number references with section heading anchors
