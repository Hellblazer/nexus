---
title: "Claude-Assisted Upgrade and Remediation: Opt-In, Consent-Gated Enlistment of the User's Own Agent Across CLI and Claude Desktop, with a Read-Only Diagnostic Trust Boundary"
id: RDR-182
type: Architecture
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-07-10
accepted_date: 2026-07-10
related_issues: [nexus-ykzbj, nexus-c4143, nexus-pnwu0, nexus-sot7v]
related: [RDR-126, RDR-159, RDR-162, RDR-166, RDR-174, RDR-178]
---

# RDR-182: Claude-Assisted Upgrade and Remediation

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The store-upgrade path has a repeating failure signature: an upgrade hits a
store-state edge (a divergent constraint, a legacy id shape, an RLS-hidden
row), the engine crash-loops or the migration hard-fails, and the operator —
or an autonomous agent acting for them — reaches for a destructive "unblock"
(the GH #1390 constraint drop being the canonical case). Every one of these
was ultimately *diagnosed and remediated by a Claude walking a read-only
psql/forensic playbook* — but that Claude-in-the-loop remediation is today an
after-the-fact, hand-authored artifact (relay prompts written to `/tmp`),
not a built-in, safe, opt-in part of upgrade.

This RDR makes Claude-assisted diagnosis and remediation a first-class,
**opt-in** capability of the upgrade/update surface, reachable from both the
`nx` CLI and Claude Desktop, under a trust boundary that keeps every
diagnostic read-only and every byte of store content on the user's own
machine. The product *emits guidance*; the user's own agent *executes it
locally with the user's own authority*. The product never runs the user's
agent and never receives the user's data.

### Enumerated gaps to close

#### Gap 1: Upgrade edges dead-end at a bare error, tempting a destructive unblock

Today a store-state edge surfaces as a bare failure (a crash-loop, a 409
wall, a "vector service unreachable"). GH #1390 is the proof of harm: an
autonomous session, blocked by a 409-ing migration, dropped four chash
constraints to "unblock" it and silently corrupted the store. The safe path
(diagnose → remediate → verify) exists only in doc sections a stressed
operator (or agent) does not read. The fix: at each such edge, offer a
built-in, safe, guided path — and make it the *easy* path, so the destructive
reflex never fires.

#### Gap 2: Remediation knowledge is not in-product

The GH #1390 recovery, the legacy-id re-index, the poisoned-target rollback —
all live in `docs/migration-runbook.md` prose and in hand-authored `/tmp`
relay prompts. There is no `nx` verb that emits a self-contained, executable
diagnostic or recovery playbook. The `nx daemon service install-binary`
chash-poison gate (shipped, GH #1390 / nexus-pnwu0) is a first, one-off
instance: it prints a clickable runbook URL and a paste-to-Claude prompt.
This RDR generalizes that into a surface (`nx forensics` / `nx remediate`,
nexus-ykzbj).

#### Gap 3: No opt-in / consent model for enlisting an external agent

Enlisting the user's Claude to run diagnostics or remediation is a
user-initiated act that must NEVER be automatic or default-on. There is no
consent primitive today: no explicit opt-in, no record of what the user
agreed to, no scoping of what an enlisted agent may see or do. Without this,
the capability is both a privacy hazard and a trust violation. The fix: a
first-class opt-in (per-invocation and/or a durable preference), with the
default being *off* — the product surfaces the option, the user chooses it.

**Consent taxonomy (gate Layer 3, reconciling with Gap 2's shipped
precedent)**: two distinct surfaces with two distinct requirements. (a)
*Display-only guidance a human must voluntarily act on* — the shipped
`install-binary` paste-to-Claude prompt, and CLI `nx forensics` printing a
playbook — needs NO consent gate: printing text the operator chooses to copy
is itself the consent act, and gating it would be over-friction. (b) *A tool
an agent can autonomously invoke* — the MCP `forensics`/`remediate` surface —
MUST be consent-gated (the durable `claude_assisted_remediation.enabled`
flag, default-off), because there is no human-in-the-loop copy step to serve
as the consent gesture. This distinction is what keeps `nx forensics` from
being over-gated relative to what already ships, while closing the
autonomous-enlistment hole on the MCP surface.

#### Gap 4: No Claude Desktop surface integration

RDR-126 established a Claude Desktop deployment (unified chat/cowork surface).
The upgrade guidance shipped so far is terminal-only: a CLI error string and a
prompt the user must copy. A Desktop user has no in-surface path from
"my upgrade is stuck" to "my agent is walking me through the fix." The fix:
the same opt-in remediation must be reachable and renderable in Claude Desktop
(e.g. an MCP-native remediation/forensics tool the Desktop agent already has,
and/or a surface the Desktop renders), not just the terminal.

#### Gap 5: The security / privacy / compliance trust boundary is undefined

Enlisting an agent to inspect a user's store raises the load-bearing
questions this RDR must answer as *locked constraints*, not afterthoughts:
what may the enlisted agent read (schema/metadata only, never note/document
content); how is read-only-by-construction *guaranteed* (not merely
requested); how is remediation (which mutates) consented, allow-listed, and
audited; and how is it proven that nothing leaves the machine. Absent an
explicit boundary, a well-meaning agent could exfiltrate content, escalate, or
re-commit the very destructive act (drop the constraint) the capability
exists to prevent.

## Context

### Background

Discovered across a run of upgrade incidents whose common thread is
Claude-in-the-loop remediation: GH #1390 (self-inflicted constraint drop under
a 409 wall — attributed via a three-round read-only forensic relay to a remote
operator's Claude, session 9bb22dc2), nexus-ms57z (catalog-013-2 crash-loop),
nexus-1wjmq (FORCE-RLS silent no-op), and nexus-sot7v (legacy-id ETL
hard-fail). The remediation pattern proved out end-to-end with a *remote*
operator's agent running committed, read-only playbooks and returning
structured findings. Full strategic framing: T2
`nexus/strategic-claude-assisted-upgrade-2026-07-10`.

The first in-product instance already ships (nexus-pnwu0 gate on
`install-binary`): full clickable runbook URL + paste-to-Claude prompt.
nexus-c4143 is the concrete engine-side "fail clean, not crash-loop" fix this
vision generalizes; nexus-ykzbj is the tactical `nx forensics` / `nx
remediate` command surface under this RDR.

### Technical Environment

conexus (Python 3.12 `nx` CLI + MCP servers), the Java engine-service +
pgvector substrate (RDR-152/155), the guided-upgrade path (RDR-159/162/178),
managed-service journeys (RDR-166), the unified install lifecycle (RDR-174),
and the Claude Desktop deployment (RDR-126). MCP is the natural integration
seam for Desktop (the plugin already ships MCP servers/tools).

## Research Findings

### Investigation

Three parallel codebase-deep-analyzer passes (2026-07-10) verified the three
Critical Assumptions against source. Full evidence: T2
`nexus/rdr182-critical-assumption-1-readonly-by-construction`,
`nexus/rdr182-assumption3-opt-in-primitives-investigation`, and T3
`analysis-codebase-rdr182-desktop-mcp-forensics-boundary` (doc
`08ddba2d27119e2798464102f211227f`, catalog-linked to RDR-126).

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| MCP (tool surface for Desktop) | Yes | Two local FastMCP **stdio** servers ship (`nx-mcp` = `src/nexus/mcp/core.py`, `nx-mcp-catalog` = `src/nexus/mcp/catalog.py`); a tool is a `@mcp.tool()` decorator; `daemon_uninstall` (core.py:5661) is the describe-then-confirm template; zero outbound HTTP in core.py. |
| Claude Desktop (RDR-126 surface) | Yes | RDR-126's `.mcpb` installs BOTH servers into Desktop (live-verified, `tools/list` served). A Desktop agent already reaches any registered tool — no new transport. The ONLY reliably user-visible payload channel is the **tool return value** (`notifications/message`, `instructions`, and a2ui/MCP-Apps rendering are NOT reliably visible on Desktop per RDR-126 A2). |
| Postgres read-only role | Yes | **Does not exist.** Only `nexus_admin` (DDL owner) and `nexus_svc` (full DML) are provisioned; the diagnostic probes today run as `nexus_admin` — the exact privilege class that made GH #1390's `DROP CONSTRAINT` possible. |
| Config / consent primitives | Yes | Durable prefs: `config.py:800` `set_config_value` → `config.yml` (atomic, 0600), exposed by `nx config set`. Per-invocation consent: `_confirm_voyage_cost` pattern (`click.confirm` aborts non-interactive). Default-off precedent: `attention_guided_v1` (config.py:695) ships default-False + locked exact-equality test `tests/test_config.py::TestTelemetryConfig`. |

### Key Discoveries

- **Verified**: the read-only-forensic-relay pattern works with a remote
  operator's agent end-to-end (GH #1390 three-round relay pinned the root
  cause with zero writes).
- **Verified**: an in-product gate can both block a destructive upgrade and
  hand off a paste-to-Claude remediation prompt (nexus-pnwu0, shipped).
- **Verified (A2)**: Desktop integration is REUSE, not new transport — add a
  `@mcp.tool()` (`forensics(topic) -> str` / `remediate(topic, confirm=False)
  -> str`) in `core.py` beside `daemon_uninstall`, returning the playbook as
  the tool's return string (the proven-visible channel). Extract
  `_emit_chash_poison_gate`'s prompt-construction into a shared `(topic,
  store_state) -> Playbook` used by both the CLI gate and the MCP tool.
- **Verified (A3)**: opt-in is REUSE — `_confirm_voyage_cost` for
  per-invocation, `config.yml`/`nx config set` for the durable revocable
  preference (`claude_assisted_remediation.enabled`, zero new plumbing),
  `attention_guided_v1` + its locked test as the default-off template. Only a
  small consent-AUDIT table is net-new.
- **Documented**: the engine-side changelog CANNOT cleanly self-heal a
  present-but-violating constraint (checksum lock + FORCE-RLS count skew) —
  so "fail clean and enlist the operator's agent" is the right posture, not
  "auto-fix in the migration" (nexus-c4143 analysis).
- **Verified + REFINED (A1) — the load-bearing honesty finding**: no probe in
  this codebase reads row/document content (all are counts/lengths/system-
  catalog reads), but that norm is enforced by author care, not tooling, and
  the probes run as the DDL-owner role. A dedicated read-only role
  (`nexus_diag`, SELECT-only, needs building) is the ONLY mechanism that makes
  the DB itself refuse a `DROP CONSTRAINT` — BUT it constrains only the
  connection the product's own tooling opens. It cannot stop the user's
  enlisted agent from opening its own `psql` as `nexus_admin` and free-typing
  DDL, because RDR-182's trust boundary is precisely "the agent executes
  locally with the user's live credentials." Design consequence (below): the
  read-only guarantee is scoped to the DIAGNOSTIC tooling path; the ultimate
  defense against the GH #1390 reflex is behavioural — make the safe path the
  easy path — plus keeping mutation on a separate, consented, audited
  `remediate` path with the do-NOTs front-and-center. This is why Gap 1
  (safe-path-is-easy-path) is load-bearing, not cosmetic.

### Critical Assumptions

- [x] Read-only diagnostics can be guaranteed by construction — **Status**:
  Verified + REFINED — **Method**: Source Search. A `nexus_diag` SELECT-only
  role (+ `SET TRANSACTION READ ONLY` defense-in-depth + a pre-emission
  statement allow-list lint reusing the `_DML_TARGET_RE` pattern) makes the
  product's diagnostic path read-only by construction. Caveat now locked into
  the design: this binds the tooling connection, NOT what the user's own agent
  can type as `nexus_admin` — the behavioural safe-path-is-easy-path principle
  (Gap 1) and the separate consented `remediate` path carry that.
- [x] A Desktop-resident agent can be handed a playbook via an MCP tool
  without the product transmitting store content — **Status**: Verified —
  **Method**: Source Search. `.mcpb` already installs the servers into Desktop;
  add one `@mcp.tool()`; payload is the return string; core.py has no outbound
  HTTP; the playbook is guidance text built from local diagnostic labels only.
- [x] Opt-in **CLI-side** expressible per-invocation AND durable/revocable
  without weakening default-off — **Status**: Verified — **Method**: Source
  Search. `_confirm_voyage_cost` (interactive-terminal-only) +
  `config.yml`/`nx config set` + `attention_guided_v1`'s default-False +
  locked test cover the CLI path; a small T2 `record_consent()` table is
  net-new. SCOPE (gate Layer 3): this covers the CLI ONLY.
- [x] Opt-in **enforceable at the MCP tool boundary** (a config-flag read
  inside `forensics`/`remediate` that refuses before emitting content) —
  **Status**: Verified — **Method**: Spike (nexus-ykzbj.1, 2026-07-12). A
  registered throwaway `@mcp.tool()` (`rdr182_gate_spike`) gated by a
  first-statement `_remediation_opt_in()` read of
  `claude_assisted_remediation.enabled` refuses with an exact-remedy string
  and provably enters zero content path when the flag is false/absent
  (tests/mcp/test_rdr182_opt_in_gate.py, 8 mechanical tests). Two
  load-bearing spike findings locked for Phase 3: `nx config set` stores the
  raw STRING (`"true"`/`"false"`), so the gate parses strictly and
  fail-closed (naive truthiness would invert an explicit `"false"` disable);
  and the flag is read fresh per invocation (long-lived MCP server —
  mid-session enable takes effect on the next call). Exact gate shape +
  reuse contract: T2 `nexus/rdr182-a4-spike-gate-shape.md`.

## Proposed Solution

### Approach

A layered, opt-in capability:

1. **Detection layer (exists, extend)**: read-only probes (`_check_migration_state`
   chash-conformance, and siblings for other edges) that classify store-state
   risk. Already feeds `nx doctor` and the `install-binary` gate.
2. **Emission layer (new, nexus-ykzbj)**: `nx forensics <topic>` emits a
   self-contained, read-only diagnostic playbook; `nx remediate <topic>`
   emits a guided recovery playbook (allow-listed, consented, sequenced,
   with the hard do-NOTs). Full clickable https URLs (pinned to `main`).
3. **Consent layer (new)**: opt-in is default-off. The product SURFACES the
   option at an edge; the user opts in per-invocation or via a durable
   revocable preference. Consent scope is recorded (what was agreed, when).
4. **Surface layer (new)**: the same emission + consent reachable from the
   CLI AND Claude Desktop (RDR-126) via an MCP tool a Desktop-resident agent
   already holds. Because that tool is autonomously agent-invocable, it
   enforces the durable opt-in flag at its own entry (§ Technical Design) —
   the autonomous surface carries at least as much consent friction as the
   CLI, never less.
5. **Trust boundary (locked)**: the product emits guidance; the user's agent
   executes locally with the user's creds. The product never runs the user's
   agent, never receives store content, and diagnostics are read-only by
   construction. Remediation that mutates is explicitly consented and
   audited.

### Technical Design

Research (2026-07-10) resolved the mechanisms; design is now concrete:

- **Playbook emitter (shared)**: extract `_emit_chash_poison_gate`'s
  prompt-construction (`src/nexus/commands/daemon.py`) into a shared
  `(topic, store_state) -> Playbook`, where `Playbook` carries (context, hard
  READ-ONLY/do-NOT constraints, ordered steps, structured-deliverable schema,
  clickable https URL pinned to `main`, "environment gone" escape). Renders to
  (a) a terminal prompt (CLI) and (b) a tool return string (MCP/Desktop).
- **MCP/Desktop surface**: two new `@mcp.tool()` functions in
  `src/nexus/mcp/core.py` beside `daemon_uninstall` — `forensics(topic) ->
  str` (read-only diagnostic playbook) and `remediate(topic, confirm=False)
  -> str`. The RDR-126 `.mcpb` already exposes these to Desktop; the tool
  RETURN STRING is the payload channel (the only reliably-visible one).
  **OPT-IN ENFORCEMENT AT THE TOOL BOUNDARY (gate Critical, Layer 3)**: unlike
  a CLI command — which a human must explicitly type (an implicit consent
  gesture) — an MCP tool is *autonomously agent-invocable*. So the FIRST
  statement of BOTH tools reads `claude_assisted_remediation.enabled` from
  config and, when false (the default), returns a refusal string ("capability
  not enabled — run `nx config set claude_assisted_remediation.enabled true`")
  BEFORE emitting any diagnostic content. The autonomously-reachable surface
  gets AT LEAST as much consent friction as the CLI, never less. `remediate`'s
  five-layer contract is explicit so the opt-in check is never collapsed into
  the confirm flag: (1) opt-in check → (2) describe (confirm=False) → (3)
  confirm=True → (4) mutate → (5) audit-record. There is no existing pattern
  in this codebase for gating an `@mcp.tool()` behind a config flag —
  `daemon_uninstall` deletes the user's OWN local daemon (no consent stakes)
  and is a shape template only, NOT evidence that describe-then-confirm alone
  satisfies Gap 3. This gate — a config read at tool entry — is net-new and
  is its own Critical Assumption (A4 below).
- **Read-only-by-construction (diagnostic path)**: a new `nexus_diag`
  SELECT-only Postgres role (Liquibase changeset + grant), used by the
  diagnostic tooling connection, with `SET TRANSACTION READ ONLY` as
  defense-in-depth and a pre-emission statement allow-list lint (reusing the
  `_DML_TARGET_RE` classification pattern from `tests/test_changelog_rls_lint.py`)
  over any SQL the product emits. LOCKED CAVEAT (research A1): this binds the
  product's own tooling connection, not what the user's enlisted agent can run
  as `nexus_admin` — so mutation lives ONLY on the consented `remediate` path
  and Gap 1's behavioural safe-path-is-easy-path carries the rest.
- **Opt-in**: default-off via `config.yml`
  (`claude_assisted_remediation.enabled`, using `set_config_value` /
  `nx config set`), the `attention_guided_v1` default-False + locked
  exact-equality test as the template; per-invocation surfacing via the
  `_confirm_voyage_cost` pattern (`click.confirm` aborts non-interactive).
- **Consent audit (net-new)**: a small T2 `record_consent(scope, ts)` on the
  `Telemetry` surface (alongside `record_tier_write` / `record_nx_answer_run`)
  writing a dedicated `claude_assisted_remediation_consents` table.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Read-only store-state probes | `src/nexus/health.py` `_check_migration_state` | Reuse/Extend: single source of truth for the chash probe already |
| Playbook emitter | `src/nexus/commands/daemon.py` `_emit_chash_poison_gate` | Extend: hoist prompt-construction into a shared `(topic, store_state) -> Playbook` |
| Recovery playbook content | `docs/migration-runbook.md` §8/§8.1 | Reuse: the canonical remediation text the emitter references |
| Desktop surface | RDR-126 `.mcpb` + `src/nexus/mcp/core.py` | Extend: add `forensics`/`remediate` `@mcp.tool()` beside `daemon_uninstall` (its describe-then-confirm template) |
| Read-only diagnostic role | `src/nexus/db/pg_provision.py` + `role-001`/`grants-nexus-svc.xml` | Build: new `nexus_diag` SELECT-only role (none exists; `nexus_admin`/`nexus_svc` are both write-capable) |
| Durable opt-in preference | `src/nexus/config.py` `set_config_value` + `nx config set` | Reuse: `claude_assisted_remediation.enabled`, default-off (`attention_guided_v1` template) |
| Per-invocation consent | `_confirm_voyage_cost` / `render_cost_confirmation` | Reuse: `click.confirm` aborts non-interactive |
| Consent audit | `src/nexus/db/t2/telemetry.py` `Telemetry` | Build: `record_consent()` + a dedicated T2 table (net-new, small) |
| Statement allow-list lint | `tests/test_changelog_rls_lint.py` `_DML_TARGET_RE` | Reuse pattern: pre-emission read-only classification of emitted SQL |

### Decision Rationale

The changelog/engine cannot self-heal every edge (nexus-c4143 proves the
present-but-violating case is unfixable in-migration), and fully-automatic
remediation is both unsafe (mutation without consent) and a privacy hazard.
Enlisting the *user's own* agent, opt-in, with a read-only diagnostic boundary,
keeps authority and data with the user while making the safe path the easy
path — directly targeting the destructive-unblock reflex the track record
exposes.

## Alternatives Considered

### Alternative 1: Fully-automatic in-product remediation (no agent)

**Description**: the product detects the edge and auto-runs the fix.

**Pros**: zero friction.

**Cons**: mutation without explicit consent; cannot cover the cases that need
judgement (which collections to re-index, whether note text is recoverable);
re-introduces the "auto-drop-the-constraint" hazard class if a fix is wrong.

**Reason for rejection**: violates the consent + safe-path-is-easy-path
principles; the engine-side analysis (c4143) shows the hard cases need a human
+ agent in the loop.

### Briefly Rejected

- **Default-on agent enlistment**: violates the opt-in requirement and is a
  privacy non-starter.
- **Terminal-only guidance (status quo)**: leaves Desktop users (RDR-126)
  with no path (Gap 4).
- **Send store diagnostics to a hosted service for analysis**: violates the
  nothing-leaves-the-box privacy constraint.

## Trade-offs

### Consequences

- (+) The safe remediation path becomes the easy path at every upgrade edge —
  targets the root behavioral cause of the incident class.
- (+) Data and authority stay with the user; the product never sees store
  content.
- (−) A new consent + surface layer to build and maintain across CLI and
  Desktop.
- (−) Read-only-by-construction is harder than read-only-by-prompt and must be
  engineered, not asserted.

### Risks and Mitigations

- **Risk**: an enlisted agent runs a mutating/destructive statement under the
  banner of "diagnostics."
  **Mitigation**: diagnostics are allow-listed read-only by construction;
  remediation is a separate, explicitly-consented, audited path with the hard
  do-NOTs front-and-center.
- **Risk**: store content leaks into the agent's context or off-box.
  **Mitigation**: diagnostics read schema/metadata only (databasechangelog,
  pg_constraint, counts) — never row content; playbooks carry an explicit
  no-exfiltration constraint; nothing is transmitted by the product.
- **Risk**: opt-in erodes into default-on for "convenience."
  **Mitigation**: default-off is a locked constraint; parity tests assert it.

### Failure Modes

- **Opt-in not enabled (the default)**: the MCP `forensics`/`remediate` tools
  return a refusal string naming the exact enable command (`nx config set
  claude_assisted_remediation.enabled true`) before any content — visible,
  actionable, no capability leak. The CLI `nx forensics` (display-only) is
  unaffected (it needs no gate per the consent taxonomy).
- **Store-state probe cannot run** (PG down, not service mode): degrades to a
  non-fatal warn and the display-only runbook URL — never a false "clean" and
  never a hard block on unrelated work (matches the shipped `install-binary`
  gate's probe-skip posture).
- **Desktop hand-off unavailable**: degrades to the terminal path (the same
  clickable URL + paste-to-Claude prompt already shipped) — Desktop is an
  additive surface, never a hard dependency.
- **User declines the per-invocation confirm**: the action does not run; the
  runbook URL remains on screen for manual follow-up.

## Implementation Plan

### Prerequisites

- [x] All Critical Assumptions verified — A4 (MCP tool-boundary opt-in
  enforcement) verified 2026-07-12 by the nexus-ykzbj.1 gating spike (see
  Critical Assumptions above; gate shape in T2
  `nexus/rdr182-a4-spike-gate-shape.md`).
- [ ] RDR-126 Desktop surface + MCP tool model reviewed

### Minimum Viable Validation

Two proofs, both in scope:

1. **The safety property**: with `claude_assisted_remediation.enabled` false
   (default), the MCP `forensics`/`remediate` tools return the refusal string
   and emit ZERO diagnostic content — the autonomous-enlistment hole is
   closed by construction, asserted mechanically.
2. **The end-to-end flow**: with the flag enabled, a poisoned store → operator
   opts in at the upgrade edge → `nx remediate chash-poison` (and/or the
   Desktop MCP tool) hands the agent a read-only diagnostic + consented
   recovery playbook → agent walks §8.1 → a re-run upgrade succeeds, with an
   assertion that no store-CONTENT read occurred (only schema/metadata).

### Phase 1: Code Implementation

To be decomposed into beads under nexus-ykzbj during `/conexus:create-plan`.

## Test Plan

- **Scenario**: default is off — no invocation enlists an agent without an
  explicit opt-in — **Verify**: parity/behaviour test asserts default-off
  (exact-equality on the config default, `attention_guided_v1` template).
- **Scenario**: MCP `forensics`/`remediate` invoked with the flag FALSE —
  **Verify**: returns the refusal string, emits zero diagnostic content (the
  autonomous-enlistment hole; the gate's Critical, asserted mechanically).
- **Scenario**: `remediate` five-layer ordering — **Verify**: opt-in check
  precedes describe; confirm=False never mutates; a consent record is written
  on the mutate path.
- **Scenario**: diagnostics are read-only by construction — a remediation
  topic's diagnostic playbook contains no mutating statement — **Verify**:
  allow-list/lint over emitted diagnostics.
- **Scenario**: no store content in emitted playbooks — **Verify**: emitted
  diagnostic references only schema/metadata objects, never row content.
- **Scenario**: Desktop hand-off — a Desktop-resident agent can receive the
  remediation playbook via the MCP tool — **Verify**: MCP tool returns the
  playbook payload; degrades to the terminal path when Desktop is absent.
- **Scenario**: consent is recorded and revocable — **Verify**: opt-in scope
  persists/revokes as designed.

## Validation

### Testing Strategy

Covered by the Test Plan; the read-only-by-construction and default-off
guarantees are the load-bearing, non-functional properties and get explicit
mechanical tests (not estimates).

## Finalization Gate

> Complete before Accepted. To be filled during `/conexus:rdr-gate`.

### Contradiction Check

No contradictions found between research findings, design, and the related
RDRs. Cross-checked against RDR-126 (gate Layer 3): RDR-182's claim that the
MCP tool RETURN STRING is the only reliably-visible Desktop payload channel
correctly reuses RDR-126's established finding (`instructions` /
`notifications/message` / a2ui inline UI are not reliably visible on Desktop),
rather than contradicting it. No conflict with RDR-159/178's upgrade flow.

### Assumption Verification

The FOUR Critical Assumptions above (A1-A4) must be Verified before
implementation. A1/A2/A3 are Verified (Source Search). A4 (MCP tool-boundary
opt-in enforcement) is currently Unbuilt and requires the
tool-boundary-refusal spike (proving the tool refuses when the flag is false)
before any implementation — it is the load-bearing safety mechanism for the
first-class Desktop surface.

### Scope Verification

The Minimum Viable Validation (one end-to-end opt-in remediation flow with the
no-exfiltration assertion) is in scope, not deferred.

### Cross-Cutting Concerns

- **Versioning**: N/A (behavioural surface) — confirm at gate.
- **Deployment model**: CLI + Claude Desktop (RDR-126) — first-class.
- **Incremental adoption**: opt-in, default-off — the whole point.
- **Secret/credential lifecycle**: the enlisted agent uses the user's own
  local creds; the product mints/stores nothing new — confirm at gate.
- **Privacy / data residency**: no store *content* leaves the box —
  diagnostics read schema/metadata only (counts, constraint names,
  conformance flags), never row/document/note content. Those schema/metadata
  labels ARE visible to whichever agent the user chose to enlist, which — for
  a cloud-hosted Claude — necessarily transits to Anthropic's inference API as
  ordinary conversational context; that is inherent to "enlist your Claude,"
  not a leak. LOCKED constraint: content stays local; the product itself
  transmits nothing.
- **Security**: read-only-by-construction diagnostics; consented + audited
  remediation; the safe path is the easy path — LOCKED constraint.

### Proportionality

Draft scaffold — right-sized for a strategic direction; research and gate will
add the verified design detail and trim speculation.

## References

- T2 `nexus/strategic-claude-assisted-upgrade-2026-07-10` (strategic framing +
  locked security/privacy/compliance requirements)
- T2 `nexus/gh1390-forensic-CLOSED-self-inflicted` (the incident + relay pattern)
- `docs/migration-runbook.md` §8 / §8.1 (canonical remediation content)
- `src/nexus/commands/daemon.py` `_emit_chash_poison_gate`;
  `src/nexus/health.py` `_check_migration_state` (shipped precedent)
- RDR-126 (Claude Desktop Deployment), RDR-159/162/178 (upgrade/migration),
  RDR-166 (managed journeys), RDR-174 (install lifecycle)
- Issues: nexus-ykzbj, nexus-c4143, nexus-pnwu0, nexus-sot7v

## Revision History

- 2026-07-10: Draft scaffolded (`/conexus:rdr-create`). Opt-in and Claude
  Desktop integration added as first-class requirements per Hal.
- 2026-07-10: `/conexus:rdr-research` — 3 parallel source investigations. All
  three Critical Assumptions VERIFIED against source (A1 verified + refined
  with the read-only-role-binds-tooling-not-agent caveat; A2/A3 clean reuse).
  Technical Design + Infrastructure Audit made concrete. Evidence in T2/T3
  (see Investigation).
- 2026-07-10: `/conexus:rdr-gate` — Layer 1 (structural) + Layer 2
  (assumptions Source-Searched) PASSED; Layer 3 critic BLOCKED on 1 Critical
  (MCP/Desktop tools had no specified opt-in enforcement despite Gap 3;
  A3 overclaimed Desktop coverage). Remediated: added tool-boundary opt-in
  enforcement + the 5-layer `remediate` contract to Technical Design;
  split A3 into CLI-verified (A3) and MCP-unbuilt (A4, spike-required);
  added the consent taxonomy (display-only vs agent-invocable) reconciling
  Gap 2's shipped precedent; tightened the privacy line; enumerated Failure
  Modes; expanded MVV + Test Plan for the refusal property. Re-gate pending.
