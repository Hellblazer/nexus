---
title: "Plugin↔CLI Version Lockstep: a SessionStart Version-Marker Hook That Keeps the nx CLI in Sync With the conexus Plugin"
id: RDR-143
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-01
related_issues: []
related_rdrs: [RDR-076, RDR-125, RDR-141, RDR-142]
supersedes: []
related_tests: []
implementation_notes: ""
---

# RDR-143: Plugin↔CLI Version Lockstep: a SessionStart Version-Marker Hook That Keeps the nx CLI in Sync With the conexus Plugin

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

#### Gap 1: the plugin and the nx CLI ship the same version but update through independent channels, so they silently drift out of lockstep

The conexus plugin and the `nx` CLI ship the **same version** (CI enforces parity across the manifests), but they **update via independent channels**: the plugin updates through `/plugin update`, while the CLI updates through `uv tool install`. Nothing keeps the two in lockstep. The result is silent version skew: a user updates the plugin but not the CLI (or vice versa), and the two halves of conexus run mismatched versions.

That skew is the **root of this session's RDR-141 / RDR-142 / 5.6.2 P0s**, the version-skew double-writer (RDR-141), the migration-completeness-vs-version-row disagreement (RDR-142), and the 5.6.2 local-mode-search outage all trace back to a plugin and CLI that were allowed to disagree on version. The user proposed a self-removing `SessionStart` hook that auto-updates the CLI so a plugin update drags the CLI along with it.

## Context

- **This RDR owns one decision:** *how to keep the nx CLI in version lockstep with the conexus plugin once the plugin updates.* The embedder-default question (384-vs-768 local provisioning) was previously floated inside this RDR; it has been **pulled out into RDR-144** (guided onboarding / local-embedder provisioning), where it belongs with the guided-choice mechanism. RDR-143 stays scoped to plugin↔CLI version lockstep.
- **RDR-076** established the idempotent upgrade mechanism (`nx upgrade`) and the `_nexus_version` gate, the safe, extras-preserving upgrade path that any lockstep automation must route through.
- **RDR-141 / RDR-142** are the concrete failures this lockstep is meant to prevent: they are downstream symptoms of allowing the plugin and CLI to disagree on version.

### Capability finding (claude-code-guide, current docs)

- **There is NO self-removing plugin hook.** `once: true` is a skill / agent-frontmatter affordance only, it is **not** available for plugin `hooks` / `hooks.json`. And once-ever is the **wrong semantics** anyway: we want **once-per-version**, not once-ever, because the hook must re-fire after each plugin update.
- **The idiomatic supported pattern is `SessionStart` (matcher `startup`) + a version-marker file.** The hook reads a marker file recording the last version it acted on; it acts only when the current plugin version differs, then rewrites the marker. This is **idempotent**, fires **once per plugin version**, and **re-fires on each update**, exactly the desired semantics.
- **`SessionStart` / `startup` is SYNCHRONOUS** (it blocks session startup, with a 10-minute default timeout) and **can emit `additionalContext`** back into the session. The synchronous-blocking property is the central constraint on any action shape (see CA-4): a long or hanging upgrade would wedge startup.

## Dominant Hazard

A naive auto `uv tool install conexus==X` **STRIPS the `[local]` extra** (and the mineru scripts) → the install falls back to the **384-dim** ONNX MiniLM embedder → which **reintroduces the exact 5.6.2 local-mode-search P0** on an install that was previously a healthy 768-dim `[local]` install.

**Locked requirement:** any automating action MUST route through `nx upgrade` / an **extras-preserving install**, and **NEVER** a raw `uv tool install`. This is the hard constraint that rejects Shape C and shapes the safe action in Shape B.

## The docs point at the same footgun (in-scope, "do first")

The raw-`uv tool install conexus` footgun is already recommended across the docs and one hook script. These all recommend the extras-stripping raw install:

- `README:50`
- `docs/getting-started.md:21`, `:254`, `:260`, `:271`
- `docs/configuration.md:16`
- `docs/cli-reference.md:1407`
- `docs/integrations/devonthink-smart-rules.md:69`
- `conexus/hooks/scripts/preflight.py:90-92`

**Nuance: this is not a blind find-replace.** A *fresh* raw install yields a **consistent** MiniLM-384 embedder (lower quality, **not broken**). The **break** is the **UPGRADE-strips-`[local]`** case on an **existing 768-dim install**. The correct replacement command depends on **CA-2** (which upgrade verb actually preserves the extras, `uv tool upgrade` vs `scripts/reinstall-tool.sh` vs `nx upgrade`), so this is **research-then-fix**, not a mechanical substitution.

The doc correction is **in-scope** and explicitly **"do first"**: it is a **live footgun**, independently shippable **ahead of the hook**, and reduces the blast radius before any automation lands.

## Proposed Solution

_Draft, lock after research. Three shapes, lightest to heaviest. No decision is locked in this draft._

- **Shape A: detect + nudge via `additionalContext` (leaning; lowest risk, ~90% of the value).** The `SessionStart` hook compares the plugin version against the version-marker file; on a mismatch, it emits an `additionalContext` message telling the user the CLI is out of lockstep and how to update it safely (the extras-preserving command). It takes **no mutating action**, it only informs. Lowest risk, no startup-blocking work, and captures most of the value (the user is told, in-session, the moment skew exists).
- **Shape B: detect + run `nx upgrade` (automated safe action).** On a version mismatch, the hook **runs `nx upgrade`** (the extras-preserving, idempotent path, RDR-076), implemented to **fast-fail / non-blocking** so it cannot wedge synchronous startup, and to **skip editable / dev installs** (do not clobber a `uv sync` dev tree). Automates the fix while honoring the dominant-hazard constraint by routing through `nx upgrade`, never a raw install.
- **Shape C: auto raw `uv tool install conexus==X` (REJECTED).** Rejected on three independent grounds: it **strips the `[local]` extra** (the dominant hazard → reintroduces the 5.6.2 P0), it **blocks synchronous startup** on a network-bound install, and it **clobbers dev / editable installs**.

## Decision

_Undecided, this RDR is a draft. No decision is locked. Shape A is **leaning** (lowest risk, ~90% of the value), but the choice between A and B depends on the Critical Assumptions below (notably CA-2 / CA-4). Do **not** treat this section as a locked decision._

## Implementation Plan

_No shape locked; sketch only, pending research on CA-1..CA-4._

- **Doc-footgun correction (do first, shippable ahead of the hook):** replace the raw `uv tool install conexus` recommendations at the file:line sites listed above with the extras-preserving command, once CA-2 settles which verb preserves `[local]` + migrations. Research-then-fix; not a blind find-replace.
- **Version-marker hook (Shape A or B):** a `SessionStart` (matcher `startup`) hook that reads/writes a version-marker file, acts once-per-version, and either emits `additionalContext` (A) or runs `nx upgrade` (B).
- **Action safety (if Shape B):** fast-fail / non-blocking so synchronous startup is never wedged; skip editable/dev installs; route only through `nx upgrade`, never raw `uv tool install`.

## Critical Assumptions

_Unverified, draft. Next step: `/conexus:rdr-research` CA-1..CA-4, then `/conexus:rdr-gate`._

- **CA-1: the version-marker hook is implementable with documented I/O.** A `SessionStart`/`startup` hook can read a marker file, compare it to the current plugin version, emit `additionalContext`, and rewrite the marker, all within the documented hook contract (synchronous, `additionalContext`-emitting, 10-min timeout).
- **CA-2: `nx upgrade` preserves `[local]` + migrations.** `nx upgrade` (RDR-076) is genuinely extras-preserving and migration-safe, and we can name **which** upgrade verb preserves the extras (`uv tool upgrade` vs `scripts/reinstall-tool.sh` vs `nx upgrade`). This also determines the correct doc-footgun replacement command.
- **CA-3: editable-install detection.** The hook/action can reliably detect an editable / dev (`uv sync`) install and **skip** the auto-action, so it never clobbers a development tree.
- **CA-4: non-blocking action.** The action (especially Shape B's `nx upgrade`) can be made fast-fail / non-blocking so it does not wedge the synchronous `SessionStart`/`startup` path within its timeout.

## Finalization Gate

_Pending. Next: `/conexus:rdr-research` to verify CA-1..CA-4, then `/conexus:rdr-gate`._

## References

- This session's discussion: plugin-vs-CLI independent update channels; the self-removing-hook proposal; the `uv tool install` extras-strip hazard.
- RDR-076 (idempotent, extras-preserving `nx upgrade` + `_nexus_version` gate), RDR-125, RDR-141 (T2 version-skew double-writer, a downstream symptom of plugin↔CLI skew), RDR-142 (migration completeness vs the version row, another downstream symptom).
- RDR-144 (guided onboarding / local-embedder provisioning, the 384-vs-768 embedder-default decision was pulled out of this RDR into there).
- Capability source: claude-code-guide (current docs), `SessionStart`/`startup` synchronous + `additionalContext`; no self-removing plugin hook; version-marker file as the once-per-version idiom.
- Doc-footgun sites: `README:50`, `docs/getting-started.md:21/254/260/271`, `docs/configuration.md:16`, `docs/cli-reference.md:1407`, `docs/integrations/devonthink-smart-rules.md:69`, `conexus/hooks/scripts/preflight.py:90-92`.

## Revision History

- 2026-06-01: Draft. Originated from the realization that the plugin and nx CLI update through independent channels with nothing keeping them in lockstep, the root of this session's RDR-141 / RDR-142 / 5.6.2 P0s. Capability finding: no self-removing plugin hook; `SessionStart`/`startup` + version-marker file is the once-per-version idiom. Dominant hazard: raw `uv tool install` strips `[local]` and reintroduces the 5.6.2 search P0, so any action must route through `nx upgrade`. Shapes A (nudge, leaning) / B (`nx upgrade`) / C (rejected). Doc-footgun correction in-scope and "do first." CA-1..CA-4 pending research. Embedder-default decision pulled out into RDR-144.
