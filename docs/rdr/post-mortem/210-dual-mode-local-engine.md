# RDR-210 Post-Mortem: One Local Engine Serves Both Embedders

**Closed** 2026-10-26 · **Accepted** 2026-10-18 · **Epic** nexus-ddmfg (24 step beads and the accept-time residuals, all closed; nexus-4lqi7 open on purpose) · **Shipped** engine-service-v0.1.161 (dual embedder behind a version fence), v0.1.164 (Phase 6 routes and the cutover), v0.1.165 (nexus-6zchp, nexus-3va7p); client 7.83.0 onward

## What the RDR set out to do

A local install could use Voyage, but only as a switch for the whole engine. Setting a Voyage model made the engine refuse every existing bge-768 collection. Sam's requirement (2026-09-15) was that local mode serves both at once, so a switch strands nothing. The RDR proposed one dual engine that routes by the collection's model family, siblings found by the catalog row tuple and never by name, a re-embed job to move a collection between families, doctor rows, a stated egress allowlist with tests that hold it, and a keyed gate in the release battery.

## Implementation status

Implemented in six phases. The dual gate runs as the fifth leg of the release battery, 54 checks against the jar and 51 against the v0.1.164 native build, with Voyage spend under 3,000 tokens. The end-of-RDR phase-review pass (T2 `nexus_rdr/210-phase-review-gate-end-of-rdr-2026-10-25`) found 23 of 24 steps covered and one partial. The close critique returned `partial` with 0 Critical and 3 Significant findings, all text; they were fixed before this close (T2 `nexus_rdr/210-substantive-critique-2026-10-26`).

## Implementation vs plan

### As planned

The dual router and the supervisor's key and profile plumbing, the row-tuple resolver, the re-embed verb with its job table and doctor rows, the cutover, the egress census and witnesses, and the gate script with an absent key as a FAIL.

### Diverged

1. **Phase 6 shipped inside v0.1.164, not in its own paired tag.** The RDR said the client half would be paired with a dedicated engine tag. The routes went out with the cutover, and the client needed nothing past that tag. `REQUIRED_ENGINE_VERSION` moves to v0.1.165 with the client release after the next engine cut (nexus-j67ix). Closed: the RDR text now says so.
2. **The Rdr204 journey test still uses a fake local member** (nexus-9nndg). It never drives a production Voyage embed through the router. The dual gate script covers the live path. Open as a known limit.
3. **Egress guard: JDBC and Hikari pool URLs, and the effect of `Main.goOffline`, are not covered.** Sam deferred both on 2026-10-25 (nexus-2ykf1) to nexus-4lqi7. Open.
4. **The register-at-first-batch cleanup was withdrawn** after review showed it was a data-loss path, and re-filed as nexus-6zchp with a transactional emptiness check. It ships in v0.1.165.
5. **The privacy policy's stop-Voyage remedy was wrong.** It said setting `local.embed_model` back to bge stops Voyage traffic. Under the dual engine the supervisor passes the key whatever that setting says; only removing the key stops it. Found by the close critique, fixed in `docs/privacy-policy.md`.
6. **The RDR body trailed the code.** Fourth versus fifth battery leg, citations to a removed rename branch (nexus-avjrb), bead states, and the egress test counts. Fixed at close.

## Drift classification

- Divergence 1: unvalidated assumption (that a paired tag was needed).
- Divergence 2: deferred critical constraint.
- Divergence 3: deferred critical constraint.
- Divergence 4: missing failure mode.
- Divergence 5: internal contradiction (policy against the supervisor's plumbing).
- Divergence 6: under-specified architecture, in the sense that nothing re-read the RDR against the code between phases.

## Lessons

A sentence in a user-facing policy that states a remedy needs the code path behind it read, not only the design. The first privacy edit on 2026-10-26 fixed one sentence of a paragraph and left its neighbour contradicting the shipped plumbing; the close critique found it. Read the whole paragraph against the supervisor when one sentence of it changes.
