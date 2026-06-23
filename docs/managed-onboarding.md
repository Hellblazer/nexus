# Managed-service onboarding (greenfield)

This is the no-prior-install path: point `nx` at the hosted Conexus managed
service and reach a working search/store state with **no local service stack and
no migration**. (Already running a local Chroma install you want to move to the
managed service? That is the migration path, `nx migrate-to-service` — see
[migration-runbook.md](migration-runbook.md), not this page.)

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

# …or per-shell environment (takes precedence over config.yml):
export NX_SERVICE_URL=https://api.conexus-nexus.com
export NX_SERVICE_TOKEN=<your-bearer-token>
```

Resolution order is **env first, then `config.yml`** for both values, so an
exported `NX_SERVICE_URL` overrides a persisted one. The token is sent as
`Authorization: Bearer <token>`; treat it as a secret.

Tell `nx` to use the service backend:

```bash
export NX_STORAGE_BACKEND=service
```

> The storage-backend selector is env-only today (`config.yml` persistence for
> it is a tracked follow-up); the endpoint + token above persist via `nx config`.
> Put the `export` in your shell profile, or run with it set, until that lands.

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
nx store "my first managed note" --collection knowledge__<owner>__voyage-context-3__v1
nx search "first note"
```

Embeddings are computed server-side (the managed service is Voyage-mode), so no
local embedder or API key is needed on your machine for search/store.

## Token lifetime + rotation

The bearer is opaque and does not expire on a timer; rotation is
revoke-and-reissue, operator-side. If a call returns `HTTP 401`, re-fetch a
fresh token from the operator and re-run `nx config set service_token` (or
re-export `NX_SERVICE_TOKEN`).

## Scope note

One token maps to one tenant. `pgvector`-to-managed cross-deployment migration is
a documented limitation, not part of this journey (tracker: nexus-wm3t5); this
page covers greenfield managed onboarding and the Chroma-source migration path
covers local-to-managed.
