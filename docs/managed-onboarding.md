# Managed-service onboarding (greenfield)

This is the managed journey, and the only one: point `nx` at the hosted Conexus
managed service and reach a working search/store state with **no local service
stack and no migration**. Managed onboarding is greenfield-only — tenants index
fresh against the endpoint. Migrating an existing install's data INTO the
managed service is retired (decision 2026-07-17): the one such migration that
ever happened completed in 2026-06, and no supported population remains. An
existing local install that wants managed re-indexes from source against the
managed endpoint.

The managed service is operator-provisioned: you receive a base URL and a
per-tenant bearer token out of band (the Conexus operator issues them; `nx` does
not self-serve signup or mint tokens). `nx` is purely a consumer of that pair.

## 1. Configure the endpoint + token

Two equivalent surfaces; `nx config` persists across shells, env vars are
per-shell and win when both are set.

```bash
# Persistent (written to ~/.config/nexus/config.yml, mode 0600):
nx config set service_url   https://api.conexus-nexus.com
nx config set service_token <your-bearer-token>

# …or per-shell environment (takes precedence over config.yml). This also
# selects the service backend, so all three exports belong together:
export NX_STORAGE_BACKEND=service
export NX_SERVICE_URL=https://api.conexus-nexus.com
export NX_SERVICE_TOKEN=<your-bearer-token>
```

Resolution order is **env first, then `config.yml`** for both `NX_SERVICE_URL`
and `NX_SERVICE_TOKEN`, so an exported `NX_SERVICE_URL` overrides a persisted
one. The token is sent as `Authorization: Bearer <token>`; treat it as a
secret.

> The storage-backend selector is env-only today (`config.yml` persistence for
> it is a tracked follow-up); the endpoint + token above persist via `nx config`.
> Put the `export NX_STORAGE_BACKEND=service` in your shell profile, or run
> with it set, until that lands.

## 2. Verify the endpoint (fail-loud capability probe)

```bash
nx doctor          # includes the managed-service probe when service_url is set
```

The probe runs **only** when a managed endpoint is configured (it never
default-probes the public endpoint). It hits the unauthenticated `/version`
handshake and fails loud on:

- **unreachable** (connect / TLS / DNS / timeout) — check `service_url` and connectivity;
- **incompatible** (non-200, or an `app_version` below the supported floor) — the
  endpoint may not be a Conexus managed service, or it is unhealthy.

A healthy probe confirms the URL, the TLS path, and that the service version is
compatible before you write anything. (The `/version` route is unauthenticated
by contract, so the probe works before the token is accepted; an **invalid or
expired token** surfaces at the first authenticated call as an actionable
`HTTP 401`.)

## 3. First store + search

With a healthy probe you are ready. There is no local index to build and no
migration to run:

```bash
# <owner>: your username or project name — any identifier works, it's just a namespace segment
echo "my first managed note" | nx store put - --title first-note --collection knowledge__<owner>__voyage-context-3__v1
nx search "first note"
```

Embeddings are computed server-side (the managed service is Voyage-mode), so no
local embedder or API key is needed on your machine for search/store.

## Token lifetime + rotation

The bearer is opaque and does not expire on a timer; rotation is
revoke-and-reissue, operator-side. If a call returns `HTTP 401`, re-fetch a
fresh token from the operator and re-run `nx config set service_token` (or
re-export `NX_SERVICE_TOKEN`).

## Migration into the managed service: retired

There is no supported data-migration journey into the managed service
(decision 2026-07-17). This section previously documented
`nx config set service_url` + `nx upgrade` as a managed-migration path; it was
design-derived and never exercised by any user other than the operator, whose
own migration completed in 2026-06. Managed onboarding is greenfield-only: an
existing install (local Chroma or local-service pgvector alike) re-indexes from
source against the managed endpoint. The historical record of the one completed
migration lives in [migration-runbook.md](migration-runbook.md).

## Scope note

One token maps to one tenant. This page covers the one managed consumer
journey: greenfield onboarding. Data migration into the managed service is
retired (above).
