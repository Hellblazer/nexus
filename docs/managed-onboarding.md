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

## Migrating an existing local install to the managed service

If you already have a local (Chroma or local-service) install and want to move
its data to the managed service, that is the migration journey, not greenfield:

```bash
export NX_SERVICE_TOKEN=<your-bearer-token>          # the operator-provisioned tenant token
nx guided-upgrade --service-url https://api.conexus-nexus.com
```

`guided-upgrade` health-gates and version-pins the managed endpoint, then drives
the ETL (detect → migrate T2/catalog/T3 → validate → unlock), copy-not-move (your
local source is the rollback origin and is never modified). Notes:

- **Cost:** collections that change embedding model are re-embedded through the
  managed Voyage key (billed); `guided-upgrade` shows an estimate-and-confirm
  prompt before proceeding (RDR-166). Same-model voyage collections are copied
  vector-for-vector with no re-embed (and no charge).
- **TLS:** the managed `https://…:443` endpoint is handled end-to-end (RDR-166).
- See [migration-runbook.md](migration-runbook.md) for the full migration detail.

### Known limitation: pgvector → managed is not supported

Moving an *already-on-pgvector* local-service install to the managed service
(pgvector → managed, a cross-deployment data move) is **not supported** — there
is no `pg_dump`/restore path across deployments in `nx`. Supported migration
origins are a legacy Chroma install or a local-service install via
`guided-upgrade`. The pgvector→managed path is tracked as a documented follow-on
(nexus-wm3t5); for now, a pgvector-local user who wants managed re-indexes from
source against the managed endpoint.

## Scope note

One token maps to one tenant. This page covers the two managed consumer journeys:
greenfield onboarding (above) and migrating a local install to managed
(`guided-upgrade --service-url`). The pgvector→managed cross-deployment move is
the documented limitation noted above.
