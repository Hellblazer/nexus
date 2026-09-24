# Security Notes — nexus-service (RDR-152)

## Token Posture (RDR-152 Phase 5, bead nexus-gmiaf.32, shipped)

The Phase 1-4 bootstrap posture — a single shared `NX_SERVICE_TOKEN` where any
token holder could claim any tenant via the `X-Nexus-Tenant` header — is retired.
`AuthFilter` now resolves the presented bearer server-side: the token is SHA-256
hashed (`TokenHashing`) and looked up against the `service_tokens` registry;
missing, revoked, or expired is a 401. The matched row's `tenant_id` is
authoritative and the client-supplied `X-Nexus-Tenant` header is ignored (logged
at debug on mismatch) — every token, including the persistent root token, is
strictly tenant-bound, so no token can cross tenants. RLS (with `FORCE`) still
enforces the boundary at the DB layer underneath this.

An optional `X-Nexus-T1-Session` header is resolved the same way against
`session_tokens`: a minted, live row must belong to the resolved tenant (else
401) and its server-resolved session id is what session-scoped handlers use — a
client cannot supply an arbitrary session id and act as it. Deployment in
shared or multi-principal environments is no longer blocked on this file's
account.

## GUC / Pooler Constraint

`SET LOCAL` (GUC `is_local=true`) is transaction-scoped and safe under a
**transaction-mode** pooler. A session-mode pooler (e.g. PgBouncer in its default
mode) would leak the `nexus.tenant` GUC stamp to the next connection borrower.
v1 connects directly to local PostgreSQL with no interposing pooler. If PgBouncer or
an equivalent is ever added it **must** be configured in transaction mode.

## Token Comparison

Raw tokens are never compared in application code at all — `AuthFilter` hashes
the presented bearer with `TokenHashing#sha256Hex` and resolves it by an
indexed primary-key equality against `service_tokens` / `session_tokens`, so
there is no single-secret timing oracle to guard with a constant-time byte
comparison. (An earlier version of this file described a `MessageDigest.isEqual`
comparison; no code path uses it — the hash-and-index-lookup design above
replaced it.)
