# Post-mortem: RDR-166 — Managed-Service Consumer Journeys

**Closed:** 2026-06-24 · **Type:** Architecture · **Outcome:** Implemented as designed; one P3 limitation documented-and-deferred (not silent scope reduction).

## What shipped

The two journeys connecting a user to the managed service at `api.conexus-nexus.com`,
documented, tested, and with the surfaced correctness defect fixed:

- `docs/managed-onboarding.md` — greenfield managed onboarding (no prior install,
  `nx` configured against the managed endpoint with a tenant token) and the
  local→managed migration journey (`nx guided-upgrade --service-url …`).
- **Scheme-aware `resolve_service_config`** (`src/nexus/db/service_endpoint.py`,
  bead `nexus-n3bwh`) — a real correctness fix, not a confirm: the pre-gate /
  version-pin legs built `http://{host}:{port}` from a resolved `(host, port)`,
  turning an `https://…:443` managed endpoint into `http://…:443` and breaking TLS.
- E2E local→managed migration test against a throwaway managed tenant (`nexus-6hoa6`).
- Same-model vector passthrough — skip the Voyage re-embed when
  `source_model == target_model` (`nexus-hxry2`).
- Cost / idempotency UX guardrail — estimate-and-confirm before a Voyage re-embed
  (`nexus-cewad` / `nexus-d977b`).
- P4 documentation including the pgvector→managed known limitation (`nexus-fiyv9`).

14/15 children of epic `nexus-abnn8` closed; gate PASSED 2026-06-22 (0 critical);
greenfield journey and the n3bwh fix each stacked-reviewed (code-review-expert +
substantive-critic).

## What's worth remembering

1. **Same ledger drift as RDR-165.** Accepted in T2 on 2026-06-22 (gate PASSED),
   but the file frontmatter was never flipped from `draft`, so `rdr-close` BLOCKED
   on stale file status. Two RDRs accepted in the same session both drifted —
   this is a defect in the accept step, not a one-off. The accept path must flip
   the *file* frontmatter, not only write T2; the close gate reads the file.

2. **A documented limitation is a legitimate close state, not a partial.** The
   ETL source is always Chroma (the driver opens local Chroma + Chroma Cloud read
   legs); a user already on local PG service has no Chroma source, so
   pgvector→managed cross-deployment migration is **not built** — deliberately,
   per the 2026-06-22 scope decision. It is documented as a known limitation and
   tracked under `nexus-wm3t5` (P3), detached to a standalone tracker at close so
   the deferral survives the epic close. Build only if demand warrants (needs a
   new ETL source + cross-deployment recall-parity validation).

3. **RDR close vs cross-repo dependency.** The cross-repo conexus asks
   (`nexus-i67t3` / `nexus-hm389`, under the `nexus-w5v8j` consumer epic) are
   owner-deferred and held; they are carried *before* §Approach Phase 1, not as a
   gate on this RDR. The managed-consumer capability is post-6.0.0 and the RDR
   close is independent of `nexus-luxe6`.

## Follow-ons (not gates)

- `nexus-wm3t5` (P3) — pgvector→managed cross-deployment migration, documented-unsupported.
- Cross-repo conexus asks under `nexus-w5v8j` remain owner-deferred.
