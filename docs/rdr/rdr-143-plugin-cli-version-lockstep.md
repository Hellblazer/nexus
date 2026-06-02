---
title: "Plugin↔CLI Version Lockstep: a SessionStart Version-Marker Hook That Keeps the nx CLI in Sync With the conexus Plugin"
id: RDR-143
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-01
related_issues: [nexus-bc0x7]
related_rdrs: [RDR-076, RDR-125, RDR-141, RDR-142]
supersedes: []
related_tests: []
implementation_notes: ""
---

# RDR-143: Plugin↔CLI Version Lockstep: a SessionStart Version-Marker Hook That Keeps the nx CLI in Sync With the conexus Plugin

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

#### Gap 1: the conexus plugin and the `nx` CLI are versioned together but installed and updated through independent channels, so they silently drift out of lockstep

The conexus plugin and the `nx` CLI ship the **same version number** by construction (CI enforces parity across `pyproject.toml`, the four plugin manifests, and both `source.ref` fields — every release stamps one version everywhere). But they are **delivered through two unrelated channels**:

- The **plugin** updates when a new immutable tag is cut and the user runs `/plugin update` (or Claude Code auto-updates the marketplace-pinned source).
- The **`nx` CLI** is a separate PyPI package (`uv tool install conexus`) that the user must upgrade manually (`scripts/reinstall-tool.sh`, `nx upgrade`, or `uv tool install --upgrade`).

Nothing keeps the two in step. A user who updates the plugin but not the CLI (or vice versa) runs a plugin whose commands/hooks/skills assume CLI capabilities the installed `nx` does not have — or, worse, a CLI whose schema/daemon expectations differ from what the plugin drives. This session alone shipped **three P0/P1 fixes whose root was version skew** between separately-updated components (RDR-141 daemon-vs-client schema skew; RDR-142 migration-state skew; the 5.6.2 local-mode break surfaced during a wide 4.28→5.6.0 jump). The plugin↔CLI seam is the one skew axis with **no** reconciling mechanism today.

The user's proposal: ship a plugin hook that, on session launch, updates the `nx` CLI to match the plugin, then removes itself after firing once. The intent (lockstep) is right; the specific mechanism needs correction (see §Context) and the *action* the hook takes carries a severe footgun (see §Trade-offs).

## Context

**Hook-capability findings (verified against current Claude Code docs, 2026-06-01, via claude-code-guide):**

- **No self-removing plugin hook exists.** The `once: true` field is documented **only** for *skill/agent* hook frontmatter, not for a plugin's `hooks/hooks.json`. Plugin-shipped hooks fire on every matching event; there is no sanctioned way for one to deregister itself, and no documented post-install / post-update plugin hook.
- **"Remove after first firing" is the wrong semantics anyway.** That is *once-ever*; the plugin updates on **every** release, so lockstep needs *once-per-plugin-version* — fire on the 5.6.2→5.6.3 update, and again on 5.6.3→5.6.4. A self-deleting hook would fire once and never again.
- **The idiomatic supported pattern is `SessionStart` (matcher `startup`) + a version-marker file.** The hook reads the plugin's version, compares to a marker it wrote last time, acts only when they differ, then rewrites the marker. This is once-per-version, idempotent, and re-fires on each plugin update — exactly the desired trigger.
- **`SessionStart`/`startup` is synchronous** — it blocks session startup until the command returns (default command-hook timeout 10 min, overridable via `timeout`). It can emit `additionalContext` (text injected into the session). This makes a long blocking action on the hot path unacceptable.

**Docs already point at the footgun.** The current install/upgrade documentation (README / docs) recommends the **raw `uv tool install conexus`** — the very command that drops the `[local]` extra (and the mineru console scripts), so a user who follows the docs lands a `[local]`-less install → 384-dim fallback → the 5.6.2 local-mode-search break. Correcting the documented install/upgrade path to the extras-preserving command (`scripts/reinstall-tool.sh` / `nx upgrade` / `uv tool install "conexus[local]"`) is **in scope** for this RDR: the docs are a version/extras sync surface as much as the hook is, and they are wrong *today* regardless of the hook design.

**nexus context:** the plugin already knows its version (manifests == `pyproject`), so the hook's sync target is simply `conexus == <plugin-version>`. Existing machinery to reuse: `nx upgrade` (sanctioned in-tool upgrade that runs migrations), `nx doctor` (already reports version drift), `scripts/reinstall-tool.sh` (the one path that **preserves `[local]` and other extras**), `src/nexus/mcp/_first_run.py` (already runs at MCP startup to ensure the daemon), RDR-125 (each plugin ships its own hook rules).

## Research Findings

_To be populated via `/conexus:rdr-research`. Key questions:_

1. **Editable/dev-install detection.** How does the hook reliably tell an editable/source install (`uv sync` from this repo) from a PyPI tool install, so it never clobbers a developer's working tree? (`importlib.metadata` direct-url / `uv tool list` / the `.dist-info` `direct_url.json`?)
2. **Extras preservation.** Confirm precisely which install command preserves `[local]` + the mineru console-script symlinks (`scripts/reinstall-tool.sh` does; a bare `uv tool install conexus==X` does NOT). Can `nx upgrade` be the sanctioned action, and does it preserve extras + run migrations?
3. **Where does the marker live and what keys it?** Plugin dir is pinned/read-only-ish; the marker likely belongs in `~/.config/nexus/` keyed on `(plugin_version, installed_cli_version)`. Confirm the hook can read the plugin's own version at runtime (env var / manifest path).
4. **Cost/latency of the chosen action** on the synchronous startup path, and the fast-fail/async story (can we detect-and-defer rather than block?).
5. **Does Claude Code re-read plugin hook config mid-session**, and is `additionalContext` the right surface for a nudge?

## Proposed Solution

_Draft — three candidate shapes; lock one after research. All use the `SessionStart`/`startup` + version-marker trigger (once-per-plugin-version)._

- **Shape A — Detect + nudge (lowest risk).** On skew (`plugin_version != nx --version`), the hook emits `additionalContext` / prints a one-line, actionable nudge ("nx CLI 5.6.1 ≠ plugin 5.6.2 — run `nx upgrade`") and writes the marker so it nudges once per version. No mutation of the user's environment; no startup blocking; no consent problem. Gives up full automation.
- **Shape B — Detect + run `nx upgrade` (automated, safe action).** On skew, the hook invokes **`nx upgrade`** — the sanctioned in-tool path that preserves extras and runs migrations — NOT a raw `uv tool install` (which strips `[local]`). Must be fast-fail / non-blocking (background or bounded timeout) so startup is never wedged, and must skip editable installs. Higher automation; the risk is doing work on the startup path.
- **Shape C — Auto `uv tool install conexus==<v>` (REJECTED, stated for the record).** Strips `[local]`/mineru extras → 384-dim fallback → reintroduces the exact local-mode-search P0 that 5.6.2 just fixed; blocks startup on a 30–60s network install; clobbers dev installs. Rejected.

Leaning: **Shape A** (or A-with-an-opt-in-to-B), because it delivers ~90% of the lockstep value (the user learns of skew immediately, with the exact remediation) at near-zero risk, and because auto-mutating a global tool install on session start is an outward action that should be opt-in. Research + gate to confirm.

## Implementation Plan

_To be detailed after the shape is locked. Must include:_

- **Doc correction (do first, independent of the hook).** Replace every documented `uv tool install conexus` recommendation (README, `docs/` install/upgrade sections, any quickstart) with the extras-preserving form (`uv tool install "conexus[local]"` for a fresh install, `scripts/reinstall-tool.sh` / `nx upgrade` for upgrades). This is a live footgun shippable on its own ahead of the hook.
- The `SessionStart`/`startup` hook in the plugin's `hooks/hooks.json`; the version-marker read/compare/write (keyed on plugin version, marker under `~/.config/nexus/`); editable-install detection (skip).
- For any automating shape, the action routed through `nx upgrade` (NEVER raw `uv tool install`) with fast-fail/non-blocking semantics.
- A test that the hook is a no-op when versions match, fires exactly once per version, and never strips extras / never blocks startup beyond a bound.

## Trade-offs

- **Extras-stripping is the dominant hazard.** Any automating shape MUST route through `nx upgrade` / extras-preserving install; a naive `uv tool install conexus==X` drops `[local]` and re-creates the 5.6.2 P0. This is a locked requirement, not a preference.
- **Startup is the hot path.** `SessionStart`/`startup` blocks; the action must be a nudge or a bounded/async operation, never a synchronous network install.
- **Consent / surprise.** Mutating a user's global `nx` install without asking is outward-facing; Shape A avoids it entirely, Shape B should be opt-in or at least loud + reversible.
- **Dev installs.** Editable/source installs must be detected and skipped or the hook clobbers a developer's tree.
- **Partial value of A.** Shape A does not *fix* skew automatically; it surfaces it. Given the asymmetry of harms, that is likely the right trade.

## Alternatives Considered

- **Self-removing / `once` plugin hook** (the original proposal): not supported for plugin hooks, and once-ever is the wrong granularity. Rejected on capability + semantics.
- **Bundle the CLI inside the plugin** (no separate PyPI install): out of scope here; a much larger packaging change and contrary to the current `uv tool install conexus` distribution model.
- **MCP `_first_run.py` does the version check** instead of a hook: viable variant — it already runs at MCP startup; consider whether the check belongs there rather than in a SessionStart hook (research question).

## Critical Assumptions

- **CA-1**: A `SessionStart`/`startup` plugin hook can read the plugin's own version at runtime and write/read a marker under `~/.config/nexus/` — i.e. the once-per-version trigger is implementable with documented hook I/O.
- **CA-2**: `nx upgrade` (or an equivalent in-tool path) upgrades the CLI **while preserving `[local]`/mineru extras and running migrations** — so an automating shape has a safe action that does not reintroduce the 5.6.2 extras-stripping P0.
- **CA-3**: Editable/source installs are reliably distinguishable from PyPI tool installs at hook runtime, so a developer's working tree is never clobbered.
- **CA-4**: The chosen action can run without blocking session startup beyond an acceptable bound (nudge = instant; any install routed async or fast-fail).

## Finalization Gate

_Pending. Run `/conexus:rdr-gate` after research verifies CA-1..CA-4._

## References

- claude-code-guide hook-capability research (2026-06-01): no self-removing plugin hook; `once` is skill/agent-frontmatter only; `SessionStart`/`startup` is synchronous + supports `additionalContext`. Docs: code.claude.com/docs/en/hooks.md, hooks-guide.md, plugins.md, skills.md.
- RDR-076 (idempotent upgrade), RDR-125 (each plugin ships its own hook rules), RDR-141 / RDR-142 (this session's version-skew P0s motivating lockstep).
- `scripts/reinstall-tool.sh` (extras-preserving install), `nx upgrade`, `nx doctor` (drift report), `src/nexus/mcp/_first_run.py` (MCP-startup ensure path), `.claude-plugin/marketplace.json` (pinned-source release model).
- T2: `nexus/project_release_5_6_2` (the version-skew arc).

## Revision History

- 2026-06-01: Draft. Originated from a user proposal for a self-removing SessionStart hook to update the CLI in lockstep with the plugin. Capability research corrected the mechanism (no self-removing plugin hook; use SessionStart + version-marker, once-per-version) and surfaced the extras-stripping hazard (a naive auto-`uv tool install` would reintroduce the 5.6.2 local-mode P0). Three shapes drafted (A nudge / B auto-`nx upgrade` / C raw-install rejected); leaning A. Direction to be locked after research.
