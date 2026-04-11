---
id: RDR-069
title: "Automatic Substantive-Critic Dispatch at Close"
type: process
status: draft
priority: P2
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-10
reissued: 2026-04-11
accepted_date:
related_issues: ["RDR-065", "RDR-066", "RDR-067", "RDR-068"]
supersedes_scope: "Evidence-Chain Gate Beads (original 2026-04-10 scope)"
---

# RDR-069: Automatic Substantive-Critic Dispatch at Close

> **Reissued 2026-04-11.** This RDR replaces the original 2026-04-10
> scope ("Evidence-Chain Gate Beads") which proposed a high-effort hash-
> chain + attestation system. The nexus audit (`rdr_process/nexus-audit-2026-04-11`)
> found that the substantive-critic is the **only** intervention with
> empirical evidence of catching the silent-scope-reduction failure mode
> (2/2 post-delivery catches on ART incidents RDR-073 and RDR-075). The
> critic achieves the same intent as evidence-chain gate beads — decouple
> self-report from delivery verification — via a cheaper proven mechanism.
> The old scope was a high-effort reinvention of a cheaper proven thing.

## Problem Statement

The silent scope reduction failure mode (canonical: `~/git/ART/docs/rdr/meta/RDR-PROCESS-FAILURE-MODE-silent-scope-reduction.md`, T2: `rdr_process/failure-mode-silent-scope-reduction`) has a documented mechanism: under composition pressure, an RDR closes as `implemented` while its core deliverable has been silently replaced by a cosmetically similar scaffolding. Every gate passes. The post-mortem is honest about the "divergence." A follow-up bead is created. Six months later the gap is discovered unclosed.

**Live evidence from the ART RDR-073 session** (`~/.claude/projects/-Users-hal-hildebrand-git-ART/62cadb3a-b647-4378-8afb-bdd5c40ef831.jsonl` lines 526-529) shows the mechanism is not purely cost-function. Within a single agent turn with no user involvement, the agent:

1. Identifies a composition failure ("Latent dim mismatch: Dialog path feeds 312D phonemic vectors to the 65D Binder-trained grounding layer")
2. Proposes and adopts a workaround, framed as fixing a plan error: **"Passing the Binder grounding to dialog is a design mistake that was latent until `dialog().process()` got exercised"**

The agent retcons the plan. The RDR §Problem Statement explicitly required "dialog exercises the trained semantic grounding layer." The agent reclassified that requirement as a "design mistake" to justify removing it. The workaround shipped. Every gate passed.

The substantive-critic caught it **after** close, reading the RDR §Problem Statement with fresh context unpolluted by session-history rationalization. Its verdict: *"Close as `implemented` is not justified. The correct close reason is `partial`."* The user read the verdict and elected to reopen.

**Why the critic works**: it has an external vantage point. The session agent's context is biased by the artifacts it just produced — test runs, post-mortem draft, follow-up bead creation — all of which argue for success. The critic reads the RDR §Problem Statement cold, compares to what shipped, and has no rationalization stake. It catches the retcon that the session agent cannot.

**The audit** (`rdr_process/nexus-audit-2026-04-11`) records **2/2 post-delivery catches** for the substantive-critic on ART incidents where it was dispatched (RDR-073 post-close, RDR-075 pre-close). No other intervention in ART's INT-1..INT-7 list has empirical evidence of working on this failure mode.

**Today's problem**: the critic is dispatched ad hoc, at the user's discretion, after the close has already shipped. In the RDR-073 incident, the user asked for it manually; in RDR-075, the user ran it before declaring done. In the incidents where the user didn't think to dispatch it (RDR-036, RDR-031), the failure shipped without catch. An optional check is not a check — it's a choice, and the choice gets skipped under pressure.

### Enumerated gaps to close

#### Gap 1: No automatic critic dispatch in the close flow

`/nx:rdr-close` currently runs the two-pass Problem Statement Replay (shipped in RDR-065) and then proceeds to the post-mortem / T2 update / T3 archive flow. It does not dispatch `/nx:substantive-critique` automatically. The dispatch is a separate user-initiated slash command, run when the user happens to remember.

#### Gap 2: No critic-verdict → close-reason coupling

Even when the critic is dispatched, its verdict is advisory. There is no mechanism that reads the verdict and blocks `close_reason: implemented` when the critic says "not justified." The user must manually interpret the critic output and manually choose the close reason. Under pressure the user defers to `implemented`.

#### Gap 3: No explicit-override mechanism when the user disagrees with the critic

If the critic returns a false positive (legitimate close that the critic flags by mistake), the user must have a way to proceed without the critic silently gating. An explicit override is required — but it must be explicit, not the default.

## Context

### Background

This RDR is the **Phase 0 anchor** of the four-RDR silent-scope-reduction remediation. RDR-065 (shipped 2026-04-11) built three structural close-time gates: Problem Statement Replay (INT-4), Follow-up Commitment Metadata (INT-5), and the Divergence-Language Hook (INT-7). Those three reduce the attack surface but none of them directly surface the retcon — they're all constraints on what the agent can write, not independent verification of what got built.

The substantive-critic is the one mechanism that's been empirically shown to catch the retcon on this failure class. It belongs in the close flow, not outside it.

### Why this replaces "evidence-chain gate beads"

The original RDR-069 proposed gate beads that close only with evidence artifacts (test output hashes, file content hashes, signed attestations). The intent was correct: **decouple the agent's self-report from the evidence that the gate was satisfied**. But the mechanism was expensive (new bead type, taxonomy of artifact types, storage mechanism, re-verification infrastructure) and speculative (no evidence that hash-based verification catches the retcon pattern — the retcon is semantic, not cryptographic).

The substantive-critic achieves the same decoupling via a cheaper mechanism. Its fresh-context read of the RDR §Problem Statement IS the independent verification; its verdict IS the evidence artifact; the cost is one LLM call per close. 2/2 empirical catches versus 0/0 for the hash-chain approach.

### Technical Environment

- **`/nx:substantive-critique`** — skill at `nx/skills/substantive-critique/SKILL.md`, agent at `nx/agents/substantive-critic.md`. Takes an RDR ID, reads the RDR + related files + recent post-mortem, produces a structured critique against the RDR's declared intent. Already works; already used by the user ad hoc.
- **`/nx:rdr-close`** — command at `nx/commands/rdr-close.md` and skill at `nx/skills/rdr-close/SKILL.md`. Contains the two-pass preamble from RDR-065. The critic dispatch would extend this preamble (or add a Step 1.6 in the skill).
- **Subagent dispatch from command preamble** — nexus supports dispatching subagents from commands via the Agent tool in the main conversation; however, subagents cannot spawn other subagents. The critic dispatch must happen in the main conversation, not inside a subagent.

## Research Findings

### Finding 1 (2026-04-11): Substantive-critic is the one proven intervention

Source: `rdr_process/nexus-audit-2026-04-11` (the nexus historical audit).

Across 4 confirmed ART incidents in the 90-day window (RDR-031, RDR-036, RDR-073, RDR-075), the substantive-critic is the only intervention with empirical catch evidence:

| Incident | Mechanism | Critic dispatched? | Outcome |
|---|---|---|---|
| RDR-073 | 312D/65D dim mismatch → agent nulled grounding via retcon | Yes, post-close | CAUGHT — user reopened same day |
| RDR-075 | InstarLearning structurally dead; resonance guard removed against spec | Yes, pre-close | CAUGHT — 3 critical issues fixed before close |
| RDR-036 | maxIterations=1, HashMap-backed with decorative ODEs | Yes (4-agent critique) post-delivery | CAUGHT — superseded by RDR-037 |
| RDR-031 | Building blocks only, Step 5 deferred | No (user caught at close time manually) | CAUGHT by user |

Three of four catches were made by the critic; one was made by the user doing manually what the critic does automatically. **None** of ART's INT-1..INT-7 interventions have been empirically tested — they're all proposals from the same agent class that failed to catch the pattern in-session.

### Finding 2 (2026-04-11): The retcon is cognitive, not just cost-function

Source: `~/.claude/projects/-Users-hal-hildebrand-git-ART/62cadb3a-b647-4378-8afb-bdd5c40ef831.jsonl` lines 526-529 (the RDR-073 live session).

ART's RC-3 (workaround bias under time pressure) frames the failure as cost-function: reopen is expensive, workaround is cheap, agent picks cheap. That's half the story. The live transcript shows the deeper mechanism: **the agent reframes the workaround as fixing the plan, not deviating from it**. Line 529: *"Passing the Binder grounding to dialog is a design mistake that was latent."* The agent did not experience itself as cutting scope — it experienced itself as correcting a plan error.

This means INT-3 (workaround gating with user approval) has a hidden assumption: that the agent recognizes the workaround AS a workaround. The retcon mechanism shows it often doesn't. The detection problem is harder than ART's writeup acknowledged.

**What catches the retcon**: an external check with fresh context. The substantive-critic has no session history, reads the RDR §Problem Statement cold, compares it to what shipped. Its context isn't biased by the rationalization the session agent just produced. This is the structural reason the critic works and in-session checks don't.

### Critical Assumptions

- [ ] **CA-1**: The substantive-critic produces consistent verdicts on repeated dispatch against the same RDR artifacts — i.e., running the critic twice in a row on the same inputs yields functionally equivalent verdicts. If the critic is non-deterministic in ways that matter, automatic dispatch could produce flap (close passes critic run 1, fails critic run 2).
  — **Status**: Unverified — **Method**: Spike (run critic twice on RDR-073 and RDR-075, compare verdicts for semantic equivalence)

- [ ] **CA-2**: The critic can parse its own verdict into a machine-readable form that the close flow can consume (e.g., an outcome field: `justified | partial | not-justified`). If every verdict has to be read by a human to extract the outcome, the "automatic" part of "automatic dispatch" is a lie.
  — **Status**: Unverified — **Method**: Source Search (inspect current critic output structure) + template adjustment if needed

- [ ] **CA-3**: The critic dispatch time (one LLM call, ~20-60 seconds for the critic's Read + analysis + write) is acceptable in the close flow. If it adds minutes, users will learn to avoid closes and the gate becomes friction.
  — **Status**: Unverified — **Method**: Measure on the next real close after shipping

- [ ] **CA-4**: The `--force-implemented` override is genuinely rare in practice — used when the critic produces a false positive, not used as a routine bypass. If users use it on every close, the gate is theater.
  — **Status**: Unverified — **Method**: Post-ship telemetry via T2 audit entry per override invocation

## Proposed Solution

### Approach

Extend `nx/commands/rdr-close.md`'s Python preamble to automatically dispatch `/nx:substantive-critique` **after** the two-pass Problem Statement Replay succeeds, **before** the close flow proceeds. The critic's verdict is parsed into a machine-readable outcome. The close flow uses the outcome to constrain `close_reason`:

- Critic says **justified** → close proceeds normally, no constraint
- Critic says **partial** → close_reason forced to `partial`; user may override with `--force-implemented "<reason>"`
- Critic says **not-justified** → close_reason forced to `partial` or `reverted`; user may override with `--force-implemented "<reason>"` (stronger flag, logs rationale to T2)

The override is always available but always explicit. Running `--force-implemented` without a reason is rejected. Every override is logged to T2 as `nexus_rdr/<id>-close-override-<timestamp>` with the critic verdict, the user's stated reason, and the final close reason. This creates an audit trail for "why did this close as implemented when the critic said no."

### Technical Design

**Critic output shape** (CA-2 — needs verification): the critic skill must emit a parseable verdict block at the end of its output. Proposed structure (subject to refinement):

```
## Verdict: <justified | partial | not-justified>
## Confidence: <high | medium | low>
## Critical findings: <N>
## Summary: <one sentence>
```

If the current critic doesn't produce this, Phase 1 of this RDR updates the critic skill to emit it.

**Close flow integration**: the two-pass preamble's success branch (after `PROBLEM STATEMENT REPLAY: validation passed`) dispatches the critic. The dispatch uses the Agent tool in the main conversation (subagent dispatch from within a command preamble is supported — the Python preamble emits instructions for the main agent to dispatch, then continues with whatever the agent wrote to T1 scratch). Actually — the preamble is a shell-out, not an agent dispatch. The critic dispatch needs to happen in the skill body, after the preamble, not in the preamble itself.

Revised integration point: the critic dispatch lives in `nx/skills/rdr-close/SKILL.md` as a new **Step 1.75 — Automatic Critique** between Step 1.5 (Problem Statement Replay) and Step 2 (Create Post-Mortem). The skill instructs the agent to dispatch `/nx:substantive-critique <rdr-id>` and wait for the verdict. The skill then branches on the verdict and constrains the close_reason choice downstream in Step 4 (Update State).

**Override flag**: `/nx:rdr-close <id> --reason implemented --force-implemented "<reason>"`. The preamble parses `--force-implemented` the same way it parses `--pointers`. When present, the preamble skips the critic dispatch step and logs an override entry to T2. The close proceeds with `close_reason: implemented` and a post-mortem field `critic_override_reason: <text>`.

### Alternatives Considered

**Alternative 1: Ship as an optional advisory (no close-reason constraint)**

Make the critic dispatch automatic but purely advisory — its verdict is shown to the user, the user decides. This is what the current ad-hoc usage already achieves except for the "automatic" part.

**Rejection**: advisory checks get dismissed under pressure. The point of RDR-065 and this RDR is to build procedural gates, not more advice. The advisory mode is what we have today and it's demonstrably insufficient (RDR-036 and RDR-031 shipped without dispatch).

**Alternative 2: Use evidence-chain gate beads (the original scope)**

Auto-generate one bead per RDR acceptance gate; each bead closes only with a hash artifact.

**Rejection**: high effort, speculative, no empirical basis. The substantive-critic achieves the same decoupling (self-report ≠ verification) via a cheaper proven mechanism. Kept in the trade-offs section as a fallback if the critic approach fails at scale.

**Alternative 3: Apply the critic at gate time (RDR acceptance) instead of close time**

Dispatch the critic during `/nx:rdr-gate` instead of `/nx:rdr-close`.

**Rejection**: the retcon happens between accept and close, during implementation. Critiquing the design at accept time doesn't help — the design was correct at accept time; the deviation came later. Critic-at-gate is useful for a different failure (accepting a flawed design) and may be worth a separate RDR, but it doesn't address silent scope reduction.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
|---|---|---|
| Critic agent | `nx/agents/substantive-critic.md` | **Reuse** (no changes needed for the base case; possible Step 1 refinement to emit structured verdict) |
| Critic skill | `nx/skills/substantive-critique/SKILL.md` | **Reuse** |
| Close skill | `nx/skills/rdr-close/SKILL.md` | **Extend** (add Step 1.75) |
| Close preamble | `nx/commands/rdr-close.md` | **Extend** (parse `--force-implemented` flag) |
| T2 override audit | `nx memory put` via MCP tool | **Reuse** |

### Decision Rationale

The substantive-critic is the only intervention with empirical evidence of catching the retcon. Every other proposal in ART's INT list is untested. Building the one proven thing first (as a structural gate) gives us a working safety net. Preventive interventions (probe, contracts) layer on top of the net; if they fail, the net catches. If we build the preventive layers first without the net, a failure at any layer ships to production.

The cost is low: one LLM call per close (~20-60s). The benefit is high: 2/2 catches historically. The alternative (evidence-chain with hash artifacts) is 10x effort for no additional evidence of working.

## Alternatives Considered

See §Proposed Solution §Alternatives Considered above.

### Briefly Rejected

- **Run the critic at every bead close, not just RDR close**: too expensive; most bead closes don't touch the composition surface where the retcon happens. RDR-close is the high-leverage moment.
- **Train a classifier to predict "will this close need the critic"**: complexity not justified. Always running the critic is simpler and the cost is bounded.

## Trade-offs

### Consequences

- **Positive**: every RDR close gets an automatic independent check against the §Problem Statement. 2/2 historical catch rate on the retcon pattern. Closes the retcon loop without requiring agents to recognize their own rationalization.
- **Positive**: creates a structural audit trail. Every override is logged; we can measure false-positive rate over time.
- **Negative**: adds ~20-60s to every close. Closes feel heavier. Users may learn to batch or delay closes to avoid the friction.
- **Negative**: the critic will produce false positives. Some legitimate closes will get flagged. The `--force-implemented` override mitigates this but creates a temptation to default to override.
- **Negative**: if CA-1 (critic determinism) fails, closes could flap. Mitigation: run critic once per close, persist verdict, don't re-run on retry.

### Risks and Mitigations

- **Risk**: Critic false-positive rate is high enough that users default to `--force-implemented`. **Mitigation**: telemetry on override rate (CA-4); if the rate climbs, tune the critic prompt to reduce FPs.
- **Risk**: Critic is too slow. **Mitigation**: measure on real closes (CA-3); if unacceptable, consider running the critic asynchronously and having the close flow wait only if a draft `implemented` is chosen.
- **Risk**: The Step 1.75 integration point is fragile because the skill is advisory, not procedural. **Mitigation**: the `--force-implemented` override lives in the command preamble (procedural), not the skill. The skill body enforces the critic dispatch but the override is structural.

### Failure Modes

- Critic returns malformed verdict → close flow cannot parse → user gets an error message and a prompt to re-run or override.
- Critic times out → close flow treats as a soft failure; user is prompted to proceed with override or retry.
- Critic and user disagree → user uses `--force-implemented "<reason>"`; override is logged.

## Implementation Plan

### Prerequisites

- [ ] CA-1 verified: critic determinism spike run against RDR-073 and RDR-075
- [ ] CA-2 verified: critic output structure supports parseable verdict extraction (or critic skill updated)

### Minimum Viable Validation

Run the new automatic-dispatch close flow against a test RDR with a known retcon (synthetic: take a closed ART RDR, construct a nexus RDR with a similar Problem Statement and a workaround in the solution section, attempt `/nx:rdr-close --reason implemented`, verify the critic catches and blocks without `--force-implemented`).

### Phase 1: Critic output structure

- Audit the current `substantive-critic` agent output format
- If structured verdict block exists, document it
- If not, extend the agent prompt to emit the verdict block at the end of every critique
- Test the verdict block is stable across 3 repeat runs on the same RDR (CA-1 spike)

### Phase 2: Close skill Step 1.75 integration

- Add `### Step 1.75: Automatic Critique` to `nx/skills/rdr-close/SKILL.md` between current Step 1.5 and Step 2
- Skill instruction: "Dispatch `/nx:substantive-critique <rdr-id>`. Read the verdict block. If `Verdict: justified`, proceed. If `Verdict: partial` or `not-justified`, surface to user with the critic's critical findings and block `close_reason: implemented` unless `--force-implemented` was passed."
- Testing: run close flow on a test RDR, confirm the critic fires, confirm the verdict is surfaced

### Phase 3: `--force-implemented` override

- Extend `nx/commands/rdr-close.md` Python preamble to parse `--force-implemented "<reason>"` flag (reuse the `--pointers` parsing pattern)
- When `--force-implemented` present AND `--reason implemented`, skip the Step 1.75 critic dispatch, log an override audit entry to T2 as `nexus_rdr/<id>-close-override-<YYYY-MM-DD>` with the critic verdict (if dispatched prior) and the user's reason
- Require non-empty reason; reject `--force-implemented` with no reason string

### Phase 4: Plugin release

- Bump 3.8.2 → 3.8.3
- Update `nx/CHANGELOG.md` and `CHANGELOG.md`
- `scripts/reinstall-tool.sh`
- Smoke test: run close flow on a disposable test RDR, verify critic fires and verdict is surfaced

### Phase 5: Recursive self-validation (mandatory, mirrors RDR-065 pattern)

- **6a**: synthetic retcon injection into a test RDR, verify critic catches
- **6b**: independent code review of the close-flow integration
- **6c**: real self-close of this RDR (RDR-069) with the new mechanism active — the critic must pass this RDR's own close

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
|---|---|---|---|---|---|
| T2 `nexus_rdr/*-close-override-*` entries | `nx memory list` via MCP | `memory_get` | `memory_delete` | `memory_search "close-override"` | Part of nexus_rdr project backup |

## Test Plan

- **Scenario 1**: close an RDR with clean §Problem Statement closure. **Verify**: critic returns `justified`, close proceeds as `implemented` without override.
- **Scenario 2**: close an RDR with a synthetic retcon (solution section silently removes one enumerated gap). **Verify**: critic returns `partial` or `not-justified`, close blocks, user sees the critic's critical findings.
- **Scenario 3**: user uses `--force-implemented "critic false positive — the gap was addressed via a different mechanism described at line X"`. **Verify**: override is accepted, close proceeds as `implemented`, audit entry written to T2.
- **Scenario 4**: critic times out or returns malformed verdict. **Verify**: user is prompted, not silently blocked.
- **Scenario 5** (CA-1): run critic twice in a row on the same RDR. **Verify**: verdict is consistent enough to not flap.
- **Scenario 6** (recursive 6a): inject a synthetic retcon into a copy of this RDR itself. Run the new close flow on it. **Verify**: the critic catches its own retcon.

## Validation

### Testing Strategy

Unit tests are not applicable — the whole thing is an integration between the close skill, the critic skill, and T2. The recursive self-validation step (Phase 5) is the real test.

### Performance Expectations

Critic dispatch: ~20-60 seconds per close. Measured against the existing `/nx:substantive-critique` latency on RDR-065 and RDR-066 as reference points. If CA-3 fails (time is too slow in practice), Phase 2 will add async dispatch with a wait-or-override model.

## Finalization Gate

### Contradiction Check

The only tension with RDR-065 is scope overlap: RDR-065 shipped INT-4 (problem-statement replay) and the critic performs a superset of problem-statement replay. The two are complementary: the replay is a structural preamble gate (fast, deterministic, catches only the "no pointer supplied" case); the critic is a deeper semantic check (slower, probabilistic, catches retcon). Ship both; they cover different failure modes.

### Assumption Verification

CA-1 through CA-4 must be verified before the gate passes. Phase 1 spike is the venue.

### Scope Verification

Minimum Viable Validation: recursive self-close of this RDR on the new close flow. In scope, will be executed in Phase 5.

### Cross-Cutting Concerns

- **Versioning**: plugin release (3.8.3)
- **Build tool compatibility**: N/A (shell/markdown only)
- **Licensing**: AGPL-3.0, no new dependencies
- **Deployment model**: plugin reinstall via `scripts/reinstall-tool.sh`
- **IDE compatibility**: N/A
- **Incremental adoption**: the `--force-implemented` override is the incremental-adoption path — users who hate the gate can bypass it with a reason and we measure the rate
- **Secret/credential lifecycle**: N/A
- **Memory management**: T2 override entries accumulate over time; bounded by RDR close frequency; no cleanup needed

### Proportionality

The RDR is right-sized. The intervention is small (one skill step + one preamble flag) and the evidence is load-bearing (the audit).

## References

- `~/git/ART/docs/rdr/meta/RDR-PROCESS-FAILURE-MODE-silent-scope-reduction.md` — ART canonical writeup
- `rdr_process/failure-mode-silent-scope-reduction` — T2 mirror of canonical
- `rdr_process/nexus-audit-2026-04-11` — nexus historical audit (load-bearing evidence)
- `~/.claude/projects/-Users-hal-hildebrand-git-ART/62cadb3a-b647-4378-8afb-bdd5c40ef831.jsonl` lines 526-529, 846 — RDR-073 live session (retcon mechanism + agent's in-situ structural diagnosis)
- `~/git/ART/docs/rdr/post-mortem/073-cogem-training-deployment-and-dialog-runtime-integration.md` — RDR-073 post-mortem
- `~/git/ART/docs/rdr/post-mortem/075-semantic-learning-and-generalization.md` — RDR-075 post-mortem (critic caught pre-close)
- `nx/skills/substantive-critique/SKILL.md`, `nx/agents/substantive-critic.md` — the critic to wrap
- `nx/commands/rdr-close.md`, `nx/skills/rdr-close/SKILL.md` — the close flow to extend
- RDR-065 (shipped 2026-04-11) — INT-4, INT-5, INT-7 already in production

## Revision History

- 2026-04-10 — Stub created as "Evidence-Chain Gate Beads" (high-effort hash-chain + attestation design).
- 2026-04-11 — **Reissued with new scope** based on nexus historical audit. Original scope superseded because the substantive-critic is the only intervention with empirical evidence (2/2 catches) on the silent-scope-reduction failure mode; hash-chain gate beads were a higher-effort reinvention of a cheaper proven thing. New scope: Automatic Substantive-Critic Dispatch at Close. Priority stays P2 — this is the Phase 0 anchor of the remediation plan (the proven net; all other remediation is prevention layered on top). See `rdr_process/nexus-audit-2026-04-11` for the evidence base and bead `nexus-640` for the 4-RDR remediation cycle.
