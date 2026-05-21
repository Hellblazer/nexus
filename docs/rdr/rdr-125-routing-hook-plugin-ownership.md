---
title: "Routing-Hook Plugin Ownership: Each Plugin Ships Its Own Rules"
id: RDR-125
type: Architecture
status: accepted
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-20
accepted_date: 2026-05-20
related_issues: []
related_rdrs: [RDR-121, RDR-120]
related_tests: []
implementation_notes: ""
---

# RDR-125: Routing-Hook Plugin Ownership: Each Plugin Ships Its Own Rules

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

RDR-121 (shipped in conexus 4.33.0) introduced a PreToolUse routing-hook
framework under `nx/hooks/scripts/routing/` plus three rules:
`grep_for_symbols_redirects_to_serena`, `git_add_all_redirects_to_explicit_paths`,
`phase_review_close_requires_gate`. All three live in the nx plugin.

This is correct for two of the three rules but architecturally wrong for
the third. `grep_for_symbols_redirects_to_serena` redirects users to
Serena MCP tools (`mcp__plugin_sn_serena__jet_brains_find_symbol`,
`jet_brains_find_referencing_symbols`, `jet_brains_get_symbols_overview`).
Those tools ship in the **sn** plugin, not nx. A user who installs nx
without sn gets a hook that denies their `grep` and redirects them to
tools they do not have.

The escape token (`# routing-allow: <reason>`) lets them bypass on a
per-call basis, but the default behavior is user-hostile.

The deeper problem: the nx plugin is making routing decisions on behalf
of tools it does not own. This violates the substrate-vs-consumer
boundary that RDR-120 § A8 articulated for storage and tests: substrate
provides shape; consumers own content. The same principle applies to
routing rules: the plugin that ships a tool owns the rule that routes
to it.

### Enumerated gaps to close

#### Gap 1: nx ships routing rules whose targets it does not control

`grep_for_symbols_redirects_to_serena.py` is in `nx/hooks/scripts/routing/`
and registered in `nx/hooks/hooks.json`. It fires on every `grep` /
`rg` against a code file regardless of whether the sn plugin is
installed. The redirect message names Serena MCP tools that may not
exist in the user's session.

#### Gap 2: There is no ownership rule for routing-hook placement

RDR-121 documented the framework contract (JSON envelope, fail-open
default, escape token, telemetry) but did not specify which plugin
owns which rule. The decision was implicit: "all RDR-121 rules live in
nx". With one rule that decision is wrong; the next rule someone adds
will inherit the same ambient pattern.

#### Gap 3: The framework itself has no story for cross-plugin consumption

Other plugins cannot author routing hooks today without copy-pasting
the framework. `_lib.py` lives at a path only nx-shipped scripts can
import. A sibling plugin that wants a routing rule has three bad
options: vendor `_lib.py` (drift), shell out to nx (latency), or
re-implement the JSON-envelope contract from scratch.

## Context

### Background

- **RDR-121** (`docs/rdr/rdr-121-hook-enforced-tool-routing.md`,
  status: closed, shipped 4.33.0) introduced the framework and the
  three initial rules. The closure rationale named the three rules as
  P2 cohort without distinguishing which plugin owns each.
- **RDR-120** (`docs/rdr/rdr-120-storage-substrate-split.md`, status:
  draft) articulates substrate-vs-consumer for storage. § A8
  generalized the principle to test infrastructure. This RDR
  generalizes it to routing rules.
- The nx plugin (`nx/`) and sn plugin (`sn/`) ship from the same
  marketplace today. sn provides Serena + Context7 MCP tools; nx
  provides the nexus stack (T1/T2/T3, RDR lifecycle skills, beads
  integration, hook framework). Plugins are loaded independently;
  users may install one without the other.

### Technical Environment

In scope:
- `nx/hooks/scripts/routing/grep_for_symbols_redirects_to_serena.py`
  (moves to sn)
- `nx/hooks/scripts/routing/_lib.py` (stays in nx; becomes the
  framework other plugins import or vendor)
- `nx/hooks/scripts/routing/registry.yaml` (loses the grep rule entry)
- `nx/hooks/hooks.json` (loses the grep PreToolUse registration)
- `sn/hooks/scripts/routing/grep_for_symbols_redirects_to_serena.py`
  (new home)
- `sn/hooks/scripts/routing/registry.yaml` (new file)
- `sn/hooks/hooks.json` (new PreToolUse Bash matcher)

Out of scope:
- A general plugin-dependency declaration mechanism (Claude Code does
  not have first-class plugin-deps; this RDR works within that
  constraint).
- Moving `_lib.py` out of nx into a separate "framework-only" plugin.
  Possible but premature; revisit when a third plugin wants to author
  routing rules.

## Research Findings

### Investigation

Direct observation during the v4.33.0 shakeout (2026-05-20): the
`grep_for_symbols_redirects_to_serena` hook fired correctly under
Claude Code's PreToolUse pipeline; the deny message pointed at
Serena tool names. With sn installed the message was actionable;
without sn it would dead-end the user.

### Key Discoveries

- The deny message names exact `mcp__plugin_sn_serena__*` tool IDs.
  Those IDs only resolve when sn is loaded. There is no way for the
  hook to know at runtime whether sn is loaded; Claude Code's
  PreToolUse stdin payload does not include the available-tools list.
- Plugin install state IS observable on disk: `~/.claude/plugins/data/`
  contains a directory per installed plugin (e.g. `sn-nexus-plugins`).
  An nx-side detection heuristic is feasible but workaround-shaped;
  it solves the symptom not the cause.
- The two other RDR-121 rules are correctly placed:
  `phase_review_close_requires_gate` depends on
  `/nx:phase-review-gate` (an nx-shipped skill) → nx owns it.
  `git_add_all_redirects_to_explicit_paths` enforces a generic git
  discipline; it has no plugin-specific redirect target → nx is a
  reasonable default home.

### Critical Assumptions

- [x] **A1**: **Each plugin can author and register PreToolUse
  hooks via its own `hooks.json`.** Claude Code's plugin loader
  merges hook registrations from every loaded plugin. **Status**:
  VERIFIED (High confidence). **Method**: Source Search.
  **Evidence**: T2 entry `125-research-A1`. sn plugin 4.33.0 already
  ships `hooks.json` at
  `~/.claude/plugins/cache/nexus-plugins/sn/4.33.0/hooks/hooks.json`
  with three event types (SessionStart, SubagentStart,
  PermissionRequest with matcher `mcp__plugin_sn_.*`). Both nx and
  sn hooks fire in the current session, confirming Claude Code
  merges registrations. Adding a PreToolUse:Bash matcher to sn's
  hooks.json is structurally identical to the entries already
  present.
- [~] **A2**: **A hook in plugin Y can import a Python module from
  plugin X at runtime.** **Status**: PARTIALLY REFUTED (High
  confidence on the constraint, Medium on the chosen mitigation).
  **Method**: Source Search of `nx/hooks/scripts/_run_python_hook.sh`.
  **Evidence**: T2 entry `125-research-A2`. The wrapper selects
  system `python3.13` / `python3.12` directly (`for py in
  python3.13 python3.12; do command -v "$py" && exec "$py" "$@";
  done`). It does NOT use `uv run`. Comment explicitly says "hooks
  are stdlib-only". Why: the 40ms python startup is a budgeted line
  item under RDR-121 § Locked Contracts (per-hook <50ms p95). `uv
  run` adds ~30-100ms (venv resolution). Therefore the clean
  `import nexus.routing_hook_lib` path is blocked: the hook runs
  under a system python with no `conexus` venv on `sys.path`.
  **Cross-plugin import options surveyed:**
  1. Relative-path traversal from sn's `$CLAUDE_PLUGIN_ROOT` up to
     `~/.claude/plugins/cache/nexus-plugins/nx/<version>/hooks/scripts/routing/_lib.py`.
     Fragile: version selection is implicit; marketplaces upgrade
     independently.
  2. Vendor `_lib.py` in sn with a CI byte-equality check. Small
     file (~250 lines), frozen contract. Drift becomes loud at PR
     time, not silent divergence.
  3. Move framework into `conexus` and switch hooks to `uv run`.
     Clean imports but blows the 40ms startup budget. Rejected.
  **Chosen mitigation: option 2**, with A3 softened accordingly.
- [x] **A3** (revised): **Vendoring `_lib.py` in sn is acceptable
  WITH a CI byte-equality guard, scoped to the monorepo
  development model.** **Status**: Asserted (High confidence).
  **Method**: pragmatic refinement after A2 evidence. The original
  A3 prohibited vendoring on drift grounds. A2 showed the clean
  import path is structurally blocked by the stdlib-only startup-
  budget constraint, leaving vendoring as the best of the imperfect
  options. The drift risk is mitigated, not eliminated, by a
  `tests/test_routing_lib_drift.py` byte-equality test: any
  divergence between `nx/hooks/scripts/routing/_lib.py` and
  `sn/hooks/scripts/routing/_lib.py` fails CI loudly. The framework
  contract is frozen per RDR-121 § Locked Contracts; the rate of
  legitimate changes is low.
  **Enforcement perimeter (gate critique fix-in-place):** the
  byte-equality test runs in the nexus monorepo CI pipeline.
  Because `nx/` and `sn/` both live in `/Users/hal.hildebrand/git/nexus/`
  and ship through the same release pipeline (4.32.x, 4.33.0, ...
  publish both plugins from the same tag), an sn-side modification
  to `_lib.py` is caught by the same test that catches an nx-side
  one — the test reads both files from a single working tree. The
  guard is symmetric within this monorepo model. **Known
  limitation**: if nx and sn ever split into separate marketplaces
  or separate CI pipelines, the guard becomes one-directional (nx
  CI catches nx-side edits; sn CI catches sn-side edits; neither
  catches cross-edits). The current model holds for the foreseeable
  release horizon; revisit if the split happens.
- [x] **A4**: **The ownership rule generalizes cleanly.** Any future
  rule that names a plugin-specific tool ID in its redirect message
  should live in the plugin that ships that tool. **Status**: Asserted
  (High confidence). **Method**: argument by symmetry with RDR-120
  substrate-vs-consumer.

## Proposed Solution

### Approach

Three phases.

A1 and A2 are resolved at draft time (see § Critical Assumptions).
The previously-planned P0 spike collapses into the P1 migration
since the chosen mechanism (vendored `_lib.py` + byte-equality CI)
needs no separate verification.

1. **P1 Migration**: move `grep_for_symbols_redirects_to_serena.py`
   from nx to sn.
   - Copy script to `sn/hooks/scripts/routing/`. Import path stays
     identical (`sys.path.insert(0, os.path.dirname(__file__))` then
     `import _lib`) because `_lib.py` is vendored next to it.
   - Vendor `_lib.py` into `sn/hooks/scripts/routing/` (byte-identical
     copy of nx's).
   - Create `sn/hooks/scripts/routing/registry.yaml` with the rule
     entry (lift the entry from nx's registry).
   - Extend `sn/hooks/hooks.json` with a PreToolUse Bash matcher
     entry that points at the new script path.
   - Delete the script + registry entry + hooks.json entry from nx.
   - Add `tests/test_routing_lib_drift.py`: asserts byte-equality
     between `nx/hooks/scripts/routing/_lib.py` and
     `sn/hooks/scripts/routing/_lib.py`. Any divergence fails CI.
   - Update `tests/test_routing_grep_for_symbols.py` to point at the
     new sn-side script path.
   - Update RDR-121's frontmatter `implementation_notes` with a
     back-reference to RDR-125.

2. **P2 Convention**: document the ownership rule in
   `nx/hooks/scripts/routing/README.md` AND in
   `sn/hooks/scripts/routing/README.md` (new file). The rule: "If
   your redirect message names a `mcp__plugin_<owner>_*` tool, the
   hook lives in the `<owner>` plugin. The framework (`_lib.py`) is
   vendored into each plugin's routing directory; a CI byte-equality
   test guards drift. The `nx hook routing-stats` CLI stays in nx
   and reads from the shared `~/.config/nexus/routing_log.jsonl` so
   it sees every plugin's events."

   **Cross-plugin hook-count cap (gate critique fix-in-place):**
   RDR-121 § Performance Expectations imposes a 4-hook cap on
   PreToolUse:Bash. After RDR-125, that cap is an **aggregate**
   across all installed plugins, not a per-plugin count: Claude
   Code merges hook registrations from every plugin and fires them
   sequentially on each matched call. The 4-hook cap protects the
   <300ms p95 cumulative budget. Post-migration, the count is:
   nx contributes 2 routing hooks (`git_add_all`,
   `phase_review_close`) plus `pre_close_verification_hook.sh`;
   sn contributes 1 (`grep_for_symbols`). Aggregate: 4. **At the
   cap as of P1.** The P2 README in each plugin's routing/ directory
   must state explicitly: "aggregate cap is 4 routing hooks across
   nx + sn + any future plugin; current aggregate count is shown
   below." A combined-registry audit script (`scripts/audit_routing_registry.py`)
   reads both plugins' `registry.yaml` files and refuses to commit
   when the union exceeds 4 — that lint is invoked in CI as a
   sibling to `test_routing_lib_drift.py`. Adding a fifth hook in
   ANY plugin requires consolidation or a budget revision in this
   RDR's successor.

   **Ownership rule scope (gate critique fix-in-place):** the rule
   is stated for single-target hooks (one `mcp__plugin_<owner>_*`
   tool in the redirect message). Multi-target hooks (a deny
   message that names tools from two plugins) are out of scope for
   this RDR; the README must say so explicitly so the first future
   author who needs that case files a follow-on RDR rather than
   silently inventing a convention.

### Anti-goals

- **Do not** invent a plugin-dependency declaration in this RDR.
  Claude Code may add first-class plugin-deps later; designing one
  here speculatively will date the RDR.
- **Do not** extract `_lib.py` into a third "framework-only" plugin.
  One consumer (sn) and one author (nx) do not justify a separate
  marketplace artifact.
- **Do not** ship a stopgap detection heuristic in nx 4.33.1. The
  migration is the correct fix; the heuristic would have to be
  retired anyway.

## Alternatives Considered

### Alt 1: Heuristic detection in nx

The grep hook checks for `~/.claude/plugins/data/sn-nexus-plugins`
existence at startup; allows when absent. **Rejected**: solves the
symptom (no-sn UX), not the cause (wrong ownership). The hook still
lives in the wrong plugin and continues to make routing decisions on
behalf of tools it does not own. Drift-prone (sn's install directory
name is not stable across marketplaces).

### Alt 2: Move `_lib.py` into a separate framework plugin

A new plugin (`nx-routing-framework`) holds `_lib.py`; both nx and sn
import from it. **Rejected**: premature. Marketplaces are cheap; the
extra plugin install step is not. Revisit when a third plugin needs
the framework.

### Alt 3: Vendor `_lib.py` in sn (chosen, after A3 revision)

sn ships its own copy with a `tests/test_routing_lib_drift.py`
byte-equality guard. Originally rejected on drift grounds; A2
investigation showed the clean import path is structurally blocked
by the stdlib-only hook startup budget, leaving this as the best
practical option. The drift risk is controlled by the CI check, not
prevented. Chosen for P1.

### Alt 4: Status quo — leave it in nx, accept the bad UX

**Rejected**: violates the substrate-vs-consumer boundary the project
just articulated for storage and tests. Cheap to fix now; cheaper to
fix now than to accumulate a second mis-placed rule before noticing.

## Trade-offs

- **Cost**: P1 migration is ~50 lines of move + path-adjustment work
  plus one CI cycle. P0 spike is half a day if A2 fails and we have
  to pick a fallback.
- **Benefit**: the no-sn case becomes structurally impossible. No
  detection heuristic, no stopgap, no user-hostile default.
- **Risk**: if A2 fails badly (plugins cannot import across plugin
  boundaries), the fallback (vendor `_lib.py` in sn) is unappealing
  but workable. Worst case we end up with two copies of a small file
  and a CI guard that asserts byte-equality.

## Implementation Plan

A1 and A2 resolved at draft time (T2 entries `125-research-A1` and
`125-research-A2`). No P0 spike required.

### Phase 1: Migration (1 bead)

Move the script and vendor `_lib.py` per the approach. Single PR
landing:
- `sn/hooks/scripts/routing/grep_for_symbols_redirects_to_serena.py` (new)
- `sn/hooks/scripts/routing/_lib.py` (vendored from nx)
- `sn/hooks/scripts/routing/registry.yaml` (new)
- `sn/hooks/scripts/routing/README.md` (new)
- `sn/hooks/hooks.json` (extended with PreToolUse Bash matcher)
- `nx/hooks/scripts/routing/grep_for_symbols_redirects_to_serena.py` (deleted)
- `nx/hooks/scripts/routing/registry.yaml` (rule entry removed)
- `nx/hooks/hooks.json` (matcher entry removed)
- `tests/test_routing_lib_drift.py` (new — byte-equality guard)
- `tests/test_routing_registry_aggregate_cap.py` (new — refuses to
  commit when union of plugin registries exceeds 4 PreToolUse:Bash
  rules; covers the cross-plugin cap that RDR-121 § Performance
  Expectations imposed)
- `tests/test_routing_grep_for_symbols.py` (path-updated)
- `docs/rdr/rdr-121-hook-enforced-tool-routing.md` (frontmatter
  cross-reference)

### Phase 2: Convention (folded into P1's PR)

`nx/hooks/scripts/routing/README.md` + new
`sn/hooks/scripts/routing/README.md` document the ownership rule and
the vendor-with-byte-equality pattern.

## Test Plan

- P0 spike: trivial sn-side hook that emits `allow_envelope("smoke")`
  and exits 0; verify Claude Code fires it on the matcher.
- P1 migration: existing 22 tests in `tests/test_routing_grep_for_symbols.py`
  pass against the new sn-shipped script. Add one test asserting the
  script no longer exists at the nx path.
- Live shakeout: in a session with sn installed, `grep MyClass src/foo.py`
  still denies. In a session with sn uninstalled (verify by removing
  the plugin from `~/.claude/settings.json`), the same grep is
  allowed because the hook does not exist.

## Validation

The RDR is validated when:
- Phase 1 PR merges to main
- `grep MyClass src/foo.py` is denied in a session with sn loaded
- The same command is allowed in a session without sn
- `nx hook routing-stats` no longer lists `grep_for_symbols_redirects_to_serena`
  (because it is not an nx rule anymore)
- `sn hook routing-stats` (or whatever the sn-side surface ends up
  named — TBD in P1) does list it

## Finalization Gate

To be run before acceptance. See `/nx:rdr-gate`.

## References

- RDR-121 (closed): hook-enforced tool routing framework + initial rules
- RDR-120 § A8: substrate-vs-consumer boundary applied to test fixtures
- RDR-101: catalog/T3 split (single source of truth precedent for A3)
- conexus 4.33.0 CHANGELOG: shipped state
- `feedback_phase_closeout_scope_audit.md`: motivation for the third
  routing rule (correctly placed in nx)

## Revision History

- 2026-05-20: created (draft), surfaced during v4.33.0 live shakeout
  when the no-sn UX gap became concrete.
- 2026-05-20: A1 verified, A2 partially refuted, A3 revised, P0
  spike collapsed into P1. T2 evidence at `125-research-A1`
  (id=1384) and `125-research-A2` (id=1385). Chosen mechanism:
  vendor `_lib.py` with byte-equality CI guard.
- 2026-05-20: gate-driven fix-in-place. Two Significant findings
  from `/nx:rdr-gate 125` substantive-critic pass folded in:
  (1) A3 enforcement perimeter scoped to monorepo development
  model (symmetric guard within the current nexus monorepo; would
  become one-directional if nx + sn ever split into separate
  CI pipelines); (2) cross-plugin hook-count cap acknowledged in
  P2 convention; new `tests/test_routing_registry_aggregate_cap.py`
  added to Implementation Plan; ownership rule scoped explicitly
  to single-target hooks with multi-target case deferred to a
  follow-on RDR.
