---
title: "Developer Agent Circuit Breaker for Test Failure Escalation"
id: RDR-040
type: Architecture
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-25
accepted_date: 2026-03-25
related_issues: []
---

# RDR-040: Developer Agent Circuit Breaker for Test Failure Escalation

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The `nx:developer` agent thrashes on test failures instead of escalating to the debugger. The existing "Automatic Escalation Triggers" section in the developer agent prompt says to *recommend* debugger after 2 failures, but this is advisory — the agent outputs a "Next Step" recommendation at the *end* of its run, by which point it has already wasted 10+ minutes going in circles.

Observed: developer agent spent 400+ lines of output (10+ minutes) re-reading source code and hypothesizing without running diagnostics. After manual intervention, the debugger agent solved the same issue in 3 minutes with a 2-line fix.

## Context

### Background

Incident: 2026-03-25, ART project, RDR-029 Wave 5. Developer agent implementing Chunk Representation POC (ART-63hr). 3 of 6 tests failing — `MaskingField.update()` called sequentially didn't form chunks because ShuntingDynamics decayed first node's activation before second pattern was presented.

The developer agent:
- Read MaskingField source code repeatedly
- Hypothesized about activation wash-out
- Re-read the same methods looking for the "real" cause
- Self-admitted "I'm going in circles"
- Never wrote a diagnostic test

The debugger agent:
1. Confirmed the hypothesis (sequential `update()` vs batch `processTemporalPattern()`)
2. Checked the passing Wave 3 POC test that used the batch API
3. Changed both production code and test to use `processTemporalPattern()`
4. All 6 tests green in ~3 minutes

Full incident memo: T2 `nexus_active/investigation-developer-agent-thrashing-debugger-underuse`

### Technical Environment

- Developer agent: `nx/agents/developer.md` (sonnet model)
- Debugger agent: `nx/agents/debugger.md` (opus model)
- Development skill: `nx/skills/development/SKILL.md`
- Orchestration skill: `nx/skills/orchestration/SKILL.md`
- Known constraint: subagents cannot spawn other subagents

## Research Findings

### Investigation

Reviewed developer agent prompt (`developer.md`):

- Lines 187-195: "Automatic Escalation Triggers" — advisory only. Says "Recommend **debugger** (via Next Step output) if test failures after 2 fix attempts." This fires at completion time, not at failure time.
- Lines 103-116: "Recommended Next Step (MANDATORY output)" — mechanism is output-only; the agent continues working until it finishes, then suggests what to do next.
- Lines 169-175: "Problem-Solving Approach" — says to use sequential thinking but has no hard stop.

Reviewed development skill (`development/SKILL.md`):
- No instructions for the parent to detect failure escalation from the developer agent.
- No convention for structured failure output.

Reviewed orchestration skill (`orchestration/SKILL.md`):
- Pipeline shows `developer → code-review-expert → test-validator` but no `developer → debugger` escalation path.

### Key Discoveries

- **Verified** — Developer agent's escalation is output-only, fires at end of turn. By then, the damage (wasted context, wasted time) is done.
- **Verified** — Debugger agent solves these problems efficiently when given clear context (failing test, error, hypothesis). The gap is invocation timing, not capability.
- **Verified** — Subagents cannot spawn other subagents. The developer cannot launch a debugger; it can only signal the parent.
- **Documented** — The `using-nx-skills` routing table already says "Test fails → /nx:debug IMMEDIATELY" but this is advisory to the human, not enforced in agent prompts.

### Critical Assumptions

- [x] Developer agent will obey a hard stop instruction in its system prompt — **Status**: Verified — **Method**: Documented (Claude agents follow system prompt constraints when stated as mandatory/non-negotiable)
- [x] Parent conversation can parse a structured failure block from agent output and dispatch debugger — **Status**: Verified — **Method**: Documented (standard relay pattern already works this way)

## Proposed Solution

### Approach

Add a hard circuit breaker to the developer agent prompt that fires *during* execution, not at completion. After 2 consecutive test run failures (any run where one or more tests fail), the agent must stop and output a structured failure report. The parent conversation (or skill) then dispatches the debugger. The counter tracks test runs, not root causes — no ambiguity about "same issue" classification.

### Technical Design

#### 1. Developer agent (`developer.md`) — Circuit Breaker section

Add a new `## Circuit Breaker` section that overrides the advisory escalation:

```markdown
## Circuit Breaker (MANDATORY — overrides all other behavior including Completion Protocol)

**Track consecutive test failures.** Every time you run the test command and
one or more tests fail, increment your failure counter. A partial pass (some
tests pass, some fail) counts as a failure.

**Counter resets to 0 when:**
- Any test run ends with ALL tests green
- You are dispatched as a new agent invocation (fresh start)

Do not try to classify "same issue" vs "different issue." Count test runs,
not root causes.

**After 2 consecutive failures (counter reaches 2):**

1. **STOP immediately.** Do not read more source code. Do not try another fix.
2. **Output ONLY the escalation report below.** Do NOT output the normal
   `## Next Step: code-review-expert` block — the circuit breaker supersedes
   the Completion Protocol.
3. **End your turn.**

<!-- ESCALATION -->
## ESCALATION: Debugger Required

**Failing test(s)**: [test name(s)]
**Error**: [exact error message or assertion failure]
**What I tried**:
1. [first attempt and result]
2. [second attempt and result]
**Hypothesis**: [your best guess at the root cause]
**Diagnostic suggestion**: [what a debugger should investigate first, or "none" if truly lost]

**This is not optional.** The debugger agent solves these problems in minutes.
Continuing past 2 failures wastes time — observed in production (ART RDR-029
Wave 5: developer thrashed 10+ min, debugger solved in 3 min).
```

Replace the current advisory "Recommend **debugger**" in the Automatic Escalation Triggers section with a cross-reference to the Circuit Breaker. Also add an exception clause to the Completion Protocol: "Exception: If the Circuit Breaker fires, do not output `## Next Step: code-review-expert` — the escalation block is your sole terminal output."

#### 2. Development skill (`development/SKILL.md`) — Parent-side dispatch

Add a `## Debugger Escalation` section after Agent Invocation:

```markdown
## Debugger Escalation

If the developer agent returns with an `## ESCALATION: Debugger Required` block
(detected by the `<!-- ESCALATION -->` sentinel or the H2 header):

1. **Do not re-dispatch the developer.** The circuit breaker fired for a reason.
2. **Dispatch the debugger immediately** using this relay template:

## Relay: debugger

**Task**: Diagnose test failure that developer could not resolve: [Failing test(s)]
**Bead**: [same bead as developer]

### Input Artifacts
- Error: [Error field from escalation report]
- Hypothesis: [Hypothesis field from escalation report]
- What was tried: [What I tried field — both attempts]
- Diagnostic suggestion: [Diagnostic suggestion field]
- Files: [files from original developer relay]

### Deliverable
Root cause analysis and fix with all tests passing

### Quality Criteria
- [ ] Root cause identified with evidence
- [ ] Fix implemented
- [ ] All failing tests now pass

3. After debugger resolves the issue, re-dispatch developer to continue
   the remaining plan steps.
```

#### 3. Orchestration skill — Add escalation edge

Add `developer → debugger` escalation path to the routing diagram (labeled `[on escalation]` — this is conditional, not equal-weight) and add a row to the quick reference table: `| Implement code (escalation) | debugger | developer circuit breaker → debugger → developer resumes |`

### Decision Rationale

The circuit breaker is in the agent prompt because that's where the behavior needs to change. The agent itself must stop; the parent cannot force-stop a running subagent. The structured failure report gives the debugger agent maximum context with minimum overhead.

A PostToolUse hook was considered and rejected — it would require regex-matching test output across multiple languages and build systems, is fragile, and doesn't solve the core problem (the agent is still running when the hook fires).

## Alternatives Considered

### Alternative 1: PostToolUse hook for test failure detection

**Description**: Hook watches Bash tool output for test failure patterns, warns the parent.

**Pros**: Works without changing agent prompts.

**Cons**: Fragile (regex across languages), doesn't stop the agent, adds complexity.

**Reason for rejection**: The agent-level circuit breaker is simpler and more reliable.

### Alternative 2: Developer agent spawns debugger subagent

**Description**: Developer agent directly launches debugger as a child agent.

**Pros**: No parent involvement needed.

**Cons**: Subagents cannot spawn other subagents — this is a known platform limitation.

**Reason for rejection**: Not currently possible.

### Briefly Rejected

- **Reduce developer agent to 1 failure attempt**: Too aggressive — legitimate first-attempt failures (typos, import order) would trigger unnecessary escalation.

## Trade-offs

### Consequences

- Positive: Developer agent stops wasting time on failures it can't solve
- Positive: Debugger agent gets invoked early with structured context
- Positive: Observed improvement in ART incident — 10+ min thrashing → 3 min resolution (N=1; Phase 2 validation should track additional incidents)
- Negative: Adds complexity to the agent prompt and skill
- Negative: 2-failure threshold may occasionally escalate prematurely for simple issues

### Risks and Mitigations

- **Risk**: Developer agent ignores the circuit breaker instruction
  **Mitigation**: Use strong language (MANDATORY, overrides all other behavior). If this proves insufficient, escalate to a PreToolUse hook.
- **Risk**: Structured failure report is malformed, debugger can't use it
  **Mitigation**: The template is simple (5 fields). Even partial output gives the debugger more than nothing.

### Failure Modes

- **Agent doesn't count failures correctly**: Worst case, it continues past 2 failures — same as current behavior. No regression.
- **Parent doesn't detect the escalation block**: The `## ESCALATION:` header is distinctive. If missed, the developer's output still contains the diagnostic, which the human can use to manually invoke debugger.

## Implementation Plan

### Prerequisites

- [ ] All Critical Assumptions verified (done — both verified)

### Phase 1: Prompt Changes

#### Step 1: Add Circuit Breaker to developer.md

Add `## Circuit Breaker` section. Update Automatic Escalation Triggers to cross-reference it.

#### Step 2: Add Debugger Escalation to development/SKILL.md

Add parent-side dispatch instructions with relay template.

#### Step 3: Update orchestration/SKILL.md

Add `developer → debugger` escalation edge to routing diagram and table.

### Phase 2: Validation

#### Step 1: Manual test

Dispatch developer agent on a task with a known test failure (e.g., the ART MaskingField scenario). Verify it stops after 2 failures and outputs the structured report. Verify parent can dispatch debugger with the report.

## Test Plan

- **Scenario**: Developer agent hits 2 consecutive test failures — **Verify**: Agent stops, outputs `## ESCALATION: Debugger Required` with all 5 fields populated
- **Scenario**: Developer agent hits 1 failure then succeeds — **Verify**: No escalation, work continues normally
- **Scenario**: Parent receives escalation block — **Verify**: Debugger dispatched with failure context, resolves issue
- **Scenario**: Developer agent hits 1 failure, then all tests go green, then a different test fails — **Verify**: Counter reset by the green run; new failure starts counter at 1, not 2
- **Scenario**: Partial pass (3 of 5 tests pass, 2 fail) — **Verify**: Counts as a failure, counter increments

## References

- T2 incident memo: `nexus_active/investigation-developer-agent-thrashing-debugger-underuse`
- Developer agent: `nx/agents/developer.md`
- Development skill: `nx/skills/development/SKILL.md`
- Orchestration skill: `nx/skills/orchestration/SKILL.md`
- Known limitation: subagents cannot spawn other subagents (CLAUDE.md)

## Revision History

### Gate Review (2026-03-25)

### Critical — Resolved

**C1. Counter-reset semantics undefined — RESOLVED.** "Same issue" vs
"different issue" was ambiguous. Fixed: counter tracks consecutive test runs
(not root causes). Resets to 0 on any fully green run or new agent invocation.
Partial passes (some tests fail) count as failures. No "same issue"
classification needed.

**C2. `## ESCALATION:` header conflicts with mandatory Completion Protocol —
RESOLVED.** The Circuit Breaker said "end your turn" but the Completion
Protocol mandated `## Next Step: code-review-expert`. Fixed: Circuit Breaker
explicitly supersedes Completion Protocol. Added `<!-- ESCALATION -->` HTML
sentinel for reliable detection. Added exception clause to Completion Protocol
in implementation plan.

### Significant — Resolved

**S1. No debugger relay template — RESOLVED.** The skill addition said
"dispatch debugger" but didn't show how to map escalation fields to the relay.
Fixed: full relay template added with explicit field mapping (Failing tests →
Task, Error/Hypothesis/Attempts → Input Artifacts).

**S2. Partial pass definition missing — RESOLVED.** "Consecutive test
failures" didn't define partial passes. Fixed: "A partial pass (some tests
pass, some fail) counts as a failure." Added test plan scenario for this case.

### Observations — Applied

- O1: Orchestration diagram edge labeled `[on escalation]` (not equal-weight)
- O2: "Diagnostic suggestion" field marked as accepting "none" if agent is lost
- O3: "3 minute resolution" reworded as N=1 observation with note to track more
- O4: Critical assumptions acknowledged as "Documented" pre-verification; Phase 2 manual test is the real verification
