---
title: "Standing Beta Release Channel: a conexus-beta Marketplace Entry + PyPI Pre-Releases to De-Risk Migration-Heavy Releases Before GA"
id: RDR-172
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
created: 2026-06-27
related_issues: []
related_rdrs: [RDR-155, RDR-159]
supersedes: []
amends: "AGENTS.md § Release cadence policy rule 3 (one channel until proven otherwise)"
---

# RDR-172: Standing Beta Release Channel

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The conexus release model is **single-channel** by deliberate policy (cadence rule 3: "one channel
until proven otherwise. No `-dev` / `-rc` / `-canary` variants. If a beta channel becomes necessary,
file an RDR."). That has served well — until 6.0.0.

6.0.0 is the **migration-capable** release: post-RDR-155-P4a, any user who updates to it must run the
`nx guided-upgrade` Chroma→pgvector migration. That migration has been validated only on Hal's own
machine and in the isolated `migration-rehearsal` harness — **never by a real user in the wild**. With
single-channel releases, the *first real-user migration is the GA itself*: the blast radius (every
user's data) meets an unproven-in-the-wild path at the worst possible moment.

Nobody is force-updated (pinned-source marketplace + PyPI versioning both make updates opt-in), but
the cliff is real: the moment a user *chooses* to update past P4a, the mandatory migration fires. We
want a population of opt-in users to traverse that path and surface failures **before** GA.

This is exactly the "a beta channel becomes necessary" trigger rule 3 names — hence this RDR.

## Decision

Stand up **one** standing beta channel: a `conexus-beta` plugin entry in the existing
`.claude-plugin/marketplace.json`, its `source.ref` pinned to immutable **pre-release tags**,
installing a **pre-release `conexus`** from PyPI. Opt-in via `/plugin install conexus-beta`. Beta and
stable share one develop→main lineage; promotion is a normal GA cut. Parity tests stay strict, made
**per-entry** rather than single-channel. Scope is bounded to exactly one beta channel — no further
`-dev`/`-canary` proliferation without another RDR.

First use: ship `6.0.0rc1` on `conexus-beta`, let opt-in users run the real guided-upgrade migration,
then promote to `6.0.0` GA.

## Approach

Numbered so the phase-review gate can cross-walk each against its closing bead.

1. **Marketplace `conexus-beta` entry.** Add a second plugin entry to `.claude-plugin/marketplace.json`
   (name `conexus-beta`), `source.ref` pinned to immutable pre-release tags (`vX.Y.ZrcN` / `vX.Y.ZbN`),
   distinct from the stable `conexus` entry. One manifest, one repo.

2. **PyPI pre-release versioning.** Publish PEP 440 pre-releases (`6.0.0rc1`, `6.1.0b1`) from develop.
   `pip`/`uv` ignore them without `--pre`; the `conexus-beta` plugin (its `mcpb/` dependency pin) pins
   the pre-release explicitly so stable users never receive one.

3. **Release-workflow pre-release discrimination (the load-bearing trap).** The tag-triggered Release
   workflow must distinguish a pre-release tag (`vX.Y.ZrcN`/`bN`) from a GA tag (`vX.Y.Z`): a
   pre-release tag publishes to PyPI **as a pre-release** and bumps **only** the `conexus-beta`
   marketplace entry's `source.ref`; it must **never** touch the stable `conexus` entry or publish a
   non-pre PyPI release. A GA tag behaves as today.

4. **Per-entry parity tests (cadence rule 6 stays strict).** Extend `TestMarketplaceVersion`
   (source.ref == version, and the 7-manifest parity) to assert **per channel**: the `conexus-beta`
   entry's `source.ref` matches its declared pre-release version; the stable `conexus` entry's parity
   is unchanged. No loosening of the stable guarantee — the assertion becomes per-channel, not single.

5. **Promotion (RC→GA) mechanics.** Document + mechanize that beta and stable are the same
   develop→main lineage: a GA cut bumps the stable entry + the 7 manifests to the GA version and the
   beta line advances to the next pre-release; there is no separate beta codebase or cherry-pick.

6. **`mcpb/` beta variant decision.** Decide (and implement) whether `mcpb/pyproject.toml` +
   `mcpb/manifest.json` get a beta variant or stay stable-only; reconcile with the parity gate either
   way.

7. **SKILL + DOC surface updates (carried in THIS RDR, not deferred — the engine-release lesson).**
   a. `.claude/skills/release/SKILL.md` — add the beta-cut path: pre-release version scheme, the
      pre-release-vs-GA tag discrimination, which marketplace entry bumps, per-entry parity, and the
      RC→GA promotion step. Either a new Step or a dedicated "Beta channel" section.
   b. `AGENTS.md` § Release cadence policy — amend rule 3 (one channel → one stable + one standing
      beta, governed by this RDR; still no `-dev`/`-canary`); add a "Beta channel" subsection mirroring
      the skill.
   c. Consumer-facing opt-in doc — how a user installs/leaves the beta channel
      (`/plugin install conexus-beta`), what beta implies (pre-GA, may run unproven migrations), and
      the downgrade/rollback path.
   d. Reconcile with the two-lifecycle model (T2 `nexus/two-release-lifecycles-engine-vs-pypi`): this
      beta channel is the **PyPI/plugin** lifecycle only; the engine-service lifecycle is unaffected.

8. **Beta-user migration safety / rollback story.** Document that the Chroma source is copy-not-move
   (RDR-155 RF-5) so a beta migration is rollback-safe; spell out the opt-out/downgrade path for a beta
   user whose migration misbehaves.

## Alternatives Considered

- **Per-risky-release RC (no standing channel):** cut `rcN` tags only for risky releases, promote, done.
  Lighter, but Hal chose a standing track for a continuous early-adopter population (per-release RC
  gives no durable beta cohort).
- **PyPI pre-releases only, no marketplace beta:** reaches CLI testers but not plugin users (the
  primary consumers) — contradicts the standing-marketplace-track decision.
- **Separate marketplace.json / repo for beta:** cleaner channel isolation but duplicates marketplace
  plumbing and the source-of-truth surface; rejected for the single-manifest two-entry pattern.

## Consequences

- Opt-in users validate migration-heavy releases (starting with 6.0.0's guided-upgrade) in the wild
  before GA — the core win.
- Two live channels = ongoing dual-channel maintenance and a strictly-bounded parity surface; the
  per-entry parity tests are the guardrail that keeps the stable channel's guarantee intact.
- The release process gains a beta-cut path; the skill + AGENTS.md + a consumer opt-in doc move in
  lockstep (Approach §7) so the channel isn't half-documented (the engine-drift failure mode).
- Scope is fixed at one beta channel; any further channel needs its own RDR.

## Open Items (resolve in research phase)

1. Exact pre-release tag/version grammar and the precise workflow conditional that discriminates
   pre-release from GA (and never bumps the stable entry on a pre-release tag).
2. Per-entry parity-test redesign details against the current `TestMarketplaceVersion` /
   `source.ref` assertions.
3. `mcpb/` beta-variant decision (§6).
4. Whether the beta cadence is per-develop-milestone or time-boxed; who triggers (human, rule 5).

## Design Heritage

Approved design memo: T2 `nexus/design-beta-release-channel.md` (brainstorming-gate, 2026-06-27).
Governing context: AGENTS.md § Release cadence policy (rules 3 + 6) + the marketplace pinned-source
playbook. Motivating release: RDR-155 (pgvector cutover) + RDR-159 (guided-upgrade migration). No
prior art in T3 (first beta channel).
