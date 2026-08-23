---
title: "Independent Plugin Release Channel (plugin-vX.Y.Z-n)"
id: RDR-197
type: Architecture
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: Sam (lgtm 2026-08-22); gate battery per References
created: 2026-08-22
accepted_date: # YYYY-MM-DD, set by /rdr-accept
related_issues: ["nexus-cm9yt", "nexus-qkbo7"]
---

# RDR-197: Independent Plugin Release Channel (plugin-vX.Y.Z-n)

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

## Problem Statement

A plugin-only change is a diff that touches only the Claude Code plugin
surface: the skills, agents, hooks, commands, and resources under `conexus/`
and `sn/`. It contains no Python that ships in the `conexus` wheel (the
package installed from PyPI), no Java engine code, and no dependency pins.

Installed users never see such a change until a full client release ships.
The marketplace manifest (`.claude-plugin/marketplace.json`) pins each
plugin's `source.ref` to a release tag, and Claude Code loads plugin content
from that tag, never from a working tree. The pinning is deliberate: on
2026-07-25, a merged safety guard was believed live while sessions were
still being served from the previous day's tag, and three guards protected
nothing. The pin is right. The question is what it costs when a
plugin-surface fix is urgent and no client release is imminent.

The deeper problem is coupling: the plugin surface (skills, agents, hooks,
commands, resources) and the wheel (the nx CLI and MCP servers) are
different products that happen to share a repo, and today the plugin can
only ship by re-releasing the wheel. This RDR decouples them: a release
channel for plugin functionality that is independent of the nx CLI and MCP,
mechanically prevented from ever carrying wheel content. An adversarial
review of the first draft measured current demand honestly (over 60 days:
20 client releases, 17 commits touching exclusively the plugin's
loader-visible surface, one-to-two-day typical inertness), so the channel
ships with usage discipline, not a quota: client releases remain the usual
carrier while they are frequent; the channel exists so plugin functionality
never has to wait on, or force, a wheel release.

### Enumerated gaps to close

#### Gap 1: Plugin functionality cannot ship without re-releasing the wheel

The full release costs the ~2-hour test battery (the release checklist's
full gate sequence), a seven-manifest version bump, a PyPI publish of a
byte-identical wheel, and release ceremony. That is the wrong denominator
for shipping a skill, a hook fix, or a workflow: plugin functionality with
zero wheel change pays the whole wheel price or waits, inert, in the drift
ledger. The urgent case (a misbehaving hook in every installed session with
no release imminent) is the sharpest form, not the only one.

#### Gap 2: Every coupling that enforces the pin assumes one release channel

Four mechanisms tie the plugin pin to the wheel version: a parity test
requiring `source.ref` to equal `v` + the wheel version; the release
workflow treating every `v*` tag as a PyPI publish; the version-lockstep
hook (the SessionStart check that auto-upgrades the installed `nx` when the
plugin demands a newer one); and the release-window logic of the
drift-ledger test (the check on `conexus/PENDING_RELEASE.md`, the file
declaring every merged-but-not-live plugin change). An independent channel must alter each deliberately or prove it
unaffected.

#### Gap 3: "Plugin-only" needs a mechanical definition, not a judgment call

Two subdirectories of `conexus/` (`conexus/plans/`, `conexus/daemon/`) ship
inside the wheel via the build config's force-include (a hatch setting that
copies paths from outside `src/` into the built package). So "touches only
`conexus/`" is not the same as "touches nothing the wheel ships." Without a
mechanical proof, an independent channel will eventually carry code.

## Context

### Background

Out of the Claude Code release-notes leverage arc (epic nexus-qkbo7). The
design went through: an options memo (T2
`s7-plugin-release-channel-options-2026-08-22` [23346]); an `nx_plan_audit`
(NOT READY, four defects); a revision folding all four; conversion to a
first RDR draft; and a three-reviewer battery on that draft (fact-check
[23354], fidelity critique [23355], fable adversarial [23356]). The
adversarial review found a design-killing tag collision and the demand
numbers above; this draft incorporates every verdict, including the
repositioning that followed; at acceptance Sam reframed the channel as
independent plugin releasing (this revision) rather than emergency-only.

### Technical Environment

- Pinned-source marketplace model: `marketplace.json` `plugins[].source`
  is `git-subdir` with `ref` = an immutable tag.
- `tests/test_plugin_structure.py` (parity tests),
  `tests/test_plugin_release_drift_ledger.py` (the drift ledger:
  `conexus/PENDING_RELEASE.md` declares every merged-but-not-live plugin
  change; `SURFACE_BY_PLUGIN` there defines each plugin's loader-visible
  surface), `.github/workflows/release.yml` (fires on `v*` tags),
  `conexus/hooks/scripts/version_lockstep_hook.py` (RDR-143).
- Precedent: `engine-service-vX.Y.Z`, an accepted second tag family for the
  Java engine, decoupled from the PyPI cadence.

## Research Findings

### Investigation

Three passes over the live tree (develop @ 334990826): the memo's coupling
reading, the plan audit, and the draft battery. The fact-check confirmed
all four coupling claims at their cited lines after the audit's correction
round, confirmed the force-include and sdist (the source archive PyPI
publishes beside the wheel) path lists, confirmed that no
proposed name collides with anything existing, and refuted two numbers in
the first draft (both corrected below, at their sources).

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| hatch build config (`pyproject.toml`) | Yes | force-include ships `conexus/plans` and `conexus/daemon` inside the wheel; sdist adds `dt/scripts`, `README.md`, `LICENSE` |
| `release.yml` triggers | Yes | `v*` push tag fires PyPI publish; `plugin-v*` matches nothing today |
| lockstep hook read path | Yes | Reads plugin.json `version`; a cut that never touches version fields never skews it |
| drift-ledger window logic | Yes | `_in_release_window()` accepts only `v{pyproject_version}` refs today |

### Key Discoveries

- **Verified** (adversarial review, git mining, 60-day window): 20 client
  releases; 2,309 commits; 17 commits touching exclusively the
  loader-visible plugin surface; of 214 allowlist-qualifying commits, 197
  (roughly 12 in 13) were docs-only. Typical merged-plugin-change inertness
  one to two days; the observed maximum (~6 days) predates the current
  cadence. Routine demand for this channel is near zero.
- **Verified** (adversarial review): the first draft's tag scheme
  self-destructed on its second cycle: a counter that resets at each
  client release re-mints the immutable `plugin-v1` tag. Fixed by anchoring
  tags to the client release they build on (below).
- **Verified** (adversarial review, live example): an allowlist cut can
  split an atomic feature. Bead nexus-77cct's entry in today's
  `PENDING_RELEASE.md` spans `conexus/registry.yaml` (allowlisted) and
  `conexus/plans/builtin/` + `src/nexus/` (excluded); a naive cut would
  ship half the feature as a never-tested combination. Mitigated by a cut
  precondition (below).
- **Verified** (audit measurement, corrected by fact-check): develop
  currently carries 197 changed files (+15,498/−8,041) relative to main
  (the full unreleased diff, not wheel-specific content). Any cut branched
  off develop would drag it to main; the cut must branch off main.
- **Verified** (build config): the wheel-surface proof must be an
  allowlist, because the natural denylist misses `dt/scripts/`, `mcpb/`,
  `scripts/`, `README.md`, `LICENSE`: all real content of the wheel, the
  sdist, or the release assets.
- **Verified** (grep): only `sn/`'s plugin.json version has a hard parity
  test against the wheel version; a cut that touches no version field
  keeps every version-field parity test green and needs zero lockstep-hook
  change (the source.ref parity test itself needs this RDR's OR-extension).
- **Verified** (grep): `release-sandbox.sh smoke`, mandatory for plugin
  changes per AGENTS.md, runs in no CI workflow today (independent gap,
  own bead).
- **Documented**: `/plugin update` re-resolves `source.ref` and fetches the
  new tree immediately. This cuts both ways: a fix lands fast,
  and a bad cut lands just as fast, on every installed user, with no
  opt-in step. That reach is why cuts are scripted, batched, and
  deliberate.
- **Assumed**: a pathspec-limited import plus explicit revert of the two
  force-include subpaths makes the allowlist true by construction for adds
  and edits. File deletions are a known gap (`git checkout -- ` does not
  stage deletions); the cut script must diff-and-apply.

### Critical Assumptions

- [ ] The allowlist stays synchronized with the hatch build config.
  **Status**: Unverified. **Method**: a test asserting every force-include
  path in `pyproject.toml` is carved out of the allowlist.
- [ ] The lockstep hook stays silent on a plugin-cut install.
  **Status**: Unverified. **Method**: Spike (the one-time end-to-end cut,
  checking `lockstep.log` for absence of an upgrade attempt).
- [ ] The atomic-split precondition is checkable from ledger entries.
  **Status**: Unverified. **Method**: Spike against the current ledger
  (nexus-77cct is the test case).

## Proposed Solution

### Approach

Build the channel completely (tests, workflow, cut script,
documentation), verify it once end to end, and use it with the discipline
below.

- **Tag shape**: `plugin-v{X.Y.Z}-{n}` (for example `plugin-v7.15.0-1`):
  the client release the cut builds on, plus a sequence number within it,
  derived at cut time from the existing tag list. Collision with an
  existing tag is impossible by construction: the number comes from
  enumerating real tags, and there is no reset to forget because there is
  no stored state at all. This replaces the first draft's bare counter,
  whose reset rule collided with tag immutability on the second cycle.
- **Usage discipline** (not a quota): cut when accumulated plugin
  functionality is worth shipping and no client release is imminent; an
  open release PR always wins (never cut while one is in flight, and a
  scheduled client release carries the plugin content for free). Batch
  related plugin work into one cut, the same cadence judgment every other
  release lifecycle here uses. A misbehaving hook in installed sessions is
  the clearest cut-now case.
- **Sunset trigger** (gate warn 2026-08-22): if two years pass with zero
  cuts, decommission the channel (delete the workflow, the
  parity OR-branch, and the cut script) rather than paying its maintenance
  forever; the RDR record keeps the design recoverable if the need
  returns.

### Technical Design

- **The safety proof**: `test_plugin_tag_leaves_wheel_surface_untouched`
  (beside the drift-ledger tests, reusing `SURFACE_BY_PLUGIN`): every path
  in `git diff <base client tag>..HEAD --name-only` must be inside
  `{conexus/, sn/, .claude-plugin/marketplace.json}` and outside
  `{conexus/plans/, conexus/daemon/}`. `docs/` is deliberately NOT in the
  allowlist: the adversarial review measured docs-only commits outnumbering
  plugin-surface commits about 12:1, and a plugin channel that can carry
  docs will be argued into carrying docs. Fails loud when the comparison
  cannot resolve.
- **Parity test extension, stated exactly**:
  `test_marketplace_source_ref_matches_pyproject` accepts
  `ref == "v" + pyproject_version` (unchanged happy path), OR
  `ref == "plugin-v" + pyproject_version + "-" + n` for any positive
  integer `n`, judged per plugin. Any other shape still fails. There is
  NO counter file: git's own tag list is the only record of cuts. The
  cut script picks `n` by enumerating existing tags
  (`git tag -l 'plugin-v{version}-*'`) and taking the next number, so a
  collision with an immutable tag is impossible by construction, and no
  reset, no ownership, and no cross-plugin state exist to get wrong.
  (Two earlier designs tracked `n` in a repo file; a shared counter was
  refuted outright by plan-audit round 3, and the per-plugin file it
  briefly became was deleted the same day at Sam's direction: it
  duplicated state git already keeps.)
- **Atomic-split precondition**: the cut script maps each
  `PENDING_RELEASE.md` entry it would ship to its cited bead, and refuses
  if any such bead's commits since the base client tag touch excluded
  paths (the nexus-77cct shape: half a feature in the allowlist, half in
  the wheel). Override requires editing the ledger to defer that entry,
  never a flag.
- **The cut** (script with its own tests, not a checklist): branch
  `plugin-release/{X.Y.Z}-{n}` off main; import allowlisted paths from
  develop by diff-and-apply (deletion-safe); revert `conexus/plans/` and
  `conexus/daemon/` to main's content; derive `n` from the existing tag
  list; move `source.ref`
  for the changed plugin(s) only, with no version field moving anywhere; empty
  covered ledger entries; run the minimal battery; PR to main; merge; tag;
  push; back-merge main into develop (expected conflict-free: main gains
  nothing develop lacks).
- **Minimal battery** (~10-15 min): the `-m lint` bucket,
  `tests/test_plugin_release_drift_ledger.py`, `tests/hooks/`, and
  `./tests/e2e/release-sandbox.sh smoke`, the only gate proving a real
  isolated install round-trip, already mandatory for plugin changes,
  currently absent from CI. Explicitly skipped, with reasons: substrate
  gates, migration rehearsal, fresh-install MVVs, shakedown, engine floor.
  None of them executes plugin-loader content.
- **Workflow**: `plugin-release.yml` on `plugin-v*` tags, verify-only (the
  proof + emptied ledger), publishes nothing to PyPI, no wheel, no `.mcpb`
  (the Desktop bundle attached to client releases). Gets the same
  wiring-coverage tests the drift-ledger workflow has.
- **Window logic**: `_in_release_window()` (the drift-ledger test's notion
  of "a release is being cut right now") gains the anchored-tag shape.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Allowlist proof | `tests/test_plugin_release_drift_ledger.py` (`SURFACE_BY_PLUGIN`) | Extend: same file, same dict, one new test |
| plugin-release.yml | `plugin-drift-ledger.yml` shape + its wiring-test pattern | Reuse the pattern; new workflow file |
| Tag lifecycle | `engine-service-release.yml` precedent | Reuse the shape (verify-only variant) |
| Cut script | none | Build, with tests; the deletion-safe import and the atomic-split check are its hard parts |

### Decision Rationale

The plugin surface and the wheel are different products; a channel that
decouples their release cadences is the point, not a workaround. The
mechanical fence (the allowlist proof) is what makes the decoupling safe,
and the anchored tag shape is what makes it durable. The 60-day demand
numbers (17 loader-surface commits, 1-2 day inertness, near-daily client
releases) are the honest context for the usage discipline: while client
releases are frequent they carry plugin content for free, so cuts should
be batched and deliberate, not reflexive. The 2026-07-25 incident class (a
guard everyone believes is live) is the sharpest single justification: a
tested channel beats either a 2-hour ceremony or improvised git surgery
under pressure. Sam's decision at review (2026-08-22): frame this as
independent plugin releasing, not an emergency-only path.

## Alternatives Considered

### Alternative 1: Unanchored plugin-vN counter channel (this RDR's first draft)

**Description**: the same channel with a bare reset-per-release counter
and `docs/` in the allowlist, cut from a develop-based branch.
**Pros**: none over the accepted form.
**Cons**: the tag scheme re-mints an immutable tag on its second cycle;
the develop-based cut drags unreleased wheel changes to main by
construction; `docs/` in the allowlist invites scope creep (docs-only
commits outnumber plugin-surface commits about 12:1).
**Rejection**: every defect was found by the draft battery and fixed in
the accepted form (anchored tags, main-based cut, docs/ excluded).

### Alternative 2: Separate plugin repository

**Description**: move `conexus/`+`sn/` to their own repo and marketplace.
**Pros**: fully independent tagging.
**Cons**: a pinned tag in a second repo is exactly as inert; parity
enforcement goes cross-repo; the single-tree drift-ledger model dies.
**Rejection**: does not shrink the window; multiplies surfaces.

### Alternative 3: Do nothing (batching + drift ledger, status quo)

**Description**: keep riding client releases.
**Pros**: zero new machinery; honest fit to measured demand.
**Cons**: leaves the sharpest case exactly where 2026-07-25 found it
(choose between a 2-hour ceremony and improvised surgery, under pressure)
and keeps every other piece of plugin functionality coupled to wheel
cadence forever.
**Rejection**: leaves the plugin surface permanently unable to ship
without a wheel release; acceptable while client releases are near-daily,
wrong as a permanent coupling between two different products.

### Alternative 4: Change-class-gated cheap client releases

**Description**: classifier skips wheel-heavy gates when a release's diff
is plugin-only.
**Pros**: helps ordinary releases too.
**Cons**: still republishes an identical wheel, bumps seven surfaces, pays
full ceremony.
**Rejection as substitute**: filed as an independent follow-on for the
client-release path.

## Trade-offs

### Consequences

Two tag families in one repo (unambiguous prefix; anchored shape removes
the collision). Plugin.json's `version` never marks plugin cuts; the
tag alone does. The release-cadence rule gains a narrowly-worded
exception covering an independent plugin channel with a written usage
discipline. Maintenance:
window shapes, wiring tests, the cut script, and contributor docs exist
even when no cut is pending; that cost is accepted for the decoupling and
was weighed against demand honestly.

### Risks

- Allowlist drift from the hatch config (guarded by the sync test).
- The cut script's deletion handling and atomic-split check (guarded by
  script tests plus the one-time spike; never run as a one-liner).
- Channel creep: a cheap channel attracts traffic that belongs elsewhere.
  Guarded three ways: the
  proof fails loud on any wheel-surface touch, `docs/` is out of the
  allowlist, and the usage discipline is written into AGENTS.md's exception
  text so "was this warranted" is checkable in review.
- Instant blast radius of a bad cut (every installed user within one
  refresh). Guarded by the sandbox-smoke requirement in the minimal
  battery and by the usage discipline: batched, deliberate cuts. Rollback is the
  same mechanism in reverse: repoint `source.ref` back to the base client
  tag (or cut `-{n+1}` with the fix) through the same PR path; the next
  refresh restores every install.

## Implementation Plan

1. Phase 1: tests first: parity-test extension (exact OR-logic above),
   allowlist proof + hatch-sync assertion, window shape, anchored-tag
   validation, lockstep-silence regression test.
2. Phase 2: `plugin-release.yml` + wiring coverage; the cut script
   (deletion-safe import, atomic-split precondition) with tests.
3. Phase 3: AGENTS.md exception text (independent channel + usage
   discipline + sunset trigger), contributing-doc section; the one-time end-to-end
   spike cut of `plugin-v{current}-1` on a scratch fix.
4. Independent follow-on beads (not gating): sandbox-smoke CI wiring;
   change-class battery for client releases.

## Test Plan

All Phase 1 items are pytest in the two existing plugin test files plus
`tests/hooks/`; the workflow gets wiring-coverage tests; the cut script
gets unit tests for import/deletion/split-check; the spike is the only
manual step and doubles as validation.

## Validation

The spike cut: sandboxed `/plugin update` picks up the anchored tag within
one refresh; `lockstep.log` shows no upgrade attempt; sandbox smoke passes;
the atomic-split check refuses a deliberately-straddling test entry. After
the next real client release: parity tests green, window logic correct,
`git log` shows the spike commit in main's history.

## Finalization Gate

Not yet run. Gate after Sam's review of this draft.

## References

- T2 `s7-plugin-release-channel-options-2026-08-22` [23346] (revised memo)
- `nx_plan_audit` 2026-08-22 (NOT READY; four defects, folded)
- Draft-battery verdicts: facts [23354], fidelity [23355], adversarial
  [23356] (tag collision, demand mining, atomic-split evidence)
- Beads nexus-cm9yt (stream), nexus-qkbo7 (epic)
- Precedent: AGENTS.md § Engine-service release; RDR-143 (lockstep)

## Revision History

- 2026-08-22 (design simplification, Sam's direction): the channel is
  COUNTER-LESS. `conexus/PLUGIN_CHANNEL_VERSION` is deleted from the
  design entirely; the parity test validates the tag SHAPE per plugin,
  and the cut script derives the next sequence number from git's own tag
  list. This supersedes the same-day erratum below (which had made the
  counter per-plugin): the root observation is that a counter file
  duplicates state git already keeps, and every audit-round defect
  (shared-vs-per-plugin, reset ownership, read source) was a property of
  that duplication, not of the channel.
- 2026-08-22 (post-acceptance erratum, plan-audit round 3): the shared
  channel counter is refuted and becomes per-plugin (finding B1: after a
  second cut on the other plugin, one global counter cannot match both
  anchored refs, so parity goes permanently red). Reset value corrected
  from 1 to 0 (0 = no cuts on this client version; the counter names the
  most recent cut, so its first value is 1 only after the first cut).
  Two further divergences recorded rather than left silent: the
  wheel-surface proof anchors to the TAG under audit, never to HEAD; the
  cut script reads the counter from the ref it replaces on main, never
  from a working tree.

- 2026-08-22 (acceptance review): reframed at Sam's direction from
  "dormant emergency path" to an independent plugin release channel: the
  plugin surface and the wheel are different products, and this channel
  decouples their cadences. Mechanics unchanged (anchored tags, allowlist
  proof, cut flow, minimal battery); the emergency-only use trigger became
  a usage discipline (batch, defer to imminent client releases); the
  adoption trigger dissolved (the channel is usable when warranted); the
  sunset trigger stays.

- 2026-08-22: first draft from the audited memo.
- 2026-08-22 (gate): finalization gate PASSED, all six criteria; sunset
  trigger added per the gate's one warn.
- 2026-08-22 (same day): full battery on the draft; repositioned from
  routine channel to a disciplined form; tag scheme anchored to the
  client release (collision fix); `docs/` removed from the allowlist;
  atomic-split precondition added; two factual errors corrected (the
  2026-07-25 tag was about one day stale, not "five releases"; the
  develop-vs-main measurement restated as the full diff, 197 files at
  measurement time); parity OR-logic written out; open questions resolved
  (the shared-vs-per-plugin counter question, then thought moot; `docs/`
  decided out). The 2026-08-22 erratum above supersedes the counter half:
  per-plugin, refuted-not-moot.
- 2026-08-22 (verification round, T2 [23358]): the garbled commit-count
  sentence rewritten (214 qualifying, 197 of them docs-only); the parity
  claim narrowed to version-field parity tests, naming the `source.ref`
  test's own OR-extension as the exception; a rollback antidote added to
  Risks; the open-release-PR exclusion written into the use trigger; the
  shared-counter resolution moved from this history into the technical
  design; `battery`, `drift ledger`, `sdist`, and the counter reset rule
  each defined at first use.
