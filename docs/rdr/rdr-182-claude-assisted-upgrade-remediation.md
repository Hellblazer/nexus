---
title: "Claude-Assisted Upgrade and Remediation: Opt-In, Consent-Gated Enlistment of the User's Own Agent Across CLI and Claude Desktop, with a Read-Only Diagnostic Trust Boundary"
id: RDR-182
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-07-10
accepted_date:
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

To be completed during `/conexus:rdr-research`. Anchors to investigate:

- The existing in-product precedent: `src/nexus/commands/daemon.py`
  `_emit_chash_poison_gate` (the paste-to-Claude + clickable-URL pattern) and
  `src/nexus/health.py` `_check_migration_state` (the read-only probe reused
  by both `nx doctor` and the gate).
- The `/tmp` relay prompts authored for GH #1390 (the manual template for a
  self-contained read-only playbook: context + hard READ-ONLY/do-NOT
  constraints + steps + structured deliverable + "environment gone" escape).
- RDR-126's Claude Desktop deployment surface and its MCP tool inventory —
  what a Desktop-resident agent already has, and where a remediation/forensics
  tool would live.
- Consent/opt-in prior art in the codebase (any existing preference or
  confirm primitives) and the plugin's MCP tool-permission model.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| MCP (tool surface for Desktop) | No | TBD in research: how a remediation tool is exposed to a Desktop-resident agent |
| Claude Desktop (RDR-126 surface) | No | TBD: rendering + tool availability in the Desktop chat/cowork surface |

### Key Discoveries

- **Verified**: the read-only-forensic-relay pattern works with a remote
  operator's agent end-to-end (GH #1390 three-round relay pinned the root
  cause with zero writes).
- **Verified**: an in-product gate can both block a destructive upgrade and
  hand off a paste-to-Claude remediation prompt (nexus-pnwu0, shipped).
- **Documented**: the engine-side changelog CANNOT cleanly self-heal a
  present-but-violating constraint (checksum lock + FORCE-RLS count skew) —
  so "fail clean and enlist the operator's agent" is the right posture, not
  "auto-fix in the migration" (nexus-c4143 analysis).
- **Assumed** (verify): MCP is the seam that makes Desktop integration
  seamless rather than copy-paste.

### Critical Assumptions

- [ ] Read-only diagnostics can be guaranteed by construction (allow-listed
  read-only SQL/commands), not merely by prompt instruction — **Status**:
  Unverified — **Method**: Spike
- [ ] A Desktop-resident agent can be handed a remediation playbook via an
  MCP tool the plugin ships, without the product transmitting store content —
  **Status**: Unverified — **Method**: Source Search
- [ ] Opt-in can be expressed both per-invocation and as a durable, revocable
  preference without weakening the default-off guarantee — **Status**:
  Unverified — **Method**: Spike

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
   CLI AND Claude Desktop (RDR-126) — the leading candidate is an MCP tool a
   Desktop-resident agent already holds, so "enlist my agent" is one action,
   not a copy-paste.
5. **Trust boundary (locked)**: the product emits guidance; the user's agent
   executes locally with the user's creds. The product never runs the user's
   agent, never receives store content, and diagnostics are read-only by
   construction. Remediation that mutates is explicitly consented and
   audited.

### Technical Design

To be detailed during research/gate. Design is deliberately not over-specified
in this draft. Interfaces to define:

- A playbook-emitter contract: `(topic, store-state) -> Playbook` where a
  `Playbook` carries (context, hard constraints, ordered steps, structured
  deliverable schema, escape clause) and renders to (a) a terminal prompt +
  clickable URL and (b) an MCP-tool payload for Desktop.
- A read-only guarantee mechanism (allow-listed statements / a restricted
  execution contract) — NOT prompt-only.
- A consent record contract (scope, timestamp, revocation).

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Read-only store-state probes | `src/nexus/health.py` `_check_migration_state` | Reuse/Extend: single source of truth for the chash probe already |
| In-product remediation prompt + URL | `src/nexus/commands/daemon.py` `_emit_chash_poison_gate` | Extend: generalize the one-off gate into the emission layer |
| Recovery playbook content | `docs/migration-runbook.md` §8/§8.1 | Reuse: the canonical remediation text the emitter references |
| Desktop surface | RDR-126 deployment + plugin MCP servers | Extend: add a remediation/forensics MCP tool |

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

To be enumerated during gate: what an operator sees when they decline consent;
what happens when the store-state probe cannot run; how a Desktop hand-off
degrades to the terminal path.

## Implementation Plan

### Prerequisites

- [ ] All Critical Assumptions verified
- [ ] RDR-126 Desktop surface + MCP tool model reviewed

### Minimum Viable Validation

One end-to-end opt-in flow: a poisoned store, the operator opts in at the
upgrade edge, `nx remediate chash-poison` (and/or the Desktop MCP tool) hands
their agent a read-only diagnostic + a consented recovery playbook, the agent
walks §8.1, and a re-run upgrade succeeds — with an assertion that no
store-content read or off-box transmission occurred.

### Phase 1: Code Implementation

To be decomposed into beads under nexus-ykzbj during `/conexus:create-plan`.

## Test Plan

- **Scenario**: default is off — no invocation enlists an agent without an
  explicit opt-in — **Verify**: parity/behaviour test asserts default-off.
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

TBD.

### Assumption Verification

The three Critical Assumptions above must be Verified before implementation.

### Scope Verification

The Minimum Viable Validation (one end-to-end opt-in remediation flow with the
no-exfiltration assertion) is in scope, not deferred.

### Cross-Cutting Concerns

- **Versioning**: N/A (behavioural surface) — confirm at gate.
- **Deployment model**: CLI + Claude Desktop (RDR-126) — first-class.
- **Incremental adoption**: opt-in, default-off — the whole point.
- **Secret/credential lifecycle**: the enlisted agent uses the user's own
  local creds; the product mints/stores nothing new — confirm at gate.
- **Privacy / data residency**: nothing leaves the box; diagnostics read
  schema/metadata only — LOCKED constraint.
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
