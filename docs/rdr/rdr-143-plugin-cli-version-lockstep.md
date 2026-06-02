---
title: "Plugin↔CLI Version Lockstep: a SessionStart Version-Marker Hook That Keeps the nx CLI in Sync With the conexus Plugin"
id: RDR-143
type: Architecture
status: accepted
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-01
accepted_date: 2026-06-02
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
- **RDR-076** established the idempotent **migration** mechanism (`nx upgrade`) and the `_nexus_version` gate. Note (CA-2): `nx upgrade` runs migrations only; it does **not** upgrade the binary. The extras-preserving **binary** upgrade is `uv tool upgrade conexus`. Any lockstep automation routes the binary upgrade through `uv tool upgrade conexus` (never raw `uv tool install`) and the migration step through `nx upgrade`.
- **RDR-141 / RDR-142** are the concrete failures this lockstep is meant to prevent: they are downstream symptoms of allowing the plugin and CLI to disagree on version.

### Capability finding (claude-code-guide, current docs)

- **There is NO self-removing plugin hook.** `once: true` is a skill / agent-frontmatter affordance only, it is **not** available for plugin `hooks` / `hooks.json`. And once-ever is the **wrong semantics** anyway: we want **once-per-version**, not once-ever, because the hook must re-fire after each plugin update.
- **The idiomatic supported pattern is `SessionStart` (matcher `startup`) + a version-marker file.** The hook reads a marker file recording the last version it acted on; it acts only when the current plugin version differs, then rewrites the marker. This is **idempotent**, fires **once per plugin version**, and **re-fires on each update**, exactly the desired semantics.
- **`SessionStart` / `startup` is SYNCHRONOUS** (it blocks session startup, with a 10-minute default timeout) and **can emit `additionalContext`** back into the session. The synchronous-blocking property is the central constraint on any action shape (see CA-4): a long or hanging upgrade would wedge startup.

## Dominant Hazard

A naive auto `uv tool install conexus==X` **STRIPS the `[local]` extra** (and the mineru scripts) → the install falls back to the **384-dim** ONNX MiniLM embedder → which **reintroduces the exact 5.6.2 local-mode-search P0** on an install that was previously a healthy 768-dim `[local]` install.

**Locked requirement (restated per CA-2):** any automating action MUST perform the **binary** upgrade via `uv tool upgrade conexus` (an **extras-preserving** verb) and the **migration** step via `nx upgrade`, and **NEVER** a raw `uv tool install`. This is the hard constraint that rejects Shape C and shapes the safe action in Shape B.

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

_Three shapes, lightest to heaviest. **Shape B is locked** (see Decision); Shape A is folded into B as the in-session signal; Shape C is rejected._

- **Shape A: detect + nudge via `additionalContext` (RETAINED as B's in-session signal, not a standalone alternative).** The `SessionStart` hook compares the plugin version against the version-marker file; on a mismatch, it emits an `additionalContext` message telling the user the CLI is out of lockstep and that the upgrade is in flight. No startup-blocking work. In the locked design this nudge fires alongside Shape B's detached action, so the user is both informed and fixed in the same session. (As a standalone shape it was rejected: detect-only leaves the nudge-ignoring 10% unprotected, which is the RDR-141/142 population.)
- **Shape B: detect + run the two-command safe action (automated).** On a version mismatch, the hook runs **`uv tool upgrade conexus`** (extras-preserving binary upgrade) followed by **`nx upgrade`** (migrations, idempotent, RDR-076), routed through a **detached subprocess** (`nohup ... &`, then `exit 0`) so it cannot wedge synchronous startup (CA-4), and **skipping editable / dev installs** via the uv-receipt gate (CA-3) so it never clobbers a `uv sync` dev tree. Automates the fix while honoring the dominant-hazard constraint (never a raw `uv tool install`). **Limitation (CA-4):** because the upgrade is detached, it completes *after* the current session has already started against the old binary, so Shape B achieves **next-session** lockstep, not within-session lockstep. A foreground action that would achieve within-session lockstep is rejected because it wedges synchronous startup.
- **Shape C: auto raw `uv tool install conexus==X` (REJECTED).** Rejected on three independent grounds: it **strips the `[local]` extra** (the dominant hazard → reintroduces the 5.6.2 P0), it **blocks synchronous startup** on a network-bound install, and it **clobbers dev / editable installs**.

## Decision

**Shape B (detect + automated two-command safe action) is LOCKED.** Decided 2026-06-02 by the author with concurrence from the nexus user base. Rationale: the version-skew failures this RDR exists to prevent (RDR-141, RDR-142, the 5.6.2 P0) occurred under complete silence and, crucially, in the population that would *ignore* a Shape A nudge. Detect-only (Shape A) leaves exactly that 10% unprotected, which is the 10% that caused the incidents. The CAs that gated this choice are all clear: CA-4 establishes the action can be made non-blocking via a detached subprocess; CA-3 supplies the editable-tree gate so dev checkouts are never clobbered; CA-2 names the correct two-command action (`uv tool upgrade conexus` for the binary, `nx upgrade` for migrations).

**Accepted limitation (CA-4):** because the upgrade is detached, the new CLI takes effect on the **next** session, not the current one. Shape B delivers next-session lockstep. A foreground action that would close within-session is rejected (it wedges synchronous `SessionStart`). This is acceptable: the skew window shrinks from "indefinite, silent" to "one session, with the fix already in flight."

**Shape A is retained as the in-session signal, not an alternative.** On a detected mismatch the hook still emits `additionalContext` (the user is told skew exists and that the CLI is upgrading) AND fires the detached upgrade. So the user gets both the nudge and the action in the same session.

## Implementation Plan

_Shape B locked (see Decision). CA-1..CA-4 verified._

- **Doc-footgun correction (do first, shippable ahead of the hook):** replace the raw `uv tool install conexus` recommendations at the file:line sites listed above with the extras-preserving command, once CA-2 settles which verb preserves `[local]` + migrations. Research-then-fix; not a blind find-replace.
- **Version-marker hook (Shape A or B):** a `SessionStart` (matcher `startup`) hook that reads/writes a version-marker file, acts once-per-version, and either emits `additionalContext` (A) or runs the two-command upgrade (B). **Open at planning:** specify marker-write timing precisely. For Shape A, rewriting the marker immediately on nudge silences future nudges for that version (one warning then quiet); deferring the rewrite until a confirmed upgrade keeps nudging. Pick deliberately.
- **Action safety (if Shape B):** detached subprocess so synchronous startup is never wedged; skip editable/dev installs via the uv-receipt gate; perform the binary upgrade via `uv tool upgrade conexus` and migrations via `nx upgrade`, never raw `uv tool install`.

## Critical Assumptions

_Unverified, draft. Next step: `/conexus:rdr-research` CA-1..CA-4, then `/conexus:rdr-gate`._

- **CA-1: the version-marker hook is implementable with documented I/O.** A `SessionStart`/`startup` hook can read a marker file, compare it to the current plugin version, emit `additionalContext`, and rewrite the marker, all within the documented hook contract (synchronous, `additionalContext`-emitting, 10-min timeout).
- **CA-2: `nx upgrade` preserves `[local]` + migrations.** `nx upgrade` (RDR-076) is genuinely extras-preserving and migration-safe, and we can name **which** upgrade verb preserves the extras (`uv tool upgrade` vs `scripts/reinstall-tool.sh` vs `nx upgrade`). This also determines the correct doc-footgun replacement command.
- **CA-3: editable-install detection.** The hook/action can reliably detect an editable / dev (`uv sync`) install and **skip** the auto-action, so it never clobbers a development tree.
- **CA-4: non-blocking action.** The action (especially Shape B's `nx upgrade`) can be made fast-fail / non-blocking so it does not wedge the synchronous `SessionStart`/`startup` path within its timeout.

## Research Findings

_Verified 2026-06-02 (claude-code-guide for CA-1/CA-4; codebase-deep-analyzer for CA-2/CA-3). T2: `nexus_rdr/143-research-CA1-CA4`._

- **CA-1 VERIFIED.** A `SessionStart` (matcher `startup`) hook can read a marker file, compare it to the plugin version, emit `additionalContext`, and rewrite the marker, all within the documented contract. The stdout injection contract is `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "<msg>"}}`. The plugin version is readable at runtime from `${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json`. Confirmed: no self-removing plugin hook (`once: true` is skill/agent-frontmatter only, ignored in `hooks.json`); the version-marker-in-script is the supported once-per-version idiom.
- **CA-4 VERIFIED (with nuance).** `SessionStart`/`startup` is synchronous and blocks startup; default timeout 600s, configurable per-hook. A foreground network-bound command wedges startup until completion or timeout. `async: true` is not honored on `SessionStart`. Fire-and-forget is achievable by detaching a subprocess (`nohup ... &`) and returning `exit 0` immediately. So a Shape B auto-action is only safe if detached.
- **CA-2 PARTIAL, material correction.** `nx upgrade` (`src/nexus/commands/upgrade.py`) is migration-only; it never invokes `uv` and never touches the binary (only `nx daemon t2 stop` at :225 and `ensure-running` at :258). The verb that preserves the `[local]` extra on a binary upgrade is **`uv tool upgrade conexus`** (and `scripts/reinstall-tool.sh` via explicit `uv-receipt.toml` extras inspection at :17-43). `nx upgrade` is migration-safe and idempotent (`cli_version` gate at `migrations.py:2406-2499`, downgrade protection, `0.0.0` pre-release guard, `t2_migration_flock` serialization). Migrations also auto-run on daemon-startup bootstrap. The consequence: Shape B's safe action is **two steps**, `uv tool upgrade conexus` (binary, extras-preserving) then `nx upgrade` (migrations), not a single `nx upgrade`. This confirms the already-shipped doc-footgun fix (`uv tool upgrade conexus`).
- **CA-3 VERIFIED.** Editable/dev-tree detection is production-proven: `_uv_receipt_path()` at `src/nexus/commands/init.py:52-68` runs `uv tool dir` and checks for `<dir>/conexus/uv-receipt.toml`, returning `None` (dev/editable or no-uv, so skip) or a `Path` (safe tool-install). It already gates `_ensure_local_extra()` (`init.py:71-109`). It is a private name in `init.py` only; Shape B should extract a shared helper or re-implement the 3-line check inline. All edge cases fail safe to skip.

**Decision impact:** all four CAs clear. Both shapes are implementable. Shape B is feasible but requires (1) a detached subprocess (CA-4), (2) the uv-receipt editable gate (CA-3), and (3) a two-command action (CA-2 correction). The CA-2 correction restates the dominant-hazard phrasing as: binary upgrade via `uv tool upgrade conexus` (never raw `uv tool install`); migrations via `nx upgrade`. Shape A (nudge) is unaffected by the CA-2 correction and remains lowest-risk for ~90% of the value.

## Finalization Gate

_Pending. CA-1..CA-4 verified above. Next: `/conexus:rdr-gate`._

## References

- This session's discussion: plugin-vs-CLI independent update channels; the self-removing-hook proposal; the `uv tool install` extras-strip hazard.
- RDR-076 (idempotent, extras-preserving `nx upgrade` + `_nexus_version` gate), RDR-125, RDR-141 (T2 version-skew double-writer, a downstream symptom of plugin↔CLI skew), RDR-142 (migration completeness vs the version row, another downstream symptom).
- RDR-144 (guided onboarding / local-embedder provisioning, the 384-vs-768 embedder-default decision was pulled out of this RDR into there).
- Capability source: claude-code-guide (current docs), `SessionStart`/`startup` synchronous + `additionalContext`; no self-removing plugin hook; version-marker file as the once-per-version idiom.
- Doc-footgun sites: `README:50`, `docs/getting-started.md:21/254/260/271`, `docs/configuration.md:16`, `docs/cli-reference.md:1407`, `docs/integrations/devonthink-smart-rules.md:69`, `conexus/hooks/scripts/preflight.py:90-92`.

## Revision History

- 2026-06-02: **Shape B LOCKED** by author with nexus user-base concurrence. Detect-only (Shape A) leaves the nudge-ignoring 10% unprotected, which is the population that caused RDR-141/142. Shape A retained as the in-session signal alongside the detached action (user gets nudge + upgrade in the same session); accepted next-session-lockstep limitation per CA-4.
- 2026-06-02: CA-1..CA-4 verified (Research Findings section added). Material CA-2 correction propagated through Context, Dominant Hazard, Proposed Solution, and Implementation Plan: `nx upgrade` is migration-only and does not touch the binary, so the safe action is two commands, `uv tool upgrade conexus` (extras-preserving binary upgrade) then `nx upgrade` (migrations). Shape B now records its detached-subprocess requirement (CA-4) and the resulting next-session-only lockstep limitation, the uv-receipt editable gate (CA-3), and an open marker-write-timing question for planning. Gate run: PASSED (0 Critical, 2 Significant, both resolved in-place).
- 2026-06-01: Draft. Originated from the realization that the plugin and nx CLI update through independent channels with nothing keeping them in lockstep, the root of this session's RDR-141 / RDR-142 / 5.6.2 P0s. Capability finding: no self-removing plugin hook; `SessionStart`/`startup` + version-marker file is the once-per-version idiom. Dominant hazard: raw `uv tool install` strips `[local]` and reintroduces the 5.6.2 search P0, so any action must route through `nx upgrade`. Shapes A (nudge, leaning) / B (`nx upgrade`) / C (rejected). Doc-footgun correction in-scope and "do first." CA-1..CA-4 pending research. Embedder-default decision pulled out into RDR-144.
