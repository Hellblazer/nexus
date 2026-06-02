---
title: "Guided Install / Onboarding & Local-Embedder Provisioning (nx init): Make the 384-vs-768 Embedder an Explicit Guided Choice, Not a Blunt Packaging Default"
id: RDR-144
type: Architecture
status: closed
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-01
accepted_date: 2026-06-01
closed_date: 2026-06-02
related_issues: [nexus-qwl7o]
related_rdrs: [RDR-076, RDR-126, RDR-143, RDR-038, RDR-109]
supersedes: []
related_tests: []
implementation_notes: "Shipped in conexus 5.7.0 (P1-P6 + phase-review gate, all beads closed). Post-mortem: docs/rdr/post-mortem/144-guided-onboarding-embedder-provisioning.md"
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

_Verified 2026-06-01 (codebase-deep-analyzer; T2 `nexus_rdr/144-research-CA1-CA4`). All four CAs hold; Shape A + sub-(i) is confirmed feasible with no blocker forcing the packaging default. Three refinements materially sharpen the implementation contract:_

1. **Model pre-fetch (CA-1).** Tier-1 bge is fetched lazily on first `__call__` via `fastembed.TextEmbedding` (`local_ef.py:141`); `nx init` pre-fetches by calling `_init_ef()` / a warmup embed (no dedicated download-only API). Cache dir = `FASTEMBED_CACHE_PATH` else **`$TMPDIR/fastembed_cache`** (`fastembed/common/utils.py:48-58`). **Refinement A — the `$TMPDIR` default is fragile** (wiped on reboot → model re-downloads); `nx init` must set a **stable** `FASTEMBED_CACHE_PATH` (e.g. under `~/.local/share/nexus/`). **Refinement B — offline failure is NOT a clean exception**: with `HF_HUB_OFFLINE` + cache-miss, fastembed `logger.error`s and returns `None` → `None`-deref crash; `nx init` must wrap the warmup in `except Exception` and emit an actionable message (point at the cache path). MinerU's download notice (`commands/mineru.py:243`) is the UX precedent (message-only).
2. **Runtime extra-add, editable-safe (CA-2).** `scripts/reinstall-tool.sh` (`uv tool install --reinstall --from "SOURCE[extras]"` reading `$(uv tool dir)/conexus/uv-receipt.toml`) is the correct shell-out for uv-tool installs. **Editable/dev detection = absence of `uv-receipt.toml`** (a `uv sync` dev tree has none) → skip auto-reinstall, instruct manual (`pip install "conexus[local]"`). Receipt present + a `directory` key = local-source install, safe to reinstall. The clobber-a-dev-tree hazard is ruled out by receipt absence.
3. **Existing-corpus detection + reindex (CA-3).** `nx doctor` / `health._check_t3_local` (`health.py:258-309`, the #1061 E1 check) already does a dummy-vector **dimension probe** and emits the 384→768 fix hint (`:289-295`); it is dimension-based so it covers local→local 384→768, and `nx init` can reuse it. **Critical refinement — "automatic re-indexing on model change" is misleading**: `doc_indexer.py:637-641` re-embeds into **new** collection names on a model change but does **not** delete/merge the **old 384-dim collections**, which remain as orphan dead weight that `nx search` silently returns zero from. `nx init` MUST explicitly offer old-collection cleanup/reindex — it cannot rely on "automatic."
4. **No cloud bloat (CA-4, clean).** `fastembed` is optional-only (`pyproject.toml:103` `local = ["fastembed>=0.7.0"]`), not in base deps; cloud installs carry zero fastembed (cloud uses `voyageai`). So "768 default for local, lean for cloud" works purely via guided `nx init` conditionally adding `[local]` (Shape A + sub-i) — **the packaging default (sub-ii) is unnecessary**. `NX_LOCAL_EMBED_MODEL` (`t3_client.py:102-104`) is an optional override `nx init` may write.

**Strongest implementation risks:** the offline/`None`-deref crash (CA-1 Refinement B) and the orphan-old-collections gap (CA-3 — must actively clean up; the docs lie).

## Proposed Solution

_Draft — lock after research. Spectrum from lightest to most capable; the embedder-default decision rides whichever mechanism is chosen._

- **Shape A — Guided `nx init` verb (recommended starting point).** A first-run/`nx init` (or broadened `nx config init`) that: detects cloud vs local → for local, **recommends bge-768** and states the one-time model-download cost, offering the light 384 as the explicit alternative → if 768, ensures the extra is present (shell `uv tool install --reinstall "conexus[local]"`) and **pre-fetches the model with a progress bar** → records the choice → for an existing 384-corpus user, detects and offers reindex. Reuses every surface above; no packaging change; the choice is explicit and informed. Can be invoked by the plugin's first-run and surfaced by `nx doctor`.
- **Shape B — `curl | sh` bootstrap.** Installs `uv` + `conexus[choice]` + runs `nx init`. The preflight hook already half-does this. A friendlier zero-to-running path; larger surface.
- **Shape C — MCPB / one-click** for the Claude Desktop / plugin path — RDR-126 territory; cross-reference, do not duplicate.
- **Embedder-default sub-decision (orthogonal to A/B/C):**
  - **(i) Guided default** — `nx init` defaults the *recommendation* to bge-768 for local and provisions it; bare `uv tool install conexus` still ships MiniLM until the user runs init. Pairs with Shape A; no packaging bloat for cloud users.
  - **(ii) Packaging default** — promote `fastembed`+bge into base deps (like mineru was promoted) so `uv tool install conexus` *is* bge-768. True zero-config 768, but heavier install for everyone + first-use model download for all. Rejected-leaning unless we accept the universal weight.

**LOCKED (research-confirmed): Shape A + sub-(i)** — "768 by default for local, presented as an informed choice via `nx init`, model fetched visibly." Research (CA-4) confirmed this delivers the maintainer's intent (768 default for local) without imposing bge weight on cloud-only installs or silently downloading a model, and that the packaging default (sub-ii) is **unnecessary** — so (ii) is rejected (kept only as a documented fallback if zero-config-768-for-all is ever explicitly wanted). Shapes B/C remain future options layered on the same `nx init` core.

## Implementation Plan

_Shape A + sub-(i) locked; phase-detail at accept. Must include (research-derived):_

- **`nx init` guided flow** (or broadened `nx config init`): detect cloud vs local → for local, recommend bge-768 with the one-time model-download cost stated, offer light-384 → record choice (config; optionally `NX_LOCAL_EMBED_MODEL`).
- **Extra-add, editable-safe (CA-2):** gate on `$(uv tool dir)/conexus/uv-receipt.toml` — present → shell `reinstall-tool.sh`-style `uv tool install --reinstall "conexus[local]"`; **absent (uv-sync dev tree) → do NOT auto-reinstall**, print the manual `pip install "conexus[local]"` instruction.
- **Model pre-fetch (CA-1):** warmup-embed to fetch bge, with a **stable `FASTEMBED_CACHE_PATH`** under `~/.local/share/nexus/` (NOT the fragile `$TMPDIR` default), and a `try/except Exception` around the warmup that converts an offline/cache-miss failure into an actionable message (never a `None`-deref crash or a wedged first `search`).
- **Existing-384 handling (CA-3):** reuse `health._check_t3_local`'s dimension probe to detect 384-dim collections under an active 768 embedder; **explicitly offer cleanup/reindex of the OLD collections** — do not rely on the "automatic re-index" (it creates new collections and orphans the old ones, which then silently return zero).
- **Surfacing:** plugin first-run / `_first_run.py` invokes or points at `nx init`; `nx doctor` flags "ran with default 384, run `nx init` for bge-768."
- **Docs:** present the embedder choice at onboarding (supersede the 5.6.x doc stopgap's secondary "or" framing).
- **Tests:** fresh-local → bge provisioned + warmup fetch (mocked); cloud → no fastembed touched; existing-384 → cleanup/reindex offered (not silent orphan); offline → graceful message, not crash/hang; editable tree → manual instruction, no reinstall.

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

_Verified 2026-06-01 (codebase-deep-analyzer)._

- **CA-1 — VERIFIED (with refinements)**: `nx init` can pre-fetch bge via `_init_ef()`/warmup (`local_ef.py:141`); fastembed shows progress. Refinements folded into the plan: set a stable `FASTEMBED_CACHE_PATH` (the `$TMPDIR` default is wiped on reboot) and wrap the warmup in `except Exception` (offline cache-miss is a `logger.error`+`None`-deref, not a clean exception).
- **CA-2 — VERIFIED (with refinement)**: runtime extra-add via the `reinstall-tool.sh` mechanism is safe; editable/dev installs are reliably excluded by the **absence of `$(uv tool dir)/conexus/uv-receipt.toml`** (uv-sync trees have none → instruct manual, don't auto-reinstall).
- **CA-3 — VERIFIED (with critical refinement)**: `health._check_t3_local` (`:258-309`) already detects the 384-under-768 dimension mismatch and is dimension-based (covers local→local). BUT the "automatic re-index" is misleading — it orphans the old 384 collections; `nx init` must **explicitly** offer cleanup/reindex.
- **CA-4 — VERIFIED (clean)**: `fastembed` is optional-only (`pyproject.toml:103`); cloud installs carry none. "768 default for local, lean for cloud" is achievable via guided `nx init` (Shape A + sub-i) with **no packaging change** — the packaging default (ii) is unnecessary.

## Finalization Gate

_Pending. Run `/conexus:rdr-gate` — research (CA-1..CA-4) complete and verified._

## References

- This session's discussion: delivery-vs-onboarding distinction; "768 should be the default, for local"; the model-download tradeoff.
- RDR-076 (idempotent upgrade), RDR-038 (local T3 backend), RDR-109 (local-mode embedder naming / dimension handling), RDR-126 (Desktop MCPB one-click), RDR-143 (plugin↔CLI lockstep — embedder-default pulled out of it into here).
- Surfaces: `nx config init`, `src/nexus/mcp/_first_run.py`, `nx doctor`, `scripts/reinstall-tool.sh`, `conexus/hooks/scripts/preflight.py`, `NX_LOCAL_EMBED_MODEL` (config).
- 5.6.x doc fix (PR #1071) surfacing `[local]` + extras-preserving upgrade (the necessary-but-insufficient stopgap this RDR supersedes for the default decision).

## Revision History

- 2026-06-01: Draft. Originated from the realization that `uv tool install` is delivery, not onboarding, and a blunt packaging default cannot express the 384-vs-768 embedder choice. Pulls the "768 default for local" decision out of RDR-143 into this guided-provisioning RDR.
- 2026-06-01: Research (CA-1..CA-4 verified, codebase-deep-analyzer). **Shape A + sub-(i) locked**; packaging default (ii) rejected as unnecessary (CA-4). Three refinements folded into the plan: stable `FASTEMBED_CACHE_PATH` + offline-wrap (CA-1), uv-receipt-presence as the editable-install gate (CA-2), and explicit old-collection cleanup because "automatic re-index" orphans the 384 collections (CA-3). Ready for gate.
