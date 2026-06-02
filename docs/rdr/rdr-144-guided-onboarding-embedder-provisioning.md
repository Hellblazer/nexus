---
title: "Guided Install / Onboarding & Local-Embedder Provisioning (nx init): Make the 384-vs-768 Embedder an Explicit Guided Choice, Not a Blunt Packaging Default"
id: RDR-144
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-01
related_issues: []
related_rdrs: [RDR-076, RDR-126, RDR-143, RDR-038, RDR-109]
supersedes: []
related_tests: []
implementation_notes: ""
---

# RDR-144: Guided Install / Onboarding & Local-Embedder Provisioning (`nx init`)

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

#### Gap 1: delivery (`uv tool install`) is declarative and cannot guide a choice, so we silently default local users to the lower-quality 384-dim embedder without telling them

Nexus is delivered via `uv tool install conexus` — a *declarative, non-interactive* package install. The local embedder is gated entirely on whether the `[local]` extra was installed:

- `uv tool install conexus` (the default) → tier-0 **ONNX MiniLM, 384-dim**.
- `uv tool install "conexus[local]"` → tier-1 **bge-base-en-v1.5, 768-dim** (materially better local search).

So the default install silently lands the **lower-quality** embedder, and until this session the docs only contrasted MiniLM-local against Voyage-*cloud* for "higher-quality embeddings" — the better *local* option (`[local]`/bge-768) was effectively undocumented. A user gets the weaker search experience without being told a better local option exists, or what the trade is.

The naive fix — flip the packaging default to `[local]` — is the wrong tool, because **a packaging default cannot express a choice**: bge-768 is not bundled like the ONNX MiniLM; it comes via `fastembed`, which **downloads the model (~hundreds of MB) on first embed**. Defaulting to 768 silently imposes that download on everyone (including cloud-only users who never touch bge) and breaks the current "instant, fully offline from the first command" property. Defaulting to 384 under-serves local users. Neither *tells the user* anything.

The missing capability is not a different package manager — it is a **provisioning / onboarding step** that runs *after* delivery and makes the embedder (and mode) an explicit, informed choice: detect cloud-vs-local, recommend bge-768 for local with the model-download cost stated up front, fetch the model with visible progress, and record the decision. That is what "guide the user through a clear choice" requires.

## Context

- **The decision this RDR owns:** *what local embedder a fresh user ends up with, and how that choice is presented.* The maintainer's stated intent: **768 should be the default for local.** The open question this RDR resolves is the *mechanism* — packaging default vs guided provisioning vs a blend — and the handling of the inherent first-use model download. (This decision was previously floated inside RDR-143; it is pulled here, where it belongs with the guided-choice mechanism. RDR-143 stays scoped to plugin↔CLI version lockstep.)
- **We already have onboarding surfaces to build on — a full new installer is not required:** `nx config init` (cloud-creds wizard), `src/nexus/mcp/_first_run.py` (runs at MCP startup, already ensures the daemon), `nx doctor` (already detects the active embedder + dimension mismatch — RDR-109, and the #1061 E1 outage check), `scripts/reinstall-tool.sh` (proves `nx` can re-drive its own `uv tool install --reinstall "conexus[local]"` to add an extra), and `conexus/hooks/scripts/preflight.py` (already bootstraps uv + conexus). RDR-038 established the local T3 backend; RDR-126 explores the MCPB one-click path for Desktop.
- **Delivery (PyPI) stays.** This RDR adds provisioning on top of it, not a replacement distribution.

## Research Findings

_To be populated via `/conexus:rdr-research`. Key questions:_

1. **Model-fetch mechanics & UX.** How does `fastembed`/bge fetch + cache the model? Can `nx init` pre-fetch it with a progress bar (and offline/air-gapped failure handling) so it is a deliberate, visible step rather than a surprise mid-`search`?
2. **Can `nx` add its own extra cleanly at runtime?** `reinstall-tool.sh` shells `uv tool install --reinstall "conexus[local]"`; can `nx init` invoke that safely (editable-install detection, extras preservation, no clobber of a dev tree)?
3. **Default-for-local mechanism.** Three sub-mechanisms to compare (see §Proposed Solution): packaging default (promote `[local]` deps), guided `nx init` default, or a hybrid. Which delivers "768 by default for local" without bloating cloud-only installs or breaking instant-first-run?
4. **Existing-user migration.** A user already on MiniLM-384 with 384-dim collections who moves to bge-768 needs a **reindex** (vectors are dimension-incompatible — RDR-109 / the staleness check). What does the guided flow do for them — detect, warn, offer reindex?
5. **Where does the choice get recorded** (config key / `NX_LOCAL_EMBED_MODEL`) and how do `nx doctor` + the daemon read it consistently?

## Proposed Solution

_Draft — lock after research. Spectrum from lightest to most capable; the embedder-default decision rides whichever mechanism is chosen._

- **Shape A — Guided `nx init` verb (recommended starting point).** A first-run/`nx init` (or broadened `nx config init`) that: detects cloud vs local → for local, **recommends bge-768** and states the one-time model-download cost, offering the light 384 as the explicit alternative → if 768, ensures the extra is present (shell `uv tool install --reinstall "conexus[local]"`) and **pre-fetches the model with a progress bar** → records the choice → for an existing 384-corpus user, detects and offers reindex. Reuses every surface above; no packaging change; the choice is explicit and informed. Can be invoked by the plugin's first-run and surfaced by `nx doctor`.
- **Shape B — `curl | sh` bootstrap.** Installs `uv` + `conexus[choice]` + runs `nx init`. The preflight hook already half-does this. A friendlier zero-to-running path; larger surface.
- **Shape C — MCPB / one-click** for the Claude Desktop / plugin path — RDR-126 territory; cross-reference, do not duplicate.
- **Embedder-default sub-decision (orthogonal to A/B/C):**
  - **(i) Guided default** — `nx init` defaults the *recommendation* to bge-768 for local and provisions it; bare `uv tool install conexus` still ships MiniLM until the user runs init. Pairs with Shape A; no packaging bloat for cloud users.
  - **(ii) Packaging default** — promote `fastembed`+bge into base deps (like mineru was promoted) so `uv tool install conexus` *is* bge-768. True zero-config 768, but heavier install for everyone + first-use model download for all. Rejected-leaning unless we accept the universal weight.

Leaning: **Shape A + (i)** — "768 by default for local, presented as an informed choice via `nx init`, model fetched visibly" — delivers the maintainer's intent (768 default for local) without imposing bge weight on cloud-only installs or silently downloading a model. Confirm at research/gate.

## Implementation Plan

_To be detailed after the shape is locked. Likely includes: an `nx init` / first-run guided flow; bge model pre-fetch with progress + offline failure handling; runtime extra-add via the reinstall-tool path (editable-install-safe); choice recorded to config and read consistently by `nx doctor` + daemon; existing-384-user detect-and-offer-reindex; docs rewritten so the embedder choice is presented at onboarding (not buried); plugin first-run invokes/surfaces `nx init`. Tests: fresh-local → bge-768 provisioned + model fetched (mocked); cloud → no bge; existing-384 → reindex offered; offline → graceful failure not a wedged search._

## Trade-offs

- **First-use model download is inherent to bge-768** — any path to 768 (guided or packaging) pays it once. The guided path makes it *visible and chosen*; the packaging default makes it *silent and universal*. This is the core argument for Shape A over (ii).
- **An onboarding step adds friction** vs a one-line install. Mitigated by making `nx init` fast, skippable, and auto-surfaced by `nx doctor` when skipped.
- **Cloud-only users** should not carry bge weight — favors guided/(i) over packaging/(ii).
- **Existing users** moving 384→768 must reindex; the flow must detect and not silently strand them on a dimension mismatch (the #1061 E1 / RDR-109 hazard).

## Alternatives Considered

- **Blunt packaging default flip to `[local]`** (the first instinct): rejected as primary — it cannot express the choice, imposes the model download universally (incl. cloud users), and breaks instant-first-run. Retained only as sub-option (ii) if zero-config-768-for-all is explicitly wanted.
- **Do nothing / docs-only** (the 5.6.x doc fix): necessary but insufficient — it tells users the choice exists but still leaves the default weak and unguided.
- **Bundle bge like the ONNX MiniLM** (ship the model in the wheel): wheel-size/licensing-prohibitive; fastembed's fetch-on-first-use is the standard pattern.

## Critical Assumptions

- **CA-1**: `nx init` can pre-fetch the bge model with visible progress and degrade gracefully offline (no wedged first `search`).
- **CA-2**: `nx` can add the `[local]` extra to its own install at runtime safely (extras-preserving, editable-install-aware) via the `reinstall-tool.sh` mechanism.
- **CA-3**: The guided flow can detect an existing 384-dim corpus and offer reindex rather than silently dimension-mismatching (ties to RDR-109 / #1061 E1).
- **CA-4**: "768 default for local" is achievable via guided provisioning (Shape A + (i)) WITHOUT bloating cloud-only installs or imposing a universal model download — i.e. the maintainer's intent does not actually require the packaging default (ii).

## Finalization Gate

_Pending. Run `/conexus:rdr-gate` after research verifies CA-1..CA-4._

## References

- This session's discussion: delivery-vs-onboarding distinction; "768 should be the default, for local"; the model-download tradeoff.
- RDR-076 (idempotent upgrade), RDR-038 (local T3 backend), RDR-109 (local-mode embedder naming / dimension handling), RDR-126 (Desktop MCPB one-click), RDR-143 (plugin↔CLI lockstep — embedder-default pulled out of it into here).
- Surfaces: `nx config init`, `src/nexus/mcp/_first_run.py`, `nx doctor`, `scripts/reinstall-tool.sh`, `conexus/hooks/scripts/preflight.py`, `NX_LOCAL_EMBED_MODEL` (config).
- 5.6.x doc fix (PR #1071) surfacing `[local]` + extras-preserving upgrade (the necessary-but-insufficient stopgap this RDR supersedes for the default decision).

## Revision History

- 2026-06-01: Draft. Originated from the realization that `uv tool install` is delivery, not onboarding, and a blunt packaging default cannot express the 384-vs-768 embedder choice. Pulls the "768 default for local" decision out of RDR-143 into this guided-provisioning RDR. Leaning Shape A (guided `nx init`) + sub-option (i) (guided default), with the packaging default (ii) retained only if zero-config-768-for-all is explicitly chosen. Direction to be locked after research.
