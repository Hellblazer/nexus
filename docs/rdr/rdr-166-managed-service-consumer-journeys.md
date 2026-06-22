---
title: "Managed-Service Consumer Journeys: Greenfield Onboarding and Local→Managed Migration to conexus-nexus.com"
id: RDR-166
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-22
related_issues: [nexus-w5v8j, nexus-vwvv5.12, nexus-gilf2, nexus-jioh1, nexus-luxe6]
related: [RDR-001, RDR-152, RDR-155, RDR-157, RDR-159, RDR-162]
---

## Problem Statement

A user can install and run nexus locally, and `nx guided-upgrade` migrates a
local pre-upgrade (Chroma) install onto a *local* PG+pgvector service. But the
two journeys that connect a user to the **managed** service at
`api.conexus-nexus.com` are not covered, validated, or documented end-to-end:

1. **Greenfield managed onboarding** — a brand-new user, no prior install,
   wants to use the managed service directly: configure `nx` against the
   managed endpoint with a tenant token, no local service, no migration. The
   client plumbing exists (`nexus-vwvv5.12`: managed-endpoint config + HTTP
   capability probe), but the first-run *journey* is not a documented, tested
   flow.
2. **Local→managed migration** — an existing local user moves their data onto a
   managed tenant. Mechanically this is `nx guided-upgrade --service-url
   https://api.conexus-nexus.com` with `NX_SERVICE_TOKEN`: detect local Chroma
   → verify the remote (voyage capability + version) → cross-model re-embed
   (local bge-768/minilm-384 → the cloud's voyage models, server-side) → ETL
   upsert into the tenant's pgvector → validate; copy-not-move keeps local
   Chroma as a free rollback. The building blocks exist (and `nexus-gilf2` just
   made the cross-model target voyage-aware — a prerequisite), but there is **no
   tenant-onboarding handoff, no E2E rehearsal/regression coverage, and an
   un-validated cross-model→voyage leg against a live tenant.**

Gaps identified 2026-06-22:
- **Tenant onboarding.** No path to obtain a tenant + `NX_SERVICE_TOKEN`;
  account/token issuance is conexus-owned (RDR-001) and not tracked as an
  end-to-end journey in the `nexus-w5v8j` consumer epic.
- **No E2E validation.** Rehearsal harnesses cover local→*local*-service and
  cold-acquire; `--guided --with-cloud` is explicitly rejected as incoherent.
  Nothing exercises local→hosted, so gilf2's cross-model→voyage leg is
  unvalidated against a live tenant.
- **TLS/443 endpoint.** Migration legs resolve via HOST/PORT (the `nexus-qvemn`
  contract), not `NX_SERVICE_URL`; an `https://…:443` managed endpoint flowing
  through `resolve_service_config` needs confirming.
- **Cost / idempotency.** A full voyage re-embed of every chunk's text is
  network-bound and billed to the operator key (`nexus-jioh1`); re-runs re-copy
  at full cost (`nexus-1sx01`).
- **pgvector→managed (out of scope).** A user already on the *local* PG service
  has no Chroma source; cross-deployment pgvector→managed migration is **not**
  built. Per the 2026-06-22 scope decision this is **documented as a known
  limitation** with a follow-on bead, not built here.

## Decision

Make both managed-service consumer journeys first-class, validated, and
documented, under an **operator-provisioned token** model (the nexus CLI
*consumes* a tenant `NX_SERVICE_TOKEN` issued out-of-band by the conexus
operator; the CLI does not drive self-serve signup). Specifically:

1. **Greenfield onboarding.** A documented + tested first-run path that points
   `nx` at the managed endpoint with a tenant token, runs the capability probe,
   and reaches a working search/store state with no local service and no
   migration.
2. **Local→managed migration.** Harden and validate the
   `guided-upgrade --service-url` path against a managed tenant: confirm the
   TLS/443 endpoint resolution, validate the cross-model→voyage leg E2E, add
   rehearsal/regression coverage, and surface the cost/idempotency UX.
3. **pgvector→managed.** Document as an explicit known limitation; file a
   follow-on for the cross-deployment migration if demand warrants.

The managed-service half (tenant + token issuance, the onboarding API) is
**conexus-owned (RDR-001)**; this RDR captures the *nexus-side* consumer
requirements and the documented journeys, and the cross-repo asks are relayed
to the conexus instance (the T2 bus is passive; Hal relays).

## Approach (phased)

1. **Consumer-requirement + handoff design.** Pin the operator-provisioned
   token contract (where the token comes from, how `nx` consumes it, failure
   modes). Enumerate the conexus-side asks (tenant provisioning, token issuance,
   onboarding doc) and relay them; mirror each as a child under `nexus-w5v8j`.
2. **Greenfield onboarding journey.** Document + test the no-prior-install path
   against the managed endpoint (config, capability probe fail-loud, first
   search/store). Confirm `nx init`/config shape for a managed-only client.
3. **Local→managed migration hardening.** Confirm `resolve_service_config`
   handles the `https://…:443` managed endpoint; validate the cross-model→voyage
   ETL leg E2E against a (throwaway) managed tenant; add a rehearsal target;
   surface cost + the re-migration foot-gun in the UX and notes.
4. **Documentation.** Both journeys documented (links from RDR-165's lifecycle
   doc); pgvector→managed limitation stated explicitly with the follow-on bead.

## Alternatives considered

- **Self-serve onboarding via the nexus CLI.** Rejected (2026-06-22): a large
  new client surface dependent on a conexus self-serve API that may not exist;
  operator-provisioned token matches the current per-tenant model + vwvv5.12
  plumbing.
- **Build pgvector→managed now.** Rejected (2026-06-22): significant new ETL
  source + cross-deployment recall-parity validation; document as unsupported
  and defer to keep the RDR shippable.
- **Treat as incidental capability, no RDR.** Rejected: `--service-url` against
  the managed endpoint "works" mechanically but with no onboarding half, no E2E
  gate, and an unvalidated cross-model leg — exactly the silent-partial-coverage
  class this project guards against.

## Consequences

- Two validated, documented managed-service journeys; the managed offering
  becomes a real consumer path, not an incidental capability.
- Explicit, cross-referenced asks to conexus (RDR-001) instead of lost
  cross-repo requirements.
- A documented pgvector→managed limitation (honest non-coverage) with a tracked
  follow-on.

## Open Questions

1. **Token issuance UX** — `NX_SERVICE_TOKEN` env is the consume point
   (research). **Remaining:** the `nx config` ergonomics for setting
   `NX_SERVICE_URL` + `NX_SERVICE_TOKEN` cleanly, and how the operator delivers
   the token. (Cross-repo: conexus issuance side.)
2. ~~**Managed `nx init`**~~ — **ANSWERED (research):** greenfield reuses the
   `NX_SERVICE_URL`/`_resolve_endpoint` (https-capable) client + the vwvv5.12
   probe; no provisioning. Remaining work is the journey + config ergonomics,
   not a distinct init.
3. **E2E managed-tenant test fixture** — how to exercise local→managed against a
   throwaway managed tenant without touching the real `nexus` tenant (mirror the
   RDR-164 throwaway-tenant probe discipline). Open.
4. ~~**TLS/443 resolution**~~ — **ANSWERED (research):** the pre-gate /
   version-pin legs (`resolve_service_config`, http-only host/port) break on an
   https managed endpoint; the data path (`NX_SERVICE_URL`) does not. Fix =
   scheme-aware resolution for the pre-gate. (See Research Finding 2.)
5. **Cost guardrails** — should a managed migration estimate + confirm the
   voyage re-embed cost before proceeding (ties to `nexus-jioh1`)? Open
   (design choice for the gate).

## Research Findings

Endpoint + journey audit, 2026-06-22 (full detail: T2 `nexus_rdr/166-research-1`):

1. **Two endpoint-resolution paths; one is https-capable.**
   `http_vector_client._resolve_endpoint()` reads `NX_SERVICE_URL` (a **full URL,
   used verbatim — https-capable**) or the lease. The ETL data client
   (`migrate_cmd.py:153,170` `HttpVectorClient()`) and the steady-state managed
   client both use this. So the **migration data/upsert path and greenfield
   search/store are https-capable** via `NX_SERVICE_URL`.
   `service_endpoint.resolve_service_config()` returns `(host, port, token)` and
   callers build **`http://{host}:{port}` (hardcoded http)**; the guided-upgrade
   pre-gate / version-pin legs use this.
2. **Q4 (TLS/443) — narrow, precise gap.** The guided-upgrade **pre-gate /
   version-pin legs are http-only.** qvemn (#1284) pins HOST/PORT from the
   verified `--service-url`; for `https://api.conexus-nexus.com:443` that yields
   `http://api.conexus-nexus.com:443` → TLS break. So a managed-https migration's
   *data* path works but its *pre-gate* breaks. Fix = scheme-aware
   `resolve_service_config` (carry https) or route the managed pre-gate through
   the URL path. Bounded, not a rewrite.
3. **Q1 (token surface).** `NX_SERVICE_TOKEN` env is the consume point for both
   paths; operator-provisioned = user sets `NX_SERVICE_URL` + `NX_SERVICE_TOKEN`.
   `health._check_managed_service_probe` runs **only** when `NX_SERVICE_URL` is
   explicitly set — never default-probes the public endpoint (good isolation);
   vwvv5.12 = `nexus.db.managed_endpoint.probe_managed_service(base_url)`.
4. **Q2 (greenfield) — largely plumbed.** Steady-state client (https-ok) +
   capability probe + fail-loud already exist (vwvv5.12). The gap is the
   documented first-run **journey** + `nx` config ergonomics, not transport —
   **lower code risk than migration.**
5. **Readiness asymmetry.** Greenfield ≈ plumbed (needs journey + docs + config
   ergonomics). Local→managed migration: data path https-ready, but needs the
   pre-gate scheme fix (#2) + E2E validation of the gilf2 cross-model→voyage leg
   against a throwaway managed tenant (Q3, mirror the RDR-164 throwaway-tenant
   discipline) + a cost guardrail (Q5, ties `nexus-jioh1`).
6. **pgvector→managed.** ETL source is always Chroma (driver opens local Chroma +
   Chroma Cloud read legs) → no pgvector→managed path; stays a documented
   limitation per the scope decision.
