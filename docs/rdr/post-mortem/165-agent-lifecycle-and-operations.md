# Post-mortem: RDR-165 — Agent Lifecycle & Operations

**Closed:** 2026-06-24 · **Type:** Documentation · **Outcome:** Implemented as designed, no divergences.

## What shipped

One authoritative operator-facing lifecycle/operations document plus the code that
closed the surfaced CLI gap:

- `docs/operations/agent-lifecycle.md` — the single source of truth for the agent
  model (engine-service daemon + `nx` CLI + storage tiers + RDR-149 lease lifecycle),
  the state model (uninstalled → installed → provisioned → running → upgrading →
  uninstalled), and the three operational walkthroughs (install / uninstall / upgrade).
- First-class **service-aware** `nx uninstall` (`src/nexus/commands/uninstall.py`,
  bead `nexus-eu4u4`): `service stop --with-pg` + autostart removal + marker clear +
  data-preserving default + `--remove-data`, wrapping `installer.uninstall_daemon` —
  closing the prior MCP-only `daemon_uninstall` gap.
- `config.unset_credential` (bead `nexus-a11ge`).
- Managed-only client teardown (bead `nexus-wigzi`, the RDR-165↔166 seam).
- README / `docs/cli-reference.md` wiring (bead `nexus-8lqrb`).

All 8 children of epic `nexus-kecp7` closed; gate PASSED 2026-06-22 (0 critical);
both phase boundaries stacked-reviewed (code-review-expert + substantive-critic).

## What's worth remembering

1. **Ledger drift between T2 and the RDR frontmatter.** RDR-165 was gated and
   accepted in T2 on 2026-06-22 (`status: accepted`, gate PASSED), but the file
   frontmatter was never flipped from `draft`. At close time the `rdr-close`
   preamble read the stale file status and BLOCKED, even though the lifecycle had
   genuinely been completed. This is the same bifurcated-ledger class that
   `nexus-7h69i` reconciled for RDR-154/157. The accept step must flip the *file*
   frontmatter, not only write T2 — the file is what the close gate reads. Treat
   a draft-frontmatter-but-accepted-in-T2 mismatch as drift to reconcile, not as
   a skipped gate to force past.

2. **The deliverable is release-gated, but the RDR is not.** The lifecycle doc
   wires into the 6.0.0 release notes, and `nexus-eu4u4` is a declared 6.0.0
   blocker (`blocks nexus-y5avl`). 6.0.0 itself is frozen behind `nexus-luxe6`
   (develop unreleasable until the install-collapse + migration-orchestration
   story lands). The RDR's work — doc + CLI — is complete and merged to develop;
   closing the RDR records that the design is realized. The release shipping is a
   separate, still-held gate. An RDR being done does not imply its release is
   unblocked.

## Follow-ons (not gates)

- Managed→managed cross-deployment migration is documented-unsupported and tracked
  under RDR-166 (`nexus-wm3t5`, P3).
- The 6.0.0 release remains held by `nexus-luxe6`; `nexus-eu4u4` is satisfied and
  ready when the release window opens.
