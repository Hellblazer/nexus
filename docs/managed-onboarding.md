# Managed-service onboarding (greenfield)

This is the managed journey: point `nx` at the hosted Conexus managed service
and reach a working search/store state with **no local service stack**. The
first-time path is greenfield: tenants index fresh against the endpoint.
Carrying an existing install's data into the managed service is a separate,
unrehearsed hop made with the current client; it is laid out in
[Getting that data into the managed cloud](migration-runbook.md#getting-that-data-into-the-managed-cloud)
and at the end of [Getting Started](getting-started.md), and for content you
have the source of, re-indexing against the managed endpoint is the cheaper path.

The managed service is operator-provisioned: you receive a base URL and a
credential out of band (the Conexus operator issues it; `nx` does not
self-serve signup). The credential is one of two kinds. A static per-tenant
bearer token (`service_token`) is sent as is. A mint credential (`mint_token`
plus the tenant it is bound to, `mint_tenant`) is the kind managed users
usually hold: `nx` uses it to mint short-lived data tokens itself and sends
those, so you never handle a data token by hand. Hold one kind; see
[Configuration § Managed-Cloud Credentials](configuration.md#managed-cloud-credentials).

## 1. Configure the endpoint + token

Two equivalent surfaces; `nx config` persists across shells, env vars are
per-shell and win when both are set. `nx config init` asks which kind of
credential you hold and saves only that kind.

```bash
# Persistent (written to ~/.config/nexus/config.yml, mode 0600):
nx config set service_url   https://api.conexus-nexus.com
nx config set service_token <your-bearer-token>      # a static token, or:
nx config set mint_token    <your-mint-credential>   # a mint credential, with
nx config set mint_tenant   <its-tenant>             # the tenant it is bound to

# …or per-shell environment (takes precedence over config.yml):
export NX_SERVICE_URL=https://api.conexus-nexus.com
export NX_SERVICE_TOKEN=<your-bearer-token>          # or NX_MINT_TOKEN + NX_MINT_TENANT
```

Resolution order is **env first, then `config.yml`** for each key, so an
exported `NX_SERVICE_URL` overrides a persisted one. A static token is sent as
`Authorization: Bearer <token>`. The CLI prefers a saved `service_token` over a
mint credential when both are present. Treat every credential as a secret.

`NX_STORAGE_BACKEND=service` is not needed: `service` is the default and only
backend (RDR-158 retired the SQLite alternative — setting `sqlite` now hard-
errors rather than selecting anything). There is no backend to select.

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
# the collection is a bare subject area; the system names the full collection from it
echo "my first managed note" | nx store put - --title first-note --collection my-first-subject
nx search "first note"
```

Embeddings are computed server-side (the managed service is Voyage-mode), so no
local embedder or API key is needed on your machine for search/store.

## Token lifetime + rotation

A static bearer is opaque and does not expire on a timer; rotation is
revoke-and-reissue, operator-side. If a call returns `HTTP 401`, re-fetch a
fresh token from the operator and re-run `nx config set service_token` (or
re-export `NX_SERVICE_TOKEN`). A mint credential is renewed the same way
(`mint_token`), and the short-lived data tokens it mints are refreshed by `nx`
without any action from you.

## Migration into the managed service

`nx config set service_url` alone moves nothing. An existing install's data
reaches the managed service only through the exports and re-indexing described
in [migration-runbook.md](migration-runbook.md#getting-that-data-into-the-managed-cloud),
a plan that has not been rehearsed end to end; the page says what to check as
you go. For code, docs and RDRs, re-indexing from source against the managed
endpoint is the cheaper path.

## Scope note

One credential maps to one tenant. This page covers onboarding a client to the
managed service; moving existing data is the runbook above.
