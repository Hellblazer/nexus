---
id: RDR-067
title: "Cross-Project RDR Audit Loop"
type: process
status: accepted
priority: P2
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-10
reissued: 2026-04-11
accepted_date: 2026-04-11
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

Even if the skill exists and the convention is documented, nothing runs the audit on a schedule. The audit on 2026-04-11 fired because the user remembered to ask. An unaudited loop is indistinguishable from no loop at all — the pattern ossifies if no one looks. This gap requires a scheduling mechanism that actually dispatches the audit — the Claude Code harness `schedule` skill (primary), external cron/launchd invoking `claude -p` (fallback), or an Anthropic-API-hosted scheduled agent. See CA-3 for the mechanism survey and ranking.

## Context

### Background

This RDR is **Phase 2** of the four-RDR silent-scope-reduction remediation. Phase 0 (RDR-069, critic at close) and Phase 1 (RDR-066, composition probe) are preventive — they catch the failure before or at close time. This RDR is the **feedback loop**: after those interventions ship, run audits periodically to track whether the failure mode is still occurring in the historical record.

**Honest framing** (from substantive-critic review 2026-04-11): the audit measures incident **frequency**, not causal **effectiveness** of the interventions. A 90-day audit reads historical post-mortems and classifies the incidents found there. If the rate drops after interventions ship, that is *correlational* evidence — not proof that the interventions caused the drop. Statistical significance on a 1-2/month baseline over a 90-day window is weak; longer windows or more data are needed for causal claims. The audit is valuable as a frequency monitor and a trend indicator; it should not be oversold as a causal effectiveness measurement.

### Why this supersedes the original scope

The original RDR-067 (2026-04-10) proposed 5 custom structured metrics:
1. Close-reason distribution (ratio implemented : partial : superseded : reverted)
2. Follow-up bead aging (median age of P2+ beads created within 48h of close)
3. Problem-statement closure rate (fraction of gaps with code pointers in close artifacts)
4. Composition-failure reopen rate (beads reopened after integration test surfaced composition failure)
5. Divergence-language density (grep density of "divergence/workaround/deferred/follow-up" per post-mortem per quarter)

Plus a collection convention + `nx:rdr-audit` skill. The metrics would each require infrastructure: T2 queries, aggregations, time-windowing, per-project projection, visualization.

**The audit on 2026-04-11 proved all 5 metrics are unnecessary.** A dispatch of a deep-research-synthesizer subagent with a fixed prompt produced equivalent information about incident frequency and pattern classification. Note (from substantive-critic review 2026-04-11): the 2026-04-11 audit combined a subagent dispatch for post-mortem pattern analysis with main-session work for session-transcript mining. The most analytically novel finding — the RDR-073 cognitive retcon mechanism at session lines 526-529 — came from main-session transcript mining, not from the delegated subagent. The `nx:rdr-audit` skill must specify what's delegated to the subagent vs. what's done in the main session before dispatch (transcript mining cannot currently be delegated because the subagent doesn't have efficient access to `~/.claude/projects/*`). For projects without existing post-mortems (where transcripts are the primary evidence source), the skill may need to surface raw transcript excerpts to the main session for manual inspection. This is an open design question for Phase 2.

The subagent dispatch alone produced:
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
- **`schedule` skill** (Claude Code harness built-in) — creates cron-scheduled remote agents via `CronCreate`/`CronList`/`CronDelete`/`RemoteTrigger` tools. Purpose-built for "recurring remote agents that execute on a cron schedule." Primary scheduling candidate per CA-3 Finding 1.
- **`bd defer`** — bd 1.0.0 command for deferring an issue to a future date. NOT a scheduling mechanism — surfaces an issue on its due date but does not dispatch any action. Use for issue hygiene only, not for audit triggering.
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

### Finding 3 (2026-04-11): CA-3 scheduling mechanism survey — `bd defer` is not a scheduling mechanism

Source: `nexus_rdr/067-research-1-ca3-scheduling-mechanism-survey` (id 742). Code-analytic inspection of `bd defer --help` + Claude Code harness skill list.

**`bd defer` is REJECTED as a scheduling mechanism.** `bd defer <id> --until <time>` puts a bead on ice and makes it reappear in `bd list` on the due date — but **it does not dispatch any action**. When the deferred bead's time elapses, the bead is just visible again; no skill fires, no subagent dispatches, no trigger executes. `bd defer` is bead visibility hygiene, not workflow orchestration. Original CA-3 ranked it candidate #1 on the assumption it had a trigger/execution hook. It does not.

**The Claude Code harness `schedule` skill is the PRIMARY candidate.** Available tools: `CronCreate`, `CronList`, `CronDelete`, `RemoteTrigger`. Description from the session start skill list: "Create, update, list, or run scheduled remote agents (triggers) that execute on a cron schedule." Purpose-built for recurring remote agents — direct match for periodic audit dispatch. One cron-scheduled trigger per project, firing `/nx:rdr-audit <scope>` at the chosen cadence (90 days per the RDR design), with no user intervention needed.

**External cron/launchd + `claude -p '/nx:rdr-audit <scope>'`** is the universal fallback. Higher friction (per-machine cron entries, manual output handling, no integration with Claude Code session state), but it ALWAYS works. Reserved for cases where the `schedule` skill's remote agent context lacks MCP tool access or other capability gaps surface in Phase 1b spike.

**Material impact (as of Finding 3, later superseded by Finding 4)**: Phase 4 scope collapses from "implement a scheduling mechanism" to "document the one-liner `CronCreate` invocation per project" + a thin helper subcommand `/nx:rdr-audit schedule <project>` that encapsulates the trigger creation. CA-3 disposition upgrades from "unverified" to "STRUCTURAL ranking verified, end-to-end viability PENDING Phase 1b spike."

**Superseded by Finding 4**: Further analysis of the `schedule` skill's actual constraints (no MCP connectors attached, remote agents cannot access local files including `~/git/ART/.beads/dolt/ART/`, minimum 1h interval) dropped the `schedule` skill to a secondary option. The Phase 1b spike (Finding 4) verified that external cron/launchd + local `claude -p` is the primary mechanism. Read Finding 3 as the initial ranking inversion against `bd defer`; read Finding 4 for the final option A selection.

### Finding 4 (2026-04-11): CA-3 Phase 1b spike — headless `claude -p` + plugin + subagent dispatch verified

Source: `nexus_rdr/067-research-2-ca3-phase1b-spike-result` (id 743). Live test via `claude -p --max-budget-usd 1.00 '/nx:substantive-critique 066'`.

**Headless Claude Code mode supports plugin slash command invocation with subagent dispatch end-to-end.** Test verified:

1. Fresh `claude -p` session loads nx plugin from installed location
2. `/nx:substantive-critique` slash command resolves to the substantive-critic skill
3. Skill body executes — reads the target RDR file from disk, constructs the relay
4. `Agent` tool dispatches the substantive-critic subagent from within the headless main-thread session
5. Subagent runs to completion (read + grep + MCP tool calls), returns structured output
6. Headless session prints the subagent's findings and exits cleanly

**This verifies CA-3 Option A** (external cron/launchd + `claude -p`) end-to-end: the cron wrapper invokes `claude -p '/nx:rdr-audit <project>'`, the headless session loads the plugin, the audit skill fires, the research-synthesizer subagent dispatches, the audit writes findings to T2 via MCP, and the process exits.

**Headless main-thread capability floor is sufficient for all three options; environmental constraints differentiate them, not raw capability.** The live test established the capability floor: a headless main-thread session can load a plugin, resolve a slash command, execute skill body, and dispatch a subagent via the `Agent` tool. CCR `schedule` skill (Option B) and GitHub Actions `claude-code-action@v1` (Option C) rest on the same primitives, so the capability floor extends there — but **neither environment was actually exercised by the test**. The differentiators are environmental, not capability: (a) **MCP tool availability** — local Claude Code has nx MCP registered by default; CCR needs a user-registered connector at claude.ai/settings/connectors; GH Actions needs a secret-provisioned equivalent. (b) **Local-file access** — the audit reads `~/git/ART/.beads/dolt/ART/`, reachable only from local context; neither B nor C can touch that path. These environmental differentiators — not capability gaps — are why Option A is selected. Options B and C are **structurally ruled out for the full audit scope**, not capability-verified for it. If and when a reduced-scope variant targets git-cloneable-only evidence, the capability floor applies there too, and B or C could be reopened.

**Sub-finding — substantive-critic canonical Verdict block NOT emitted in headless mode (RESOLVED in PR #149, commit c103ece)**: the subagent returned findings in Significant + Minor sections but did NOT emit the canonical bullet-dash `## Verdict` block that RDR-069 Phase 1/4a introduced. The RDR-069 Step 1.75 close-time gate's canonical-parse path (`- **outcome**:` line grep) would have failed against this output; fallback section-counting would have been invoked. **Resolution**: PR #149 strengthened the Output Format directive in `nx/agents/substantive-critic.md` to cover the entire section structure (not just the Verdict block) and explicitly named headless `claude -p`, scheduled remote CCR, and GitHub Actions as invocation contexts the directive applies to. The T2 incident (id 743) is cited in the agent file as a documented precedent to prevent recurrence.

**Bonus — critic surfaced 11 post-close drift findings against RDR-066**: the test dispatch against closed RDR-066 found 5 Significant + 6 Minor cleanup targets. Same retro-cleanup-backlog treatment as the earlier RDR-069 retro pass. Not in scope for this finding.

### Critical Assumptions

- [ ] **CA-1**: The canonical audit prompt (used on 2026-04-11) generalizes across projects. The prompt was written for ART as the primary target; applying it to another project requires swapping the scope section. If the prompt is too project-specific, each new project requires custom prompting and the skill becomes a template not a tool.
  — **Status**: Unverified — **Method**: Re-run the audit against a second project (e.g., nexus itself, which has ~24 post-mortems; or a different user project).
  — **Acceptance criteria** (structurally parallel to CA-2): output MUST contain (a) at least one confirmed incident OR an explicit INCONCLUSIVE verdict with honest sampling caveats, (b) explicit enumeration of what was and wasn't covered, (c) frequency estimate with a confidence level (HIGH / MEDIUM / LOW), (d) drift-category classification for each confirmed incident (unwiring / dim-mismatch / deferred-integration / other). Structural absence of any of (a)–(d) means the prompt is too ART-specific and needs generalization before the skill is production-ready; generalization work is CA-1 remediation, not a skill-level decision.
  — **Do NOT** require incident-count similarity across projects — projects differ in both base rate and post-mortem inventory; the criterion is structural completeness of the output, not incident-count overlap.

- [ ] **CA-2**: The audit subagent produces **verdict-category-consistent** results on repeated dispatch. Verdict category = frequency tier (0-1 / 2-3 / 4+ incidents) + recommendation (VERIFIED / PARTIALLY VERIFIED / FALSIFIED / INCONCLUSIVE). If two consecutive runs on ART land in different frequency tiers or return different recommendations, the audit is a lottery not a measurement.
  — **Status**: Unverified — **Method**: Run the audit twice in sequence against ART; compare verdict tier and recommendation. **Acceptable variance**: ±1-2 incidents in the confirmed list, same recommendation category. **Do NOT require exact incident-list match** — RDR-069's CA-1 spikes (`nexus_rdr/069-research-2-ca1-ca3-critic-determinism-spike`, `nexus_rdr/069-research-3-ca1-flap-test-rdr067`) showed finding-level determinism is NOT stable for LLM-based subagent dispatches but verdict-category determinism IS stable (n=2 targets, 4/4 runs). The analogous expectation applies here: incident counts will vary ±1-2 but the frequency tier and recommendation should be consistent.

- [x] **CA-3**: A usable scheduling mechanism exists for periodic audits. **VERIFIED 2026-04-11** via Research Findings 1 + 2 (T2 `nexus_rdr/067-research-1-ca3-scheduling-mechanism-survey` id 742, `nexus_rdr/067-research-2-ca3-phase1b-spike-result` id 743). Primary mechanism selected: **external cron/launchd + `claude -p '/nx:rdr-audit <project>'`**.
  — **Status**: VERIFIED. `claude -p` headless invocation confirmed end-to-end via live test: plugin loads, slash command resolves, skill body executes, subagent dispatch works (`Agent` tool active on headless main-thread sessions). Test cost ~$0.10-0.30, wall-clock ~2min.
  — **Why external cron is primary**: only mechanism with (a) local file access — the audit reads `~/git/ART/.beads/dolt/ART/` which is outside any git repo and unreachable from any cloud-hosted agent context, (b) full nx MCP tool access — T2 memory_put for findings without requiring external connector registration, (c) full nx plugin loading via the local Claude Code installation.
  — **Trade-off**: per-machine cron entries; users running Claude Code from multiple machines need separate cron per machine. No cross-machine sync. Output to log file, manual failure inspection.
  — **Secondary candidates (documented for future scope)**:
    - `schedule` skill (Claude Code harness CCR) — viable for reduced-scope remote audits against git-cloneable sources only. Not a drop-in for the full audit (no ART Dolt archive access, no nx MCP without connector registration). Reserved for cross-machine scheduling if local cron friction becomes painful.
    - GitHub Actions + `anthropics/claude-code-action@v1` — same capability profile as `schedule` skill (main-thread headless, git-checkout-only, no local files). Useful for CI-triggered audits or if the user prefers GitHub-hosted infrastructure over local cron. Secondary.
  — **Rejected**: `bd defer` is bead visibility hygiene only, no trigger/execution hook (Finding 1). Manual runbook does not close Gap 3 structurally.
  — **Future enhancement (out of scope for this RDR)**: wrap cron/launchd install + health-check behind an nx MCP tool — `mcp__plugin_nx_nexus__schedule_audit(project, cadence, machine)` — to centralize the per-machine setup friction across multiple machines. Captured as a possible v2 enhancement if user scale warrants it. v1 ships plain cron/launchd templates.

- [ ] **CA-4**: The `rdr_process` collection template (for project-filed incidents) is rich enough to capture the pattern without being burdensome. Too heavy and projects won't file; too light and filings won't aggregate.
  — **Status**: Unverified — **Method**: Draft the template; test on a synthetic project filing; refine based on the audit subagent's ability to ingest it

## Proposed Solution

### Approach

Three components:

1. **`nx:rdr-audit` skill**: new skill at `nx/skills/rdr-audit/SKILL.md` that wraps the proven pattern. **Core audit dispatch** takes a project target (defaults to ART; accepts any project name as argument), dispatches `nx:deep-research-synthesizer` with the canonical prompt (pinned in the skill), persists result to `rdr_process/audit-<project>-<YYYY-MM-DD>`, surfaces summary to user. The skill also exposes **bare-bones management subcommands** so users and agents can inspect and manage scheduling from inside Claude Code without shelling out to OS primitives by hand:
    - `/nx:rdr-audit list` — list all scheduled audits across projects by parsing `launchctl list | grep rdr-audit` (macOS) or `crontab -l` (Linux). **Read-only.**
    - `/nx:rdr-audit status <project>` — show next-fire timestamp (from launchd/cron inspection) and last-run outcome (from the most recent `rdr_process/audit-<project>-*` T2 entry). **Read-only.**
    - `/nx:rdr-audit history <project>` — list the last N audit findings for a project from T2 via `memory_list` + `memory_get`. **Read-only.**
    - `/nx:rdr-audit schedule <project>` — **print** the platform-specific plist/crontab install commands for the user to review and run manually. Does NOT auto-execute the install (system-level installs are privileged).
    - `/nx:rdr-audit unschedule <project>` — **print** the uninstall commands for the user to review and run manually. Does NOT auto-execute.
   The split between **read-only** (list/status/history) and **print-only** (schedule/unschedule) keeps the skill safe to invoke from any session without risk of unauthorized privileged OS changes. The management surface makes the feedback loop inspectable from inside Claude Code — any session (interactive or `claude -p`) can answer "is anything scheduled, and did it fire?" without leaving the agent context.

2. **`rdr_process` collection template**: documented schema for cross-project incident filings. Template at `nx/resources/rdr_process/INCIDENT-TEMPLATE.md`. Projects that encounter the failure mode file entries under `rdr_process/<project>-incident-<slug>` with structured sections: mechanism, files involved, close artifacts, drift class, intervention that caught it (if any), lessons.

3. **Scheduling mechanism**: **primary** is external cron/launchd + a shell wrapper that invokes `claude -p '/nx:rdr-audit <project>'` from the user's local machine. Local context gives the audit full nx MCP tool access (T2 memory_put for findings), full local file access (critical — reads `~/git/ART/.beads/dolt/ART/` which is outside any git repo), and local session state. Trade-off: per-machine cron entries; users running Claude Code from multiple machines need separate cron per machine. **Secondary (constrained variant)**: the Claude Code harness `schedule` skill is viable for reduced-scope audits against git-cloneable sources only, writing findings as committed markdown PRs instead of T2 — see Finding 3 for the constraint analysis. The secondary variant is reserved for future scope expansion; it is not a drop-in replacement for the primary design. **Rejected as not-a-mechanism**: `bd defer` is bead visibility hygiene only (see Finding 1). **Last resort** (primary fails): Gap 3 is explicitly deferred to a follow-on RDR; the skill ships with manual-invocation only and the gap stays open as a tracked known limitation, not a papered-over closure.

### Technical Design

**Skill shape** (`nx/skills/rdr-audit/SKILL.md`):

```
---
name: rdr-audit
description: Use when auditing a project's RDR lifecycle for silent-scope-reduction frequency, or when inspecting/managing scheduled periodic audits
---

## When to use
- User invokes `/nx:rdr-audit <project>` (default: current project) — runs the audit
- Periodic audit reminder fires (launchd/cron invokes `claude -p '/nx:rdr-audit <project>'`)
- User invokes a management subcommand: `list`, `status`, `history`, `schedule`, `unschedule`

## Inputs
- First positional argument is either the subcommand OR a project name:
  - No argument → run audit on current project
  - `<project>` → run audit on named project
  - `list` → list scheduled audits (read-only)
  - `status <project>` → show next-fire + last-run (read-only)
  - `history <project>` → last N audit findings (read-only)
  - `schedule <project>` → print install commands (does not execute)
  - `unschedule <project>` → print uninstall commands (does not execute)
- Optional (audit mode): time window (default: last 90 days)
- Optional (audit mode): pinpoint incident (e.g. a specific RDR ID to audit)

## Behavior — audit dispatch (default mode)
1. Main-session pre-step: read `~/.claude/projects/*` transcripts relevant to the target project. Transcript mining is not delegatable — subagents cannot access that path efficiently — so the main session gathers excerpts before dispatch.
2. Dispatch `nx:deep-research-synthesizer` with the canonical prompt + the pre-gathered transcript excerpts
3. Wait for result
4. Parse the incident count and recommendation
5. Persist full result to `rdr_process/audit-<project>-<date>` via memory_put, including a `next_expected_fire` timestamp for health-check use
6. Surface summary to user
7. If INCONCLUSIVE or if a recommendation contradicts a prior audit, flag for user review

## Behavior — management subcommands (bare bones)
- `list`: shell out to `launchctl list | grep rdr-audit` (macOS) and `crontab -l 2>/dev/null | grep rdr-audit` (Linux); format the combined output as a table (`project | platform | schedule | next-fire`). Read-only.
- `status <project>`: parse `launchctl print` (macOS) or the matching crontab line (Linux) to extract next-fire; read the most recent `rdr_process/audit-<project>-*` entry via memory_get to extract last-run outcome; display both side-by-side. Read-only.
- `history <project>`: enumerate `rdr_process/audit-<project>-*` via `memory_list`; fetch the most recent N (default 5) via `memory_get`; display title, date, and outcome summary for each. Read-only.
- `schedule <project>`: render the platform-specific plist (macOS) or crontab line (Linux) with `<project>` substituted, and print to the user together with the `launchctl load` / `crontab -e` install instructions. Does NOT execute the install. The user reviews and runs the commands manually.
- `unschedule <project>`: print the uninstall commands (`launchctl unload ~/Library/LaunchAgents/com.nexus.rdr-audit.<project>.plist` + `rm ...`, or Linux `crontab -e` instructions). Does NOT execute.

The read-only subcommands are safe to invoke from any session (interactive, `claude -p`, CCR). The print-only subcommands never touch privileged system state directly — system-level installs are privileged actions the user must authorize.
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

**Scheduling**: primary mechanism is external cron/launchd + a shell wrapper that invokes `claude -p '/nx:rdr-audit <project>'` from the user's local machine. Shape (macOS launchd example — ship a plist in `scripts/launchd/` or the equivalent for Linux cron):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTD/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.nexus.rdr-audit.ART.90d</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/bin/claude</string>
    <string>-p</string>
    <string>/nx:rdr-audit ART</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Day</key><integer>1</integer>
    <key>Hour</key><integer>3</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/tmp/rdr-audit-ART.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/rdr-audit-ART.err</string>
</dict>
</plist>
```

Linux cron equivalent:

```cron
0 3 1 */3 * /usr/local/bin/claude -p '/nx:rdr-audit ART' >> ~/.local/state/rdr-audit-ART.log 2>&1
```

The local Claude Code context fires the audit skill with full nx MCP tool access (T2 memory_put for the audit finding record) and full local file access (including `~/git/ART/.beads/dolt/ART/` where the ART bead Dolt archive lives). Audit output lands in T2 as `rdr_process/audit-<project>-<date>` and console output goes to the log file. **Secondary (constrained variant)** if per-machine cron friction is unacceptable: the Claude Code harness `schedule` skill runs a reduced-scope audit against git-cloneable sources only (no local Dolt archive), writing findings as committed markdown PRs on the nexus repo instead of T2. The secondary is not a drop-in replacement — see Findings 3 + 4 for the constraint analysis. **Rejected**: `bd defer` is not a scheduling mechanism. **Phase 1b end-to-end spike DONE 2026-04-11** — Finding 4 executed the spike live; CA-3 VERIFIED (T2 id 743).

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
| Scheduling | External cron/launchd + `claude -p '/nx:rdr-audit <scope>'` shell wrapper | **Reuse** host cron/launchd — primary mechanism per CA-3 Findings 1 + 3. The Claude Code harness `schedule` skill is a constrained-scope secondary (remote-only, no local files, no nx MCP without a registered connector). `bd defer` is NOT a scheduling mechanism. |

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
- **Negative**: scheduling via external cron/launchd requires per-machine setup; users running Claude Code from multiple machines need separate cron entries per machine. No cross-machine sync. The `schedule` skill could provide cross-machine scheduling but runs in a constrained remote-only context (no local files, no nx MCP without a registered connector) — unsuitable for RDR-067's full audit scope

### Risks and Mitigations

- **Risk**: Audit subagent verdicts drift over time as the model changes. **Mitigation**: pin the canonical prompt in the skill; re-calibrate CA-2 annually.
- **Risk**: `rdr_process` collection gets one entry per project and stops (CA-4 too heavy). **Mitigation**: template is aspirational; the audit subagent mines post-mortems directly as the primary source, not the template filings.
- **Risk**: 90-day cadence too slow to catch a recurring failure. **Mitigation**: make the cadence configurable; reduce to 30 days for projects actively being audited.

### Failure Modes

- Audit subagent times out → surface the failure, do not persist a partial result
- Audit produces INCONCLUSIVE verdict repeatedly → prompt user to investigate scope assumptions or narrow the question
- External cron/launchd trigger fails to fire (cron expression invalid, system suspended/shut down, `claude` binary path changed, etc.) → mitigation requires trigger-health check. Every successful audit run writes a T2 record with the next-expected-fire timestamp; a drive-by `bd preflight`-style check surfaces T2 records whose next-expected-fire is overdue. Launchd on macOS has `StartCalendarInterval` semantics that fire on next-boot if the machine was asleep at the scheduled time (catch-up behavior), so machine-suspended windows are not silently lost. Linux cron with `anacron` has analogous catch-up. A deeper failure mode — `claude` binary missing or `nx:rdr-audit` skill absent — surfaces as stderr in the log file; manual inspection during each audit-cadence window is advised.

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

- Create `nx/skills/rdr-audit/SKILL.md` with the pinned canonical prompt and subcommand dispatch
- **Core audit dispatch**: main-session pre-step reads `~/.claude/projects/*` transcripts for the target project (transcript mining is not delegatable — subagents cannot access that path efficiently); then skill body dispatches `nx:deep-research-synthesizer` with the canonical prompt + gathered excerpts and handles the result
- **Persistence**: write full audit to `rdr_process/audit-<project>-<date>` with `next_expected_fire` timestamp; summary to user
- **Management subcommands (bare bones)**: implement subcommand dispatch for `list` / `status` / `history` / `schedule` / `unschedule`. Read-only subcommands shell out to `launchctl list` / `crontab -l` + T2 `memory_list` / `memory_get`. Print-only subcommands (`schedule` / `unschedule`) render platform-specific install/uninstall commands for the user to review and run. **No auto-execution of privileged OS changes** — system-level installs are always the user's explicit step.
- **Tests**:
  - Run `/nx:rdr-audit ART` and compare to the 2026-04-11 manual audit (MVV)
  - Run `/nx:rdr-audit list` with at least one audit scheduled → output table includes that entry
  - Run `/nx:rdr-audit status ART` → shows next-fire + last-run
  - Run `/nx:rdr-audit history ART` → shows the 2026-04-11 audit
  - Run `/nx:rdr-audit schedule ART` → prints plist/crontab with correct project substitution
  - Run `/nx:rdr-audit unschedule ART` → prints uninstall commands (verify nothing is actually unloaded)

### Phase 3: Collection template

- Create `nx/resources/rdr_process/INCIDENT-TEMPLATE.md`
- Document the template in the skill's help
- Optional: extend `/nx:rdr-close` to offer the incident template when closing with `close_reason: partial` (not required for MVV)

### Phase 4: Scheduling

- **CA-3 VERIFIED** (Research Findings 3 + 4, T2 ids 742 + 743) — Finding 4 executed the Phase 1b end-to-end spike live. External cron/launchd is the primary mechanism; the `schedule` skill is a constrained-scope secondary; `bd defer` is rejected as not-a-mechanism.
- **Phase 4 scope (minimal)**: ship a shell wrapper `scripts/cron-rdr-audit.sh` that invokes `claude -p '/nx:rdr-audit <project>'` with a `PROJECT` environment variable, and document the per-platform setup:
  - **macOS**: ship a launchd plist template at `scripts/launchd/com.nexus.rdr-audit.PROJECT.plist` with 90-day `StartCalendarInterval` and log-file output paths. User customizes `PROJECT` and runs `launchctl load ~/Library/LaunchAgents/com.nexus.rdr-audit.ART.plist` once.
  - **Linux**: ship a crontab line template in `scripts/cron/rdr-audit.crontab` commented with install instructions.
  - **Windows**: optional — Task Scheduler equivalent. Document only if a user asks.
- The `/nx:rdr-audit schedule <project>` **management subcommand** (defined in Phase 2's skill scope) handles the print-the-install-commands step — users run the commands manually. System-level installs are never auto-executed.
- **Phase 1b end-to-end spike DONE 2026-04-11** (Finding 4, T2 id 743): headless `claude -p` invocation verified plugin loading, slash command resolution, skill body execution, and `Agent`-tool subagent dispatch end-to-end. CA-3 VERIFIED. No further spike work needed before Phase 2 starts.
- **Secondary (deferred to follow-on RDR)**: investigate whether the `schedule` skill's constrained-scope variant can be made useful for a reduced audit pattern that uses only git-cloneable sources. Pre-requisite: a user-registered nx MCP connector at claude.ai/settings/connectors. Not part of RDR-067's primary scope.

### Phase 5: Plugin release + recursive self-validation

- Version bump + reinstall
- **5a**: run the audit skill on ART; verify output matches manual baseline (MVV)
- **5b**: substantive-critic on this RDR (via `/nx:rdr-close` Step 1.75 close-time gate)
- **5c**: real self-close of RDR-067 via the close flow

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
|---|---|---|---|---|---|
| `rdr_process/audit-*` entries | `memory_list` MCP | `memory_get` | `memory_delete` | `memory_search` | nexus_rdr backup |
| `rdr_process/*-incident-*` entries | same | same | same | same | same |
| launchd/cron audit entries | `launchctl list \| grep rdr-audit` (macOS) / `crontab -l` (Linux) | `launchctl print` (macOS) / inspect crontab line (Linux) | `launchctl unload` + `rm ~/Library/LaunchAgents/*.plist` (macOS) / `crontab -e` (Linux) | Check `/tmp/rdr-audit-*.log` for last run timestamp | `~/Library/LaunchAgents/` or host crontab in user dotfiles |

## Test Plan

- **Scenario 1**: `/nx:rdr-audit ART` — matches manual baseline (±1 incident) with same VERIFIED recommendation (CA-1 seeding run)
- **Scenario 2**: `/nx:rdr-audit nexus` — CA-1 generalization test; verdict must satisfy acceptance criteria (a)–(d) from CA-1
- **Scenario 3**: `/nx:rdr-audit ART` run twice in a row — CA-2 consistency test (±1-2 incident variance, same recommendation tier)
- **Scenario 4**: file a synthetic incident using the INCIDENT-TEMPLATE — verify it appears in the next audit (CA-4)
- **Scenario 5**: launchd/cron test entry fires — install a near-term launchd plist (or Linux cron line) that invokes `claude -p '/nx:rdr-audit ART'`, wait for it to fire, verify the audit runs to completion, verify the finding lands in T2 as `rdr_process/audit-ART-<date>`, inspect the log file for any errors. Unload the test entry after verification. Expected latency window: ~3-5 minutes end-to-end including audit subagent dispatch.
- **Scenario 6** (management subcommands): with at least one schedule active, run in sequence:
  - `/nx:rdr-audit list` → output table includes the active entry
  - `/nx:rdr-audit status ART` → shows next-fire timestamp + last-run outcome
  - `/nx:rdr-audit history ART` → shows recent audit findings including the 2026-04-11 baseline
  - `/nx:rdr-audit schedule ART` → prints plist/crontab with correct project substitution (verify no OS-level install side-effect)
  - `/nx:rdr-audit unschedule ART` → prints uninstall commands (verify nothing is actually unloaded)
  - Read-only subcommands (`list`/`status`/`history`) must not alter OS or T2 state; print-only subcommands (`schedule`/`unschedule`) must not execute privileged installs.

## Validation

### Testing Strategy

The load-bearing test is Phase 1 spike: does the audit subagent produce consistent high-signal output on repeat + cross-project? Everything else is mechanical wrapper code.

### Performance Expectations

Audit latency: ~3 minutes per project (one subagent dispatch). Measured against the 2026-04-11 baseline (190 seconds for 20 post-mortems + canonical). Hard caveat: if a project has 200 post-mortems instead of 20, latency scales. Budget cap built into the skill if needed.

## Finalization Gate

### Contradiction Check

No contradictions with RDRs 065, 066, 068, 069. The audit loop is the feedback loop; the others are interventions. They compose.

### Assumption Verification

CA-3 verified **pre-gate** via Finding 4 (T2 id 743) — the Phase 1b end-to-end spike was executed 2026-04-11. CA-1 and CA-2 are to be verified in **Phase 1 spike** (canonical prompt extraction + ART/nexus cross-project runs, with CA-1's structural acceptance criteria (a)–(d) and CA-2's ±1-2 incident variance rule). CA-4 is to be verified in **Phase 3** (draft template + test on synthetic filing).

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
- 2026-04-11 — **Critic-driven fixes** from the RDR-069 CA-1 flap-test spike (`nexus_rdr/069-research-3-ca1-flap-test-rdr067`). Two runs of `nx:substantive-critic` against this RDR surfaced real issues. Stable findings both runs agreed on were addressed here: (a) CA-2 spec rewritten to measure verdict-category consistency (±1-2 incident variance) instead of exact incident-list match, cross-referencing RDR-069's finding-vs-verdict distinction; (b) CA-3 scheduling candidates extended to include the nexus `schedule` skill as a second candidate before the manual-runbook last resort, with explicit branching to a follow-on RDR if all candidates fail. Single-run findings also applied: (c) the "single dispatch" claim clarified — the 2026-04-11 proof-of-concept combined subagent dispatch for post-mortem analysis with main-session transcript mining; the skill must specify what's delegated vs. main-session; (d) causal vs. correlational framing made honest — the audit measures frequency, not causal effectiveness. Bead: nexus-sia.
- 2026-04-11 — **Research Finding 1 / CA-3 scheduling mechanism survey**. Code-analytic inspection of `bd defer --help` confirmed it is NOT a scheduling mechanism — bead visibility hygiene only, no trigger/execution hook. Intermediate conclusion (later refined): the `schedule` skill was initially ranked primary. T2: `nexus_rdr/067-research-1-ca3-scheduling-mechanism-survey` (id 742).
- 2026-04-11 — **Research Finding 4 / CA-3 Phase 1b end-to-end spike — VERIFIED**. Live `claude -p --max-budget-usd 1.00 '/nx:substantive-critique 066'` test in a fresh headless main-thread session confirmed: plugin loads, slash command resolves, skill body executes, `Agent`-tool subagent dispatch works end-to-end. External cron/launchd + local `claude -p` selected as primary mechanism (Option A); `schedule` skill and GitHub Actions reclassified as structurally ruled out for the full audit scope (no local-file access to ART Dolt archive; no nx MCP without connector registration). CA-3 marked VERIFIED. Sub-finding: substantive-critic did NOT emit the canonical Verdict block in headless mode — retro-cleanup item filed. T2: `nexus_rdr/067-research-2-ca3-phase1b-spike-result` (id 743). See `research/rdr-067-ca3-spike` branch / PR #150.
- 2026-04-11 — **Gate PASSED + drift cleanup + management surface added**. `/nx:rdr-gate 067` result: Layer 1 PASS, Layer 2 PASS, Layer 3 partial (0 Critical, 2 Significant, 5 Observations) — T2 `nexus_rdr/067-gate-latest` (id 744). This pass addresses:
  - **SIG-1**: Finding 4 "by extension verifies Options B and C" reframed as "headless main-thread capability floor sufficient for all three options; environmental constraints (MCP access, local-file access) differentiate them, not raw capability." Options B and C clearly labeled as structurally ruled out for the full audit, not capability-verified for it.
  - **SIG-2**: CA-1 sharpened with structural acceptance criteria (a)–(d) parallel to CA-2 (at-least-one-confirmed-incident-or-INCONCLUSIVE, sampling caveats enumeration, frequency-estimate-with-confidence, drift-category per incident). Explicit note that incident-count similarity across projects is NOT required.
  - **OBS-1**: Phase 4 citation "T2 ids 742 and TBD / Research Findings 1 + 3" corrected to "T2 ids 742 + 743 / Research Findings 3 + 4."
  - **OBS-2**: Phase 5 subsection labels 6a / 6b / 6c corrected to 5a / 5b / 5c.
  - **OBS-3**: "Phase 1b end-to-end spike is optional" language in §Technical Design + §Implementation Plan Phase 4 converted to past-tense "DONE 2026-04-11 (Finding 4, T2 id 743)."
  - **OBS-4**: §Finalization Gate §Assumption Verification reframed to distinguish CA-3 pre-gate verified from CA-1 / CA-2 (Phase 1 spike) and CA-4 (Phase 3).
  - **OBS-5**: Finding 4 sub-finding "retro cleanup backlog item" updated to "RESOLVED in PR #149 commit c103ece" — the Output Format directive was strengthened and explicitly names headless / CCR / GH Actions contexts.
  - **NEW scope (folded into Phase 2)**: bare-bones management surface for the audit skill — `list` / `status` / `history` (read-only) + `schedule` / `unschedule` (print-only). Wraps host OS primitives (`launchctl list`, `crontab -l`) and T2 `memory_list` / `memory_get` so users and agents can inspect scheduling state from inside Claude Code without shelling out by hand. Closes the "is anything scheduled, and did it fire?" observability gap that Phase 4's OS-level install templates otherwise left open. Bead: `nexus-gate-067-cleanup`.
- 2026-04-11 — **Research Finding 4 / CA-3 Phase 1b spike + option A selection**. Live `claude -p` test verified headless mode supports plugin slash-command invocation + subagent dispatch end-to-end. Further analysis of the `schedule` skill revealed structural constraints (no MCP connectors attached, remote agents cannot access local files like `~/git/ART/.beads/dolt/ART/`, minimum 1h interval, triggers cannot be deleted programmatically) that make it unsuitable as primary for RDR-067's full audit scope. **Option A selected as primary**: external cron/launchd + `claude -p '/nx:rdr-audit <project>'` shell wrapper running in the user's local context with full MCP + local file access. Secondary candidates retained for future scope expansion (CCR `schedule` skill for constrained remote-only audits; GitHub Actions `anthropics/claude-code-action@v1` for CI-triggered audits). Future enhancement noted: wrap cron/launchd install + health-check behind an nx MCP tool (`mcp__plugin_nx_nexus__schedule_audit`) if per-machine friction becomes painful across a multi-machine user base — not in scope for v1. RDR sections updated to reflect option A as primary: CA-3, §Problem Statement Gap 3, §Context, §Proposed Solution, §Technical Design Scheduling (with launchd plist + crontab examples), §Existing Infrastructure Audit, §Trade-offs §Consequences, §Failure Modes (launchd/cron failure modes instead of schedule-skill modes), §Implementation Plan Phase 4 (shell wrapper + per-platform templates instead of CronCreate invocation), §Day 2 Operations, §Test Plan Scenario 5. CA-3 disposition upgraded from unverified to **VERIFIED**. Sub-findings from the spike: substantive-critic canonical Verdict block is NOT emitted in headless `claude -p` mode (retro backlog for RDR-069); the spike's test dispatch against closed RDR-066 surfaced 11 post-close drift findings (retro backlog for RDR-066). Both sub-findings are logged as retro cleanup targets, not blocking for RDR-067 CA-3 verification. T2: `nexus_rdr/067-research-2-ca3-phase1b-spike-result` (id 743).
