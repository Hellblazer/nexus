# RDR-174 Post-Mortem: Unified nx init and Service-Supervisor Lifecycle

**Closed:** 2026-06-29 · **Reason:** implemented · **Type:** Architecture
**Epic:** nexus-423yt (closed, 18/18) · **Author:** Hal Hildebrand

## Outcome

The install collapsed to one command. `uv tool install conexus` →
`nx daemon service install-binary <tag>` → `nx init` now provisions Postgres +
pgvector, fetches the bge-768 ONNX, starts the native nexus-service, and offers
to register the OS autostart unit for reboot-persistence. The two-init-mental-
models gap (embedder picker vs heavy `--service` provision), the vestigial
T2-daemon step, and the reboot-persistence gap are all closed.

## §Approach items (all delivered)

| # | Item | Closing bead | Notes |
| --- | --- | --- | --- |
| 1 | Mode detection in `nx init` (NX_LOCAL > service_url; remove `_auto_service`) | nexus-0aibx | P1.1 |
| 2 | Managed path (RDR-166 wizard + probe) | nexus-r2auz | P1.2 |
| 3 | Local path (`nx init` provisions; embedder-picker removal) | nexus-5kdia | P1.3 |
| 4 | Service-supervisor autostart | nexus-3pfj0 (+ y2yj6 P2.1 unit, exfns P2.2 PG-no-op, 1brzs P2.3 subsumed) | re-scoped twice — see below |
| 5 | `--service` deprecation notice | nexus-dnkfj | P3.1 |
| 6 | Remove the T2-daemon step | nexus-ms7t3 | P3.2 — delivered as a regression assertion |
| 7 | README + getting-started + cli-reference | nexus-dqxz9 | P4.1 |

Reviews: P1 (dsagc/4piz7), P2 (8yl22/d5xgw), P3 (gue10), P4 (dxt5a), and the
P-GATE (3z1a0) all passed.

## Divergence: §4 re-scoped twice at implementation time

The single notable area. §4 (service-supervisor autostart) was the part of the
RDR whose gate-locked text most diverged from what the codebase actually
required. Both re-scopings are recorded as in-body breadcrumbs in §4 (the RDR
keeps its accepted status; the text was annotated, not silently changed).

1. **PG boot-ordering — VERIFIED NO-OP (P2.2, nexus-exfns).** The gate text
   (critic SIG-2/SIG-3) assumed an external Postgres to order the unit against
   (`After=postgresql.service`, a macOS PG-readiness wrapper). Implementation-time
   verification showed the supervisor starts its OWN nx-owned PG cluster as step 1
   of startup, with boot-safe binary discovery — there is no external
   `postgresql.service` in any mode. Both deltas were wrong; the only real boot
   need is `After=network.target` (already present). Delivered as a template
   comment + a boot-robustness regression test.

2. **Supervisor handoff/ordering — SUPERSEDED by RDR-175 (P2.3/P2.4).** The P2.3
   analysis surfaced that the in-process respawn layer duplicated the new OS-init
   watchdog and was the double-spawn root cause. Rather than smuggle a
   reviewed-contract retirement into a phase task, this was spun into its own
   RDR-175 ("OS-Init as the Single Process Watchdog"). P2.3 (nexus-1brzs) was
   subsumed by RDR-175's MVV; the decide-autostart-first ordering requirement was
   recorded on P2.4 (nexus-3pfj0) and shipped there. RDR-175 closed (partial)
   before this RDR's P2.4 landed.

P3.2 (item 6) is a secondary, minor divergence: the T2-daemon "removal" was
delivered as a regression assertion, because verification found `nx init` never
registered a T2 unit in code (the step was a README instruction). The default
all-SERVICE config needs no SQLite T2 daemon; the `nx daemon t2 install` command
stays as an explicit opt-in (full deletion is RDR-158 P4).

## Deferred / follow-ups

- `uninstall_daemon()` targets the T2 unit on a service-tier cleanup (P2 review
  MEDIUM-1) — filed as a follow-up bug.
- The SQLite-T2-daemon-socket-model docs (`container-integration.md`,
  `contributing.md`, parts of `desktop-deployment.md`) still describe the
  pre-collapse architecture; updating them is RDR-158 (retire-SQLite-T2)
  territory, flagged on the docs-review bead nexus-dxt5a.

## Lessons

- **Verify gate-locked premises against the codebase at implementation time.**
  §4's PG-ordering deltas were locked from a model (external Postgres) the code
  never matched. A premise that names infrastructure is worth confirming the
  infrastructure exists before building ordering around it.
- **A finding bigger than the current phase task earns its own RDR.** The
  in-process respawn retirement was a reviewed-contract change; spinning RDR-175
  (with its own §Decision, gate, and reviews) was the right call over folding it
  into a P2 task.
- **"Remove X from the docs/init" can be a test-only change** when X was never in
  the code to begin with — but say so explicitly (assertion, not deletion) so the
  phase-gate does not read it as silent scope reduction.
