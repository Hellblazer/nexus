---
title: "Agent Lifecycle & Operations: Document the nexus Agent (engine-service + nx CLI) and the Full Install / Uninstall / Upgrade Story"
id: RDR-165
type: Documentation
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-22
related_issues: [nexus-luxe6, nexus-y5avl]
related: [RDR-002, RDR-144, RDR-149, RDR-152, RDR-155, RDR-157, RDR-159, RDR-161]
---

## Problem Statement

There is no single operator-facing account of what the "nexus agent" *is*, what
states it moves through, and how a user installs, uninstalls, or upgrades it.
The knowledge exists but is scattered across the RDR record and code:

- **What the agent is** — the engine-service daemon (PG16 + pgvector + native
  Java service), the `nx` CLI, the service-registry/lease lifecycle (RDR-149),
  and the three storage tiers — has no consolidated description. A user reads
  `docs/architecture.md` for tiers, RDR-152 for the service, RDR-149 for daemon
  lifecycle, and infers the rest.
- **Install** — split across RDR-157 (distribution model), RDR-161 (native-only
  install), RDR-144 (`nx init` embedder onboarding), and the `nx init --service`
  / `nx daemon service install-binary` commands, with no end-to-end walkthrough.
- **Uninstall** — exists only as the `daemon_uninstall` **MCP tool** (removes the
  OS autostart unit, stops the daemon, clears the first-run marker, optionally
  deletes the data dir). Whether there is a coherent, discoverable **CLI**
  uninstall with the same completeness is unverified — a likely small gap.
- **Upgrade** — RDR-002 / RDR-159 (`nx guided-upgrade`) and RDR-162 (cross-model
  upgrade chain) cover the Chroma→service migration, but the *operational*
  framing (when do I upgrade, what does it touch, how do I roll back) is buried
  in migration-engine RDRs.

The result: the lifecycle is implemented but not legible. As the 6.0.0
migration-capable release approaches (`nexus-y5avl`), the absence of an
authoritative lifecycle/operations doc is a real adoption and supportability
gap — every install/upgrade question becomes archaeology across a dozen RDRs.

This RDR is the **nexus-only, documentation-first** half of a two-RDR pair. Its
companion (RDR-166) covers the managed-service (conexus-nexus.com) consumer
journeys: greenfield onboarding and local→managed migration.

## Decision

Produce one authoritative, operator-facing **Agent Lifecycle & Operations**
document (target: `docs/operations.md` or `docs/lifecycle.md`, TBD in research)
that is the single source of truth for:

1. **The agent model** — engine-service daemon + `nx` CLI + storage tiers +
   service-registry/lease lifecycle, with a state diagram (uninstalled →
   installed → provisioned → running → upgrading → uninstalled).
2. **Install** — the full first-run path (`nx init --service`,
   `nx daemon service install-binary <tag>`, PG bundle, bge-768 ONNX fetch,
   token plumbing), consolidating RDR-157/161/144 into one walkthrough.
3. **Uninstall** — a complete, discoverable teardown, with a data-preserving
   default and an explicit `--remove-data` escalation. Close any CLI gap so the
   `daemon_uninstall` MCP capability has a first-class `nx` command equivalent.
4. **Upgrade** — the operational framing of `nx guided-upgrade`: what it
   detects, provisions, migrates, validates, and how copy-not-move gives a free
   rollback; the re-migration foot-gun (`nexus-1sx01`) and version-pin handshake.

Documentation-first; the only code in scope is closing small surfaced gaps
(uninstall CLI completeness being the prime candidate). Anything larger spins
out as its own tracked bead/RDR rather than expanding this one.

## Approach (phased)

1. **Inventory & gap audit.** Cross-walk every lifecycle surface (the RDRs and
   CLI commands above, the `daemon_uninstall` MCP tool, the service-registry
   lifecycle in `src/nexus/daemon/service_registry.py`) against the four
   lifecycle stages. Produce a coverage matrix: what's documented, what's
   code-only, what's missing. Identify CLI gaps (esp. uninstall).
2. **Author the lifecycle doc.** Write the consolidated operations doc with the
   state model + the three operational walkthroughs (install / uninstall /
   upgrade). Link out to the authoritative RDRs rather than duplicating design
   rationale.
3. **Close surfaced CLI gaps.** Implement the small gaps the audit finds — most
   likely a first-class `nx` uninstall command with the `daemon_uninstall`
   tool's completeness (autostart unit removal, daemon stop, marker clear,
   data-preserving default + `--remove-data`). TDD; nexus-only.
4. **Wire into the docs surface.** Reference the new doc from README,
   `docs/cli-reference.md`, and the 6.0.0 release notes; ensure discoverability.

## Alternatives considered

- **Leave it scattered, rely on RDRs.** Rejected: RDRs are design records, not
  operator manuals; new users and supporters should not reverse-engineer the
  lifecycle from a dozen design docs.
- **Fold into RDR-166 (one umbrella).** Rejected per the scope decision
  (2026-06-22): the docs/lifecycle work is nexus-only and can land fast, while
  the managed-consumer journeys are cross-repo (conexus-coordinated) and slower.
  Coupling them would gate the docs on the cross-repo half.

## Consequences

- A single discoverable lifecycle doc; install/uninstall/upgrade questions stop
  being archaeology.
- A complete CLI teardown story (closing the MCP-only `daemon_uninstall` gap).
- Strengthens the 6.0.0 release: the migration-capable release ships with a
  legible operations story, not just a migration tool.

## Open Questions

1. **Doc home & shape** — `docs/operations.md` vs `docs/lifecycle.md` vs a
   section in an existing doc? One page or a small set?
2. **Uninstall CLI gap** — does a complete `nx` uninstall already exist, or only
   the `daemon_uninstall` MCP tool? (Audit in Phase 1.) If a gap, is the fix in
   scope here or a separate bead?
3. **Where does the agent state diagram live** — inline in the doc, or a shared
   asset referenced by both this doc and `docs/architecture.md`?
4. **Relationship to RDR-149** — the service-registry lifecycle is the
   authoritative mechanism; how much to restate vs link?

## Research Findings

_(to be populated via `/conexus:rdr-research`)_

Initial grounding (2026-06-22, pre-research):
- `nx guided-upgrade --service-url` verifies an already-running service instead
  of provisioning (`src/nexus/commands/guided_upgrade_cmd.py:51,127`).
- Uninstall is currently the `daemon_uninstall` MCP tool: removes the OS
  autostart unit (LaunchAgent/systemd user unit), stops the daemon, clears the
  first-run marker; `--remove-data` also deletes `~/.config/nexus/`.
- Install surfaces: `nx init --service`, `nx daemon service install-binary <tag>`
  (cold-acquire + cosign-verify the native binary + relocatable PG bundle).
