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
   default and an explicit `--remove-data` escalation. Close the CLI gap so the
   `daemon_uninstall` MCP capability has a first-class `nx` command equivalent
   (bead **nexus-eu4u4**), and — crucially — make it **service-aware**: it must
   stop the engine-service/PG (`nx daemon service stop --with-pg`), not just
   `nx daemon t2 stop`.
   - **3.a Managed-only client teardown** (bead **nexus-wigzi**, the RDR-165↔166
     seam). A user who followed RDR-166's greenfield onboarding has a
     managed-only config (`NX_SERVICE_URL`/`NX_SERVICE_TOKEN`, no local
     service/PG). The same `nx` uninstall must handle this case: clear the
     managed config + cached probe state, **skip** `service stop --with-pg`,
     **skip** data-wipe (data is remote). RDR-165 owns this because it owns the
     CLI uninstall surface.
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
3. **Close surfaced CLI gaps.** Implement a first-class **service-aware** `nx`
   uninstall (bead **nexus-eu4u4**): `service stop --with-pg` + autostart removal
   + marker clear + data-preserving default + `--remove-data`, wrapping
   `installer.uninstall_daemon`. Plus the managed-only client teardown
   (bead **nexus-wigzi**, Phase 3.a). TDD; nexus-only.
   - **Release-timing decision (OWNER, PENDING).** 6.0.0 is the migration-capable
     release that instructs every user to install the PG + engine-service stack.
     Shipping install with no clean CLI uninstall is the asymmetry this RDR
     closes. **Decision needed:** is `nexus-eu4u4` a **6.0.0 blocker**
     (`blocks nexus-y5avl`) or a **6.0.1 fast-follow**? Recommendation: 6.0.0,
     since mandating a service install without a teardown is exactly the
     "cleanup later" failure mode. To be confirmed by the owner before gate.
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

1. ~~**Doc home & shape**~~ — **ANSWERED (research):** `docs/operations/agent-lifecycle.md`
   (the `docs/operations/` dir already exists). One page, state diagram inline.
2. ~~**Uninstall CLI gap**~~ — **ANSWERED (research):** gap is real and deeper than
   stated — the complete teardown is MCP-only, and the canonical `uninstall_daemon`
   does not stop the engine-service/PG. In scope here as a tracked code phase
   (service-aware `nx` uninstall wrapping `uninstall_daemon` + `service stop
   --with-pg`).
3. ~~**Agent state diagram home**~~ — **ANSWERED (research):** inline in
   `docs/operations/agent-lifecycle.md`.
4. ~~**Relationship to RDR-149**~~ — **ANSWERED (research):** link, don't restate;
   RDR-149 is the authoritative lease/registry mechanism.

_Open Questions resolved by the 2026-06-22 gap audit. Tracked beads:
**nexus-eu4u4** (service-aware uninstall), **nexus-wigzi** (managed-client
teardown, Phase 3.a). One owner decision remains before gate: is `nexus-eu4u4`
a 6.0.0 blocker or a 6.0.1 fast-follow (see §Approach Phase 3)._

## Research Findings

Gap audit, 2026-06-22 (full detail: T2 `nexus_rdr/165-research-1`):

1. **Uninstall CLI gap — CONFIRMED.** The complete teardown logic exists as
   `nexus.daemon.installer.uninstall_daemon(confirm, remove_data)` (autostart
   unit + best-effort daemon stop + first-run marker; `remove_data` wipes
   `nexus_config_dir()`), but is reachable **only** via the MCP `daemon_uninstall`
   tool. There is no `nx` CLI wrapper. The CLI's `nx daemon t3 uninstall
   --autostart` / `nx daemon t2 … uninstall` are **autostart-unit-only**, and the
   `nx daemon service` group has `start`/`stop`/`install-binary` but **no
   uninstall**. → Fix = a first-class `nx` uninstall wrapping `uninstall_daemon`.
2. **Service-era teardown depth — NEW.** `uninstall_daemon`'s
   `_stop_daemon_best_effort` runs only `nx daemon t2 stop`
   (`installer.py:241`); it does **not** stop the engine-service/PG (that is
   `nx daemon service stop --with-pg`). The canonical teardown predates the
   RDR-152/155 service stack. A complete 6.0.0-era uninstall must orchestrate
   `service stop --with-pg` + autostart removal + marker clear + optional data
   wipe. This bumps the code portion from "small" to a tracked phase.
3. **Install/uninstall asymmetry.** Install is rich (`nx init --service`
   provisions PG + fetches bge-768 ONNX + starts the service; `nx daemon service
   install-binary <tag>` cold-acquires + cosign-verifies binary + PG bundle);
   there is no matching complete teardown verb. RDR-165 closes the asymmetry.
4. **Doc home (Q1 ANSWERED).** `docs/operations/` already exists
   (`audit-membership-interpretation.md`, `t3-health.md`); the lifecycle doc
   lands as `docs/operations/agent-lifecycle.md` with the state diagram inline.
   No existing lifecycle/install/upgrade doc.
