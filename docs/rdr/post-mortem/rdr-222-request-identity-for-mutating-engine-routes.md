# RDR-222 Post-Mortem: Request Identity for Mutating Engine Routes

**Drafted** 2026-10-31 for the close · **Accepted** 2026-10-18 · **Epic** nexus-yye8l (every child closed at drafting; nexus-k9kr9 open as a scoped-out follow-up) · **Shipped** engine-service-v0.1.137 (Phase 0), v0.1.162 (Phase 1 and 1a), v0.1.173 (ledger retention for a tenant with no token); conexus 7.83.1 (client, pins 0.1.162) and 7.91.0 (pins 0.1.173).

## What the RDR set out to do

A mutating engine call that ends in a gateway error (a 502, 503 or 504 from the edge) leaves the client unable to tell whether the engine committed it, is still running it, or rolled it back. For a garbage-collection sweep that means the client either resends and risks a second sweep, or gives up and reports a failure that may have succeeded (nexus-ll31n). The RDR proposed a client-minted `Idempotency-Key` per request, a durable request ledger in the engine that records each keyed request's state and response, and a status route, `GET /v1/requests/<id>`, so the client can ask after an ambiguous outcome. Phase 0 instrumented the raced-embed case first; Phase 1 keyed the bounded sweeps; Phase 2 would key the content writes; Phase 3 was a separate run fence for document-scoped writes.

## Implementation status

Implemented for Phases 0, 1 and 1a. A keyed bounded expire-quarantine sweep sent through the edge to the cloud engine read back `committed` from the status route on 2026-10-21 (request `3e74554c-c197-4525-8bec-bbfa4083db88`). The edge relays the key, `Idempotency-Replayed` and `Retry-After` unchanged: the 200 and replay paths were probed live on 2026-10-21, and the 409 and 503 paths are pinned by conexus integration tests (`EdgeProxyHandlerIT`, conexus `a2398338e`) because a live 409 or 503 needs an in-flight conflict or a held sweep gate. That relay is a standing dependency the client cannot see: on `unknown` the client resends, which is safe only if the engine saw the key, and RDR-007 A5's CloudFront policy once dropped it (conexus-c4i2). A scheduled re-probe is nexus-t0ms7.

Phase 2 (keyed content writes) stays deferred: the double-embed it was meant to prevent measured 11 raced embeds against 14,693 chunks (0.075%) on 2026-10-18, and nexus-wvek6 closed as not material. Phase 3 (run fence) was closed by Sam on 2026-09-28 as design of record only. Still unkeyed, by design: `/gc/quarantine-restore` (nexus-k9kr9), the unbounded and floor-only sweep forms (no statement bound, so no lease covers them), and the catalog-client mutating routes.

## Implementation vs. plan

### As planned

- Client-minted keys on the bounded sweeps; the engine ledger with tenant row-level security; the status route answering `committed`, `in_flight`, `rolled_back` or `unknown`; replay of a committed response with `Idempotency-Replayed: true`; a 409 with `Retry-After` for a request already in flight; a resend cap of 3.
- The guarantee as shipped is narrower than the RDR's "exact" states: `rolled_back` is not final for the key (a same-key resend can take the row over and commit), and a status read can report `rolled_back`, in a window narrower than one statement, for a marker whose UPDATE had already scanned the row. The takeover re-checks holder and state, so nothing runs twice. The guarantee to rely on is that resending with the same key is safe.
- Engine before client (choreography (a)): the client release raised `REQUIRED_ENGINE_VERSION` to the engine tag that carries the ledger.

### Diverged

1. **The bounded expire-quarantine form was missing, so it was added after accept as Amendment A1** (nexus-1579m, Sam 2026-10-21). The accepted RDR keyed the bounded sweeps that existed; expire-quarantine had no bounded form, so it could not be keyed and the gateway-ambiguity case stayed open for it, and `nx index repo`'s prune calls it on every run. A1 added `gc_expire_quarantine_bounded` and keyed it, riding v0.1.162 with Phase 1, recorded under the existing acceptance without a re-gate. *Drift class: scope underestimation.*

2. **Retention visited only tenants that hold a service token.** The ledger's retention pass enumerated tenants through the reaper's list (the default tenant plus tenants with a token), so a tenant that lost its last token kept its ledger rows forever. The engine review found it (nexus-yye8l.20 item 6); it was declined for Phase 1 as one row per sweep and later built anyway as a registry table filled by a BEFORE INSERT trigger, with a backfill (nexus-yye8l.21, v0.1.173). The first live pass visited 5 tenants. *Drift class: missing Day 2 operation.*

3. **Design gaps closed at accept, not in the RDR text until the close** (added then as Technical Design's As built list). The accept dispositions (nexus-zxgld, nexus-icdmw) changed the design in the beads, which the epic says win over the RDR: no `doc_id` column, uuid4 keys instead of UUIDv7, `finished_at = clock_timestamp()` with a `(tenant_id, finished_at)` index, a 95 s lease, the echo precondition dropped. Claim exhaustion answers 503 `request_claim_contended` rather than 422 (nexus-yye8l.20). *Drift class: under-specified architecture.*

4. **The edge check for 409 and 503 was done by test, not live probe** (nexus-yye8l.10). The bead asked for five live probes. Three passed and a fourth passed on 200 only; the remaining header relay on 409 and 503 needs a held conflict on production, so conexus pinned it in its edge integration tests instead. *Drift class: unvalidated assumption (accepted as pinned, not probed).*

## What to carry forward

- A sweep that has no bounded form cannot be keyed; check that every route the RDR names has the statement bound the lease depends on before accepting.
- Any per-tenant maintenance pass needs a tenant enumeration that covers tenants with data but no credential. Ask what the list admits, not only who it is for.
- Changeset numbers collide across concurrent engine branches; merge develop and renumber before pushing.
