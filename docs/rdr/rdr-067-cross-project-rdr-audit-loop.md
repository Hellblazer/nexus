---
id: RDR-067
title: "Cross-Project RDR Audit Loop"
type: process
status: draft
priority: P2
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-10
reissued: 2026-04-11
accepted_date:
related_issues: ["RDR-065", "RDR-066", "RDR-068", "RDR-069"]
supersedes_scope: "Cross-Project RDR Observability (original 2026-04-10 scope proposed 5 custom structured metrics + collection + skill)"
---

# RDR-067: Cross-Project RDR Audit Loop

> **Reissued 2026-04-11.** This RDR replaces the original 2026-04-10
> scope ("Cross-Project RDR Observability") which proposed building 5
> custom structured metrics (close-reason distribution, follow-up bead
> aging, problem-statement closure rate, composition-failure reopen rate,
> divergence-language density) plus a collection convention plus an
> `nx:rdr-audit` skill. The nexus audit (`rdr_process/nexus-audit-2026-04-11`)
> **empirically proved** that a single dispatch of a general-purpose
> deep-research-synthesizer subagent with a fixed prompt produces the
> same information as all 5 structured metrics — and catches incidents
> the metrics would not (because the subagent uses LLM classification
> with reasoning, not regex/count patterns, so it can distinguish
> composition failures from unrelated drift). The old scope was a
> 10x-effort reinvention of a cheaper proven thing. New scope: wrap
> the proven audit-subagent dispatch pattern as a reusable skill +
> define the cross-project collection convention + schedule periodic
> dispatch. The audit I just ran is the canonical first invocation
> of the tool this RDR formalizes.

## Problem Statement

The silent-scope-reduction failure mode is by definition invisible within a single closed RDR — every gate is green, the post-mortem is honest, the close ships as `implemented`. The pattern only becomes visible **across** RDRs and **across** projects. ART filed the canonical writeup after observing "at least 2-3 prior instances" in its own project memory before RDR-073. Nexus ran its first cross-project audit on 2026-04-11 and found 4 confirmed incidents in the 90-day window on ART alone — but the audit was ad hoc, dispatched once by the user because the user happened to remember to run it.

**Today's problem**: there is no reusable skill that encapsulates the audit pattern. There is no cross-project convention for how projects file evidence of this failure mode. There is no mechanism for periodic audits to catch the pattern as it accumulates across time. The audit I ran on 2026-04-11 produced high-value data (see `rdr_process/nexus-audit-2026-04-11`) and exercised the pattern once; this RDR formalizes it so the next audit isn't ad hoc.

Without a repeatable audit loop, the remediation shipped in RDRs 065, 066, 068, 069 cannot be measured. Shipping an intervention with no way to check whether it's working is exactly the failure mode the intervention was meant to prevent: declaring done without evidence.

### Enumerated gaps to close

#### Gap 1: No `nx:rdr-audit` skill exists

The audit on 2026-04-11 was executed manually: the user asked me to run an audit, I drafted a prompt, I dispatched a `nx:deep-research-synthesizer` subagent, I read the result and wrote it to T2 manually. None of that is reusable. The next time someone wants to run this audit, they have to re-derive the prompt and the persistence pattern from scratch. A skill that wraps all of this — canonical prompt, dispatch, result persistence, verdict surfacing — is the reusability step.

#### Gap 2: No `rdr_process` cross-project collection convention

ART filed one entry (`failure-mode-silent-scope-reduction`) and nexus filed one entry (`nexus-audit-2026-04-11`) under the cross-project `rdr_process` project in T2. These are the only two entries. The convention is not documented, not advertised in nexus skills, and has no template for what an `rdr_process` entry should contain. Without a convention, future sibling entries from other projects will be inconsistent and hard to aggregate into a cross-project audit.

#### Gap 3: No scheduled/periodic audit dispatch

Even if the skill exists and the convention is documented, nothing runs the audit on a schedule. The audit on 2026-04-11 fired because the user remembered to ask. An unaudited loop is indistinguishable from no loop at all — the pattern ossifies if no one looks. This gap requires either a bd-native scheduled reminder (bd defer with an explicit future date), a cron/launchd job, or an Anthropic-API-hosted scheduled agent.

## Context

### Background

This RDR is **Phase 2** of the four-RDR silent-scope-reduction remediation. Phase 0 (RDR-069, critic at close) and Phase 1 (RDR-066, composition probe) are preventive — they catch the failure before or at close time. This RDR is the **feedback loop**: after those interventions ship, run audits periodically to measure whether the failure mode is still occurring. Without the feedback loop, we ship preventive interventions and have no way to know if they work. That's exactly how we got here.

### Why this supersedes the original scope

The original RDR-067 (2026-04-10) proposed 5 custom structured metrics:
1. Close-reason distribution (ratio implemented : partial : superseded : reverted)
2. Follow-up bead aging (median age of P2+ beads created within 48h of close)
3. Problem-statement closure rate (fraction of gaps with code pointers in close artifacts)
4. Composition-failure reopen rate (beads reopened after integration test surfaced composition failure)
5. Divergence-language density (grep density of "divergence/workaround/deferred/follow-up" per post-mortem per quarter)

Plus a collection convention + `nx:rdr-audit` skill. The metrics would each require infrastructure: T2 queries, aggregations, time-windowing, per-project projection, visualization.

**The audit on 2026-04-11 proved all 5 metrics are unnecessary.** A single dispatch of a deep-research-synthesizer subagent with a fixed prompt produced:
- 4 confirmed incidents in the 90-day window (covers metrics 1, 4, 5)
- Classification by drift category (covers metric 3)
- Near-miss identification (RDR-021 research-phase catch)
- Frequency estimate (1-2/month on ART)
- Pattern classification (unwiring vs dim mismatch — richer than any of the 5 metrics would produce)
- Confidence level on the classification
- Explicit caveats on what was and wasn't sampled

And it did it **in a single call** without building any metric infrastructure. The subagent's LLM-driven classification is more flexible than the 5 structured metrics because it can distinguish composition failures from unrelated drift (a regex on "deferred" cannot).

**The right scope is to wrap the proven pattern**, not build 5 custom metrics from scratch.

### Technical Environment

- **`nx:deep-research-synthesizer`** (`nx/agents/deep-research-synthesizer.md`) — the agent that ran the 2026-04-11 audit. Takes a research question, mines sources, produces a structured synthesis.
- **T2 `rdr_process` project** — cross-project memory namespace already in use. Contains `failure-mode-silent-scope-reduction` (ART) and `nexus-audit-2026-04-11` (nexus). Accessible via `mcp__plugin_nx_nexus__memory_get` / `memory_put` / `memory_search` / `memory_list`.
- **`bd defer`** — bd 1.0.0 command for deferring an issue to a future date. Could be used to schedule an audit reminder.
- **`nx scratch` / `nx memory`** — existing persistence mechanisms for audit results.
- **`~/.claude/projects/*`** — cross-project session transcripts the audit can mine.

## Research Findings

### Finding 1 (2026-04-11): The audit pattern works on first invocation

Source: `rdr_process/nexus-audit-2026-04-11` (the audit itself is the evidence).

A single dispatch of the `deep-research-synthesizer` subagent with this prompt structure:
1. Frame the question ("does the composition-failure mode occur in real work, at rate justifying intervention")
2. Name the scope (ART post-mortems + canonical writeup + ART-related Claude session transcripts + T2 `rdr_process`)
3. List indicator patterns (reopen+because, dimension+mismatch, integration bead broken, drift classifications)
4. Specify output format (sources consulted, confirmed incidents, frequency estimate, recommendation)
5. Budget tool calls (read canonical writeup first, then post-mortems, then sample transcripts)

Produced a **HIGH confidence** audit report in a single subagent call (~16 tool uses, ~190 seconds). Output included:
- 4 confirmed composition-failure incidents with file:line citations to post-mortems
- 1 near-miss (research-phase catch)
- Classification into 2 shape categories (unwiring vs dimensional mismatch)
- Frequency estimate with honest confidence
- 4 honest caveats on what the audit did NOT cover

This is the **proof of concept** for the audit skill. The canonical prompt and the output structure should be pinned to the skill.

### Finding 2 (2026-04-11): LLM classification beats structured metrics for this task

The audit distinguished 4 composition failures from dozens of post-mortems covering every kind of drift (research corrections, parameter errors, benchmark target wrong, CI suppression, healthy scope expansion). A regex-based metric ("grep density of 'deferred'") would have drowned in false positives — "deferred" appears in every post-mortem that discusses scope trade-offs, most of which are NOT composition failures. The LLM's ability to read context and classify semantically is the feature that makes the audit high-signal.

### Critical Assumptions

- [ ] **CA-1**: The canonical audit prompt (used on 2026-04-11) generalizes across projects. The prompt was written for ART as the primary target; applying it to another project requires swapping the scope section. If the prompt is too project-specific, each new project requires custom prompting and the skill becomes a template not a tool.
  — **Status**: Unverified — **Method**: Re-run the audit against a second project (e.g., nexus itself, which has ~24 post-mortems; or a different user project) and measure if the output quality matches

- [ ] **CA-2**: The audit subagent produces consistent verdicts on repeated dispatch. If two consecutive runs on ART return radically different incident counts, the audit is a lottery not a measurement.
  — **Status**: Unverified — **Method**: Run the audit twice in sequence against ART; compare incident counts and confidence levels

- [ ] **CA-3**: `bd defer` or equivalent provides a usable scheduling mechanism for periodic audits. If no nexus/bd-native scheduling exists, we need an external cron job or scheduled Claude Code agent.
  — **Status**: Unverified — **Method**: Investigate `bd defer` semantics and bd calendar integration; fall back to scheduled-agents pattern if needed

- [ ] **CA-4**: The `rdr_process` collection template (for project-filed incidents) is rich enough to capture the pattern without being burdensome. Too heavy and projects won't file; too light and filings won't aggregate.
  — **Status**: Unverified — **Method**: Draft the template; test on a synthetic project filing; refine based on the audit subagent's ability to ingest it

## Proposed Solution

### Approach

Three components:

1. **`nx:rdr-audit` skill**: new skill at `nx/skills/rdr-audit/SKILL.md` that wraps the proven pattern. Takes a project target (defaults to ART; accepts any project name as argument). Dispatches `nx:deep-research-synthesizer` with the canonical prompt (pinned in the skill). Persists result to `rdr_process/audit-<project>-<YYYY-MM-DD>`. Surfaces summary to user.

2. **`rdr_process` collection template**: documented schema for cross-project incident filings. Template at `nx/resources/rdr_process/INCIDENT-TEMPLATE.md`. Projects that encounter the failure mode file entries under `rdr_process/<project>-incident-<slug>` with structured sections: mechanism, files involved, close artifacts, drift class, intervention that caught it (if any), lessons.

3. **Scheduling mechanism**: `bd defer` an audit reminder for every active project at a 90-day cadence. On defer-trigger, the reminder points at `/nx:rdr-audit <project>`. If `bd defer` is insufficient (too passive, too easy to snooze), fall back to a scheduled Claude Code agent via `claude cron` or similar.

### Technical Design

**Skill shape** (`nx/skills/rdr-audit/SKILL.md`):

```
---
name: rdr-audit
description: Audit a project's RDR lifecycle for silent-scope-reduction pattern
---

## When to use
- User invokes `/nx:rdr-audit <project>` (default: current project)
- Periodic audit reminder fires

## Inputs
- Project name (default: current working directory's project)
- Optional: time window (default: last 90 days)
- Optional: pinpoint incident (e.g. a specific RDR ID to audit)

## Behavior
1. Dispatch `nx:deep-research-synthesizer` with the canonical prompt
2. Wait for result
3. Parse the incident count and recommendation
4. Persist full result to `rdr_process/audit-<project>-<date>` via memory_put
5. Surface summary to user
6. If INCONCLUSIVE or if a recommendation contradicts a prior audit, flag for user review
```

**Collection template shape** (`nx/resources/rdr_process/INCIDENT-TEMPLATE.md`):

```
---
project: <project-name>
rdr: <rdr-id>
incident_date: <YYYY-MM-DD>
drift_class: <unwiring | dim-mismatch | deferred-integration | other>
caught_by: <substantive-critic | composition-probe | dim-contracts | user | post-hoc>
outcome: <reopened | partial | shipped-silently>
---

# Incident: <RDR-ID> <title>
## What was meant to be delivered
## What was actually delivered
## The gap
## Decision point (transcript citation if available)
## Mechanism
## What caught it
## Cost (beads reopened, PRs reverted, wall time lost)
## Lessons (for the process, not the project)
```

**Scheduling**: `bd defer RDR-067 --until=+90d --reason=audit-cadence`. The deferred issue surfaces on the due date; when it fires, the user or agent runs `/nx:rdr-audit` on whichever project is active. Alternative if `bd defer` isn't the right vehicle: `nx schedule` or `claude schedule` — investigated in Phase 1.

### Alternatives Considered

**Alternative 1: Build the 5 custom structured metrics (original scope)**

Build infrastructure for close-reason distribution, follow-up bead aging, etc. as queryable metrics.

**Rejection**: 10x effort. The 2026-04-11 audit proved the subagent-dispatch pattern delivers equivalent information without metric infrastructure. Saved as a fallback if CA-1 or CA-2 falsify (i.e., if the subagent pattern turns out to be too noisy).

**Alternative 2: Rely on ad-hoc audits (status quo)**

No skill, no template, no schedule. Run audits when the user remembers.

**Rejection**: this is what we have today. The 2026-04-11 audit only happened because the user explicitly asked for it during an unrelated remediation conversation. Ad hoc is indistinguishable from never.

**Alternative 3: Metrics via query against existing T2/catalog**

Instead of a subagent, write SQL/FTS5 queries against existing T2 entries (post-mortems, close metadata) to compute the 5 metrics.

**Rejection**: post-mortems are prose. The interesting information (drift classification, composition failure identification) is not in structured metadata — it's in the narrative. FTS5 queries would surface "deferred" hits but not distinguish them semantically. This is Finding 2's point.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
|---|---|---|
| Audit skill | `nx/skills/rdr-audit/SKILL.md` | **New** (create) |
| Research subagent | `nx/agents/deep-research-synthesizer.md` | **Reuse** — proven in the 2026-04-11 audit |
| T2 persistence | MCP `memory_put` / `memory_get` tools | **Reuse** |
| `rdr_process` namespace | Already in T2 (1 ART entry + 1 nexus entry as of 2026-04-11) | **Formalize with template** |
| Collection template | `nx/resources/rdr_process/INCIDENT-TEMPLATE.md` | **New** (create) |
| Scheduling | `bd defer` (bd 1.0.0) OR `nx schedule` | **Reuse** `bd defer` first; investigate alternatives in Phase 1 |

### Decision Rationale

The 2026-04-11 audit is the proof: a single subagent dispatch with a fixed prompt produces actionable evidence in ~3 minutes with LLM-quality classification. The old scope (5 structured metrics) was an educated guess about what would be useful; the new scope is a concretization of what already worked. Build the proven thing first, not the speculative thing.

Without the feedback loop, Phases 0 and 1 (RDR-069 and RDR-066) ship blind. The loop is not optional if we want to measure the preventive interventions' effectiveness.

## Alternatives Considered

See §Proposed Solution §Alternatives Considered.

### Briefly Rejected

- **GitHub Actions scheduled audit**: requires CI infrastructure for every consuming project; too heavy
- **Auditor agent that runs continuously**: overkill; 90-day cadence is sufficient for a failure that occurs 1-2 times per month

## Trade-offs

### Consequences

- **Positive**: formalizes a proven pattern as a one-command skill
- **Positive**: creates cross-project evidence accumulation via `rdr_process` convention
- **Positive**: measures whether the preventive interventions (RDR-066, 069) actually work
- **Negative**: audit subagent dispatch cost (~3 minutes + LLM tokens) per invocation; bounded by 90-day cadence per project
- **Negative**: collection template adoption depends on project discipline; projects that don't file entries won't be in the audit's source material (except via post-mortem mining)
- **Negative**: scheduling via `bd defer` requires the user to see the reminder; fully passive infrastructure would need external cron

### Risks and Mitigations

- **Risk**: Audit subagent verdicts drift over time as the model changes. **Mitigation**: pin the canonical prompt in the skill; re-calibrate CA-2 annually.
- **Risk**: `rdr_process` collection gets one entry per project and stops (CA-4 too heavy). **Mitigation**: template is aspirational; the audit subagent mines post-mortems directly as the primary source, not the template filings.
- **Risk**: 90-day cadence too slow to catch a recurring failure. **Mitigation**: make the cadence configurable; reduce to 30 days for projects actively being audited.

### Failure Modes

- Audit subagent times out → surface the failure, do not persist a partial result
- Audit produces INCONCLUSIVE verdict repeatedly → prompt user to investigate scope assumptions or narrow the question
- `bd defer` reminder is missed → silent failure; mitigated by requiring every audit result to set the next reminder before completing

## Implementation Plan

### Prerequisites

- [ ] RDR-069 (Phase 0) shipped
- [ ] RDR-066 (Phase 1) shipped OR at least designed (the audit needs SOMETHING to measure)

### Minimum Viable Validation

Run `/nx:rdr-audit ART` via the new skill and produce a result that matches (within reasonable variance) the 2026-04-11 manual audit. If the canonical prompt is correctly pinned, the result should be structurally identical: same incidents, same frequency estimate, same VERIFIED recommendation.

### Phase 1: Canonical prompt extraction + CA-1/CA-2 spike

- Extract the exact prompt used on 2026-04-11 from the audit record
- Generalize the scope section (remove ART-specific project references; parameterize)
- Re-run the audit twice (once targeted at ART, once targeted at nexus) — CA-1 (generalizes across projects) and CA-2 (consistent across repeats)
- Document verdict stability and cross-project applicability

### Phase 2: `nx:rdr-audit` skill

- Create `nx/skills/rdr-audit/SKILL.md` with the pinned canonical prompt
- Dispatch mechanism: skill body dispatches `nx:deep-research-synthesizer` and handles the result
- Persistence: write full audit to `rdr_process/audit-<project>-<date>`; summary to user
- Test: run `/nx:rdr-audit ART` and compare to the 2026-04-11 manual audit

### Phase 3: Collection template

- Create `nx/resources/rdr_process/INCIDENT-TEMPLATE.md`
- Document the template in the skill's help
- Optional: extend `/nx:rdr-close` to offer the incident template when closing with `close_reason: partial` (not required for MVV)

### Phase 4: Scheduling

- Investigate `bd defer` semantics: does it surface on the due date? Can it trigger a skill dispatch?
- If yes: `bd defer RDR-067-audit-reminder --until=+90d --skill=nx:rdr-audit`
- If no: fall back to a manual runbook step in `nx/skills/rdr-audit/SKILL.md` that reminds the user to set a calendar event

### Phase 5: Plugin release + recursive self-validation

- Version bump + reinstall
- **6a**: run the audit skill on ART; verify output matches manual baseline
- **6b**: substantive-critic on this RDR
- **6c**: real self-close of RDR-067 via new close flow

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
|---|---|---|---|---|---|
| `rdr_process/audit-*` entries | `memory_list` MCP | `memory_get` | `memory_delete` | `memory_search` | nexus_rdr backup |
| `rdr_process/*-incident-*` entries | same | same | same | same | same |
| `bd defer` reminders | `bd list --status=deferred` | `bd show` | `bd undefer` | N/A | N/A |

## Test Plan

- **Scenario 1**: `/nx:rdr-audit ART` — matches manual baseline (±1 incident) with same VERIFIED recommendation
- **Scenario 2**: `/nx:rdr-audit nexus` — CA-1 generalization test
- **Scenario 3**: `/nx:rdr-audit ART` run twice in a row — CA-2 consistency test
- **Scenario 4**: file a synthetic incident using the INCIDENT-TEMPLATE — verify it appears in the next audit
- **Scenario 5**: `bd defer` reminder fires — verify the user sees it and can invoke the skill

## Validation

### Testing Strategy

The load-bearing test is Phase 1 spike: does the audit subagent produce consistent high-signal output on repeat + cross-project? Everything else is mechanical wrapper code.

### Performance Expectations

Audit latency: ~3 minutes per project (one subagent dispatch). Measured against the 2026-04-11 baseline (190 seconds for 20 post-mortems + canonical). Hard caveat: if a project has 200 post-mortems instead of 20, latency scales. Budget cap built into the skill if needed.

## Finalization Gate

### Contradiction Check

No contradictions with RDRs 065, 066, 068, 069. The audit loop is the feedback loop; the others are interventions. They compose.

### Assumption Verification

CA-1 through CA-4 verified in Phase 1 spike and Phase 4 investigation.

### Scope Verification

MVV: `/nx:rdr-audit ART` matches the 2026-04-11 manual baseline. Concrete, measurable, executable in Phase 2.

### Cross-Cutting Concerns

- **Versioning**: plugin release
- **Build tool compatibility**: N/A (markdown only)
- **Licensing**: AGPL-3.0
- **Deployment model**: plugin reinstall
- **IDE compatibility**: N/A
- **Incremental adoption**: the skill is opt-in per project; no coercion
- **Secret/credential lifecycle**: audit subagent uses existing Anthropic API creds
- **Memory management**: T2 audit entries accumulate at ~4/year per project; bounded

### Proportionality

Right-sized. One skill, one template, one scheduling integration. No new infrastructure.

## References

- `rdr_process/failure-mode-silent-scope-reduction` — ART canonical writeup
- `rdr_process/nexus-audit-2026-04-11` — the proof-of-concept audit this RDR formalizes
- `nx/agents/deep-research-synthesizer.md` — the agent the skill wraps
- RDR-066 (Phase 1 composition probe) — measured by this audit
- RDR-068 (Phase 3 dimensional contracts) — measured by this audit
- RDR-069 (Phase 0 automatic critic) — measured by this audit
- RDR-065 (shipped) — measured by this audit

## Revision History

- 2026-04-10 — Stub created as "Cross-Project RDR Observability" with 3 gaps: 5 structured metrics + rdr_process collection + nx:rdr-audit skill.
- 2026-04-11 — **Reissued with new scope** based on the nexus historical audit. The 2026-04-11 audit proved a single subagent dispatch produces equivalent information to all 5 custom metrics with LLM-quality classification that regex-based metrics cannot achieve. The old scope proposed a 10x-effort reinvention of a cheaper proven thing. New scope: wrap the proven pattern as a skill + document the rdr_process collection + schedule periodic audits. Priority stays P2 — this is the feedback loop without which Phases 0-1 ship blind. See `rdr_process/nexus-audit-2026-04-11` for evidence and bead `nexus-640` for the 4-RDR cycle.
