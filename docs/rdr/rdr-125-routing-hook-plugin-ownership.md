---
title: "Routing-Hook Plugin Ownership: Each Plugin Ships Its Own Rules"
id: RDR-125
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-20
accepted_date:
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

- [ ] **A1**: **Each plugin can author and register PreToolUse
  hooks via its own `hooks.json`.** Claude Code's plugin loader
  merges hook registrations from every loaded plugin. **Status**:
  Unverified. **Method**: read the Claude Code plugin loader source;
  smoke-test with a trivial sn-side hook that just prints
  `additionalContext`. **Verification owner**: P0 spike.
- [ ] **A2**: **A hook in plugin Y can import a Python module from
  plugin X at runtime.** sn's `grep_for_symbols_redirects_to_serena.py`
  needs `_lib.py` which ships in nx. The simplest mechanism is a
  filesystem path relative to a shared install root; the harder
  mechanism is a Python package both plugins depend on. **Status**:
  Unverified. **Method**: inspect how the existing nx hook scripts
  import `_lib.py` (relative-path `sys.path.insert(0, dirname)`); see
  whether the same trick works across plugin install directories.
  **Verification owner**: P0 spike.
- [ ] **A3**: **Vendoring `_lib.py` in sn is unacceptable.** Two
  copies of the framework drift; the JSON envelope contract could
  silently diverge. **Status**: Asserted (Medium confidence).
  **Method**: argument from RDR-101 catalog/T3 split precedent
  (single source of truth wins over drift-prone duplication).
- [ ] **A4**: **The ownership rule generalizes cleanly.** Any future
  rule that names a plugin-specific tool ID in its redirect message
  should live in the plugin that ships that tool. **Status**: Asserted
  (High confidence). **Method**: argument by symmetry with RDR-120
  substrate-vs-consumer.

## Proposed Solution

### Approach

Three phases.

1. **P0 Spike**: verify A1 and A2. Smoke-test a trivial sn-side
   PreToolUse hook that imports nx's `_lib.py`. If A2 fails under
   plugin isolation, fall back to vendoring `_lib.py` in sn (a
   reluctant A3 override) OR move `_lib.py` to a marketplace-shared
   location (escalation to a follow-on RDR). Document the chosen
   import mechanism.

2. **P1 Migration**: move `grep_for_symbols_redirects_to_serena.py`
   from nx to sn.
   - Copy script to `sn/hooks/scripts/routing/`
   - Adjust the import path for `_lib.py` per P0's verified mechanism
   - Create `sn/hooks/scripts/routing/registry.yaml` with the rule
     entry
   - Create or extend `sn/hooks/hooks.json` with a PreToolUse Bash
     matcher entry
   - Delete the script + registry entry + hooks.json entry from nx
   - Update RDR-121's frontmatter `implementation_notes` to record
     the migration
   - Update `tests/test_routing_grep_for_symbols.py` to assert
     against the new sn location

3. **P2 Convention**: document the ownership rule in
   `nx/hooks/scripts/routing/README.md`. The rule: "If your redirect
   message names a `mcp__plugin_<owner>_*` tool, the hook lives in
   the `<owner>` plugin. The framework (`_lib.py` + the
   `nx hook routing-stats` CLI) stays in nx."

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

### Alt 3: Vendor `_lib.py` in sn

sn ships its own copy. **Rejected**: A3 says no. The JSON envelope
contract is RDR-121 § Locked Contracts; two copies of it WILL drift
the first time someone touches one.

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

### Phase 0: Spike (1 bead)

Verify A1 (sn can register PreToolUse hooks) and A2 (sn's hook can
import nx's `_lib.py`). Smoke test ships in a worktree, no PR.
Outcome captured as T2 research finding `125-research-A1A2`.

### Phase 1: Migration (1 bead)

Move the script per the approach. Single PR. Tests updated to
reference the new path. Closes the RDR.

### Phase 2: Convention (folded into P1's PR)

Update `nx/hooks/scripts/routing/README.md` with the ownership rule
and update RDR-121's frontmatter `implementation_notes` with a back-
reference to RDR-125.

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
