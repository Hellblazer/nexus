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

Across 4 confirmed ART incidents in the 90-day window (RDR-031, RDR-036, RDR-073, RDR-075), external critique has empirical catch evidence. **Be precise about the numbers**: the `/nx:substantive-critique` skill (the one this RDR would automate) has **2 empirical catches** — RDR-073 and RDR-075, the two cases where it was actually dispatched. RDR-036 was caught by a different multi-agent pattern (a 4-agent critique dispatched in a different way). RDR-031 was caught by the user manually at close time.

| Incident | Mechanism | How caught | Catches `/nx:substantive-critique`? |
|---|---|---|---|
| RDR-073 | 312D/65D dim mismatch → agent nulled grounding via retcon | `/nx:substantive-critique` dispatched post-close | **empirical, yes** |
| RDR-075 | InstarLearning structurally dead; resonance guard removed against spec | `/nx:substantive-critique` dispatched pre-close | **empirical, yes** |
| RDR-036 | maxIterations=1, HashMap-backed with decorative ODEs | 4-agent critique (different pattern) post-delivery | retrospective — would have caught |
| RDR-031 | Building blocks only, Step 5 deferred | User caught manually at close time | retrospective — would have caught |

**The honest empirical base is 2/2** (RDR-073 and RDR-075 via the skill). RDR-036 and RDR-031 are retrospective attributions — the critic would probably have caught them based on the pattern, but we have no direct evidence because the skill was not dispatched on those closes. The 2/2 empirical rate plus 2 retrospective matches is still the strongest evidence in ART's INT-1..INT-7 list (every other intervention is 0/0 — never tested), but the numbers should not be overstated. **None** of ART's INT-1..INT-7 interventions have been empirically tested — they're all proposals from the same agent class that failed to catch the pattern in-session.

### Finding 7 (2026-04-11): CA-1 n=4 — RDR-068 target, and substantive RDR-068 fixes surfaced

**Fourth target.** Dispatched the critic twice against RDR-068. Both runs returned `not-justified` (Run 1: 1 Critical; Run 2: 2 Critical). Latency 96s and 108s. T2: `nexus_rdr/069-research-5-ca1-rdr068-n4` (id 722).

**CA-1 running total**: n=4 targets, 8 runs, 4/4 verdict-category stability. Latency median shifts to ~111s (was 122s at n=3). Still no flap observed.

**Substantive RDR-068 findings surfaced by this spike** (applied in the same commit as this finding):

- Stable Critical: CA numbering collision between RDR-068 and RDR-066 creates a false-confirmation risk. A gate check searching T2 for "CA-2 verified" could surface RDR-066's spike and mistakenly satisfy RDR-068's unrelated CA-2.
- Stable Critical: RDR-068's CA-2 (cross-bead mismatch detection) is treated as implemented in §Technical Design despite being Unverified — no design branch analog to RDR-066's "Design branches on CA-1 outcome."
- Stable Significant: Phase label `6a/b/c` not renamed to `4a/b/c` (same fix RDR-066 had; not propagated).
- Stable Significant: Phase 2 (template + skill update) can ship before CA-068-2 (mismatch detection) is verified, enabling the ceremonial failure mode the §Trade-offs section names.
- Run 1 only: enrichment latency extrapolation was unrealistic. At 50s per contract × 100-200 contracts for a 20-bead plan = 83 minutes to 14 hours. The RDR's "~5 minutes" cap was off by 17-40x.
- Run 2 only: `CONTRACT MISMATCH` was described as "blocks enrichment" in §Technical Design but "advisory warning" in §Trade-offs — internal contradiction.

**All stable + correct single-run findings applied** to RDR-068 in this commit: CA renumbered with `CA-068-*` prefix; CA-068-4 added for cross-bead provenance fields; §Technical Design given explicit design branches on CA-068-1 and CA-068-2; §Finding 1 label disambiguated; mismatch behavior softened to advisory; phase labels renamed 4a/b/c; §Prerequisites strengthened with `bd dep` enforcement; latency extrapolation made honest.

### Finding 6 (2026-04-11): CA-1 meta-test — n=3 targets, critic's recursive critique of RDR-069 itself

**Meta-test.** Dispatched the critic twice against RDR-069 itself — the RDR about automatic critic dispatch, critic'd. T2: `nexus_rdr/069-research-4-ca1-meta-test-rdr069` (id 721).

**Result**:

| Target | Run 1 | Run 2 | Verdict stable? |
|---|---|---|---|
| RDR-066 (Finding 4) | not-justified (2 Crit) | not-justified (1 Crit) | ✓ |
| RDR-067 (Finding 5) | justified (0 Crit, 5 Sig) | justified (0 Crit, 3 Sig) | ✓ |
| RDR-069 (Finding 6) | not-justified (3 Crit) | not-justified (2 Crit) | ✓ |

**n=3 targets, 6 total runs, 3/3 verdict-category stability.** No flap case observed in any target.

**Latency across 6 runs**: 99, 114, 95, 217, 154, 130 seconds. Median ~122s, range 95-217s. The ~3x spread reflects clean-vs-broken variance (the 217s outlier was Run 1 on a clean target). Median is higher than the ~107s from Finding 4 because the meta-test on RDR-069 (a more complex RDR) pulled the median up.

**Finding-level determinism confirmed NOT stable on all three targets.** Each run surfaces different specific Critical/Significant issues. On RDR-069 specifically, both runs agreed on 2 Critical issues:

1. **Verdict block discrepancy**: the canonical format in §Technical Design conflicts with the Finding 3 T2 entry format; neither format exists in the actual `substantive-critic.md` agent prompt. Fix: canonicalize one format, cross-reference it consistently, specify the fallback parse rule explicitly.
2. **`--force-implemented` phase ordering + regex collision**: the RDR describes the flag in present tense (as if implemented) but the preamble doesn't parse it; and the existing `--force` regex accidentally matches `--force-implemented` as a prefix, silently setting `force=True`. Fix: bundle the preamble extension into Phase 2 atomically with the gate; fix the `--force` regex to use a word boundary.

Plus Run 1 only Critical: remove the preamble-vs-skill dispatch contradiction in Technical Design (first paragraph proposes preamble dispatch, next paragraph retracts it).

Plus Run 2 only Significant: stale Performance Expectations (~20-60s) contradicts CA-3 measured range (95-217s); phase labels `6a/b/c` not renamed to `5a/b/c` (RDR-066 already had this fix); override logging in skip-dispatch needs `critic_verdict: skipped` clarification; dispatch isolation risk from relay framing.

Plus stable observation both runs flagged: the "2/2 catches" framing in §Finding 1 overstates the empirical base. Only RDR-073 and RDR-075 are empirical; RDR-036 and RDR-031 are retrospective attribution.

**All stable findings applied in this commit**:

- Canonical Verdict block format (5-field sub-bullet structure) in §Technical Design; fallback parse rule explicit
- Preamble-vs-skill dispatch contradiction removed from §Technical Design
- `--force-implemented` phase ordering fixed: Phase 2 now bundles the preamble extension atomically with the gate integration
- `--force` regex collision fixed: note added that Phase 2 must change `r'--force'` to `r'--force\b'`
- Phase labels `6a/b/c` → `4a/4b/4c` (renamed Phase 5 to Phase 4 since Phase 3 was merged into Phase 2)
- Performance Expectations updated with measured numbers
- Dispatch isolation risk added to §Risks and Mitigations
- Override logging clarified with `critic_verdict: skipped` vs prior-verdict record distinction
- §Finding 1 table corrected to honestly distinguish 2 empirical catches from 2 retrospective attributions

**CA-1 disposition**: **VERIFIED (n=3)**. Upgraded from n=2 in Finding 5. Remaining caveat: still not finding-deterministic, still no true flap case observed, still theoretically possible on a target with exactly 1 Critical issue at the edge of the Critical/Significant boundary. The 3 targets tested here were (a) clearly broken, (b) solidly clean, (c) clearly broken. None was borderline by the "exactly 1 marginal Critical" definition.

**CA-3 disposition**: **VERIFIED with updated range**. Median ~122s (was 104.5s), range 95-217s, n=6 runs. Clean closes may exceed 3 minutes. `--force-implemented` serves as both override AND latency-skip escape hatch.

### Finding 5 (2026-04-11): CA-1 flap test — no flap found on a second target (n=2)

**Flap-at-gate failure mode test.** Finding 4's determinism spike used n=1 (RDR-066, a target where both runs returned `not-justified`). The open question was whether a *clean* or *borderline* target could produce different verdict categories between runs (Run 1 finds 0 Critical → `justified`; Run 2 finds 1 Critical → `not-justified`). This spike dispatched the critic twice against RDR-067 (not-yet-critic'd, plausibly borderline). T2: `nexus_rdr/069-research-3-ca1-flap-test-rdr067`.

**Result**:

| Target | Run 1 | Run 2 | Verdict stable? |
|---|---|---|---|
| RDR-066 (Finding 4) | not-justified (2 Critical) | not-justified (1 Critical) | ✓ |
| RDR-067 (Finding 5) | justified (0 Critical, 5 Significant) | justified (0 Critical, 3 Significant) | ✓ |

**n=2 targets, 4 total runs, 2/2 verdict-category stability.** Both a known-broken target and a borderline target produced consistent gate-decision outcomes across repeat runs. **No flap found.**

**Latency update**: n=4 runs now. Durations: 99s, 114s, 95s, 217s. Median ~104.5s, range 95-217s. The 217s outlier was Run 1 on the clean target RDR-067 — confirming "clean" takes longer than rejecting "broken" (the critic can short-circuit on a Critical issue; confirming no Critical issues requires exhaustive searching). **Widening CA-3 tolerance**: clean closes may take 3-4 minutes, not just the ~107s median claimed in Finding 4.

**Finding-level determinism — confirmed NOT stable on both targets.** On RDR-067, runs overlapped on 2 Significant issues (CA-2 spec wrong, Gap 3 scheduling fallback absent) but each surfaced additional issues the other missed. The stable-findings overlap is roughly 50% of Significant issues on both targets.

**RDR-067 follow-ups applied as part of this arc** (findings both runs agreed on):

1. CA-2 spec rewritten to match RDR-069 finding-vs-verdict framing (define variance tolerance)
2. Gap 3 / scheduling fallback extended to reference the `schedule` skill as an alternative to `bd defer`
3. (Run 1 single-finding, applied anyway): "single dispatch" claim clarified vs. transcript mining was main-session
4. (Run 2 single-finding, applied anyway): causal vs. correlational framing made honest — the audit measures frequency, not causal effectiveness

**Updated CA-1 disposition**: **VERIFIED at the verdict-category level** (upgraded from PARTIALLY VERIFIED in Finding 4). n=2 is better than n=1 but not exhaustive; the flap case is still theoretically possible on a truly borderline target (one where exactly 1 Critical issue is on the edge of being classified). RDR-067 turned out to be solidly clean rather than edge-case.

### Finding 4 (2026-04-11): CA-1 + CA-3 — critic is outcome-deterministic at verdict level; ~107s median latency

**Determinism + latency spike.** Dispatched `nx:substantive-critic` twice sequentially against `rdr-066-composition-smoke-probe-at-coordinator-beads.md` with an identical prompt. Compared outputs. T2: `nexus_rdr/069-research-2-ca1-ca3-critic-determinism-spike`.

**Latency**: Run 1 = 114s (9 tool uses), Run 2 = 99s (11 tool uses). **Median ~107s.** Within the 60-180s prediction from Finding 3.

**Verdict-level determinism — STABLE.** Both runs classified RDR-066 as `not-justified` (Critical count > 0). The close-flow gate's decision (block `close_reason: implemented` when Critical > 0) is the same in both runs. No flap at the verdict-decision layer.

**Finding-level determinism — NOT STABLE.** Each run found specific Critical issues the other missed:

| Finding | Run 1 | Run 2 |
|---|---|---|
| Overall verdict | not-justified (2 Critical) | not-justified (1 Critical) |
| Phase prerequisite under-enforced | Significant | Significant |
| Coordinator tagging has gaps | Significant | Significant (deeper — proposed new CA-5) |
| CA numbering discrepancy | **Critical** | missed |
| Probe attribution treated as solved despite CA-2 unverified | **Critical** | missed |
| Design-before-spike ordering (§Technical Design locked before CA-1 resolved) | missed | **Critical** |
| Probe trigger is text instruction not structural hook | Significant | missed |
| Phase label inconsistency (5a/b/c vs 6a/b/c) | missed | Significant |

Both runs surfaced real issues. The union is richer than either alone.

**Implications for RDR-069 implementation**:

1. **Treat verdict-level determinism as the contract** (`Critical > 0 → not-justified`). Do not promise finding-level determinism — individual Critical issues vary between runs.
2. **Surface the critic's specific findings to the user as informational**, but base the gate decision on the verdict category, not on any specific issue.
3. **Consider a `--critique-runs=N` flag** (default 1) that runs the critic multiple times and takes the union of findings. Useful for high-stakes closes; not required for Phase 1.
4. **Caveat — n=1 target, 2 runs.** This spike did not test a clean RDR where the critic might find 0 Critical issues in one run and 1 in another (that's the flap-at-gate failure mode). A follow-up spike after shipping should run the critic 5+ times against a clean target to check for that case.

**CA-1 disposition**: **PARTIALLY VERIFIED (outcome-deterministic, not finding-deterministic).**
**CA-3 disposition**: **VERIFIED — acceptable.** ~107s median is within close-flow friction tolerance. `--force-implemented` remains useful but not load-bearing for latency avoidance.

### Finding 3 (2026-04-11): CA-2 — critic output is semi-structured, minimal extension required

**Source Search** against `nx/agents/substantive-critic.md`, `nx/skills/substantive-critique/SKILL.md`, and `nx/commands/substantive-critique.md`. Cross-referenced with the live RDR-073 critic output at session `62cadb3a-...jsonl` line 839. T2: `nexus_rdr/069-research-1-ca2-critic-output-verified`.

The critic's agent prompt (lines 200-223) defines a fixed Output Format with stable section headers: `## Critique Summary`, `## Critical Issues`, `## Significant Issues`, `## Observations`, `## Verification Performed`. Each Issue has structured fields (Location, Problem, Impact, Recommendation, Evidence). Section headers are grep-countable, which means a verdict can be derived mechanically:

- `### Issue:` count under `## Critical Issues` > 0 → **not-justified**
- Critical == 0 AND Significant > 0 → **partial**
- All clear → **justified**

Real-world compliance was observed on RDR-073 (session line 839) — the critic followed the template exactly. The template is reliable in practice but not enforced by code, so any drifted run would break a section-header parser.

**Recommended fix for RDR-069 Phase 2** (~15 lines in one agent file): add an explicit `## Verdict` block to the end of the Output Format:

```
## Verdict

- **outcome**: <justified | partial | not-justified>
- **confidence**: <high | medium | low>
- **critical_count**: N
- **significant_count**: N
- **summary**: <one sentence>
```

Close flow parses this block primarily; falls back to `### Issue:` header counting as a safety net if the Verdict block is missing or malformed.

**CA-2 disposition**: **PARTIALLY VERIFIED** — the path is clear. The machine-readable form exists today via section counting; add the explicit Verdict block for robustness.

**CA-3 implication** (latency): the critic is `model: sonnet, effort: high` and follows sequential-thinking + persistence protocols. Expect ~60-180 seconds per dispatch in typical cases, more for complex RDRs. This is at the boundary of acceptable close-flow friction — CA-3 measurement in Phase 1 will determine whether `--force-implemented` becomes load-bearing for users who can't tolerate the latency.

### Finding 2 (2026-04-11): The retcon is cognitive, not just cost-function

Source: `~/.claude/projects/-Users-hal-hildebrand-git-ART/62cadb3a-b647-4378-8afb-bdd5c40ef831.jsonl` lines 526-529 (the RDR-073 live session).

ART's RC-3 (workaround bias under time pressure) frames the failure as cost-function: reopen is expensive, workaround is cheap, agent picks cheap. That's half the story. The live transcript shows the deeper mechanism: **the agent reframes the workaround as fixing the plan, not deviating from it**. Line 529: *"Passing the Binder grounding to dialog is a design mistake that was latent."* The agent did not experience itself as cutting scope — it experienced itself as correcting a plan error.

This means INT-3 (workaround gating with user approval) has a hidden assumption: that the agent recognizes the workaround AS a workaround. The retcon mechanism shows it often doesn't. The detection problem is harder than ART's writeup acknowledged.

**What catches the retcon**: an external check with fresh context. The substantive-critic has no session history, reads the RDR §Problem Statement cold, compares it to what shipped. Its context isn't biased by the rationalization the session agent just produced. This is the structural reason the critic works and in-session checks don't.

### Critical Assumptions

- [x] **CA-1**: The substantive-critic produces consistent verdicts on repeated dispatch against the same RDR artifacts — i.e., running the critic twice in a row on the same inputs yields functionally equivalent verdicts. If the critic is non-deterministic in ways that matter, automatic dispatch could produce flap (close passes critic run 1, fails critic run 2).
  — **Status**: **VERIFIED at verdict-category level (2026-04-11, n=4)** — 4 targets, 8 total runs, 4/4 stability on gate-decision outcome. RDR-066 both runs `not-justified`; RDR-067 both runs `justified`; RDR-069 both runs `not-justified`; RDR-068 both runs `not-justified`. Flap-at-gate failure mode NOT observed. Caveat: still not finding-deterministic (specific Critical/Significant issues vary between runs). A truly borderline target (exactly 1 marginal Critical issue at the Critical/Significant boundary) could theoretically still flap — but 4/4 streak on diverse targets is strong enough to ship.
  — **Method**: Spikes — see Finding 4 (RDR-066), Finding 5 (RDR-067), Finding 6 (RDR-069 meta-test), Finding 7 (RDR-068). T2: `nexus_rdr/069-research-2-ca1-ca3-critic-determinism-spike` (id 719), `nexus_rdr/069-research-3-ca1-flap-test-rdr067` (id 720), `nexus_rdr/069-research-4-ca1-meta-test-rdr069` (id 721), `nexus_rdr/069-research-5-ca1-rdr068-n4` (id 722)

- [x] **CA-2**: The critic can parse its own verdict into a machine-readable form that the close flow can consume (e.g., an outcome field: `justified | partial | not-justified`). If every verdict has to be read by a human to extract the outcome, the "automatic" part of "automatic dispatch" is a lie.
  — **Status**: **PARTIALLY VERIFIED (2026-04-11)** — Source search confirms the critic's Output Format has stable grep-countable section headers (`## Critical Issues`, `## Significant Issues`) with structured `### Issue:` entries underneath. Verdict can be derived from section counts. Template compliance is prompt-only but was observed to hold in practice (RDR-073 session line 839). Recommended Phase 2 work: add a ~15-line explicit `## Verdict` block with outcome/confidence/counts/summary for robustness; fall back to section-header counting if the Verdict block is missing.
  — **Method**: Source Search + live transcript cross-reference — see Finding 3. T2: `nexus_rdr/069-research-1-ca2-critic-output-verified`

- [x] **CA-3**: The critic dispatch time (one LLM call, ~60-220 seconds for the critic's Read + analysis + Completion Protocol persistence) is acceptable in the close flow. If it adds more than that, users will learn to avoid closes and the gate becomes friction.
  — **Status**: **VERIFIED with wider tolerance (2026-04-11, n=8)** — median ~111s per dispatch, range **95-217s** (n=8 runs across 4 targets). Clean RDRs take longer to critic than broken ones (2-4 minutes vs ~100s); confirming "clean" requires exhaustive searching, confirming "broken" can short-circuit on the first Critical issue. Within close-flow friction tolerance. `--force-implemented` useful as both false-positive override AND as a "skip the critic I know this is clean" escape hatch for impatient users.
  — **Method**: Measured alongside CA-1 spikes — see Findings 4, 5, 6, 7. T2: `nexus_rdr/069-research-2-ca1-ca3-critic-determinism-spike` (id 719), `nexus_rdr/069-research-3-ca1-flap-test-rdr067` (id 720), `nexus_rdr/069-research-4-ca1-meta-test-rdr069` (id 721), `nexus_rdr/069-research-5-ca1-rdr068-n4` (id 722)

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

**Critic output shape — canonical Verdict block format** (Phase 1 adds this to `nx/agents/substantive-critic.md`):

```
## Verdict

- **outcome**: <justified | partial | not-justified>
- **confidence**: <high | medium | low>
- **critical_count**: <N>
- **significant_count**: <N>
- **summary**: <one sentence>
```

This single canonical format is binding across the whole design: the agent prompt extension in Phase 1, the close-flow parser in Phase 2, and the future override audit entries in Phase 3 all use this structure. The parser reads the `- **outcome**:` line to extract the verdict category. **Fallback parse rule**: if the Verdict block is missing or malformed, the close flow counts `### Issue:` headers under `## Critical Issues` and `## Significant Issues` and derives `outcome` mechanically (Critical > 0 → `not-justified`; Critical == 0 AND Significant > 0 → `partial`; all clear → `justified`). The fallback is a known-fragile safety net — it exists to prevent close-flow failure if the Verdict block is absent, not to substitute for the canonical format.

**Close flow integration** — the critic dispatch lives in `nx/skills/rdr-close/SKILL.md` as a new **Step 1.75 — Automatic Critique** between Step 1.5 (Problem Statement Replay) and Step 2 (Create Post-Mortem). The skill body invokes the Agent tool to dispatch `/nx:substantive-critique <rdr-id>` and reads the response for the Verdict block. **The dispatch cannot live in the Python preamble** at `nx/commands/rdr-close.md` because the preamble is a shell-out (`!{...}` block) that cannot invoke the Agent tool — Agent dispatch requires the conversational agent context that only the skill body has. The `knowledge-tidier` dispatch in Step 6 of the current close skill is the reference pattern.

The skill branches on the Verdict block's `outcome` field:
- `justified` → close proceeds normally, no constraint
- `partial` → close_reason forced to `partial`; user may override with `--force-implemented "<reason>"`
- `not-justified` → close_reason forced to `partial` or `reverted`; user may override with `--force-implemented "<reason>"` (stronger audit trail — logs critic verdict + user reason to T2)

**Override flag** — `/nx:rdr-close <id> --reason implemented --force-implemented "<reason>"`. **Currently not implemented** — the preamble at `nx/commands/rdr-close.md` lines 111-124 parses `--reason`, `--force`, and `--pointers` but NOT `--force-implemented`. Phase 2 (below) must ship the preamble extension atomically with the gate integration — otherwise Phase 2 ships a blocking gate with no escape hatch. Phase 2 work: (a) add `--force-implemented` parsing to the preamble (reuse the `--pointers` parsing pattern), (b) fix the existing `--force` regex at line 114 from `r'--force'` to `r'--force\b'` or equivalent boundary, to prevent `--force-implemented` from accidentally matching `--force` as a substring (which would currently set `force=True` and skip the status gate). When `--force-implemented` is present AND `--reason implemented` is set, the skill (Step 1.75) skips the critic dispatch and logs the override to T2 as `nexus_rdr/<id>-close-override-<YYYY-MM-DD>` with `critic_verdict: skipped`, the user's reason, and the final close reason. If the user invokes `--force-implemented` AFTER seeing a critic verdict (e.g., re-running the close with the override after the first run returned `not-justified`), the audit entry records the actual critic verdict from the prior run if available, otherwise `critic_verdict: skipped`.

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

The cost is one LLM call per close — median ~111s, range 95-217s measured over n=9 runs (CA-3 VERIFIED). Clean closes approach 3-4 minutes because confirming "no Critical issues" requires exhaustive searching. The benefit is high: 2/2 empirical catches historically (RDR-073, RDR-075 via `/nx:substantive-critique`), plus 2 retrospective attributions (RDR-036 via a 4-agent critique pattern; RDR-031 via user manual catch). The alternative (evidence-chain with hash artifacts) is 10x effort for no additional evidence of working.

## Alternatives Considered

See §Proposed Solution §Alternatives Considered above.

### Briefly Rejected

- **Run the critic at every bead close, not just RDR close**: too expensive; most bead closes don't touch the composition surface where the retcon happens. RDR-close is the high-leverage moment.
- **Train a classifier to predict "will this close need the critic"**: complexity not justified. Always running the critic is simpler and the cost is bounded.

## Trade-offs

### Consequences

- **Positive**: every RDR close gets an automatic independent check against the §Problem Statement. 2/2 historical catch rate on the retcon pattern. Closes the retcon loop without requiring agents to recognize their own rationalization.
- **Positive**: creates a structural audit trail. Every override is logged; we can measure false-positive rate over time.
- **Negative**: adds ~100-220s to every close (median ~111s; clean closes approach 3-4 minutes because the critic cannot short-circuit on a Critical issue it doesn't find). Closes feel heavier. Users may learn to batch or delay closes to avoid the friction; `--force-implemented` provides a latency escape hatch for high-confidence closes.
- **Negative**: the critic will produce false positives. Some legitimate closes will get flagged. The `--force-implemented` override mitigates this but creates a temptation to default to override.
- **Negative**: if CA-1 (critic determinism) fails, closes could flap. Mitigation: run critic once per close, persist verdict, don't re-run on retry.

### Risks and Mitigations

- **Risk**: Critic false-positive rate is high enough that users default to `--force-implemented`. **Mitigation**: telemetry on override rate (CA-4); if the rate climbs, tune the critic prompt to reduce FPs.
- **Risk**: Critic is too slow. **Mitigation**: measure on real closes (CA-3); if unacceptable, consider running the critic asynchronously and having the close flow wait only if a draft `implemented` is chosen.
- **Risk**: The Step 1.75 integration point is fragile because the skill is advisory, not procedural. **Mitigation**: the `--force-implemented` override lives in the command preamble (procedural), not the skill. The skill body enforces the critic dispatch but the override is structural.
- **Risk: Dispatch isolation — the critic's "fresh context" advantage depends on relay prompt isolation.** The critic catches retcons because it reads the RDR §Problem Statement without session-history bias. Automatic dispatch from the rdr-close skill runs in the main conversation's context. If the main session's relay framing inherits session rationalization (e.g., "we decided X was actually a design mistake"), the subagent can be primed by the relay's wording even though its own tool use is fresh. **Mitigation**: Phase 2's Step 1.75 relay template must be fixed-shape and minimal — pass only the RDR ID and standard input artifacts, never a session-generated summary of what was built. The relay Task field should be templated, not free-form from the main session.
- **Risk: Finding-level non-determinism confuses users.** RDR-069 CA-1 spikes showed that individual Critical/Significant issues vary between runs of the same critique. Users re-running the critic may see different issue lists, which could feel like the critic "changed its mind." **Mitigation**: surface the verdict category as the authoritative outcome; frame specific issues as "this run found these" not "the complete issue list." Consider a `--critique-runs=N` flag in a later phase that takes the union across multiple runs.
- **Risk: `--force-implemented` vs `--force` flag collision.** The existing `--force` regex in the preamble (`re.search(r'--force', args)`) would match `--force-implemented` as a prefix, silently setting `force=True` and skipping the status-gate check before the critic dispatch exists. **Mitigation**: Phase 2 must fix the `--force` regex to use a word boundary (`r'--force\b'` or `r'--force(?!-)'`) in the same commit that adds the `--force-implemented` parsing. This is bundled into Phase 2 atomically.

### Failure Modes

- Critic returns malformed verdict → close flow cannot parse → user gets an error message and a prompt to re-run or override.
- Critic times out → close flow treats as a soft failure; user is prompted to proceed with override or retry.
- Critic and user disagree → user uses `--force-implemented "<reason>"`; override is logged.

## Implementation Plan

### Prerequisites

- [x] CA-1 verified (n=3 targets, 3/3 verdict-category stability — see Finding 6)
- [x] CA-2 partially verified (grep-countable today; Phase 1 adds the canonical Verdict block to the agent prompt for robustness)
- [x] CA-3 verified with wider tolerance (median ~122s, range 95-217s — see Finding 6)

### Minimum Viable Validation

Run the new automatic-dispatch close flow against a test RDR with a known retcon (synthetic: take a closed ART RDR, construct a nexus RDR with a similar Problem Statement and a workaround in the solution section, attempt `/nx:rdr-close --reason implemented`, verify the critic catches and blocks without `--force-implemented`).

### Phase 1: Critic Verdict block extension + CA-4 threshold definition

- Extend `nx/agents/substantive-critic.md` Output Format section (lines 200-223) with the canonical Verdict block defined in §Technical Design (5 fields: outcome, confidence, critical_count, significant_count, summary)
- Verify the Verdict block is stable across 3 repeat runs on the same RDR target — the block format itself must be deterministic even if specific finding lists vary (prior CA-1 spikes show verdict-level stability)
- Define the CA-4 override threshold before Phase 2 ships: "If override rate exceeds 20% of closes in any 30-day window, Phase 2 dispatchment is treated as failing; the gate degrades to advisory mode and the critic prompt is tuned." Document the threshold in Day 2 Operations.

### Phase 2: Close skill Step 1.75 integration + `--force-implemented` preamble extension (ATOMIC)

**These two must ship atomically.** Shipping Step 1.75 without the override is a hard-block with no escape hatch.

- Add `### Step 1.75: Automatic Critique` to `nx/skills/rdr-close/SKILL.md` between current Step 1.5 and Step 2
- Skill instruction: "Dispatch `/nx:substantive-critique <rdr-id>` via the Agent tool. Parse the `## Verdict` block from the response, extracting the `- **outcome**:` field. If `justified`, proceed. If `partial` or `not-justified`, surface the critic's critical findings to the user and block `close_reason: implemented` unless `--force-implemented` was passed on the invocation."
- **In the same commit**: extend `nx/commands/rdr-close.md` Python preamble to parse `--force-implemented "<reason>"` flag (reuse the `--pointers` parsing pattern; regex: `r"--force-implemented\s+['\"]([^'\"]*)['\"]"` with fallback to single-token).
- **In the same commit**: fix the existing `--force` regex at preamble line 114 from `re.search(r'--force', args)` to `re.search(r'--force(?![-])', args)` or `re.search(r'--force\b', args)` to prevent `--force-implemented` from accidentally matching `--force` as a substring (which would currently set `force=True` and skip the status gate).
- When `--force-implemented` present AND `--reason implemented`, the skill (Step 1.75) skips the critic dispatch entirely and logs the override to T2 as `nexus_rdr/<id>-close-override-<YYYY-MM-DD>` with `critic_verdict: skipped`, the user's reason, and `close_reason: implemented`.
- If a prior critic dispatch has already produced a verdict (e.g., the close is being re-run after a first attempt returned `not-justified` and the user wants to override), the audit entry records the prior verdict rather than `skipped`.
- Require non-empty reason; reject `--force-implemented` with no reason string.
- Testing: run close flow on a test RDR with each of the three verdict paths (justified pass-through, not-justified block, not-justified + `--force-implemented` override); confirm T2 audit entries exist for all overrides.

### Phase 3: Plugin release

- Bump 3.8.2 → 3.8.3
- Update `nx/CHANGELOG.md` and `CHANGELOG.md`
- `scripts/reinstall-tool.sh`
- Smoke test: run close flow on a disposable test RDR, verify critic fires and verdict is surfaced

### Phase 4: Recursive self-validation (mandatory, mirrors RDR-065 pattern)

- **4a**: synthetic retcon injection into a test RDR, verify critic catches
- **4b**: independent code review of the close-flow integration (substantive-critic dispatched on the RDR itself — precedent: this session's meta-test produced n=3 CA-1 data)
- **4c**: real self-close of this RDR (RDR-069) with the new mechanism active — the critic must pass this RDR's own close

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

Unit tests are not applicable — the whole thing is an integration between the close skill, the critic skill, and T2. The recursive self-validation step (Phase 4) is the real test.

### Performance Expectations

Critic dispatch: **median ~122s, range 95-217s** per close (n=6 runs across 3 targets — see Finding 6). Clean RDRs (confirming zero Critical issues) take longer than broken ones because the critic cannot short-circuit; budget up to 3-4 minutes for a clean close. The Completion Protocol in `substantive-critic.md` also requires T2 persistence and bead creation BEFORE generating the final response, so the measured latency includes persistence overhead. If CA-3 is re-measured post-ship and the 95-217s range degrades, Phase 2 can add async dispatch with a wait-or-override model.

## Finalization Gate

### Contradiction Check

The only tension with RDR-065 is scope overlap: RDR-065 shipped INT-4 (problem-statement replay) and the critic performs a superset of problem-statement replay. The two are complementary: the replay is a structural preamble gate (fast, deterministic, catches only the "no pointer supplied" case); the critic is a deeper semantic check (slower, probabilistic, catches retcon). Ship both; they cover different failure modes.

### Assumption Verification

CA-1 through CA-4 must be verified before the gate passes. Phase 1 spike is the venue.

### Scope Verification

Minimum Viable Validation: recursive self-close of this RDR on the new close flow. In scope, will be executed in Phase 4.

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
- 2026-04-11 — **Reissued with new scope** based on nexus historical audit. Original scope superseded because the substantive-critic has the only empirical evidence (2/2 catches) on the silent-scope-reduction failure mode; hash-chain gate beads were a higher-effort reinvention of a cheaper proven thing. New scope: Automatic Substantive-Critic Dispatch at Close. Priority stays P2 — this is the Phase 0 anchor of the remediation plan (the proven net; all other remediation is prevention layered on top). See `rdr_process/nexus-audit-2026-04-11` for the evidence base and bead `nexus-640` for the 4-RDR remediation cycle.
- 2026-04-11 (third iteration) — **Gate Layer 3 post-fix cleanup**. Formal `/nx:rdr-gate 069` run on the committed second-iteration document returned 0 Critical + 3 Significant (all prose-level inconsistencies that didn't propagate through the second-iteration rename): (a) two "Phase 5" references in §Validation renamed to "Phase 4" (the rename from the second iteration didn't propagate to Validation section prose); (b) §Decision Rationale and §Trade-offs Consequences latency claims updated from the pre-measurement `~20-60s` to the measured `~100-220s` / median 111s to match CA-3's verified range; (c) CA-3 problem statement threshold updated from `~20-60 seconds` to `~60-220 seconds` to match its own verification status. Gate result stored at `nexus_rdr/069-gate-latest` (id 723): **PASSED** (0 Critical after these fixes land). Bead: nexus-sia.
- 2026-04-11 (second iteration) — **Critic-driven fixes** from the RDR-069 CA-1 meta-test spike (`nexus_rdr/069-research-4-ca1-meta-test-rdr069`, T2 id 721). Two runs of `nx:substantive-critic` against RDR-069 itself (meta-recursive test) found 2 stable Critical issues + multiple Significant issues + 1 stable honesty observation. Fixes applied in this iteration: (a) canonical Verdict block format set in §Technical Design (5-field sub-bullet structure) with explicit fallback parse rule; (b) preamble-vs-skill dispatch contradiction removed from §Technical Design (only Step 1.75 skill-body integration remains); (c) Implementation Plan phases reordered — Phase 2 now bundles the `--force-implemented` preamble extension atomically with the Step 1.75 gate (prevents shipping a blocking gate with no escape hatch); (d) `--force` regex collision fix explicitly required in Phase 2 (existing `r'--force'` regex would match `--force-implemented` as prefix, silently setting `force=True`); (e) Phase numbering updated — old Phase 3 merged into Phase 2, old Phase 4 plugin release becomes Phase 3, old Phase 5 recursive validation becomes Phase 4 with sub-labels renamed `6a/b/c → 4a/4b/4c`; (f) Performance Expectations updated from ~20-60s to measured median ~122s, range 95-217s, n=6 runs; (g) §Risks and Mitigations extended with dispatch isolation risk, finding-level non-determinism risk, and `--force` regex collision mitigation; (h) §Finding 1 table rewritten to honestly distinguish 2 empirical catches (RDR-073, RDR-075 via `/nx:substantive-critique` skill) from 2 retrospective attributions (RDR-036 via a different 4-agent critique pattern; RDR-031 via user manual catch); (i) CA-1 and CA-3 dispositions upgraded to VERIFIED with n=3 targets, 6 runs. Bead: nexus-sia.
