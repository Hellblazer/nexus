---
title: "Prose Style Enforcement Policy"
id: RDR-197
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: # reviewer name(s)
created: 2026-08-21
accepted_date: # YYYY-MM-DD, set by /rdr-accept
related_issues: ["nexus-ptwm2"]
---

# RDR-197: Prose Style Enforcement Policy

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> In repos that ship docs/writing-style.md, prose follows it; the gate runs nx prose lint on this file and blocks on findings (--skip-prose is the audited override).

## Problem Statement

The prose register work (bead nexus-ptwm2) shipped a spec, a lint, a gate, and a ratchet in one pass, with no design review. A fable-level review round (T2 `ptwm2-design-critique-fable-2026-08-21`, `ptwm2-code-review-fable-2026-08-21`, `ptwm2-rdr-sweep-editorial-fable-2026-08-21`) found the mechanism sound but several policy decisions made implicitly that should have been made deliberately. The immediate defects were remediated the same day (sweep reverted, gate scoped and given an override, lint corrected, baseline regenerated). What remains is the policy layer this RDR decides.

### Enumerated gaps to close

#### Gap 1: No admission rule for lint rules

The lint's rule set was chosen by one session from a graded research document. There is no stated bar for what evidence admits a rule, so future rules can accrete by taste. The em-dash rule is a maintainer preference; the marker lexicon has corpus-study backing; the contrast frame had zero hits in this repo's corpus. Each is enforceable today with the same force.

#### Gap 2: Enforcement strength is not mapped to surface or status

Current state after remediation: zero tolerance for templates, the spec, and CHANGELOG `[Unreleased]`; ratchet for docs, blog, README, and active RDRs; gate-time block (with `--skip-prose`) for gated RDRs; nothing for commit messages, bead text, T2 write-backs, or PR bodies, though the spec claims to cover them. The mapping was chosen under remediation pressure. It needs a decision of record, including whether the unlinted surfaces stay aspirational or get a check, and where the spec itself lives: today it is repo-local and the plugin's gate is conditional on its presence, so other conexus repos get no register unless they copy it. Decide repo-local versus plugin-shipped.

#### Gap 3: No policy for historical prose

The reverted sweep proved mechanical rewriting of the record is the wrong tool: 19 percent of 721 edits read worse or drifted in meaning, and three accepted RDRs were altered without revision notes. The standing exemptions (closed RDRs, shipped CHANGELOG sections) are precedent, not policy. Decide when, if ever, existing prose is rewritten, by whom, and with what review.

#### Gap 4: The measurement the research called for was never run

The research record's own recommendation was to test exemplar-based steering against rule lists on real subagent output before assuming either wins. The spec asserts the positive-instruction position on B/C-grade evidence. Decide the experiment's design, cost ceiling, and what changes if the result contradicts the spec's framing.

## Context

### Background

Sam directed the register change on 2026-08-21 ("it kind of sucks... I'd really like it to not suck"), then asked for RDRs to be included, then ordered a full critique after the first implementation merged without one. The remediation commit and this draft are the response. Authoritative artifacts: T3 `research-ai-slop-prose-removal-2026-08-21` (graded source survey), the three fable review verdicts in T2, and `docs/writing-style.md` as remediated.

### Technical Environment

`src/nexus/prose_lint.py` (regex engine, masking, ratchet helpers), `nx prose lint`, `tests/test_prose_style_lint.py` (`-m lint`), `docs/.prose-baseline.json`, the prose sub-check in `nx rdr preamble rdr-gate`, and the plugin surfaces (subagent preflight row, substantive-critic prose section, RDR templates) which ship to every conexus install.

## Research Findings

### Investigation

See T3 `research-ai-slop-prose-removal-2026-08-21` for the graded survey (corpus studies of LLM lexical markers at grade A; Williams and Bizup, Tufte, GOV.UK at A/B; practitioner convergence on negative-instruction weakness at B/C). The fable review verdicts constitute the implementation-side findings.

### Key Discoveries

- **Verified**: mechanical rewriting of existing prose fails review at scale (721-edit sweep read line by line; 19 percent worse or meaning-drifted; reverted).
- **Verified**: a lint rule shipped without corpus evidence produces false positives in this repo's register ("Certainly, ...", "Overall, ..."; both narrowed during remediation).
- **Documented**: readability formulas are unsuitable as targets (research §3).
- **Assumed**: positive-spec-plus-exemplar steering beats rule lists for this project's agents. This is Gap 4's experiment.

### Critical Assumptions

- [ ] The ratchet holds without regressions across releases. **Status**: Unverified. **Method**: observe two release cycles.
- [ ] Gate-time prose blocking does not push authors to `--skip-prose` by default. **Status**: Unverified. **Method**: audit override frequency after five gated RDRs.

## Proposed Solution

### Approach

To be decided; the candidate positions per gap are:

1. Gap 1: admit a rule only with (a) grade A/B evidence or a Sam directive, and (b) a measured false-positive probe over the repo corpus before it gains blocking force; taste rules enter as warn-only.
2. Gap 2: keep the remediated mapping; add commit-message linting via the existing hook layer as warn-only; leave bead text and T2 unlinted (records, not publications).
3. Gap 3: existing prose is rewritten only by a human-reviewed, per-file decision, never by fleet dispatch; the ratchet is the only pressure on legacy files.
4. Gap 4: run the exemplar-versus-rules comparison on real subagent deliverables with pre-registered criteria, reusing the RDR-196 bench harness shape and cost ceilings.

### Decision Rationale

Deferred to acceptance; the remediation encoded positions 2 and 3 provisionally and they have held under test.

## Alternatives Considered

### Alternative 1: No mechanical enforcement (spec only)

**Description**: keep `docs/writing-style.md`, delete the lint and gate.
**Pros**: zero false positives; no teaching-to-the-test pressure.
**Cons**: the May docs sweep regressed once already (em dashes returned within months); an unenforced spec is a preference, not a policy.
**Rejection**: the ratchet demonstrably holds a line the spec alone did not.

### Alternative 2: Full sweep of all legacy prose

**Description**: drive every surface to zero and drop the ratchet.
**Pros**: one tolerance class; simpler tests.
**Cons**: refuted empirically by the reverted RDR sweep; loses git-blame on thousands of lines; alters records.
**Rejection**: review verdict was categorical.

## Trade-offs

### Consequences

Two tolerance classes and a per-repo conditional gate cost explanation (this RDR is part of that cost). Authors of gated RDRs bear a new fix-or-override step.

### Risks

Override normalization (Gap 2 assumption above); rule accretion without the Gap 1 bar; the ratchet baseline drifting stale if the regenerate path is bypassed. The baseline's stale-high check makes the last loud.

## Implementation Plan

1. Phase 1: decide Gaps 1-3 (this RDR's acceptance).
2. Phase 2: run the Gap 4 experiment; fold its result into `docs/writing-style.md` §1.
3. Phase 3: commit-message warn-only lint, if Gap 2 lands with it.

## Test Plan

Existing: `tests/test_prose_lint.py` (engine), `tests/test_prose_style_lint.py` (`-m lint` gate + ratchet), gate tests in `tests/test_rdr_preamble.py`. Phase 2 adds the pre-registered experiment harness; Phase 3 adds hook tests.

## Validation

Gap 2 assumption audited after five gated RDRs; ratchet observed across two releases; experiment criteria pre-registered before any spend.

## Finalization Gate

Not yet run. Gate after Gaps 1-3 have decisions recorded.

## References

- T3 `research-ai-slop-prose-removal-2026-08-21`
- T2 `ptwm2-design-critique-fable-2026-08-21`, `ptwm2-code-review-fable-2026-08-21`, `ptwm2-rdr-sweep-editorial-fable-2026-08-21`
- Bead nexus-ptwm2

## Revision History

- 2026-08-21: drafted as part of the nexus-ptwm2 remediation round.
